"""Software-driven RF gain control for "sw_auto" gain mode - an explicit
alternative to "auto" (real hardware AGC), not a replacement for it; see
``DeviceConfig.gain_mode`` for why both are kept as user-facing options.

Real-hardware finding (RTL-SDR Blog V4, real strong FM broadcast stations):
the tuner's own onboard AGC (``gain_mode == "auto"`` - ``SoapyBackend``/
``RtlSdrBackend`` just call ``setGainMode(True)`` and leave the hardware to
it) does *not* reliably keep the ADC out of overload. Measured
directly: a 180 kHz slice of a real capture read back at +0.7 dBFS - *above*
full scale - with a locally-strong FM station and the tuner's own AGC engaged.
RTL-SDR/R820T-family tuner AGC is a coarse, slow, multi-step hardware
mechanism that reacts to average level over a fairly long window, not peak -
it was never designed to guarantee headroom the way a real closed-loop
software AGC can, and multiple simultaneously-strong stations landing in one
wideband capture (routine in a dense FM broadcast band, or several strong ATC
transmitters) makes it worse, not better, since the hardware only sees the
combined captured band's overall level, not any specific signal of interest.

Fix: "sw_auto" never touches the tuner's own AGC hardware at all - see
``ScannerEngine._maybe_apply_gain``/``_maybe_run_software_agc``, which instead
drive the device with ordinary explicit (``"manual"``) gain values,
periodically recomputed here from the actual captured IQ's own peak
amplitude. Same class of control loop SDR++ and other mature SDR software use
instead of trusting a given device's onboard AGC, and the only way to
actually guarantee headroom regardless of which SoapySDR-supported device
(gotcha 17: this app isn't RTL-SDR-specific at the SoapySDR layer) happens to
be plugged in - a good outcome here doesn't depend on how well any particular
vendor's own tuner AGC happens to behave.
"""
from __future__ import annotations

import math
import time


class SoftwareAgc:
    """Peak-tracking gain controller. One instance per backend-open session
    (see ScannerEngine._maybe_apply_gain - created fresh whenever "auto" gain
    mode is (re)entered, discarded when leaving it), fed one peak measurement
    per SCANNING tick via step().
    """

    # Target peak (linear amplitude, 0..1) for the captured IQ's own peak
    # magnitude - a real margin below full scale (1.0) so genuine signal
    # peaks, fading, or more than one strong station landing in the same
    # capture don't reach the ADC's actual clipping point, without giving up
    # so much headroom that weak signals sit needlessly close to the noise
    # floor. Real user-reported case: hardware AGC on a real antenna/location
    # measured peak at +1dBFS (genuinely overdriving) while this was -9dBFS -
    # a real ~10dB gap, part of which showed up as "software AGC sounds
    # quieter", since the demod-stage output AGC's own makeup-gain ceiling
    # (dsp.py's _AGC_TARGET_PEAK/_AGC_MIN_ENVELOPE, ~+7.2dB) can't fully
    # absorb a gap that large on its own. Raised from -9dBFS to -6dBFS to
    # close 3dB of that gap - still a real 6dB clipping margin, nowhere near
    # the hardware-AGC overdrive case that motivated this class existing
    # (gotcha 27) in the first place. -6 dBFS ~= 0.501 linear.
    TARGET_PEAK = 0.501
    TARGET_PEAK_DB = 20.0 * math.log10(TARGET_PEAK)
    # No adjustment while the measured peak is within this many dB of
    # TARGET_PEAK - without a dead zone the controller would nudge gain on
    # every single measurement, purely from ordinary signal-level noise/
    # fading, each nudge a small but real, audible/visible gain step.
    DEADZONE_DB = 3.0
    # Minimum real time between two gain changes - a hardware gain step
    # isn't instant (some tuners take tens of ms to settle) and is a
    # potential transient of its own; adjusting at most once a second keeps
    # this occasional and deliberate rather than hunting every ~30ms tick.
    MIN_ADJUST_INTERVAL_S = 1.0
    # Largest single adjustment, in dB - bounds any one step even after a
    # huge level jump (antenna reconnected, retuned onto a very different
    # window, ...) so correction happens over a couple of steps instead of
    # one large, audible jump.
    MAX_STEP_DB = 6.0
    # Below this, treat the measurement as "no real signal yet" (e.g. the
    # very first tick after opening, or a fully dead capture) rather than
    # computing a wild step_db from an almost-zero peak.
    _MIN_MEASURABLE_PEAK = 1e-6

    def __init__(self, initial_gain_db: float):
        self._gain_db = initial_gain_db
        self._last_adjust = 0.0

    @property
    def gain_db(self) -> float:
        return self._gain_db

    def step(self, peak_linear: float, gain_range_db: tuple[float, float]) -> float | None:
        """Called once per measurement with the current capture's own peak
        |IQ| amplitude (0..1, i.e. ``float(np.max(np.abs(iq)))`` over
        whatever the caller considers one tick's worth of samples). Returns
        the new gain (dB) to apply if an adjustment is due and would
        actually change anything (already clamped to ``gain_range_db`)),
        else None - the common case for most calls, since MIN_ADJUST_INTERVAL_S
        and DEADZONE_DB together mean most ticks do nothing.
        """
        now = time.monotonic()
        if now - self._last_adjust < self.MIN_ADJUST_INTERVAL_S:
            return None
        if peak_linear <= self._MIN_MEASURABLE_PEAK:
            return None
        error_db = 20.0 * math.log10(peak_linear) - self.TARGET_PEAK_DB
        if abs(error_db) <= self.DEADZONE_DB:
            return None
        step_db = max(-self.MAX_STEP_DB, min(self.MAX_STEP_DB, -error_db))
        lo, hi = gain_range_db
        new_gain = max(lo, min(hi, self._gain_db + step_db))
        if abs(new_gain - self._gain_db) < 0.05:
            return None
        self._gain_db = new_gain
        self._last_adjust = now
        return new_gain
