# Changelog

All notable changes to this project.  The version lives in `version.py` and
is shown by `--version`, in the TUI header and help overlay, and in every
plot.  Tags are named `vX.Y.Z`.

## 1.4.0 - 2026-11-15

* **New-file command** (`n`): closes the current recording and starts a fresh
  `<stamp>-radar.txt.gz` + `-radar-cmd.log.gz` + `-radar-info.txt.gz` set
  without leaving the TUI.  The prompt is preloaded with the current time in
  the same `YYYY-MMDD-HHMMSS` scheme, `Ctrl-U` clears it, `Esc` cancels, and
  existing names are rejected.  Rotation is serialised with the reader and
  probe threads, so no rows are lost and the old gzip files are closed
  cleanly.

## 1.3.0 - 2026-11-15

* **Reboot command** (`b`): sends the undocumented `#reset`, which restarts
  the unit, keeps every setting and has no reply.  The TUI waits for the unit
  to come back (~3.5 s to answer, ~4.3 s NMEA gap), compares `#get_info`
  before/after and reports whether settings changed.  Verified on hardware.
* **"Are you sure?" popup** for both factory reset (`r`) and reboot (`b`),
  replacing the typed `RESET` confirmation: `y` sends, `n`/`Esc` cancels.
* Factory reset result shows the `#factory_reset:OK` reply (the undocumented
  `#set_raw=<hex>` dump is left in the probe log).
* Pre-reset settings snapshot committed for reference.

## 1.2.1 - 2026-11-15

* Hardware-tested `#factory_reset` on firmware 2.5.4: the link stays up (no
  power cycle needed), the unit returns in mm with `sensor_height = 0`, and
  the reply includes a `#set_raw=<hex>` settings dump.  Docs and dialog
  corrected accordingly (firmware quirk 8).

## 1.2.0 - 2026-11-15

* Factory reset command (`r`) with confirmation and probe-log entry.

## 1.1.0 - 2026-11-15

* Repository URL shown at the top of the TUI and of every plot;
  `REPO_URL` added to `version.py`.

## 1.0.1 - 2026-11-15

* Fix `_csv.Error: line contains NUL` when a device sentence contains a NUL
  byte that passed the device checksum: the plotter now substitutes U+FFFD
  instead of aborting.

## 1.0.0 - 2026-11-15

* First tagged release: `version.py`, `--version` in both programs, version
  in the TUI header and plot titles/banner.
* `plot_radar.py`: accepts a directory and lists its logs for selection;
  gzip logs are read transparently; independent legend toggles for lines and
  points; mouse-wheel zoom active by default; larger, darker markers.
* `geolux_tui.py`: `?` help overlay; set dialog for sensor height, level
  offset, dead zone min/max, measurement frequency and averaging frames;
  gzip-compressed logs (`-radar.txt.gz`, `-radar-cmd.log.gz`,
  `-radar-info.txt.gz`); settings panel and probe log as before.
