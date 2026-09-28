# AGENTS.md — SmartScanATC

Guidance for AI coding agents working in this repository.

## What this is

SmartScanATC (`smartscanatc/`) is a PySide6 desktop app that scans a radio band
(default: airband ATC 118–136 MHz) via SoapySDR (Linux) or a native
librtlsdr backend (Windows/macOS - see gotcha 16) and records audio **only**
for transmissions the scanner actually stops on — never the retuning noise.
Full functional spec was provided by the user in Polish; the state machine in
`smartscan/scanner_engine.py` is the authoritative implementation of it.

## Running it

```
cd smartscanatc
python3 main.py
```

`PySide6`, `numpy`, `scipy`, `pyqtgraph`, `pyrtlsdr`/`pyrtlsdrlib` and
`sounddevice` all have reliable pip wheels on every platform — install via
`pip install -r requirements.txt` (or a venv); on Windows/macOS this is
enough by itself to get a working RTL-SDR + live audio setup, no system
packages needed (see gotcha 16). `SoapySDR` and `PyAudio`/PortAudio remain
the two things that need system-level installation on Linux (apt - see
`requirements.txt`) and stay optional-but-preferred on Windows/macOS. If no
SDR library/hardware is available at all, the app auto-falls-back to
`SimulatedBackend` (see below) rather than failing to start.

**Migrated from PyQt6 to PySide6** (see git history) specifically so the
project isn't forced onto GPLv3 by its GUI toolkit's own license — PySide6 is
LGPLv3 (Qt Company), which doesn't impose that constraint the way Riverbank's
PyQt6 (GPLv3/commercial) does. If you're adding new Qt code: PySide6's signal/
slot classes are `Signal`/`Slot` (not `pyqtSignal`/`pyqtSlot`), everything else
(scoped enums like `Qt.Key.Key_Delete`, `.exec()`, QtMultimedia, QtWidgets
layout) is essentially identical between the two for Qt6 — this codebase
already used the modern scoped-enum style throughout, which is exactly what
made the migration low-risk. `pyqtgraph` auto-detects whichever Qt binding is
importable; if you ever have both PyQt6 and PySide6 installed side by side in
the same environment (e.g. while testing), set `PYQTGRAPH_QT_LIB=PySide6`
explicitly so it doesn't guess wrong — a real app install only ever has
PySide6 present, so this ambiguity doesn't exist for end users.

**i18n**: every user-facing string is English source text passed through
`tr()` (see `smartscan/i18n.py`); translations are plain `.po` files under
`smartscan/locale/` parsed at runtime by a small hand-rolled parser —
deliberately not real `gettext`, which would need a compiled `.mo` (via the
external `msgfmt` tool) as one more step between "someone writes a
translation" and "the app can use it". `AppConfig.language` defaults to
`"auto"`, resolved once at startup via `i18n.resolve_system_language()`
(`QLocale.system()`, not the stdlib `locale` module — reliable cross-platform,
env vars like `LANG` aren't) to whichever supported language matches the
OS, or English if none does. Changing language still needs a restart (no
live retranslation — every widget's text is set once, at construction, from
whatever `tr()` returned then), but `LanguagePanel` + `MainWindow._restart_app()`
do it as one click: clean shutdown (same path as closing the app) then
`os.execv()` re-launches the same process, the same self-re-exec trick
`main.py` already uses for the venv redirect. Never build a module-level
constant (list/dict) from `tr()` calls — a handful of files did this early on
(`_WEEKDAY_LABELS`, table `COLUMNS`, sort-option lists) and it's a real bug:
the constant freezes in whatever language was active at *import* time, which
is always English since imports happen before `i18n.set_language()` ever
runs in `main()` — wrap it in a small function called at point of use
instead. `military_presets.py`'s `BAND_PRESETS` labels/tooltips hit the same
trap from the opposite direction — they're plain English strings in that
module (translated only at the `scanner_panel.py` call site via `tr(preset.label)`)
for exactly this reason. Add a new language by dropping one `.po` file — see
`smartscan/locale/README.md` — no code changes, no build step.

## Architecture

- `smartscan/models.py` — `ScannerState` enum, `Channel`, `BlacklistEntry`, dataclasses.
- `smartscan/config.py` — `AppConfig`, persisted to `~/.config/SmartScanATC/config.json`.
  `device.sample_rate_hz` doubles as the wideband **capture bandwidth** (default 2.4 MHz,
  matched to RTL-SDR); `memory_channel_bandwidth_hz` is the per-channel power-window width
  used only in Memory List mode (VFO Range mode uses `vfo_range.step_hz` instead).
- `smartscan/storage.py` — CSV read/write for `channels.csv` / `blacklist.csv` (same config dir).
- `smartscan/channelizer.py` — wideband channelization: `plan_windows()` greedily groups a
  sorted set of channel frequencies into the fewest ~2.4 MHz capture windows; `channel_powers_dbfs()`
  does one FFT per captured block and sums power per channel's bins (this is what lets the
  scanner watch dozens of channels at once instead of retuning to each); `spectrum_snapshot()`
  feeds the GUI's mini-spectrum.
- `smartscan/dsp.py` — `level_dbfs()` (RMS power in dBFS, still used for narrowband contexts)
  and `Demodulator` (AM/NFM/WFM → audio). `Demodulator.demod(iq, modulation, offset_hz)` mixes
  a channel down from a wideband capture's window center to baseband *before* decimating and
  demodulating — order matters (see gotcha 6 below).
- `smartscan/sdr_backend.py` — `SoapyBackend` (real hardware via SoapySDR, exposes
  `gain_range_db` read from the device and a live `set_gain()`), `RtlSdrBackend` (real
  hardware via native librtlsdr/pyrtlsdr, same contract - see gotcha 16), and
  `SimulatedBackend` (deterministic fake bursts on 121.5/118.1/124.0 MHz, reacts to
  `set_gain()` by scaling the fake signal so the Gain slider does something visible even
  without hardware). `open_real_backend()` picks between the two real backends (see gotcha
  16); `create_backend()` transparently falls back to simulation only if *that* fails too.
  `AcquisitionThread` wraps whatever `create_backend()`/`open_real_backend()` returns - see
  gotcha 11 below, this is what `ScannerEngine._backend` actually is at runtime, not the
  raw backend.
- `smartscan/audio_output.py` — live speaker monitoring via PyAudio, falling back to
  `sounddevice` if PyAudio isn't installed (see gotcha 16) - a no-op if neither is.
- `smartscan/recorder.py` — WAV writer only; Single-file vs Split-file modes. Always writes
  WAV in real time regardless of `RecordingConfig.output_format` — MP3 is a
  finish-line transcode, not a `Recorder` concern (see gotcha 14 below and
  `smartscan/audio_transcode.py`). `AppConfig.load()` still drops a stale `audio_format`
  key from an earlier, never-shipped mp3 attempt that used that name.
- `smartscan/scanner_engine.py` — `ScannerEngine(threading.Thread)`, the state machine:
  `CALCULATE_NEXT_FREQ → TUNING → SCANNING → SIGNAL_DETECTED ⇄ LINGER`. In
  `CALCULATE_NEXT_FREQ` the tuner retunes to a whole *capture window* (`_next_window()`),
  not a single channel. `SCANNING` computes power for every non-blacklisted channel in the
  window from one FFT and picks the strongest; if it clears the trigger, that specific
  channel becomes `_cur_freq`/`_cur_offset_hz` and demod/recording lock onto it while the
  tuner stays put. Recording and live audio are only ever active in
  `SIGNAL_DETECTED`/`LINGER`. Talks to the GUI thread via `ScannerSignals(QObject)`
  (`Signal`), which Qt marshals across threads safely. `_maybe_apply_gain()` is polled
  once per loop iteration so a live Gain-slider drag takes effect mid-scan without restarting.
- `smartscan/gui/` — `main_window.py` wires three panels (`panels/scanner_panel.py`,
  `panels/frequency_manager.py`, `panels/recording_panel.py`) plus `widgets.py`
  (`LevelMeter`, `RecIndicator`, `SliderSpin` — a QSlider+spinbox pair that updates live
  while dragging, used for Trigger Level, Squelch offset, Gain, Tuning Time, Linger Time — and
  `SpectrumView`, a pyqtgraph mini-spectrum of the currently captured window) and
  `theme.py` (dark QSS).

## Known gotchas (hit these once already — don't reintroduce)

1. **Signal-loading guard.** Every GUI panel that both (a) loads values from
   `AppConfig` into Qt widgets on construction and (b) has those same widgets'
   `valueChanged`/`toggled` wired to a "push to config + persist" slot **must**
   set `self._loading = True` before populating widgets and `False` after
   (see `scanner_panel.py`, `recording_panel.py`). Without it, `setValue()`
   calls fire mid-load with sibling widgets still at their constructor
   defaults, and the half-initialized state gets written straight to
   `~/.config/SmartScanATC/config.json` on every launch.
2. **Don't use `QComboBox.findData()` with tuple/object payloads.** It's
   unreliable (works in isolated repros, silently returns -1 in the full app).
   Do a manual linear scan over `itemData(i)` instead — see
   `scanner_panel._load_from_config`.
3. **`SimulatedBackend.read()` must sleep for `num_samples / sample_rate`.**
   Without real-time pacing, the engine thread spins the SCANNING dwell
   thousands of times a second and floods the GUI with signal emissions.
   Burst timing itself is a deterministic function of wall-clock time + a
   hash of the tuned frequency (not a persisted random-until timestamp),
   specifically so retuning to the same frequency can't cancel a scheduled
   burst.
4. **SoapySDR also enumerates "audio" driver devices** (from
   `soapysdr-module-audio`, e.g. sound cards) as if they were SDR receivers.
   `SoapyBackend.enumerate_devices()` filters `driver == "audio"` out —
   don't remove that filter or the device dropdown fills with sound cards.
5. **ATC airband is AM, not FM.** `Modulation.AM` is the correct default for
   118–136 MHz; don't "fix" this to FM.
6. **A second squelch threshold is only safe if it's an offset from trigger,
   never an independent absolute value.** The user hit a real bug in
   SDR++ from an independent squelch: it could mute audio even when
   SDR++'s own scan logic had correctly decided a signal was present,
   because the two used unrelated thresholds. `config.trigger_level_dbfs`
   still alone drives the stop-scanning decision (`SIGNAL_DETECTED`/
   `LINGER`) - that state machine is untouched by squelch. A second,
   *optional* threshold was added later (`config.squelch_enabled` /
   `squelch_offset_db`, off by default) specifically to drop the loud
   FM-discriminator noise burst that follows a transmitter releasing PTT
   (see gotcha 7 below on `Demodulator.demod()` — a phase-derivative FM
   discriminator fed pure noise, once the real carrier drops, produces loud
   broadband output despite low actual RF power: the "FM threshold effect"
   — not a bug, just how FM demod behaves below carrier) from the live
   speaker entirely, and from the recording's *audible content* (but not
   its timeline - see below). Went through three follow-up revisions after
   the speaker-only version shipped first:
   - v2 dropped squelched blocks from the recording too
     (`_play_and_record_slot()` returned before `slot.recorder.write_audio()`
     as well as `self._audio_out.write()`) - a recording should end where
     the real signal did, not trail into that noise tail.
   - v3 replaced that skip with writing *silence* to the recording instead
     (`slot.recorder.write_audio(silence, real=False)`) for an explicit,
     deliberate reason: skipping the block outright made a single,
     still-open recording jump-cut mid-file, so on playback there was no
     way to tell "that message was continuous" from "that's a signal
     caught in bursts with real gaps edited out". A real, audible gap
     belongs *between* recordings - once LINGER fully expires and a later,
     genuinely new detection opens a separate file, two recordings is
     exactly right - never *inside* one. Silence keeps a single recording's
     duration matching real elapsed wall-clock time (so a listener can
     gauge the actual pause between two transmissions) while still never
     recording the loud garbage itself. `Recorder`'s `real=False` param
     (vs. the default `real=True`) tracks whether a transmission ever had
     anything *actually* worth hearing, independent of anything having been
     *written* - see `Recorder.end_transmission()`'s own docstring (its
     phantom-recording cleanup had to be updated accordingly: an
     all-silence file must still be treated as nothing worth keeping,
     exactly like the old zero-bytes-written case it originally targeted).
   - v4 (current) refined v3 further, again by explicit request: a *middle*
     gap (more real audio follows later, same transmission) should still
     read as real silence, but the *trailing* tail (transmission ends while
     squelched, nothing else ever follows) should be trimmed away entirely,
     not kept as dead air at the end of the file - "the end should just
     end". `Recorder` can't know which case it's in at write time, only in
     hindsight, so `write_audio(audio, real=False)` now *holds* silence in
     `self._pending_silence` instead of writing it immediately: a
     subsequent `real=True` write flushes any pending silence first (in
     order) before the real audio - that's the "it was a real pause" case -
     while `_close_file()` (reached via `end_transmission()`/
     `stop_session()`) discards whatever's still pending, unwritten - "it
     was just the tail". Bounded memory (at most one LINGER period's worth
     of chunks). If you touch this again: a middle gap and a trailing tail
     are indistinguishable at the moment a squelched block arrives - the
     only correct way to tell them apart is still "did real audio ever
     follow, before this file closed" - don't try to guess from
     e.g. how long the gap has lasted so far.
   - **Consequence that bit us once already: `analyze_wav()`
     (playback/audio_analysis.py) computes "noise floor" as a low
     percentile of per-window RMS - v3's introduction of *exact* zero-
     amplitude windows (real receiver noise is never exactly 0; silence
     written by write_audio(..., real=False) is) let that percentile land
     on a literal 0.0 for any recording with enough squelched gap, blowing
     the SNR/quality-score calculation up to nonsense (100+ dB, quality
     pinned at 100) for what was often a genuinely quiet/marginal
     recording.** Fixed by excluding exact-zero-RMS windows from the noise-
     floor/signal-level/clipping-ratio calculations entirely (see
     `analyze_wav()`'s own comment) - measure quality from the real content
     only, invariant to however much silence surrounds it. **Any future
     change to quality-scoring logic must also account for
     `QualityCache`/`TranscriptionCache`-style caches never auto-
     recomputing: a cache entry keyed by path+size+mtime doesn't know the
     *code* that produced it changed** - fixing `analyze_wav()` alone left
     already-cached bad values sitting on disk indefinitely (the file
     itself never changes, so its cache key doesn't either). Had to also
     directly invalidate (delete) the affected cache entries by hand this
     time; a real fix touching scoring/analysis logic again should do the
     same for whatever's affected, not just fix the function.
   It avoids
   reopening the SDR++ bug by construction, not by which sinks it touches:
   `ScannerEngine._effective_squelch_dbfs()` = `_effective_trigger_dbfs(window)
   + min(0.0, squelch_offset_db)` — an offset (clamped <= 0) *from* the
   same effective trigger the state machine uses, so its threshold can
   never sit above trigger and therefore can never fire while level is
   still >= trigger (i.e. SIGNAL_DETECTED) - only during the LINGER tail,
   once level has already dropped below trigger. The LINGER timer/slot-
   close decision itself is still keyed to trigger_dbfs alone, regardless
   of squelch. If you touch this: keep it an offset from trigger, never an
   independent absolute dBFS, and keep the SIGNAL_DETECTED/LINGER state
   machine itself untouched — reintroducing either of those is the SDR++
   bug again (dropping squelched blocks from more sinks, as happened here,
   is not).
7. **In `Demodulator.demod()`, mix-to-baseband must happen before decimation,
   and decimation before AM/FM demod** — not after. `resample_poly`'s
   anti-alias filter is what actually rejects neighbouring channels in a
   wideband capture; demodulating the full-rate multi-channel signal first
   (the old narrowband-era order) lets adjacent channels leak into the AM
   envelope / FM discriminator once more than one channel is present in a
   block.
8. **`SoapyBackend.read()` must raise if it gets zero samples for a full
   second, and `create_backend()` must not silently fall back to
   `SimulatedBackend` when the user explicitly picked a real device.**
   `readStream` on a dead/unplugged USB device just keeps returning nothing
   without raising - if `read()` swallowed that (returning an empty array),
   the state machine would happily keep cycling CALCULATE_NEXT_FREQ →
   TUNING → SCANNING forever on dead air, which looks exactly like a
   working scan from the GUI. This is a real bug a user hit (unplugged the
   RTL-SDR mid-scan, app kept "hopping through frequencies"). The fix is
   two-sided: `read()` raises `RuntimeError` on a fully empty read, and
   `create_backend()` only auto-simulates for `driver in ("", "auto")` -
   an explicitly-chosen real driver that fails to open propagates the error
   instead of quietly substituting fake data.
9. **`SimulatedBackend` must inject a fake signal at its offset from the
   currently tuned frequency, not only when `freq_hz` exactly equals the
   "active" frequency.** In wideband mode the tuner sits on a window center,
   which is deliberately nudged off any exact channel frequency (see
   `plan_windows`), so an exact-match check silently never fires. Iterate
   `_ACTIVE_FREQS`, check each is within the current capture bandwidth of
   `freq_hz`, and place the tone at `active_freq - freq_hz`.
10. **Auto-reconnect (flaky USB) is deliberate, not accidental resilience.**
    `ScannerEngine.run()` doesn't stop on a `RuntimeError` from the backend -
    it calls `_open_backend_with_retry()`, which retries with escalating
    backoff (`RECONNECT_BACKOFF_S` = 2/5/10/20/30s) until the device reopens
    or the user presses Stop, emitting `ScannerState.RECONNECTING` meanwhile.
    Critically, once a session has proven real hardware was present
    (`_had_real_hardware`), reconnect attempts call `SoapyBackend(...)`
    directly instead of `create_backend(...)` - bypassing its "auto" fallback
    to `SimulatedBackend` - so a still-unplugged dongle keeps raising and
    keeps retrying instead of silently swapping to fake data (see gotcha 8;
    this would otherwise reopen that exact bug on every reconnect). Any
    in-progress recording is closed via `_end_transmission()` before
    retrying, since that transmission is definitively over. Don't
    "simplify" this back to a bare try/except that just stops the thread.
11. **`ScannerEngine._backend` is an `AcquisitionThread` (sdr_backend.py),
    not the raw `SoapyBackend`/`SimulatedBackend` - don't call
    `backend.read()` directly from `_main_loop()`'s thread ever again.**
    Root cause this fixed, confirmed on real RTL-SDR hardware: the old
    design called the real `backend.read()` and did *everything else*
    (demod/noise-reduction/file-write for every simultaneously active slot,
    FFT for new-detection candidates, ...) on the same thread, sequentially.
    The RTL-SDR driver keeps filling its own small (~100ms) internal ring
    buffer regardless of when we call `readStream()` - if that "everything
    else" ever took too long between two reads (many simultaneous active
    slots is enough on its own - measured 21 simultaneous slots on a dense
    FM broadcast segment, ticks running 5x over their real-time budget),
    the driver's buffer overflowed and *silently dropped/spliced real
    samples* - a genuine hardware-level data loss, nothing to do with
    squelch or noise reduction, audible as a click/"ścinka" in both live
    audio and recordings since both come from that same corrupted IQ block.
    Measured historically at ~5% of all 5-second windows having at least
    one such overflow event, 99.6% having at least one read() slower than
    one tick's budget (see `sdr_io_diag.jsonl`, `SdrIoDiagLogger`). Fix:
    `AcquisitionThread` is a dedicated thread whose only job is calling the
    real backend's `read()` back-to-back into a bounded FIFO queue - no
    demod, no file I/O, no FFT ever runs between two of its own read()
    calls, so a slow consumer can never delay the actual hardware read
    (exactly the acquisition/processing separation SDR++ and other mature
    SDR software use, and the same pattern this project already applied to
    `AudioOutput`'s ring buffer and `_SpectrumWorker` - just never, until
    this fix, to the one thing that actually touches the hardware).
    Verified on real hardware: the identical stress scenario that produced
    14 `SOAPY_SDR_OVERFLOW` events in 35 seconds pre-fix produced 0
    post-fix, at *higher* system load. `AcquisitionThread` proxies
    `read()`/`set_frequency()`/`set_gain()`/`close()`/`label`/
    `gain_range_db`/`stream_mtu` with the exact same contract a raw
    `SdrBackend` has (including gotcha 8's dead-device `RuntimeError`
    propagating through unchanged), so `ScannerEngine` needed no other
    changes - keep it that way; don't have `_main_loop()` reach past the
    wrapper to the raw backend for "just this one call".
    **`AcquisitionThread` is single-owner: it is the *only* thread that
    ever touches the real backend, for reads AND for
    `set_frequency()`/`set_gain()`.** The first version of this fix instead
    shared a `_device_lock` between this thread and whichever thread called
    `set_frequency()`/`set_gain()` - and broke real scanning as a direct
    result: this thread also runs with SCHED_FIFO real-time priority (see
    `_boost_priority()`, needed for prompt/reliable reads), and a lock
    shared with a normal-priority caller is a textbook real-time priority-
    inversion setup, not just a theoretical risk - measured on real
    hardware, a retune's wait to acquire that lock was starved for up to
    **11.7 seconds** by this thread continuously winning the race to
    re-acquire it first every time it released it, because the kernel keeps
    running a runnable SCHED_FIFO thread ahead of a normal-priority one
    with no fairness guarantee for who gets a plain `threading.Lock` next.
    Symptom actually reported by the user: a 121-136 MHz civil-aviation
    sweep (7 capture windows) that normally takes ~2.7s took 20+ seconds,
    each window "stuck" with nothing moving in the spectrum, for no visible
    reason. The fix: `set_frequency()`/`set_gain()` now hand a *command* to
    this thread (`_pending_command` + `_command_done`, both `_cond`-guarded)
    instead of touching the device themselves; this thread applies it
    (clearing the queue and bumping `_generation`) right before its next
    read(), bounding retune latency to about one read()'s worth of time
    (~30-100ms on this project's own hardware) instead of an unbounded,
    priority-dependent lock wait. Re-verified on real hardware after this
    second fix: the same 121-136 MHz sweep returned to ~2.9s (72 retunes in
    30s, gap between them a consistent 383-403ms, vs. the broken version's
    383ms-12000ms). If you ever touch the retune path: never reintroduce a
    lock shared between this thread and any other - route the request
    through a pending-command handoff like this one instead, so this
    thread stays the single owner of the real device no matter what.
    `_generation` (bumped whenever a command is actually applied) still
    guards the queue against handing out a block whose read() started
    before that point - now essentially unreachable in practice since
    command application and read() are strictly sequential on this one
    thread, but kept as a cheap, harmless second line of defense.
12. **`_service_active_slots()` demodulates every simultaneously active
    slot in parallel via `self._slot_pool` (a `ThreadPoolExecutor`), not
    one after another.** AcquisitionThread (gotcha 11) stops the *driver*
    from dropping samples when a tick runs long, but it doesn't make the
    tick itself faster - with enough simultaneously active slots (measured
    on real hardware: 10 on a dense FM broadcast segment), demod +
    noise-reduction + file-write for every one of them, done sequentially,
    still blew the ~30ms per-tick real-time budget 2.6-5x over on its own -
    which then backs up `AcquisitionThread`'s own bounded queue until *it*
    starts dropping chunks too, the exact same audible "ścinka" symptom
    just moved one layer up, and starves `AudioOutput`'s ring buffer
    (repeated "re-priming after N consecutive empty callbacks" - audible
    live dropouts). Benchmarked before committing to this fix (see the
    session that added it): `Demodulator.demod()`/`NoiseReducer.process()`
    are dominated by `resample_poly`/FFT calls that release the GIL, so
    running N slots' worth on a thread pool measured 3-4.6x real wall-clock
    speedup on this project's own real workload/data sizes - not just a
    theoretical GIL argument. Verified on real hardware, identical stress
    scenario as gotcha 11's: pre-parallelization, every single tick was
    over budget (max ~74-100ms); post-parallelization, zero ticks over
    budget (max ~20-28ms), `AudioOutput`'s buffer stayed healthily full
    (~520ms, stable) instead of repeatedly starving. Safe to parallelize
    because each `_Slot` owns its own `Demodulator`/`NoiseReducer`/
    `Recorder` (see `_Slot`'s docstring) - no shared *mutable* state between
    these tasks to guard against; `self._audio_out` (shared, since only the
    primary slot ever writes to it) already locks internally. If you touch
    this: keep each slot's own demod/noise-reduction/recording objects
    exclusively owned by that slot (never share one across slots/threads),
    and keep the LINGER timer/slot-close bookkeeping on the main thread
    after every future has resolved (not inside the pooled task itself) -
    `_close_slot()` mutates `self._slots`, which only this one thread
    should ever touch.
13. **Manual "Record" button records continuously and independently of the
    trigger state machine — it is not just a manual override of auto-recording.**
    `ScannerEngine.start_manual_recording()`/`stop_manual_recording()`/
    `is_manual_recording()` own a second, separate `Recorder` instance
    (`self._manual_recorder`, constructed with `Recorder(..., single_file=True)`
    — see gotcha 23 - a manual session is always one file, the per-
    transmission file-per-slot behaviour doesn't apply) guarded by its own
    `self._manual_recorder_lock`, entirely independent of the per-slot
    `Recorder`s that `SIGNAL_DETECTED`/`LINGER` drive. It's fed from two call
    sites: `_play_and_record_slot()` mirrors the primary slot's demodulated
    audio into it (`_feed_manual_recording(mono)`, only when
    `slot.freq_hz == self._primary_freq` — a manual recording always follows
    whatever the live speaker is currently monitoring, never a secondary
    slot) whenever a real signal is active, and `_main_loop()`'s SCANNING
    branch feeds it a chunk of literal zeros once per tick when
    `not any_active` — this is what makes it "continuous, including
    silence" rather than only capturing whatever auto-recording would have
    captured anyway. The lock exists because these two feed paths run on
    different threads (the engine's main loop thread for the idle-silence
    tick, and `self._slot_pool` worker threads for the mirrored real audio —
    see gotcha 12) plus the GUI thread calling start/stop. Unlike the
    per-slot recorders, a manual recording is never dropped as a "phantom" —
    `write_audio(audio, real=True)` unconditionally, no `real=False`/
    pending-silence dance (gotcha 6's squelch logic doesn't apply here: this
    button's whole point is "record everything, including the boring
    parts", not "record only what's worth hearing"). `_teardown()` force-stops
    it first, before anything else, so stopping the scanner always closes any
    open manual recording rather than leaving a file handle dangling.
    `AppConfig.recording.enabled` ("Auto-recording") and the manual button are
    two independent, coexisting mechanisms, not one config flag with two UIs —
    confirmed by explicit user request after two rounds of clarification: the
    checkbox (surfaced identically, kept in sync, in both the Listen tab and
    Options — `ScannerPanel.auto_recording_toggled` /
    `RecordingPanel.enabled_changed`, wired together in `MainWindow`) still
    means "record every detected transmission automatically", default off;
    the button means "record the live channel right now, no matter what the
    trigger is doing", off by default too but never persisted (it's always
    off again on next launch/Start — a deliberate one-off action, not a
    setting). `PlaybackPanel.update_recording_notice()` shows an amber banner
    in Odtwarzacz whenever auto-recording is off, purely informational (it
    doesn't know or care about manual recording), so a user who only ever
    used the manual button isn't confused about why their auto-recorded
    library is empty.
    **Follow-up, same session, by explicit request ("manualne nagrywanie
    powinno zapisywać pierwszą w nagraniu częstotliwość"):** a manual
    recording that actually captures a real signal is renamed, once it
    stops, from the generic `models.MANUAL_FREQ_KHZ` "_session" naming
    (gotcha 19) to the same frequency-tagged naming split-mode auto-
    recordings use - `ScannerEngine._manual_recording_first_freq` is a
    first-wins latch (set only once per recording, never overwritten by a
    later different frequency, reset in `start_manual_recording()`) updated
    from `_feed_manual_recording()`'s `freq_hz` parameter - passed only by
    `_play_and_record_slot()`'s *real*-audio mirror (`slot.freq_hz` when
    `slot.freq_hz == self._primary_freq`), never by the squelched-silence
    mirror just above it in the same function, and never by
    `_main_loop()`'s idle-tick silence feed - both of those correctly leave
    it at its `None` default, since neither represents a real captured
    frequency. `stop_manual_recording()` reads the latch (and resets it)
    *before* calling `recorder.stop_session()`'s mp3 transcode step (gotcha
    14) - `Recorder.rename_session_file_with_frequency()` (a `@staticmethod`
    living next to `_single_file_path()`/`_split_file_path()`, the naming
    convention's other two sources of truth) does the actual rename on the
    closed `.wav`, *before* any mp3 transcode, so the mp3's filename
    inherits the frequency too rather than the transcode needing its own
    separate renaming logic. A recording that never captured a real
    frequency (e.g. tuned to a range with no active traffic the whole time)
    keeps the existing "_session"/"Manual recording" fallback exactly as
    before - verified deterministically (a custom band with no simulated
    activity in it at all, not just "got lucky/unlucky with timing"), not
    just the common case. A failed rename (filesystem error) leaves the
    original "_session" name in place rather than losing the recording.
14. **MP3 output (`RecordingConfig.output_format`) is a transcode that runs
    strictly *after* a WAV file is fully closed, at the engine's own
    finish-line call sites — never inside `Recorder`, and never before this
    project's WAV metadata chunk has already been written.**
    `smartscan/audio_transcode.py`'s `transcode_to_mp3()` shells out to a
    system `ffmpeg` (checked via `ffmpeg_available()`; a missing ffmpeg
    silently leaves the WAV in place rather than losing the recording or
    erroring the scanner — same "system dependency degrades gracefully"
    pattern as SoapySDR/PyAudio). `Recorder` itself stays WAV-only and
    doesn't know `output_format` exists at all - keeping the transcode
    decision at the call site is what makes the ordering constraint
    tractable: `ScannerEngine._close_slot()` calls `append_metadata_chunk()`
    (a RIFF/LIST/INFO chunk - see `wav_metadata.py`) on the still-WAV file
    *first*, and only then, if `output_format == "mp3"`, transcodes and
    swaps `finished_path` to the `.mp3` - an mp3 container has no room for
    that RIFF chunk, so doing it in the other order would silently lose the
    metadata (or corrupt the mp3). The pre-existing gap where "Single File"
    mode's per-slot `Recorder.end_transmission()` always returns `None` (no
    metadata, no `recording_finished` signal - see its own docstring) is
    untouched by this: `_close_slot()` still transcodes that mode's file
    too (captures `current_path` before `stop_session()` closes it) but
    deliberately does *not* start emitting `recording_finished` for it,
    since that would be a behavior change unrelated to adding mp3.
    `stop_manual_recording()` has its own transcode call for the same
    reason (a manual recording is a separate `Recorder`, gotcha 13, with no
    metadata chunk involved at all) - captures `current_path` before
    `stop_session()`, transcodes after, and only *then* emits
    `recording_finished`/updates `manual_recording_changed`, so a listener
    reacting to either signal always gets a path that actually exists on
    disk. If you touch this again: never move the transcode call earlier
    than metadata embedding, and never put it inside `Recorder` itself -
    format is an engine-level, post-close concern, not a writer concern.
15. **Raw IQ recording (`smartscan/iq_recorder.py`, punkt 7 - "for technical
    users") is a *third*, fully independent recording mechanism - not audio
    at all, and not `Recorder`-based.** `IQRecorder` writes the actual
    wideband complex samples this project's channelizer/demodulator itself
    works from, in SigMF (a `.sigmf-data` raw complex64 file +
    `.sigmf-meta` JSON sidecar - a real, tool-interoperable format: GNU
    Radio, Inspectrum, SDR++ and others can read it directly), fed only
    from `_main_loop()`'s SCANNING-state read
    (`self._feed_raw_iq(iq, window.center_hz)`, right after
    `self._backend.read(chunk)`) - never the TUNING-state read, which is
    deliberately muted garbage while the tuner settles (same "never the
    retuning noise" rule this project already applies to audio, gotcha
    6/AGENTS.md's own opening line). A capture can span several retunes
    (the scanner hopping between capture windows mid-recording) - SigMF's
    `captures` list records each frequency segment's starting sample offset
    (`IQRecorder.write()` appends a new entry whenever `center_freq_hz`
    changes), so a single continuous file still carries an accurate
    frequency-vs-time record instead of falsely implying one fixed center
    frequency throughout. Deliberately does **not** go through
    `recording_finished` (see `ScannerSignals.raw_iq_recording_changed`
    instead) - that signal feeds the Odtwarzacz library
    (`playback_panel.ingest_finished_recording`), which expects a playable
    WAV/MP3, and a raw complex-sample binary is neither; files land in a
    dedicated `raw_iq/` subfolder of the recordings output dir instead,
    kept out of the audio library/organizer entirely on purpose. Manual,
    momentary control only (a "Record raw IQ"/"■ Stop recording IQ" button
    in the Listen tab, same `record_row` as the manual audio Record button -
    moved there from Options by explicit request, see git history -
    mirroring gotcha 13's pattern) - deliberately *not* an auto-record
    toggle, since uncompressed IQ is large (~sample_rate x 8 bytes/second -
    ~19 MB/s at this project's
    default 2.4 MSPS capture bandwidth), which the button's own tooltip
    says explicitly. `ScannerEngine._teardown()` force-stops it (alongside
    the manual audio recorder) so a Stop click always finalizes the SigMF
    meta file rather than leaving `captures`/sample-count bookkeeping
    incomplete. If you touch this: keep it reading from the *shared* window
    capture (whatever `_service_active_slots()` also demodulates from that
    same tick), not a second independent hardware read - there is only one
    `AcquisitionThread` (gotcha 11) and one read per tick to go around.
16. **Windows/macOS get RTL-SDR without SoapySDR at all, through
    `RtlSdrBackend` (native librtlsdr via `pyrtlsdr`/`pyrtlsdrlib`) - a real
    hardware backend, not a shim over `SimulatedBackend`.** SoapySDR has no
    pip wheel and PothosSDR (its Windows installer bundle) has been
    unmaintained since 2021, so requiring it there would mean "doesn't
    really work on Windows" in practice. `pyrtlsdrlib` ships a prebuilt
    librtlsdr (with RTL-SDR Blog V4 support) for Windows/macOS/Linux inside
    its own wheel - `pip install -r requirements.txt` alone is enough on
    Windows/macOS (Windows still needs the WinUSB driver via Zadig, the
    same one SDR++/SDR# require - nothing this project can automate). On
    Linux the bundled library lives entirely inside the venv's
    site-packages and never touches the system's own `librtlsdr.so` that
    SoapySDR links against - installing `pyrtlsdr`/`pyrtlsdrlib` alongside
    `python3-soapysdr` is safe, no collision (confirmed - both a real
    RTL-SDR device and SoapySDR were present on the machine this was
    verified on).
    `RtlSdrBackend` matches `SoapyBackend`'s contract on every point gotcha
    8/10/11 depend on: bias-tee forced off on open and every retune,
    `SdrBackend._wait_for_reopen_cooldown()`, and `read()` raising instead
    of ever returning short/empty (librtlsdr's sync read raises
    LibUSBError/IOError on a dead device - never silently returns fewer
    bytes). It also matches SoapyRTLSDR's own CF32 sample scaling exactly
    (`(i - 127.4) / 128`, not pyrtlsdr's own default `read_samples()`
    scaling of `i / 127.5 - 1`, which is close but not identical) so a
    trigger/squelch threshold tuned on Linux/SoapySDR still means the same
    dBFS on Windows/macOS with the same `config.json`. `read()` always
    requests a whole multiple of 512 bytes (the USB packet size - a known
    source of libusb overflow errors otherwise) and carries any leftover
    bytes over to the next call (`self._carry`, discarded on
    `set_frequency()` - leftover bytes belong to the frequency that's now
    gone, splicing them into the next read would corrupt one IQ block the
    same way gotcha 11's overflow bug did) - verified directly against real
    RTL-SDR Blog V4 hardware on Linux (20 consecutive odd-sized,
    non-512-aligned reads, a retune mid-stream, no shape/dtype corruption).
    `enumerate_devices()` (module-level function, not `SoapyBackend`'s own
    static method - `ScannerPanel._populate_devices()` now calls this one)
    only adds native entries when Soapy doesn't already list an `rtlsdr`
    device, so the one dongle never appears twice on Linux with both
    present - verified on the same real hardware.
    `open_real_backend()` (used by both `create_backend()` and
    `ScannerEngine._open_backend_with_retry()`'s reconnect path - never
    the raw `SoapyBackend`/`RtlSdrBackend` constructor directly) is the
    only thing that knows how to pick between the two: an explicit
    `driver="rtlsdr_native"` always uses this backend; an explicit
    `driver="rtlsdr"` (SoapySDR's own key) uses native librtlsdr instead
    *only* when SoapySDR isn't installed at all - the point being a
    `config.json` saved on Linux with SoapySDR's driver key still opens
    correctly on a Windows/macOS install of the exact same config; `"auto"`
    tries SoapySDR first, native RTL-SDR second, and only raises (letting
    `create_backend()`'s own separate try/except substitute
    `SimulatedBackend`) if both fail - never silently prefers simulation
    while *either* real backend could have worked. **Known caveat found
    while verifying this on real hardware, pre-existing on `SoapyBackend`
    and NOT something this backend introduced or fixes by design (it just
    happens to usually paper over the symptom as a side effect of trying
    native RTL-SDR second):** `SoapyBackend.__init__`'s own `"auto"` path
    calls `SoapySDR.Device({})` with a genuinely empty filter, which lets
    SoapySDR match *any* registered driver, including `soapysdr-module-
    audio`'s "audio" driver (see gotcha 4) - confirmed reproducing this
    directly (`SoapyBackend("auto", "", ...)` raised a PulseAudio
    `activateStream()` error instead of ever reaching the RTL-SDR) on a
    machine with both a real RTL-SDR and `soapysdr-module-audio` installed.
    Since gotcha 8 makes `create_backend()` treat any `"auto"` failure as
    "substitute `SimulatedBackend`", this means **Automatic device
    selection can silently fall back to simulation despite real, working
    hardware being present**, purely because of unrelated audio-driver
    detection ordering - `open_real_backend()`'s two-backend fallback
    happens to route around it here (native RTL-SDR doesn't have this
    "audio" driver ambiguity at all), but the underlying `SoapyBackend`
    bug is still there and would resurface on Linux with SoapySDR-only (no
    pyrtlsdr) plus `soapysdr-module-audio` installed. Not fixed as part of
    this work - flagging for whoever picks it up next: the fix is
    `SoapyBackend`'s own `"auto"` args should filter out `driver="audio"`
    the same way `enumerate_devices()` already does, not leave `{}`
    genuinely unconstrained.
    `audio_output.py`'s `sounddevice` fallback (only used when
    `HAVE_PYAUDIO` is `False` - Linux keeps PyAudio, confirmed unaffected
    by installing `sounddevice` alongside it) shares one `_next_frames()`
    helper between both backends' callbacks (`_pa_callback`/`_sd_callback`)
    - if you touch the priming/underrun logic, it lives there once, not
    twice. `io_diag_log.py`'s `_loadavg1()` also now catches
    `AttributeError` alongside `OSError` - `os.getloadavg()` doesn't exist
    on Windows at all, and this runs on the SDR acquisition thread, so it
    must never raise regardless of platform.
17. **Hardware support beyond RTL-SDR is mostly already there on Linux "for
    free" via `SoapyBackend` - it's fully generic, nothing RTL-SDR-specific
    lives in it (all the RTL-SDR-only code is confined to `RtlSdrBackend`,
    gotcha 16).** `SoapyBackend.enumerate_devices()` lists whatever
    SoapySDR itself discovers (minus the "audio" driver - gotcha 4), so
    HackRF/LimeSDR/Airspy/SDRplay/USRP(UHD)/etc. show up automatically the
    moment the matching `soapysdr-module-*` is installed, no code changes
    needed - confirmed on real hardware: the UHD module happened to already
    be installed on the machine this was verified on. The one *actual* bug
    that *was* artificially blocking other hardware: `ScannerPanel`'s
    capture-bandwidth spinner (`bandwidth_spin`) was hardcoded to
    `setRange(0.25, 3.2)` with a tooltip literally saying "e.g. 2.4 MHz for
    RTL-SDR" - a leftover that capped every other device below its own real
    capability (HackRF: 20 MHz, LimeSDR: 61.44 MHz, ...) for no reason since
    the backend itself was never the constraint. Fixed to `(0.25, 61.44)` -
    a generous, round "typical consumer/prosumer SDR range" bound (LimeSDR's
    own max sample rate), not a hardware limit this app enforces; raise it
    further if someone's real device genuinely needs more.
    **Windows/macOS remain RTL-SDR-only for the "zero extra install" path**
    (gotcha 16's `pyrtlsdr`/`pyrtlsdrlib` is RTL-SDR-specific by what the
    library does, not a design choice this app made) - SoapySDR itself has
    no pip wheel there. Researched (not implemented/verified - no non-
    RTL-SDR hardware was available to test against): conda-forge is the
    current, actively-maintained path (unlike PothosSDR, dead since 2021) -
    `soapysdr` itself publishes win-64/osx builds today, and so do several
    per-device modules (`soapysdr-module-hackrf`, `-limesdr` if/when
    published, `-airspyhf`, `-uhd`, `-bladerf`, ...) - see
    `requirements.txt`'s own note on this. This is a *separate* Python
    environment from this project's pip/venv install, not something
    `main.py`'s venv-redirect logic bridges into - documented as an
    advanced, self-serve path for Windows/macOS users with non-RTL-SDR
    hardware, deliberately not built into the app's own install flow until
    an actual user with such hardware confirms it's worth the complexity.
18. **Transcription (faster-whisper) is an in-app-installable optional
    extra, not a `requirements.txt` dependency and not a manual venv recipe
    anymore (punkt 11).** `smartscan/playback/transcription_availability.py`'s
    `is_transcription_installed()` (`importlib.util.find_spec` after
    `importlib.invalidate_caches()` - the latter matters: a package a pip
    subprocess just installed into a directory this process already has on
    `sys.path` wouldn't otherwise be seen, since path-based finders cache
    directory listings) is the *one* place everything else asks - keep it
    that way rather than letting a second ad-hoc `import faster_whisper`
    try/except grow somewhere else.
    `smartscan/playback/transcription_install.py`'s `TranscriptionInstallWorker`
    (a `QThread`, launched only from `TranscriptionPanel`) tries a plain
    `pip install` straight into whatever's running the app *first* - this
    is enough, and needs no restart at all, for Windows/macOS and any Linux
    that isn't PEP 668 "externally managed". Only on the specific
    `externally-managed-environment` error (confirmed against this
    project's own dev machine, a real PEP-668 Ubuntu) does it fall back to
    creating `smartscan/venv_paths.VENV_DIR` (`--system-site-packages`) and
    installing there instead - that fallback path is the *only* one that
    needs a restart (`TranscriptionPanel.restart_requested`, reusing
    `MainWindow._offer_restart()` - now takes an optional `body` so it isn't
    hardcoded to the language-change wording it was written for first). A
    genuine failure (network, disk, ...) at either step is reported as-is -
    never silently swallowed into "must be the PEP 668 case", since a venv
    wouldn't fix an unrelated error. If an existing `VENV_DIR` is already
    there, the worker installs into it directly without re-attempting the
    direct install it already knows fails on this machine.
    `main.py`'s own startup re-exec check (unchanged in spirit, but now
    reads `smartscan/venv_paths.py` instead of computing its own POSIX-only
    path) is what makes a later `python3 main.py` transparently pick up a
    venv the installer *just* created - verified end-to-end: launching with
    the system interpreter directly re-execs into `.venv/bin/python3`
    (confirmed via `/proc/<pid>/cmdline` after the fact, not just "didn't
    crash"). `venv_paths.py` also fixed a real cross-platform gap the old
    inline computation had: it only ever checked `bin/python3` (POSIX
    layout) - a venv created on Windows uses `Scripts\python.exe`, so the
    original check could never have found one there at all.
    `TranscriptionPanel` is a real three-state UI, not just on/off:
    not-installed (intro text + Install button + GPU checkbox + live
    install log, checkbox/backlog text hidden), installed-off,
    installed-on - `_refresh_installed_state()` is the one place that
    decides which. `PlaybackPanel.refresh_transcription_availability()`
    mirrors the same installed/not-installed split for the Odtwarzacz tab's
    own Auto-transcribe checkbox/progress label (hidden when not installed)
    and a `transcription_unavailable_label` banner (shown when not,
    pointing at Options) - the same visual pattern as the existing
    `recording_disabled_label` for auto-recording. `installed_changed`
    (emitted only when the *no-restart* branch succeeds) is what lets
    Odtwarzacz's own copy update immediately without the user needing to
    stumble back onto the Options tab to notice anything changed.
    `PlaybackPanel._launch_transcription_worker()` now also gates on
    `is_transcription_installed()` before ever constructing a
    `TranscriptionWorker` - without this, every recording would still get
    queued, fail deep inside `transcribe_recording()` with a plain
    `ImportError` on the lazy `from faster_whisper... import`, get cached
    with status `"error"`, and then be retried **forever** (see
    `TranscriptionWorker.run()`'s own "`error` isn't a durable result"
    comment - it's deliberately always retried) - wasted work on every
    single batch for as long as it stays uninstalled, not just a cosmetic
    issue. The manual per-recording "Re-transcribe" button isn't separately
    gated the same way (it doesn't loop, so the failure-mode risk doesn't
    apply) - it's just hidden by the same installed/not-installed check
    instead, for discoverability rather than correctness.
    `requirements.txt` deliberately does **not** list `faster-whisper` as a
    real dependency line - a `pip install -r requirements.txt` must not pay
    its full size/download cost by default; the version constraint
    (`>=1.2`) lives only as a comment for the installer/anyone doing this by
    hand to match.
19. **A "single" mode filename (manual recording, gotcha 13; the global
    "Single File" auto-recording option) has no frequency in it at all
    (`recorder._single_file_path()` - literally `..._session.wav`), which
    `FILENAME_REGEX`/`parse_filename()` (`smartscan/playback/config.py`/
    `organizer.py`) didn't originally account for at all.** Real user-hit
    bug: `organize()` silently skipped every such file forever (`parse_filename()`
    returning `None` for a name it can't parse) and `scan_recordings()` did
    the same, so a manual recording **worked perfectly** (a real, playable
    WAV with real audio got written to disk - confirmed on the reporting
    user's own machine: several hours' worth of orphaned `..._session.wav`
    files sitting loose in their output directory, one from the exact
    session where they reported "Record doesn't do anything") but was
    **completely invisible in Odtwarzacz** - looked exactly like "manual
    recording is broken" from the user's side, backed by zero evidence
    either way since there was nothing to click into. Compounded by
    `ScannerPanel._on_manual_record_toggled()`'s button label updating
    optimistically on click (`setText()` right there, before the engine
    confirms anything - see gotcha 13) - a real failure to start would
    *also* show "■ Stop recording" with nothing happening, so this bug and
    an actual engine-side failure would have looked identical from the UI
    alone; always check the filesystem directly (`ls` the output dir) before
    assuming which one you're looking at.
    Fixed by extending `FILENAME_REGEX` to accept the literal `session`
    token as an alternative to a numeric frequency, `parse_filename()`
    mapping that case to `models.MANUAL_FREQ_KHZ` (`0` - never a real radio
    frequency, so it can't collide with an actual channel) instead of
    returning `None`. Every frequency-label call site needed its own
    `MANUAL_FREQ_KHZ` special case (`Recording.freq_label`,
    `RecordingsTableModel._freq_display()`, `PlaybackPanel._freq_display()`
    - yes, two independent copies of that method existed already -
    `FrequencyStat.label`) so it reads "Manual recording" instead of a
    nonsensical "0.000 MHz"; also disabled `save_freq_btn` for a
    `MANUAL_FREQ_KHZ` recording specifically, since "Save this frequency to
    the Memory List" would otherwise create a bogus 0 Hz channel. A useful
    side effect, not a separate fix: `organize()` now also files these into
    the normal `YYYY-MM-DD/` date folders instead of leaving them loose at
    the root forever, same as every other recording. If you touch filename
    parsing again: `FILENAME_REGEX` is the single source of truth every
    consumer (`organizer.py`, `library.py`, `playback_panel.py`'s live
    ingest) goes through - a new naming scheme needs exactly one regex
    change, not one per consumer, which is exactly how this bug happened in
    the first place (the "single" mode naming existed before any of those
    consumers accounted for it).
    **Follow-up, same session, by explicit request ("napraw widoczność +
    dodaj prawdziwy dekoder MP3 do analizy"):** an MP3-transcoded recording
    (gotcha 14) had the exact same invisibility problem one layer further
    down - `FILENAME_REGEX` only ever matched `\.wav$`, and
    `organizer.iter_all_recordings()`/`organize()`'s loose-file scan both
    only looked for `.wav`. Fixed the same way (extend `FILENAME_REGEX` to
    `\.(?:wav|mp3)$`, both loose-file scans check both suffixes). But unlike
    the manual-recording bug above, this one was never just a filename-
    parsing gap: `audio_analysis.py`'s `analyze_wav()`/
    `compute_waveform_peaks()` are built on Python's stdlib `wave` module,
    which cannot decode mp3 at all. Fixed by decoding through a system
    `ffmpeg` (`_mp3_to_wav_bytes()`) to an **in-memory WAV** first, then
    handing that to the exact same `wave`-based parsing - not a new decoder
    library (e.g. PyAV): mp3 output already requires ffmpeg to exist as a
    system binary (gotcha 14), so this reuses that same dependency instead
    of adding a new one to the *core* (non-optional) playback path - PyAV
    only ever gets installed as part of the *optional* transcription extra
    (gotcha 18), which this module must keep working without.
    **Real bug hit and fixed while verifying this, not before**: ffmpeg's
    WAV muxer can't know the final file size in advance when writing to a
    *non-seekable* pipe (exactly what decoding to stdout for this in-memory
    path does), so it writes a sentinel placeholder for the data chunk size
    (observed: `0x7FFFFFFF`) instead of the real one. Python's `wave`
    module takes that literally as `getnframes()` - taken at face value for
    `duration_sec = n_frames / framerate` as the code originally did, a
    genuinely ~0.8-second recording came back reporting a duration of
    **178956 seconds** (~2 days). `readframes()` itself is *not* fooled -
    it's correctly bounded by the real bytes actually available in the
    stream regardless of the header's claim, so the fix is deriving
    `duration_sec` from `len(raw) // (sampwidth * n_channels)` (the actual
    data read) instead of the declared frame count - correct for a real WAV
    file too (the two only ever disagree for the piped/mp3 case), so
    nothing about the plain-WAV path needed to change. Verified end-to-end:
    a real recording transcoded to mp3 and back through this whole path
    reports the same duration/SNR as its original WAV (within lossy-codec
    rounding), `readable=True`, a real non-flat waveform - not just "didn't
    crash".
20. **Legal disclaimers (`smartscan/gui/legal_disclaimer.py`) are tracked
    per *UI language*, not once-ever - `AppConfig.legal_disclaimer`
    (`LegalDisclaimerConfig`) holds two separate lists of language codes
    (`launch_seen_languages`/`recording_seen_languages`), keyed off
    `i18n.current_language()`.** Dismissing this in English doesn't mean a
    Polish-reading user (or the same person switching languages later) ever
    actually read it - the whole point is making sure it was seen in
    whatever language is actually in effect, so a language switch means
    seeing it again, by explicit design, not a bug to "fix" by making it
    once-ever. Two independent notices, both by explicit request:
    - `maybe_show_launch_disclaimer()` - shown once per language at
      startup (`MainWindow.__init__()`'s last line), purely informational
      (a single acknowledgement button, `QMessageBox.information`) - general
      "laws about listening/recording vary by country, you're responsible
      for knowing yours" framing, doesn't block using the app.
    - `confirm_recording_disclaimer()` - a real *gate*, shown the first
      time (per language) the user is actually about to start recording
      anything, with Continue/Cancel. **All three recording-starting
      actions funnel through this one shared function and shared
      `recording_seen_languages` state** - auto-recording (both
      `MainWindow._on_scanner_auto_recording_toggled`/
      `_on_options_auto_recording_toggled`, gated identically since either
      tab's checkbox can trigger it), the manual Record button (gotcha 13),
      and raw IQ recording (gotcha 15) - not three separate disclaimers,
      since the underlying legal question ("are you allowed to record
      this") is the same regardless of which mechanism is doing the
      recording. A decline (`False` return) must revert whatever
      toggle/button the user just clicked back to its prior state
      (`set_auto_recording(False)`/`set_enabled(False)`/
      `set_manual_recording_active(False)`/`set_raw_iq_recording_active(False)`
      - each already existed as the sync method for the *other* direction,
      gotcha 13/15's cross-tab sync) *and* return early *before* actually
      calling `config.recording.enabled = True` / `engine.start_*_recording()`
      - a decline must have zero side effects, not just a cosmetically
      reverted checkbox with recording silently still happening underneath.
    Both functions take the `LegalDisclaimerConfig` object directly (not
    the whole `AppConfig`) and mutate it in place on acceptance; persisting
    the change to disk is the caller's job
    (`MainWindow._maybe_show_launch_disclaimer()`/
    `_confirm_recording_disclaimer()` both call `self._save_config()`
    immediately after) - keeps the two functions testable in isolation
    (verified: mocking just `QMessageBox`/`QMessageBox.information` inside
    `legal_disclaimer.py`'s own namespace, no GUI event loop needed to
    drive them) without needing a `MainWindow` at all. If you add a fourth
    recording-starting mechanism later: route it through
    `MainWindow._confirm_recording_disclaimer()` like the other three, not
    a new standalone check.
21. **Band presets (`smartscan/military_presets.py`) are no longer a
    hardcoded Python list - they're a bundled, human-editable JSON file
    plus a user-editable one, merged live at every point of use.** By
    explicit request, after the international-relevance pass that removed
    the two Poland-only entries (`fire_vhf`/`prm` - see git history):
    `load_builtin_presets()` reads `smartscan/presets/band_presets.json`
    (ships with the app - the 8 internationally-relevant presets), and
    `load_user_presets()`/`save_user_presets()` read/write
    `CONFIG_DIR / "band_presets.json"` (absent on a fresh install, never
    touched by an app update since it lives in the config dir, not the
    package). `get_all_presets()` = builtins + user's own, called fresh
    every time (`ScannerPanel`'s dropdown, `PresetManagerPanel`'s list) -
    **deliberately not cached in a module-level constant** the way the old
    `BAND_PRESETS` list was, for the same reason the i18n footgun elsewhere
    in this file forces a function instead of a frozen constant (different
    root cause here - live edits, not translation timing - but the exact
    same shape of bug: a cached list can't reflect a change made after it
    was built). `BandPreset` gained `to_dict()`/`from_dict()` (plain
    `dataclasses.asdict()`/reconstruction - every field is already a JSON-
    safe `str`/`int`) so a preset round-trips through a file with zero
    custom serialization code, the same principle this project already
    uses for `AppConfig` itself.
    `PresetManagerPanel` (Options tab) is a real add/edit/delete/export/
    import UI, not just a viewer - **built-in presets are shown but never
    editable/deletable there** (they come back on the next app update
    regardless, so "editing" one in place would be silently undone later -
    add a new user preset instead, even one that starts from the same
    numbers). Deleting a user preset only removes it from the dropdown -
    it deliberately does **not** touch any band a user already added to
    their own `config.json` from it (`VfoRangeConfig` copies the preset's
    fields once, at "Add preset" time, via `BandPreset.to_band()` - there's
    no ongoing link back to the preset it came from). Import never
    silently overwrites an existing preset (built-in or user's own) on an
    id collision - the imported one gets a fresh, distinct id instead (see
    `_unique_id()`), so sharing/receiving a preset pack can't clobber
    something the receiving user already had. `presets_changed` (emitted
    only on an actual on-disk change - add/edit/delete/import, not just
    opening the panel) is what lets `ScannerPanel.reload_preset_combo()`
    (a public method now, called both at construction and from
    `MainWindow` in response to this signal) refresh the Listen tab's
    dropdown immediately - verified end-to-end: adding a preset from
    Options is visible in the Listen tab's dropdown without a restart, in
    the same test run.
    The migration also fixed a real, separate i18n bug found while
    reviewing presets for international relevance: `AppConfig`'s default
    fresh-install band had its `label` hardcoded as `"Lotnictwo cywilne"`
    (Polish) directly in the `bands` field's `default_factory` - every
    fresh install, in every language, saw Polish text in its very first
    band. Fixed to plain English (`"Civil aviation (118-137 MHz)"`), not
    wrapped in `tr()`: a band's `label` is free, user-editable text shown
    as-is everywhere (`QLineEdit(entry.label)`), exactly like every other
    band's label already works - only the *default seed value itself* was
    wrong, not the display mechanism.
    **Follow-up, same session, by explicit request ("skoro ktoś zmieni
    sobie nazwę presetu to ją sobie ustawi na własne potrzeby - od wtedy
    jego preset będzie traktowany tak jak preset własny"):** a built-in
    preset can now be edited or deleted too, from the very same
    `PresetManagerPanel` - **`_BUILTIN_PRESETS_PATH` (the file that ships in
    the package) is never written to** by either operation; both are
    represented purely in the user's own file instead, and
    `get_all_presets()` is the one place that reconciles them:
    - **Edit**: the edited version is saved into the user-presets list
      *under the same id* the built-in had - a built-in whose id also
      appears in the user list is excluded from `get_all_presets()`'s
      built-in half, so the user version "shadows" it. From that point on
      it's indistinguishable from (and managed exactly like) a preset
      created from scratch, matching the request literally.
    - **Delete**: `hide_builtin_preset()` adds the id to a
      `hidden_builtin_ids` set (now part of the same user JSON file -
      `{"presets": [...], "hidden_builtin_ids": [...]}`, with
      `_load_user_data_raw()` staying backward-compatible with this
      feature's own first-cut format, a bare list with no hidden-ids
      concept) instead of touching the user-presets list at all - nothing
      to "shadow", it's just gone from `get_all_presets()`.
    `is_user_owned_preset(id)` (checks only the user-presets list, not
    hidden ids) is what `PresetManagerPanel` uses to label an item
    "(built-in)" in the list - purely informational now, not a permission
    check: **every preset's Edit/Delete buttons are enabled regardless**.
    Deleting a still-pure built-in and deleting an already-shadowed
    (edited) one take different code paths for exactly this reason (a
    shadowed one has a real user-list entry to remove; a pure built-in
    doesn't - it only gets hidden). If you touch this again: never make
    `get_all_presets()` write anything - it's a pure read/merge function,
    every mutation happens in `hide_builtin_preset()`/`save_user_presets()`/
    `save_user_data()` instead, called only from the panel's own
    button handlers.
22. **Options -> "Reset to defaults" (`MainWindow._on_reset_to_defaults()`)
    resets in-memory state *before* restarting, never after.** `_shutdown()`
    (which `_restart_app()` always calls first) unconditionally persists
    whatever's currently in `self.config`/`self.channels`/`self.blacklist`
    to disk - if this reset were done in the opposite order (restart, then
    reset), `_shutdown()`'s own save calls would silently overwrite the
    fresh defaults right back with the still-old values on the way out.
    So: `self.config = AppConfig()`, `self.channels = []`,
    `self.blacklist = []`, delete `military_presets.USER_PRESETS_PATH` if
    it exists (clears both custom presets and hidden-builtin-ids together,
    gotcha 21), *then* `self._restart_app()` - which both persists the
    now-already-reset in-memory state for real and reloads the whole app
    from those fresh files, the same restart mechanism gotcha 18/language-
    change already use for "a bunch of state changed, cleanest way to make
    every widget reflect it is to just reload". Deliberately does **not**
    touch anything under the recordings output directory (quality/
    transcription caches, favorites, the recordings themselves) - "reset to
    defaults" means settings/Memory-List/blacklist/presets, never a
    user's actual recorded library, and the confirmation dialog says so
    explicitly. A real confirmation gate (Yes/No, defaulting to No) sits in
    front of this - it's genuinely irreversible (no undo, no backup made
    first) and was verified to leave state completely untouched on a "No"
    answer, not just "asks and proceeds anyway".

23. **"Single File" auto-recording mode was removed entirely, not fixed - by
    explicit user request after a design discussion, not a silent
    refactor.** Investigated first because the user asked "co właściwie
    robi single file, jak działa i czy ma sens?" - the empirical finding
    (a real simulated test, not just reading the code) was that it never
    did what its own label promised: `_open_slot()` creates a brand-new
    `Recorder` per detected transmission regardless of mode (gotcha 12's
    per-slot isolation design), so "Single File" produced one file *per
    transmission* exactly like Split - just with no frequency in the
    filename (mislabeled "Manual recording" in Odtwarzacz, gotcha 19), no
    metadata chunk (gotcha 14's `_close_slot()` skip), and a pointless
    1200Hz/0.12s marker tone prepended to every file (dead code path,
    `begin_transmission()`'s marker-insertion branch, confirmed
    unreachable in practice since every `Recorder` only ever got exactly
    one `begin_transmission()` call). After presenting both a "build a real
    session-wide continuous-file architecture" option and a "just remove
    it, manual Record already covers one-continuous-file" option, the user
    chose removal: "No to wywalamy tryb single file całkowicie, usuń też
    całkowicie z opcji wybór."
    What was removed: `RecordingConfig.mode`/`insert_marker` fields
    entirely (see `RecordingConfig`'s own comment - manual recording's
    genuine "one continuous file" need is unrelated to this config and
    keeps working via `Recorder`'s own constructor flag, see below);
    `recorder.py`'s `_marker_tone()` function; `RecordingPanel`'s
    Single/Split `QRadioButton` pair and its `mode_row` layout entirely
    (format choice is WAV/MP3 only now); the two now-orphaned Polish
    catalog entries for the removed radio button labels
    (`smartscan/locale/pl.po`); `_close_slot()`'s dead `single_mode_path`
    branch (was transcoding a file from a mode that no longer exists).
    `Recorder.__init__` gained a real `single_file: bool = False`
    constructor parameter that replaces the old config-driven mode concept
    for the one legitimate remaining single-file use case: manual
    recording (`ScannerEngine.start_manual_recording()` passes
    `single_file=True` explicitly - see gotcha 13 - never derived from
    `config`). With `single_file=True`, `start_session()` opens the one
    file immediately and `begin_transmission()`/`end_transmission()` are
    simply never called by that caller; with the default `False` (every
    per-slot auto-recording `Recorder`), behavior is unconditionally what
    Split always did - `start_session()` opens nothing, each transmission
    gets its own file via `begin_transmission()`.
    Backward compatibility: `AppConfig.load()` pops the now-stale `"mode"`
    and `"insert_marker"` keys out of an existing user's saved
    `recording` JSON block before constructing `RecordingConfig` (same
    pattern as the pre-existing stale `"audio_format"` key from an earlier,
    never-shipped mp3 attempt) - verified against the actual real
    `~/.config/SmartScanATC/config.json` on the dev machine (which does
    contain both stale keys from before this change) that it still loads
    without crashing, silently dropping the two dead settings instead of
    erroring or losing every other recording setting in the same block.
    If you touch this again: manual recording's `single_file=True` is the
    *only* remaining reason `Recorder` supports single-file behavior at
    all - don't reintroduce a second, config-driven path to the same flag
    without re-solving the per-slot-fresh-`Recorder` problem that made the
    original version not actually work.

24. **`RecordingConfig.max_concurrent_recordings` (0 = unlimited, default) is
    a recording-only cap, checked in one place -
    `ScannerEngine._at_recording_capacity()`, gating the
    `elif best_freq is not None and best_level >= effective_trigger` branch
    in `_main_loop()`'s SCANNING state right before it would otherwise call
    `_open_slot()`.** Added by explicit request as a safety valve for
    exactly the spectral-leakage scenario gotcha 23's neighbour discussion
    surfaced: tightly-spaced channels (e.g. 12.5 kHz NFM) whose per-channel
    power windows touch edge-to-edge with no guard band (see
    `channelizer.channel_powers_from_spectrum()`) can each individually
    cross `effective_trigger` from a single strong/nearby real transmission
    leaking into its immediate neighbours - each one then opens its *own*
    slot/recording over the next few ticks (`_main_loop()` only opens one
    new slot per tick, whichever candidate is strongest - see the "no cap
    on how many can open" comment there, which is correct/intentional for
    genuinely simultaneous different transmissions and untouched by this
    feature). This setting caps the resulting fan-out without touching that
    design or the FFT/trigger math itself.
    Deliberately **only** gates recording, not scanning/live listening:
    `_at_recording_capacity()` returns `False` unconditionally whenever
    `config.recording.enabled` is `False` or the limit is `<= 0` - a capped
    scan with auto-recording *off* must keep watching/demodulating every
    channel exactly as before, since nothing is actually being written to
    disk for it to protect. When the cap *is* active, a candidate that
    can't open this tick isn't lost - it's simply re-evaluated as a
    candidate on the next tick (nothing marks it as rejected), so it opens
    automatically the moment any existing slot's LINGER expires and frees
    capacity, with no special-case bookkeeping needed. `len(self._slots)`
    is the correct thing to compare against the limit only because every
    open slot's `Recorder` really is being written to whenever
    `config.recording.enabled` is `True` (see `_open_slot()`) - if that
    ever stops being true (e.g. a future per-slot opt-out), this check
    would need to count actually-recording slots instead of all open ones.
    Manual recording (gotcha 13) and raw IQ recording (gotcha 15) are each
    their own independent single recorder, entirely outside `self._slots`
    - this cap has no effect on either, by design (the runaway-fan-out
    problem this solves is specific to auto-recording's per-slot
    architecture, gotcha 12). Verified end-to-end on `SimulatedBackend`
    (whose 3 fixed fake bursts - 121.5/118.1/124.0 MHz - only ever
    coexist in one capture window at a wide-enough sample rate, ~2.4 MHz
    isn't enough to span all three): uncapped, all 3 opened simultaneously;
    with `max_concurrent_recordings=2`, never more than 2 did.
    `RecordingPanel`'s new "Max simultaneous recordings" `QSpinBox`
    (0..50, `setSpecialValueText(tr("Unlimited"))` at 0) follows the same
    signal-loading-guard pattern as every other widget in that panel
    (gotcha 1) - wired through the existing `_push_settings()`/
    `_load_from_config()`, no new signal needed since nothing outside this
    panel needs to react live to a change in the limit.

25. **`Demodulator`'s AGC (dsp.py) tracks a smoothed envelope across calls
    now, never a single chunk's own peak in isolation - see the `_AGC_*`
    constants' docstring in dsp.py for the full story.** Real-hardware
    finding while chasing a "quality/SNR reads low even though the live
    dBFS looked strong" report: the original code re-normalized *every*
    ~30ms chunk to a fixed peak using *only that chunk's own* peak, with no
    memory between calls - any chunk whose peak happened to clear 0.35 (a
    real, common case for discriminator noise/threshold-effect clicks, not
    just genuine voice) got rescaled to the *same* loudness as a genuine
    voice chunk, destroying the actual signal-to-noise contrast the RF
    chain had delivered before `analyze_wav()` ever saw the samples -
    confirmed by running the real `analyze_wav()` function against
    synthesized "voice bursts between noisy gaps" audio processed both old
    and new ways: 15.2 dB SNR/50.6 quality (old) vs 18.0 dB SNR/60.1 quality
    (new) from *identical* underlying signal. Fixed with a real
    attack-fast/release-slow envelope follower (`Demodulator._apply_agc`,
    state in `self._agc_envelope`, cleared by `reset()`) - the same
    asymmetric-time-constant shape a real AGC/compressor uses, not a peak
    normalizer re-run from scratch every chunk. Works unchanged on WFM's
    stereo (n, 2) output too (one shared scalar gain, L/R balance untouched).
    If you touch this again: never go back to computing gain from a single
    call's own audio in isolation - that's exactly the bug this replaced.
26. **`AudioOutput`'s PortAudio callback thread (`_next_frames`, both
    `_pa_callback`/`_sd_callback` wrappers) now requests real-time
    (SCHED_FIFO) scheduling too, the same fix AGENTS.md gotcha 11 already
    applied to the SDR acquisition thread, for the same underlying reason.**
    This callback is Python code, so it needs the GIL like any other thread
    in the process - left at plain SCHED_OTHER, it can be starved by a
    nearby SCHED_FIFO thread (the acquisition thread) with no fairness
    guarantee (gotcha 11's own measurement: up to 11.7s in the worst case
    for a *different* normal-priority caller), except here a single missed
    callback is an *instant* audible glitch, a harder real-time deadline
    than a retune has. Can't be boosted at construction time like
    `AcquisitionThread` does (PortAudio, not this class, creates and owns
    this thread) - `_boost_priority_once()` requests it lazily, the first
    time the callback actually runs, and is a no-op after that (flag-
    guarded). Best-effort, same fallback as the acquisition thread's own
    version. Verified the request actually lands: inspecting
    `/proc/<pid>/task/*/comm`+`os.sched_getscheduler()` while a real stream
    was running showed the callback's own OS thread at `SCHED_FIFO` after
    its first invocation, previously always `SCHED_OTHER`.
27. **"Auto" gain mode (`DeviceConfig.gain_mode == "auto"`) no longer means
    "let the tuner's own onboard AGC hardware handle it" - it's a real
    software control loop now (`smartscan/agc.py`'s `SoftwareAgc`,
    driven by `ScannerEngine._maybe_apply_gain`/`_maybe_run_software_agc`).**
    Real-hardware finding (RTL-SDR Blog V4, real strong local FM broadcast
    stations): the tuner's own AGC (`SoapyBackend`/`RtlSdrBackend`'s old
    `set_gain("auto", ...)` → `setGainMode(True)`) does not reliably prevent
    ADC overload - measured a captured channel's own power at **+0.7 dBFS**,
    above full scale, with the hardware AGC engaged and multiple strong FM
    stations in the same wideband capture (which the tuner's own AGC only
    ever sees as one combined average level, not per-station). This also
    demonstrably worsened the "one strong signal trips several neighbour
    channels" spectral-leakage symptom (see the max_concurrent_recordings
    feature's own discussion) by making the overload itself worse: the same
    real capture that showed 7-8 simultaneously "active" slots under
    hardware AGC showed only 2-3 once overload was actually fixed.
    `SoftwareAgc.step()` is a peak-tracking controller (target -9 dBFS
    linear peak, +/-3dB dead zone, >=1s between adjustments, max 6dB/step,
    clamped to the device's own `gain_range_db`) fed one measurement per
    SCANNING tick from that tick's own already-captured IQ
    (`float(np.max(np.abs(iq)))`) - no extra hardware read. `_maybe_apply_gain`
    creates a fresh `SoftwareAgc` (seeded from the same `db` Manual mode
    would use, so toggling Auto<->Manual doesn't jump gain for no reason)
    whenever effective mode transitions *into* "auto" (globally, or via a
    band's own advanced override - `_effective_gain()` still resolves both
    the same way it always did), and discards it (`self._software_agc = None`)
    the instant effective mode leaves "auto" - a plain one-shot
    `backend.set_gain("manual", db)` handles Manual mode exactly as before,
    untouched. The device backends (`SoapyBackend`/`RtlSdrBackend`) are
    otherwise unchanged - they never receive `"auto"` from the engine
    anymore in practice, only ever explicit `"manual"` values the engine
    itself computed, so their own `setGainMode(True)` branch is effectively
    dead code from ScannerEngine's own call path now (harmless if some
    other future caller still wants literal hardware AGC - just not what
    "Auto" in this app's own UI means anymore). Verified end-to-end on real
    hardware: the exact capture that read +0.7 dBFS/measured 3 spuriously
    "active" slots under the old hardware-AGC "auto" settled to -18.3 dBFS/1
    real active slot with software AGC, gain auto-converging to 36 dB from
    the same starting config.
    If you touch the gain path again: never call `backend.set_gain("auto", ...)`
    from the engine - "auto" is purely an engine-side policy now, the
    backend only ever sees concrete numeric gain values.

28. **`_service_active_slots()` runs the *primary* slot (the one and only
    one that ever reaches the live speaker) synchronously, first, on the
    scanner's own thread - never submitted to `self._slot_pool` alongside
    everything else, unlike every non-primary (recording-only) slot.** Real
    user report, after gotchas 26/27 shipped: live audio still stuttered
    noticeably on the user's own machine despite working cleanly during
    real-hardware testing here, and the user's own diagnosis nailed the
    correlation before any code was touched - "chyba jak jest tylko jedna
    [transmisja] to jest lepiej" (seems better with just one transmission) -
    i.e. more simultaneously-active (recording) slots made *live listening*
    measurably worse, even though only the primary slot's audio ever reaches
    `AudioOutput`. Root cause: gotcha 12's original design dispatched *every*
    active slot (primary included) into the same `self._slot_pool` batch,
    awaited together - the primary slot had no priority over any other, it
    was just one more task among N, so more non-primary slots directly added
    GIL/CPU contention to the one slot whose latency is actually real-time
    critical (a live-audio glitch is instantly audible; a recording file's
    write landing a tick late never is - it has no deadline at all). Fixed
    by processing primary's own `_process_one_slot()` call directly, to
    completion, *before* any non-primary slot is even submitted to the pool
    - during primary's own demod/noise-reduction/`AudioOutput.write()` work,
    nothing else in this tick is yet competing for the GIL. Non-primary
    slots are still parallelized among themselves exactly as gotcha 12
    established, just no longer sharing a batch with primary. Trade-off:
    one tick's total wall-clock cost is now `primary_time +
    max(non-primary times run in parallel)` instead of
    `max(all times run in parallel)` - strictly more sequential, but every
    real-hardware measurement taken while chasing this (up to 8 simultaneous
    WFM slots) stayed well under the 30ms tick budget either way, and a
    non-primary slot's own timing was never real-time-critical to begin
    with. If you touch this again: never let a non-primary slot's processing
    start before primary's own call has already returned - that ordering,
    not just "primary happens to be parallelized too", is what actually
    protects the live-audio path.
    **Also see gotcha 27's own real-hardware finding for a second,
    independent contributor to the exact same "more simultaneous slots =
    worse live audio" symptom**: the pre-gotcha-27 hardware "auto" gain
    could itself inflate the slot count (spectral leakage/overload spuriously
    tripping neighbour channels - 7-8 "active" slots measured for what was
    really one strong station, vs. 2-3 real ones once the overload itself
    was fixed) - the `max_concurrent_recordings` option (added the same
    session) is a direct, user-facing lever for capping this regardless of
    root cause, and was itself found to have a real UI bug while
    troubleshooting this report - see the immediately following note.
    **`RecordingPanel`'s "Max simultaneous recordings" `QSpinBox` had two
    separate real bugs stacked on top of each other, found across two
    rounds of the same user report ("nie jestem w stanie nic tutaj
    wpisać/zmienić").** First round: `setSpecialValueText(tr("Unlimited"))`
    for 0 made the field silently reject typed digits - `QSpinBox`'s
    validator treats the special-value text as the field's literal
    displayed text, so typing a number directly (the natural first thing
    anyone tries) is rejected as an unparseable edit to the string
    "Unlimited"; only the tiny spin-arrows/scroll wheel (which bypass the
    text validator entirely) worked. Fixed by dropping
    `setSpecialValueText` - the field shows a plain "0" instead, which has
    no such trap. **Second round, same symptom persisted anyway**: the real
    remaining cause was `RecordingPanel.set_running(True)` disabling this
    spinbox for the *entire* duration of a scan, same as `enable_check`/
    `wav_radio`/`mp3_radio` - so a user testing live listening (scanner
    running, exactly when they'd want to try this limit) found *nothing*
    responded, not even the spin arrows or scroll wheel, with no visible
    explanation why. Unlike `output_format`/the enable toggle (which really
    can't safely change mid-recording), this setting has no persisted/
    opened-file state depending on it at all -
    `ScannerEngine._at_recording_capacity()` already reads
    `config.recording.max_concurrent_recordings` fresh every single tick -
    so there was never a correctness reason to lock it during a scan.
    Fixed by removing it from `set_running()`'s disable list, making it
    live-editable while scanning exactly like Trigger Level/Squelch/Gain/
    Tuning/Linger already are (`widgets.SliderSpin`). Also added a
    permanent, always-visible `(unlimited)` label next to the spinbox
    (updates live via `_update_max_concurrent_hint`, no signal needed
    beyond the existing `valueChanged`) after further feedback that a
    tooltip alone doesn't make "0 = unlimited" discoverable. If you add
    another spinbox with a sentinel value like this: don't reach for
    `setSpecialValueText` for anything a user is expected to type over
    directly, and double-check `set_running()`'s (or any similar
    mode-gated) disable list actually needs to include it - "locked while
    scanning" should be reserved for settings a live change could actually
    corrupt, not applied reflexively to every new Options control.
29. **Both real-time-priority boosts (`AcquisitionThread._boost_priority`,
    gotcha 11; `AudioOutput._boost_priority_once`, gotcha 26) now print
    their own outcome unconditionally, not just under
    `SMARTSCAN_DEBUG_LIVE_AUDIO`.** Added chasing the exact same user report
    as gotcha 28: a missing rtprio ulimit/CAP_SYS_NICE silently lands on the
    existing `except (PermissionError, OSError): pass` branch, which is
    completely indistinguishable from the outside from "the fix worked but
    didn't help" - a real, plausible gap specifically because a desktop-
    icon-launched session doesn't always apply the same PAM
    `limits.conf`/`rtprio` entry a login-shell terminal session does, so the
    exact same user account can get real SCHED_FIFO when *this developer*
    tests interactively from a terminal and silently not get it when the
    user's own everyday launch path differs. One `print(..., flush=True)`
    line either way, at the point each boost is attempted, turns this from
    "unfalsifiable without a debugger" into "grep the terminal output" - if
    you're ever debugging a live-audio/scan-speed report again, check for
    these two lines *first*, before assuming either fix is actually active
    in whatever session is misbehaving.

## Testing without touching the user's real config/recordings

There's no test suite yet. To smoke-test end to end, run the app with
`QT_QPA_PLATFORM=offscreen`, select the "Symulacja" device entry, and point
`recording.output_dir` at a scratch directory before starting — otherwise
config edits during a manual test run land in the real
`~/.config/SmartScanATC/config.json`. Verified working combos so far: real
RTL-SDR via `SoapyBackend('rtlsdr', ...)`, and full state-machine + WAV
recording cycle via `SimulatedBackend`.
