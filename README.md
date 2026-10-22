# FalconSDR

<img src="packaging/icons/falconsdr.png" width="96" align="right" alt="">

A wideband scanner for an RTL-SDR (or any SoapySDR receiver) that records
**only the transmissions it actually stops on** - aircraft band ATC out of the
box, but any band works (fire brigade, military UHF, repeaters…).

- Scans up to ~2.4 MHz at once, so several conversations on different
  frequencies are recorded at the same time.
- Live listening, a spectrum and a frequency display in the browser - on the
  computer itself or, in server mode, from any phone or PC on your network.
- A recordings library with quality scores, search, favorites, statistics and
  (optional) Whisper transcription that can run on another, faster computer.
- Memory List of named channels, per-band settings, a blacklist.
- EU airband 8.33 kHz channels are shown the way they are published
  (126.805, not 126.800).

## Install

**Ubuntu/Debian (.deb, nothing else needed):**
`sudo apt install ./falconsdr_<version>_amd64.deb`, then start *FalconSDR*
from the menu. Build it yourself with `packaging/linux-deb/build.sh`.
Unplug and replug the dongle once after installing (the package adds a udev
rule and keeps the DVB-T TV driver from grabbing it).

**From source (Linux/Windows/macOS):**

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python main.py            # opens http://127.0.0.1:8765
.venv/bin/python main.py --server   # reachable from the whole network
```

A headless receiver (e.g. a small Linux box with the dongle) is set up with
`packaging/remote-server/install.sh` - see its README.

## Getting good recordings

- **Trigger level** decides what counts as a transmission. Lower = weaker
  stations get recorded, but more noise too.
- **Some frequencies are just a constant carrier or interference** (birdies
  from your own computer, a nearby transmitter's harmonics, a dead carrier).
  They trip the trigger all the time and keep you from lowering it. Add them
  to the **blacklist** (Listen → *Skip + blacklist*, or Channels → Blacklist).
  After that you can usually lower the trigger level by several dB and pick
  up the weak stations you were missing.
- **Gain** defaults to *Auto (software)*: the tuner's own hardware AGC tends
  to overload on a strong nearby station.
- **Hold after a transmission** (5 s by default) keeps a recording open
  through the pause between a call and its answer.
- If the Activity feed says *the computer can't keep up with the SDR*, data
  is being lost (short gaps in audio and recordings). A virtual machine with
  USB passed through is a common cause - run it on the host instead.

## Icon

The one source is [`falconsdr/web/static/icon.svg`](falconsdr/web/static/icon.svg).
`packaging/icons/make-icons.sh` renders `falconsdr.png` and the Windows
`falconsdr.ico` from it.

## License

GPL-3.0-or-later (see `LICENSE`) - required by `pyrtlsdr` and the bundled
`librtlsdr`.
