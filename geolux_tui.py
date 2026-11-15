#!/usr/bin/env python3
"""
Geolux LX-80 radar - live terminal UI, RS-232 TX probe and CSV logger.

NMEA (device -> host), see LX-80 manual ch. 6.1 / 7.1:
    $LVX,L1,L2,T1,L3,L4,S1,S2*CSUM   level measurement report
    $ANG,A1,A2*CSUM                  tilt angle report

Servicing protocol (host -> device), manual ch. 7.2:
    #get_info                        replies with a '# key:value' block

NMEA is unidirectional, so it can only prove the device's TX line works.
The servicing protocol answers requests on the same RS-232 port, so a reply
to #get_info proves *our* TX line -> device RX path.  This program probes
periodically and keeps a running tally of successful probes.

Every card shows the title above the value.  The actual ("real") distance
(L1) is logged with the capture time to a CSV file named

    YYYY-MMDD-HHMMSS-radar.txt

alongside <stamp>-radar-cmd.log (probe history) and <stamp>-radar-info.txt
(device settings snapshot).

Requires pyserial:   pip install pyserial
Keys:  q quit | p pause logging | f freeze display | s set parameter
       i device settings | g refresh settings | c reset counters

Port is opened 115200 8N1 with echo disabled (termios ECHO cleared) and all
CR characters stripped from received lines.
"""

from __future__ import annotations

import argparse
import csv
import curses
import datetime as dt
import locale
import math
import os
import queue
import sys
import threading
import time

try:
    import serial
except ImportError:  # pragma: no cover
    sys.stderr.write("pyserial is required:  pip install pyserial\n")
    raise SystemExit(2)

try:
    import termios
except ImportError:  # pragma: no cover - non-POSIX
    termios = None


DEFAULT_PORT = "/dev/ttyUSB0"
DEFAULT_BAUD = 115200
DATA_ROOT = "data"           # recordings go to <DATA_ROOT>/<YYYY-MMDD>
DATE_DIR_FORMAT = "%Y-%m%d"  # 2026-1114 - year, month+day without separator


def default_log_dir(now=None) -> str:
    """Default log directory for today: data/YYYY-MMDD (created if missing)."""
    now = now or dt.datetime.now()
    return os.path.join(DATA_ROOT, now.strftime(DATE_DIR_FORMAT))
GET_INFO = "#get_info"
CMD_TERMINATOR = "\r\n"

# $LVX field order (firmware > 2.3.2).  Older firmware sends only L1..L4.
LVX_FIELDS = ["L1", "L2", "T1", "L3", "L4", "S1", "S2"]

# (title, field, value kind) - title is rendered above the value
CARDS = [
    ("Actual distance", "L1", "dist"),
    ("Average distance", "L2", "dist"),
    ("Level", "L3", "dist"),
    ("Average level", "L4", "dist"),
    ("Temperature", "T1", "temp"),
    ("SNR", "S1", "snr"),
    ("Level std. dev.", "S2", "std"),
    ("Tilt X", "A1", "angle"),
    ("Tilt Y", "A2", "angle"),
]

# $ANG values are reported in hundredths of a degree (-84 -> -0.84 deg)
ANGLE_DIVISOR = 100.0

# #get_info unit_type -> unit symbol (manual ch. 7.2)
UNIT_NAMES = {0: "mm", 1: "cm", 2: "m", 3: "in", 4: "ft"}
UNIT_TO_M = {"mm": 0.001, "cm": 0.01, "m": 1.0, "in": 0.0254, "ft": 0.3048}

# Settable parameters exposed by the "set" dialog (manual ch. 7.2).
#   key   -> command is  #set_<key>=<value>
#   info  -> field name reported back by #get_info (used for read-back verify)
#   lo/hi -> accepted range, is_int -> integer only
PARAMS = (
    {
        "key": "sensor_height", "label": "Sensor height", "info": "sensor_height",
        "lo_m": 0.0, "hi_m": 100.0, "is_int": False, "unit": True,
        "note": "height above gauge zero; level = sensor_height - distance",
    },
    {
        "key": "level_offset", "label": "Level offset", "info": "level_offset",
        "lo_m": -1000.0, "hi_m": 1000.0, "is_int": False, "unit": True,
        "note": "shifts the reported distance (level = sensor_height - distance)",
    },
    {
        "key": "frame_number", "label": "Averaging frames", "info": "averaging_frame_number",
        "lo": 1, "hi": 1000, "is_int": True, "unit": False,
        "note": "samples averaged; output rate stays at measurement_frequency",
    },
)


# --------------------------------------------------------------------------
# protocol
# --------------------------------------------------------------------------
def xor_checksum_ok(sentence: str) -> bool:
    """Validate the NMEA *CSUM (XOR of everything between '$' and '*')."""
    if "*" not in sentence:
        return True  # be lenient, some builds omit it
    body, _, tail = sentence.partition("*")
    body = body.lstrip("$")
    calc = 0
    for ch in body:
        calc ^= ord(ch)
    try:
        return int(tail.strip()[:2], 16) == calc
    except ValueError:
        return False


def parse_sentence(text: str):
    """Return (kind, {field: float}) or None if the sentence is invalid."""
    if not text.startswith("$") or not xor_checksum_ok(text):
        return None
    body = text.split("*", 1)[0][1:]
    parts = body.split(",")
    kind = parts[0].strip().upper()

    def num(tok):
        tok = tok.strip()
        if not tok:
            return None
        try:
            return float(tok)
        except ValueError:
            return None

    if kind == "LVX":
        fields = {}
        for name, tok in zip(LVX_FIELDS, parts[1:]):
            v = num(tok)
            if v is not None:
                fields[name] = v
        return ("LVX", fields)

    if kind == "ANG":
        fields = {}
        for name, tok in zip(("A1", "A2"), parts[1:]):
            v = num(tok)
            if v is not None:
                fields[name] = v / ANGLE_DIVISOR
        return ("ANG", fields)

    return (kind, {})


def parse_info(lines) -> dict:
    """Parse '# key:value' reply lines into an ordered dict."""
    info = {}
    for line in lines:
        body = line.lstrip("#").strip()
        key, sep, value = body.partition(":")
        if sep and key.strip():
            info[key.strip()] = value.strip()
    return info


def decode_line(raw: bytes) -> str:
    """Decode a serial line and remove CR characters (and surrounding space)."""
    return raw.decode("ascii", "replace").replace("\r", "").strip()


# --------------------------------------------------------------------------
# logging directory
# --------------------------------------------------------------------------
def ensure_dir(path: str) -> str:
    """Create the log directory (default ./data) if it does not exist."""
    path = path or "."
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        sys.stderr.write(f"cannot create log directory {path}: {exc}; using .\n")
        return "."
    return path


# --------------------------------------------------------------------------
# serial port setup
# --------------------------------------------------------------------------
def configure_serial(ser) -> bool:
    """Explicit 8N1 raw port with echo disabled and no CR/NL translation.

    pyserial already selects raw mode; this clears the echo/canonical bits
    (per requirement), disables software flow control and guarantees that CR
    bytes are delivered untouched - they are then stripped in software.
    """
    if termios is None:
        return False
    try:
        fd = ser.fileno()
        attrs = termios.tcgetattr(fd)
        attrs[0] &= ~(termios.IXON | termios.IXOFF | termios.IXANY)
        attrs[1] &= ~termios.OPOST
        attrs[2] &= ~(termios.ICRNL | termios.INLCR | termios.IGNCR)
        attrs[3] &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG)
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        return True
    except (termios.error, OSError, AttributeError):
        return False


# --------------------------------------------------------------------------
# one-shot #get_info (used before the reader thread exists)
# --------------------------------------------------------------------------
def read_info_block(ser, timeout=1.5, quiet=0.25):
    """Send #get_info and collect the '# key:value' block.

    Returns (info dict, rtt_ms or None, number of reply lines).  The device
    does not terminate the block, so it ends when a '$' sentence arrives or
    when no '#' line has been seen for *quiet* seconds.
    """
    start = time.time()
    try:
        ser.reset_input_buffer()
        ser.write((GET_INFO + CMD_TERMINATOR).encode("ascii"))
        ser.flush()
    except (serial.SerialException, OSError):
        return {}, None, 0

    lines = []
    first = last_hash = None
    while time.time() - start < timeout:
        raw = ser.readline()
        if not raw:
            if lines and last_hash is not None and (time.time() - last_hash) >= quiet:
                break
            continue
        text = decode_line(raw)
        if not text:
            continue
        if text.startswith("#"):
            if first is None:
                first = time.time()
            lines.append(text)
            last_hash = time.time()
        elif text.startswith("$") and lines:
            break

    info = parse_info(lines)
    rtt = None if first is None else (first - start) * 1000.0
    return info, rtt, len(lines)


def write_info_snapshot(path, info):
    """Write the device settings block to a file (device format)."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(f"# snapshot:{dt.datetime.now().astimezone().isoformat(timespec='seconds')}\n")
        for key, value in info.items():
            fh.write(f"# {key}:{value}\n")
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# shared state between reader thread and UI
# --------------------------------------------------------------------------
class SharedState:
    def __init__(self):
        self._lock = threading.Lock()
        self.values: dict = {}
        self.raw = ""
        self.count = 0          # valid $LVX frames
        self.rx = 0             # every '$' sentence received
        self.bad = 0            # sentences rejected by the checksum
        self.junk = 0           # lines that are not '$'/'#' sentences
        self.bad_sample = ""    # last rejected sentence
        self.bad_ts = None
        self.reset_ts = time.time()
        self.start = time.time()
        self.last_rx = None
        self.error = None
        self.paused = False
        self.frozen = False

    def update(self, fields, raw, now, count=True):
        with self._lock:
            self.values.update(fields)
            self.raw = raw
            self.last_rx = now
            if count:
                self.count += 1

    def set_error(self, msg):
        with self._lock:
            self.error = msg

    def note_sentence(self, valid: bool, text: str = ""):
        with self._lock:
            self.rx += 1
            if not valid:
                self.bad += 1
                self.bad_sample = text
                self.bad_ts = time.time()

    def note_junk(self):
        with self._lock:
            self.junk += 1

    def reset_counters(self):
        with self._lock:
            self.rx = self.bad = self.junk = 0
            self.bad_sample = ""
            self.bad_ts = None
            self.reset_ts = time.time()

    def toggle(self, attr):
        with self._lock:
            setattr(self, attr, not getattr(self, attr))
            return getattr(self, attr)

    def snapshot(self):
        with self._lock:
            return {
                "values": dict(self.values),
                "raw": self.raw,
                "count": self.count,
                "bad": self.bad,
                "rx": self.rx,
                "junk": self.junk,
                "bad_sample": self.bad_sample,
                "bad_ts": self.bad_ts,
                "reset_ts": self.reset_ts,
                "start": self.start,
                "last_rx": self.last_rx,
                "error": self.error,
                "paused": self.paused,
                "frozen": self.frozen,
            }


# --------------------------------------------------------------------------
# CSV logger + probe history
# --------------------------------------------------------------------------
class Recorder:
    """One CSV row per measurement frame: capture time + real distance."""

    def __init__(self, directory: str, units: str, stamp: str):
        self.path = os.path.join(directory or ".", f"{stamp}-radar.txt")
        self._fh = open(self.path, "w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._fh)
        self._writer.writerow(
            [
                "captured_at",
                "epoch_seconds",
                f"actual_distance_{units}",
                f"average_distance_{units}",
                "snr_db",
                "temperature_c",
                "raw_sentence",
            ]
        )
        self._fh.flush()
        self.rows = 0

    def write(self, fields, raw, stamp, epoch):
        self._writer.writerow(
            [
                stamp.isoformat(timespec="milliseconds"),
                f"{epoch:.3f}",
                _cell(fields.get("L1")),
                _cell(fields.get("L2")),
                _cell(fields.get("S1")),
                _cell(fields.get("T1")),
                raw,
            ]
        )
        self._fh.flush()
        self.rows += 1

    def close(self):
        try:
            self._fh.close()
        except Exception:
            pass


def _cell(value):
    return "" if value is None else f"{value:g}"


class CommandLog:
    """Append-only probe history: sent_at, command, ok, rtt_ms, ..."""

    def __init__(self, path: str):
        self.path = path
        self._fh = open(path, "w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._fh)
        self._writer.writerow(["sent_at", "command", "ok", "rtt_ms", "reply_lines", "error"])
        self._fh.flush()
        self.lines = 0

    def write(self, sent_at, command, ok, rtt, n_lines, error=""):
        self._writer.writerow(
            [
                dt.datetime.fromtimestamp(sent_at).astimezone().isoformat(timespec="milliseconds"),
                command,
                int(ok),
                "" if rtt is None else f"{rtt:.1f}",
                n_lines,
                error,
            ]
        )
        self._fh.flush()
        self.lines += 1

    def close(self):
        try:
            self._fh.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# TX link monitor (periodic #get_info)
# --------------------------------------------------------------------------
class LinkMonitor(threading.Thread):
    """Probe the device with #get_info and tally the replies.

    The serial reader thread feeds every received line into feed_line(); a
    reply block is complete when a '$' NMEA sentence arrives after at least
    one '#' line, or after a short quiet gap (the device sends no terminator).
    """

    def __init__(self, ser, interval=10.0, timeout=1.5, quiet=0.25,
                 cmd_log: CommandLog | None = None, snapshot_path: str | None = None):
        super().__init__(daemon=True, name="link-monitor")
        self.ser = ser
        self.interval = max(0.5, interval)
        self.timeout = timeout
        self.quiet = quiet
        self.cmd_log = cmd_log
        self.snapshot_path = snapshot_path
        self.wake = threading.Event()
        self._stop_evt = threading.Event()   # not '_stop': that is a Thread method
        self._lock = threading.Lock()
        self._pending = None
        self.stats = {
            "sent": 0, "ok": 0, "timeout": 0, "bad": 0,
            "fail_streak": 0, "rtt_ms": None, "last_ok": None, "last_error": "",
        }
        self.info: dict = {}
        self._jobs: queue.Queue = queue.Queue()
        self.job = None

    # -- called from the serial reader thread -----------------------------
    def feed_line(self, text: str):
        with self._lock:
            p = self._pending
            if p is None:
                return
            if text.startswith("#"):
                now = time.time()
                if not p["lines"]:
                    p["first"] = now
                p["lines"].append(text)
                p["last"] = now
            elif text.startswith("$") and p["lines"]:
                p["complete"] = True

    # -- thread control ----------------------------------------------------
    def request_probe(self):
        self.wake.set()

    def stop(self):
        self._stop_evt.set()
        self.wake.set()

    def snapshot(self):
        with self._lock:
            return {
                "stats": dict(self.stats),
                "info": dict(self.info),
                "job": dict(self.job) if self.job else None,
            }

    # -- command exchange --------------------------------------------------
    def _exchange(self, cmd, timeout=None, quiet=None):
        """Send one command and collect the '#' reply lines.

        The device does not terminate a reply block, so it ends when a '$'
        sentence arrives or when no '#' line has been seen for *quiet* s.
        """
        timeout = self.timeout if timeout is None else timeout
        quiet = self.quiet if quiet is None else quiet
        start = time.time()
        with self._lock:
            self._pending = {
                "start": start, "lines": [], "first": None,
                "last": None, "complete": False,
            }

        error = ""
        try:
            self.ser.write((cmd + CMD_TERMINATOR).encode("ascii"))
            self.ser.flush()
        except (serial.SerialException, OSError) as exc:
            error = str(exc)

        deadline = start + timeout
        while time.time() < deadline:
            with self._lock:
                p = self._pending
                if p is None:
                    break
                if p["complete"] or (
                    p["lines"] and p["last"] is not None
                    and (time.time() - p["last"]) >= quiet
                ):
                    break
            time.sleep(0.02)

        with self._lock:
            p = self._pending
            self._pending = None

        rtt = None if p["first"] is None else (p["first"] - start) * 1000.0
        return {
            "cmd": cmd, "lines": p["lines"], "rtt": rtt,
            "ok": bool(p["lines"]), "error": error,
            "start": start, "elapsed": time.time() - start,
        }

    # -- periodic TX probe -------------------------------------------------
    def probe(self):
        res = self._exchange(GET_INFO)
        info = parse_info(res["lines"])
        ok = bool(info)
        self._record(GET_INFO, ok, res, error=res["error"] if not ok else "")
        if ok and info != self.info:
            self.info = info
            if self.snapshot_path:
                try:
                    write_info_snapshot(self.snapshot_path, info)
                except OSError:
                    pass
        return ok

    def _record(self, cmd, ok, res, error=""):
        s = self.stats
        s["sent"] += 1
        if ok:
            s["ok"] += 1
            s["fail_streak"] = 0
            s["rtt_ms"] = res["rtt"]
            s["last_ok"] = time.time()
            s["last_error"] = ""
        elif res["lines"]:
            s["bad"] += 1
            s["fail_streak"] += 1
            s["last_error"] = "unparsable reply"
        else:
            s["timeout"] += 1
            s["fail_streak"] += 1
            s["last_error"] = error or "no reply"
        if self.cmd_log is not None:
            self.cmd_log.write(res["start"], cmd, ok, res["rtt"],
                               len(res["lines"]), s["last_error"])

    # -- setting a parameter (runs in this thread, keeps one exchange at a time)
    def submit_set(self, param, value_text):
        with self._lock:
            job = self.job
            if job is not None and not job.get("done", True):
                return False                      # already busy
            self.job = {
                "label": param["label"], "key": param["key"], "param": param,
                "value": value_text, "done": False, "ok": None,
                "cmd": f"#set_{param['key']}={value_text}",
                "started": time.time(), "ack": [], "message": "sending...",
            }
        self._jobs.put((param, value_text))
        self.wake.set()
        return True

    def _run_set(self, param, value_text):
        cmd = f"#set_{param['key']}={value_text}"
        res = self._exchange(cmd)
        ack = res["lines"]
        ack_ok = any(line.lower().rstrip().endswith(":ok") for line in ack)
        self._record(cmd, ack_ok, res, error="no ack")

        info_res = self._exchange(GET_INFO)
        info = parse_info(info_res["lines"])
        field = param["info"]
        old = self.info.get(field)
        new = info.get(field)
        match = values_match(value_text, new, param.get("is_int", False))

        if match:
            msg = "applied - read-back matches"
        elif new is None:
            msg = "no read-back from #get_info"
        elif ack_ok:
            msg = "device accepted the command but the value did not change"
        else:
            msg = "rejected - no ack and no change"

        with self._lock:
            self.job = {
                "label": param["label"], "key": param["key"], "param": param,
                "value": value_text, "done": True, "ok": bool(match),
                "cmd": cmd, "ack": ack, "old": old, "new": new,
                "rtt": res["rtt"], "message": msg, "started": res["start"],
            }
        if info:
            self.info = info
            if self.snapshot_path:
                try:
                    write_info_snapshot(self.snapshot_path, info)
                except OSError:
                    pass

    def run(self):
        self.probe()                       # probe immediately at start-up
        while not self._stop_evt.is_set():
            try:
                param, value = self._jobs.get_nowait()
            except queue.Empty:
                pass
            else:
                self._run_set(param, value)     # set + read-back verify
                continue
            self.wake.wait(self.interval)
            self.wake.clear()
            if self._stop_evt.is_set():
                break
            self.probe()


# --------------------------------------------------------------------------
# serial reader thread
# --------------------------------------------------------------------------
def reader_loop(ser, shared: SharedState, recorder, stop: threading.Event,
                link: LinkMonitor | None = None):
    while not stop.is_set():
        try:
            raw = ser.readline()
        except (serial.SerialException, OSError) as exc:
            shared.set_error(f"serial read failed: {exc}")
            return
        if not raw:
            continue  # read timeout
        text = decode_line(raw)
        if not text:
            continue

        now = time.time()
        if text.startswith("#"):
            if link is not None:
                link.feed_line(text)
            continue
        if not text.startswith("$"):
            shared.note_junk()        # framing garbage, e.g. wrong baud rate
            continue

        if link is not None:
            link.feed_line(text)      # any '$' sentence ends an info block

        parsed = parse_sentence(text)
        if parsed is None:
            shared.note_sentence(False, text)   # bad checksum
            continue
        shared.note_sentence(True)
        kind, fields = parsed
        if kind in ("LVX", "ANG"):
            shared.update(fields, text, now, count=(kind == "LVX"))
            if kind == "LVX" and recorder is not None:
                if not shared.paused and "L1" in fields:
                    recorder.write(fields, text, dt.datetime.now().astimezone(), now)


# --------------------------------------------------------------------------
# formatting / layout helpers
# --------------------------------------------------------------------------
def fmt_value(kind: str, value, units: str) -> str:
    if value is None:
        return "--"
    if kind == "dist":
        return f"{value:.2f} {units}"
    if kind == "temp":
        return f"{value:.1f} \u00b0C"
    if kind == "snr":
        return f"{value:.2f} dB"
    if kind == "std":
        return f"{value:.1f}"
    if kind == "angle":
        return f"{value:+.2f}\u00b0"
    return f"{value:g}"


def fmt_rate(count: int, elapsed: float) -> str:
    if elapsed <= 0.5 or count == 0:
        return "-- Hz"
    return f"{count / elapsed:.1f} Hz"


def fmt_probe(stats, with_age=False) -> str:
    sent = stats.get("sent", 0)
    if not sent:
        return "TX probe: waiting"
    pct = 100.0 * stats["ok"] / sent
    txt = f"TX probe {stats['ok']}/{sent} ok ({pct:.0f}%)"
    if stats["rtt_ms"] is not None:
        txt += f" \u00b7 {stats['rtt_ms']:.0f} ms"
    if with_age and stats["last_ok"] is not None:
        txt += f" \u00b7 last {time.time() - stats['last_ok']:.0f}s ago"
    if stats.get("fail_streak"):
        txt += f" \u00b7 {stats['fail_streak']} fails"
    return txt


def param_range(param, units="m"):
    """Accepted (low, high) bounds expressed in *units*."""
    if "lo_m" in param:
        factor = UNIT_TO_M.get(units, 1.0)
        return param["lo_m"] / factor, param["hi_m"] / factor
    return param["lo"], param["hi"]


def parse_param_value(text: str, param: dict, units: str = "m"):
    """Validate a user-entered value. Returns (text_to_send, error)."""
    text = (text or "").strip()
    if not text:
        return None, "no value entered"
    try:
        value = float(text)
    except ValueError:
        return None, f"'{text}' is not a number"
    if param.get("is_int"):
        if value != int(value):
            return None, f"{param['label']} must be a whole number"
        out = str(int(value))
    else:
        out = text
    lo, hi = param_range(param, units)
    if not (lo <= value <= hi):
        return None, f"out of range {lo:g}..{hi:g}" + (f" {units}" if param.get("unit") else "")
    return out, None


def parse_readback(text, scaled=True):
    """Decode a numeric #get_info value.

    Firmware 2.5.4 prints thousandths-scaled values as "%d.%d" (integer
    thousandths, no zero padding), so the fraction is misleading:
        0.05  -> '0.50'        0.005 -> '0.5'
        1.05  -> '1.49'   (1.05 * 1000 truncated to 1049)
        -0.25 -> '0.-250'
    Passing scaled=False parses the text literally (integers, counts, etc).
    """
    if text is None:
        return None
    t = str(text).strip()
    if not t:
        return None
    if not scaled or "." not in t:
        try:
            return float(t)
        except ValueError:
            return None
    whole, _, frac = t.partition(".")
    try:
        w = int(whole or 0)
    except ValueError:
        return None
    sign = -1 if frac.startswith("-") else 1
    digits = frac.lstrip("+-")
    if not digits.isdigit():
        return None
    return w + sign * int(digits) / 1000.0


def values_match(requested_text: str, read_back, is_int: bool = False) -> bool:
    """Compare the requested value with what #get_info reported."""
    try:
        want = float(requested_text)
    except (TypeError, ValueError):
        return False
    got = parse_readback(read_back, scaled=not is_int)
    if got is None:
        return False
    if is_int:
        return int(round(got)) == int(round(want))
    # the firmware truncates to thousandths, so allow 2 thousandths of slack
    return abs(got - want) <= max(2e-3, 1e-3 * abs(want))


def pretty_value(key, raw):
    """Human readable value for the settings panel (decodes the level offset)."""
    if key not in ("level_offset", "sensor_height"):
        return raw
    val = parse_readback(raw)
    if val is None:
        return raw
    frac = str(raw).partition(".")[2].lstrip("+-")
    if frac.isdigit() and int(frac) == 0:      # 0.0, 10.0 -> nothing to decode
        return raw
    shown = f"{val:.3f}"
    return raw if shown == str(raw) else f"{shown} [raw {raw}]"


def csum_attr(csum: dict) -> str:
    """Colour bucket for the checksum accumulator."""
    rx = csum.get("rx", 0)
    bad = csum.get("bad", 0) + csum.get("junk", 0)
    if not bad:
        return None
    pct = 100.0 * bad / rx if rx else 100.0
    return "error" if pct >= 1.0 else "warn"


def fmt_csum(csum: dict, long: bool = False) -> str:
    """Compact checksum-error accumulator text."""
    rx = csum.get("rx", 0)
    bad = csum.get("bad", 0)
    junk = csum.get("junk", 0)
    if not rx and not bad and not junk:
        return "csum: no sentences yet"
    pct = (100.0 * bad / rx) if rx else 0.0
    txt = f"csum {bad} err / {rx} sent ({pct:.2f}%)"
    if junk:
        txt += f" \u00b7 junk {junk}"
    if bad and csum.get("bad_ts"):
        txt += f" \u00b7 last err {time.time() - csum['bad_ts']:.0f}s ago"
    elif not bad:
        txt += " \u00b7 no errors"
    if long and csum.get("reset_ts"):
        txt += f" \u00b7 since {dt.datetime.fromtimestamp(csum['reset_ts']):%H:%M:%S}"
    return txt


def box_chars():
    try:
        "\u250c\u2510\u2514\u2518\u2500\u2502".encode(
            getattr(sys.stdout, "encoding", None) or "utf-8"
        )
        return {
            "tl": "\u250c", "tr": "\u2510", "bl": "\u2514", "br": "\u2518",
            "h": "\u2500", "v": "\u2502",
        }
    except Exception:
        return {"tl": "+", "tr": "+", "bl": "+", "br": "+", "h": "-", "v": "|"}


def addstr(stdscr, y, x, text, attr=0):
    try:
        stdscr.addstr(y, x, text, attr)
    except (curses.error, UnicodeEncodeError):
        pass


# --------------------------------------------------------------------------
# curses UI
# --------------------------------------------------------------------------
COLORS = {}


def init_colors():
    COLORS.clear()
    if not curses.has_colors():
        return
    try:
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        curses.init_pair(1, curses.COLOR_CYAN, bg)    # titles / borders
        curses.init_pair(2, curses.COLOR_WHITE, bg)   # values
        curses.init_pair(3, curses.COLOR_GREEN, bg)   # primary value
        curses.init_pair(4, curses.COLOR_YELLOW, bg)  # warning
        curses.init_pair(5, curses.COLOR_RED, bg)     # error
        COLORS.update(
            title=curses.color_pair(1),
            value=curses.color_pair(2) | curses.A_BOLD,
            primary=curses.color_pair(3) | curses.A_BOLD,
            warn=curses.color_pair(4) | curses.A_BOLD,
            error=curses.color_pair(5) | curses.A_BOLD,
            border=curses.color_pair(1) | curses.A_DIM,
        )
    except curses.error:
        COLORS.clear()


def draw_card(stdscr, y, x, width, height, title, value, value_attr, box):
    """height == 4 -> bordered card, height == 2 -> compact (no border)."""
    inner = max(0, width - 3)
    border = COLORS.get("border", curses.A_DIM)
    if height >= 4:
        addstr(stdscr, y, x, box["tl"] + box["h"] * (width - 2) + box["tr"], border)
        for dy in (1, 2):
            addstr(stdscr, y + dy, x, box["v"], border)
            addstr(stdscr, y + dy, x + width - 1, box["v"], border)
        addstr(stdscr, y + 3, x, box["bl"] + box["h"] * (width - 2) + box["br"], border)
        addstr(stdscr, y + 1, x + 2, title[:inner], COLORS.get("title", curses.A_DIM))
        addstr(stdscr, y + 2, x + 2, value[:inner], value_attr)
    else:
        addstr(stdscr, y, x, title[:width], COLORS.get("title", curses.A_DIM))
        addstr(stdscr, y + 1, x, value[:width], value_attr)


def draw_info_panel(stdscr, info: dict, h, w, stats=None, snapshot_path=None,
                    csum=None, color_for=None, probe_enabled=True):
    """Full-screen view of the device settings block."""
    addstr(stdscr, 0, 0,
           " Device settings (#get_info) - g: refresh   i/Esc: close ".ljust(w - 1)[: w - 1],
           curses.A_REVERSE)

    footer_lines = []
    if stats:
        footer_lines.append((fmt_probe(stats, with_age=True), "error" if stats.get("fail_streak", 0) >= 3 else None))
    if csum:
        footer_lines.append((fmt_csum(csum, long=True), csum_attr(csum) if color_for else None))
        if csum.get("bad_sample"):
            footer_lines.append((f"last bad: {csum['bad_sample']}", None))
    if snapshot_path:
        footer_lines.append((f"snapshot: {snapshot_path}", None))
    footer = len(footer_lines)
    if not info:
        msg = ("settings unavailable: running with --no-probe"
               if not probe_enabled
               else "no reply to #get_info - check the TX line")
        addstr(stdscr, 2, 2, msg, COLORS.get("error", curses.A_BOLD))
    else:
        items = list(info.items())
        cols = 1 if w < 78 else 2
        rows_avail = max(1, h - 2 - footer)
        total = len(items)
        per_col = math.ceil(total / cols)
        if per_col > rows_avail:        # more fields than rows: truncate
            per_col = rows_avail
            items = items[: per_col * cols]
        key_w = min(30, max(len(k) for k, _ in items) + 1)
        col_w = max(key_w + 8, w // cols)
        for i, (key, value) in enumerate(items):
            r = i % per_col
            c = i // per_col
            value = pretty_value(key, value)
            text = f"{key:<{key_w}}{value}"
            addstr(stdscr, 1 + r, 1 + c * col_w, text[: col_w - 1], COLORS.get("value", 0))
        if len(items) < total:
            addstr(stdscr, 2 + per_col, 1, f"... {total - len(items)} more fields",
                   curses.A_DIM)

    for i, (text, attr_key) in enumerate(footer_lines):
        attr = COLORS[attr_key] if attr_key in COLORS else curses.A_DIM
        addstr(stdscr, h - footer + i, 1, (" " + text)[: w - 2], attr)


def param_label(param, units):
    return f"{param['label']} ({units})" if param.get("unit") else param["label"]


def param_current(param, info):
    return (info or {}).get(param["info"], "?")


def draw_set_dialog(stdscr, h, w, dlg, meta, info, link_job):
    """Modal 'set parameter' dialog: dropdown, value entry, result."""
    box = box_chars()
    params = PARAMS
    inner_w = min(max(46, w - 10), 66)
    phase = dlg["phase"]

    if phase == "select":
        box_h = len(params) + 8
    elif phase == "value":
        box_h = 12
    else:
        box_h = 11
    box_h = min(box_h, h - 2)
    y0 = max(1, (h - box_h) // 2)
    x0 = max(1, (w - inner_w) // 2)
    box_w = inner_w
    border = COLORS.get("border", curses.A_DIM)

    # blank the area first, otherwise the cards show through the dialog
    blank = " " * box_w
    for dy in range(box_h):
        addstr(stdscr, y0 + dy, x0, blank, curses.A_NORMAL)

    # frame
    addstr(stdscr, y0, x0, box["tl"] + box["h"] * (box_w - 2) + box["tr"], border)
    for dy in range(1, box_h - 1):
        addstr(stdscr, y0 + dy, x0, box["v"], border)
        addstr(stdscr, y0 + dy, x0 + box_w - 1, box["v"], border)
    addstr(stdscr, y0 + box_h - 1, x0, box["bl"] + box["h"] * (box_w - 2) + box["br"], border)

    title = " Set parameter "
    addstr(stdscr, y0, x0 + 2, title, COLORS.get("title", curses.A_BOLD))
    iw = box_w - 4

    if phase == "select":
        addstr(stdscr, y0 + 2, x0 + 2, f"{'parameter':<26}{'current':<10}"[:iw],
               curses.A_DIM)
        for idx, param in enumerate(params):
            y = y0 + 3 + idx
            sel = idx == dlg["index"]
            label = param_label(param, meta["units"])
            cur = param_current(param, info)
            text = f"{'>' if sel else ' '} {label:<24}{cur:<10}"[:iw]
            addstr(stdscr, y + 0, x0 + 2, text,
                   curses.A_REVERSE if sel else 0)
        hint = " j/k or arrows: choose   Enter: edit   Esc: cancel "
        addstr(stdscr, y0 + box_h - 2, x0 + 2, hint[:iw], curses.A_DIM)
        note = PARAMS[dlg["index"]].get("note", "")
        addstr(stdscr, y0 + box_h - 3, x0 + 2, note[:iw], curses.A_DIM)

    elif phase == "value":
        param = params[dlg["index"]]
        addstr(stdscr, y0 + 2, x0 + 2, f"parameter : {param_label(param, meta['units'])}"[:iw],
               COLORS.get("value", 0))
        addstr(stdscr, y0 + 3, x0 + 2, f"current   : {param_current(param, info)}"[:iw],
               curses.A_DIM)
        buf = dlg.get("buffer", "")
        addstr(stdscr, y0 + 5, x0 + 2, "new value :", COLORS.get("title", 0))
        addstr(stdscr, y0 + 5, x0 + 14, (buf + "_")[: max(0, iw - 13)],
               COLORS.get("primary", curses.A_BOLD))
        if dlg.get("error"):
            addstr(stdscr, y0 + 7, x0 + 2, dlg["error"][:iw], COLORS.get("error", curses.A_BOLD))
        else:
            lo, hi = param_range(param, meta["units"])
            unit = f" {meta['units']}" if param.get("unit") else ""
            addstr(stdscr, y0 + 7, x0 + 2,
                   f"range {lo:g} .. {hi:g}{unit}"[:iw], curses.A_DIM)
        addstr(stdscr, y0 + 9, x0 + 2, " Enter: apply   Esc: back "[:iw], curses.A_DIM)

    else:                                   # apply / result
        job = link_job or {}
        addstr(stdscr, y0 + 2, x0 + 2, f"parameter : {dlg['label']}"[:iw],
               COLORS.get("value", 0))
        addstr(stdscr, y0 + 3, x0 + 2, f"command   : {dlg.get('cmd', '')}"[:iw],
               COLORS.get("title", 0))
        if phase == "apply":
            dots = "." * (1 + int(time.time() * 2) % 3)
            addstr(stdscr, y0 + 5, x0 + 2, f"waiting for ack + read-back{dots}"[:iw],
                   COLORS.get("warn", curses.A_BOLD))
        else:
            for k, line in enumerate((job.get("ack") or [])[:2]):
                addstr(stdscr, y0 + 4 + k, x0 + 2, f"ack       : {line}"[:iw], curses.A_DIM)
            key = job.get("key", "")
            old = pretty_value(key, job.get("old", "?"))
            new = pretty_value(key, job.get("new", "?"))
            addstr(stdscr, y0 + 6, x0 + 2, f"read-back : {old} -> {new}"[:iw],
                   COLORS.get("value", 0))
            ok = job.get("ok")
            addstr(stdscr, y0 + 8, x0 + 2, (" OK  " if ok else " FAILED  ") + str(job.get("message", ""))[: iw - 10],
                   COLORS.get("primary" if ok else "error", curses.A_BOLD))
        addstr(stdscr, y0 + box_h - 2, x0 + 2,
               (" Esc: close " if phase == "result" else " please wait ")[:iw], curses.A_DIM)


def draw(stdscr, shared: SharedState, meta, recorder, view):
    snap = shared.snapshot()
    if not snap["frozen"]:
        view["values"] = snap["values"]
        view["raw"] = snap["raw"]

    values = view["values"]
    h, w = stdscr.getmaxyx()
    stdscr.erase()

    if h < 8 or w < 30:
        addstr(stdscr, 0, 0, "terminal too small")
        stdscr.noutrefresh()
        curses.doupdate()
        return

    if view.get("show_info"):
        draw_info_panel(stdscr, view.get("info") or {}, h, w,
                        (view.get("link") or {}).get("stats"), meta.get("snapshot_path"),
                        snap, color_for=True, probe_enabled=meta.get("probe_enabled", True))
        stdscr.noutrefresh()
        curses.doupdate()
        return

    box = view["box"]
    elapsed = max(0.0, time.time() - snap["start"])
    link_stats = (view.get("link") or {}).get("stats", {})

    # ---- header -----------------------------------------------------
    stale = snap["last_rx"] is None or (time.time() - snap["last_rx"]) > 3.0
    if snap["error"]:
        status = "ERROR"
    elif snap["frozen"]:
        status = "FROZEN"
    elif stale:
        status = "NO DATA"
    else:
        status = "LIVE"
    if link_stats.get("fail_streak", 0) >= 3:
        status += " / TX FAIL"
    age = "" if snap["last_rx"] is None else f" \u00b7 {time.time() - snap['last_rx']:.1f}s ago"
    head = (
        f" Geolux LX-80 {box['v']} {meta['port']} @ {meta['baud']} 8N1 "
        f"{box['v']} {status} {box['v']} {fmt_rate(snap['count'], elapsed)}"
        f" {box['v']} {snap['count']} frames{age} "
    )
    addstr(stdscr, 0, 0, head.ljust(w - 1)[: w - 1],
           curses.A_REVERSE | (COLORS.get("error", 0) if link_stats.get("fail_streak", 0) >= 3 else 0))

    # ---- log file + TX probe tally ----------------------------------
    if snap["error"]:
        addstr(stdscr, 1, 0, f" {snap['error']}"[: w - 1], COLORS.get("error", curses.A_BOLD))
    elif link_stats.get("fail_streak", 0) >= 3:
        msg = f" {fmt_probe(link_stats)} - no response to {GET_INFO} ({link_stats.get('last_error', '')})"
        addstr(stdscr, 1, 0, msg[: w - 1], COLORS.get("error", curses.A_BOLD))
    else:
        log = f" log: {recorder.path} ({recorder.rows} rows)" if recorder is not None else " logging: off"
        txt = f"{log}  \u00b7  {fmt_probe(link_stats)}"
        if snap["paused"]:
            txt += "  [paused]"
        addstr(stdscr, 1, 0, txt[: w - 1], curses.A_DIM)

    # ---- cards ------------------------------------------------------
    n = len(CARDS)
    ncols = max(1, min(4, w // 22))
    nrows = math.ceil(n / ncols)
    x_gap = 1
    card_w = max(8, (w - (ncols - 1) * x_gap) // ncols)
    y0 = 3
    footer = 4          # checksum strip + raw sentence + key bar + margin

    # pick the roomiest layout that still fits: bordered -> compact -> tight
    for card_h, y_gap in ((4, 1), (2, 1), (2, 0)):
        if y0 + nrows * card_h + (nrows - 1) * y_gap <= h - footer:
            break
    block_h = nrows * card_h + (nrows - 1) * y_gap
    top = y0 + max(0, (h - footer - y0 - block_h) // 2)

    for i, (title, key, kind) in enumerate(CARDS):
        r, c = divmod(i, ncols)
        y = top + r * (card_h + y_gap)
        x = c * (card_w + x_gap)
        if y + card_h > h - footer:
            break
        attr = COLORS.get("value", curses.A_BOLD)
        val = values.get(key)
        if key == "L1":
            attr = COLORS.get("primary", curses.A_BOLD)
        if key == "S1" and val is not None and val < 10:
            attr = COLORS.get("warn", curses.A_BOLD)
        draw_card(stdscr, y, x, card_w, card_h, title, fmt_value(kind, val, meta["units"]), attr, box)

    # ---- checksum accumulator strip ---------------------------------
    csum_key = csum_attr(snap)
    csum_attr_val = COLORS.get(csum_key, curses.A_DIM) if csum_key else curses.A_DIM
    addstr(stdscr, h - 3, 0,
           f" {fmt_csum(snap)}   \u00b7   c: reset"[: w - 1], csum_attr_val)

    # ---- footer -----------------------------------------------------
    raw = view["raw"][: max(0, w - 3)]
    addstr(stdscr, h - 2, 0, f" {raw}"[: w - 1], curses.A_DIM)

    keys = " q quit  p pause log  f freeze  s set  i settings  g refresh "
    addstr(stdscr, h - 1, 0, keys.ljust(w - 1)[: w - 1], curses.A_REVERSE)

    if view.get("dialog"):
        draw_set_dialog(stdscr, h, w, view["dialog"], meta,
                        view.get("info") or {}, (view.get("link") or {}).get("job"))

    stdscr.noutrefresh()
    curses.doupdate()


def run_tui(stdscr, shared: SharedState, meta, recorder, link):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(100)
    init_colors()
    view = {"values": {}, "raw": "", "box": box_chars(), "show_info": False,
            "info": {}, "link": {}, "dialog": None}

    while True:
        ch = stdscr.getch()

        # ---- modal set-parameter dialog ----
        dlg = view["dialog"]
        if dlg is not None:
            if dlg["phase"] == "apply":
                job = (view.get("link") or {}).get("job")
                if job and job.get("done"):
                    dlg["phase"] = "result"
            elif dlg["phase"] == "select":
                if ch == 27 or ch in (ord("s"), ord("S")):
                    view["dialog"] = None
                elif ch in (curses.KEY_DOWN, ord("j")):
                    dlg["index"] = (dlg["index"] + 1) % len(PARAMS)
                elif ch in (curses.KEY_UP, ord("k")):
                    dlg["index"] = (dlg["index"] - 1) % len(PARAMS)
                elif ch in (10, 13, curses.KEY_ENTER):
                    dlg["phase"] = "value"
                    dlg["buffer"] = ""
                    dlg["error"] = None
            elif dlg["phase"] == "value":
                if ch == 27:
                    dlg["phase"] = "select"
                elif ch in (10, 13, curses.KEY_ENTER):
                    param = PARAMS[dlg["index"]]
                    text, err = parse_param_value(dlg.get("buffer", ""), param, meta["units"])
                    if err:
                        dlg["error"] = err
                    elif link is None:
                        dlg["error"] = "TX probe disabled (--no-probe)"
                    elif link.submit_set(param, text):
                        dlg["phase"] = "apply"
                        dlg["error"] = None
                        dlg["cmd"] = f"#set_{param['key']}={text}"
                        dlg["label"] = param_label(param, meta["units"])
                    else:
                        dlg["error"] = "another command is still in progress"
                elif ch in (curses.KEY_BACKSPACE, 8, 127):
                    dlg["buffer"] = dlg.get("buffer", "")[:-1]
                elif 32 <= ch < 127 and chr(ch) in "0123456789.-+":
                    if len(dlg.get("buffer", "")) < 16:
                        dlg["buffer"] = dlg.get("buffer", "") + chr(ch)
            else:                            # result
                if ch in (27, 10, 13, curses.KEY_ENTER, ord("s"), ord("S")):
                    view["dialog"] = None

            if link is not None:
                view["link"] = link.snapshot()
                view["info"] = view["link"]["info"]
            draw(stdscr, shared, meta, recorder, view)
            if meta["duration"] and time.time() - shared.start >= meta["duration"]:
                break
            continue

        if ch in (ord("q"), ord("Q")):
            break
        if ch in (ord("p"), ord("P")):
            shared.toggle("paused")
        elif ch in (ord("f"), ord("F")):
            shared.toggle("frozen")
        elif ch in (ord("s"), ord("S")):
            view["dialog"] = {"phase": "select", "index": 0, "buffer": "", "error": None}
        elif ch in (ord("i"), ord("I")):
            view["show_info"] = not view["show_info"]
        elif ch == 27:                       # Esc closes the settings panel
            view["show_info"] = False
        elif ch in (ord("c"), ord("C")):
            shared.reset_counters()
        elif ch in (ord("g"), ord("G")) and link is not None:
            link.request_probe()

        if link is not None:
            view["link"] = link.snapshot()
            view["info"] = view["link"]["info"]

        draw(stdscr, shared, meta, recorder, view)

        if meta["duration"] and time.time() - shared.start >= meta["duration"]:
            break


# --------------------------------------------------------------------------
# headless mode
# --------------------------------------------------------------------------
def run_headless(shared: SharedState, meta, recorder, link):
    last_count = 0
    last_probe = None
    while True:
        snap = shared.snapshot()
        if snap["count"] != last_count:
            last_count = snap["count"]
            v = snap["values"]
            stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            print(
                f"{stamp}  dist={fmt_value('dist', v.get('L1'), meta['units']):>10s}"
                f"  avg={fmt_value('dist', v.get('L2'), meta['units']):>10s}"
                f"  snr={fmt_value('snr', v.get('S1'), meta['units']):>10s}",
                flush=True,
            )
        if link is not None:
            stats = link.stats
            key = (stats["sent"], stats["ok"], stats["last_error"])
            if key != last_probe:
                last_probe = key
                print(f"           {fmt_probe(stats)}"
                      + (f"  err={stats['last_error']}" if stats["last_error"] else ""),
                      flush=True)
            info = link.info
            if info and not getattr(run_headless, "_info_shown", False):
                run_headless._info_shown = True
                print("           " + "  ".join(
                    f"{k}={info.get(k)}" for k in
                    ("device_type", "firmware", "serial_number", "unit_type", "nmea_protocol_flags")
                ), flush=True)
        if snap["error"]:
            print(f"error: {snap['error']}", file=sys.stderr, flush=True)
            return
        if meta["duration"] and time.time() - shared.start >= meta["duration"]:
            return
        time.sleep(0.05)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
USAGE_HELP = """\
geolux_tui.py [-h | --help | -?] [-p PORT] [-b BAUD] [--units UNIT] [-d DIR]
               [--no-log] [--no-tui] [--no-probe] [--probe-interval SEC]
               [--probe-timeout SEC] [--duration SEC]"""

DESCRIPTION_HELP = """\
Geolux LX-80 non-contact radar level meter: live terminal UI, RS-232
transmit-line probe with success tally, device settings browser, parameter
editing and CSV logging.

NMEA sentences (device -> host, manual ch. 6.1 / 7.1) are shown as cards with
the title above each value and logged to CSV.  The servicing protocol
(host -> device, manual ch. 7.2) is used for the TX probe and for changing
device parameters."""

EPILOG_HELP = """\
examples:
  ./geolux_tui.py                        live TUI on /dev/ttyUSB0 at 115200
  ./geolux_tui.py -p /dev/ttyUSB1        use another serial port
  ./geolux_tui.py -d ~/radar-logs        write the log files elsewhere
  ./geolux_tui.py --no-tui               plain text output (ssh, pipes, logs)
  ./geolux_tui.py --no-tui --duration 60 record one minute, then exit
  ./geolux_tui.py --no-probe             read-only: never write to the device
  ./geolux_tui.py --units ft             override the unit label
  ./geolux_tui.py -b 9600                device whose RS-232 baud was changed

tui keys:
  q  quit                    p  pause / resume CSV logging
  f  freeze the display      s  set a device parameter (dialog)
  i  device settings panel   g  refresh settings (#get_info now)
  c  reset the counters      Esc close a panel or dialog

  The set dialog (s) is a single "set" command: pick the parameter from the
  dropdown with j/k or the arrow keys, Enter to edit, type the value, Enter
  to apply, Esc to go back.  Every write is confirmed by reading #get_info
  back; the device ack and the old -> new values are shown, and a change is
  only reported as OK when the read-back matches.

files (all in --dir, default ./data/YYYY-MMDD - one directory per day - and
sharing one YYYY-MMDD-HHMMSS stamp):
  <stamp>-radar.txt       CSV, one row per measurement frame
  <stamp>-radar-cmd.log   CSV, one row per command/probe
  <stamp>-radar-info.txt  device settings snapshot (# key:value format)

  CSV columns:
    captured_at, epoch_seconds, actual_distance_<unit>, average_distance_<unit>,
    snr_db, temperature_c, raw_sentence

tx probe and link health:
  The LX-80 only transmits NMEA, so received data cannot prove your transmit
  line works.  This program sends "#get_info" (manual ch. 7.2) every
  --probe-interval seconds and tallies the replies; a reply needs the host TX
  line, the adapter and the device RX path all to work.

    NMEA flowing + probes answered -> link fully OK
    NMEA flowing + probes silent   -> host TX line or device RX is the problem
    nothing at all                 -> cable, power, baud rate or SDI-12 sleep

  The tally is shown on the main view and in the settings panel, and every
  probe is written to <stamp>-radar-cmd.log.  The directory is chosen when the
  program starts: a run that crosses midnight keeps writing to its own date.

settable parameters (single "set" dialog):
  Sensor height     #set_sensor_height=<value>   height above the gauge zero
  Level offset      #set_level_offset=<value>    small correction, same unit
  Averaging frames  #set_frame_number=<1..1000>  number of 10 Hz samples

  Sensor height defines the datum: level = sensor_height - distance, so
  raising it raises every reported level by the same amount and leaves the
  distance readings alone.  Ranges are checked in the device's own unit
  (sensor height 0..100 m on a metric unit, i.e. 0..100000 when the device is
  configured in mm).

  The measurement/output rate itself (measurement_frequency, 10 Hz on the unit
  tested) cannot be changed: it is not in the manual's command list and the
  plausible command names are silently ignored.  Averaging frames is the
  supported rate-related knob.

firmware notes (verified on firmware 2.5.4):
  * #set_level_offset shifts the reported distance, so the level moves the
    other way: level = sensor_height - distance.
  * thousandths-scaled values are printed as "%d.%d", so 0.05 reads back as
    "0.50", 1.05 as "1.49" and -0.25 as "0.-250".  The values are applied
    correctly; the program decodes this format before comparing.
  * the device answers "#set_<param>:OK" even for malformed input, so a write
    is only reported as successful when the read-back matches.

troubleshooting:
  "cannot open /dev/ttyUSB0"   check the cable and "ls -l /dev/ttyUSB0";
                               add your user to the "dialout" group if needed
  "NO DATA" but TX probes OK   wrong baud rate, NMEA flags = 2, or the device
                               is in SDI-12 sleep mode (power-cycle it and
                               reconnect within 60 s)
  high "csum" error count      marginal link, wrong baud rate or a noisy
                               adapter; untagged garbage is counted as "junk"
  "TX FAIL"                    the device is not answering requests - see
                               "tx probe and link health" above

install (mamba, not pip):
  mamba env create -f environment.yml    then:  mamba activate radar
  See README.md for the step-by-step WSL2 instructions (usbipd-forwarded USB
  adapter, dialout group, Windows Terminal).

see also:
  README.md                full documentation
  plot_radar.py --help     plot the logged CSV files with Bokeh

exit status:
  0  normal exit (q, --duration reached, Ctrl-C)
  2  could not open the serial port / missing pyserial
"""


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        prog="geolux_tui.py",
        add_help=False,
        usage=USAGE_HELP,
        description=DESCRIPTION_HELP,
        epilog=EPILOG_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("-h", "--help", "-?", action="help",
                    help="show this help and exit")
    ap.add_argument("-p", "--port", default=DEFAULT_PORT, metavar="PORT",
                    help="serial device (default: %(default)s)")
    ap.add_argument("-b", "--baud", type=int, default=DEFAULT_BAUD, metavar="BAUD",
                    help="RS-232 baud rate; the LX-80 uses 8N1 (default: %(default)s)")
    ap.add_argument("--units", choices=tuple(UNIT_NAMES.values()), default=None,
                    metavar="UNIT",
                    help="unit label for distances/levels; default is read from "
                         "#get_info unit_type (0=mm 1=cm 2=m 3=in 4=ft)")
    ap.add_argument("-d", "--dir", default=default_log_dir(), metavar="DIR",
                    help="directory for the log files, created if missing "
                         "(default: data/<YYYY-MMDD>, i.e. %(default)s)")
    ap.add_argument("--no-log", action="store_true",
                    help="do not create any log file (nothing is written to disk)")
    ap.add_argument("--no-tui", action="store_true",
                    help="no full-screen UI; print one line per frame plus probe "
                         "results (useful over ssh or when piping)")
    ap.add_argument("--no-probe", action="store_true",
                    help="never write to the device: disables the #get_info probes, "
                         "the settings panel and the set dialog")
    ap.add_argument("--probe-interval", type=float, default=10.0, metavar="SEC",
                    help="seconds between #get_info probes, minimum 0.5 "
                         "(default: %(default)s)")
    ap.add_argument("--probe-timeout", type=float, default=1.5, metavar="SEC",
                    help="seconds to wait for a command reply (default: %(default)s)")
    ap.add_argument("--duration", type=float, default=0.0, metavar="SEC",
                    help="exit after SEC seconds (default: 0 = run until q/Ctrl-C)")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    locale.setlocale(locale.LC_ALL, "")

    try:
        ser = serial.Serial(
            args.port,
            args.baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=1.0,
            write_timeout=1.0,
        )
    except (serial.SerialException, OSError) as exc:
        sys.stderr.write(f"cannot open {args.port}: {exc}\n")
        return 2

    configure_serial(ser)      # echo off, no CR/NL translation
    try:
        ser.reset_input_buffer()
    except (serial.SerialException, OSError):
        pass

    # one probe before any thread starts: validates TX and reports units
    info, rtt, n_lines = ({}, None, 0) if args.no_probe else read_info_block(
        ser, timeout=args.probe_timeout
    )
    units = args.units or UNIT_NAMES.get(_int(info.get("unit_type")), "m")

    shared = SharedState()
    log_dir = ensure_dir(args.dir)
    stamp = dt.datetime.now().strftime("%Y-%m%d-%H%M%S")  # YYYY-MMDD-HHMMSS
    recorder = None if args.no_log else Recorder(log_dir, units, stamp)
    cmd_log = None
    snapshot_path = None
    if not args.no_log and not args.no_probe:
        cmd_log = CommandLog(os.path.join(log_dir, f"{stamp}-radar-cmd.log"))
        snapshot_path = os.path.join(log_dir, f"{stamp}-radar-info.txt")
        if info:
            try:
                write_info_snapshot(snapshot_path, info)
            except OSError:
                pass

    meta = {
        "port": args.port,
        "baud": args.baud,
        "units": units,
        "duration": args.duration,
        "snapshot_path": snapshot_path,
        "probe_enabled": not args.no_probe,
    }

    link = None
    if not args.no_probe:
        link = LinkMonitor(
            ser,
            interval=args.probe_interval,
            timeout=args.probe_timeout,
            cmd_log=cmd_log,
            snapshot_path=snapshot_path,
        )
        link.info = info

    stop = threading.Event()
    reader = threading.Thread(
        target=reader_loop, args=(ser, shared, recorder, stop, link), daemon=True
    )
    reader.start()
    if link is not None:
        link.start()

    try:
        if args.no_tui:
            run_headless(shared, meta, recorder, link)
        else:
            curses.wrapper(run_tui, shared, meta, recorder, link)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        if link is not None:
            link.stop()
            link.join(timeout=2.0)
        reader.join(timeout=2.0)
        try:
            ser.close()
        except Exception:
            pass
        if recorder is not None:
            recorder.close()
            print(f"log saved: {recorder.path}  ({recorder.rows} rows)")
        if cmd_log is not None:
            cmd_log.close()
            print(f"probe log: {cmd_log.path}  ({cmd_log.lines} probes)")
        if snapshot_path and link is not None and link.info:
            print(f"settings snapshot: {snapshot_path}")
    return 0


def _int(value, default=None):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


if __name__ == "__main__":
    raise SystemExit(main())
