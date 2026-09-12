"""Wideband channelization: plan capture windows that cover many channels at
once (RTL-SDR can capture ~2.4 MHz in one shot), then measure per-channel
power from a single FFT per block instead of retuning channel-by-channel.

This is what lets "skanuj szeroko" work: instead of hopping the tuner across
every 25 kHz/8.33 kHz step in the range (hundreds of retunes), the whole
118-136 MHz airband typically collapses into a handful of 2.4 MHz windows -
often just one if the memory list is narrow enough - and every channel in a
window is watched simultaneously.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from .config import VfoRangeConfig

# Fraction of the raw sample rate considered clean enough to use: RTL-SDR
# tuners roll off near the band edges and can show a DC/LO-leakage spur at
# the exact center, so we keep a margin at both edges.
USABLE_FRACTION = 0.9


@dataclass
class ScanWindow:
    center_hz: int
    freqs: list[int]
    # Which configured band this window was built purely from, if any - None
    # for windows from the pooled default lane spanning more than one band,
    # from Memory List channels, or from weighted-channel "wide anchor"
    # windows. Set by ScannerEngine._rebuild_windows() after the fact (not
    # here - plan_windows() itself is band-agnostic, just groups frequencies)
    # so ScannerEngine._effective_*() can resolve a band's "advanced"
    # overrides (trigger level, timing, gain, noise reduction) per window
    # instead of only ever using the global Scanner-tab settings.
    band: "VfoRangeConfig | None" = None


def plan_windows(freqs: list[int], sample_rate_hz: float, channel_bw_hz: float) -> list[ScanWindow]:
    """Greedily group a sorted set of channel frequencies into the fewest
    capture windows such that every channel in a window fits within the
    usable capture bandwidth around that window's center frequency.
    """
    if not freqs:
        return []
    usable_bw = sample_rate_hz * USABLE_FRACTION
    ordered = sorted(set(freqs))
    windows: list[ScanWindow] = []
    group: list[int] = [ordered[0]]

    def _flush(members: list[int]) -> None:
        lo, hi = members[0], members[-1]
        center = (lo + hi) // 2
        # Nudge off the exact grid mid-point so a channel doesn't land
        # precisely on the window center (DC/LO-leakage spur territory).
        center += int(channel_bw_hz * 0.37)
        windows.append(ScanWindow(center_hz=center, freqs=list(members)))

    for f in ordered[1:]:
        span = f - group[0]
        if span + channel_bw_hz <= usable_bw:
            group.append(f)
        else:
            _flush(group)
            group = [f]
    _flush(group)
    return windows


def channel_powers_dbfs(
    iq: np.ndarray, sample_rate: float, center_hz: float, freqs: list[int],
    channel_bw_hz: float | dict[int, float],
) -> dict[int, float]:
    """One FFT for the whole captured block, then sum power in the bins
    belonging to each requested channel. Much cheaper than demodulating
    (or even just down-converting) every channel individually just to
    check its level.

    ``channel_bw_hz`` is either one bandwidth shared by every channel, or a
    per-frequency dict (band-swept channels and Memory List channels can use
    different bandwidths within the very same capture window since both are
    now watched together in one scan).

    Per-channel cost is O(log n) (binary search into the sorted FFT bin
    frequencies) + O(1) (cumulative-sum lookup), not O(n) - a naive
    `abs(bin_freqs - offset) <= half_bw` boolean mask has to touch *every*
    one of the (tens of thousands of) FFT bins for *every single channel*,
    which is fine for a handful of channels but doesn't scale: measured at
    over 100ms for a single call on a real ~2300-channel airband sweep
    (118-136.975 MHz at 8.33 kHz steps in one window) - more than 3x this
    function's entire real-time budget, every ~30ms tick, which is exactly
    what was making live audio fall further behind real time with every
    tick on a densely-stepped band. bin_freqs is monotonically sorted
    (fftshift(fftfreq(...)) always is), so np.searchsorted finds each
    channel's bin range directly instead of scanning everything.

    All ``freqs`` are searched and summed in one shot (vectorized
    searchsorted for every channel's edges at once, then each channel's
    in-band sum read off a single running cumulative sum of psd -
    `psd[lo:hi].sum() == cumsum[hi] - cumsum[lo]` for any lo/hi once cumsum
    is built) rather than one Python-level loop iteration per channel -
    measured ~7ms for 241 channels (~23% of one tick's 30ms budget) with
    the old per-channel-loop version; the per-channel *cost* was already
    O(log n) there too, but paying Python call/dict-write overhead 2-3x per
    channel every single tick added up on a densely-stepped band. Verified
    to produce results matching the old per-channel-loop version to float64
    rounding noise (~1e-9 relative) across hundreds of randomized cases
    (varied capture sizes, sample rates, shared and per-channel bandwidths,
    duplicate frequencies) before this replaced it, since this number
    directly decides which channels the scanner treats as "active" (and
    therefore what gets recorded).
    """
    if iq.size == 0 or not freqs:
        return {f: -120.0 for f in freqs}
    n = iq.size
    window = np.hanning(n)
    win_power = float(np.sum(window ** 2))
    spectrum = np.fft.fftshift(np.fft.fft(iq * window))
    psd = (np.abs(spectrum) ** 2) / (win_power * n)
    bin_freqs = np.fft.fftshift(np.fft.fftfreq(n, d=1.0 / sample_rate))

    freqs_arr = np.asarray(freqs, dtype=np.float64)
    offsets = freqs_arr - center_hz
    if isinstance(channel_bw_hz, dict):
        half_bw = np.array([channel_bw_hz[f] for f in freqs], dtype=np.float64) / 2.0
    else:
        half_bw = np.full(freqs_arr.shape, channel_bw_hz / 2.0)

    lo = np.searchsorted(bin_freqs, offsets - half_bw, side="left")
    hi = np.searchsorted(bin_freqs, offsets + half_bw, side="right")

    cumsum = np.concatenate(([0.0], np.cumsum(psd)))
    power = np.maximum(cumsum[hi] - cumsum[lo], 1e-12)
    db = 10.0 * np.log10(power)
    return dict(zip(freqs, db.tolist()))


# How far past a lane's own true edge extend_boundary_window() is willing to
# probe, as a fraction of the sample rate - a "peek at the neighbouring
# territory", not a full extra sweep even if plan_windows() left a lot of
# slack unused (e.g. a narrow custom range with a lot of room left in its
# one window).
_MAX_EXTEND_FRACTION = 0.15


def extend_boundary_window(
    window: ScanWindow, sample_rate_hz: float, step_hz: float, direction: str,
) -> list[int]:
    """Extra probe frequencies to opportunistically watch just past a lane's
    own true edge - below the lowest window's lowest channel, or above the
    highest window's highest channel (``direction`` is "below"/"above").
    Detection only: the caller decides whether/how to fold these into the
    window's own ``freqs`` (see ScannerEngine._rebuild_windows), but nothing
    here changes the window's center_hz, so this never needs an extra retune
    and never exceeds what the hardware already captures around that center
    - it only uses whatever capture room plan_windows() left unused inside
    USABLE_FRACTION (the same margin plan_windows() itself budgets against),
    capped by _MAX_EXTEND_FRACTION so a narrow, mostly-empty window doesn't
    balloon into re-sweeping most of the rest of the capture.
    """
    if not window.freqs or step_hz <= 0:
        return []
    usable_bw = sample_rate_hz * USABLE_FRACTION
    lo, hi = min(window.freqs), max(window.freqs)
    slack = usable_bw - (hi - lo + step_hz)
    extend_by = max(0.0, min(slack, sample_rate_hz * _MAX_EXTEND_FRACTION))
    n_extra = int(extend_by // step_hz)
    if n_extra <= 0:
        return []
    edge = lo if direction == "below" else hi
    sign = -1 if direction == "below" else 1
    return [int(edge + sign * step_hz * i) for i in range(1, n_extra + 1)]


def spectrum_snapshot(iq: np.ndarray, sample_rate: float, center_hz: float, num_points: int = 512) -> tuple[np.ndarray, np.ndarray]:
    """Downsampled dBFS spectrum of a captured block, for the GUI's
    mini-spectrum display. Returns (freqs_hz, magnitude_db).

    Each display point sums (not averages) the PSD of the ~n/num_points raw
    FFT bins it represents - the same "integrate power over a bandwidth"
    convention channel_powers_dbfs() uses when summing bins across one
    channel's own bandwidth for the trigger/level-meter reading. Averaging
    (the old behaviour) instead reports power *density* per single narrow
    bin, which reads dramatically lower for the same real signal purely
    because it isn't integrated over any real channel's bandwidth - measured
    at over 20 dB lower for a typical ~140-bins-per-point display at 2.4 MHz,
    which is exactly why a signal sitting well above trigger_level_dbfs used
    to visually barely clear the noise floor on this plot: the trace and the
    trigger line were drawn on the same axis but weren't actually measuring
    comparable quantities. Still not a bin-for-bin match to any specific
    channel's own bandwidth (display buckets are a fixed ~n/num_points wide,
    a real channel's bw varies 8.33/12.5 kHz by band/Memory List setting),
    but it's now the right physical quantity and in the right ballpark
    instead of off by a bin-count-dependent, always-understating constant.
    """
    if iq.size == 0:
        return np.zeros(num_points), np.full(num_points, -120.0)
    n = iq.size
    window = np.hanning(n)
    win_power = float(np.sum(window ** 2))
    spectrum = np.fft.fftshift(np.fft.fft(iq * window))
    psd = (np.abs(spectrum) ** 2) / (win_power * n)
    freqs = np.fft.fftshift(np.fft.fftfreq(n, d=1.0 / sample_rate)) + center_hz

    if n > num_points:
        edge = n - (n % num_points)
        psd = psd[:edge].reshape(num_points, -1).sum(axis=1)
        freqs = freqs[:edge].reshape(num_points, -1).mean(axis=1)
    db = 10.0 * np.log10(np.clip(psd, 1e-12, None))
    return freqs, db
