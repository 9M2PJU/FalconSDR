"""Audio recording quality analysis.

Recordings come from a squelch-triggered scanner: the file spans from the
moment noise opens to when it closes. So there's usually background (noise/
carrier) audible, plus - when someone's transmitting - a voice clearly
louder than that background. We score transmission quality as exactly that
ratio of signal level to background level (SNR), additionally penalizing
clipping and suspiciously short squelch "bursts".
"""
from __future__ import annotations

import io
import math
import subprocess
import wave
from pathlib import Path

import numpy as np

from smartscan.playback.models import QualityMetrics

WINDOW_SEC = 0.05          # window for computing the RMS envelope
NOISE_PERCENTILE = 15      # envelope percentile treated as the background level
SIGNAL_PERCENTILE = 92     # envelope percentile treated as the signal level
SNR_FOR_100 = 30.0         # SNR (dB) corresponding to a score of 100
CLIP_THRESHOLD_FRAC = 0.975  # threshold (as a fraction of full scale) considered clipping
CLIP_PENALTY_SCALE = 300.0
SHORT_RECORDING_SEC = 1.0
SHORT_RECORDING_PENALTY = 15.0


def _mp3_to_wav_bytes(path: Path) -> bytes:
    """Decodes an mp3 to an in-memory WAV via a system `ffmpeg` - the stdlib
    `wave` module used below can't read mp3 at all. Reuses the exact same
    ffmpeg dependency this project already requires for *writing* mp3
    (audio_transcode.py, gotcha 14) rather than pulling in a decoder
    library (e.g. PyAV) into this core, non-optional playback path - PyAV
    is only ever installed as part of the *optional* transcription extra
    (gotcha 18), and this module must work without it. Raises on any
    failure (no ffmpeg, corrupt file, ...) - the caller
    (_read_samples(), always reached through analyze_wav()'s/
    compute_waveform_peaks()'s own try/except) treats that exactly like any
    other unreadable file, same as a truncated/corrupt WAV would."""
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-f", "wav", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    )
    return proc.stdout


def _read_samples(path: Path) -> tuple[np.ndarray, int, float]:
    # ffmpeg's WAV muxer writes a normal RIFF header (correct sample rate/
    # channels/bit depth) - decoding to an in-memory WAV and handing it to
    # the exact same `wave` parsing below means mp3 needs no separate
    # sample-width/channel-averaging logic of its own.
    source = io.BytesIO(_mp3_to_wav_bytes(path)) if path.suffix.lower() == ".mp3" else str(path)
    with wave.open(source, "rb") as wf:
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        framerate = wf.getframerate() or 1
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)

    # Duration from the *actual* bytes read, not the header's declared frame
    # count: ffmpeg's WAV muxer can't know the final size in advance when
    # writing to a non-seekable pipe (the mp3-decode path above) and writes
    # a sentinel placeholder (observed: 0x7FFFFFFF) for it instead - taken
    # literally, that gave a "recording" tens of thousands of seconds long.
    # readframes() itself is correctly bounded by the real data actually
    # available regardless, so deriving duration from len(raw) is accurate
    # for both a real WAV file (matches its true frame count exactly) and a
    # piped one (the only case where the two would ever disagree).
    actual_n_frames = len(raw) // max(1, sampwidth * n_channels)
    duration_sec = actual_n_frames / framerate

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
        raise ValueError(f"Unsupported sample width: {sampwidth} bytes")

    if n_channels > 1 and data.size:
        usable = (data.size // n_channels) * n_channels
        data = data[:usable].reshape(-1, n_channels).mean(axis=1)

    return data, full_scale, duration_sec


def compute_waveform_peaks(path: Path, n_buckets: int = 400) -> np.ndarray | None:
    """Peak amplitude (0..1) per time bucket, for WaveformView's visual
    overview in Odtwarzacz (see gui/widgets.py) - lets a listener see at a
    glance where a recording is quiet/silent (e.g. a squelched gap - see
    ScannerEngine._play_and_record_slot, which writes silence rather than
    cutting the block so the file's timeline stays real) versus genuinely
    loud/quiet content, without having to play it first.

    Reuses _read_samples() (same WAV-format handling as analyze_wav()) -
    reading and reducing even a several-minutes-long recording this way is
    a handful of vectorized numpy calls, fast enough to run synchronously
    on the GUI thread right as playback starts (see
    PlaybackPanel._on_playback_started) rather than needing a background
    worker thread like the library-wide quality scan does.

    Returns None (not an all-zero array) for an unreadable/corrupt file or
    one with no samples at all, so the caller/widget can tell "no data" (->
    draws a flat placeholder line) apart from "genuinely silent throughout"
    (-> draws a flat line too, but that's a real, meaningful measurement,
    not a fallback)."""
    try:
        data, full_scale, _ = _read_samples(path)
    except Exception:
        return None
    if data.size == 0:
        return None

    n_buckets = max(1, min(n_buckets, data.size))
    edge = data.size - (data.size % n_buckets)
    if edge == 0:
        edge = data.size
        n_buckets = 1
    trimmed = np.abs(data[:edge]) / full_scale
    peaks = trimmed.reshape(n_buckets, -1).max(axis=1)
    peak_max = float(peaks.max()) if peaks.size else 0.0
    if peak_max > 1e-4:
        peaks = peaks / peak_max
    return np.clip(peaks, 0.0, 1.0).astype(np.float32)


def analyze_wav(path: Path) -> QualityMetrics:
    """Computes quality metrics for a single wav file."""
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

    # Windows of EXACT digital silence (RMS == 0.0) are squelch-substituted
    # gaps (see ScannerEngine._play_and_record_slot / Recorder.write_audio's
    # ``real=False`` silence, written so a recording's timeline stays real
    # instead of jump-cutting - see AGENTS.md gotcha 6), not real receiver
    # noise, however quiet - genuine ADC noise/thermal/quantization dither
    # essentially never makes an entire ~50ms window land on exactly 0.
    # Left in, a recording with enough squelched gaps (a real, common case -
    # any transmission with a mid-conversation pause) pushes the 15th-
    # percentile "noise floor" straight to 0.0, and dividing by that below
    # blows the SNR up to nonsense (100+ dB, quality pinned at 100) for a
    # recording that's often genuinely quiet/marginal - measure quality from
    # the real content only, exactly like a listener skipping the silent
    # parts would judge it.
    real_mask = rms > 0.0
    if not real_mask.any():
        # Every window is exact silence - only possible for genuinely
        # unreadable/all-zero data at this point (an all-squelched
        # transmission never gets this far - see Recorder's own phantom-
        # recording cleanup) - fall back to the original, unfiltered arrays
        # rather than dividing by an empty selection.
        real_mask = np.ones_like(rms, dtype=bool)
    real_rms = rms[real_mask]
    real_data = frames[real_mask].reshape(-1)

    noise_floor = float(np.percentile(real_rms, NOISE_PERCENTILE))
    signal_level = float(np.percentile(real_rms, SIGNAL_PERCENTILE))

    eps = 1e-6
    snr_db = 20.0 * math.log10(max(signal_level, eps) / max(noise_floor, eps))

    clip_thresh = CLIP_THRESHOLD_FRAC * full_scale
    clipping_ratio = float(np.mean(np.abs(real_data) >= clip_thresh)) if real_data.size else 0.0

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
