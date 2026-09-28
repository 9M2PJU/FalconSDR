"""The heart of SmartScanATC: a background thread implementing the scanner
state machine from spec section 5, tightly coupled to the recorder so audio
is only ever written to disk in SIGNAL_DETECTED / LINGER.

Wideband mode: the RTL-SDR (and most SoapySDR devices) can capture ~2.4 MHz
in one shot, far wider than a single ATC channel. Instead of retuning the
tuner to every channel in turn, we group the whole scan range/memory list
into as few capture windows as will fit, and watch every channel inside a
window simultaneously via one FFT per block (see channelizer.py). Often the
entire configured range collapses into a single window, and the tuner never
needs to move again once a scan starts.

Since a window is already watching several channels at once for detection
purposes, it can just as well *service* more than one active transmission at
once too, instead of locking onto the single strongest one and ignoring the
rest of the window until it's done - see _Slot. There's no cap on how many
can be open at once (bounded only by how many channels a window actually
groups together, which plan_windows already keeps sane) - each slot's DSP
cost (a lightweight AM/NFM demod + optional noise reduction per ~30ms block)
is cheap enough that even a handful running at once is a non-issue.
"""
from __future__ import annotations

import datetime as dt
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, Signal

from .audio_output import AudioOutput
from .audio_transcode import transcode_to_mp3
from .calibration_log import CalibrationLogger
from .edge_compensation import EdgeCompensation
from .channelizer import (
    USABLE_FRACTION, ScanWindow, channel_powers_dbfs, channel_powers_from_spectrum, extend_boundary_window,
    plan_windows, spectrum_cumsum, spectrum_snapshot,
)
from .config import AppConfig, CONFIG_DIR, RecordingConfig, VfoRangeConfig
from .dsp import Demodulator, NoiseReducer
from .i18n import tr
from .iq_recorder import IQRecorder
from .models import BlacklistEntry, Channel, Modulation, ScannerState
from .recorder import Recorder
from .sdr_backend import AcquisitionThread, create_backend, open_real_backend
from .wav_metadata import append_metadata_chunk

AUDIO_RATE = 12500
CHUNK_SECONDS = 0.03
MAX_VFO_CHANNELS = 20_000
# Reuses SMARTSCAN_DEBUG_LIVE_AUDIO (see live-audio debugging history) to
# also gate a once-a-second scanner-loop timing summary: how many SCANNING
# ticks in the last second took longer than CHUNK_SECONDS to process (demod
# + noise reduction, per active slot), the worst one, and the live-audio
# ring buffer's current fill level (see AudioOutput.buffered_seconds) - a
# genuine CPU-throughput deficit (ticks consistently over budget) looks
# different from real-time thread-priority starvation (which SCHED_FIFO,
# see _maybe_boost_priority, would have already fixed) and needs a
# different fix (less/faster work per tick, not a scheduling nudge).
_DEBUG_TIMING = os.environ.get("SMARTSCAN_DEBUG_LIVE_AUDIO") == "1"
# A single backend.read() call this slow gets its own immediate log line
# (not just folded into the once-a-second summary below) - found via
# cross-checking WAV metadata's open->close wall-clock duration against the
# actual recorded audio length on a real live session: 307/307 recent
# recordings had *some* excess wall-clock time unaccounted for by audio
# (median ~0.3s, tail up to 8s), uncorrelated with recording length
# (correlation ~0.07 - a per-tick processing deficit would instead scale
# *with* length). That signature points to rare, discrete stalls, not a
# steady-state deficit - exactly what an averaged-per-second summary is
# liable to wash out, and specifically outside what the tick-processing
# timing below ever measured: tick_start there is captured *after*
# backend.read() already returned, so the read call's own blocking time
# (USB/driver waiting for the SDR to actually have `chunk` samples ready)
# was invisible to it.
_READ_STALL_LOG_THRESHOLD_S = 0.5

_STARTUP_CHECKPOINT_PATH = CONFIG_DIR / "startup_checkpoints.log"


def _checkpoint(msg: str) -> None:
    """Always-on, best-effort breadcrumb trail through scanner startup -
    truncated fresh at the start of every run() (see ScannerEngine.run())
    so it only ever reflects the most recent attempt, not an ever-growing
    history. Exists to chase a real, still-unexplained bug: the whole
    process has been observed to vanish completely a few seconds after
    Start is clicked - no Python traceback, no fatal-signal trace even with
    faulthandler active (see main.py), no kernel OOM message, no
    systemd-oomd kill logged - reproducibly when launched from the .desktop
    file but seemingly never from a terminal. Generic system/journal logs
    were checked exhaustively (environment variables, resource limits,
    cgroup/oomd policy, kernel dmesg) without finding the mechanism. This
    narrows a future recurrence down to "got past checkpoint X but not
    X+1" from a file this app writes directly, instead of inferring from
    OS-level logs that turned out not to capture whatever this actually
    is."""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with _STARTUP_CHECKPOINT_PATH.open("a", encoding="utf-8") as f:
            f.write(f"{time.time():.3f} {msg}\n")
    except OSError:
        pass


# Reconnect attempts after the SDR drops out (flaky USB dongles, cable
# issues, etc.) retry at a constant fast interval - never longer than 2s -
# so a user can physically unplug/replug the dongle a few times in a row
# and have the next attempt land quickly, instead of waiting out a
# lengthening backoff.
RECONNECT_BACKOFF_S = [2]
# _next_window()'s blacklist-skip retry bound: generous enough to cycle past
# a handful of fully-blacklisted lanes/windows in a row without looping forever
# when everything happens to be blacklisted.
MAX_PICK_ATTEMPTS = 64
# Minimum real time between "floor" calibration snapshots of the *same*
# window (see CalibrationLogger) - it costs one extra full-window
# channel_powers_dbfs() call, cheap occasionally but not worth paying every
# tick just to get more samples than the calibration data will ever need.
_FLOOR_LOG_INTERVAL_S = 30.0


class _Lane:
    """One schedulable unit for the weighted scanner: either a whole band
    sweeping through its own windows in turn (``is_range=True``), or one
    (possibly merged) set of priority Memory List frequencies. Lanes with
    weight 0 don't get their own _Lane - they're pooled into the flat
    "default lane" instead (see ScannerEngine._rebuild_windows).
    """

    __slots__ = ("weight", "windows", "is_range", "index", "current", "just_wrapped")

    def __init__(self, weight: float, windows: list[ScanWindow], is_range: bool):
        self.weight = weight
        self.windows = windows
        self.is_range = is_range
        self.index = 0
        self.current = 0.0  # _WeightedScheduler's running credit for this lane
        self.just_wrapped = False

    def advance(self) -> ScanWindow | None:
        """Returns this lane's next window and moves its own internal
        rotation forward by one step, so consecutive turns for the same lane
        resume exactly where the previous one left off."""
        if not self.windows:
            self.just_wrapped = False
            return None
        window = self.windows[self.index]
        self.index += 1
        if self.index >= len(self.windows):
            self.index = 0
            self.just_wrapped = True
        else:
            self.just_wrapped = False
        return window


class _WeightedScheduler:
    """Smooth weighted round robin over lanes with weight > 0 (same
    algorithm nginx uses for upstream weights): a lane with twice the weight
    of another gets picked twice as often, interleaved evenly rather than in
    bursts - e.g. weights (1, 1) alternate A,B,A,B,... and (2, 1) come out
    roughly A,B,A,A,B,A,...
    """

    def __init__(self, lanes: list[_Lane]):
        self.lanes = lanes
        self.total_weight = sum(l.weight for l in lanes)

    def pick(self) -> _Lane:
        for lane in self.lanes:
            lane.current += lane.weight
        best = max(self.lanes, key=lambda l: l.current)
        best.current -= self.total_weight
        return best


def _band_grid(vr: VfoRangeConfig) -> tuple[list[int], Modulation]:
    """The full list of channel frequencies a band/range sweeps at its own
    step, plus the single modulation it demodulates all of them with."""
    step = max(1, vr.step_hz)
    lo, hi = min(vr.start_hz, vr.stop_hz), max(vr.start_hz, vr.stop_hz)
    n = max(1, (hi - lo) // step + 1)
    band_mod = Modulation(vr.modulation) if vr.modulation in Modulation._value2member_map_ else Modulation.AM
    return [lo + i * step for i in range(n)], band_mod


def _overlapping_band(freqs: list[int], bands: list[VfoRangeConfig]) -> VfoRangeConfig | None:
    """The first enabled band (weighted or not) whose range contains any of
    ``freqs`` - used to decide whether a priority Memory List frequency gets
    the "wide anchored window" treatment (rule: a weighted channel inside an
    actively-scanned band should keep covering its neighbours too)."""
    for vr in bands:
        lo, hi = min(vr.start_hz, vr.stop_hz), max(vr.start_hz, vr.stop_hz)
        if any(lo <= f <= hi for f in freqs):
            return vr
    return None


def _wide_anchor_windows(freqs: list[int], band: VfoRangeConfig, sample_rate: float) -> list[ScanWindow]:
    """Two full-width capture windows for a priority frequency (or small
    merged group of them) that lies inside ``band``: one anchoring the group
    near the left edge of the capture, one near the right edge, each filled
    out with that band's own grid so a priority "visit" doubles as an
    ordinary wide sweep of the surrounding channels (this is what lets
    detections on a neighbouring frequency still work normally, and lets the
    band's own rotation skip nothing - it just doesn't need to dwell here as
    often). Alternating which edge is anchored on successive visits sweeps
    fresh neighbouring spectrum each time instead of always re-covering the
    same span. Returns a single window if both edges end up identical (band
    narrower than one capture)."""
    capture_bw = sample_rate * USABLE_FRACTION
    step = max(1, band.step_hz)
    lo_band, hi_band = min(band.start_hz, band.stop_hz), max(band.start_hz, band.stop_hz)
    anchor_lo, anchor_hi = min(freqs), max(freqs)

    def _variant(edge: str) -> ScanWindow:
        if edge == "left":
            win_start = max(lo_band, anchor_lo - capture_bw * 0.1)
            win_end = min(hi_band, win_start + capture_bw)
            win_start = max(lo_band, win_end - capture_bw)
        else:
            win_end = min(hi_band, anchor_hi + capture_bw * 0.1)
            win_start = max(lo_band, win_end - capture_bw)
            win_end = min(hi_band, win_start + capture_bw)
        n = max(1, int((win_end - win_start) // step) + 1)
        grid = [int(win_start + i * step) for i in range(n)]
        for f in freqs:
            if f not in grid:
                grid.append(f)
        grid.sort()
        center = (grid[0] + grid[-1]) // 2
        return ScanWindow(center_hz=center, freqs=grid)

    left, right = _variant("left"), _variant("right")
    return [left] if left.freqs == right.freqs else [left, right]


class _SpectrumWorker:
    """Runs spectrum_snapshot() (an FFT over the *whole* captured IQ block -
    tens of thousands of samples, a much bigger FFT than anything in the
    demod/noise-reduction path, which only ever sees the much shorter
    per-channel *audio* output) on its own thread, decoupled from the
    scanner thread the same way AudioOutput decouples PortAudio's writes
    (see its docstring) - live audio has a real-time deadline and a slow
    spectrum frame must never be allowed to delay it. This purely feeds the
    GUI's spectrum widget from the raw IQ - it never touches demodulated
    audio or the recorder, so recording is completely unaffected by this.

    Only ever keeps the *latest* submitted job: if the worker is still busy
    when a newer one arrives, the older one is simply dropped instead of
    queueing up and falling behind - fine for a refresh-rate visual display
    (a skipped frame is imperceptible), never fine for audio.
    """

    def __init__(self, signals: "ScannerSignals"):
        self._signals = signals
        self._lock = threading.Lock()
        self._pending: tuple | None = None
        self._wake = threading.Event()
        self._stop_evt = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def submit(
        self, iq: np.ndarray, sample_rate: float, center_hz: float, trigger_dbfs: float, custom: bool = False,
    ) -> None:
        with self._lock:
            self._pending = (iq, sample_rate, center_hz, trigger_dbfs, custom)
        self._wake.set()

    def stop(self) -> None:
        self._stop_evt.set()
        self._wake.set()
        self._thread.join(timeout=1.0)

    def _run(self) -> None:
        while True:
            self._wake.wait()
            self._wake.clear()
            if self._stop_evt.is_set():
                return
            with self._lock:
                job = self._pending
                self._pending = None
            if job is None:
                continue
            iq, sample_rate, center_hz, trigger_dbfs, custom = job
            try:
                freqs, db = spectrum_snapshot(iq, sample_rate, center_hz)
                self._signals.spectrum_ready.emit(freqs, db, trigger_dbfs, custom)
            except Exception:
                pass


class _Slot:
    """One in-progress transmission being demodulated/recorded. Unlike the
    scheduling data above (shared, rebuilt wholesale by _rebuild_windows),
    each slot owns its *own* Demodulator/NoiseReducer/Recorder, created when
    it opens and discarded when it closes - so two channels active at once
    never share filter state, and there's no fixed cap on how many slots can
    exist (see module docstring). ``linger_deadline`` is None while the
    channel is genuinely above the trigger level right now; once it dips
    below, it holds the monotonic time the slot should actually close if the
    level doesn't come back up before then (mirrors the old LINGER state,
    just tracked per-slot instead of as a single shared engine state)."""

    def __init__(
        self, freq_hz: int, name: str, modulation: Modulation, offset_hz: float,
        demod: Demodulator, noise_reducer: NoiseReducer, recorder: Recorder,
    ):
        self.freq_hz = freq_hz
        self.name = name
        self.modulation = modulation
        self.offset_hz = offset_hz
        self.demod = demod
        self.noise_reducer = noise_reducer
        self.recorder = recorder
        self.linger_deadline: float | None = None
        # True while the previous tick's block was dropped by squelch (see
        # _effective_squelch_dbfs) - lets _play_and_record_slot() detect the
        # exact tick audio resumes after a squelched gap, to reset
        # noise_reducer's state right then (see its own comment there).
        self.was_squelched = False
        # Everything worth remembering about this transmission for the WAV
        # metadata chunk (see wav_metadata.py) - filled in at open time in
        # _open_slot(), topped up with the closing details in _close_slot().
        self.meta: dict = {}
        self.opened_at = time.monotonic()
        # Set by _open_slot() right after construction - which window (and
        # therefore which band's "advanced" overrides, if any) this slot was
        # opened from. See _effective_* / _apply_noise_reduction.
        self.window: ScanWindow | None = None


class ScannerSignals(QObject):
    state_changed = Signal(str)
    level_changed = Signal(float)
    # list[tuple[freq_hz, name]] - one entry per currently active channel,
    # primary (the one actually audible live) first, or a single placeholder
    # entry for the tuned window while nothing's active yet.
    freq_changed = Signal(object)
    recording_changed = Signal(bool, str)
    recording_finished = Signal(str)  # path of a just-closed per-transmission file (split mode)
    # Manual, trigger-independent continuous recording (see
    # start_manual_recording()) - (active, path). Separate from
    # recording_changed, which reflects only the automatic, per-transmission
    # recording driven by the trigger/signal-detection state machine.
    manual_recording_changed = Signal(bool, str)
    # Raw wideband IQ recording (see start_raw_iq_recording()) - (active,
    # path). A third, independent recording mechanism again: not audio at
    # all, so its finished file never goes through recording_finished (the
    # Odtwarzacz library only expects playable WAV/MP3 there).
    raw_iq_recording_changed = Signal(bool, str)
    blacklisted = Signal(int)
    # freqs_hz, db, trigger_dbfs, custom (True = this window's band has its
    # own "advanced" overrides - see ScannerPanel/SpectrumView's accent color)
    spectrum_ready = Signal(object, object, float, bool)
    # Real, *measured* seconds since this same window (by center_hz) was
    # last tuned to - not a theoretical estimate (weighted scheduling makes
    # that dishonest - different windows get visited at very different real
    # rates). Only emitted from the second visit onward (nothing to compare
    # the first time).
    cycle_time_changed = Signal(float)
    gain_range_changed = Signal(float, float)
    log = Signal(str)
    finished = Signal()


class ScannerEngine(threading.Thread):
    def __init__(self, config: AppConfig, channels: list[Channel], blacklist: list[BlacklistEntry]):
        super().__init__(daemon=True)
        self.signals = ScannerSignals()
        self.config = config
        self.channels = channels
        self.blacklist = blacklist
        # Vectorized mirror of self.blacklist - see _rebuild_blacklist_arrays().
        self._bl_freqs = np.empty(0, dtype=np.float64)
        self._bl_margins = np.empty(0, dtype=np.float64)
        self._rebuild_blacklist_arrays()

        self._stop_evt = threading.Event()
        self._skip_evt = threading.Event()          # "Skip" - move on now, frequency stays scannable
        self._skip_lockout_evt = threading.Event()   # "Skip + Blacklist" - move on now, permanently blacklist it
        self._muted_evt = threading.Event()

        self._backend = None
        self._audio_out: AudioOutput | None = None
        self._spectrum_worker: _SpectrumWorker | None = None
        self._edge_compensation: EdgeCompensation | None = None
        self._logged_checkpoints: set[str] = set()  # see _checkpoint_once()

        # Guards the scheduling state below, which _rebuild_windows() replaces
        # wholesale (from the GUI thread) while _next_window() reads/advances
        # it (from this thread) - see request_rebuild().
        self._lock = threading.Lock()
        # Set while the Scanner tab has a band open in "advanced" edit mode -
        # see set_solo_band()/ScannerPanel. Restricts _rebuild_windows() to
        # that one band alone (ignoring every other band and the whole
        # Memory List) so tuning a band's own trigger/timing/gain/noise
        # settings can be heard/seen in isolation, live, without the rest of
        # the configured scan competing for turns.
        self._solo_band: VfoRangeConfig | None = None
        self._priority_lanes: list[_Lane] = []
        self._default_lane = _Lane(weight=0.0, windows=[], is_range=False)
        self._scheduler: _WeightedScheduler | None = None
        self._macro_cycle_counter = 0.0
        self._default_due = False
        self._channel_bw_map: dict[int, float] = {}
        self._channel_name_map: dict[int, str] = {}
        self._channel_name_by_khz: dict[int, str] = {}  # fallback lookup - see _rebuild_windows
        self._channel_mod_map: dict[int, Modulation] = {}
        self._channel_weight_map: dict[int, float] = {}

        self._cur_window: ScanWindow | None = None
        # Keyed by window center_hz - real monotonic() timestamp of the last
        # time we tuned to this window, for cycle_time_changed - see
        # _pick_next_locked's caller in _main_loop.
        self._window_last_visit: dict[int, float] = {}
        # Last center_hz actually pushed to the SDR - lets _main_loop skip a
        # pointless retune (and the TUNING settle wait that goes with it)
        # when the round-robin picks the exact same window it's already
        # parked on, e.g. a narrow band that fits entirely in one window
        # (nothing else to rotate to) or being "solo"'d via the Scanner
        # tab's "advanced" edit mode - see _main_loop's CALCULATE_NEXT_FREQ.
        self._last_tuned_center_hz: float | None = None
        # Keyed by freq_hz - naturally unique among *active* transmissions
        # (a frequency can only be in one slot at a time), no fixed size.
        self._slots: dict[int, _Slot] = {}
        self._primary_freq: int | None = None  # which slot's audio is on the live speaker
        self._primary_weight = -1.0

        self._applied_gain_mode = ""
        self._applied_gain_db = None
        self._had_real_hardware = False

        # Manual, trigger-independent continuous recording - see
        # start_manual_recording()/stop_manual_recording(). None while
        # inactive. Guarded by its own lock (not self._lock, which is about
        # scheduling state) since it's written from the GUI thread
        # (start/stop) and read/written from both the main loop thread
        # (idle-tick silence) and slot-processing worker threads (mirroring
        # the primary slot's audio) - see _feed_manual_recording().
        self._manual_recorder: Recorder | None = None
        self._manual_recorder_lock = threading.Lock()
        # First real (non-idle-silence) frequency captured during the
        # current manual recording, if any - "first-wins" latch, reset only
        # when a new manual recording starts. Used to rename the file from
        # its generic "..._session.wav" name to the same "..._<freq_khz>.wav"
        # naming auto-recordings use, once we actually know what it was -
        # see start_manual_recording()/stop_manual_recording().
        self._manual_recording_first_freq: int | None = None

        # Raw wideband IQ recording (see start_raw_iq_recording()) - only
        # ever touched by the GUI thread (start/stop) and the main loop
        # thread (feeding it each SCANNING-state read), never the slot pool
        # (it captures the whole window, not any one demodulated channel) -
        # but still locked for the same GUI-thread-vs-engine-thread safety
        # as the manual audio recorder above.
        self._iq_recorder: IQRecorder | None = None
        self._iq_recorder_lock = threading.Lock()

        # Demod/noise-reduction/file-write for every simultaneously active
        # slot runs on this pool instead of one after another on this
        # thread - see _service_active_slots/AGENTS.md gotcha 12. Sized
        # generously (Python's own ThreadPoolExecutor default heuristic,
        # cpu_count()+4 capped at 32) since idle worker threads cost
        # nothing and the whole point is to never be the bottleneck itself;
        # measured 3-4.6x real wall-clock speedup for this workload
        # (resample_poly/FFT-heavy - releases the GIL enough to matter).
        self._slot_pool = ThreadPoolExecutor(
            max_workers=min(32, (os.cpu_count() or 4) + 4), thread_name_prefix="slot-demod",
        )

        self._calibration_log = CalibrationLogger()
        # Keyed by window center_hz - throttles "floor" snapshots, see
        # _FLOOR_LOG_INTERVAL_S.
        self._last_floor_log: dict[int, float] = {}
        # Keyed by window center_hz - see _register_recency_boost /
        # _due_recency_boost_locked. Only ever touched from this thread (both
        # registration in _close_slot and reads in _pick_next_locked happen
        # here), so unlike _priority_lanes/_scheduler it doesn't need
        # self._lock for correctness - it's just held under it incidentally
        # since the read site is inside an already-locked method.
        self._recency_boosts: dict[int, dict] = {}

        # SMARTSCAN_DEBUG_LIVE_AUDIO-gated timing stats - see _DEBUG_TIMING.
        self._tick_n = 0
        self._tick_over_budget = 0
        self._tick_max_s = 0.0
        self._tick_report_at = 0.0
        # backend.read()'s own blocking time, separate from tick processing
        # time above - see _READ_STALL_LOG_THRESHOLD_S.
        self._read_n = 0
        self._read_over_budget = 0
        self._read_max_s = 0.0

        # Last state actually handed to signals.state_changed - see _emit_state().
        self._last_emitted_state: ScannerState | None = None

    # -- public control (safe to call from the GUI thread) -----------
    def request_stop(self) -> None:
        self._stop_evt.set()

    def request_skip(self) -> None:
        """Abandon the current channel(s)/transmission(s) and move on
        immediately, without touching the blacklist - for when something on
        one of these frequencies is worth leaving alone for a future pass,
        you just don't want to wait it out right now."""
        self._skip_evt.set()

    def request_skip_and_blacklist(self) -> None:
        """Same as request_skip(), but also permanently blacklists every
        frequency currently active. Deliberately no confirmation dialog -
        this needs to be fast, and undoing it is just a Delete in the
        Blacklist tab."""
        self._skip_lockout_evt.set()

    def set_live_muted(self, muted: bool) -> None:
        """Mute/unmute the live monitoring output only - recording to disk
        (when enabled) keeps running regardless, so a muted session can
        still be replayed later."""
        if muted:
            self._muted_evt.set()
        else:
            self._muted_evt.clear()

    # -- manual, trigger-independent continuous recording -----------------
    # A completely separate mechanism from the automatic per-transmission
    # recording above (config.recording.enabled + each _Slot's own
    # Recorder): this one is user-initiated, records continuously for as
    # long as it's on (silence included), and is fed from two places -
    # _play_and_record_slot() (mirrors the primary slot's actual audio,
    # squelched blocks included as silence, independent of mute) and
    # _main_loop()'s SCANNING branch (feeds silence on a tick where nothing
    # was active at all) - see _feed_manual_recording(). Always writes one
    # continuous file (forced "single" mode, regardless of the global
    # Single/Split setting, which only applies to the automatic recording -
    # "continuous" wouldn't mean anything split across files), and never
    # trims a trailing tail the way squelch-aware auto-recording does
    # (Recorder.write_audio(..., real=True) unconditionally) - the whole
    # point of this mode is to capture literally everything that elapsed
    # while it was on, not to make an editorial judgment about what's worth
    # keeping.
    def start_manual_recording(self) -> None:
        with self._manual_recorder_lock:
            if self._manual_recorder is not None:
                return
            manual_config = RecordingConfig(enabled=True, output_dir=self.config.recording.output_dir)
            recorder = Recorder(manual_config, AUDIO_RATE, single_file=True)
            recorder.start_session()
            self._manual_recorder = recorder
            self._manual_recording_first_freq = None
        self.signals.manual_recording_changed.emit(True, recorder.current_path)

    def stop_manual_recording(self) -> None:
        with self._manual_recorder_lock:
            recorder = self._manual_recorder
            if recorder is None:
                return
            self._manual_recorder = None
            first_freq = self._manual_recording_first_freq
            self._manual_recording_first_freq = None
        path = recorder.current_path
        recorder.stop_session()
        if path and first_freq is not None:
            renamed = Recorder.rename_session_file_with_frequency(Path(path), first_freq)
            if renamed is not None:
                path = str(renamed)
        if path and self.config.recording.output_format == "mp3":
            mp3_path = transcode_to_mp3(Path(path))
            if mp3_path is not None:
                path = str(mp3_path)
        self.signals.manual_recording_changed.emit(False, "")
        if path:
            self.signals.recording_finished.emit(path)

    def is_manual_recording(self) -> bool:
        with self._manual_recorder_lock:
            return self._manual_recorder is not None

    def _feed_manual_recording(self, audio: np.ndarray, freq_hz: int | None = None) -> None:
        """Mirrors ``audio`` (AUDIO_RATE-domain, mono) into an in-progress
        manual recording, if any - a cheap no-op (one lock check) otherwise,
        safe to call unconditionally from every tick. ``freq_hz`` (only ever
        passed by _play_and_record_slot()'s real-audio path, never the idle-
        silence tick) records the *first* one seen this recording - see
        stop_manual_recording(), which renames the file to it once known."""
        with self._manual_recorder_lock:
            recorder = self._manual_recorder
            if recorder is not None and freq_hz is not None and self._manual_recording_first_freq is None:
                self._manual_recording_first_freq = freq_hz
        if recorder is not None:
            recorder.write_audio(audio, real=True)

    # -- raw wideband IQ recording (technical) -----------------------------
    # See iq_recorder.py's module docstring for the format (SigMF) and why
    # this is a third, independent recording mechanism from both the
    # per-transmission auto-recording above and manual audio recording. Fed
    # only from _main_loop()'s SCANNING-state read (never TUNING, which is
    # deliberately muted garbage while the tuner settles).
    def start_raw_iq_recording(self) -> None:
        with self._iq_recorder_lock:
            if self._iq_recorder is not None:
                return
            recorder = IQRecorder(self.config.recording.output_dir, self.config.device.sample_rate_hz)
            self._iq_recorder = recorder
        self.signals.raw_iq_recording_changed.emit(True, recorder.current_path)

    def stop_raw_iq_recording(self) -> None:
        with self._iq_recorder_lock:
            recorder = self._iq_recorder
            if recorder is None:
                return
            self._iq_recorder = None
        recorder.close()
        self.signals.raw_iq_recording_changed.emit(False, "")

    def is_recording_raw_iq(self) -> bool:
        with self._iq_recorder_lock:
            return self._iq_recorder is not None

    def _feed_raw_iq(self, iq: np.ndarray, center_freq_hz: float) -> None:
        with self._iq_recorder_lock:
            recorder = self._iq_recorder
        if recorder is not None:
            recorder.write(iq, center_freq_hz)

    def update_channels(self, channels: list[Channel]) -> None:
        self.channels = channels
        self._rebuild_windows()

    def update_blacklist(self, blacklist: list[BlacklistEntry]) -> None:
        self.blacklist = blacklist
        self._rebuild_blacklist_arrays()

    def _rebuild_blacklist_arrays(self) -> None:
        """Vectorized mirror of self.blacklist, rebuilt only here and in
        __init__ - i.e. only when the blacklist itself actually changes,
        never per-tick. See _blacklist_mask() for why this exists: checking
        every candidate frequency against every BlacklistEntry with a
        Python-level any()-over-a-generator call (one Python function call +
        one dataclass attribute access per comparison) is fine for the
        occasional single-frequency check (_next_window(), which short-
        circuits on the first non-blacklisted frequency anyway - most
        windows have far more clean channels than blacklisted ones) but was
        being paid in full, with no short-circuit, for *every* frequency in
        the window on *every* SCANNING tick (~30ms) via the candidate_freqs
        list comprehension in _main_loop() - O(freqs x blacklist) Python-
        level work, repeatedly, for as long as a window has any non-active
        channel left to check (i.e. most of the time). A blacklist can grow
        to dozens/hundreds of entries over a long session (every "Skip +
        Blacklist" adds one), so that cost only ever grows the longer the
        program keeps running - exactly backwards from "shouldn't slow down
        the longer it runs". Doing the same comparisons as one vectorized
        numpy broadcast instead keeps the same O(freqs x blacklist) count of
        individual comparisons but runs them at C speed with no per-
        comparison Python call/attribute-access overhead.
        """
        if self.blacklist:
            self._bl_freqs = np.array([e.freq_hz for e in self.blacklist], dtype=np.float64)
            self._bl_margins = np.array([e.margin_hz for e in self.blacklist], dtype=np.float64)
        else:
            self._bl_freqs = np.empty(0, dtype=np.float64)
            self._bl_margins = np.empty(0, dtype=np.float64)

    def _blacklist_mask(self, freqs: list[int]) -> np.ndarray:
        """Vectorized equivalent of [self._is_blacklisted(f) for f in freqs] -
        see _rebuild_blacklist_arrays() for why this exists alongside (not
        instead of) _is_blacklisted(), which remains the right tool for a
        single-frequency, short-circuiting check."""
        if not freqs or self._bl_freqs.size == 0:
            return np.zeros(len(freqs), dtype=bool)
        arr = np.asarray(freqs, dtype=np.float64)[:, None]
        return np.any(np.abs(arr - self._bl_freqs[None, :]) <= self._bl_margins[None, :], axis=1)

    def request_rebuild(self) -> None:
        """Called from the GUI after any change that affects what gets
        scanned - bands, channels, weights, the Memory List master toggle,
        step/modulation edits, ... - so it takes effect immediately on a
        running scan, without needing to Stop/Start."""
        self._rebuild_windows()

    def set_solo_band(self, band: VfoRangeConfig | None) -> None:
        """Restricts scanning to just this one band (None = back to normal,
        every configured band + Memory List) - see self._solo_band and
        ScannerPanel's advanced per-band edit mode. Safe to call before the
        engine thread has even started (e.g. right after construction, to
        pick up an edit already in progress when Start is pressed)."""
        self._solo_band = band
        self._rebuild_windows()

    # -- thread body ---------------------------------------------------
    def _maybe_boost_priority(self) -> None:
        """Best-effort: ask the OS to schedule this thread with real-time
        priority (SCHED_FIFO), the same technique real-time audio servers
        (JACK, PipeWire's own RT thread) use, so it gets CPU time promptly
        and predictably instead of waiting its turn behind whatever else the
        desktop happens to be doing right now - directly attacking the
        "momentarily too slow to keep up with real time" root cause behind
        live-audio underruns (see audio_output.py). Linux-only, and needs
        either root or a system-granted rtprio allowance (commonly already
        set up for the "audio" group on desktop distros via
        /etc/security/limits.d - PipeWire itself relies on the same thing).
        Silently does nothing if neither is available: this is purely a
        "nice to have if the system allows it" improvement, deliberately
        never a hard requirement - scanning and recording work identically
        either way, only live-audio smoothness benefits."""
        if not hasattr(os, "sched_setscheduler"):
            return  # not Linux
        try:
            tid = threading.get_native_id()
            os.sched_setscheduler(tid, os.SCHED_FIFO, os.sched_param(10))
        except (PermissionError, OSError):
            pass

    def _checkpoint_once(self, msg: str) -> None:
        """Like _checkpoint(), but only the first time this exact message is
        reached per run() - for state-machine transitions inside the
        per-tick loop that would otherwise write on every single tick."""
        if msg not in self._logged_checkpoints:
            self._logged_checkpoints.add(msg)
            _checkpoint(msg)

    def run(self) -> None:
        self._logged_checkpoints.clear()
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            _STARTUP_CHECKPOINT_PATH.write_text("")  # fresh trail for this attempt - see _checkpoint()
        except OSError:
            pass
        _checkpoint("run() start")
        self._maybe_boost_priority()
        _checkpoint("priority boost attempted")
        self._setup_static()
        _checkpoint("_setup_static() done")
        connected = self._open_backend_with_retry(allow_simulated_fallback=True)
        _checkpoint(f"initial backend open done, connected={connected}")
        self._had_real_hardware = connected and self._backend.label != "simulated"

        while connected and not self._stop_evt.is_set():
            try:
                self._main_loop()
            except Exception as exc:
                self.signals.log.emit(tr("SDR connection lost: {error}").format(error=exc))
                for freq_hz in list(self._slots.keys()):
                    self._close_slot(freq_hz, reason="connection_lost")
                if self._backend is not None:
                    self._backend.close()
                    self._backend = None
                # Once real hardware has been proven present, never silently
                # drop back to fake data on reconnect - keep insisting on the
                # real device until it's actually back (or Stop is pressed).
                connected = self._open_backend_with_retry(allow_simulated_fallback=not self._had_real_hardware)
                if connected and self._backend.label != "simulated":
                    self._had_real_hardware = True

        # Covers Stop being pressed while we were still (re)connecting, where
        # _main_loop() (and its own IDLE emission) never ran at all - without
        # this the GUI could get stuck showing RECONNECTING forever.
        self._emit_state(ScannerState.IDLE)
        self._teardown()
        self.signals.finished.emit()

    def _setup_static(self) -> None:
        """Everything that doesn't depend on the SDR hardware being present."""
        dev = self.config.device
        self._audio_out = AudioOutput(AUDIO_RATE, dev.audio_output_device)
        _checkpoint(f"AudioOutput created, available={self._audio_out.available}")
        if not self._audio_out.available:
            self.signals.log.emit(tr("No audio output (PyAudio) - live monitoring disabled."))
        self._spectrum_worker = _SpectrumWorker(self.signals)
        _checkpoint("_SpectrumWorker thread started")
        # Read once per engine start (not re-read mid-session) - roll-off is
        # a stable physical property of the tuner, so there's no need to
        # keep re-loading the (potentially large) calibration log while
        # scanning. See edge_compensation.py.
        self._edge_compensation = EdgeCompensation()
        _checkpoint(f"EdgeCompensation loaded, buckets={self._edge_compensation.bucket_count}")
        if self._edge_compensation.has_data:
            self.signals.log.emit(
                tr("Edge roll-off compensation: active ({count} data buckets from listening history).").format(
                    count=self._edge_compensation.bucket_count,
                )
            )
        self._rebuild_windows()
        _checkpoint("_rebuild_windows() done")

    def _open_backend(self, allow_simulated_fallback: bool) -> None:
        dev = self.config.device
        _checkpoint(f"_open_backend: constructing (driver={dev.driver!r})")
        if allow_simulated_fallback or dev.driver == "simulated":
            raw_backend = create_backend(dev.driver, dev.device_args, dev.sample_rate_hz, dev.gain_mode, dev.gain_db)
        else:
            # Bypass create_backend()'s friendly auto-simulate fallback: this
            # is a reconnect after real hardware was already proven present,
            # so a failure here must keep raising, never quietly substitute
            # fake data (that's exactly the bug that confused a user before).
            raw_backend = open_real_backend(dev.driver, dev.device_args, dev.sample_rate_hz, dev.gain_mode, dev.gain_db)
        # Wrapped in a dedicated acquisition thread (see AcquisitionThread's
        # docstring) so nothing this loop does between two backend.read()
        # calls - demod/noise-reduction/file I/O for however many slots are
        # simultaneously active, or CPU contention from elsewhere - can ever
        # delay the *actual* hardware read and risk the driver silently
        # dropping/splicing samples (measured: this was happening for real,
        # unrelated to squelch - see its docstring). Every other use of
        # self._backend below is unaffected - AcquisitionThread proxies
        # label/gain_range_db/stream_mtu/set_frequency/set_gain/read/close
        # with the same contract a raw SdrBackend has.
        self._backend = AcquisitionThread(raw_backend, self._chunk_size())
        _checkpoint(f"_open_backend: constructed (label={self._backend.label})")
        self._applied_gain_mode, self._applied_gain_db = dev.gain_mode, dev.gain_db
        self.signals.gain_range_changed.emit(*self._backend.gain_range_db)
        if self._backend.label == "simulated" and dev.driver != "simulated":
            self.signals.log.emit(tr("SoapySDR not found/configured - running in SIMULATION mode."))
        # Direct readout of what the driver actually hands back on one
        # readStream() call (see SoapyBackend.__init__ and its comment on
        # bufflen) - instead of continuing to guess from statistics alone,
        # this number states outright whether one tick's worth (chunk_size
        # samples) fits in a single call, or whether the driver hands back
        # less regardless of how big a buffer we give it.
        mtu = getattr(self._backend, "stream_mtu", None)
        if mtu is not None:
            chunk = self._chunk_size()
            fits = tr("fits in one readStream() call") if mtu >= chunk else tr("STILL needs multiple calls per tick")
            self.signals.log.emit(
                tr("SDR stream MTU: {mtu} samples/call (tick = {chunk} samples - {fits}).").format(
                    mtu=mtu, chunk=chunk, fits=fits,
                )
            )

    def _open_backend_with_retry(self, allow_simulated_fallback: bool) -> bool:
        """Keep (re)trying to open the SDR with escalating backoff until it
        succeeds or the user presses Stop. Returns False only on Stop."""
        attempt = 0
        while True:
            try:
                self._open_backend(allow_simulated_fallback)
                if attempt > 0:
                    self.signals.log.emit(tr("Reconnected to the SDR - resuming scanning."))
                return True
            except Exception as exc:
                if self._stop_evt.is_set():
                    return False
                attempt += 1
                backoff = RECONNECT_BACKOFF_S[min(attempt - 1, len(RECONNECT_BACKOFF_S) - 1)]
                self.signals.log.emit(
                    tr("Failed to connect to the SDR ({error}) - retrying in {backoff}s (attempt {attempt})...").format(
                        error=exc, backoff=backoff, attempt=attempt,
                    )
                )
                self._emit_state(ScannerState.RECONNECTING)
                deadline = time.monotonic() + backoff
                while time.monotonic() < deadline:
                    if self._stop_evt.is_set():
                        return False
                    time.sleep(0.1)

    def _teardown(self) -> None:
        # Neither the manual audio recording nor the raw IQ recording makes
        # sense outliving the engine that feeds them - stop both here so a
        # Stop click always finalizes whatever's still open instead of
        # leaving a file handle (or, for the manual recording, the WAV
        # header) dangling. The manual recording's file also still needs to
        # reach the Odtwarzacz library via recording_finished (see
        # stop_manual_recording()); raw IQ never does (see
        # ScannerSignals.raw_iq_recording_changed).
        self.stop_manual_recording()
        self.stop_raw_iq_recording()
        for freq_hz in list(self._slots.keys()):
            self._close_slot(freq_hz, reason="stopped")
        if self._audio_out is not None:
            self._audio_out.close()
        if self._spectrum_worker is not None:
            self._spectrum_worker.stop()
        if self._backend is not None:
            self._backend.close()
        # wait=True: every slot was already closed above (nothing left
        # queued), this just makes sure no worker thread is still mid-write
        # to a WAV file this same process might touch again right after
        # (e.g. a quick Stop/Start) before actually returning.
        self._slot_pool.shutdown(wait=True)

    def _rebuild_windows(self) -> None:
        """Rebuilds everything the weighted scanner needs to pick its next
        window.

        Bands and Memory List channels with weight 0 (the default) are
        pooled into one flat "default lane", scanned in the usual
        round-robin order exactly like before weights existed. Anything with
        weight > 0 becomes its own _Lane - either a "range lane" (a band,
        sweeping through its own windows turn by turn) or a "point lane" (one
        or more nearby weighted Memory List frequencies captured together in
        one window). Lanes compete via _WeightedScheduler, proportionally to
        their weight, so e.g. a band and a channel both weighted 1 alternate
        turns 50/50 regardless of how many windows the band itself has.

        The default lane gets a turn whenever a range-lane completes a full
        lap over its own windows (or, if priority lanes exist but none of
        them are range-lanes, once every full weighted-scheduler cycle) -
        that's what makes weight-0 items "wait their turn" and then get
        visited promptly once the weighted stuff has had a full sweep.
        """
        sample_rate = self.config.device.sample_rate_hz
        mem_bw = float(self.config.memory_channel_bandwidth_hz)

        if self._solo_band is not None:
            # "Advanced" per-band edit mode (see set_solo_band) - everything
            # else (other bands, the whole Memory List) sits out until this
            # ends, so the band being tuned is the only thing competing for
            # airtime while its own settings are being dialled in live.
            channels = []
            enabled_channels: list[Channel] = []
            weighted_channels: list[Channel] = []
            plain_channels: list[Channel] = []
            enabled_bands = [self._solo_band] if self._solo_band.enabled else []
        else:
            channels = list(self.channels) if self.config.memory_list_enabled else []
            enabled_channels = [c for c in channels if c.enabled]
            weighted_channels = [c for c in enabled_channels if c.weight > 0]
            plain_channels = [c for c in enabled_channels if c.weight <= 0]
            enabled_bands = [b for b in self.config.bands if b.enabled]

        weighted_bands = [b for b in enabled_bands if b.weight > 0]
        plain_bands = [b for b in enabled_bands if b.weight <= 0]

        bw_map: dict[int, float] = {}
        name_map: dict[int, str] = {}
        mod_map: dict[int, Modulation] = {}
        # Only used to decide which simultaneously-active channel gets the
        # live speaker when more than one is on at once - see _open_slot().
        weight_map: dict[int, float] = {}

        def _claim(freq: int, bw: float, mod: Modulation, name: str = "", weight: float = 0.0) -> None:
            bw_map[freq] = bw
            mod_map[freq] = mod
            weight_map[freq] = weight
            if name:
                name_map[freq] = name

        def _extend_edges(windows: list[ScanWindow], step_hz: float) -> None:
            """Widens a lane's own lowest- and highest-frequency window with
            a few opportunistic probe frequencies just past its true edge -
            "wyjdz na poprzednie/nastepne [okno]" - so a signal sitting right
            at the boundary of a configured band/range is more likely to get
            caught instead of silently falling in the dead zone the 90%
            USABLE_FRACTION margin already leaves unwatched today. See
            channelizer.extend_boundary_window() for why this never needs an
            extra retune or changes the window's own schedule identity
            (center_hz, and therefore _window_last_visit bookkeeping, is
            untouched - this only ever widens what a window watches).
            New probe frequencies inherit the modulation/weight of whichever
            real channel currently sits at that edge."""
            if not windows:
                return
            lowest = min(windows, key=lambda w: min(w.freqs))
            highest = max(windows, key=lambda w: max(w.freqs))
            for window, direction in ((lowest, "below"), (highest, "above")):
                edge_freq = min(window.freqs) if direction == "below" else max(window.freqs)
                mod = mod_map.get(edge_freq, Modulation.AM)
                weight = weight_map.get(edge_freq, 0.0)
                added = False
                for f in extend_boundary_window(window, sample_rate, step_hz, direction):
                    if f not in bw_map:
                        _claim(f, step_hz, mod, weight=weight)
                        window.freqs.append(f)
                        added = True
                if added:
                    window.freqs.sort()

        priority_lanes: list[_Lane] = []

        # -- priority point-lanes: weighted Memory List channels, merged
        # whenever nearby ones can be captured together in a single retune
        # (decision: shared visits benefit both, so there's no downside to
        # combining them; the combined lane's weight is the higher one). ---
        if weighted_channels:
            weight_by_freq = {c.freq_hz: c.weight for c in weighted_channels}
            mod_by_freq = {c.freq_hz: c.modulation for c in weighted_channels}
            name_by_freq = {c.freq_hz: c.name for c in weighted_channels}
            groups = plan_windows([c.freq_hz for c in weighted_channels], sample_rate, mem_bw)
            for group in groups:
                lane_weight = max(weight_by_freq[f] for f in group.freqs)
                owning_band = _overlapping_band(group.freqs, enabled_bands)
                if owning_band is not None:
                    # Inside an actively-scanned band: go wide so a priority
                    # visit doubles as free coverage of the neighbours too
                    # (still a scanner - detections nearby still work).
                    windows = _wide_anchor_windows(group.freqs, owning_band, sample_rate)
                    band_mod = (Modulation(owning_band.modulation)
                                if owning_band.modulation in Modulation._value2member_map_ else Modulation.AM)
                    for w in windows:
                        for f in w.freqs:
                            if f in mod_by_freq:
                                _claim(f, mem_bw, mod_by_freq[f], name_by_freq.get(f, ""), weight_by_freq[f])
                            elif f not in bw_map:
                                _claim(f, float(max(1, owning_band.step_hz)), band_mod, weight=owning_band.weight)
                else:
                    windows = [group]
                    for f in group.freqs:
                        _claim(f, mem_bw, mod_by_freq.get(f, Modulation.AM), name_by_freq.get(f, ""),
                               weight_by_freq.get(f, 0.0))
                priority_lanes.append(_Lane(weight=lane_weight, windows=windows, is_range=False))

        # -- priority range-lanes: weighted bands, each sweeping its own
        # grid in its own internal rotation across turns. -------------------
        for vr in weighted_bands:
            freqs, band_mod = _band_grid(vr)
            if len(freqs) > MAX_VFO_CHANNELS:
                keep_every = -(-len(freqs) // MAX_VFO_CHANNELS)
                freqs = freqs[::keep_every]
                self.signals.log.emit(
                    tr("Too many channels in band \"{label}\" ({count}) - thinned every {every}.").format(
                        label=vr.label, count=len(freqs) * keep_every, every=keep_every,
                    )
                )
            for f in freqs:
                if f not in bw_map:
                    _claim(f, float(max(1, vr.step_hz)), band_mod, weight=vr.weight)
            windows = plan_windows(freqs, sample_rate, max(1, vr.step_hz))
            _extend_edges(windows, float(max(1, vr.step_hz)))
            for w in windows:
                w.band = vr  # unambiguous: this whole lane is one band's own grid
            priority_lanes.append(_Lane(weight=vr.weight, windows=windows, is_range=True))

        # -- default lane: everything with weight 0, pooled together exactly
        # like the original (pre-weights) implementation. -------------------
        default_freqs: list[int] = []
        # Which single plain band (if any) each pooled frequency came from -
        # a window built from this pool only gets tagged with a band (see
        # below) if *every* one of its frequencies traces back to that same
        # band; a window straddling two bands' channels (only possible if
        # their ranges are close enough to share one capture) stays
        # band=None; safe default (global settings), rather than guessing
        # which of the two "advanced" overrides should apply.
        freq_to_band: dict[int, VfoRangeConfig] = {}
        for c in plain_channels:
            if c.freq_hz not in bw_map:
                _claim(c.freq_hz, mem_bw, c.modulation, c.name, c.weight)
            default_freqs.append(c.freq_hz)
        for vr in plain_bands:
            freqs, band_mod = _band_grid(vr)
            if len(freqs) > MAX_VFO_CHANNELS:
                keep_every = -(-len(freqs) // MAX_VFO_CHANNELS)
                freqs = freqs[::keep_every]
                self.signals.log.emit(
                    tr("Too many channels in band \"{label}\" ({count}) - thinned every {every}.").format(
                        label=vr.label, count=len(freqs) * keep_every, every=keep_every,
                    )
                )
            for f in freqs:
                if f not in bw_map:
                    _claim(f, float(max(1, vr.step_hz)), band_mod, weight=vr.weight)
                default_freqs.append(f)
                freq_to_band[f] = vr

        default_grouping_bw = max((bw_map[f] for f in default_freqs), default=25_000.0)
        default_windows = plan_windows(default_freqs, sample_rate, default_grouping_bw)
        _extend_edges(default_windows, default_grouping_bw)
        for w in default_windows:
            # VfoRangeConfig is a plain (mutable, unhashable) dataclass, so
            # identity-compare (`is`) rather than building a set of them.
            owners = [freq_to_band[f] for f in w.freqs if f in freq_to_band]
            if owners and all(b is owners[0] for b in owners):
                w.band = owners[0]

        # Display-only name lookup for _open_slot(), built from *every* named
        # channel regardless of its own enabled state, and matched by
        # rounded kHz rather than exact Hz. Two reasons this has to be
        # separate from bw_map/mod_map/weight_map above (which correctly do
        # respect .enabled - a disabled channel must never affect what gets
        # scanned or how):
        #  1. A disabled channel can still get incidentally caught by a wide
        #     band sweep it happens to fall inside - Odtwarzacz shows that
        #     channel's name for its recordings regardless of whether it's
        #     currently enabled (name lookup there has no notion of
        #     "enabled" at all - see PlaybackPanel.set_known_frequencies),
        #     so live display should be consistent with that instead of
        #     showing nothing just because the channel happens to be toggled
        #     off right now.
        #  2. A Memory List channel's exact freq_hz rarely lands precisely
        #     on a wide band's own step grid (e.g. an 8.33 kHz-spaced band
        #     and a channel someone typed in as "121.500" won't generally
        #     coincide to the exact Hz) - so a named channel and an unnamed
        #     nearby band-grid point can end up as two distinct candidate
        #     frequencies watching the same real transmission, and whichever
        #     one wins the (near-tie) power comparison decides whether a
        #     name shows up live if matched by exact Hz. Odtwarzacz never
        #     has this problem because it matches by rounded kHz (same
        #     rounding recording filenames already use) - this mirrors that
        #     same tolerance.
        name_by_khz: dict[int, str] = {}
        for c in self.channels:
            if c.name:
                name_by_khz[round(c.freq_hz / 1000)] = c.name

        with self._lock:
            self._priority_lanes = priority_lanes
            self._default_lane = _Lane(weight=0.0, windows=default_windows, is_range=False)
            self._scheduler = _WeightedScheduler(priority_lanes) if priority_lanes else None
            self._macro_cycle_counter = 0.0
            self._default_due = False
            self._channel_bw_map = bw_map
            self._channel_name_map = name_map
            self._channel_name_by_khz = name_by_khz
            self._channel_mod_map = mod_map
            self._channel_weight_map = weight_map

        total_windows = len(default_windows) + sum(len(lane.windows) for lane in priority_lanes)
        extra = (
            " " + tr("({count} priority-weighted entries)").format(count=len(priority_lanes))
            if priority_lanes else ""
        )
        self.signals.log.emit(
            tr("Wideband scanning: {channels} channels in {windows} capture window(s) ({mhz:.2f} MHz each){extra}.").format(
                channels=len(bw_map), windows=total_windows, mhz=sample_rate / 1e6, extra=extra,
            )
        )

    def _chunk_size(self) -> int:
        return max(1, int(self.config.device.sample_rate_hz * CHUNK_SECONDS))

    def _is_blacklisted(self, freq_hz: int) -> bool:
        return any(e.contains(freq_hz) for e in self.blacklist)

    def _maybe_apply_gain(self, window: ScanWindow | None) -> None:
        mode, db = self._effective_gain(window)
        if mode != self._applied_gain_mode or db != self._applied_gain_db:
            self._backend.set_gain(mode, db)
            self._applied_gain_mode, self._applied_gain_db = mode, db

    def _next_window(self) -> ScanWindow | None:
        with self._lock:
            for _ in range(MAX_PICK_ATTEMPTS):
                window = self._pick_next_locked()
                if window is None:
                    return None
                if any(not self._is_blacklisted(f) for f in window.freqs):
                    return window
            return None

    def _due_recency_boost_locked(self) -> ScanWindow | None:
        """A boosted window whose next extra visit has come due, if any -
        see RecencyBoostConfig/_register_recency_boost. Checked first, ahead
        of the normal weighted rotation, so a boost actually delivers the
        "visit more often than the schedule alone would give it" it
        promises - but it's still just one extra pick slotted in among the
        normal ones, never a takeover: the very next pick goes right back to
        the regular scheduler, so nothing else waits any longer because of
        this."""
        if not self._recency_boosts:
            return None
        now = time.monotonic()
        expired = [k for k, b in self._recency_boosts.items() if now >= b["expires_at"]]
        for k in expired:
            del self._recency_boosts[k]
        if not self.config.recency_boost.enabled:
            return None
        for boost in self._recency_boosts.values():
            if now >= boost["next_due"]:
                boost["next_due"] = now + boost["interval"]
                return boost["window"]
        return None

    def _pick_next_locked(self) -> ScanWindow | None:
        """Must be called with self._lock held - see _rebuild_windows()'s
        docstring for the scheduling rules this implements."""
        boosted = self._due_recency_boost_locked()
        if boosted is not None:
            return boosted

        if self._default_due and self._default_lane.windows:
            self._default_due = False
            return self._default_lane.advance()

        if self._scheduler is not None and self._scheduler.lanes:
            lane = self._scheduler.pick()
            window = lane.advance()
            if lane.is_range and lane.just_wrapped:
                self._default_due = True
            elif not any(l.is_range for l in self._scheduler.lanes):
                # No range-lane to hook the "give weight-0 stuff a turn" event
                # onto - fall back to once per full weighted-scheduler cycle.
                self._macro_cycle_counter += 1
                if self._macro_cycle_counter >= self._scheduler.total_weight:
                    self._macro_cycle_counter = 0.0
                    self._default_due = True
            return window

        return self._default_lane.advance()

    def _emit_state(self, state: ScannerState) -> None:
        """Deduplicated: the SCANNING-tick caller (_main_loop) re-derives and
        re-emits this every ~30ms regardless of whether it actually changed
        (_display_state() only changes when a slot opens/closes/lingers),
        so without this a queued cross-thread Qt signal was being posted to
        the GUI thread - and a QLabel.setStyleSheet() re-polish paid there,
        noticeably pricier than a plain setText() - roughly 30x more often
        than the displayed value could possibly change. Safe: the only
        consumer (ScannerPanel.update_state, wired in MainWindow) just
        displays the current state, nothing here relies on a steady stream
        of identical events as a heartbeat."""
        if state == self._last_emitted_state:
            return
        self._last_emitted_state = state
        self.signals.state_changed.emit(state.value)

    def _display_state(self) -> ScannerState:
        """Coarse status text for the GUI, derived from the slots - purely
        cosmetic, doesn't drive the loop's own control flow (see
        _main_loop, which only ever needs CALCULATE_NEXT_FREQ/TUNING/
        SCANNING for that)."""
        if not self._slots:
            return ScannerState.SCANNING
        if any(s.linger_deadline is None for s in self._slots.values()):
            return ScannerState.SIGNAL_DETECTED
        return ScannerState.LINGER

    def _submit_spectrum(
        self, iq: np.ndarray, sample_rate: float, center_hz: float, trigger_dbfs: float, custom: bool = False,
    ) -> None:
        """Hands the raw IQ block to _SpectrumWorker's own thread - never
        computes the FFT here, on the same thread that also has to keep
        live audio flowing in real time (see _SpectrumWorker's docstring)."""
        self._spectrum_worker.submit(iq, sample_rate, center_hz, trigger_dbfs, custom)

    def _emit_freqs(self) -> None:
        """Tells the GUI what to show in the frequency display: every
        currently active channel (primary - the audible one - first, the
        rest by descending weight), or a placeholder describing the tuned
        window while nothing's active yet."""
        order = sorted(
            self._slots.keys(),
            key=lambda f: (0 if f == self._primary_freq else 1, -self._channel_weight_map.get(f, 0.0)),
        )
        entries = [(f, self._slots[f].name) for f in order]
        if not entries and self._cur_window is not None:
            window = self._cur_window
            if len(window.freqs) == 1:
                label = self._channel_name_map.get(window.freqs[0], "")
            else:
                label = f"{len(window.freqs)} kanalow w oknie"
            entries = [(window.center_hz, label)]
        self.signals.freq_changed.emit(entries)

    def _main_loop(self) -> None:
        _checkpoint("_main_loop() entered")
        state = ScannerState.CALCULATE_NEXT_FREQ
        self._emit_state(state)
        tuning_deadline = scanning_deadline = 0.0
        sample_rate = self.config.device.sample_rate_hz

        while not self._stop_evt.is_set():
            self._maybe_apply_gain(self._cur_window)
            chunk = self._chunk_size()

            if state == ScannerState.CALCULATE_NEXT_FREQ:
                self._checkpoint_once("first CALCULATE_NEXT_FREQ")
                window = self._next_window()
                if window is None:
                    time.sleep(0.3)
                    continue
                self._cur_window = window
                self._emit_freqs()  # no slots active here (guaranteed - see below) -> shows the window placeholder
                if window.center_hz == self._last_tuned_center_hz:
                    # Already parked here (e.g. a band narrow enough to fit
                    # in a single window, or a solo'd band) - retuning would
                    # just be a no-op hardware call followed by a pointless
                    # tuning-settle wait, and would misleadingly report a
                    # "return to this window" cycle time for a window we
                    # never actually left. Skip straight to SCANNING.
                    scanning_deadline = time.monotonic() + self.config.timing.scanning_dwell_ms / 1000.0
                    state = ScannerState.SCANNING
                    self._emit_state(state)
                else:
                    self._backend.set_frequency(window.center_hz)
                    self._last_tuned_center_hz = window.center_hz
                    self._checkpoint_once("first set_frequency() done")
                    now = time.monotonic()
                    last_visit = self._window_last_visit.get(window.center_hz)
                    self._window_last_visit[window.center_hz] = now
                    if last_visit is not None:
                        self.signals.cycle_time_changed.emit(now - last_visit)
                    tuning_deadline = time.monotonic() + self._effective_tuning_time_ms(window) / 1000.0
                    state = ScannerState.TUNING
                    self._emit_state(state)

            elif state == ScannerState.TUNING:
                self._checkpoint_once("first TUNING read() about to run")
                self._backend.read(chunk)  # discard: head is muted while the tuner settles
                self._checkpoint_once("first TUNING read() returned")
                self._skip_evt.clear()
                self._skip_lockout_evt.clear()
                if time.monotonic() >= tuning_deadline:
                    scanning_deadline = time.monotonic() + self.config.timing.scanning_dwell_ms / 1000.0
                    state = ScannerState.SCANNING
                    self._emit_state(state)

            elif state == ScannerState.SCANNING:
                self._checkpoint_once("first SCANNING read() about to run")
                read_start = time.monotonic() if _DEBUG_TIMING else 0.0
                iq = self._backend.read(chunk)
                self._checkpoint_once("first SCANNING read() returned")
                if _DEBUG_TIMING:
                    self._record_read_timing(time.monotonic() - read_start)
                tick_start = time.monotonic() if _DEBUG_TIMING else 0.0
                window = self._cur_window
                self._feed_raw_iq(iq, window.center_hz)

                # One FFT/PSD/cumsum for the whole captured block, shared by
                # both steps below (already-active slots + new-detection
                # candidates) instead of each independently recomputing its
                # own - see channelizer.spectrum_cumsum()'s docstring.
                spectrum = spectrum_cumsum(iq, sample_rate)

                # 1. Advance whatever's already active (demod/play/record,
                # decide continue/linger/close) - see _service_active_slots.
                primary_level = self._service_active_slots(iq, spectrum, sample_rate, window)
                any_active = bool(self._slots)
                if not any_active:
                    # Nothing was serviced this tick at all, so
                    # _play_and_record_slot() (the other place manual
                    # recording gets fed) never ran - keep a manual
                    # recording's timeline continuous through pure idle
                    # scanning by feeding it one tick's worth of silence
                    # directly. A no-op (one lock check) if no manual
                    # recording is running.
                    self._feed_manual_recording(
                        np.zeros(max(1, round(AUDIO_RATE * CHUNK_SECONDS)), dtype=np.float32)
                    )

                # 2. Look for new activity - either the very first detection
                # in this window, or an *additional* channel while something
                # else is already active. No cap on how many can open.
                # Vectorized blacklist check (see _blacklist_mask) - a plain
                # per-frequency any()-over-blacklist call here would re-scan
                # the whole blacklist for every one of window.freqs, every
                # tick, with no short-circuit (list comprehensions can't).
                not_blacklisted = ~self._blacklist_mask(window.freqs)
                candidate_freqs = [
                    f for f, ok in zip(window.freqs, not_blacklisted)
                    if ok and f not in self._slots
                ]
                raw_powers: dict[int, float] = {}
                best_freq = best_level = None
                if candidate_freqs:
                    raw_powers = channel_powers_from_spectrum(
                        spectrum, window.center_hz, candidate_freqs, self._channel_bw_map,
                    )
                    # Corrected only for picking/triggering - see
                    # _apply_edge_compensation. raw_powers (uncorrected)
                    # stays around for calibration logging/WAV metadata.
                    corrected = self._apply_edge_compensation(raw_powers, window.center_hz)
                    best_freq, best_level = _strongest(corrected)

                effective_trigger = self._effective_trigger_dbfs(window)
                self.signals.level_changed.emit(
                    primary_level if primary_level is not None
                    else (best_level if best_level is not None else -120.0)
                )
                self._submit_spectrum(
                    iq, sample_rate, window.center_hz, effective_trigger,
                    window.band is not None and window.band.has_advanced_overrides,
                )
                self._emit_state(self._display_state())

                if self._skip_evt.is_set() or self._skip_lockout_evt.is_set():
                    lockout = self._skip_lockout_evt.is_set()
                    if any_active:
                        self._skip_all_slots(lockout)
                    elif lockout and best_freq is not None:
                        self.signals.blacklisted.emit(best_freq)
                    self._skip_evt.clear()
                    self._skip_lockout_evt.clear()
                    state = ScannerState.CALCULATE_NEXT_FREQ
                    self._emit_state(state)
                elif (
                    best_freq is not None and best_level >= effective_trigger
                    and not self._at_recording_capacity()
                ):
                    raw_level = raw_powers[best_freq]
                    self._calibration_log.log_detect(
                        best_freq, window.center_hz, sample_rate, self._applied_gain_mode,
                        self._applied_gain_db, raw_level, effective_trigger,
                        self._channel_bw_map.get(best_freq, 0.0),
                    )
                    self._open_slot(best_freq, window, raw_level)
                elif not any_active and time.monotonic() >= scanning_deadline:
                    # Nothing active (never started, or just ended this
                    # tick) and the window's had its dwell - move on, same
                    # as always: the round-robin will circle back to it.
                    self._maybe_log_floor(iq, sample_rate, window)
                    state = ScannerState.CALCULATE_NEXT_FREQ
                    self._emit_state(state)

                if _DEBUG_TIMING:
                    self._record_tick_timing(time.monotonic() - tick_start)

        # Stop requested mid-transmission: make sure any open segment(s) are closed.
        for freq_hz in list(self._slots.keys()):
            self._close_slot(freq_hz, reason="stopped")
        self._emit_state(ScannerState.IDLE)

    def _record_read_timing(self, elapsed: float) -> None:
        """SMARTSCAN_DEBUG_LIVE_AUDIO-gated: tracks how long backend.read()
        itself blocks (separate from the tick-processing time below - see
        _READ_STALL_LOG_THRESHOLD_S for why this is measured at all). Folded
        into _record_tick_timing's once-a-second summary; a single call this
        slow also gets an immediate line of its own so a rare multi-second
        stall isn't averaged away in the aggregate."""
        self._read_n += 1
        if elapsed > CHUNK_SECONDS:
            self._read_over_budget += 1
        self._read_max_s = max(self._read_max_s, elapsed)
        if elapsed > _READ_STALL_LOG_THRESHOLD_S:
            print(
                f"[scanner-timing STALL {time.strftime('%H:%M:%S')}] backend.read() blocked for "
                f"{elapsed * 1000:.0f}ms (budget {CHUNK_SECONDS * 1000:.0f}ms) - active_slots={len(self._slots)}",
                flush=True,
            )

    def _record_tick_timing(self, elapsed: float) -> None:
        """SMARTSCAN_DEBUG_LIVE_AUDIO-gated: once a second, prints how many
        SCANNING ticks took longer than CHUNK_SECONDS to process (i.e. the
        real work - demod, noise reduction, per active slot - didn't fit in
        the time its own chunk represents, a genuine throughput deficit),
        the worst one, and the live-audio ring buffer's current fill level.
        A fill level that's climbing tick over tick (not just hovering near
        the ~0.2s priming cushion) confirms production is outrunning
        real-time playback rather than the reverse (underrunning). Also
        reports the same stats for backend.read()'s own blocking time (see
        _record_read_timing) - a *processing* deficit here means "too much
        work per tick", but a *read* deficit means the SDR/USB itself isn't
        delivering samples in real time, a completely different problem with
        a completely different fix."""
        self._tick_n += 1
        if elapsed > CHUNK_SECONDS:
            self._tick_over_budget += 1
        self._tick_max_s = max(self._tick_max_s, elapsed)
        now = time.monotonic()
        if now - self._tick_report_at < 1.0:
            return
        buffered_ms = self._audio_out.buffered_seconds * 1000 if self._audio_out else 0.0
        print(
            f"[scanner-timing {time.strftime('%H:%M:%S')}] ticks={self._tick_n} "
            f"over_budget={self._tick_over_budget} max_tick={self._tick_max_s * 1000:.1f}ms "
            f"budget={CHUNK_SECONDS * 1000:.0f}ms active_slots={len(self._slots)} "
            f"audio_buffered={buffered_ms:.0f}ms | "
            f"read_over_budget={self._read_over_budget}/{self._read_n} max_read={self._read_max_s * 1000:.1f}ms",
            flush=True,
        )
        self._tick_n = self._tick_over_budget = 0
        self._tick_max_s = 0.0
        self._read_n = self._read_over_budget = 0
        self._read_max_s = 0.0
        self._tick_report_at = now

    def _correction_db(self, offset_hz: float) -> float:
        """0 dB until _setup_static() has actually built the curve (or if
        there's no calibration data yet to build one from) - see
        edge_compensation.py."""
        if self._edge_compensation is None:
            return 0.0
        return self._edge_compensation.correction_db(offset_hz)

    def _apply_edge_compensation(self, powers: dict[int, float], center_hz: float) -> dict[int, float]:
        """Corrected copy of a channel_powers_dbfs() result, used only for
        the trigger/level-meter decision - never for what gets logged to
        CalibrationLogger or stored in a recording's own WAV metadata (those
        stay raw, since correcting them would feed the correction back into
        its own source data - see edge_compensation.py's module docstring)."""
        if self._edge_compensation is None or not self._edge_compensation.has_data:
            return powers
        return {f: v + self._correction_db(f - center_hz) for f, v in powers.items()}

    # -- per-band "advanced" overrides (Scanner tab) --------------------
    # A window tagged with a band (see ScanWindow.band / _rebuild_windows)
    # can carry its own trigger level/timing/gain/noise-reduction, set apart
    # from every other band's - e.g. a noisier civil-aviation band running a
    # less sensitive trigger than a quieter fire-brigade band would need.
    # None on the band's own field (the default - "no override yet") always
    # falls back to the ordinary global Scanner-tab setting, so an
    # unconfigured band behaves exactly as it always has.
    def _effective_trigger_dbfs(self, window: ScanWindow | None) -> float:
        band = window.band if window is not None else None
        if band is not None and band.trigger_level_dbfs is not None:
            return band.trigger_level_dbfs
        return self.config.trigger_level_dbfs

    def _effective_tuning_time_ms(self, window: ScanWindow | None) -> int:
        band = window.band if window is not None else None
        if band is not None and band.tuning_time_ms is not None:
            return band.tuning_time_ms
        return self.config.timing.tuning_time_ms

    def _effective_linger_time_ms(self, window: ScanWindow | None) -> int:
        band = window.band if window is not None else None
        if band is not None and band.linger_time_ms is not None:
            return band.linger_time_ms
        return self.config.timing.linger_time_ms

    def _effective_gain(self, window: ScanWindow | None) -> tuple[str, float]:
        band = window.band if window is not None else None
        dev = self.config.device
        mode = band.gain_mode if band is not None and band.gain_mode is not None else dev.gain_mode
        db = band.gain_db if band is not None and band.gain_db is not None else dev.gain_db
        return mode, db

    def _effective_squelch_dbfs(self, window: ScanWindow | None) -> float | None:
        """None when squelch is off (the default - see AppConfig.squelch_enabled),
        otherwise the absolute dBFS level below which both the live speaker
        AND the recording drop this block (see _play_and_record_slot) -
        never the LINGER timer/slot-close decision, which stays keyed to
        trigger_dbfs alone regardless of squelch. Deliberately computed as
        this window's *effective trigger* (respecting any per-band override
        - see _effective_trigger_dbfs) plus squelch_offset_db, never as an
        independent absolute number - squelch_offset_db is clamped to <= 0
        here too (belt and suspenders; the Scanner tab's slider already
        can't go positive) so this can never sit above trigger and
        therefore can never mute anything while the state machine is still
        in SIGNAL_DETECTED (level >= trigger) - see AGENTS.md gotcha 6 for
        why that invariant (not whether recording is also squelched) is
        what actually keeps this safe."""
        if not self.config.squelch_enabled:
            return None
        return self._effective_trigger_dbfs(window) + min(0.0, self.config.squelch_offset_db)

    def _effective_noise_reduction(self, window: ScanWindow | None) -> tuple[bool, float]:
        band = window.band if window is not None else None
        nr = self.config.noise_reduction
        enabled = (
            band.noise_reduction_enabled if band is not None and band.noise_reduction_enabled is not None
            else nr.enabled
        )
        strength = (
            band.noise_reduction_strength if band is not None and band.noise_reduction_strength is not None
            else nr.strength
        )
        return enabled, strength

    def _maybe_log_floor(self, iq: np.ndarray, sample_rate: float, window: ScanWindow) -> None:
        """See CalibrationLogger - a background-noise snapshot of this whole
        window, throttled to once every _FLOOR_LOG_INTERVAL_S per window
        (center_hz) so the extra full-window channel_powers_dbfs() call
        (cheap once in a while, not worth paying every tick) never competes
        with the real-time audio path."""
        now = time.monotonic()
        if now - self._last_floor_log.get(window.center_hz, 0.0) < _FLOOR_LOG_INTERVAL_S:
            return
        self._last_floor_log[window.center_hz] = now
        powers = channel_powers_dbfs(iq, sample_rate, window.center_hz, window.freqs, self._channel_bw_map)
        self._calibration_log.log_floor(
            window.center_hz, sample_rate, self._applied_gain_mode, self._applied_gain_db, powers,
        )

    # -- slot management -----------------------------------------------
    def _service_active_slots(self, iq, spectrum, sample_rate: float, window: ScanWindow) -> float | None:
        """Advances every currently-open slot by one IQ block. Returns the
        primary slot's current level (for the level meter), or None if
        nothing is active. ``spectrum`` is this tick's (bin_freqs, cumsum)
        pair from channelizer.spectrum_cumsum(iq, sample_rate) - computed
        once by the caller and reused here instead of each recomputing its
        own FFT over the same iq (see _main_loop's SCANNING branch)."""
        if not self._slots:
            return None
        active_freqs = list(self._slots.keys())
        raw_powers = channel_powers_from_spectrum(spectrum, window.center_hz, active_freqs, self._channel_bw_map)
        trigger_dbfs = self._effective_trigger_dbfs(window)
        squelch_dbfs = self._effective_squelch_dbfs(window)
        linger_time_ms = self._effective_linger_time_ms(window)
        primary_level = None

        # Demod/noise-reduction/file-write for every active slot is
        # dispatched to self._slot_pool instead of running one slot after
        # another on this thread - see AGENTS.md gotcha 12 for the real-
        # hardware measurements that led to this (many simultaneously
        # active slots alone was enough to blow the per-tick real-time
        # budget several times over, backing up the acquisition queue -
        # see AcquisitionThread - until it started dropping chunks, same
        # audible symptom as the driver-level overflow that fix solved,
        # just moved one layer up). Safe to parallelize: each slot owns its
        # own Demodulator/NoiseReducer/Recorder (see _Slot's docstring), so
        # there's no shared *mutable* state between these tasks - only
        # ``iq`` itself is shared, but every task only ever reads it.
        # ``self._audio_out`` is shared too, but AudioOutput.write() already
        # holds its own lock internally.
        tasks = []
        for freq_hz, slot in list(self._slots.items()):
            # Corrected for the linger/level-meter decision (see
            # _apply_edge_compensation) - a slot near the window's edge
            # shouldn't have to cling to a lower real level than one near
            # the center just to avoid lingering out.
            level = raw_powers.get(freq_hz, -120.0) + self._correction_db(slot.offset_hz)
            if freq_hz == self._primary_freq:
                primary_level = level
            # Squelch (see _effective_squelch_dbfs) mutes the live speaker
            # AND skips writing this block to the recording (see
            # _play_and_record_slot) - but never the LINGER timer/slot-close
            # decision below, which still depends only on trigger_dbfs.
            # squelch_dbfs is always <= trigger_dbfs (see
            # _effective_squelch_dbfs), so this can never fire while level
            # is still >= trigger - i.e. never while the state machine
            # itself says SIGNAL_DETECTED.
            squelched = squelch_dbfs is not None and level < squelch_dbfs
            future = self._slot_pool.submit(self._process_one_slot, slot, iq, squelched)
            tasks.append((freq_hz, slot, level, future))

        for freq_hz, slot, level, future in tasks:
            future.result()  # re-raises whatever that slot's own processing raised, same as the old sequential call would have
            if level >= trigger_dbfs:
                slot.linger_deadline = None
            elif slot.linger_deadline is None:
                slot.linger_deadline = time.monotonic() + linger_time_ms / 1000.0
            elif time.monotonic() >= slot.linger_deadline:
                self._close_slot(freq_hz)
        return primary_level

    def _process_one_slot(self, slot: _Slot, iq: np.ndarray, squelched: bool) -> None:
        """Runs on self._slot_pool's worker threads - see
        _service_active_slots(). Demodulates this one slot's own channel
        out of the shared ``iq`` block and hands it to
        _play_and_record_slot(). Reading the same ``iq`` array from several
        threads at once is safe (nothing anywhere writes back into it,
        numpy reads don't mutate); each call only ever touches this one
        slot's own Demodulator/NoiseReducer/Recorder."""
        stereo = slot.modulation == Modulation.WFM and self.config.wfm_stereo_enabled
        audio = slot.demod.demod(iq, slot.modulation, slot.offset_hz, stereo=stereo)
        self._play_and_record_slot(slot, audio, squelched=squelched)

    def _at_recording_capacity(self) -> bool:
        """True if opening one more slot would exceed
        ``config.recording.max_concurrent_recordings`` (0 = unlimited, the
        default - always returns False then). Only gates auto-recording:
        irrelevant while auto-recording itself is off, since every open
        slot's own ``Recorder`` only actually writes anything when
        ``config.recording.enabled`` is True (see _open_slot) - a capped
        scan with auto-recording off would otherwise stop *watching*
        channels live for no reason, not just stop recording them."""
        limit = self.config.recording.max_concurrent_recordings
        if not self.config.recording.enabled or limit <= 0:
            return False
        return len(self._slots) >= limit

    def _open_slot(self, freq_hz: int, window: ScanWindow, level_dbfs: float) -> None:
        dev = self.config.device
        demod = Demodulator(dev.sample_rate_hz, AUDIO_RATE)
        noise_reducer = NoiseReducer(AUDIO_RATE)
        recorder = Recorder(self.config.recording, AUDIO_RATE)
        name = self._channel_name_map.get(freq_hz) or self._channel_name_by_khz.get(round(freq_hz / 1000), "")
        modulation = self._channel_mod_map.get(freq_hz, Modulation.AM)
        offset_hz = float(freq_hz - window.center_hz)
        slot = _Slot(freq_hz, name, modulation, offset_hz, demod, noise_reducer, recorder)
        slot.window = window  # remembered for _register_recency_boost on close
        self._slots[freq_hz] = slot

        # Higher weight wins the live speaker; a tie keeps whichever slot
        # already had it ("first heard" - only a strictly higher weight
        # actually takes over).
        weight = self._channel_weight_map.get(freq_hz, 0.0)
        if self._primary_freq is None or weight > self._primary_weight:
            self._primary_freq = freq_hz
            self._primary_weight = weight

        timing = self.config.timing
        nr_enabled, nr_strength = self._effective_noise_reduction(window)
        # Everything the app can gather about this transmission at open
        # time, for the WAV metadata chunk (see wav_metadata.py) - lets a
        # recording later be cross-checked against the exact settings/RF
        # conditions it was captured under (gain, trigger, offset from the
        # window's center, ...). Written to the file once it closes (see
        # _close_slot), since wave.Wave_write finalises its header on close.
        # Trigger/timing/noise-reduction fields reflect this window's *own*
        # effective settings (a band's "advanced" override, if it has one -
        # see _effective_*), not necessarily the global Scanner-tab values.
        slot.meta = {
            "app": "SmartScanATC",
            "freq_hz": freq_hz,
            "name": name,
            "modulation": modulation.value,
            "channel_weight": weight,
            "window_center_hz": window.center_hz,
            "offset_from_center_hz": offset_hz,
            "sample_rate_hz": dev.sample_rate_hz,
            "channel_bw_hz": self._channel_bw_map.get(freq_hz),
            "trigger_level_dbfs": self._effective_trigger_dbfs(window),
            "measured_dbfs_at_trigger": level_dbfs,
            "edge_correction_db": self._correction_db(offset_hz),
            "gain_mode": self._applied_gain_mode,
            "gain_db": self._applied_gain_db,
            "device_driver": dev.driver,
            "scanning_dwell_ms": timing.scanning_dwell_ms,
            "tuning_time_ms": self._effective_tuning_time_ms(window),
            "linger_time_ms": self._effective_linger_time_ms(window),
            "noise_reduction_enabled": nr_enabled,
            "noise_reduction_strength": nr_strength,
            "opened_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        }

        if self.config.recording.enabled:
            recorder.start_session()
            recorder.begin_transmission(freq_hz)
        self.signals.recording_changed.emit(True, self._active_recording_path())
        self._emit_freqs()

    def _close_slot(self, freq_hz: int, reason: str = "linger") -> None:
        slot = self._slots.pop(freq_hz, None)
        if slot is None:
            return
        finished_path = None
        if self.config.recording.enabled:
            finished_path = slot.recorder.end_transmission()
            slot.recorder.stop_session()
            if finished_path is not None:
                # Metadata is a bonus on top of an already-successfully-
                # written recording - never let a problem here (disk full,
                # weird path, ...) take down the scanner or be mistaken for
                # a recording failure. Must run before any mp3 transcode:
                # this project's WAV metadata chunk (wav_metadata.py) is a
                # RIFF/LIST chunk, which an mp3 container has no room for.
                try:
                    slot.meta["close_reason"] = reason
                    slot.meta["closed_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
                    slot.meta["duration_sec"] = round(time.monotonic() - slot.opened_at, 3)
                    append_metadata_chunk(Path(finished_path), slot.meta)
                except Exception:
                    pass
                if self.config.recording.output_format == "mp3":
                    mp3_path = transcode_to_mp3(Path(finished_path))
                    if mp3_path is not None:
                        finished_path = mp3_path
        if finished_path is not None:
            self.signals.recording_finished.emit(str(finished_path))
        if reason == "linger":
            # Only a natural end-of-transmission earns a boost - a manual
            # Skip means the user actively wants to move on, and
            # blacklist_skip/stopped/connection_lost aren't "might come
            # back any second" situations at all.
            self._register_recency_boost(slot.window)
        if self._primary_freq == freq_hz:
            self._reassign_primary()
        self.signals.recording_changed.emit(bool(self._slots), self._active_recording_path())
        self._emit_freqs()

    def _register_recency_boost(self, window: ScanWindow) -> None:
        """See RecencyBoostConfig - starts (or refreshes) a limited-time
        "visit this window more often" boost after a transmission on it
        ends naturally. Deliberately reads config.recency_boost live (not
        cached at rebuild time) so toggling it in Opcje takes effect on the
        very next transmission, no rebuild/restart needed - same as
        trigger_level_dbfs."""
        cfg = self.config.recency_boost
        if not cfg.enabled:
            return
        now = time.monotonic()
        self._recency_boosts[window.center_hz] = {
            "window": window,
            "expires_at": now + cfg.duration_s,
            "next_due": now + cfg.interval_s,
            "interval": cfg.interval_s,
        }

    def _reassign_primary(self) -> None:
        if not self._slots:
            self._primary_freq = None
            self._primary_weight = -1.0
            return
        freq_hz = max(self._slots, key=lambda f: self._channel_weight_map.get(f, 0.0))
        self._primary_freq = freq_hz
        self._primary_weight = self._channel_weight_map.get(freq_hz, 0.0)

    def _active_recording_path(self) -> str:
        """Path shown by the single REC indicator in the GUI - the primary
        slot's, falling back to any active one if the primary somehow has
        none (shouldn't normally happen)."""
        if self._primary_freq is not None and self._primary_freq in self._slots:
            return self._slots[self._primary_freq].recorder.current_path
        if self._slots:
            return next(iter(self._slots.values())).recorder.current_path
        return ""

    def _skip_all_slots(self, lockout: bool) -> None:
        for freq_hz in list(self._slots.keys()):
            self._close_slot(freq_hz, reason="blacklist_skip" if lockout else "skip")
            if lockout:
                self.signals.blacklisted.emit(freq_hz)

    def _play_and_record_slot(self, slot: _Slot, audio: np.ndarray, squelched: bool = False) -> None:
        # ``squelched`` (see _effective_squelch_dbfs) always skips the live
        # speaker - no one wants the loud FM-discriminator noise burst that
        # follows a transmitter releasing PTT in their ears. The recording
        # gets SILENCE for this block instead - explicit user request/
        # design decision: skipping the block outright (an earlier version
        # of this) made a single, still-open recording jump-cut mid-file
        # with no way to tell, on playback, whether a message was genuinely
        # continuous or just a signal caught in bursts with real gaps
        # edited out. A *mid*-transmission gap should sound like a real
        # pause; recorder.write_audio()'s own ``real=False`` handling is
        # what then decides, in hindsight, whether this particular silence
        # turns out to be that (more real audio follows - written as real
        # silence, in order) or the trailing tail of the whole transmission
        # (nothing follows - trimmed, unwritten, per a later explicit
        # follow-up request: the end should just end, not trail into dead
        # air). A real, audible gap belongs *between* recordings - once
        # LINGER fully expires and a later, genuinely new detection starts a
        # separate file, two recordings existing is exactly right - never
        # *inside* one. Since squelch_dbfs is always <= trigger_dbfs (see
        # _effective_squelch_dbfs), this never fires while the state
        # machine still considers the channel SIGNAL_DETECTED - only during
        # the LINGER tail, once level has already dropped below trigger.
        if squelched:
            slot.was_squelched = True
            if slot.freq_hz == self._primary_freq:
                # Mirrors what the live speaker would have heard (nothing -
                # squelch mutes it) into a manual recording, if one is
                # running - see start_manual_recording(). Independent of
                # self._muted_evt, same as the speaker-mute line below.
                self._feed_manual_recording(np.zeros(audio.shape[0], dtype=np.float32))
            if self.config.recording.enabled:
                # real=False: silence substituted for a squelched block
                # doesn't count as "this transmission had anything worth
                # hearing" - see Recorder.end_transmission()'s phantom-
                # recording cleanup, which would otherwise never trigger
                # again now that *something* always gets written.
                silence = np.zeros(audio.shape[0], dtype=np.float32)
                slot.recorder.write_audio(silence, real=False)
            return
        if slot.was_squelched:
            # Resuming right after a squelched gap: noise_reducer (see
            # _apply_noise_reduction) is a stateful STFT processor (input
            # buffer, overlap-add reconstruction buffer, minimum-statistics
            # noise-floor tracker - see dsp.py's NoiseReducer) that was
            # never called during the gap, since it only ever runs below
            # this point. Left un-reset, its first call after resuming
            # would silently splice whatever partial frame was buffered
            # from *before* the gap onto this genuinely new, non-adjacent
            # audio as if no time had passed - a real, reported glitch/
            # click right at the resume point. Resetting means losing at
            # most one FRAME_SIZE-worth (~40ms) of noise-reduction "warm-
            # up" on resume, a far smaller cost than a stitched artifact.
            # The demodulator needs no such reset: it runs every tick
            # regardless of squelch (see _service_active_slots), so its
            # own state (phase, DC estimate) was never interrupted.
            slot.noise_reducer.reset()
            slot.was_squelched = False
        if audio.ndim == 1:
            audio = self._apply_noise_reduction(slot, audio)
            mono = audio
        else:
            # Noise reduction is tuned for weak-antenna ATC voice, not
            # broadcast FM - skip it for stereo audio rather than reworking
            # it for 2 channels. Recordings stay mono regardless of live
            # stereo (see AppConfig.wfm_stereo_enabled), so downmix here.
            mono = audio.mean(axis=1).astype(np.float32)
        if slot.freq_hz == self._primary_freq and not self._muted_evt.is_set():
            self._audio_out.write(audio)
        if slot.freq_hz == self._primary_freq:
            # Manual recording mirrors the primary slot's real audio
            # regardless of mute - like the automatic recording below, it's
            # meant to capture what's actually happening, not just what's
            # currently being listened to live. freq_hz here is real
            # content (unlike the squelched branch above, which mirrors
            # silence) - this is what stop_manual_recording() renames the
            # file to, once the recording ends.
            self._feed_manual_recording(mono, freq_hz=slot.freq_hz)
        if self.config.recording.enabled:
            slot.recorder.write_audio(mono)

    def _apply_noise_reduction(self, slot: _Slot, audio):
        enabled, strength = self._effective_noise_reduction(slot.window)
        if not enabled:
            return audio
        return slot.noise_reducer.process(audio, strength=strength)


def _strongest(powers: dict[int, float]) -> tuple[int | None, float]:
    if not powers:
        return None, -120.0
    freq = max(powers, key=powers.get)
    return freq, powers[freq]
