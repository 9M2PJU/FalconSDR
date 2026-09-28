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
optional-but-preferred elsewhere. No SDR hardware/library at all →
`SimulatedBackend` fallback rather than failing to start.

PySide6 (LGPLv3), not PyQt6 (GPLv3) — `Signal`/`Slot`, not `pyqtSignal`. Set
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
   `create_backend()` must only auto-fall-back to `SimulatedBackend` for
   `driver in ("", "auto")`** — an explicitly-picked real driver that fails
   must propagate the error, never quietly simulate.
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
    about `output_format`; transcoding lives at the engine's close sites
    (`_close_slot`, `stop_manual_recording`). Missing `ffmpeg` silently
    keeps the WAV.
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
    only simulates if both fail. **Known unfixed caveat**: `SoapyBackend`'s
    own `"auto"` passes an unfiltered `{}` to `SoapySDR.Device()`, which can
    match the `"audio"` driver (gotcha 4) instead of `rtlsdr` — masked in
    practice by native RTL-SDR being tried second, not actually fixed.
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
24. **`RecordingConfig.max_concurrent_recordings`** (0 = unlimited) caps
    simultaneously-recorded transmissions — checked in
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
27. **"Auto" gain mode is a software control loop (`smartscan/agc.py`'s
    `SoftwareAgc`), never the tuner's own onboard AGC hardware** — RTL-SDR's
    built-in AGC doesn't reliably prevent ADC overload, and overload also
    worsens spurious neighbour-channel detections.
    `ScannerEngine._maybe_apply_gain()`/`_maybe_run_software_agc()` send
    explicit `"manual"` gain values computed from each tick's own captured
    IQ peak (target ≈ −9 dBFS peak, ±3 dB dead zone, ≥1s between
    adjustments). Backends never receive `"auto"` from the engine anymore —
    never call `backend.set_gain("auto", ...)` from the engine again.
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

## Testing without touching the user's real config/recordings

There's no test suite yet. To smoke-test end to end, run the app with
`QT_QPA_PLATFORM=offscreen`, select the "Symulacja" device entry, and point
`recording.output_dir` at a scratch directory before starting — otherwise
config edits during a manual test run land in the real
`~/.config/SmartScanATC/config.json`. When constructing `MainWindow`/
`ScannerEngine` directly in a script, set config overrides *before*
construction (not after) — `PlaybackPanel.__init__` and similar eagerly read
from config at construction time. Verified working combos: real RTL-SDR via
`SoapyBackend`/`RtlSdrBackend`, and full state-machine + WAV recording via
`SimulatedBackend`.
