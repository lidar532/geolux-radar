# Geolux LX-80 radar tools

2026-1014 W. Wright github.com/lidar532

Two Python programs for a [Geolux LX-80](https://www.geolux.hr/) non-contact
radar level meter connected over RS-232 (USB serial adapter):

| Program | Purpose |
|---|---|
| `geolux_tui.py` | Live terminal UI, RS-232 transmit-line probe with success tally, device settings browser, parameter editing and CSV logging |
| `plot_radar.py` | Bokeh plots of the logged CSV files (static HTML or auto-refreshing live server) |
| `environment.yml` | mamba environment (conda-forge) with `pyserial` and `bokeh` |

Repository layout:

```
geolux_tui.py        live TUI, TX probe, settings, parameter editing, CSV logging
plot_radar.py       Bokeh plots (static HTML or live server) of the CSV logs
environment.yml     mamba environment (conda-forge)
requirements.txt    reference only - the supported install is mamba
README.md           this document
2026-1114-*.txt     sample captures (radar CSV, command log, settings snapshot)
```

Tested on a real LX-80: **firmware 2.5.4, serial number 102181**, reporting in
metres at ~10 Hz. Reference: *LX-80 User Manual* v2.7.2, chapters 6.1
(serial interface), 7.1 (NMEA protocol) and 7.2 (servicing protocol).

---

## Contents

1. [Install](#install)
2. [Quick start](#quick-start)
3. [Install and run on WSL2 (step by step)](#install-and-run-on-wsl2-step-by-step)
4. [geolux_tui.py — the TUI](#geolux_tuipy--the-tui)
5. [Files written](#files-written)
6. [TX probe and link diagnostics](#tx-probe-and-link-diagnostics)
7. [Changing device parameters](#changing-device-parameters)
8. [Protocol reference](#protocol-reference)
9. [Settings available on the device](#settings-available-on-the-device)
10. [plot_radar.py — plotting](#plot_radarpy--plotting)
11. [Firmware quirks and gotchas](#firmware-quirks-and-gotchas)
12. [Troubleshooting](#troubleshooting)
13. [Design notes and extending](#design-notes-and-extending)
14. [Limitations](#limitations)

---

## Install

Everything is installed with **mamba** (conda-forge). **Do not use pip** for
this project:

* conda-forge ships a tested `pyserial`/`bokeh` pair built for your exact
  Python version, so nothing is compiled on your machine;
* conda environments are immune to the "externally managed environment"
  (PEP 668) restriction that makes plain `pip install` fail on Debian 12+,
  Ubuntu 23.04+ and their WSL counterparts;
* mixing pip into a conda environment can silently break binary packages
  (numpy, tornado, ...) that `bokeh` depends on.

`requirements.txt` is kept for reference only — the supported install is the
`environment.yml` below.

### 1. Install mamba (Miniforge)

```bash
curl -L -O "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-$(uname -m).sh"
bash "Miniforge3-Linux-$(uname -m).sh" -b -p "$HOME/miniforge3"
"$HOME/miniforge3/bin/mamba" init bash
exec bash                     # or open a new shell
mamba --version
```

`$(uname -m)` picks `x86_64` or `aarch64` automatically (the latter for
Snapdragon/ARM Windows machines).

### 2. Create and activate the environment

```bash
cd ~/github-projects/geolux-radar   # wherever the programs live
mamba env create -f environment.yml
mamba activate radar
python -c "import serial, bokeh; print(serial.__version__, bokeh.__version__)"
# 3.5 3.4.3        (or newer bokeh on Python 3.10+)
```

`environment.yml` asks for `python>=3.9`, `pyserial>=3.5` and `bokeh>=3.4`, and
mamba's solver picks a compatible set: bokeh 3.4.x on Python 3.9, newer bokeh
on 3.10+. Nothing is hard-pinned, so the environment keeps working across
releases.

Everything the programs need is in that environment: `curses` and `termios`
ship with CPython on Linux, so no system packages are required.

### 3. Run without activating (scripts, cron, systemd)

```bash
mamba run -n radar ./geolux_tui.py -d ~/radar-logs
```

### 4. Serial port access

Your user must be able to open the port:

```bash
sudo usermod -aG dialout "$USER"     # then log out / restart WSL
ls -l /dev/ttyUSB0                   # usually root:dialout, 660
```

### 5. Make the programs executable (optional)

```bash
chmod +x geolux_tui.py plot_radar.py
```

Both programs print extensive help with `--help`, `-h` or `-?`.

---

## Quick start

```bash
mamba activate radar
./geolux_tui.py                                   # live TUI on /dev/ttyUSB0
./geolux_tui.py -d ~/radar-logs                   # logs go elsewhere
./geolux_tui.py --no-tui --duration 60            # one minute, plain text
./plot_radar.py                                  # plot the newest log file
./plot_radar.py --live --no-open                 # live plot on :5006
```

The port is opened as **115200 8N1 with echo disabled** (the `termios` `ECHO`,
`ECHONL`, `ICANON` and `ISIG` bits are cleared) and **all `\r` characters are
stripped** from received lines. `\r\n` is used as the command terminator.

---

## Install and run on WSL2 (step by step)

Both programs run natively in WSL2. The one thing WSL2 cannot do is see
Windows COM ports, so the USB serial adapter has to be forwarded into the
distro with [usbipd-win](https://github.com/dorssel/usbipd-win). This is the
complete sequence, from a stock Windows machine to a running TUI.

The adapter tested here is an **FTDI FT232** (`0403:6001`); CH340, CP210x and
CDC-ACM adapters work the same way.

### Step 1 — Windows: WSL2 and a real terminal

In an **Administrator** PowerShell:

```powershell
wsl --install -d Ubuntu        # or: wsl --update  if WSL is already installed
wsl --update                   # pick up the latest WSL2 kernel
wsl --version                  # must report WSL version 2.x
```

Reboot if asked, create the Linux user, and open the distribution in
**Windows Terminal**. Do not use the legacy console — it cannot render the
curses TUI (see [issue 14](#wsl2-potential-issues-and-fixes)).

### Step 2 — Windows: forward the adapter with usbipd

```powershell
winget install --exact dorssel.usbipd-win
usbipd list                    # find the adapter, note the BUSID (e.g. 1-3)
usbipd bind   --busid 1-3      # Administrator, one time, survives reboots
usbipd attach --wsl --busid 1-3 --auto-attach
```

What the `STATE` column means:

| State | Action |
|---|---|
| `Not shared` | run `usbipd bind --busid <id>` (Administrator, once) |
| `Shared` | run `usbipd attach --wsl --busid <id>` |
| `Attached` | the device is already inside WSL |

### Step 3 — WSL2: check the device node and permissions

```bash
lsusb | grep -iE "ftdi|ch34|cp210|serial"
ls -l /dev/ttyUSB* /dev/ttyACM* 2>/dev/null
groups | tr ' ' '\n' | grep -x dialout
dmesg | tail -5                  # e.g. "ftdi_sio ... attached to ttyUSB0"
```

* FTDI / CH340 / CP210x adapters appear as **`/dev/ttyUSB0`**.
* CDC-ACM adapters appear as **`/dev/ttyACM0`** — pass `-p /dev/ttyACM0`.

If `usbipd attach` complains that the `usbip` client is missing:

```bash
sudo apt update && sudo apt install -y usbip hwdata
# older Debian/Ubuntu without a "usbip" package:
sudo apt install -y linux-tools-generic hwdata
sudo update-alternatives --install /usr/local/bin/usbip usbip \
  "$(ls -d /usr/lib/linux-tools/*/usbip | tail -1)" 20
```

If the node exists but you get `Permission denied`:

```bash
sudo usermod -aG dialout "$USER"
# then, from Windows PowerShell:  wsl --shutdown
```

…and reopen the terminal.

### Step 4 — WSL2: install mamba

```bash
curl -L -O "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-$(uname -m).sh"
bash "Miniforge3-Linux-$(uname -m).sh" -b -p "$HOME/miniforge3"
"$HOME/miniforge3/bin/mamba" init bash
exec bash
mamba --version
```

**Do not `pip install` anything here** — see [Install](#install) for why, and
[issue 19](#wsl2-potential-issues-and-fixes) if `mamba` is not found.

### Step 5 — WSL2: create the environment

Keep the project in the Linux filesystem (`~/github-projects/geolux-radar`), not in `/mnt/c`:

```bash
cd ~/github-projects/geolux-radar
mamba env create -f environment.yml
mamba activate radar
python -c "import serial, bokeh; print(serial.__version__, bokeh.__version__)"
```

### Step 6 — WSL2: first run

```bash
./geolux_tui.py
```

A working start looks like this — note the **TX probe tally**, which proves the
adapter's transmit line reaches the device:

```
 Geolux LX-80 │ /dev/ttyUSB0 @ 115200 8N1 │ LIVE │ 9.6 Hz │ 37 frames · 0.1s ago
 log: /tmp/radar/2026-1114-151137-radar.txt (37 rows)  ·  TX probe 1/1 ok (100%) · 48 ms
┌──────────────────────┐ ┌──────────────────────┐ ┌──────────────────────┐
│ Actual distance      │ │ Average distance     │ │ Level                │
│ 3.58 m               │ │ 3.58 m               │ │ 6.42 m               │
└──────────────────────┘ └──────────────────────┘ └──────────────────────┘
```

Keys: `q` quit, `p` pause logging, `f` freeze, `s` set parameter, `i` device
settings, `g` refresh, `c` reset counters, `Esc` close a panel.

### Step 7 — WSL2: record and plot

```bash
./geolux_tui.py -d ~/radar-logs                      # TUI + CSV
./geolux_tui.py --no-tui --duration 3600 -d ~/radar-logs
./plot_radar.py --no-open                           # static HTML
./plot_radar.py --live --no-open --port 5006        # http://localhost:5006
```

Use `--no-open` and open things from Windows: WSL2 forwards `localhost`, and
the static HTML is reachable at `\\wsl$\Ubuntu\home\<user>\...`.

### Day-to-day on WSL2

```powershell
usbipd list                     # Attached?  -> start WSL and run
usbipd attach --wsl --busid 1-3 # if it shows "Shared" again
```

```bash
mamba activate radar
cd ~/github-projects/geolux-radar && ./geolux_tui.py
```

---

### WSL2 potential issues and fixes

#### Windows, USB and usbipd

1. **WSL2 cannot see Windows COM ports.** There is no `/dev/ttyS*` for them
   (that is WSL1 behaviour). USB serial devices must be attached with
   `usbipd`, as in Step 2.
2. **`usbipd bind` needs Administrator, `attach` does not.** Bind once per
   device; the bind survives reboots, the attach does not.
3. **The adapter must not be open on the Windows side.** The Geolux
   Instrument Configurator, PuTTY, a serial monitor or a background service
   holding the port makes `attach` fail ("device is in use"). Close the
   program; if a service holds it, unplug and replug the adapter.
4. **The attach is lost** when the distribution shuts down (idle timeout,
   `wsl --shutdown`, Windows restart), on unplug/replug, and usually after
   sleep/resume. `--auto-attach` re-attaches automatically when the device
   reappears; otherwise re-run `usbipd attach --wsl --busid <id>`.
   `usbipd list` shows `Shared` when a re-attach is needed.
5. **The BUSID changes** when you move the adapter to a different USB port.
   Re-run `usbipd list` and use the new ID (or keep it in one port).
6. **Attach fails with "WSL is not running".** Start the distribution first
   (`wsl -d Ubuntu`), then attach.
7. **The adapter never appears in Windows either.** Check Device Manager; the
   adapter needs a working Windows driver before usbipd can export it
   (FTDI, CH340 and CP210x drivers are normally installed by Windows Update).
   Some USB cables are charge-only — try another cable.
8. **`usbip` client missing in the distro.** `sudo apt install -y usbip
   hwdata` (older releases: `linux-tools-generic` + `update-alternatives`, see
   Step 3).
9. **No driver in the WSL kernel for exotic adapters.** Check `lsusb` and
   `dmesg`; FTDI/CH340/CP210x/CDC-ACM are built in. For anything else you may
   need a custom WSL kernel — rare, and a different adapter is usually easier.
10. **USB power management drops the device.** In Windows, open Device
    Manager → the adapter → *Power Management* → uncheck "Allow the computer
    to turn off this device to save power".
11. **Very old WSL.** `wsl --update` pulls a kernel with USB/IP support; without
    it the attach silently does nothing.

#### WSL, permissions, terminal and files

12. **`Permission denied` on `/dev/ttyUSB0`.** `sudo usermod -aG dialout
    "$USER"`, then `wsl --shutdown` and reopen the terminal (a group change
    only takes effect in a new session).
13. **No udev by default.** WSL2 has no `udev` daemon unless you enable
    `systemd=true` in `/etc/wsl.conf`, so custom udev rules are not applied;
    group membership is the reliable route.
14. **The TUI looks broken in the legacy console.** Use **Windows Terminal**;
    it sets `TERM=xterm-256color`, so box drawing, colours and the alternate
    screen work. In other terminals, test with
    `TERM=xterm-256color ./geolux_tui.py`.
15. **Working directory on `/mnt/c`.** I/O is very slow over the 9p mount
    (visible with the flush-per-row CSV logging), and files there may be
    mounted `noexec` so `./geolux_tui.py` fails. Keep the project in
    `~/`, and use `python geolux_tui.py` if execution is refused.
16. **CRLF line endings.** If the files came from Windows, the `#!` line ends
    in `\r` and `./geolux_tui.py` fails with
    `bad interpreter: /usr/bin/env python3^M`. Fix with
    `sed -i 's/\r$//' geolux_tui.py plot_radar.py` (or `dos2unix`).
17. **Clock drift after sleep/resume.** WSL2's clock can jump, which shows up
    as odd `captured_at` values in the CSV. `sudo hwclock -s` (with systemd
    enabled) or `wsl --shutdown` and reopen fixes it.
18. **Idle shutdown kills long recordings.** WSL2 stops the distribution when
    nothing is running. Keep a terminal open, or run recordings inside
    `tmux`/`screen`, or use a Windows scheduled task that keeps WSL alive.

#### mamba and the environment

19. **`mamba: command not found`.** Miniforge's shell hook was not activated:
    run `"$HOME/miniforge3/bin/mamba" init bash` and open a new shell, or
    `source "$HOME/miniforge3/etc/profile.d/conda.sh"` in the current one.
20. **Do not pip-install into the environment.** Mixing pip into a conda
    environment can break binary dependencies (`numpy`, `tornado`, ...) that
    `bokeh` needs. If a package were missing, use
    `mamba install -c conda-forge <package>`.
21. **`mamba activate` does nothing in a script.** Activation is a shell
    function; for non-interactive use either source the hook or run
    `mamba run -n radar ./geolux_tui.py`.
22. **Solving is slow.** Use `mamba` (not `conda`) — it is the fast solver.
    Optional: `mamba config --set channel_priority strict` in `~/.condarc`.
23. **bokeh version vs Python version.** Bokeh 3.4.x is the last release that
    supports Python 3.9; the env requests `python>=3.9`, so you get 3.4.x.
    Ask for `python>=3.10` in `environment.yml` if you want a newer bokeh.
24. **Corporate proxy blocks conda-forge.** Set `HTTPS_PROXY`/`HTTP_PROXY` in
    the environment before `mamba env create`, or configure proxies in
    `~/.condarc`.
25. **Disk space.** Miniforge plus the environment is roughly 1–2 GB.
26. **`pip install --user` leftovers.** A `~/.local/lib/python3*/site-packages`
    from earlier attempts can shadow conda packages. Remove it
    (`rm -rf ~/.local/lib/python3*`) if imports behave oddly.

#### Plotting and the browser

27. **Nothing opens after `plot_radar.py`.** The browser opener inside WSL
    usually does nothing; use `--no-open` and open the file/URL yourself.
28. **Opening the static HTML from Windows.** Use
    `\\wsl$\Ubuntu\home\<user>\<file>-plot.html` in Explorer, or install
    `wslu` and run `wslview <file>`.
29. **`--live` in a Windows browser.** WSL2 forwards `localhost`, so
    `http://localhost:5006` works. With mirrored networking or a VPN, use the
    WSL address instead (`hostname -I` inside WSL).
30. **Port already in use.** Pass another one, e.g. `--port 5007`.
31. **Windows firewall prompt** on the first bind — allow it (private
    networks are enough for localhost access).

#### Latency

32. **usbip adds a few milliseconds.** The TX probe RTT (about 48 ms on bare
    metal, dominated by the device's reply behaviour) may read slightly
    higher; at 10 Hz this has no practical effect.

---

## geolux_tui.py — the TUI

### Layout

```
 Geolux LX-80 │ /dev/ttyUSB0 @ 115200 8N1 │ LIVE │ 9.6 Hz │ 37 frames · 0.1s ago
 log: 2026-1114-141157-radar.txt (37 rows)  ·  TX probe 7/7 ok (100%) · 48 ms
┌──────────────────────┐ ┌──────────────────────┐ ┌──────────────────────┐
│ Actual distance      │ │ Average distance     │ │ Level                │
│ 3.58 m               │ │ 3.58 m               │ │ 6.42 m               │
└──────────────────────┘ └──────────────────────┘ └──────────────────────┘
...
 csum 0 err / 2345 sent (0.00%) · no errors   ·   c: reset
 $LVX,3.576,3.576,32,6.423,6.423,47.250,1.115*5F
 q quit  p pause log  f freeze  s set  i settings  g refresh
```

* **Header** — port/baud, state, frame rate, frame count and time since the
  last sentence. State is one of `LIVE`, `PAUSED`, `FROZEN`, `NO DATA`,
  `ERROR`, and `... / TX FAIL` is appended after 3 failed probes.
* **Line 1** — log file and row count, TX probe tally and round-trip time.
* **Cards** — one per value, **title above the value** (as requested). The
  grid uses 4/3/2/1 columns depending on the terminal width and falls back
  from bordered to compact to tight cards on short terminals, so all nine
  values fit down to about 52×20.
* **Checksum strip** — `err / sent` with a percentage, plus `junk` lines.
  Coloured yellow below 1 % and red at 1 % or more.
* **Raw sentence** — the last received line.
* **Key bar** — available keys.

### Values displayed

| Card | Field | Notes |
|---|---|---|
| Actual distance | `L1` | raw, unfiltered distance |
| Average distance | `L2` | distance after the device's averaging filter |
| Level | `L3` | water level relative to the gauge zero |
| Average level | `L4` | filtered level |
| Temperature | `T1` | internal instrument temperature, °C |
| SNR | `S1` | signal-to-noise ratio, dB; < 10 dB is shown in yellow |
| Level std. dev. | `S2` | standard deviation of the filtered samples |
| Tilt X / Tilt Y | `A1`, `A2` | instrument tilt (device sends hundredths of a degree) |

`level = sensor_height − distance` (on the test unit `sensor_height = 10.0 m`,
hence level ≈ 10.0 − distance). The sensor height can be changed from the
TUI with `s` → *Sensor height*.

Distance and level cards use the unit taken from `#get_info unit_type`
(0 = mm, 1 = cm, 2 = m, 3 = in, 4 = ft) unless `--units` is given. The same
unit is used in the CSV column names.

### Keys

| Key | Action |
|---|---|
| `q` | quit |
| `p` | pause / resume CSV logging (probing continues) |
| `f` | freeze the display (values and raw line) |
| `s` | open the **set parameter** dialog |
| `i` | device settings panel (the whole `#get_info` block) |
| `g` | probe now / refresh the settings |
| `c` | reset the checksum counters |
| `Esc` | close a panel or dialog |

### Command line options

```
geolux_tui.py [-h | --help | -?] [-p PORT] [-b BAUD] [--units UNIT] [-d DIR]
               [--no-log] [--no-tui] [--no-probe] [--probe-interval SEC]
               [--probe-timeout SEC] [--duration SEC]
```

| Option | Default | Meaning |
|---|---|---|
| `-h`, `--help`, `-?` | | extensive help and exit |
| `-p`, `--port` | `/dev/ttyUSB0` | serial device |
| `-b`, `--baud` | `115200` | RS-232 baud rate (always 8N1) |
| `--units` | from device | unit label: `mm`, `cm`, `m`, `in`, `ft` |
| `-d`, `--dir` | `.` | directory for the log files |
| `--no-log` | off | write nothing to disk |
| `--no-tui` | off | plain text output (one line per frame + probe results) |
| `--no-probe` | off | never write to the device (disables probing, settings and the set dialog) |
| `--probe-interval` | `10` | seconds between `#get_info` probes, minimum 0.5 |
| `--probe-timeout` | `1.5` | seconds to wait for a command reply |
| `--duration` | `0` | exit after N seconds (0 = run until `q`/Ctrl‑C) |

Examples:

```bash
./geolux_tui.py                        # TUI + logging in the current directory
./geolux_tui.py -p /dev/ttyUSB1        # another port
./geolux_tui.py --no-tui               # usable over ssh or piped to a file
./geolux_tui.py --no-probe             # strictly read-only
./geolux_tui.py --no-tui --duration 60 # record one minute, then exit
./geolux_tui.py -b 9600                # device whose RS-232 baud was changed
```

Exit status: `0` normal, `2` port could not be opened / pyserial missing.

---

## Files written

All three files share one `YYYY-MMDD-HHMMSS` stamp and are written to `--dir`.
For example a session started on 14 November 2026 at 14:11:57 produces:

| File | Content |
|---|---|
| `2026-1114-141157-radar.txt` | CSV, one row per measurement frame |
| `2026-1114-141157-radar-cmd.log` | CSV, one row per probe / command |
| `2026-1114-141157-radar-info.txt` | device settings snapshot |

### `<stamp>-radar.txt`

```csv
captured_at,epoch_seconds,actual_distance_m,average_distance_m,snr_db,temperature_c,raw_sentence
2026-11-14T14:11:57.639-05:00,1794673917.639,3.576,3.577,47.099,32,"$LVX,3.576,3.577,32,6.423,6.422,47.099,2.449*57"
```

* `captured_at` — local time with UTC offset, millisecond resolution.
* `epoch_seconds` — the same instant as a float Unix timestamp.
* `actual_distance_<unit>` — `L1`, the real/unfiltered distance.
* `average_distance_<unit>` — `L2`.
* `snr_db`, `temperature_c` — `S1`, `T1`.
* `raw_sentence` — the original NMEA line, for audit and debugging.

Rows are flushed immediately, so the file is complete if the program is
killed. Frames rejected by the checksum are **not** written (they are counted
in the TUI instead).

### `<stamp>-radar-cmd.log`

```csv
sent_at,command,ok,rtt_ms,reply_lines,error
2026-11-14T14:11:57.820-05:00,#get_info,1,48.1,36,
2026-11-14T14:12:06.240-05:00,#set_level_offset=0.05,1,34.9,1,
2026-11-14T14:12:31.991-05:00,#get_info,0,,0,no reply
```

Every probe and every parameter change, with round-trip time, number of reply
lines and the error text if it failed.

### `<stamp>-radar-info.txt`

The full `#get_info` reply in the device's own format, rewritten whenever a
setting changes:

```
# snapshot:2026-11-14T14:11:57-05:00
# device_type:997
# firmware:2.5.4
# serial_number:102181
# baud_rate:115200
# filter_type:4
# averaging_frame_number:20
# unit_type:2
# measurement_frequency:10.0
# level_offset:0.0
# deadzone_min:0.200
# deadzone_max:15.0
# sensor_height:10.000
...
```

Useful as a "known good" baseline before changing anything.

---

## TX probe and link diagnostics

Received NMEA only proves the **device → host** direction. The servicing
protocol answers requests on the same RS-232 port, so a reply to the
`#get_info` command proves the **host → device** path (our TX line, the
adapter and the device's RX).

`geolux_tui.py` sends `#get_info` at start-up and then every
`--probe-interval` seconds (default 10 s), and tallies the results:

* the reply is complete when a `$` sentence arrives after at least one `#`
  line, or when no `#` line has been seen for 250 ms (the device sends no
  block terminator);
* the tally (`ok / sent`, percentage and RTT) is shown on the main view and
  in the settings panel;
* after three consecutive failures the header shows `... / TX FAIL` and the
  reason replaces the log line;
* every attempt is written to `<stamp>-radar-cmd.log`, including the reason
  (`no reply`, `unparsable reply`, ...).

Reading the combination is the point:

| Observation | Meaning |
|---|---|
| NMEA flowing **and** probes answered | link fully working |
| NMEA flowing, probes silent | our TX line (or the device's RX) is broken |
| Nothing at all | cable, power, wrong baud rate, or the device is asleep (SDI‑12 mode) |

---

## Changing device parameters

Press `s` for a single **set parameter** dialog. It is one command with a
dropdown of parameters:

```
┌─ Set parameter ──────────────────────────────────────────────┐
│ parameter                 current                             │
│ > Level offset (m)        0.0                                 │
│   Averaging frames        20                                  │
│ notes for the selected parameter                              │
│  j/k or arrows: choose   Enter: edit   Esc: cancel            │
└───────────────────────────────────────────────────────────────┘
```

1. Pick a parameter with `j`/`k` or the arrow keys, `Enter` to edit.
2. Type the new value (`0-9 . - +`, backspace to correct), `Enter` to apply.
3. The dialog then shows the device ack and the **read-back**
   (`old -> new`), and reports `OK` or `FAILED` with a reason.

Details:

* Values are validated locally first (numeric, whole numbers where required,
  within the documented range) so an out-of-range value is never sent.
* The device answers `#set_<param>:OK` even for garbage input, so a change is
  only reported as successful when the following `#get_info` shows the new
  value (see [Firmware quirks](#firmware-quirks-and-gotchas)).
* Writes run in the link-monitor thread, one command at a time, so the UI
  never blocks and never races the serial reader.
* Every write is recorded in `<stamp>-radar-cmd.log`.

Currently exposed (add more by extending the `PARAMS` table in
`geolux_tui.py`):

| Parameter | Command | Range | Notes |
|---|---|---|---|
| Sensor height | `#set_sensor_height=<value>` | 0 … 100 m (0 … 100000 in a mm-configured unit) | height of the instrument above the gauge zero; sets the datum for `L3`/`L4` |
| Level offset | `#set_level_offset=<value>` | −1000 … 1000, in the device's unit | small correction; shifts the reported **distance**, so the level moves the other way |
| Averaging frames | `#set_frame_number=<n>` | 1 … 1000 | number of 10 Hz samples averaged (20 ≈ 2 s window) |

**Sensor height** is the datum: `level = sensor_height − distance`. Setting it
to the instrument's real height above the gauge zero is what makes `L3`/`L4`
meaningful. Raising it by 2.5 m raises every reported level by exactly 2.5 m
and leaves the distance readings untouched (verified on the test unit:
`12.500` → `L3` 6.446 → 8.946). Ranges in the dialog are converted to the
device's current unit, so the same entry limits work whether the instrument
reports mm, cm, m, in or ft.

**Sample/output rate:** `measurement_frequency` is 10 Hz on the tested unit
and **cannot be changed** — it is not in the manual's command list, and
`#set_measurement_frequency`, `#set_measurement_freq`, `#set_frequency`,
`#set_sample_rate`, `#set_output_rate` and `#set_measurement_rate` are all
silently ignored (value unchanged, stream unchanged). *Averaging frames* is
the supported rate-related knob.

---

## Protocol reference

### NMEA sentences (device → host, manual 7.1)

```
$LVX,L1,L2,T1,L3,L4,S1,S2*CSUM      firmware > 2.3.2
$LVX,L1,L2,T1,L3,L4,S1*CSUM         firmware <= 2.3.2
$ANG,A1,A2*CSUM                     firmware > 2.3.5 (tilt, hundredths of a degree)
```

`CSUM` is the XOR of every character between `$` and `*`, as two hex digits.
Every received sentence is validated; failures are counted and skipped, and
lines that are not `$`/`#` sentences at all are counted as *junk* (a sign of a
wrong baud rate or a noisy line).

The unit also emits an **undocumented `$WAV`** sentence (wave/echo analysis,
~10 Hz). It is ignored by both programs but appears in the raw line in the
footer.

### Servicing protocol (host → device, manual 7.2)

Commands are ASCII, terminated with `\r\n`, and answered on the same port:

```
#get_info                 ->  "# key:value" lines (36 fields on fw 2.5.4)
#set_<param>=<value>      ->  "#set_<param>:OK"   (or no reply if rejected)
#factory_reset            ->  needs a power cycle afterwards
```

`#get_info` reply (abridged, test unit):

```
# device_type:997
# firmware:2.5.4
# serial_number:102181
# baud_rate:115200
# filter_type:4
# averaging_frame_number:20
# unit_type:2
# measurement_frequency:10.0
# level_offset:0.0
# deadzone_min:0.200
# deadzone_max:15.0
# sensor_height:10.000
```

---

## Settings available on the device

Documented commands (manual 7.2). Only two are exposed in the TUI so far; the
rest can be sent manually once a raw-command prompt exists, or with the
Geolux Instrument Configurator.

| Command | Values | Risk |
|---|---|---|
| `#set_deadzone_min=<v>` / `#set_deadzone_max=<v>` | in the current unit | **useful**: limits the active zone; the default `15.0` allows phantom 15 m / SNR 0 readings |
| `#set_filter_type=0..4` | 0 none, 1 IIR, 2 moving average, 3 median, 4 std-dev | safe |
| `#set_frame_number=<1..1000>` | averaging length | safe (exposed) |
| `#set_IR_constant=<0..1>` | IIR constant | safe |
| `#set_peak_detector=0..2` | max / last / first peak | safe |
| `#set_nmea_protocol_flags=0\|2` | 0 all sentences, 2 no `$ANG` | safe |
| `#set_sensor_height=<v>`, `#set_staff_gauge=<v>` | gauge-zero datum | safe (height exposed in the TUI) |
| `#force_calibration=0\|1` | recalibrate every second | safe, +20 % power |
| `#set_power_save=0\|1` | 0 operating, 1 standby | safe |
| `#set_unit=0..4` | mm, cm, m, in, ft | **caution**: also changes the units of deadzone, offset, sensor height and analog scaling |
| `#set_level_offset=<v>` | in the current unit | **caution**: manual says "should not normally be changed" (exposed) |
| `#set_analog_min/max`, `#set_modbus_*`, `#set_sdi_id` | — | RS-485 / 4–20 mA only |
| `#set_baud_rate=9600…115200` | RS-232 baud | **dangerous**: port must be reopened at the new rate |
| `#set_sdi_sleep=0` | SDI-12 auto sleep | **dangerous**: RS-232 goes dead until power-cycled and reconfigured within 60 s |
| `#factory_reset` | — | **dangerous**: needs a power cycle, erases settings |

---

## plot_radar.py — plotting

```bash
./plot_radar.py                              # newest *-radar.txt in the cwd
./plot_radar.py 2026-1114-122308-radar.txt   # a specific log
./plot_radar.py -o plot.html --no-open       # choose the output, no browser
./plot_radar.py --inline                     # self-contained HTML (offline)
./plot_radar.py --live --port 5006           # auto-refreshing Bokeh server
```

| Option | Default | Meaning |
|---|---|---|
| `-h`, `--help`, `-?` | | extensive help and exit |
| `FILE` | newest `*-radar.txt` | log file to plot |
| `-o`, `--output` | `<input>-plot.html` | output HTML (static mode) |
| `--live` | off | serve an auto-refreshing Bokeh app |
| `--port` | `5006` | port for `--live` |
| `--interval` | `1.0` | live refresh interval (s) |
| `--no-open` | off | do not open a browser |
| `--inline` | off | embed BokehJS for offline use (~1.4 MB HTML) |
| `--max-points` | `100000` | decimate long logs / cap live points (0 = keep all) |

Panels (x axis linked, one shared toolbar):

1. **Distance** — actual `L1` (solid) and average `L2` (dashed); click a
   legend entry to hide it. Markers keep single samples visible.
2. **SNR** — dB, with a dashed 10 dB low-echo reference line.
3. **Temperature** — instrument °C.

A vertical-line hover tool on each panel shows time, distance, SNR and
temperature; a banner above the plots summarises samples, duration,
min/median/max distance, median SNR and average temperature.

Live mode tails the file and streams only complete lines (never a half-written
row), so it can run while `geolux_tui.py` is still appending. The unit is read
from the CSV header, so `m`, `ft`, `mm`, `cm` and `in` all work, and empty
cells are plotted as gaps.

Exit status: `0` success, `2` no log file found.

---

## Firmware quirks and gotchas

Verified on firmware **2.5.4** (serial 102181):

1. **Read-back formatting is misleading.** Thousandths-scaled values are
   printed as `"%d.%d"` of integer thousandths, so the fraction is not zero
   padded and negatives look mangled:

   | set | read-back | actual |
   |---|---|---|
   | `0.05` | `0.50` | 0.050 |
   | `0.005` | `0.5` | 0.005 |
   | `1.05` | `1.49` | 1.049 (1000×1.05 truncated) |
   | `-0.25` | `0.-250` | −0.250 |
   | `0.25` | `0.250` | 0.250 |

   The values **are** applied correctly — measured distance shifts of
   +0.050, +0.010 and −0.250 confirmed it — so `geolux_tui.py` decodes this
   format before comparing, and shows both, e.g.
   `read-back : 0.000 [raw 0.0] -> 0.050 [raw 0.50]`. `sensor_height` happens
   to be printed zero padded (`12.500`, `9.050`), but three fractional digits
   decode identically either way, so the same rule covers it.

2. **`#set_*` answers `OK` even for malformed input.** Sending
   `#set_level_offset=abc` returns `#set_level_offset:OK`. This is why every
   write is verified with a read-back instead of trusting the ack.

3. **Out-of-range values get no ack at all** (`#set_frame_number=0` and
   `=5000` are silently ignored) — the TUI validates ranges before sending.

4. **`#set_level_offset` shifts the reported distance**, not the level:
   `L1` moves by the offset and `level = sensor_height − L1`, so the level
   moves the opposite way. The manual calls it a "level offset"; the observed
   behaviour is a distance offset.

5. **The output rate is fixed at 10 Hz** (`measurement_frequency`) — no
   supported command changes it (see
   [Changing device parameters](#changing-device-parameters)).

6. **`$WAV` is undocumented** and ignored, along with any unknown sentence.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `cannot open /dev/ttyUSB0` | cable/adapter not present; check `ls -l /dev/ttyUSB0`, add yourself to `dialout` |
| Header shows `NO DATA` but the TX probe is `ok` | wrong baud rate, `nmea_protocol_flags=2` (no `$ANG`), or the device is in SDI‑12 sleep mode — power-cycle it and reconnect within 60 s |
| Nothing at all, probe silent | wrong port, device unpowered, or SDI‑12 sleep mode |
| `TX FAIL` with NMEA still flowing | the device is not answering requests: host TX line, adapter or device RX |
| High `csum` error count | marginal RS-232 link, wrong baud rate, or a noisy adapter; check the raw line at the bottom for corruption |
| High `junk` count | framing errors — almost always a baud-rate mismatch |
| Values stuck at ~`15.00 m` with SNR 0 | no echo; the device clamps to `level_range`. Narrow `deadzone_max` to the real range |
| Set dialog says `FAILED - device accepted the command but the value did not change` | the device ignored the value (e.g. out of range on its side); check the ranges |
| TUI looks cramped | it adapts down to ~52×20; below that it shows compact cards and may omit cards |
| WSL2: port vanished after replug/sleep | re-run `usbipd attach --wsl --busid <id>` (see [WSL2 issues](#wsl2-potential-issues-and-fixes)) |
| WSL2: `Permission denied` on `/dev/ttyUSB0` | `sudo usermod -aG dialout $USER`, then `wsl --shutdown` and reopen |
| WSL2: anything else | see [WSL2 potential issues and fixes](#wsl2-potential-issues-and-fixes) |

---

## Design notes and extending

`geolux_tui.py` runs three things:

* **reader thread** — `readline()`, strips `\r`, validates checksums, updates
  shared state, writes CSV rows, and feeds `#` lines to the link monitor;
* **link monitor thread** — owns all *writes* to the port: periodic
  `#get_info` probes and queued `#set_*` jobs (one exchange at a time), with
  reply assembly, tally, read-back verification and command logging;
* **UI thread** — `curses`, redraws at 10 fps from a locked snapshot, so it
  never blocks on serial I/O.

Adding a settable parameter takes one entry in the `PARAMS` table at the top
of `geolux_tui.py`:

```python
{
    "key": "deadzone_max",         # command becomes #set_deadzone_max=<v>
    "label": "Active zone max",    # shown in the dropdown
    "info": "deadzone_max",        # #get_info field used for read-back
    "lo_m": 0.0, "hi_m": 100.0,    # range in metres (converted to the unit in
                                   # use); use "lo"/"hi" for unitless values
    "is_int": False, "unit": True, # integer only? append unit?
    "note": "ignores echoes beyond this distance",
},
```

## Limitations

* POSIX only (`curses`, `termios`); on Windows use WSL2 (see above).
* Installed with mamba/conda-forge; pip is not a supported install method.
* Only two parameters are editable from the TUI so far; the rest of the
  documented settings need a raw-command prompt or the vendor application.
* `#set_baud_rate`, `#set_sdi_sleep` and `#factory_reset` are deliberately
  **not** exposed, because a mistake can strand the link.
* The measurement/output rate cannot be changed (firmware).
* `plot_radar.py --live` relies on a Bokeh server; the static HTML mode is
  enough for reviewing a finished capture.
