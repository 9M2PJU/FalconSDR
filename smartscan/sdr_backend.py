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

# Native librtlsdr via pyrtlsdr - the pip-only path for Windows/macOS, where
# SoapySDR has no pip wheel (PothosSDR's last Windows bundle is from 2021 and
# conda is a whole separate toolchain). pyrtlsdrlib ships a prebuilt
# librtlsdr (with RTL-SDR Blog V4 support) for Windows/macOS/Linux. Broad
# except: pyrtlsdr raises plain OSError/others at import when it can't find
# the shared library at all, not just ImportError.
try:
    from rtlsdr import RtlSdr
    HAVE_RTLSDR_NATIVE = True
except Exception:  # noqa: BLE001
    HAVE_RTLSDR_NATIVE = False

#: config.device.driver value selecting RtlSdrBackend - deliberately distinct
#: from SoapySDR's own "rtlsdr" driver key, so both can coexist on Linux.
RTLSDR_NATIVE_DRIVER = "rtlsdr_native"


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
            raise RuntimeError("The python3-soapysdr module is not installed")
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
                f"SDR stream is delivering too little data ({got}/{num_samples} samples) "
                "- device disconnected or not responding"
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


# Same unsigned-8-bit -> float mapping SoapyRTLSDR's CF32 lookup table uses
# ((i - 127.4) / 128), so levels in dBFS - and therefore every trigger/
# squelch threshold a user has tuned on Linux/SoapySDR - mean the same thing
# on this backend. pyrtlsdr's own read_samples() uses (i / 127.5 - 1), which
# is close but not identical.
_RTLSDR_LUT = ((np.arange(256, dtype=np.float32) - 127.4) / 128.0).astype(np.float32)

# librtlsdr's rtlsdr_read_sync() does a raw libusb bulk transfer - lengths
# that aren't a multiple of the USB packet size (512 B) are a known source
# of overflow errors on some libusb backends. RtlSdrBackend.read() always
# asks the driver for a whole number of these and carries the remainder
# over to the next call instead of dropping it.
_RTLSDR_READ_GRANULE_BYTES = 512


class RtlSdrBackend(SdrBackend):
    """RTL-SDR dongles through librtlsdr directly (pyrtlsdr), without
    SoapySDR - see HAVE_RTLSDR_NATIVE's comment for why this exists. Same
    contract as SoapyBackend: bias-tee forced off on open and every retune,
    reopen cooldown, and read() raising instead of ever returning short.

    Uses librtlsdr's synchronous read, called back-to-back by
    AcquisitionThread (never from the busy scanner loop), so the gap between
    two bulk transfers stays tiny and the dongle's own FIFO covers it.
    """
    label = RTLSDR_NATIVE_DRIVER

    def __init__(self, device_args: str, sample_rate: float, gain_mode: str, gain_db: float):
        if not HAVE_RTLSDR_NATIVE:
            raise RuntimeError("The pyrtlsdr/pyrtlsdrlib modules are not installed")
        SoapyBackend._wait_for_reopen_cooldown()
        serial = None
        for kv in (device_args or "").split(","):
            if "=" in kv:
                k, v = kv.split("=", 1)
                if k.strip() == "serial" and v.strip():
                    serial = v.strip()
        self.device = RtlSdr(serial_number=serial) if serial else RtlSdr()
        try:
            self.device.set_bias_tee(False)
            self.sample_rate = sample_rate
            self.device.sample_rate = sample_rate
            gains = self.device.get_gains()  # tenths of a dB
            if gains:
                self.gain_range_db = (min(gains) / 10.0, max(gains) / 10.0)
            self.set_gain(gain_mode, gain_db)
        except Exception:
            self.device.close()
            raise
        self._carry = np.empty(0, dtype=np.uint8)
        self._io_diag = SdrIoDiagLogger()

    def set_frequency(self, freq_hz: float) -> None:
        self.device.center_freq = int(freq_hz)
        # See SoapyBackend.set_frequency() - re-asserted on every retune.
        self.device.set_bias_tee(False)
        # Leftover bytes belong to the previous frequency.
        self._carry = np.empty(0, dtype=np.uint8)

    def set_gain(self, gain_mode: str, gain_db: float) -> None:
        # pyrtlsdr's set_gain() already picks the nearest supported step.
        self.device.set_gain("auto" if gain_mode == "auto" else gain_db)

    def read(self, num_samples: int) -> np.ndarray:
        need = 2 * num_samples - self._carry.size
        read_start = time.monotonic()
        if need > 0:
            request = -(-need // _RTLSDR_READ_GRANULE_BYTES) * _RTLSDR_READ_GRANULE_BYTES
            try:
                # Raises (LibUSBError/IOError) on an unplugged device or a
                # short read - never returns fewer bytes than requested, so
                # the scanner's reconnect logic sees a dead stream the same
                # way it does from SoapyBackend.read().
                raw = np.ctypeslib.as_array(self.device.read_bytes(request)).copy()
            except Exception as exc:
                raise RuntimeError(f"RTL-SDR read failed - device disconnected or not responding ({exc})") from exc
            data = np.concatenate((self._carry, raw)) if self._carry.size else raw
        else:
            data = self._carry
        self._io_diag.record_read(time.monotonic() - read_start, 1)
        used = 2 * num_samples
        self._carry = data[used:]
        iq = _RTLSDR_LUT[data[:used]]
        return iq.view(np.complex64)

    def close(self) -> None:
        try:
            self.device.close()
        except Exception:
            pass
        SoapyBackend._record_close_time()

    @staticmethod
    def enumerate_devices() -> list[dict]:
        if not HAVE_RTLSDR_NATIVE:
            return []
        try:
            serials = RtlSdr.get_device_serial_addresses()
        except Exception:
            return []
        # No commas in the label - ScannerPanel joins every key into one
        # "k=v,k=v" device_args string.
        return [
            {"driver": RTLSDR_NATIVE_DRIVER, "label": f"RTL-SDR (librtlsdr) SN {serial}", "serial": serial}
            for serial in serials
        ]


def enumerate_devices() -> list[dict]:
    """Every real device the GUI can offer. Native librtlsdr devices are only
    listed when SoapySDR doesn't already expose an rtlsdr device - on Linux
    with both installed, the one dongle would otherwise show up twice."""
    devices = SoapyBackend.enumerate_devices()
    if not any(d.get("driver") == "rtlsdr" for d in devices):
        devices += RtlSdrBackend.enumerate_devices()
    return devices


def open_real_backend(driver: str, device_args: str, sample_rate: float, gain_mode: str, gain_db: float) -> SdrBackend:
    """Opens real hardware only - never falls back to SimulatedBackend.

    "auto" tries SoapySDR first (Linux's native path), then librtlsdr
    directly. A config saved on Linux with SoapySDR's "rtlsdr" driver also
    opens through librtlsdr on a machine without SoapySDR, so the same
    config.json works across platforms."""
    if driver == RTLSDR_NATIVE_DRIVER or (driver == "rtlsdr" and not HAVE_SOAPY):
        return RtlSdrBackend(device_args, sample_rate, gain_mode, gain_db)
    if driver and driver != "auto":
        return SoapyBackend(driver, device_args, sample_rate, gain_mode, gain_db)
    if not HAVE_SOAPY and HAVE_RTLSDR_NATIVE:
        return RtlSdrBackend(device_args, sample_rate, gain_mode, gain_db)
    try:
        return SoapyBackend(driver, device_args, sample_rate, gain_mode, gain_db)
    except Exception as soapy_exc:
        if not HAVE_RTLSDR_NATIVE:
            raise
        try:
            return RtlSdrBackend(device_args, sample_rate, gain_mode, gain_db)
        except Exception:
            raise soapy_exc


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
    demodulated - audible as a glitch/skip in both live audio and recordings,
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

    def __init__(self, backend: "SdrBackend", chunk_size: int, max_queued: int = 64, debug: bool = False):
        self._backend = backend
        self._chunk_size = chunk_size
        self._max_queued = max_queued
        # Gates the RT-priority outcome line and the periodic health summary
        # below (see _boost_priority()/_maybe_report_health()) - both are
        # genuinely useful when actually chasing a live-audio/timing report,
        # but pure console noise for every other launch, so they're now tied
        # to AppConfig.debug_mode (passed in by ScannerEngine) rather than
        # firing unconditionally for every user.
        self._debug = debug
        self._queue: deque[np.ndarray] = deque()
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._stop_evt = threading.Event()
        self._error: BaseException | None = None
        # Always-on health counters (not gated behind SMARTSCAN_DEBUG_LIVE_AUDIO,
        # unlike scanner_engine.py's own tick-timing summary) - see
        # _maybe_report_health()'s docstring for why this needed to exist
        # independent of that flag: a dropped chunk here is real, permanent
        # data loss (this queue overflowing, gotcha 11/28's own subject),
        # not a cosmetic timing wobble, and the one thing this class can see
        # that nothing downstream can reconstruct after the fact.
        self._chunks_read = 0
        self._chunks_dropped = 0
        self._read_slow_count = 0  # read() calls that took > one chunk's own duration
        self._read_max_s = 0.0
        self._chunk_duration_s = chunk_size / max(1.0, backend.sample_rate)
        self._health_reported_at = time.monotonic()
        # Bumped every time a retune/gain change is actually applied (see
        # _run()'s command handling) - see _run()'s comment on why a read()
        # started before that point must never be allowed to land in the
        # queue after it, as if it belonged to the new frequency/gain.
        self._generation = 0
        # set_frequency()/set_gain() (called from the *consumer* thread)
        # hand their request to this thread instead of touching
        # self._backend themselves - see _submit_command()'s docstring for
        # why a shared lock across threads was tried first and had to be
        # abandoned: this thread runs with SCHED_FIFO real-time priority
        # (see _boost_priority(), needed for prompt/reliable reads), and a
        # lock shared with a normal-priority caller is exactly the setup
        # for real-time priority inversion - measured for real on this
        # project's own hardware, a retune's attempt to acquire that lock
        # was starved for up to *11.7 seconds* by this thread continuously
        # winning the race to re-acquire it first, every time it released
        # it between reads. Single-owner (only this thread ever touches
        # self._backend, for anything) sidesteps the whole class of bug -
        # there's no second thread left to starve.
        self._pending_command: tuple | None = None
        self._command_done = threading.Event()
        self._thread = threading.Thread(target=self._run, name="sdr-acquisition", daemon=True)
        self._thread.start()

    def _boost_priority(self) -> None:
        """Same best-effort SCHED_FIFO real-time priority request as
        ScannerEngine._maybe_boost_priority() - see its docstring. Even more
        directly relevant here: this is now the one thread whose own
        promptness the whole fix depends on. Safe now to combine with the
        single-owner design above - see this class's docstring update on
        why that wasn't true of the shared-lock version this replaced.
        Always attempted regardless of ``self._debug`` - only the outcome
        print (below) is gated on it, not the actual scheduling call.

        Prints its own outcome when ``self._debug`` (AppConfig.debug_mode)
        is on - a real, user-reported case: this silently landing on the
        "except: pass" branch (missing rtprio ulimit/CAP_SYS_NICE - common
        when an app is launched from a desktop icon rather than a login-
        shell terminal, since desktop sessions don't always go through the
        same PAM limits.conf application) is indistinguishable from the
        outside, and looks exactly like "the fix didn't work" with no way
        to tell that apart from "it wasn't even applied" short of attaching
        a debugger - but for everyone else this was just console noise on
        every single launch, so it's opt-in now rather than unconditional."""
        if not hasattr(os, "sched_setscheduler"):
            if self._debug:
                print("[sdr-acquisition] real-time priority: unavailable on this platform", flush=True)
            return
        try:
            tid = threading.get_native_id()
            os.sched_setscheduler(tid, os.SCHED_FIFO, os.sched_param(10))
        except (PermissionError, OSError) as exc:
            if self._debug:
                print(
                    f"[sdr-acquisition] real-time priority NOT granted ({exc}) - running at normal "
                    "priority; check `ulimit -r` (needs >0) if live audio/scan speed seems inconsistent",
                    flush=True,
                )
        else:
            if self._debug:
                print("[sdr-acquisition] real-time priority: OK (SCHED_FIFO)", flush=True)

    _HEALTH_REPORT_INTERVAL_S = 20.0

    def _maybe_report_health(self) -> None:
        """Gated on ``self._debug`` (AppConfig.debug_mode) - periodic
        summary of this thread's own read timing and queue-drop counters,
        added chasing a real user report of live audio glitches plus
        recordings measurably shorter than the real elapsed time they
        spanned (missing *samples*, not just a live-monitoring buffering
        artifact - see AGENTS.md gotcha 30). ``_chunks_dropped`` is the one
        number that distinguishes "genuinely lost data" (this queue
        overflowing because the consumer fell behind for multiple seconds -
        see __init__'s ``max_queued`` docstring) from "everything reached
        the consumer, just with some timing jitter along the way" (slow
        individual read() calls that never actually cost a dropped chunk) -
        the two look identical from a listener's ear but need completely
        different fixes, and this is the only place in the whole pipeline
        that can actually tell them apart, since a dropped chunk is
        invisible to every downstream consumer (there's nothing to detect
        the *absence* of a chunk from the demod/recording side). Every
        counter is cumulative since this thread started, not reset per
        report - a single dropped chunk anywhere in a session should never
        quietly age out of visibility ~20s later."""
        if not self._debug:
            return
        now = time.monotonic()
        if now - self._health_reported_at < self._HEALTH_REPORT_INTERVAL_S:
            return
        self._health_reported_at = now
        print(
            f"[sdr-acquisition] health: chunks_read={self._chunks_read} "
            f"chunks_DROPPED={self._chunks_dropped} slow_reads={self._read_slow_count} "
            f"(budget {self._chunk_duration_s * 1000:.0f}ms) max_read={self._read_max_s * 1000:.0f}ms "
            f"queue_depth={len(self._queue)}/{self._max_queued}",
            flush=True,
        )

    def _run(self) -> None:
        self._boost_priority()
        while not self._stop_evt.is_set():
            # Apply any pending retune/gain change *before* the next read -
            # only this thread ever touches self._backend (see __init__'s
            # comment), so this needs no lock around the actual hardware
            # call, unlike the shared-_device_lock version that starved a
            # normal-priority caller for real. Checked once per loop, so a
            # command is applied within at most one read()'s worth of
            # latency (~30-100ms on this project's own hardware) - nowhere
            # near instant, but boundedly so, unlike the multi-second
            # lock-starvation this replaced.
            with self._cond:
                command = self._pending_command
                self._pending_command = None
                my_generation = self._generation
            if command is not None:
                kind, args = command
                try:
                    if kind == "freq":
                        self._backend.set_frequency(*args)
                    else:
                        self._backend.set_gain(*args)
                except BaseException as exc:  # noqa: BLE001 - propagated to the waiting caller below
                    with self._cond:
                        self._error = exc
                        self._cond.notify_all()
                    self._command_done.set()
                    return
                with self._cond:
                    self._queue.clear()
                    self._generation += 1
                    my_generation = self._generation
                self._command_done.set()
            try:
                read_start = time.monotonic()
                block = self._backend.read(self._chunk_size)
                read_elapsed = time.monotonic() - read_start
            except BaseException as exc:  # noqa: BLE001 - propagated verbatim to read(), see there
                with self._cond:
                    self._error = exc
                    self._cond.notify_all()
                # Wake a set_frequency()/set_gain() caller that submitted its
                # command while this read was in flight - otherwise it sits
                # out its full timeout and reports a misleading "didn't
                # respond" instead of this real error.
                self._command_done.set()
                return
            with self._cond:
                if self._stop_evt.is_set():
                    return
                if my_generation != self._generation:
                    # A retune/gain change was applied (bumping the
                    # generation, above) while this exact read() call was
                    # in flight: this block was captured *before* that
                    # change (the real device was still on the old
                    # frequency/gain when readStream() ran), so it's
                    # pre-change data that must not be handed to the caller
                    # as if it belonged to the new window - discard it.
                    # Without this check, a window's very first captured
                    # block after every single retune had a real chance of
                    # silently being the *previous* window's IQ instead,
                    # corrupting that window's FFT-based candidate
                    # detection into spurious "signal found" readings
                    # (stale strong-signal energy measured against the new,
                    # usually different, set of channel frequencies) - each
                    # bogus detection then had to run out its full LINGER
                    # timer before the scanner could move on. Reproduced
                    # and confirmed on real hardware (121-136 MHz civil
                    # aviation band, ~7 capture windows).
                    continue
                self._chunks_read += 1
                if read_elapsed > self._chunk_duration_s:
                    self._read_slow_count += 1
                self._read_max_s = max(self._read_max_s, read_elapsed)
                self._queue.append(block)
                if len(self._queue) > self._max_queued:
                    self._queue.popleft()
                    self._chunks_dropped += 1
                self._cond.notify_all()
            self._maybe_report_health()

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
                        "SDR acquisition thread didn't deliver data in time - "
                        "device disconnected or not responding"
                    )
                self._cond.wait(remaining)
            if self._error is not None:
                raise self._error
            if not self._queue:
                raise RuntimeError("SDR acquisition thread stopped")
            return self._queue.popleft()

    def _submit_command(self, kind: str, args: tuple, timeout: float = 2.0) -> None:
        """Hands a set_frequency()/set_gain() request to the acquisition
        thread instead of calling self._backend directly from here (the
        consumer thread) - see __init__'s comment for why: this thread runs
        with SCHED_FIFO real-time priority for prompt/reliable reads, and a
        lock shared between it and a normal-priority caller is a real-time
        priority-inversion hazard, not just a theoretical one (measured:
        the shared-lock version this replaced starved a retune for up to
        11.7 seconds on real hardware, turning a several-windows-per-second
        sweep into multiple seconds per window). Blocks until the
        acquisition thread has actually applied it (bounded by one read()'s
        worth of latency, not instant - see _run()) or raises on timeout/
        the same error a dead device would raise from read()."""
        self._command_done.clear()
        with self._cond:
            if self._error is not None:
                raise self._error
            self._pending_command = (kind, args)
            self._cond.notify_all()
        if not self._command_done.wait(timeout) and self._error is None:
            raise RuntimeError(
                "SDR acquisition thread didn't respond to the frequency/gain change in time"
            )
        if self._error is not None:
            raise self._error

    def set_frequency(self, freq_hz: float) -> None:
        self._submit_command("freq", (freq_hz,))

    def set_gain(self, gain_mode: str, gain_db: float) -> None:
        self._submit_command("gain", (gain_mode, gain_db))

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
            return open_real_backend(driver, device_args, sample_rate, gain_mode, gain_db)
        except Exception:
            backend = SimulatedBackend(sample_rate)
            backend.set_gain(gain_mode, gain_db)
            return backend

    # A specific real device was explicitly chosen in the GUI: if it fails to
    # open, that's worth surfacing loudly (engine stops, clear log message)
    # rather than silently switching to fake data - which looks identical to
    # a working scan and is exactly what confused a user pulling the dongle.
    return open_real_backend(driver, device_args, sample_rate, gain_mode, gain_db)
