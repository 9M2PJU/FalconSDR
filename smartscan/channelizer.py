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

import numpy as np

# Fraction of the raw sample rate considered clean enough to use: RTL-SDR
# tuners roll off near the band edges and can show a DC/LO-leakage spur at
# the exact center, so we keep a margin at both edges.
USABLE_FRACTION = 0.9


@dataclass
class ScanWindow:
    center_hz: int
    freqs: list[int]


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
    """
    if iq.size == 0 or not freqs:
        return {f: -120.0 for f in freqs}
    n = iq.size
    window = np.hanning(n)
    win_power = float(np.sum(window ** 2))
    spectrum = np.fft.fftshift(np.fft.fft(iq * window))
    psd = (np.abs(spectrum) ** 2) / (win_power * n)
    bin_freqs = np.fft.fftshift(np.fft.fftfreq(n, d=1.0 / sample_rate))

    def _bw_for(f: int) -> float:
        return channel_bw_hz[f] if isinstance(channel_bw_hz, dict) else channel_bw_hz

    result = {}
    for f in freqs:
        offset = f - center_hz
        half_bw = _bw_for(f) / 2.0
        mask = np.abs(bin_freqs - offset) <= half_bw
        power = float(psd[mask].sum()) if mask.any() else 1e-12
        result[f] = 10.0 * np.log10(max(power, 1e-12))
    return result


def spectrum_snapshot(iq: np.ndarray, sample_rate: float, center_hz: float, num_points: int = 512) -> tuple[np.ndarray, np.ndarray]:
    """Downsampled dBFS-ish spectrum of a captured block, for the GUI's
    mini-spectrum display. Returns (freqs_hz, magnitude_db).
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
        psd = psd[:edge].reshape(num_points, -1).mean(axis=1)
        freqs = freqs[:edge].reshape(num_points, -1).mean(axis=1)
    db = 10.0 * np.log10(np.clip(psd, 1e-12, None))
    return freqs, db
