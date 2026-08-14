"""Hardware abstraction over SoapySDR, with a Simulated fallback so the app
is fully testable even before a SoapySDR device/module is available.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod

import numpy as np

try:
    import SoapySDR
    from SoapySDR import SOAPY_SDR_CF32, SOAPY_SDR_RX
    HAVE_SOAPY = True
except ImportError:
    HAVE_SOAPY = False


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
        self.stream = self.device.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32)
        self.device.activateStream(self.stream)
        self._buf = np.empty(65536, dtype=np.complex64)

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
        out = np.empty(num_samples, dtype=np.complex64)
        got = 0
        deadline = time.monotonic() + 1.0
        while got < num_samples and time.monotonic() < deadline:
            chunk = min(num_samples - got, self._buf.size)
            sr = self.device.readStream(self.stream, [self._buf[:chunk]], chunk, timeoutUs=200_000)
            if sr.ret > 0:
                out[got:got + sr.ret] = self._buf[:sr.ret]
                got += sr.ret
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
