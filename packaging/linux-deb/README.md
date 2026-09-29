# SmartScanATC — self-contained .deb (test build)

## What this is

A "fat" Debian package: [PyInstaller](https://pyinstaller.org/) freezes the
app together with its own Python interpreter and every pip-installable
dependency (PySide6, numpy, scipy, pyqtgraph, pyrtlsdrlib, sounddevice) into
one self-contained folder at `/opt/smartscanatc`. A user installs it with
`sudo apt install ./smartscanatc_*.deb` and gets a working scanner with
**zero** extra `pip install`/system-package steps for RTL-SDR + live audio.

This trades size (~200-400 MB installed) for reliability: it doesn't depend
on whatever PySide6/numpy version happens to already be on the user's
system, which is exactly the "works on my machine, not on theirs" gap a
"thin" package (one that just declares `python3-pyside6` etc. as `apt`
dependencies) is exposed to across different Ubuntu/Debian releases.

## Known limitations of this build (by design, not bugs)

- **RTL-SDR only.** SoapySDR-based hardware (HackRF, LimeSDR, Airspy,
  SDRplay, USRP/UHD) isn't available — SoapySDR is a system C library with
  its own plugin/driver-module architecture that doesn't freeze into a
  single bundle the way a normal pip package does. Someone with that
  hardware needs the regular `pip install -r requirements.txt` + system
  `python3-soapysdr` install instead (see the main README).
- **No in-app transcription install.** The "Install transcription" button
  in Options needs a real, writable Python/pip to install into — a frozen
  build doesn't have one. It now fails with a clear message instead of
  doing something confusing; see `main.py`'s and
  `transcription_install.py`'s own `sys.frozen` guards.
- **MP3 output** still works, but only if the user separately has a system
  `ffmpeg` on `PATH` — this build can't bundle a system binary invoked via
  `subprocess`, only Python-importable dependencies.

None of this is a defect in the .deb itself — it's the honest tradeoff of
"self-contained enough that RTL-SDR + audio need nothing extra" vs.
"everything under the sun works out of the box", and matches how the app
already degrades gracefully when these are missing (see AGENTS.md).

## Building it

```
cd packaging/linux-deb
./build.sh
```

Builds in a **throwaway venv** (`packaging/linux-deb/build-venv/`, gitignored)
— never the project's own `.venv` — specifically so nothing from your dev
setup can leak into the frozen bundle by accident. Output:
`packaging/linux-deb/out/smartscanatc_<date>_<arch>.deb`.

## Testing it without fooling yourself

The whole point of a "fat" package is that it works on a machine that has
**none** of your dev environment. Installing it on this same machine and
running it from this same terminal is *not* a real test of that — your
shell may still have the project's `.venv` activated, `PYTHONPATH` pointing
at it, or you might launch it while `cd`'d into the repo (where Python would
happily import the *source* `smartscan/` package instead of the installed
one, silently passing a test that would fail for anyone else). Concretely:

1. **Install it:**
   ```
   sudo apt install ./packaging/linux-deb/out/smartscanatc_*.deb
   ```

2. **Make a throwaway test user** — a real second account on this same
   machine that has never touched this project, so there's no dev-venv,
   `PYTHONPATH`, or repo checkout it could accidentally fall back on:
   ```
   sudo useradd -m -s /bin/bash smartscan-test
   sudo passwd smartscan-test          # set a temporary password
   ```
   Switch to it with a full login shell (not plain `su`, which can still
   inherit some of your environment):
   ```
   sudo machinectl shell smartscan-test@   # or: su - smartscan-test
   ```
   This account still runs on your real hardware (RTL-SDR, sound card), so
   real scanning/audio testing still works — it just guarantees nothing
   from *your* environment is silently propping the app up.

3. **From that account**, confirm you're actually running the installed
   copy, not the dev one, before doing anything else:
   ```
   which smartscanatc        # must print /usr/bin/smartscanatc
   cd /                      # anywhere outside the repo - rules out
                              # accidentally importing source smartscan/
                              # via a cwd-relative sys.path
   smartscanatc
   ```
   Launch it from the applications menu too (GNOME/KDE app grid) if you
   want to check the `.desktop`/icon integration, not just the CLI path.

4. **Exercise the real things a first-time user would do**: pick the real
   RTL-SDR device (not Simulation — that needs the new Debug mode checkbox
   in Options anyway, off by default), start a scan, confirm live audio,
   record something, open it in Playback, check the waveform/quality score
   render correctly, try MP3 output both with and without `ffmpeg`
   installed on that account, and click "Install transcription" to confirm
   it now fails with the clear message above instead of doing something
   strange.

5. **Uninstall when done testing:**
   ```
   sudo apt remove smartscanatc
   sudo userdel -r smartscan-test
   ```

If you'd rather not create a second user account, the next-best (weaker)
check is at least: `deactivate` any active venv, `unset PYTHONPATH`, `cd /`,
and run `smartscanatc` — this catches the single most common false-positive
(accidentally still running against the dev `.venv`/source tree) but won't
catch "some system package your dev machine happens to already have
installed that a fresh Ubuntu install wouldn't."

## What a clean-room import/startup check already found

Before handing this to you, a bare Ubuntu 24.04 Docker container (`apt
install ./the.deb` with no other packages preinstalled, no GUI, no dev
tools) was used to catch missing shared-library dependencies early — see
the build/test log for what that verified. It cannot exercise real
GUI/audio/USB hardware paths (no display, no sound card, no dongle in a
container), which is exactly why step 2 above still matters on real
hardware.
