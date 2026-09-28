"""DSP helpers: dBFS level detection and AM/NFM/WFM demodulation to audio.

Airband ATC voice (118-136 MHz) is amplitude-modulated, unlike most VHF land
mobile / broadcast traffic, so AM is the default demodulator here.

WFM (broadcast FM) gets its own wideband path, see Demodulator.demod(): its
+/-75 kHz deviation and ~180-200 kHz occupied bandwidth (Carson's rule) would
be filtered away to noise by decimating straight down to AUDIO_RATE's ~6 kHz
complex bandwidth *before* the discriminator ever saw the frequency swings -
that's fine for AM/NFM's much narrower deviation, but not for WFM.
"""
from __future__ import annotations

import numpy as np
from scipy.signal import butter, lfilter, resample_poly

from .models import Modulation

_EPS = 1e-12

# Complex sample rate the WFM discriminator runs at, wide enough to keep a
# +/-75 kHz deviation broadcast FM signal intact (occupied bandwidth by
# Carson's rule is ~180-200 kHz) - capped to the capture's own sample rate
# when that's narrower (e.g. a small manually-set capture bandwidth). Also
# comfortably above 2x the 53 kHz stereo subcarrier's top edge, so stereo
# decoding (see _demod_wfm_stereo) doesn't alias it.
WFM_DISCRIMINATOR_RATE = 200_000
# De-emphasis time constant: 50 us is the European/CCIR broadcast FM standard
# (the US/Americas use 75 us) - without it, FM sounds thin/harsh because the
# transmitter pre-boosts high audio frequencies expecting the receiver to
# roll them back off.
WFM_DEEMPHASIS_TAU_S = 50e-6

# Stereo FM (pilot-tone system, ITU-R BS.450): the composite discriminator
# output ("MPX" signal) carries mono L+R from 0-15 kHz, a 19 kHz pilot tone,
# and a L-R difference signal DSB-SC modulated onto a (suppressed) 38 kHz
# subcarrier - i.e. occupying 23-53 kHz. Demodulating that subcarrier needs a
# coherent 38 kHz reference locked to the pilot's phase; squaring the
# (bandpass-isolated) pilot doubles its frequency to exactly 38 kHz with the
# matching phase, which is a lightweight stand-in for a proper PLL.
WFM_PILOT_HZ = 19_000.0
WFM_MONO_CUTOFF_HZ = 15_000.0
WFM_SUBCARRIER_BAND_HZ = (23_000.0, 53_000.0)
WFM_PILOT_BAND_HZ = (WFM_PILOT_HZ - 2_000.0, WFM_PILOT_HZ + 2_000.0)
# Empirical make-up gain for the coherently-demodulated L-R baseband: squaring
# the pilot halves its amplitude into the doubled-frequency term, and the
# pilot itself only carries ~10% of the composite's peak deviation - final
# output loudness is normalized regardless, but this keeps the stereo
# separation (L-R vs L+R balance) in a sane ballpark.
WFM_STEREO_GAIN = 4.0

# Demodulator output level control (see Demodulator._apply_agc). Replaces an
# earlier version that renormalized every single ~30ms chunk independently to
# a fixed peak, using *only* that chunk's own peak - found (real-hardware
# testing, strong local FM broadcast station, RTL-SDR) to be a real, audible
# defect: any chunk whose peak happened to clear _AGC_MIN_ENVELOPE (receiver
# noise alone regularly does, briefly) got rescaled to the *exact same* fixed
# loudness as a chunk that was mostly voice/program audio - destroying the
# real amplitude difference between "signal" and "noise between words" that
# the RF chain actually delivered, audible as rapid pumping/breathing synced
# to the chunk boundary, and specifically why analyze_wav()'s SNR/quality
# score could come out badly even when the *live* dBFS reading (measured pre-
# demod, straight off the RF/FFT power) looked strong: the number that ends
# up in the recorded PCM's own noise floor had already been dragged up toward
# the signal level by this per-chunk rescaling, independent of how good the
# actual reception was. Fixed with a proper attack/release-smoothed envelope
# instead (state carried *across* calls in the Demodulator instance, reset()
# clears it) - a real AGC design, the same shape SDR++ and other mature SDR
# software use, not a peak normalizer re-run from scratch every chunk.
_AGC_TARGET_PEAK = 0.8
# Envelope floor - keeps a genuinely weak/marginal signal from being
# amplified all the way up to full scale (which would just raise its noise
# floor to be loud, not make it more intelligible) - same intent the old
# per-chunk version's `max(peak, 0.35)` had for its quiet-chunk case.
_AGC_MIN_ENVELOPE = 0.35
# Fast attack (reacts to a *rising* level within ~10ms) - short enough that a
# sudden loud transient still gets pulled down before it can clip, long
# enough (versus reacting within a single sample) to not itself audibly pump.
_AGC_ATTACK_TAU_S = 0.01
# Slow release (a falling level relaxes back over ~400ms) - deliberately much
# slower than attack (asymmetric, standard AGC/compressor design): a brief
# quiet gap between words shouldn't have its noise floor boosted back up
# before the next word arrives, which is exactly the pumping this replaces.
_AGC_RELEASE_TAU_S = 0.4
# Upper bound on the gain this can ever apply - without it, a very weak/
# marginal signal (envelope tracking down toward its floor) could otherwise
# get amplified into ear-splitting hiss during a long weak stretch; matches
# the old design's implicit cap of 0.8/0.35 ~= 2.3x for its "quiet" case,
# given some headroom since the new envelope tracks real signal more
# accurately than a single chunk's own peak did.
_AGC_MAX_GAIN = 6.0


def level_dbfs(iq: np.ndarray) -> float:
    """RMS power of a complex baseband block, expressed in dBFS (0 dB = full scale)."""
    if iq.size == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(np.abs(iq) ** 2)))
    return 20.0 * np.log10(max(rms, _EPS))


def _resample_rate(x: np.ndarray, from_rate: float, to_rate: float) -> np.ndarray:
    up = int(round(to_rate))
    down = int(round(from_rate))
    g = np.gcd(up, down)
    return resample_poly(x, up // g, down // g)


class Demodulator:
    """Stateful demodulator: converts an IQ block at `sample_rate` into audio
    at `audio_rate` (mono, or stereo for WFM when requested), maintaining
    running state (DC estimate, FM phase, filter memory) across calls so that
    a burst starting mid-block doesn't click.
    """

    def __init__(self, sample_rate: float, audio_rate: int = 12500):
        self.sample_rate = sample_rate
        self.audio_rate = audio_rate
        self._dc_zi = np.zeros(1)  # lfilter state for the AM DC-blocker, see demod()
        self._prev_phase = 0.0
        self._mix_index = 0
        self._deemph_zi = np.zeros(1)
        # Stereo MPX decode filter state (see _demod_wfm_stereo) - lazily
        # (re)built the first time it's needed, since the filter coefficients
        # depend on the discriminator's intermediate rate.
        self._stereo_rate = None
        self._pilot_ba = self._sub_ba = self._mono_ba = None
        self._pilot_zi = self._sub_zi = self._mono_zi = None
        self._lr_lpf_zi = None
        self._deemph_zi_l = np.zeros(1)
        self._deemph_zi_r = np.zeros(1)
        self._pilot_amp = 0.0
        # Smoothed level-tracking state for _apply_agc() - see the _AGC_*
        # constants' docstring for why this replaced a per-chunk peak
        # normalization. Persists across demod() calls (unlike a per-chunk
        # peak, which by definition can't carry any memory) so a single
        # quiet or loud chunk doesn't instantly swing the whole output's
        # gain - only reset alongside every other piece of this
        # demodulator's running state.
        self._agc_envelope = _AGC_MIN_ENVELOPE

    def reset(self) -> None:
        self._prev_phase = 0.0
        self._mix_index = 0
        self._dc_zi = np.zeros(1)
        self._deemph_zi = np.zeros(1)
        self._pilot_zi = self._sub_zi = self._mono_zi = None
        self._lr_lpf_zi = None
        self._deemph_zi_l = np.zeros(1)
        self._deemph_zi_r = np.zeros(1)
        self._pilot_amp = 0.0
        self._agc_envelope = _AGC_MIN_ENVELOPE

    def _decimate(self, x: np.ndarray) -> np.ndarray:
        # Channel-select + resample *before* demodulating: resample_poly's
        # anti-alias filter band-limits to ~audio_rate/2, which is what keeps
        # neighbouring channels (present in a wideband capture) out of the
        # AM envelope / FM discriminator. Demodulating first and filtering
        # after would let adjacent-channel energy leak into both. Only valid
        # for AM/NFM, whose deviation comfortably fits inside that bandwidth -
        # see _demod_wfm() for why WFM needs a different approach.
        return _resample_rate(x, self.sample_rate, self.audio_rate)

    def _deemphasize(self, audio: np.ndarray, rate: float, zi: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        dt = 1.0 / rate
        alpha = dt / (WFM_DEEMPHASIS_TAU_S + dt)
        return lfilter([alpha], [1.0, -(1.0 - alpha)], audio, zi=zi)

    def _ensure_stereo_filters(self, rate: float) -> None:
        if self._stereo_rate == rate and self._pilot_ba is not None:
            return
        self._stereo_rate = rate
        nyq = rate / 2.0
        self._pilot_ba = butter(2, [f / nyq for f in WFM_PILOT_BAND_HZ], btype="bandpass")
        self._sub_ba = butter(2, [f / nyq for f in WFM_SUBCARRIER_BAND_HZ], btype="bandpass")
        self._mono_ba = butter(2, WFM_MONO_CUTOFF_HZ / nyq, btype="lowpass")
        self._pilot_zi = np.zeros(max(len(self._pilot_ba[0]), len(self._pilot_ba[1])) - 1)
        self._sub_zi = np.zeros(max(len(self._sub_ba[0]), len(self._sub_ba[1])) - 1)
        self._mono_zi = np.zeros(max(len(self._mono_ba[0]), len(self._mono_ba[1])) - 1)
        self._lr_lpf_zi = np.zeros(max(len(self._mono_ba[0]), len(self._mono_ba[1])) - 1)

    def _demod_wfm_stereo(self, composite: np.ndarray, rate: float) -> np.ndarray:
        """Decodes L/R from the composite MPX signal via the 19 kHz pilot -
        see the module-level WFM_* constants for the standard this follows.
        Returns an (n, 2) float32 array (interleaved L, R), ready for a
        stereo audio stream.
        """
        self._ensure_stereo_filters(rate)

        main, self._mono_zi = lfilter(*self._mono_ba, composite, zi=self._mono_zi)
        pilot, self._pilot_zi = lfilter(*self._pilot_ba, composite, zi=self._pilot_zi)
        sub, self._sub_zi = lfilter(*self._sub_ba, composite, zi=self._sub_zi)

        # The pilot's amplitude is in raw discriminator (Hz-deviation) units,
        # not unity - squaring it un-normalized would blow the reference's
        # scale up by orders of magnitude and swamp the L-R recovery with
        # what's essentially noise. Track a slowly-smoothed RMS estimate and
        # normalize to a unit-amplitude reference before squaring, same idea
        # as an AGC ahead of a real PLL.
        block_rms = float(np.sqrt(np.mean(pilot * pilot))) * np.sqrt(2.0)
        self._pilot_amp = 0.9 * self._pilot_amp + 0.1 * block_rms if self._pilot_amp > 0 else block_rms
        norm_pilot = pilot / max(self._pilot_amp, _EPS)

        # Doubling the (bandpass-isolated, now unit-amplitude) pilot's
        # frequency via squaring regenerates the 38 kHz subcarrier reference
        # with the matching phase - a lightweight stand-in for a proper PLL
        # (see module docstring). Coherently demodulate the L-R subcarrier
        # with it, then lowpass to recover the L-R baseband.
        reference = norm_pilot * norm_pilot
        lr, self._lr_lpf_zi = lfilter(*self._mono_ba, sub * reference, zi=self._lr_lpf_zi)
        lr = lr * WFM_STEREO_GAIN

        left, self._deemph_zi_l = self._deemphasize(main + lr, rate, self._deemph_zi_l)
        right, self._deemph_zi_r = self._deemphasize(main - lr, rate, self._deemph_zi_r)
        if rate != self.audio_rate:
            left = _resample_rate(left, rate, self.audio_rate)
            right = _resample_rate(right, rate, self.audio_rate)
        n = min(len(left), len(right))
        return np.stack([left[:n], right[:n]], axis=-1)

    def _demod_wfm(self, iq: np.ndarray, stereo: bool) -> np.ndarray:
        # Demodulate at a wide intermediate rate so the discriminator actually
        # sees the full +/-75 kHz swing, de-emphasize, and only *then* narrow
        # down to audio_rate - that just trims already-correct audio
        # bandwidth, which (unlike narrowing the RF/FM waveform itself) is
        # perfectly safe.
        intermediate_rate = min(WFM_DISCRIMINATOR_RATE, int(round(self.sample_rate)))
        baseband = _resample_rate(iq, self.sample_rate, intermediate_rate)
        phase = np.angle(baseband)
        phase = np.unwrap(np.concatenate(([self._prev_phase], phase)))
        self._prev_phase = phase[-1]
        composite = np.clip(np.diff(phase), -3.0, 3.0)

        if stereo:
            return self._demod_wfm_stereo(composite, intermediate_rate)

        audio, self._deemph_zi = self._deemphasize(composite, intermediate_rate, self._deemph_zi)
        if intermediate_rate != self.audio_rate:
            audio = _resample_rate(audio, intermediate_rate, self.audio_rate)
        return audio

    def demod(
        self, iq: np.ndarray, modulation: Modulation, offset_hz: float = 0.0, stereo: bool = False,
    ) -> np.ndarray:
        """Returns mono audio as a 1-D array, or - only for WFM with
        ``stereo=True`` - an (n, 2) interleaved L/R array. ``stereo`` has no
        effect on AM/NFM.
        """
        if iq.size == 0:
            return np.zeros(0, dtype=np.float32)

        if offset_hz:
            # Digitally shift a single channel out of a wideband capture down
            # to baseband before demodulating it, since in wideband mode the
            # tuner sits on the *window* center, not on this specific channel.
            n = iq.size
            fs = int(round(self.sample_rate))
            t = (self._mix_index + np.arange(n)) / self.sample_rate
            iq = iq * np.exp(-2j * np.pi * offset_hz * t)
            self._mix_index = (self._mix_index + n) % fs

        if modulation == Modulation.WFM:
            audio = self._demod_wfm(iq, stereo)
        else:
            baseband = self._decimate(iq)
            if modulation == Modulation.AM:
                # DC-block the envelope: same one-pole leaky-integrator math
                # as _deemphasize() above (dc[n] = (1-alpha)*dc[n-1] +
                # alpha*v[n], out = v - dc), just expressed as an
                # a-b-filter instead of an explicit per-sample Python loop -
                # lfilter runs the same recursion in C. Measured ~26x faster
                # for a typical ~30ms/375-sample chunk, bit-identical output
                # (this *is* what the loop computed, not an approximation).
                env = np.abs(baseband)
                alpha = 0.05
                dc, self._dc_zi = lfilter([alpha], [1.0, -(1.0 - alpha)], env, zi=self._dc_zi)
                audio = env - dc
            else:
                phase = np.angle(baseband)
                phase = np.unwrap(np.concatenate(([self._prev_phase], phase)))
                self._prev_phase = phase[-1]
                audio = np.clip(np.diff(phase), -3.0, 3.0)

        audio = audio.astype(np.float32)
        audio = self._apply_agc(audio)
        return np.clip(audio, -1.0, 1.0).astype(np.float32)

    def _apply_agc(self, audio: np.ndarray) -> np.ndarray:
        """Smoothly tracks this demodulator's own output envelope (peak,
        attack/release-filtered - see the _AGC_* constants) and scales
        ``audio`` to bring that *tracked* envelope to _AGC_TARGET_PEAK,
        instead of forcing every individual chunk's own peak there
        independently - see the module-level docstring above _AGC_TARGET_PEAK
        for the real, audible pumping bug this replaced. Works unchanged on
        mono (n,) or stereo (n, 2) - a single shared gain keeps L/R balance
        intact either way."""
        if audio.size == 0:
            return audio
        peak = float(np.max(np.abs(audio)))
        dt = audio.shape[0] / self.audio_rate
        tau = _AGC_ATTACK_TAU_S if peak > self._agc_envelope else _AGC_RELEASE_TAU_S
        coef = 1.0 - np.exp(-dt / tau)
        self._agc_envelope += (peak - self._agc_envelope) * coef
        self._agc_envelope = max(self._agc_envelope, _AGC_MIN_ENVELOPE)
        gain = min(_AGC_TARGET_PEAK / self._agc_envelope, _AGC_MAX_GAIN)
        return audio * gain


class NoiseReducer:
    """Streaming spectral-gate noise reducer for demodulated audio.

    Built for a weak-antenna scenario where the receiver noise floor is
    broadband and present in *every* frame, while voice is intermittent.
    Per-bin noise power is estimated via minimum-statistics (Martin 2001):
    a short EMA tames single-frame variance, then the noise estimate is the
    minimum of that smoothed power over a trailing ~1.5 s window. A plain
    frame-to-frame min-follower doesn't work here - random receiver noise
    has enough frame-to-frame variance that it dips below "the noise floor"
    on about half of all frames by pure chance, so a follower ends up
    chasing power almost everywhere instead of settling on the true floor.
    Taking the min over many frames' worth of history fixes that. Spectral
    subtraction with an oversubtraction factor + spectral floor then
    attenuates bins close to that noise estimate while leaving voice bins
    mostly alone. Runs as 41 ms STFT frames at 75% overlap (stateful across
    calls) so the applied gain changes smoothly enough to stay click/pump-free.
    """

    FRAME_SIZE = 512
    HOP_SIZE = FRAME_SIZE // 4  # 75% overlap keeps bin-gain changes smooth
    OVER_SUBTRACTION = 2.0
    SPECTRAL_FLOOR = 0.06        # -24 dB floor: avoids gappy/gated "musical noise"
    SMOOTH_TAU_S = 0.08          # short-term power smoothing before min-tracking
    MIN_WINDOW_S = 1.5           # trailing window the noise floor is the minimum over

    def __init__(self, sample_rate: int):
        self.sample_rate = sample_rate
        from scipy.signal.windows import hann
        self._window = hann(self.FRAME_SIZE, sym=False).astype(np.float64)
        self._norm = self._measure_ola_norm()
        self._smooth_coef = self._tau_to_coef(self.SMOOTH_TAU_S)
        self._min_window_frames = max(1, round(self.MIN_WINDOW_S * self.sample_rate / self.HOP_SIZE))
        self.reset()

    def _tau_to_coef(self, tau_s: float) -> float:
        return float(np.exp(-self.HOP_SIZE / (self.sample_rate * tau_s)))

    def _measure_ola_norm(self) -> float:
        """Overlap-add of the analysis window with itself settles to a
        constant (COLA); measure it numerically instead of assuming the
        textbook constant so a future window/hop change can't go silently
        wrong.
        """
        probe_len = self.FRAME_SIZE * 6
        probe = np.zeros(probe_len)
        pos = 0
        while pos + self.FRAME_SIZE <= probe_len:
            probe[pos:pos + self.FRAME_SIZE] += self._window
            pos += self.HOP_SIZE
        return float(probe[probe_len // 2])

    def reset(self) -> None:
        self._in_buf = np.zeros(0, dtype=np.float64)
        self._ola = np.zeros(self.FRAME_SIZE, dtype=np.float64)
        self._smoothed_power: np.ndarray | None = None
        n_bins = self.FRAME_SIZE // 2 + 1
        self._min_buf = np.full((self._min_window_frames, n_bins), np.inf, dtype=np.float64)
        self._min_buf_pos = 0
        self._min_buf_filled = 0

    def process(self, audio: np.ndarray, strength: float = 1.0) -> np.ndarray:
        """Apply noise reduction; `strength` in [0, 1] blends between the
        original spectrum (0) and the fully gated one (1), so it can be
        dialled in live without clicking.
        """
        if audio.size == 0:
            return audio
        strength = float(np.clip(strength, 0.0, 1.0))
        self._in_buf = np.concatenate([self._in_buf, audio.astype(np.float64)])

        out_hops = []
        while self._in_buf.size >= self.FRAME_SIZE:
            frame = self._in_buf[: self.FRAME_SIZE]
            self._in_buf = self._in_buf[self.HOP_SIZE:]

            spec = np.fft.rfft(frame * self._window)
            power = spec.real ** 2 + spec.imag ** 2

            if self._smoothed_power is None:
                self._smoothed_power = power.copy()
            else:
                sc = self._smooth_coef
                self._smoothed_power = sc * self._smoothed_power + (1 - sc) * power

            self._min_buf[self._min_buf_pos] = self._smoothed_power
            self._min_buf_pos = (self._min_buf_pos + 1) % self._min_window_frames
            self._min_buf_filled = min(self._min_buf_filled + 1, self._min_window_frames)
            noise_power = self._min_buf[: self._min_buf_filled].min(axis=0)

            with np.errstate(divide="ignore", invalid="ignore"):
                gated_gain = 1.0 - self.OVER_SUBTRACTION * np.sqrt(
                    noise_power / np.maximum(power, 1e-20)
                )
            gated_gain = np.clip(gated_gain, self.SPECTRAL_FLOOR, 1.0)
            gain = 1.0 - strength * (1.0 - gated_gain)

            out_frame = np.fft.irfft(spec * gain, n=self.FRAME_SIZE)
            self._ola += out_frame
            out_hops.append((self._ola[: self.HOP_SIZE] / self._norm).copy())
            self._ola = np.concatenate([self._ola[self.HOP_SIZE:], np.zeros(self.HOP_SIZE)])

        if not out_hops:
            return np.zeros(0, dtype=np.float32)
        return np.clip(np.concatenate(out_hops), -1.0, 1.0).astype(np.float32)
