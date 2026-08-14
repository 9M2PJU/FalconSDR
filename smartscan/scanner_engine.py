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

import threading
import time

import numpy as np
from PyQt6.QtCore import QObject, pyqtSignal

from .audio_output import AudioOutput
from .channelizer import USABLE_FRACTION, ScanWindow, channel_powers_dbfs, plan_windows, spectrum_snapshot
from .config import AppConfig, VfoRangeConfig
from .dsp import Demodulator, NoiseReducer
from .models import BlacklistEntry, Channel, Modulation, ScannerState
from .recorder import Recorder
from .sdr_backend import SoapyBackend, create_backend

AUDIO_RATE = 12500
CHUNK_SECONDS = 0.03
MAX_VFO_CHANNELS = 20_000
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


class ScannerSignals(QObject):
    state_changed = pyqtSignal(str)
    level_changed = pyqtSignal(float)
    # list[tuple[freq_hz, name]] - one entry per currently active channel,
    # primary (the one actually audible live) first, or a single placeholder
    # entry for the tuned window while nothing's active yet.
    freq_changed = pyqtSignal(object)
    recording_changed = pyqtSignal(bool, str)
    recording_finished = pyqtSignal(str)  # path of a just-closed per-transmission file (split mode)
    blacklisted = pyqtSignal(int)
    spectrum_ready = pyqtSignal(object, object, float)  # freqs_hz, db, trigger_dbfs
    gain_range_changed = pyqtSignal(float, float)
    log = pyqtSignal(str)
    finished = pyqtSignal()


class ScannerEngine(threading.Thread):
    def __init__(self, config: AppConfig, channels: list[Channel], blacklist: list[BlacklistEntry]):
        super().__init__(daemon=True)
        self.signals = ScannerSignals()
        self.config = config
        self.channels = channels
        self.blacklist = blacklist

        self._stop_evt = threading.Event()
        self._skip_evt = threading.Event()          # "Skip" - move on now, frequency stays scannable
        self._skip_lockout_evt = threading.Event()   # "Skip + Blacklist" - move on now, permanently blacklist it
        self._muted_evt = threading.Event()

        self._backend = None
        self._audio_out: AudioOutput | None = None

        # Guards the scheduling state below, which _rebuild_windows() replaces
        # wholesale (from the GUI thread) while _next_window() reads/advances
        # it (from this thread) - see request_rebuild().
        self._lock = threading.Lock()
        self._priority_lanes: list[_Lane] = []
        self._default_lane = _Lane(weight=0.0, windows=[], is_range=False)
        self._scheduler: _WeightedScheduler | None = None
        self._macro_cycle_counter = 0.0
        self._default_due = False
        self._channel_bw_map: dict[int, float] = {}
        self._channel_name_map: dict[int, str] = {}
        self._channel_mod_map: dict[int, Modulation] = {}
        self._channel_weight_map: dict[int, float] = {}

        self._cur_window: ScanWindow | None = None
        # Keyed by freq_hz - naturally unique among *active* transmissions
        # (a frequency can only be in one slot at a time), no fixed size.
        self._slots: dict[int, _Slot] = {}
        self._primary_freq: int | None = None  # which slot's audio is on the live speaker
        self._primary_weight = -1.0

        self._applied_gain_mode = ""
        self._applied_gain_db = None
        self._had_real_hardware = False

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

    def update_channels(self, channels: list[Channel]) -> None:
        self.channels = channels
        self._rebuild_windows()

    def update_blacklist(self, blacklist: list[BlacklistEntry]) -> None:
        self.blacklist = blacklist

    def request_rebuild(self) -> None:
        """Called from the GUI after any change that affects what gets
        scanned - bands, channels, weights, the Memory List master toggle,
        step/modulation edits, ... - so it takes effect immediately on a
        running scan, without needing to Stop/Start."""
        self._rebuild_windows()

    # -- thread body ---------------------------------------------------
    def run(self) -> None:
        self._setup_static()
        connected = self._open_backend_with_retry(allow_simulated_fallback=True)
        self._had_real_hardware = connected and self._backend.label != "simulated"

        while connected and not self._stop_evt.is_set():
            try:
                self._main_loop()
            except Exception as exc:
                self.signals.log.emit(f"Polaczenie z SDR przerwane: {exc}")
                for freq_hz in list(self._slots.keys()):
                    self._close_slot(freq_hz)
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
        if not self._audio_out.available:
            self.signals.log.emit("Brak wyjscia audio (PyAudio) - monitorowanie na zywo wylaczone.")
        self._rebuild_windows()

    def _open_backend(self, allow_simulated_fallback: bool) -> None:
        dev = self.config.device
        if allow_simulated_fallback or dev.driver == "simulated":
            self._backend = create_backend(dev.driver, dev.device_args, dev.sample_rate_hz, dev.gain_mode, dev.gain_db)
        else:
            # Bypass create_backend()'s friendly auto-simulate fallback: this
            # is a reconnect after real hardware was already proven present,
            # so a failure here must keep raising, never quietly substitute
            # fake data (that's exactly the bug that confused a user before).
            self._backend = SoapyBackend(dev.driver, dev.device_args, dev.sample_rate_hz, dev.gain_mode, dev.gain_db)
        self._applied_gain_mode, self._applied_gain_db = dev.gain_mode, dev.gain_db
        self.signals.gain_range_changed.emit(*self._backend.gain_range_db)
        if self._backend.label == "simulated" and dev.driver != "simulated":
            self.signals.log.emit("Nie znaleziono/nie skonfigurowano SoapySDR - dzialam w trybie SYMULACJI.")

    def _open_backend_with_retry(self, allow_simulated_fallback: bool) -> bool:
        """Keep (re)trying to open the SDR with escalating backoff until it
        succeeds or the user presses Stop. Returns False only on Stop."""
        attempt = 0
        while True:
            try:
                self._open_backend(allow_simulated_fallback)
                if attempt > 0:
                    self.signals.log.emit("Polaczono ponownie z SDR - wznawiam skanowanie.")
                return True
            except Exception as exc:
                if self._stop_evt.is_set():
                    return False
                attempt += 1
                backoff = RECONNECT_BACKOFF_S[min(attempt - 1, len(RECONNECT_BACKOFF_S) - 1)]
                self.signals.log.emit(
                    f"Nie udalo sie polaczyc z SDR ({exc}) - ponowna proba za {backoff}s (proba {attempt})..."
                )
                self._emit_state(ScannerState.RECONNECTING)
                deadline = time.monotonic() + backoff
                while time.monotonic() < deadline:
                    if self._stop_evt.is_set():
                        return False
                    time.sleep(0.1)

    def _teardown(self) -> None:
        for freq_hz in list(self._slots.keys()):
            self._close_slot(freq_hz)
        if self._audio_out is not None:
            self._audio_out.close()
        if self._backend is not None:
            self._backend.close()

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
                    f"Zbyt wiele kanalow pasma \"{vr.label}\" ({len(freqs) * keep_every}) - przerzedzono co {keep_every}."
                )
            for f in freqs:
                if f not in bw_map:
                    _claim(f, float(max(1, vr.step_hz)), band_mod, weight=vr.weight)
            windows = plan_windows(freqs, sample_rate, max(1, vr.step_hz))
            priority_lanes.append(_Lane(weight=vr.weight, windows=windows, is_range=True))

        # -- default lane: everything with weight 0, pooled together exactly
        # like the original (pre-weights) implementation. -------------------
        default_freqs: list[int] = []
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
                    f"Zbyt wiele kanalow pasma \"{vr.label}\" ({len(freqs) * keep_every}) - przerzedzono co {keep_every}."
                )
            for f in freqs:
                if f not in bw_map:
                    _claim(f, float(max(1, vr.step_hz)), band_mod, weight=vr.weight)
                default_freqs.append(f)

        default_grouping_bw = max((bw_map[f] for f in default_freqs), default=25_000.0)
        default_windows = plan_windows(default_freqs, sample_rate, default_grouping_bw)

        with self._lock:
            self._priority_lanes = priority_lanes
            self._default_lane = _Lane(weight=0.0, windows=default_windows, is_range=False)
            self._scheduler = _WeightedScheduler(priority_lanes) if priority_lanes else None
            self._macro_cycle_counter = 0.0
            self._default_due = False
            self._channel_bw_map = bw_map
            self._channel_name_map = name_map
            self._channel_mod_map = mod_map
            self._channel_weight_map = weight_map

        total_windows = len(default_windows) + sum(len(lane.windows) for lane in priority_lanes)
        extra = f", {len(priority_lanes)} pozycji z waga priorytetu" if priority_lanes else ""
        self.signals.log.emit(
            f"Skanowanie szerokopasmowe: {len(bw_map)} kanalow w {total_windows} "
            f"oknie/oknach przechwytu ({sample_rate / 1e6:.2f} MHz kazde){extra}."
        )

    def _chunk_size(self) -> int:
        return max(1, int(self.config.device.sample_rate_hz * CHUNK_SECONDS))

    def _is_blacklisted(self, freq_hz: int) -> bool:
        return any(e.contains(freq_hz) for e in self.blacklist)

    def _maybe_apply_gain(self) -> None:
        dev = self.config.device
        if dev.gain_mode != self._applied_gain_mode or dev.gain_db != self._applied_gain_db:
            self._backend.set_gain(dev.gain_mode, dev.gain_db)
            self._applied_gain_mode, self._applied_gain_db = dev.gain_mode, dev.gain_db

    def _next_window(self) -> ScanWindow | None:
        with self._lock:
            for _ in range(MAX_PICK_ATTEMPTS):
                window = self._pick_next_locked()
                if window is None:
                    return None
                if any(not self._is_blacklisted(f) for f in window.freqs):
                    return window
            return None

    def _pick_next_locked(self) -> ScanWindow | None:
        """Must be called with self._lock held - see _rebuild_windows()'s
        docstring for the scheduling rules this implements."""
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

    def _emit_spectrum(self, iq, sample_rate: float, center_hz: float) -> None:
        freqs, db = spectrum_snapshot(iq, sample_rate, center_hz)
        self.signals.spectrum_ready.emit(freqs, db, self.config.trigger_level_dbfs)

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
        state = ScannerState.CALCULATE_NEXT_FREQ
        self._emit_state(state)
        tuning_deadline = scanning_deadline = 0.0
        sample_rate = self.config.device.sample_rate_hz

        while not self._stop_evt.is_set():
            self._maybe_apply_gain()
            chunk = self._chunk_size()

            if state == ScannerState.CALCULATE_NEXT_FREQ:
                window = self._next_window()
                if window is None:
                    time.sleep(0.3)
                    continue
                self._cur_window = window
                self._backend.set_frequency(window.center_hz)
                self._emit_freqs()  # no slots active here (guaranteed - see below) -> shows the window placeholder
                tuning_deadline = time.monotonic() + self.config.timing.tuning_time_ms / 1000.0
                state = ScannerState.TUNING
                self._emit_state(state)

            elif state == ScannerState.TUNING:
                self._backend.read(chunk)  # discard: head is muted while the tuner settles
                self._skip_evt.clear()
                self._skip_lockout_evt.clear()
                if time.monotonic() >= tuning_deadline:
                    scanning_deadline = time.monotonic() + self.config.timing.scanning_dwell_ms / 1000.0
                    state = ScannerState.SCANNING
                    self._emit_state(state)

            elif state == ScannerState.SCANNING:
                iq = self._backend.read(chunk)
                window = self._cur_window

                # 1. Advance whatever's already active (demod/play/record,
                # decide continue/linger/close) - see _service_active_slots.
                primary_level = self._service_active_slots(iq, sample_rate, window)
                any_active = bool(self._slots)

                # 2. Look for new activity - either the very first detection
                # in this window, or an *additional* channel while something
                # else is already active. No cap on how many can open.
                candidate_freqs = [
                    f for f in window.freqs
                    if not self._is_blacklisted(f) and f not in self._slots
                ]
                best_freq = best_level = None
                if candidate_freqs:
                    powers = channel_powers_dbfs(
                        iq, sample_rate, window.center_hz, candidate_freqs, self._channel_bw_map,
                    )
                    best_freq, best_level = _strongest(powers)

                self.signals.level_changed.emit(
                    primary_level if primary_level is not None
                    else (best_level if best_level is not None else -120.0)
                )
                self._emit_spectrum(iq, sample_rate, window.center_hz)
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
                elif best_freq is not None and best_level >= self.config.trigger_level_dbfs:
                    self._open_slot(best_freq, window)
                elif not any_active and time.monotonic() >= scanning_deadline:
                    # Nothing active (never started, or just ended this
                    # tick) and the window's had its dwell - move on, same
                    # as always: the round-robin will circle back to it.
                    state = ScannerState.CALCULATE_NEXT_FREQ
                    self._emit_state(state)

        # Stop requested mid-transmission: make sure any open segment(s) are closed.
        for freq_hz in list(self._slots.keys()):
            self._close_slot(freq_hz)
        self._emit_state(ScannerState.IDLE)

    # -- slot management -----------------------------------------------
    def _service_active_slots(self, iq, sample_rate: float, window: ScanWindow) -> float | None:
        """Advances every currently-open slot by one IQ block. Returns the
        primary slot's current level (for the level meter), or None if
        nothing is active."""
        if not self._slots:
            return None
        active_freqs = list(self._slots.keys())
        powers = channel_powers_dbfs(iq, sample_rate, window.center_hz, active_freqs, self._channel_bw_map)
        primary_level = None
        for freq_hz, slot in list(self._slots.items()):
            level = powers.get(freq_hz, -120.0)
            if freq_hz == self._primary_freq:
                primary_level = level
            stereo = slot.modulation == Modulation.WFM and self.config.wfm_stereo_enabled
            audio = slot.demod.demod(iq, slot.modulation, slot.offset_hz, stereo=stereo)
            self._play_and_record_slot(slot, audio)
            if level >= self.config.trigger_level_dbfs:
                slot.linger_deadline = None
            elif slot.linger_deadline is None:
                slot.linger_deadline = time.monotonic() + self.config.timing.linger_time_ms / 1000.0
            elif time.monotonic() >= slot.linger_deadline:
                self._close_slot(freq_hz)
        return primary_level

    def _open_slot(self, freq_hz: int, window: ScanWindow) -> None:
        dev = self.config.device
        demod = Demodulator(dev.sample_rate_hz, AUDIO_RATE)
        noise_reducer = NoiseReducer(AUDIO_RATE)
        recorder = Recorder(self.config.recording, AUDIO_RATE)
        name = self._channel_name_map.get(freq_hz, "")
        modulation = self._channel_mod_map.get(freq_hz, Modulation.AM)
        offset_hz = float(freq_hz - window.center_hz)
        self._slots[freq_hz] = _Slot(freq_hz, name, modulation, offset_hz, demod, noise_reducer, recorder)

        # Higher weight wins the live speaker; a tie keeps whichever slot
        # already had it ("first heard" - only a strictly higher weight
        # actually takes over).
        weight = self._channel_weight_map.get(freq_hz, 0.0)
        if self._primary_freq is None or weight > self._primary_weight:
            self._primary_freq = freq_hz
            self._primary_weight = weight

        if self.config.recording.enabled:
            recorder.start_session()
            recorder.begin_transmission(freq_hz)
        self.signals.recording_changed.emit(True, self._active_recording_path())
        self._emit_freqs()

    def _close_slot(self, freq_hz: int) -> None:
        slot = self._slots.pop(freq_hz, None)
        if slot is None:
            return
        finished_path = None
        if self.config.recording.enabled:
            finished_path = slot.recorder.end_transmission()
            slot.recorder.stop_session()
        if finished_path is not None:
            self.signals.recording_finished.emit(str(finished_path))
        if self._primary_freq == freq_hz:
            self._reassign_primary()
        self.signals.recording_changed.emit(bool(self._slots), self._active_recording_path())
        self._emit_freqs()

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
            self._close_slot(freq_hz)
            if lockout:
                self.signals.blacklisted.emit(freq_hz)

    def _play_and_record_slot(self, slot: _Slot, audio: np.ndarray) -> None:
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
        if self.config.recording.enabled:
            slot.recorder.write_audio(mono)

    def _apply_noise_reduction(self, slot: _Slot, audio):
        nr = self.config.noise_reduction
        if not nr.enabled:
            return audio
        return slot.noise_reducer.process(audio, strength=nr.strength)


def _strongest(powers: dict[int, float]) -> tuple[int | None, float]:
    if not powers:
        return None, -120.0
    freq = max(powers, key=powers.get)
    return freq, powers[freq]
