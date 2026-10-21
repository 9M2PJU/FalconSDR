# AGENTS.md — SmartScanATC

Guidance for AI coding agents working in this repository.

## What this is

SmartScanATC (`smartscanatc/`) is a PySide6 desktop app that scans a radio band
(default: airband ATC 118–136 MHz) via SoapySDR (Linux) or a native librtlsdr
backend (Windows/macOS — gotcha 16) and records audio **only** for
transmissions the scanner actually stops on — never the retuning noise.
`smartscan/scanner_engine.py`'s state machine is the authoritative spec.

## Running it

```
cd smartscanatc
python3 main.py
```

`PySide6`, `numpy`, `scipy`, `pyqtgraph`, `pyrtlsdr`/`pyrtlsdrlib`,
`sounddevice` — `pip install -r requirements.txt` covers Windows/macOS fully.
`SoapySDR`/`PyAudio` need system packages on Linux (see `requirements.txt`),
optional-but-preferred elsewhere. No SDR hardware/library at all, with
`AppConfig.debug_mode` on → `SimulatedBackend` fallback rather than failing
to start; with debug off (the default), a real connection failure stays
visible (retry/backoff state) instead of silently simulating — gotcha 8.

**License: GPL-3.0-or-later** (`LICENSE`) — forced by `pyrtlsdr` (GPL-3.0-or-later)
and the bundled `librtlsdr` (GPL-2.0-or-later); every other dependency is
permissive or LGPL. A new dependency must be GPLv3-compatible.

PySide6, not PyQt6 — `Signal`/`Slot`, not `pyqtSignal`. Set
`PYQTGRAPH_QT_LIB=PySide6` if both bindings are ever installed side by side.

**i18n**: every user string goes through `tr()` (`smartscan/i18n.py`);
translations are `.po` files under `smartscan/locale/`, parsed by a small
hand-rolled reader (no `gettext`/`.mo` build step — see
`smartscan/locale/README.md` to add a language). `AppConfig.language`
resolves once at startup via `QLocale.system()`. **Never build a
module-level constant from `tr()` calls** — imports run before
`i18n.set_language()`, so it freezes to English forever; wrap it in a
function called at point of use instead (`military_presets.py`'s labels hit
the same trap from the other direction — plain English, translated only at
the `scanner_panel.py` call site).

## Architecture

- `smartscan/models.py` — `ScannerState`, `Channel`, `BlacklistEntry`.
- `smartscan/config.py` — `AppConfig`, persisted to
  `~/.config/SmartScanATC/config.json`. `device.sample_rate_hz` is the
  wideband **capture bandwidth**; `memory_channel_bandwidth_hz` is the
  per-channel power-window width for Memory List channels (a band uses its
  own `step_hz` instead).
- `smartscan/storage.py` — CSV read/write for `channels.csv`/`blacklist.csv`.
- `smartscan/channelizer.py` — `plan_windows()` groups channel frequencies
  into the fewest capture windows; `channel_powers_dbfs()`/
  `channel_powers_from_spectrum()` do one FFT per block and sum power per
  channel's bins; `spectrum_snapshot()` feeds the GUI spectrum;
  `extend_boundary_window()` probes just past a *swept band's* edge (gotcha
  31/32 — never a single point).
- `smartscan/dsp.py` — `Demodulator` (AM/NFM/WFM → audio, gotcha 7/25) and
  `NoiseReducer` (spectral-gate, minimum-statistics noise floor).
- `smartscan/sdr_backend.py` — `SoapyBackend`, `RtlSdrBackend` (gotcha 16),
  `SimulatedBackend`, `AcquisitionThread` (gotcha 11 — this is what
  `ScannerEngine._backend` actually is). `open_real_backend()`/
  `create_backend()` pick/fall back (gotcha 8/9/10).
- `smartscan/audio_output.py` — `AudioOutput`: live speaker via PyAudio or
  `sounddevice`, own ring buffer + drift correction + RT-priority callback
  (gotcha 26).
- `smartscan/agc.py` — `SoftwareAgc`, software-driven RF gain control
  (gotcha 27).
- `smartscan/recorder.py` — WAV writer only (gotcha 14: mp3 is a post-close
  transcode, not a `Recorder` concern). `single_file` flag: gotcha 13/23.
- `smartscan/scanner_engine.py` — `ScannerEngine(threading.Thread)`:
  `CALCULATE_NEXT_FREQ → TUNING → SCANNING → SIGNAL_DETECTED ⇄ LINGER`.
  Retunes to a whole capture window, not a single channel; `SCANNING` picks
  the strongest non-blacklisted candidate per tick, `_service_active_slots()`
  advances every already-open slot (gotcha 12/28). Talks to the GUI via
  `ScannerSignals(QObject)`.
- `smartscan/gui/` — `main_window.py` wires `panels/scanner_panel.py`,
  `panels/frequency_manager.py`, `panels/recording_panel.py`, plus
  `widgets.py` (`LevelMeter`, `RecIndicator`, `SliderSpin`, `SpectrumView` —
  gotcha 30) and `theme.py`.

## Known gotchas (hit these once already — don't reintroduce)

1. **Signal-loading guard.** A GUI panel that both loads `AppConfig` into
   widgets on construction and wires those widgets' change signals to a
   persist-to-config slot must set `self._loading = True` while populating,
   `False` after (see `scanner_panel.py`/`recording_panel.py`) — otherwise
   mid-load `setValue()` calls fire with sibling widgets still at
   constructor defaults and write half-initialized state to disk.
2. **Never use `QComboBox.findData()` with tuple/object payloads** —
   unreliable. Do a manual linear scan over `itemData(i)`.
3. **`SimulatedBackend.read()` must sleep for `num_samples/sample_rate`** or
   the engine spins the SCANNING dwell thousands of times/sec. Burst timing
   is a deterministic function of wall-clock time + a frequency hash, not
   persisted state.
4. **`SoapyBackend.enumerate_devices()` must filter out `driver == "audio"`**
   — `soapysdr-module-audio` enumerates sound cards as SDR receivers.
5. **ATC airband is AM, not FM** — `Modulation.AM` is the correct default
   for 118–136 MHz.
6. **Squelch (`config.squelch_enabled`/`squelch_offset_db`) must always be an
   offset from `trigger_level_dbfs` (clamped ≤ 0), never an independent
   threshold** — `_effective_squelch_dbfs()` = trigger + offset, so it can
   never fire during SIGNAL_DETECTED, only in the LINGER tail. `trigger_level_dbfs`
   alone still drives SIGNAL_DETECTED/LINGER. A squelched block writes
   *silence* to the recording (`Recorder.write_audio(audio, real=False)`,
   held in `_pending_silence`) rather than being skipped — flushed if real
   audio follows (genuine mid-transmission pause), discarded on close if not
   (trailing tail trimmed). `analyze_wav()` excludes exact-zero-RMS windows
   from its noise-floor/SNR calc for this reason. Quality-score caches
   (`QualityCache`/`TranscriptionCache`, keyed by path+size+mtime) don't
   auto-invalidate when scoring logic changes — invalidate them by hand.
7. **In `Demodulator.demod()`, mix-to-baseband → decimate → AM/FM-demod, in
   that order** — `resample_poly`'s anti-alias filter is what rejects
   neighbouring channels in a wideband capture.
8. **`SoapyBackend.read()` must raise on a fully empty read** (a dead/
   unplugged device just returns nothing, never raises on its own), **and
   `create_backend()` only ever auto-falls-back to `SimulatedBackend` for
   `driver in ("", "auto")` AND only when `debug_mode` is on** (its new
   `debug_mode` param) — a real open failure with debug off propagates
   instead (the existing `_open_backend_with_retry()` reconnect/backoff
   loop then shows it as a visible "Failed to connect... retrying" state).
   Real, reported case: a real device that merely failed to open (USB
   permission, already in use, DVB-T driver grabbed it first) looked
   identical to "no hardware at all" and silently simulated instead -
   indistinguishable from a genuinely working scan. An explicitly-picked
   real driver that fails must always propagate the error regardless of
   debug_mode, never quietly simulate.
9. **`SimulatedBackend` must inject its fake signal at its offset from the
   *currently tuned* frequency** (`abs(active_freq - freq_hz) <= half_bw`),
   not only on an exact match — the tuner sits on a window center, nudged
   off exact channel frequencies.
10. **Auto-reconnect is deliberate.** `ScannerEngine.run()` retries a backend
    `RuntimeError` via `_open_backend_with_retry()` (backoff 2/5/10/20/30s,
    `RECONNECTING` state) instead of stopping. Once real hardware has been
    proven present (`_had_real_hardware`), reconnects call the real backend
    directly, bypassing `create_backend()`'s auto-simulate fallback (gotcha
    8). Any open recording is closed via `_end_transmission()` first.
11. **`ScannerEngine._backend` is an `AcquisitionThread`, not the raw
    backend — never call `backend.read()` from the main loop thread.** It
    reads back-to-back on its own SCHED_FIFO thread into a bounded FIFO
    queue (`max_queued`) so slow consumer work can never delay the actual
    hardware read (which otherwise silently drops/splices samples at the
    driver level). **Single-owner**: the only thread that ever touches the
    real backend, including `set_frequency()`/`set_gain()` — those hand a
    command via `_pending_command`/`_cond` instead of touching the device
    from the caller's thread. Never share a lock between this thread and
    any other — a lock shared with this SCHED_FIFO thread is a real-time
    priority-inversion hazard (measured: 11.7s starvation of a normal-
    priority caller).
12. **`_service_active_slots()` demodulates every active slot via
    `self._slot_pool` (`ThreadPoolExecutor`)** — sequential per-slot demod/
    noise-reduction/write blows the per-tick budget with several
    simultaneous slots, backing up `AcquisitionThread`'s queue. Each
    `_Slot` owns its own `Demodulator`/`NoiseReducer`/`Recorder` — no shared
    mutable state between pooled tasks. LINGER/slot-close bookkeeping
    happens on the main thread after every future resolves, never inside
    the pooled task (`_close_slot()` mutates `self._slots`).
13. **Manual "Record" is a second, independent, continuous `Recorder`**
    (`self._manual_recorder`, `single_file=True`), unrelated to the trigger
    state machine and to `config.recording.enabled` (auto-recording). Fed
    real audio only when the primary slot is active
    (`slot.freq_hz == self._primary_freq`) and literal zeros on idle ticks —
    continuous including silence. Never dropped as a "phantom" (always
    `write_audio(real=True)`). Never persisted — off again on next launch.
    A recording that captures a real frequency gets renamed from
    `..._session` to a frequency-tagged name
    (`Recorder.rename_session_file_with_frequency()`, first-wins latch in
    `_manual_recording_first_freq`).
14. **MP3 (`RecordingConfig.output_format == "mp3"`) is a transcode run
    strictly after a WAV is closed AND its metadata chunk written**
    (`append_metadata_chunk()` first, transcode second — an mp3 container
    can't hold the RIFF chunk). `Recorder` is WAV-only and knows nothing
    about `output_format`; both close sites (`_close_slot`,
    `stop_manual_recording`) hand the file to `_finish_recording()`, which
    runs ffmpeg on `self._slot_pool` — never on the scan-loop thread (a slow
    transcode outlasted `AcquisitionThread`'s ~1.9s queue and dropped IQ for
    other open slots) — and emits `recording_finished` only afterwards.
    Missing `ffmpeg` silently keeps the WAV.
15. **Raw IQ recording (`iq_recorder.py`) is a third, independent
    mechanism** — not audio, not `Recorder`-based. Writes SigMF from the
    *shared* window capture (never a second hardware read). Manual/
    momentary only (uncompressed IQ ≈ 19 MB/s at 2.4 MSPS). Doesn't go
    through `recording_finished` (that feeds the Playback library, which
    expects WAV/MP3) — separate `raw_iq_recording_changed` signal, files in
    a `raw_iq/` subfolder, excluded from the audio library.
16. **Windows/macOS get real RTL-SDR via `RtlSdrBackend`** (native
    librtlsdr/`pyrtlsdrlib` — SoapySDR has no pip wheel there). Matches
    `SoapyBackend`'s contract: bias-tee off, raises instead of short-reads,
    same CF32 scaling `(i - 127.4) / 128` (so a threshold means the same
    dBFS cross-platform). `open_real_backend()` picks Soapy vs native based
    on driver string/what's installed; `"auto"` tries Soapy then native,
    only simulates if both fail. **`SoapyBackend.__init__` must turn `driver
    in ("", "auto")` into an explicit `{"driver": "rtlsdr"}` args filter,
    never pass `{}` to `SoapySDR.Device()`** — a real, measured bug, not just
    the theoretical "could match the `audio` driver" risk (gotcha 4) it was
    first flagged as: on a real dev machine, opening the *same* physical
    RTL-SDR dongle via unfiltered `{}` vs explicit `{"driver": "rtlsdr"}`
    produced **~10dB different channel power** for the identical gain_mode
    ("auto"/hardware AGC) - one path was overdriving (peak +1dBFS), the
    other wasn't (peak -9dBFS). Confirmed by direct A/B measurement, fixed
    by always filtering explicitly - `driver="auto"` only ever means "let
    the app pick an RTL-SDR, don't ask the user" here anyway (gotcha 16/35:
    this app's device list is RTL-SDR-first), so there's no real ambiguity
    being preserved by leaving the filter open.
17. **Other SoapySDR hardware (HackRF/LimeSDR/Airspy/SDRplay/USRP) already
    works via `SoapyBackend`** with no code changes once the matching
    `soapysdr-module-*` is installed — nothing in it is RTL-SDR-specific.
    `bandwidth_spin`'s range is `(0.25, 61.44)` MHz (not a hardware limit,
    just a generous bound — raise it if a real device needs more). Windows/
    macOS stay RTL-SDR-only for the zero-install path; conda-forge SoapySDR
    is a documented, unautomated path for other hardware there.
18. **Transcription (faster-whisper) is an in-app-installable optional
    extra**, not in `requirements.txt`. `is_transcription_installed()`
    (`transcription_availability.py` — `importlib.invalidate_caches()` then
    `find_spec`) is the one source of truth; don't grow a second ad-hoc
    `import faster_whisper` try/except elsewhere.
    `TranscriptionInstallWorker` tries a plain `pip install` into the
    running interpreter first; only falls back to a `--system-site-packages`
    venv (`venv_paths.VENV_DIR`) on a PEP-668 `externally-managed-environment`
    error — that fallback is the only path needing a restart.
    `PlaybackPanel` gates on `is_transcription_installed()` before queuing
    any transcription work, or a missing install retries forever
    (`TranscriptionWorker` treats `"error"` status as never durable).
19. **A frequency-less filename (`..._session.wav` — manual recording,
    gotcha 13) must be recognized by `FILENAME_REGEX`/`parse_filename()`**
    (`playback/config.py`/`organizer.py`), mapped to `models.MANUAL_FREQ_KHZ`
    (0) rather than `None` — otherwise `organize()`/`scan_recordings()`
    silently skip the file forever (a real, playable recording invisible in
    Playback). Every freq-label display site needs its own
    `MANUAL_FREQ_KHZ` case ("Manual recording", not "0.000 MHz").
    `FILENAME_REGEX` is the single source of truth for every consumer.
    MP3 files need the same regex to also match `\.mp3$`, and the stdlib
    `wave` module can't decode mp3 at all — `audio_analysis.py` decodes via
    ffmpeg to an in-memory WAV first (`_mp3_to_wav_bytes()`). Duration must
    come from actual bytes read, not the WAV header's declared frame count —
    ffmpeg's non-seekable-pipe muxer writes a sentinel `0x7FFFFFFF` there.
20. **Legal disclaimers (`gui/legal_disclaimer.py`) are tracked per UI
    language** (`AppConfig.legal_disclaimer`'s two seen-language lists), not
    once-ever — a language switch means seeing them again, by design.
    `maybe_show_launch_disclaimer()` is informational, once per language at
    startup. `confirm_recording_disclaimer()` is a real gate (Continue/
    Cancel) shared by *every* recording-starting action (auto-recording
    both tabs, manual Record, raw IQ) — a decline must revert the
    triggering toggle/button and have zero side effects (return before
    enabling anything).
21. **Band presets (`military_presets.py`) are JSON, not a hardcoded list**:
    bundled `smartscan/presets/band_presets.json` + user's
    `CONFIG_DIR/band_presets.json` (edits/additions/hidden-builtin-ids),
    merged live via `get_all_presets()` — **never cache this** (same class
    of bug as the i18n module-constant trap). Editing a built-in shadows it
    (same id saved into the user file); deleting a pure built-in adds it to
    `hidden_builtin_ids`; the shipped JSON is never modified — the two cases
    take different code paths since only a shadowed one has a real user-list
    entry to remove. Import never overwrites on id collision (generates a
    new id).
22. **"Reset to defaults" must reset in-memory config/channels/blacklist/
    presets BEFORE calling `_restart_app()`** — `_shutdown()` (which restart
    calls first) persists whatever's currently in memory, so resetting
    after would get overwritten right back. Never touches the recordings
    directory itself.
23. **"Single File" auto-recording mode was removed, not fixed** — the
    per-slot fresh-`Recorder`-per-transmission design (gotcha 12) meant it
    never produced one continuous file. `Recorder` has a real `single_file:
    bool` constructor flag now, used only by manual recording (gotcha 13),
    the one legitimate "one continuous file" case. `AppConfig.load()` pops
    the stale `mode`/`insert_marker` keys for backward compat.
24. **`RecordingConfig.max_concurrent_recordings`** (default 3, 0 = unlimited)
    caps simultaneously-recorded transmissions — checked in
    `ScannerEngine._at_recording_capacity()`, gating `_open_slot()`. Only
    gates auto-recording (`recording.enabled` must be `True`); never caps
    live scanning/listening, and doesn't affect manual/raw-IQ recording
    (separate mechanisms outside `self._slots`). A capped candidate isn't
    lost — it's simply re-evaluated next tick, opening automatically once a
    LINGER expiry frees capacity.
25. **`Demodulator`'s output-level AGC (`dsp.py`, `_apply_agc`) is a
    persistent attack-fast/release-slow envelope follower across `demod()`
    calls** (state in `self._agc_envelope`, cleared by `reset()`) — never a
    per-chunk peak normalization computed from that chunk alone. The old
    per-chunk version dragged noise-only chunks up to the same loudness as
    voice chunks whenever their own peak cleared the floor, destroying real
    SNR before `analyze_wav()` ever measured it.
26. **`AudioOutput`'s PortAudio callback thread requests SCHED_FIFO
    real-time priority on first invocation** (`_boost_priority_once`), same
    fix as gotcha 11 — this callback needs the GIL like any thread, and a
    missed callback is an instant audible glitch. Best-effort; prints its
    outcome unconditionally (not just under `SMARTSCAN_DEBUG_LIVE_AUDIO`) —
    check this line first when debugging a live-audio report.
27. **Three gain modes (`DeviceConfig.gain_mode`): `"manual"`, `"auto"` =
    the tuner's own hardware AGC (passed straight to `backend.set_gain`,
    the default), `"sw_auto"` = `SoftwareAgc` (`smartscan/agc.py`).**
    Hardware AGC can overdrive the ADC (measured +0.7/+1 dBFS peaks, extra
    spurious neighbour detections), so `"sw_auto"` exists as an explicit
    opt-in: `_maybe_apply_gain()`/`_maybe_run_software_agc()` send explicit
    `"manual"` values computed from each tick's IQ peak (target ≈ −6 dBFS,
    ±3 dB dead zone, ≥1s between steps) and never touch the hardware AGC.
    Each step pre-scales every slot's output AGC
    (`Demodulator.notify_rf_gain_change`) and clears its `level_history` —
    a gain step moves all channels' dB levels together, which
    `_detect_reflections()` would otherwise read as correlation and discard
    a real signal (rejected files are renamed `*.rejected`, never deleted).
28. **`_service_active_slots()` processes the primary slot (the only one
    that reaches the live speaker) synchronously, first** — before any
    non-primary slot is submitted to `self._slot_pool`. The old design gave
    primary no priority over recording-only slots in the same pool batch,
    so more simultaneously-recording slots directly degraded live audio.
    Non-primary slots are still parallelized among themselves as before.
    Related UI lesson from the same investigation: don't reach for
    `QSpinBox.setSpecialValueText()` for a sentinel value (e.g. 0 =
    unlimited) a user is expected to type over directly — its validator
    rejects typing over the special text, only the spin arrows bypass it.
    Also don't reflexively add a live-tick-read setting to a panel's
    `set_running()` disable list — only lock a control during a scan if a
    live change could actually corrupt something.
29. **Both real-time-priority boosts (gotcha 11, gotcha 26) print their
    outcome unconditionally** (`OK`/`NOT granted`) — check these lines
    first for any live-audio/scan-speed report; a silently-ungranted boost
    is otherwise indistinguishable from "granted but didn't help".
30. **`SpectrumView` (Listen tab mini-spectrum) must be rate-limited and
    cheap per update, or it can cause real dropped IQ chunks.** Submitting
    a frame every SCANNING tick (~30/s) with an unconditional
    `setPen()`/`setYRange()` on every call is real GIL-holding GUI-thread
    work at nearly the scanner thread's own cadence — severe enough to
    overflow `AcquisitionThread`'s bounded queue for real (this is why a
    recording can run measurably short of real elapsed time, not just a
    live-monitoring artifact). Fixed: `ScannerEngine._submit_spectrum()`
    throttles to `_SPECTRUM_FPS` (10 Hz); `SpectrumView` caches the trigger
    pen (only reassigned on color change) and skips `setYRange()` when the
    range hasn't moved > 0.5 dB. `AcquisitionThread` also has always-on
    health counters (`chunks_read`/`chunks_DROPPED`/`slow_reads`/
    `read_max_s`, printed every `_HEALTH_REPORT_INTERVAL_S`) —
    `chunks_DROPPED` is the only place that can distinguish genuine data
    loss from mere timing jitter. **Debug any live-audio/recording-timing
    report against a real `MainWindow` session, not a headless/bare-engine
    one** — GUI-thread contention doesn't exist in the latter.
31. **`_extend_edges()`/`extend_boundary_window()` (probe past a *swept
    band's* true edge) must never fire for a frequency that isn't part of a
    genuine multi-point band grid.** A standalone Memory List point channel
    or a degenerate "band" with `start_hz == stop_hz` (exactly what
    `ScannerPanel._on_add_frequency()`'s "Quick-add a frequency" button
    creates) each form a one-frequency window whose "edge" is that same Hz
    on both sides — the old code couldn't tell that apart from a real band
    edge and fabricated up to ~28 fake candidate frequencies per side,
    which then get treated as real simultaneous detections/recordings.
    Fixed via a `band_freqs` set (only frequencies from a `VfoRangeConfig`
    with `start_hz != stop_hz`) threaded through every `_extend_edges()`
    call site in `_rebuild_windows()` — the default-lane pool AND the
    weighted-band priority lane (which had no filter at all before). A real
    multi-point band still extends normally. **Any new code path that
    builds windows from a `VfoRangeConfig`'s own grid must apply this same
    `start_hz != stop_hz` check before ever calling `_extend_edges()`.**

32. **`AppConfig.debug_mode` (default off) gates the "Simulation" entry in
    the SDR device dropdown** — real users shouldn't be able to pick it by
    accident instead of their actual hardware. `ScannerPanel.refresh_device_list()`
    repopulates the combo (preserving the current selection if still valid)
    when the Options checkbox toggles it live. Also gates the RT-priority
    outcome prints (gotcha 26/29) and `AcquisitionThread`'s periodic health
    summary (gotcha 30) — `debug=self.config.debug_mode` passed into both
    at construction in `ScannerEngine._setup_static()`/`_open_backend()`.
    The actual `sched_setscheduler()` calls always still happen regardless;
    only the `print()`s are conditional, so a normal launch has zero
    diagnostic console noise.
33. **`device_combo`/`bandwidth_spin` are correctly locked while the scanner
    is running** (`set_running()`) — unlike gotcha 24's max_concurrent_recordings
    spinbox, changing SDR hardware or capture bandwidth genuinely requires
    closing and reopening the device stream, which isn't supported live.
    Don't unlock these; if this confuses users again, improve the tooltip,
    not the lock.
34. **`compute_waveform_peaks()` (Playback waveform preview) normalizes so
    the recording's own loudest bucket reaches full bar height**, not its
    raw fraction of digital full scale — a weak/distant station's real
    peaks can sit at 5-15% of full scale even where speech is clearly
    audible, which drew a waveform too flat to tell speech from silence at
    a glance. Only the *drawn* peaks are rescaled; playback level and
    `analyze_wav()`'s SNR scoring (already relative to the file's own
    content) are untouched. Guards divide-by-zero for a genuinely all-
    silent file (stays all-zero).
35. **`packaging/linux-deb/build.sh`** builds a self-contained PyInstaller
    `--onedir` bundle wrapped in a .deb (RTL-SDR + sounddevice only - no
    SoapySDR, no system PyAudio - see its own header comment and README.md
    for the full rationale/known limitations). Several real, clean-
    container-verified gotchas any other frozen build (Windows .exe,
    AppImage) will also hit - all three below are the same underlying
    failure mode (a native library loaded dynamically at *runtime*, not
    linked at import time, so PyInstaller's static analysis can't see it
    needs bundling) with three different fix shapes depending on how each
    library does its own loading:
    - **`tzdata` needs `--collect-data tzdata` explicitly** - it ships only
      data files, so PyInstaller's static import analysis never sees
      `zoneinfo`'s own runtime fallback to it and silently drops it,
      reopening the exact `ZoneInfoNotFoundError` this package was added to
      requirements.txt to prevent in the first place.
    - **`pyrtlsdrlib` needs `--collect-data pyrtlsdrlib` explicitly** -
      `pyrtlsdrlib.lib.load_librtlsdr()` locates its own bundled
      `librtlsdr.so*` via `importlib.resources.files()` at runtime, which
      this fix makes visible to the bundle at its correct package-relative
      path. Without it, the build can appear to work on a dev machine that
      happens to also have a *system* `rtl-sdr` package installed (pyrtlsdr's
      own fallback chain silently finds that instead) while failing on a
      genuinely clean machine with neither - `HAVE_RTLSDR_NATIVE` ends up
      False there, and the app falls through to the SoapySDR path this build
      deliberately doesn't ship, surfacing as "the python3-soapysdr module is
      not installed" instead of ever finding the real dongle.
    - **`libportaudio.so.2` (live audio) needs an explicit `--add-binary`,
      found via `ldconfig` at build time, plus a `sys.frozen`-gated
      `ctypes.util.find_library()` monkeypatch in `audio_output.py`** -
      `--collect-data` doesn't work here like it did for pyrtlsdrlib: Linux
      `sounddevice` only ever calls `find_library("portaudio")`, a *system-
      wide* ldconfig-cache search with no concept of a bundle's own
      `_internal/` directory, even with that directory on `LD_LIBRARY_PATH`
      (confirmed in a clean container: the .so was physically present there
      and still not found). The monkeypatch hands `find_library` the bundled
      file's real path directly instead of ever letting it search. Real,
      user-reported symptom: recording worked (demod doesn't touch audio
      output) but live listening was completely silent, with no error
      shown - `AudioOutput.available` was simply `False`. Needs
      `libportaudio2` installed on the *build* machine (system package, not
      in requirements.txt) - `build.sh` now fails loudly at build time if
      it's missing, rather than silently producing a .deb broken this way.
    - **`main.py`'s venv re-exec check and `transcription_install.py`'s
      installer both gate on `getattr(sys, "frozen", False)`** - a frozen
      build's own `sys.executable` *is* the app itself, not a real Python;
      calling `[sys.executable, "-m", "pip", ...]` would try to re-launch
      the whole GUI a second time instead of running pip. The transcription
      installer now fails with a clear message in a frozen build instead of
      doing that. Any future code path that shells out to `sys.executable`
      expecting a real interpreter needs the same guard. **That error
      message must also go through `output_line.emit()`, not just
      `finished_install.emit()`** - `TranscriptionPanel._on_install_finished()`
      shows only a generic "see the log above" label on any failure (every
      failure path, not just this one); the real explanation only ever
      reaches the user via the `install_log` box, fed by `output_line`.
    - **A fresh install has no udev rule granting a plain user access to an
      RTL-SDR dongle's USB device, and nothing stops the kernel's own DVB-T
      driver from claiming it first** - the `rtl-sdr` apt package normally
      provides both, but this self-contained build intentionally depends on
      no system package. Real, reported symptom: the device "isn't
      detected" and the app silently fell back to `Simulated` (its "auto"
      driver mode did that on *any* open failure, including a plain
      permission error) - indistinguishable from a genuine hardware
      problem. Gotcha 8's `debug_mode` gate (added later) means a non-debug
      install now surfaces this as a visible "Failed to connect... retrying"
      state instead, but the real udev/DVB-T fix below is still needed so
      it connects at all. Fixed by shipping `60-rtl-sdr.rules` (udev,
      0bda:2832/2838 → group `plugdev`) and `blacklist-rtl-dvb.conf`
      (modprobe blacklist for `dvb_usb_rtl28xxu`/`rtl2832`/`rtl2830`) via
      `postinst`. **Every `udevadm`/`rmmod` call in `postinst` is wrapped in
      `timeout`** - `udevadm control --reload-rules` can hang indefinitely
      (not just fail) with no running udev daemon (confirmed hanging the
      whole `apt install` in a bare Docker container) - a postinst must
      never be able to block an install.

## Testing without touching the user's real config/recordings

Unit tests: `.venv/bin/python -m unittest tests/test_remote_and_config.py` (config load/save, receiver path checks, sync-delete rule). To smoke-test end to end, run the app with
`QT_QPA_PLATFORM=offscreen`, select the "Symulacja" device entry, and point
`recording.output_dir` at a scratch directory before starting — otherwise
config edits during a manual test run land in the real
`~/.config/SmartScanATC/config.json`. When constructing `MainWindow`/
`ScannerEngine` directly in a script, set config overrides *before*
construction (not after) — `PlaybackPanel.__init__` and similar eagerly read
from config at construction time. Verified working combos: real RTL-SDR via
`SoapyBackend`/`RtlSdrBackend`, and full state-machine + WAV recording via
`SimulatedBackend`.
