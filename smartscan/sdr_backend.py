"""Hardware abstraction over SoapySDR, with a Simulated fallback so the app
is fully testable even before a SoapySDR device/module is available.
"""
from __future__ import annotations

import os
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from pathlib import Path

import numpy as np

from .config import CONFIG_DIR
from .io_diag_log import SdrIoDiagLogger

try:
    import SoapySDR
    from SoapySDR import SOAPY_SDR_CF32, SOAPY_SDR_RX
    HAVE_SOAPY = True
except ImportError:
    HAVE_SOAPY = False


# Real crashes observed (journalctl, see the session that added this): the
# whole process dies silently - no Python traceback, no Qt/Soapy error text
# at all, just gone within a few seconds of the RTL-SDR device being
# reopened - specifically clustered right after a previous SmartScanATC
# process had *just* closed the very same USB device (evidenced repeatedly:
# a scope's "Consumed Ns CPU time" stop line immediately followed, 2s later,
# by a new process opening the same tuner and dying silently 2-9s after
# that). Both this project's own SoapyBackend.close() and a normal app quit
# already do a clean deactivateStream()/closeStream(), but the underlying
# USB device (and whatever internal state librtlsdr/libusb still has on the
# kernel/USB-controller side right after a release) isn't necessarily ready
# for a fresh claim+stream-start instantly - reopening it within a couple of
# seconds of the previous close appears able to crash natively (a segfault
# in the C driver, unreachable from Python - nothing here could "catch" it
# even in principle). LAST_CLOSE_FILE persists the close timestamp across
# process restarts (a brand new process has no in-memory memory of when the
# previous one closed the device) so __init__ can insert a short wait when
# the previous close was suspiciously recent, without ever delaying a normal
# "it's been a while" startup at all.
LAST_CLOSE_FILE = CONFIG_DIR / "sdr_last_close.txt"
_REOPEN_COOLDOWN_S = 3.0


#: Generic RF gain range (dB) used for the GUI slider before a real device is
#: opened; SoapyBackend narrows this to the device's actual range once known.
DEFAULT_GAIN_RANGE_DB = (0.0, 50.0)


class SdrBackend(ABC):
    @abstractmethod
    def set_frequency(self, freq_hz: float) -> None: ...

    @abstractmethod
    def set_gain(self, gain_mode: str, gain_db: float) -> None: ...

    @abstractmethod
    def read(self, num_samples: int) -> np.ndarray: ...

    @abstractmethod
    def close(self) -> None: ...

    label = "unknown"
    gain_range_db: tuple[float, float] = DEFAULT_GAIN_RANGE_DB


# On the RTL-SDR Blog V4, "offset tuning" isn't a real mode anymore - the
# dongle reuses that same rtlsdr API call/GPIO line as its bias-tee toggle,
# for backward compatibility with software that has no dedicated bias-tee
# control. Bias-tee state also persists on the chip across device opens.
# So every one of these keys must be forced off, unconditionally, every
# time the device is (re)configured - setting names differ between
# SoapyRTLSDR versions ("biastee" vs "bias_tee"), hence all three.
_BIAS_TEE_ARG_KEYS = ("biastee", "bias_tee", "offset_tuning")


class SoapyBackend(SdrBackend):
    label = "soapysdr"

    def __init__(self, driver: str, device_args: str, sample_rate: float, gain_mode: str, gain_db: float):
        if not HAVE_SOAPY:
            raise RuntimeError("Modul python3-soapysdr nie jest zainstalowany")
        self._wait_for_reopen_cooldown()
        args = {"driver": driver} if driver and driver != "auto" else {}
        if device_args:
            for kv in device_args.split(","):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    k = k.strip()
                    if k in _BIAS_TEE_ARG_KEYS:
                        # Never allow the antenna to be powered - see the
                        # unconditional writeSetting() disable below.
                        continue
                    args[k] = v.strip()
        self.device = SoapySDR.Device(args)
        self._disable_bias_tee()
        self.sample_rate = sample_rate
        self.device.setSampleRate(SOAPY_SDR_RX, 0, sample_rate)
        try:
            rng = self.device.getGainRange(SOAPY_SDR_RX, 0)
            self.gain_range_db = (rng.minimum(), rng.maximum())
        except Exception:
            pass
        self.set_gain(gain_mode, gain_db)
        # SoapyRTLSDR exposes "bufflen"/"buffers" as *stream* args (found via
        # `strings` on librtlsdrSupport.so - "Number of bytes per buffer",
        # "Number of buffers in the ring") - not device args, and read()'s
        # own scratch-buffer growth (below) turned out NOT to be the real
        # constraint on how much readStream() hands back per call: real
        # session data (sdr_io_diag.jsonl) showed the multi-call-per-read
        # rate sitting at an unchanged ~55% for weeks straight, completely
        # unmoved by that fix - consistent with the driver's own internal
        # per-buffer chunking being the actual limit, not our buffer size.
        # bufflen must be a multiple of 512 (per the driver's own arg
        # description) and is in raw bytes (RTL-SDR's native 8-bit I/Q,
        # 2 bytes/complex sample - *not* the 8 bytes/sample of the CF32 we
        # actually read back) - sized here for ~100ms of margin at the
        # configured sample rate, comfortably above a real ~30ms chunk.
        raw_bufflen = (int(sample_rate * 0.1) * 2 // 512 + 1) * 512
        try:
            self.stream = self.device.setupStream(
                SOAPY_SDR_RX, SOAPY_SDR_CF32, [0], {"bufflen": str(raw_bufflen)},
            )
        except Exception:
            # Some driver/version might not accept this stream arg at all -
            # fall back to the plain call rather than fail to open the
            # device entirely over what's purely a latency tweak.
            self.stream = self.device.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32)
        self.device.activateStream(self.stream)
        try:
            self.stream_mtu = self.device.getStreamMTU(self.stream)
        except Exception:
            self.stream_mtu = None
        # Grown on demand in read() to always fit a whole requested chunk in
        # one readStream() call - kept as defense in depth even though the
        # bufflen stream arg above is the fix actually expected to matter
        # (see this method's comment above) - a too-small scratch buffer
        # here would still independently force a second call regardless of
        # how much the driver itself can hand back in one go.
        self._buf = np.empty(65536, dtype=np.complex64)
        self._io_diag = SdrIoDiagLogger()

    def _disable_bias_tee(self) -> None:
        for key in _BIAS_TEE_ARG_KEYS:
            try:
                self.device.writeSetting(key, "false")
            except Exception:
                pass

    def set_frequency(self, freq_hz: float) -> None:
        self.device.setFrequency(SOAPY_SDR_RX, 0, freq_hz)
        # Re-assert on every retune (i.e. constantly, while a scan is
        # running) - some rtlsdr driver builds re-derive the bias-tee GPIO
        # from tuner state on retune, so a one-time disable at open() is not
        # enough to keep a passive antenna from ever seeing DC.
        self._disable_bias_tee()

    def set_gain(self, gain_mode: str, gain_db: float) -> None:
        if gain_mode == "auto":
            try:
                self.device.setGainMode(SOAPY_SDR_RX, 0, True)
                return
            except Exception:
                pass
        try:
            self.device.setGainMode(SOAPY_SDR_RX, 0, False)
        except Exception:
            pass
        self.device.setGain(SOAPY_SDR_RX, 0, gain_db)

    def read(self, num_samples: int) -> np.ndarray:
        """Found via real-session SMARTSCAN_DEBUG_LIVE_AUDIO logging: the
        scratch buffer used to matter more than it looks like it should.
        readStream() can only ever hand back up to len(self._buf) samples
        per call, so if a caller's whole chunk (num_samples - e.g. 72000 for
        the default 2.4 MHz/30ms config) doesn't fit in one buffer, this
        loop has to make a *second* readStream() call just to mop up the
        remainder - and that second call pays its own full per-call
        overhead (USB round-trip, driver-side buffering granularity) on top
        of the first, roughly doubling this method's real wall-clock time
        even though there's no genuinely new work to justify it (measured:
        median ~54ms per read() against a 30ms budget, with a fixed 65536
        buffer against a 72000-sample chunk - 2 calls every single tick).
        Growing the buffer to fit num_samples up front lets a normal chunk
        complete in a single readStream() call instead.
        """
        if num_samples > self._buf.size:
            self._buf = np.empty(num_samples, dtype=np.complex64)
        out = np.empty(num_samples, dtype=np.complex64)
        got = 0
        calls = 0
        read_start = time.monotonic()
        deadline = read_start + 1.0
        while got < num_samples and time.monotonic() < deadline:
            chunk = min(num_samples - got, self._buf.size)
            sr = self.device.readStream(self.stream, [self._buf[:chunk]], chunk, timeoutUs=200_000)
            calls += 1
            if sr.ret > 0:
                out[got:got + sr.ret] = self._buf[:sr.ret]
                got += sr.ret
            elif sr.ret < 0:
                # Negative return = an error code (SOAPY_SDR_OVERFLOW and
                # friends) - see io_diag_log.py's module docstring for why
                # this specifically matters: splicing what we read *after*
                # this onto what's already in `out` risks joining two
                # non-time-contiguous blocks, which can look like a real
                # broadband signal to the FFT. Logged, not otherwise
                # handled yet - this is here to first measure whether/how
                # often it actually happens before changing behaviour.
                self._io_diag.record_error(sr.ret, calls, got, num_samples)
        self._io_diag.record_read(time.monotonic() - read_start, calls)
        if got < num_samples:
            # A live device fills the whole chunk well within this window
            # (chunk is ~30ms of real time at the configured sample rate) -
            # coming up short after the full 1s deadline means the stream is
            # dead or badly starved, not just one slow packet. This also
            # catches a flaky USB dongle that trickles in a few samples
            # (got > 0, so it wouldn't have tripped a got == 0 check) but
            # nowhere near enough to keep up: without this, the scanner
            # would keep "working" on a starved/truncated buffer forever -
            # no signal ever triggers, and it never reconnects because no
            # exception is ever raised. Raise here so that case reconnects
            # too, same as a fully dead stream.
            raise RuntimeError(
                f"Strumien SDR dostarcza za malo danych ({got}/{num_samples} probek) "
                "- urzadzenie odlaczone lub nie odpowiada"
            )
        return out[:got]

    def close(self) -> None:
        try:
            self.device.deactivateStream(self.stream)
            self.device.closeStream(self.stream)
        except Exception:
            pass
        self._record_close_time()

    @staticmethod
    def _record_close_time() -> None:
        """Persists "now" so the *next* SoapyBackend (possibly a brand new
        process - see LAST_CLOSE_FILE's module-level comment) knows how
        recently this exact device was released. Best-effort: a failure to
        write this is never worth surfacing, it just means the next open
        skips the cooldown wait instead of a crash risk being reduced."""
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            LAST_CLOSE_FILE.write_text(str(time.time()))
        except OSError:
            pass

    @staticmethod
    def _wait_for_reopen_cooldown() -> None:
        try:
            last_close = float(LAST_CLOSE_FILE.read_text().strip())
        except (OSError, ValueError):
            return  # brak pliku (pierwsze uruchomienie po restarcie itp.) - nic do czekania
        remaining = _REOPEN_COOLDOWN_S - (time.time() - last_close)
        if remaining > 0:
            time.sleep(remaining)

    @staticmethod
    def enumerate_devices() -> list[dict]:
        if not HAVE_SOAPY:
            return []
        try:
            devices = [dict(r) for r in SoapySDR.Device.enumerate()]
        except Exception:
            return []
        # The "audio" driver (soapysdr-module-audio) exposes sound-card loopback
        # devices, not RF front-ends - not useful for an RF scanner, so hide them.
        return [d for d in devices if d.get("driver") != "audio"]


class SimulatedBackend(SdrBackend):
    """Generates noise plus occasional synthetic AM bursts so the whole
    scan/detect/record pipeline can be exercised without real hardware.
    """
    label = "simulated"

    # Frequencies (Hz) that will "transmit" a fake burst when tuned to.
    _ACTIVE_FREQS = {121_500_000, 118_100_000, 124_000_000}
    _PERIOD_S = 5.0
    _ON_DURATION_S = 2.5

    def __init__(self, sample_rate: float):
        self.sample_rate = sample_rate
        self.freq_hz = 0.0
        self._rng = np.random.default_rng()
        self._t = 0.0
        self._start_time = time.monotonic()
        self._gain_scale = 1.0

    def set_frequency(self, freq_hz: float) -> None:
        self.freq_hz = freq_hz

    def set_gain(self, gain_mode: str, gain_db: float) -> None:
        # No real RF gain to control, but scale the fake signal so dragging
        # the Gain slider gives visible feedback on the level meter even
        # without hardware attached.
        reference_db = 30.0
        self._gain_scale = 10 ** ((gain_db - reference_db) / 20.0)

    def _bursting_now(self, active_freq: int) -> bool:
        # Deterministic, per-frequency duty cycle purely as a function of
        # wall-clock time - no persisted state, so retuning/recapturing near
        # the same "active" frequency can never cancel an in-flight burst.
        offset = (hash(active_freq) % 97) / 97.0 * self._PERIOD_S
        elapsed = time.monotonic() - self._start_time + offset
        return (elapsed % self._PERIOD_S) < self._ON_DURATION_S

    def read(self, num_samples: int) -> np.ndarray:
        # Pace ourselves to real time, like a real device blocking on I/O -
        # otherwise the engine thread would spin thousands of times per
        # scanning dwell and flood the GUI with signal emissions.
        dur = num_samples / self.sample_rate
        time.sleep(dur)

        noise = (self._rng.normal(size=num_samples) + 1j * self._rng.normal(size=num_samples)).astype(np.complex64)
        noise *= 0.01

        # Wideband capture: any "active" frequency that happens to fall
        # within the currently tuned capture window shows up at its offset
        # from the LO - just like a real transmission would, regardless of
        # exactly where the window center sits.
        half_bw = self.sample_rate / 2.0 * 0.95
        tt = self._t + np.arange(num_samples) / self.sample_rate
        for active_freq in self._ACTIVE_FREQS:
            offset_hz = active_freq - self.freq_hz
            if abs(offset_hz) > half_bw or not self._bursting_now(active_freq):
                continue
            tone = 0.6 * np.sin(2 * np.pi * 800 * tt)
            envelope = (1.0 + tone) * 0.5
            carrier = np.exp(1j * 2 * np.pi * offset_hz * tt)
            signal = 0.5 * envelope * carrier
            noise = noise + signal.astype(np.complex64)
        self._t += dur
        return (noise * self._gain_scale).astype(np.complex64)

    def close(self) -> None:
        pass


class AcquisitionThread:
    """Decouples the real SDR read() from whatever the caller does with the
    result, so a slow consumer (many simultaneous demodulated channels, a
    noise reducer's FFT work, WAV file I/O, GIL contention from another
    thread, ...) can never delay the *next* call into the hardware/driver.

    Why this exists: SoapyBackend.read()'s own docstring already documents
    that the RTL-SDR driver keeps filling a small (~100ms, see
    SoapyBackend's ``raw_bufflen``) internal ring buffer regardless of when
    we actually call readStream() - if the *caller* (the old design: the
    scanner's single thread, doing read() then demod/noise-reduction/file-
    write/FFT for every active slot before looping back to read() again)
    takes too long between two read() calls, that buffer overflows and the
    driver silently drops/laps real samples. Splicing what we read *after*
    that onto what came before joins two non-time-contiguous IQ blocks,
    which shows up as a broadband click/phase discontinuity once
    demodulated - audible as a "ścinka" in both live audio and recordings,
    completely unrelated to squelch or noise reduction. Measured for real
    on this project's own SDR I/O diagnostic log (see io_diag_log.py):
    historically ~5% of all 5-second windows had at least one such overflow
    event, and 99.6% had at least one read() call slower than one tick's
    real-time budget - and confirmed live on real RTL-SDR hardware tuned to
    a dense FM broadcast segment (many simultaneous demodulated channels -
    exactly the "many active slots" scenario): 14 overflow events in 35
    seconds, tick processing running 2.6-5x over its real-time budget.

    The fix (this class): a dedicated thread whose *only* job is to call
    the real backend's read() back-to-back into a bounded FIFO queue of
    already-read chunks, with nothing else - no demod, no file I/O, no FFT -
    ever running between two consecutive read() calls. The scanner's main
    loop becomes a *consumer* of this queue (via read(), same blocking/
    exception contract as calling a raw SdrBackend directly - see
    SdrBackend.read()) instead of calling the hardware directly - exactly
    the acquisition/processing separation mature SDR software (SDR++
    included) uses, and the same pattern this project already applies to
    live audio output (AudioOutput's ring buffer) and the spectrum display
    (_SpectrumWorker in scanner_engine.py) - just never, until now, to the
    one thing that actually touches the hardware.

    ``max_queued`` bounds queue growth if the consumer falls behind for a
    while (rather than unbounded memory growth) - the oldest chunk is
    dropped, which loses data far more gracefully (a bounded, measurable
    amount, from a queue - not a silent driver-level splice) than an
    overflow would. At the default chunk size this is several seconds of
    margin, comfortably more than any transient consumer stall should need.
    """

    def __init__(self, backend: "SdrBackend", chunk_size: int, max_queued: int = 64):
        self._backend = backend
        self._chunk_size = chunk_size
        self._max_queued = max_queued
        self._queue: deque[np.ndarray] = deque()
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        # Guards actual hardware access (readStream/setFrequency/setGain) -
        # held by the acquisition thread only while a read() call is
        # in-flight, and briefly by set_frequency()/set_gain() (called from
        # the *consumer* thread) - avoids relying on any particular
        # SoapySDR driver's own thread-safety guarantees for concurrent
        # streaming vs. non-streaming calls, at the cost of a retune
        # occasionally waiting out one in-flight read() call (bounded by
        # one chunk's real-time duration - negligible next to how rarely
        # retuning happens).
        self._device_lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, name="sdr-acquisition", daemon=True)
        self._thread.start()

    @staticmethod
    def _boost_priority() -> None:
        """Same best-effort SCHED_FIFO real-time priority request as
        ScannerEngine._maybe_boost_priority() - see its docstring. Even more
        directly relevant here: this is now the one thread whose own
        promptness the whole fix depends on."""
        if not hasattr(os, "sched_setscheduler"):
            return
        try:
            tid = threading.get_native_id()
            os.sched_setscheduler(tid, os.SCHED_FIFO, os.sched_param(10))
        except (PermissionError, OSError):
            pass

    def _run(self) -> None:
        self._boost_priority()
        while not self._stop_evt.is_set():
            try:
                with self._device_lock:
                    block = self._backend.read(self._chunk_size)
            except BaseException as exc:  # noqa: BLE001 - propagated verbatim to read(), see there
                with self._cond:
                    self._error = exc
                    self._cond.notify_all()
                return
            with self._cond:
                if self._stop_evt.is_set():
                    return
                self._queue.append(block)
                if len(self._queue) > self._max_queued:
                    self._queue.popleft()
                self._cond.notify_all()

    def read(self, num_samples: int, timeout: float = 1.5) -> np.ndarray:
        """Same contract as SdrBackend.read(): blocks for the next ready
        chunk (up to ``timeout``), raises on a dead/stopped acquisition
        thread (propagating whatever exception actually killed it, e.g. the
        RuntimeError SoapyBackend.read() raises for a dead device - see
        AGENTS.md gotcha 8 - so ScannerEngine's existing reconnect logic
        needs no changes at all). ``num_samples`` must match the chunk size
        this thread was started with (ScannerEngine only ever calls this
        with its own, unchanging-for-the-life-of-one-backend _chunk_size())."""
        if num_samples != self._chunk_size:
            raise ValueError(
                f"AcquisitionThread was started with chunk_size={self._chunk_size}, "
                f"but read({num_samples}) was requested - these must match."
            )
        deadline = time.monotonic() + timeout
        with self._cond:
            while not self._queue and self._error is None and not self._stop_evt.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(
                        "Watek akwizycji SDR nie dostarczyl danych na czas - "
                        "urzadzenie odlaczone lub nie odpowiada"
                    )
                self._cond.wait(remaining)
            if self._error is not None:
                raise self._error
            if not self._queue:
                raise RuntimeError("Watek akwizycji SDR zatrzymany")
            return self._queue.popleft()

    def flush(self) -> None:
        """Discards whatever's currently queued - always call right after a
        retune/gain change (see set_frequency()/set_gain(), which already
        do this): queued chunks were captured *before* the change, and
        handing them to the caller as if they matched the new frequency/
        gain would corrupt every FFT-based power measurement for however
        long the queue takes to drain otherwise."""
        with self._cond:
            self._queue.clear()

    def set_frequency(self, freq_hz: float) -> None:
        with self._device_lock:
            self._backend.set_frequency(freq_hz)
        self.flush()

    def set_gain(self, gain_mode: str, gain_db: float) -> None:
        with self._device_lock:
            self._backend.set_gain(gain_mode, gain_db)
        self.flush()

    def close(self) -> None:
        self._stop_evt.set()
        with self._cond:
            self._cond.notify_all()
        self._thread.join(timeout=2.0)
        self._backend.close()

    @property
    def label(self) -> str:
        return self._backend.label

    @property
    def gain_range_db(self) -> tuple[float, float]:
        return self._backend.gain_range_db

    @property
    def stream_mtu(self):
        return getattr(self._backend, "stream_mtu", None)


def create_backend(driver: str, device_args: str, sample_rate: float, gain_mode: str, gain_db: float) -> SdrBackend:
    if driver == "simulated":
        backend = SimulatedBackend(sample_rate)
        backend.set_gain(gain_mode, gain_db)
        return backend

    if not driver or driver == "auto":
        # No specific device was requested - fall back to simulation quietly
        # so the app is still usable/demoable with nothing plugged in.
        try:
            return SoapyBackend(driver, device_args, sample_rate, gain_mode, gain_db)
        except Exception:
            backend = SimulatedBackend(sample_rate)
            backend.set_gain(gain_mode, gain_db)
            return backend

    # A specific real device was explicitly chosen in the GUI: if it fails to
    # open, that's worth surfacing loudly (engine stops, clear log message)
    # rather than silently switching to fake data - which looks identical to
    # a working scan and is exactly what confused a user pulling the dongle.
    return SoapyBackend(driver, device_args, sample_rate, gain_mode, gain_db)
