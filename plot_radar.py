#!/usr/bin/env python3
"""
2026-1014
W. Wright
github.com/lidar532


Plot Geolux LX-80 radar logs (<stamp>-radar.txt.gz) with Bokeh.

The logs are the gzip-compressed CSV files written by geolux_tui.py:
    captured_at,epoch_seconds,actual_distance_m,average_distance_m,snr_db,temperature_c,raw_sentence

Static mode (default) renders a self-contained HTML page:

    ./plot_radar.py                                  # newest *-radar.txt[.gz] under ./data
    ./plot_radar.py 2026-1114-122308-radar.txt.gz
    ./plot_radar.py data/2026-1114/2026-1114-122308-radar.txt -o plot.html --no-open

Live mode starts a Bokeh server that tails the log file and refreshes the
plots while geolux_tui.py keeps appending to it:

    ./plot_radar.py --live 2026-1114-122308-radar.txt.gz --port 5006

Plain (uncompressed) logs from older runs are read as well.

Requires: bokeh and numpy (installed with mamba from environment.yml).
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import glob
import io
import os
import shutil
import statistics
import subprocess
import sys
import zlib


def _project_venv_python():
    return os.path.join(os.path.dirname(os.path.realpath(__file__)),
                        ".venv", "bin", "python")


def _reexec_with_project_venv():
    """Restart with the checkout's .venv when the current Python is incomplete."""
    venv_python = _project_venv_python()
    venv_dir = os.path.dirname(os.path.dirname(venv_python))
    if not os.path.isfile(venv_python):
        return False
    if os.path.abspath(sys.prefix) == os.path.abspath(venv_dir):
        return False
    os.execv(venv_python, [venv_python, os.path.realpath(__file__), *sys.argv[1:]])
    return True


try:
    import numpy as np
except ModuleNotFoundError as exc:
    if not _reexec_with_project_venv():
        raise SystemExit(
            "plot_radar.py needs numpy for the FFT panel.\n"
            "Create the project-local virtual environment with:\n"
            "  /usr/bin/python3 -m venv .venv\n"
            "  ./.venv/bin/python -m pip install -r requirements.txt\n"
            "or refresh a mamba environment with:\n"
            "  mamba env update -n radar -f environment.yml --prune"
        ) from exc

from version import REPO_URL, VERSION

try:
    from bokeh.io import output_file, save
    from bokeh.resources import INLINE
    from bokeh.layouts import column, gridplot
    from bokeh.models import ColumnDataSource, Div, HoverTool, Span
    from bokeh.plotting import figure
except ModuleNotFoundError as exc:
    if not _reexec_with_project_venv():
        raise SystemExit(
            "plot_radar.py needs bokeh and numpy for the plots.\n"
            "Create the project-local virtual environment with:\n"
            "  /usr/bin/python3 -m venv .venv\n"
            "  ./.venv/bin/python -m pip install -r requirements.txt\n"
            "or refresh a mamba environment with:\n"
            "  mamba env update -n radar -f environment.yml --prune"
        ) from exc

C_ACTUAL = "#1f77b4"
C_AVERAGE = "#ff7f0e"
C_SNR = "#9467bd"
C_TEMP = "#2ca02c"
C_SPECTRUM = "#8c564b"
C_WARN = "#d62728"

TOOLS = "pan,wheel_zoom,box_zoom,undo,redo,reset,save"

# The L1 FFT is computed after resampling onto a uniform grid.  A cap keeps
# very long (or gap-ridden) logs from allocating an enormous array.
SPECTRUM_MAX_SAMPLES = 1 << 17

# The LX-80 reports 15.000 as its no-echo sentinel: the raw amplitude drops to
# -4.743 and the sample would put a broadband spike into the FFT.  This is the
# default missing value for the spectrum; --remove-missing can replace it and
# also applies the filter to the time plots.
L1_SPIKE_VALUE = 15.0
SPIKE_TOLERANCE = 1e-9


# --------------------------------------------------------------------------
# log file reading
# --------------------------------------------------------------------------
DEFAULT_DATA_DIR = "data"
GZIP_MAGIC = b"\x1f\x8b"


def find_latest(*patterns) -> str | None:
    """Newest file matching the first glob pattern that finds anything."""
    for pattern in patterns or ("*-radar.txt*",):
        files = glob.glob(pattern)
        if files:
            return max(files, key=os.path.getmtime)
    return None


def is_log_file(name: str) -> bool:
    return name.endswith(("-radar.txt", "-radar.txt.gz"))


def logs_in(directory: str) -> list[str]:
    """Every *-radar.txt[.gz] log under *directory* (and below), newest first."""
    found = []
    for root, _dirs, names in os.walk(directory):
        for name in names:
            if is_log_file(name):
                found.append(os.path.join(root, name))

    def mtime(path):
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0.0

    return sorted(found, key=mtime, reverse=True)


def human_size(size: int) -> str:
    """File size for the picker listing."""
    value = float(size)
    for unit in ("B", "KB", "MB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} GB"


def choose_log(directory: str) -> str | None:
    """Let the user pick a log when a directory was passed; None = quit."""
    logs = logs_in(directory)
    if not logs:
        sys.stderr.write(f"no *-radar.txt[.gz] logs in {directory} "
                         "(subdirectories are searched too)\n")
        return None

    print(f"Logs in {directory} (newest first):")
    width = min(60, max(len(os.path.relpath(p, directory)) for p in logs))
    for i, path in enumerate(logs, 1):
        try:
            st = os.stat(path)
            when = dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
            size = human_size(st.st_size)
        except OSError:
            when, size = "?", "?"
        rel = os.path.relpath(path, directory)
        print(f"  {i:>3}) {rel:<{width}}  {size:>9}  {when}")

    prompt = f"Select 1-{len(logs)} [Enter = newest, q = quit]: "
    while True:
        try:
            answer = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):     # pipes, cron, ^C
            print()
            answer = ""
        if not answer:
            return logs[0]
        if answer.lower() in ("q", "quit", "exit"):
            return None
        try:
            choice = int(answer)
        except ValueError:
            print(f"  enter 1-{len(logs)}, Enter for the newest, or q to quit")
            continue
        if 1 <= choice <= len(logs):
            return logs[choice - 1]
        print(f"  {choice} is out of range 1-{len(logs)}")


def is_gzip(path: str) -> bool:
    """True for a gzip file: by .gz extension or by its magic bytes."""
    if str(path).lower().endswith(".gz"):
        return True
    try:
        with open(path, "rb") as fh:
            return fh.read(2) == GZIP_MAGIC
    except OSError:
        return False


def gunzip_text(data: bytes) -> str:
    """Decode a gzip stream, tolerating a truncated (still written) tail.

    gzip concatenates members, and an in-progress file ends mid-member, so
    decompress member by member and keep whatever has been written so far.
    """
    parts = []
    while data:
        dec = zlib.decompressobj(16 + zlib.MAX_WBITS)      # +16: gzip header
        try:
            parts.append(dec.decompress(data))
        except zlib.error:
            break
        if not dec.eof:            # member is incomplete: recorder still running
            break
        data = dec.unused_data      # another member appended later
    return b"".join(parts).decode("utf-8", "replace")


NUL = "\x00"
NUL_REPLACEMENT = "\ufffd"      # visible placeholder, keeps the CSV parseable


def read_text(path: str) -> str:
    """Read a log file, transparently decompressing gzip.

    NUL bytes are replaced: the LX-80 occasionally emits one inside a sentence
    (seen on 2026-11-15) and it passes the device's own XOR checksum, but
    Python's csv module rejects any line containing NUL.
    """
    with open(path, "rb") as fh:
        data = fh.read()
    text = gunzip_text(data) if is_gzip(path) else data.decode("utf-8", "replace")
    return text.replace(NUL, NUL_REPLACEMENT)


def open_log(path: str):
    """Open a CSV log for reading: plain or gzipped, always as text."""
    return io.StringIO(read_text(path))


def default_output(path: str) -> str:
    """<input>-plot.html, with both .gz and .txt stripped from the name."""
    base = path[:-3] if path.lower().endswith(".gz") else path
    return os.path.splitext(base)[0] + "-plot.html"


def open_html(path: str) -> bool:
    """Open *path* with the desktop browser; return True if one was launched.

    On WSL the Windows default browser is preferred (``wslview`` or
    ``cmd.exe start``); otherwise the usual Linux/macOS openers are tried.
    Output is discarded so a missing handler produces a clean fallback
    message instead of a gio/xdg-open error.
    """
    path = os.path.abspath(path)
    url = "file://" + path
    candidates = []

    wsl = bool(os.environ.get("WSL_DISTRO_NAME")) or (
        hasattr(os, "uname") and "microsoft" in os.uname().release.lower()
    )
    if wsl:
        if shutil.which("wslview"):
            candidates.append(["wslview", path])
        if shutil.which("wslpath") and shutil.which("cmd.exe"):
            try:
                win_path = subprocess.check_output(
                    ["wslpath", "-w", path], text=True,
                    stderr=subprocess.DEVNULL,
                ).strip()
            except (OSError, subprocess.SubprocessError):
                win_path = ""
            if win_path:
                candidates.append(["cmd.exe", "/c", "start", "", win_path])

    if shutil.which("xdg-open"):
        candidates.append(["xdg-open", url])
    if shutil.which("gio"):
        candidates.append(["gio", "open", url])
    if sys.platform == "darwin":
        candidates.append(["open", path])

    for command in candidates:
        try:
            result = subprocess.run(
                command, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return True
    return False


def detect_units(path: str) -> str:
    """Read the distance unit from the CSV header ('actual_distance_m')."""
    with open_log(path) as fh:
        header = next(csv.reader(fh), [])
    for name in header:
        if name.startswith("actual_distance_"):
            return name[len("actual_distance_"):] or "m"
    return "m"


def _col(fieldnames, prefix):
    for name in fieldnames or ():
        if name.startswith(prefix):
            return name
    return None


def column_map(path: str) -> dict:
    """Map our logical names to the actual CSV header names."""
    with open_log(path) as fh:
        header = next(csv.reader(fh), [])
    return {
        "dist": _col(header, "actual_distance"),
        "avg": _col(header, "average_distance"),
        "snr": _col(header, "snr"),
        "temp": _col(header, "temperature"),
    }


def _num(rec, name):
    if not name:
        return None
    raw = rec.get(name)
    if raw in (None, "", "nan", "NaN"):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _parse_row(rec, cols):
    """Convert one CSV record into a plot row, or None if unusable."""
    stamp = rec.get("captured_at") or ""
    when = None
    if stamp:
        try:
            when = dt.datetime.fromisoformat(stamp)
        except ValueError:
            when = None
    if when is None:
        epoch = _num(rec, "epoch_seconds")
        if epoch is None:
            return None
        when = dt.datetime.fromtimestamp(epoch)
    if when.tzinfo is not None:
        when = when.astimezone().replace(tzinfo=None)  # Bokeh likes naive times

    return {
        "t": when,
        "dist": _num(rec, cols["dist"]),
        "avg": _num(rec, cols["avg"]),
        "snr": _num(rec, cols["snr"]),
        "temp": _num(rec, cols["temp"]),
    }


def is_missing_value(value, missing_values) -> bool:
    """True if *value* matches one of the requested missing sentinels."""
    return value is not None and any(
        abs(value - target) <= SPIKE_TOLERANCE for target in missing_values
    )


def is_missing_l1(row, missing_values=(L1_SPIKE_VALUE,)) -> bool:
    """True if the row's L1 distance matches a missing sentinel."""
    return is_missing_value(row.get("dist"), missing_values)


def drop_missing_rows(rows, missing_values):
    """Drop rows whose L1 distance matches one of the missing sentinels."""
    return [row for row in rows if not is_missing_l1(row, missing_values)]


def load_rows(path: str, max_points: int = 0,
              missing_values=()) -> list[dict]:
    cols = column_map(path)
    with open_log(path) as fh:
        rows = []
        for rec in csv.DictReader(fh):
            row = _parse_row(rec, cols)
            if row is not None:
                if missing_values and is_missing_l1(row, missing_values):
                    continue
                rows.append(row)

    if max_points and len(rows) > max_points:
        step = len(rows) / max_points
        rows = [rows[int(i * step)] for i in range(max_points)]
    return rows


def _parse_lines(lines, cols):
    """Parse CSV lines into plot rows (skipping blanks and repeated headers)."""
    rows = []
    for rec in csv.reader(lines):
        if not rec or not rec[0] or rec[0].startswith("captured_at"):
            continue           # blank line or repeated header
        row = _parse_row(
            {
                "captured_at": rec[0],
                "epoch_seconds": rec[1] if len(rec) > 1 else "",
                cols["dist"]: rec[2] if len(rec) > 2 else "",
                cols["avg"]: rec[3] if len(rec) > 3 else "",
                cols["snr"]: rec[4] if len(rec) > 4 else "",
                cols["temp"]: rec[5] if len(rec) > 5 else "",
            },
            cols,
        )
        if row is not None:
            rows.append(row)
    return rows


def _complete_lines(text: str) -> list[str]:
    """Lines terminated by a newline; drop a half-written last line."""
    lines = text.splitlines()
    if lines and not text.endswith(("\n", "\r")):
        lines.pop()            # last line is still being written
    return lines


def initial_offset(path: str):
    """Where tailing starts: a byte position, or a line count for gzip."""
    if is_gzip(path):
        try:
            return len(_complete_lines(read_text(path)))
        except OSError:
            return 0
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def tail_rows(path: str, offset: int, cols: dict):
    """Read complete new lines from *offset*; return (rows, new_offset).

    For a plain log *offset* is a byte position and only the new chunk is read.
    A gzip stream cannot be tailed by byte position (the old data has to be
    inflated to reach the new data), so *offset* is the number of complete
    lines already consumed and the file is decompressed again each call.
    """
    if is_gzip(path):
        try:
            complete = _complete_lines(read_text(path))
        except OSError:
            return [], offset
        if offset > len(complete):     # file was truncated / replaced
            offset = 0
        return _parse_lines(complete[offset:], cols), len(complete)

    try:
        size = os.path.getsize(path)
    except OSError:
        return [], offset
    if offset > size:          # file was truncated / replaced
        offset = 0

    with open(path, "rb") as fh:
        fh.seek(offset)
        chunk = fh.read()
    if not chunk:
        return [], offset

    end = chunk.rfind(b"\n")
    if end < 0:                # partial line still being written
        return [], offset
    text = (chunk[: end + 1].decode("utf-8", "replace")
            .replace(NUL, NUL_REPLACEMENT))
    new_offset = offset + end + 1
    return _parse_lines(text.splitlines(), cols), new_offset


def to_columns(rows):
    return {
        "t": [r["t"] for r in rows],
        "dist": [r["dist"] for r in rows],
        "avg": [r["avg"] for r in rows],
        "snr": [r["snr"] for r in rows],
        "temp": [r["temp"] for r in rows],
    }


def power_spectrum(data, max_samples=SPECTRUM_MAX_SAMPLES,
                   missing_values=(L1_SPIKE_VALUE,)):
    """One-sided power spectrum of the L1 (actual distance) series.

    The LX-80 reports at an irregular ~10 Hz, so the samples are linearly
    resampled onto a uniform grid using the median sampling interval before
    the FFT.  Samples matching *missing_values* (the 15.000 no-echo sentinel
    by default) are dropped first, then a least-squares line is removed (not
    just the mean) to suppress the DC bin and the slow baseline drift that
    would otherwise leak into the lowest frequency bins; a Hann window reduces
    the remaining leakage.  Returning the log10 power directly keeps the
    Bokeh panel a plain linear axis (and avoids non-positive values on a log
    axis).

    Returns a dict of column lists (freq, power, amplitude, log10_power) that
    can be used directly as a ColumnDataSource.
    """
    empty = {"freq": [], "power": [], "amplitude": [], "log10_power": []}
    pairs = [(t, v) for t, v in zip(data.get("t") or (), data.get("dist") or ())
             if t is not None and v is not None
             and not is_missing_value(v, missing_values)]
    if len(pairs) < 4:
        return empty

    t = np.asarray([p[0].timestamp() for p in pairs], dtype=float)
    x = np.asarray([p[1] for p in pairs], dtype=float)
    order = np.argsort(t)
    t, x = t[order], x[order]

    dt = np.diff(t)
    dt = dt[dt > 0]
    if not dt.size:
        return empty
    step = float(np.median(dt))
    span = float(t[-1] - t[0])
    if not np.isfinite(step) or step <= 0 or span <= 0:
        return empty

    n = max(4, int(round(span / step)) + 1)
    n = min(n, max_samples)
    step = span / (n - 1)           # grid spacing after the sample cap
    grid = t[0] + np.arange(n) * step
    y = np.interp(grid, t, x)

    # Remove the best-fit line, not only the mean.  The centred coordinate
    # keeps the normal equations well conditioned for long captures.
    u = np.linspace(-0.5, 0.5, n)
    slope, offset = np.polyfit(u, y, 1)
    y = y - (slope * u + offset)

    win = np.hanning(n)
    spectrum = np.fft.rfft(y * win)
    freqs = np.fft.rfftfreq(n, d=step)

    # One-sided peak amplitude for a real signal, corrected for the Hann
    # window's coherent gain; power is amplitude squared.
    amplitude = 2.0 * np.abs(spectrum) / np.sum(win)
    power = amplitude ** 2

    # Drop the DC bin: the mean and linear trend are already removed and
    # log10(0) is undefined.
    freqs, amplitude, power = freqs[1:], amplitude[1:], power[1:]
    if not power.size:
        return empty

    peak = float(np.max(power))
    floor = max(peak * 1e-12, 1e-30) if peak > 0 else 1e-30
    power = np.maximum(power, floor)

    return {
        "freq": freqs.tolist(),
        "power": power.tolist(),
        "amplitude": amplitude.tolist(),
        "log10_power": np.log10(power).tolist(),
    }


# --------------------------------------------------------------------------
# summary banner
# --------------------------------------------------------------------------
def stats_html(path, data, units, total=None, decimated=False):
    """Build the subtitle HTML from a column dict (t, dist, avg, snr, temp)."""
    times = data.get("t") or []
    d = [v for v in (data.get("dist") or ()) if v is not None]
    snr = [v for v in (data.get("snr") or ()) if v is not None]
    temp = [v for v in (data.get("temp") or ()) if v is not None]
    span = ""
    if len(times) > 1:
        span = str(times[-1] - times[0]).split(".")[0]

    shown = len(times)
    bits = [f"<b>{os.path.basename(path)}</b>",
            f"{total if total is not None else shown} samples",
            f"duration {span}" if span else ""]
    if d:
        bits.append(
            f"distance min/med/max {min(d):.2f} / {statistics.median(d):.2f} / "
            f"{max(d):.2f} {units}"
        )
    if snr:
        bits.append(f"SNR median {statistics.median(snr):.1f} dB")
    if temp:
        bits.append(f"temp avg {statistics.fmean(temp):.1f} \u00b0C")
    if decimated:
        bits.append(f"(decimated to {shown} points)")
    bits.append(f'<span style="color:#999">geolux-radar {VERSION}</span>')
    return (
        '<div style="font:13px system-ui,sans-serif;color:#444;padding:6px 2px">'
        + " &nbsp;\u00b7&nbsp; ".join(b for b in bits if b)
        + "</div>"
    )


# --------------------------------------------------------------------------
# figures
# --------------------------------------------------------------------------
def build_layout(source, units, title, subtitle, spectrum=None,
                 spectrum_scale="linear"):
    p_dist = figure(
        height=380,
        title=title,
        x_axis_type="datetime",
        tools=TOOLS,
        toolbar_location="above",
        active_scroll="wheel_zoom",      # mouse wheel zooms without picking a tool
        sizing_mode="stretch_width",
    )
    p_dist.line("t", "dist", source=source, color=C_ACTUAL, line_width=2,
                legend_label="L1 line")
    p_dist.scatter("t", "dist", source=source, color=C_ACTUAL, size=6,
                   alpha=0.85, legend_label="L1 points")
    p_dist.line("t", "avg", source=source, color=C_AVERAGE, line_width=2,
                line_dash="dashed", legend_label="L2 line")
    p_dist.scatter("t", "avg", source=source, color=C_AVERAGE, size=6,
                   marker="circle_x", alpha=0.85, legend_label="L2 points")
    p_dist.yaxis.axis_label = f"Distance ({units})"
    p_dist.legend.location = "top_left"
    p_dist.legend.click_policy = "hide"
    p_dist.legend.background_fill_alpha = 0.7
    p_dist.add_tools(
        HoverTool(
            tooltips=[
                ("time", "@t{%H:%M:%S.%3N}"),
                ("actual", "@dist{0.00} " + units),
                ("average", "@avg{0.00} " + units),
                ("SNR", "@snr{0.0} dB"),
            ],
            formatters={"@t": "datetime"},
            mode="vline",
        )
    )
    p_dist.toolbar.active_inspect = None      # hover off until selected

    p_snr = figure(
        height=170,
        x_axis_type="datetime",
        x_range=p_dist.x_range,
        tools=TOOLS,
        toolbar_location="above",
        active_scroll="wheel_zoom",
        sizing_mode="stretch_width",
    )
    p_snr.line("t", "snr", source=source, color=C_SNR, line_width=1.5,
               legend_label="SNR line")
    p_snr.scatter("t", "snr", source=source, color=C_SNR, size=5, alpha=0.85,
                  legend_label="SNR points")
    p_snr.yaxis.axis_label = "SNR (dB)"
    p_snr.xaxis.visible = False
    p_snr.legend.location = "top_left"
    p_snr.legend.click_policy = "hide"
    p_snr.legend.background_fill_alpha = 0.7
    p_snr.add_layout(
        Span(location=10, dimension="width", line_color=C_WARN,
             line_dash="dashed", line_alpha=0.7)
    )
    p_snr.add_tools(
        HoverTool(
            tooltips=[("time", "@t{%H:%M:%S}"), ("SNR", "@snr{0.0} dB")],
            formatters={"@t": "datetime"},
            mode="vline",
        )
    )
    p_snr.toolbar.active_inspect = None       # hover off until selected

    p_temp = figure(
        height=170,
        x_axis_type="datetime",
        x_range=p_dist.x_range,
        tools=TOOLS,
        toolbar_location="above",
        active_scroll="wheel_zoom",
        sizing_mode="stretch_width",
    )
    p_temp.line("t", "temp", source=source, color=C_TEMP, line_width=1.5,
                legend_label="Temp line")
    p_temp.scatter("t", "temp", source=source, color=C_TEMP, size=5,
                   alpha=0.85, legend_label="Temp points")
    p_temp.yaxis.axis_label = "Temperature (\u00b0C)"
    p_temp.xaxis.axis_label = "Capture time"
    p_temp.legend.location = "top_left"
    p_temp.legend.click_policy = "hide"
    p_temp.legend.background_fill_alpha = 0.7
    p_temp.add_tools(
        HoverTool(
            tooltips=[("time", "@t{%H:%M:%S}"), ("temp", "@temp{0.0} \u00b0C")],
            formatters={"@t": "datetime"},
            mode="vline",
        )
    )
    p_temp.toolbar.active_inspect = None      # hover off until selected

    if spectrum is None:
        spectrum = ColumnDataSource(power_spectrum({}))
    log_scale = spectrum_scale == "log"
    p_fft = figure(
        height=260,
        title="L1 power spectrum (linear detrend, Hann-windowed)",
        x_axis_label="Frequency (Hz)",
        y_axis_type="log" if log_scale else "linear",
        y_axis_label=f"Power ({units}\u00b2)" + (" - log scale" if log_scale else ""),
        tools=TOOLS,
        toolbar_location="above",
        active_scroll="wheel_zoom",
        sizing_mode="stretch_width",
    )
    p_fft.line("freq", "power", source=spectrum, color=C_SPECTRUM,
               line_width=1.5, legend_label="L1 power")
    p_fft.scatter("freq", "power", source=spectrum, color=C_SPECTRUM,
                  size=3, alpha=0.65, legend_label="L1 power points")
    p_fft.legend.location = "top_right"
    p_fft.legend.click_policy = "hide"
    p_fft.legend.background_fill_alpha = 0.7
    p_fft.add_tools(
        HoverTool(
            tooltips=[
                ("frequency", "@freq{0.0000} Hz"),
                ("log10 power", "@log10_power{0.00}"),
                ("amplitude", "@amplitude{0.0000} " + units),
                ("power", "@power"),
            ],
            mode="vline",
        )
    )
    p_fft.toolbar.active_inspect = None       # hover off until selected

    # Each panel keeps its own toolbar so the controls are available where the
    # user is working.  merge_tools=False avoids ToolProxy objects: Bokeh
    # cannot pre-activate a scroll tool through a proxy, so wheel zoom would
    # start out inert.
    grid = gridplot(
        [[p_dist], [p_snr], [p_temp], [p_fft]],
        toolbar_location=None,
        merge_tools=False,
        sizing_mode="stretch_width",
    )
    header = Div(
        text=(
            '<div style="font:12px system-ui,sans-serif;color:#666;'
            'padding:4px 2px 0">'
            f'<a href="{REPO_URL}" style="color:#1f77b4;text-decoration:none">'
            f'{REPO_URL}</a>'
            '<span style="color:#ccc"> &nbsp;\u00b7&nbsp; </span>'
            f'geolux-radar {VERSION}</div>'
        ),
        sizing_mode="stretch_width",
    )
    return column(header, subtitle, grid, sizing_mode="stretch_width")


# --------------------------------------------------------------------------
# modes
# --------------------------------------------------------------------------
def run_static(args, path):
    missing_values = tuple(args.remove_missing or ())
    spectrum_missing = missing_values or (L1_SPIKE_VALUE,)
    rows = load_rows(path, args.max_points, missing_values)
    if not rows:
        sys.stderr.write(f"no usable samples in {path}\n")
        return 1
    units = detect_units(path)
    decimated = bool(args.max_points) and len(rows) >= args.max_points

    data = to_columns(rows)
    source = ColumnDataSource(data)
    spectrum = ColumnDataSource(
        power_spectrum(data, missing_values=spectrum_missing)
    )
    subtitle = Div(text=stats_html(path, source.data, units, decimated=decimated),
                   sizing_mode="stretch_width")

    out = args.output or default_output(path)
    base = os.path.basename(path)
    title = f"Geolux LX-80 - {base} (geolux-radar {VERSION})"
    layout = build_layout(
        source, units,
        f"Geolux LX-80 radar \u2014 {base}  \u00b7  geolux-radar {VERSION}",
        subtitle, spectrum, args.spectrum_scale,
    )
    if args.inline:                      # embed BokehJS -> works offline
        save(layout, filename=out, title=title, resources=INLINE)
    else:
        output_file(out, title=title)
        save(layout)
    print(f"wrote {os.path.abspath(out)}  ({len(rows)} samples)")

    if not args.no_open and not open_html(out):
        print(f"no browser could be launched; open {os.path.abspath(out)}")
    return 0


def run_live(args, path):
    from bokeh.server.server import Server

    def app(doc):
        missing_values = tuple(args.remove_missing or ())
        spectrum_missing = missing_values or (L1_SPIKE_VALUE,)
        rows = load_rows(path, args.max_points, missing_values)
        units = detect_units(path)
        cols_data = to_columns(rows)
        source = ColumnDataSource(cols_data)
        spectrum = ColumnDataSource(
            power_spectrum(cols_data, missing_values=spectrum_missing)
        )
        total = {"n": len(rows)}
        subtitle = Div(text=stats_html(path, cols_data, units, total=len(rows)),
                       sizing_mode="stretch_width")
        layout = build_layout(
            source, units,
            f"Geolux LX-80 radar \u2014 {os.path.basename(path)} (live)"
            f"  \u00b7  geolux-radar {VERSION}",
            subtitle, spectrum, args.spectrum_scale,
        )
        cols = column_map(path)
        state = {"offset": initial_offset(path)}

        def tick():
            new_rows, state["offset"] = tail_rows(path, state["offset"], cols)
            if missing_values:
                new_rows = drop_missing_rows(new_rows, missing_values)
            if not new_rows:
                return
            source.stream(to_columns(new_rows), rollover=args.max_points)
            total["n"] += len(new_rows)
            spectrum.data = power_spectrum(
                source.data, missing_values=spectrum_missing
            )
            data = source.data
            keep = min(len(data["t"]), 2000)  # keep the stats cheap
            recent = {k: data[k][len(data["t"]) - keep:] for k in ("t", "dist", "avg", "snr", "temp")}
            subtitle.text = stats_html(path, recent, units, total=total["n"])

        doc.add_periodic_callback(tick, max(100, int(args.interval * 1000)))
        doc.add_root(layout)

    server = Server(
        {"/": app},
        port=args.port,
        allow_websocket_origin=[f"localhost:{args.port}", f"127.0.0.1:{args.port}"],
    )
    server.start()
    url = f"http://localhost:{args.port}/"
    print(f"serving {os.path.basename(path)} at {url}  (ctrl-c to stop, refresh {args.interval:g}s)")
    if not args.no_open:
        server.io_loop.add_callback(server.show, "/")
    else:
        print(f"open {url} in your browser")
    try:
        server.io_loop.start()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


USAGE_HELP = """\
plot_radar.py [-h | --help | -? | --version] [FILE] [-o OUTPUT] [--live]
                     [--port PORT] [--interval SEC] [--no-open] [--inline]
                     [--max-points N] [--remove-missing VALUE]
                     [--spectrum-scale {linear,log}]"""

DESCRIPTION_HELP = f"""\
plot_radar.py {VERSION} - plot Geolux LX-80 radar logs (<stamp>-radar.txt.gz,
written by geolux_tui.py) with Bokeh.  Three linked panels show distance, SNR
and instrument temperature against the capture time, and a fourth panel shows
the L1 (actual distance) FFT power spectrum, with a linear or log y axis
(default linear), plus optional hover tools, zoom/pan tools and a summary banner.

The gzip-compressed logs are decompressed on the fly; plain <stamp>-radar.txt
logs from older runs are read too.

Static mode (default) writes a self-contained HTML page and opens it.  Live
mode (--live) starts a Bokeh server that tails the log file and refreshes the
plots while geolux_tui.py keeps appending to it."""

EPILOG_HELP = f"""\
examples:
  ./plot_radar.py                                            newest *-radar.txt[.gz] under ./data
  ./plot_radar.py data/2026-1114/2026-1114-122308-radar.txt     plot a specific log (.gz too)
  ./plot_radar.py data/2026-1115/                            list the logs in a directory and pick one
  ./plot_radar.py -o /tmp/plot.html --no-open                write HTML, do not open a browser
  ./plot_radar.py --inline                                   embed BokehJS (works offline)
  ./plot_radar.py --live --port 5006                         auto-refreshing Bokeh server
  ./plot_radar.py --live --interval 0.5                      refresh twice per second
  ./plot_radar.py --max-points 20000                         decimate a long log
  ./plot_radar.py --remove-missing=15.000                    hide L1 = 15.000 missing rows
  ./plot_radar.py --spectrum-scale log                       log-scale power spectrum

input files:
  gzip-compressed CSV logs written by geolux_tui.py (default directory
  ./data/YYYY-MMDD), named <stamp>-radar.txt.gz:
    captured_at, epoch_seconds, actual_distance_<unit>, average_distance_<unit>,
    snr_db, temperature_c, raw_sentence
  Compression is detected from the .gz suffix or the gzip magic bytes, so
  plain <stamp>-radar.txt logs from older runs are still accepted.  The
  distance unit is taken from the header suffix, so m, ft, mm, cm and in all
  work.  Missing values (empty cells) are plotted as gaps.  NUL bytes that
  occasionally appear in a device sentence (they pass the device checksum)
  are replaced with U+FFFD, because Python's csv module rejects NUL.

  FILE may also be a directory (for example ./data or a dated capture
  directory): every *-radar.txt[.gz] found there - subdirectories included -
  is listed newest first with its size and timestamp, and you pick one by
  number.  Enter selects the newest log and q quits.  With no FILE at all the
  newest log under ./data is used.

panels (the three time panels share their x axis; each panel has a toolbar):
  distance     actual (L1, solid, circles) and average (L2, dashed, crosses);
               legend entries L1 line / L1 points / L2 line / L2 points
  SNR          dB, with a dashed reference line at 10 dB (low-echo warning);
               legend entries SNR line / SNR points
  temperature  instrument temperature in degrees Celsius; legend entries
               Temp line / Temp points
  spectrum     one-sided FFT power spectrum of L1 against frequency (Hz);
               the y axis is linear power by default, and --spectrum-scale
               log switches it to a logarithmic power axis.  Legend entries
               L1 power / L1 power points.  L1 samples at 15.000 (the
               no-echo sentinel) are dropped first; the irregular ~10 Hz
               samples are then linearly resampled onto a uniform grid from
               the median sampling interval.  A least-squares line is removed to suppress
               the DC and slow baseline drift, and a Hann window is applied
               before the FFT, so the frequency axis is in Hz.

  By default the 15.000 L1 no-echo rows are plotted (as spikes in the time
  panels).  --remove-missing=VALUE drops L1 samples equal to VALUE from all
  three time panels and from the FFT; repeat the option to remove several
  values.  With no option the time panels keep every row and the FFT keeps
  dropping its built-in 15.000 sentinel.

  Every series is drawn as a line plus markers (size 3-6 px, alpha 0.65-0.85)
  so the data is readable when zoomed in.  Each panel has its own legend with
  click_policy 'hide', and line and points are separate entries, so points
  and lines can be switched on and off independently.

  Mouse-wheel zoom is active by default in all four panels - scroll to zoom,
  drag to pan, without selecting a tool first.  Each panel has its own toolbar
  above it with box zoom, undo/redo, reset and save.

  Hover is off by default in every panel.  Click the hover tool in a panel's
  toolbar to enable it: a vertical-line hover on each time panel then shows
  the time and the values at that instant, and the spectrum panel's hover
  shows frequency, log10 power, amplitude and power.  The banner above the
  plots summarises samples,
  duration, min/median/max distance, median SNR and average temperature.

live mode:
  --live serves a Bokeh app that tails FILE and streams newly appended rows
  into the plots, so it can run alongside geolux_tui.py.  Only complete lines
  are parsed (a half-written row is never read), and --max-points caps how
  many points stay in memory.  A gzip log is decompressed again on every
  refresh, so the live mode stays correct while the recorder appends rows.
  --remove-missing=VALUE also filters newly streamed rows in live mode.

install (mamba, not pip):
  mamba env create -f environment.yml    then:  mamba activate radar
  See README.md for WSL2 setup (WSL2 needs usbipd-win for the USB adapter).

exit status:
  0  success
  2  no log file found, or the given file does not exist

repository:
  {REPO_URL}
"""


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        prog="plot_radar.py",
        add_help=False,
        usage=USAGE_HELP,
        description=DESCRIPTION_HELP,
        epilog=EPILOG_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("-h", "--help", "-?", action="help",
                    help="show this help and exit")
    ap.add_argument("--version", action="version",
                    version=f"%(prog)s {VERSION}",
                    help="show the program version and exit")
    ap.add_argument("file", nargs="?", metavar="FILE",
                    help="log file, or a directory to choose a log from "
                         "(default: newest *-radar.txt[.gz] under ./data, "
                         "including its YYYY-MMDD subdirectories, "
                         "falling back to the current directory)")
    ap.add_argument("-o", "--output", metavar="OUTPUT",
                    help="output HTML file for static mode "
                         "(default: <input>-plot.html)")
    ap.add_argument("--live", action="store_true",
                    help="serve an auto-refreshing Bokeh app instead of writing HTML")
    ap.add_argument("--port", type=int, default=5006, metavar="PORT",
                    help="port for --live (default: %(default)s)")
    ap.add_argument("--interval", type=float, default=1.0, metavar="SEC",
                    help="live refresh interval in seconds (default: %(default)s)")
    ap.add_argument("--no-open", action="store_true",
                    help="do not open a browser (print the path/URL instead)")
    ap.add_argument("--inline", action="store_true",
                    help="embed BokehJS in the HTML so it works offline "
                         "(larger file, ~1.4 MB)")
    ap.add_argument("--max-points", type=int, default=100_000, metavar="N",
                    help="decimate longer logs to N points and cap live points in "
                         "memory (default: %(default)s, 0 = keep all)")
    ap.add_argument("--remove-missing", "--drop-missing", dest="remove_missing",
                    action="append", type=float, metavar="VALUE",
                    help="drop L1 samples equal to VALUE from the time plots "
                         "and the FFT; repeat the option to remove several "
                         "values (e.g. --remove-missing=15.000)")
    ap.add_argument("--spectrum-scale", choices=("linear", "log"),
                    default="linear",
                    help="Y-axis scale for the FFT power panel "
                         "(default: %(default)s)")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    path = args.file or find_latest(
        os.path.join(DEFAULT_DATA_DIR, "*", "*-radar.txt*"),   # data/YYYY-MMDD/
        os.path.join(DEFAULT_DATA_DIR, "*-radar.txt*"),       # older flat layout
        "*-radar.txt*",                                       # current directory
    )
    if path and os.path.isdir(path):
        path = choose_log(path)          # directory: list the logs and pick one
        if path is None:
            return 2
    if not path:
        sys.stderr.write("no *-radar.txt[.gz] log file found; pass a file name "
                         "or a directory to choose from\n")
        return 2
    if not os.path.exists(path):
        sys.stderr.write(f"file not found: {path}\n")
        return 2
    return run_live(args, path) if args.live else run_static(args, path)


if __name__ == "__main__":
    raise SystemExit(main())
