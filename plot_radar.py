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

Requires: pip install bokeh
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import glob
import io
import os
import statistics
import sys
import webbrowser
import zlib

from bokeh.io import output_file, save
from bokeh.resources import INLINE
from bokeh.layouts import column, gridplot
from bokeh.models import ColumnDataSource, Div, HoverTool, Span
from bokeh.plotting import figure

C_ACTUAL = "#1f77b4"
C_AVERAGE = "#ff7f0e"
C_SNR = "#9467bd"
C_TEMP = "#2ca02c"
C_WARN = "#d62728"

TOOLS = "pan,wheel_zoom,box_zoom,undo,redo,reset,save"


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


def read_text(path: str) -> str:
    """Read a log file, transparently decompressing gzip."""
    with open(path, "rb") as fh:
        data = fh.read()
    if is_gzip(path):
        return gunzip_text(data)
    return data.decode("utf-8", "replace")


def open_log(path: str):
    """Open a CSV log for reading: plain or gzipped, always as text."""
    if is_gzip(path):
        return io.StringIO(read_text(path))
    return open(path, newline="", encoding="utf-8", errors="replace")


def default_output(path: str) -> str:
    """<input>-plot.html, with both .gz and .txt stripped from the name."""
    base = path[:-3] if path.lower().endswith(".gz") else path
    return os.path.splitext(base)[0] + "-plot.html"


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


def load_rows(path: str, max_points: int = 0) -> list[dict]:
    cols = column_map(path)
    with open_log(path) as fh:
        rows = []
        for rec in csv.DictReader(fh):
            row = _parse_row(rec, cols)
            if row is not None:
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
    text = chunk[: end + 1].decode("utf-8", "replace")
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
    return (
        '<div style="font:13px system-ui,sans-serif;color:#444;padding:6px 2px">'
        + " &nbsp;\u00b7&nbsp; ".join(b for b in bits if b)
        + "</div>"
    )


# --------------------------------------------------------------------------
# figures
# --------------------------------------------------------------------------
def build_layout(source, units, title, subtitle):
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
                legend_label="Actual distance (L1)")
    p_dist.scatter("t", "dist", source=source, color=C_ACTUAL, size=6,
                   alpha=0.85, legend_label="Actual distance (L1)")
    p_dist.line("t", "avg", source=source, color=C_AVERAGE, line_width=2,
                line_dash="dashed", legend_label="Average distance (L2)")
    p_dist.scatter("t", "avg", source=source, color=C_AVERAGE, size=6,
                   marker="circle_x", alpha=0.85,
                   legend_label="Average distance (L2)")
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

    p_snr = figure(
        height=170,
        x_axis_type="datetime",
        x_range=p_dist.x_range,
        tools=TOOLS,
        toolbar_location=None,
        active_scroll="wheel_zoom",
        sizing_mode="stretch_width",
    )
    p_snr.line("t", "snr", source=source, color=C_SNR, line_width=1.5,
               legend_label="SNR")
    p_snr.scatter("t", "snr", source=source, color=C_SNR, size=5, alpha=0.85,
                  legend_label="SNR")
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

    p_temp = figure(
        height=170,
        x_axis_type="datetime",
        x_range=p_dist.x_range,
        tools=TOOLS,
        toolbar_location=None,
        active_scroll="wheel_zoom",
        sizing_mode="stretch_width",
    )
    p_temp.line("t", "temp", source=source, color=C_TEMP, line_width=1.5,
                legend_label="Temperature")
    p_temp.scatter("t", "temp", source=source, color=C_TEMP, size=5,
                   alpha=0.85, legend_label="Temperature")
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

    # Toolbars are not merged: with merge_tools=True the shared toolbar holds
    # ToolProxy objects, and Bokeh cannot pre-activate a scroll tool through a
    # proxy, so wheel zoom would start out inert.  Keeping one toolbar on the
    # distance panel and hidden (but live) toolbars on the linked panels makes
    # wheel zoom active everywhere from load.
    grid = gridplot(
        [[p_dist], [p_snr], [p_temp]],
        toolbar_location=None,
        merge_tools=False,
        sizing_mode="stretch_width",
    )
    return column(subtitle, grid, sizing_mode="stretch_width")


# --------------------------------------------------------------------------
# modes
# --------------------------------------------------------------------------
def run_static(args, path):
    rows = load_rows(path, args.max_points)
    if not rows:
        sys.stderr.write(f"no usable samples in {path}\n")
        return 1
    units = detect_units(path)
    decimated = bool(args.max_points) and len(rows) >= args.max_points

    source = ColumnDataSource(to_columns(rows))
    subtitle = Div(text=stats_html(path, source.data, units, decimated=decimated),
                   sizing_mode="stretch_width")

    out = args.output or default_output(path)
    title = f"Geolux LX-80 - {os.path.basename(path)}"
    layout = build_layout(source, units, f"Geolux LX-80 radar \u2014 {os.path.basename(path)}", subtitle)
    if args.inline:                      # embed BokehJS -> works offline
        save(layout, filename=out, title=title, resources=INLINE)
    else:
        output_file(out, title=title)
        save(layout)
    print(f"wrote {os.path.abspath(out)}  ({len(rows)} samples)")

    if not args.no_open:
        webbrowser.open("file://" + os.path.abspath(out))
    return 0


def run_live(args, path):
    from bokeh.server.server import Server

    def app(doc):
        rows = load_rows(path, args.max_points)
        units = detect_units(path)
        cols_data = to_columns(rows)
        source = ColumnDataSource(cols_data)
        total = {"n": len(rows)}
        subtitle = Div(text=stats_html(path, cols_data, units, total=len(rows)),
                       sizing_mode="stretch_width")
        layout = build_layout(
            source, units, f"Geolux LX-80 radar \u2014 {os.path.basename(path)} (live)", subtitle
        )
        cols = column_map(path)
        state = {"offset": initial_offset(path)}

        def tick():
            new_rows, state["offset"] = tail_rows(path, state["offset"], cols)
            if not new_rows:
                return
            source.stream(to_columns(new_rows), rollover=args.max_points)
            total["n"] += len(new_rows)
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
plot_radar.py [-h | --help | -?] [FILE] [-o OUTPUT] [--live] [--port PORT]
              [--interval SEC] [--no-open] [--inline] [--max-points N]"""

DESCRIPTION_HELP = """\
Plot Geolux LX-80 radar logs (<stamp>-radar.txt.gz, written by geolux_tui.py)
with Bokeh.  Three linked panels show distance, SNR and instrument
temperature against the capture time, with hover tools, zoom/pan tools and a
summary banner.

The gzip-compressed logs are decompressed on the fly; plain <stamp>-radar.txt
logs from older runs are read too.

Static mode (default) writes a self-contained HTML page and opens it.  Live
mode (--live) starts a Bokeh server that tails the log file and refreshes the
plots while geolux_tui.py keeps appending to it."""

EPILOG_HELP = """\
examples:
  ./plot_radar.py                                            newest *-radar.txt[.gz] under ./data
  ./plot_radar.py data/2026-1114/2026-1114-122308-radar.txt     plot a specific log (.gz too)
  ./plot_radar.py -o /tmp/plot.html --no-open                write HTML, do not open a browser
  ./plot_radar.py --inline                                   embed BokehJS (works offline)
  ./plot_radar.py --live --port 5006                         auto-refreshing Bokeh server
  ./plot_radar.py --live --interval 0.5                      refresh twice per second
  ./plot_radar.py --max-points 20000                         decimate a long log

input files:
  gzip-compressed CSV logs written by geolux_tui.py (default directory
  ./data/YYYY-MMDD), named <stamp>-radar.txt.gz:
    captured_at, epoch_seconds, actual_distance_<unit>, average_distance_<unit>,
    snr_db, temperature_c, raw_sentence
  Compression is detected from the .gz suffix or the gzip magic bytes, so
  plain <stamp>-radar.txt logs from older runs are still accepted.  The
  distance unit is taken from the header suffix, so m, ft, mm, cm and in all
  work.  Missing values (empty cells) are plotted as gaps.

panels (x axis linked, one toolbar on the distance panel):
  distance     actual (L1, solid, circles) and average (L2, dashed, crosses);
               click a legend entry to hide or show that element
  SNR          dB, with a dashed reference line at 10 dB (low-echo warning)
  temperature  instrument temperature in degrees Celsius

  Every series is drawn as a line plus markers (size 5-6 px, alpha 0.85) so
  the data is readable when zoomed in, and each panel has its own legend: a
  click hides/shows that element (SNR and temperature too).

  Mouse-wheel zoom is active by default in all three panels - scroll to zoom,
  drag to pan, without selecting a tool first.  The toolbar above the distance
  panel still has box zoom, undo/redo, reset and save.

  A vertical-line hover tool on each panel shows the time and the values at
  that instant.  The banner above the plots summarises samples, duration,
  min/median/max distance, median SNR and average temperature.

live mode:
  --live serves a Bokeh app that tails FILE and streams newly appended rows
  into the plots, so it can run alongside geolux_tui.py.  Only complete lines
  are parsed (a half-written row is never read), and --max-points caps how
  many points stay in memory.  A gzip log is decompressed again on every
  refresh, so the live mode stays correct while the recorder appends rows.

install (mamba, not pip):
  mamba env create -f environment.yml    then:  mamba activate radar
  See README.md for WSL2 setup (WSL2 needs usbipd-win for the USB adapter).

exit status:
  0  success
  2  no log file found, or the given file does not exist
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
    ap.add_argument("file", nargs="?", metavar="FILE",
                    help="log file to plot (default: newest *-radar.txt[.gz] under "
                         "./data, including its YYYY-MMDD subdirectories, "
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
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    path = args.file or find_latest(
        os.path.join(DEFAULT_DATA_DIR, "*", "*-radar.txt*"),   # data/YYYY-MMDD/
        os.path.join(DEFAULT_DATA_DIR, "*-radar.txt*"),       # older flat layout
        "*-radar.txt*",                                       # current directory
    )
    if not path:
        sys.stderr.write("no *-radar.txt[.gz] log file found; pass a file name\n")
        return 2
    if not os.path.exists(path):
        sys.stderr.write(f"file not found: {path}\n")
        return 2
    return run_live(args, path) if args.live else run_static(args, path)


if __name__ == "__main__":
    raise SystemExit(main())
