"""Analiza jakości nagrań audio.

Nagrania pochodzą ze skanera wyzwalanego squelchem: plik zawiera od momentu
otwarcia szumów do ich zamknięcia. Zwykle więc słychać tło (szum/nośną) oraz
- gdy ktoś nadaje - głos wyraźnie głośniejszy od tła. Jakość transmisji
oceniamy właśnie jako stosunek poziomu sygnału do poziomu tła (SNR), karząc
dodatkowo przesterowania (clipping) i podejrzanie krótkie "strzały" squelchu.
"""
from __future__ import annotations

import math
import wave
from pathlib import Path

import numpy as np

from smartscan.playback.models import QualityMetrics

WINDOW_SEC = 0.05          # okno do liczenia obwiedni RMS
NOISE_PERCENTILE = 15      # percentyl obwiedni traktowany jako poziom tła
SIGNAL_PERCENTILE = 92     # percentyl obwiedni traktowany jako poziom sygnału
SNR_FOR_100 = 30.0         # SNR (dB) odpowiadający wynikowi 100
CLIP_THRESHOLD_FRAC = 0.975  # próg (jako ułamek pełnej skali) uznawany za przesterowanie
CLIP_PENALTY_SCALE = 300.0
SHORT_RECORDING_SEC = 1.0
SHORT_RECORDING_PENALTY = 15.0


def _read_samples(path: Path) -> tuple[np.ndarray, int, float]:
    with wave.open(str(path), "rb") as wf:
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        framerate = wf.getframerate() or 1
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)

    duration_sec = n_frames / framerate

    if sampwidth == 2:
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float64)
        full_scale = 32768.0
    elif sampwidth == 1:
        data = np.frombuffer(raw, dtype=np.uint8).astype(np.float64) - 128.0
        full_scale = 128.0
    elif sampwidth == 4:
        data = np.frombuffer(raw, dtype=np.int32).astype(np.float64)
        full_scale = 2147483648.0
    else:
        raise ValueError(f"Nieobsługiwana głębia próbki: {sampwidth} bajtów")

    if n_channels > 1 and data.size:
        usable = (data.size // n_channels) * n_channels
        data = data[:usable].reshape(-1, n_channels).mean(axis=1)

    return data, full_scale, duration_sec


def analyze_wav(path: Path) -> QualityMetrics:
    """Liczy metryki jakości pojedynczego pliku wav."""
    try:
        data, full_scale, duration_sec = _read_samples(path)
    except Exception:
        return QualityMetrics(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, readable=False)

    if data.size == 0:
        return QualityMetrics(0.0, 0.0, 0.0, 0.0, 0.0, duration_sec)

    framerate_estimate = max(1.0, data.size / duration_sec) if duration_sec else 12500.0
    window = max(1, int(framerate_estimate * WINDOW_SEC))
    window = min(window, data.size)
    n_windows = max(1, data.size // window)
    trimmed = data[: n_windows * window]
    frames = trimmed.reshape(n_windows, window)
    rms = np.sqrt(np.mean(frames ** 2, axis=1))

    noise_floor = float(np.percentile(rms, NOISE_PERCENTILE))
    signal_level = float(np.percentile(rms, SIGNAL_PERCENTILE))

    eps = 1e-6
    snr_db = 20.0 * math.log10(max(signal_level, eps) / max(noise_floor, eps))

    clip_thresh = CLIP_THRESHOLD_FRAC * full_scale
    clipping_ratio = float(np.mean(np.abs(data) >= clip_thresh))

    snr_component = max(0.0, min(snr_db, SNR_FOR_100)) / SNR_FOR_100 * 100.0
    clip_penalty = min(clipping_ratio * CLIP_PENALTY_SCALE, 40.0)
    short_penalty = SHORT_RECORDING_PENALTY if duration_sec < SHORT_RECORDING_SEC else 0.0
    quality_score = max(0.0, min(100.0, snr_component - clip_penalty - short_penalty))

    return QualityMetrics(
        noise_floor=noise_floor,
        signal_level=signal_level,
        snr_db=snr_db,
        clipping_ratio=clipping_ratio,
        quality_score=quality_score,
        duration_sec=duration_sec,
    )
