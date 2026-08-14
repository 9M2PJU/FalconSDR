#!/usr/bin/env python3
"""Offline A/B preview of the noise reducer against recorded WAV files.

Usage:
    ./nr_preview.py plik1.wav [plik2.wav ...] [--strength 0.6] [--outdir DIR]

For each input WAV (mono, matches the app's AUDIO_RATE=12500 Hz recordings)
writes "<name>_A_dry.wav" (unprocessed) and "<name>_B_nr.wav" (with the noise
reducer applied) next to each other in --outdir, and prints the noise-floor
reduction measured on the last 1.0 s of the file (which in this app's
recordings is squelch tail / pure noise) plus the change on the first 0.5 s
(where the actual message usually sits), so you can judge both "how much
quieter" and "did it eat the voice" from the numbers before even listening.
"""
from __future__ import annotations

import argparse
import wave
from pathlib import Path

import numpy as np

from smartscan.dsp import NoiseReducer


def load_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            raise ValueError(f"{path}: oczekiwano mono 16-bit PCM")
        fr = w.getframerate()
        raw = w.readframes(w.getnframes())
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return audio, fr


def save_wav(path: Path, audio: np.ndarray, sample_rate: int) -> None:
    pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())


def rms_dbfs(x: np.ndarray) -> float:
    if x.size == 0:
        return float("-inf")
    rms = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
    return 20.0 * np.log10(max(rms, 1e-12))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("files", nargs="+", help="nagrania .wav (mono 16-bit, jak z SmartScanATC)")
    ap.add_argument("--strength", type=float, default=0.6, help="sila redukcji 0..1 (domyslnie 0.6, jak w GUI)")
    ap.add_argument("--outdir", default=None, help="katalog na pliki wyjsciowe (domyslnie obok oryginalu)")
    args = ap.parse_args()

    print(f"{'plik':45s} {'tail RMS przed':>15s} {'tail RMS po':>13s} {'delta tail':>11s}"
          f" {'head RMS przed':>15s} {'head RMS po':>13s} {'delta head':>11s}")

    for f in args.files:
        path = Path(f)
        audio, fr = load_wav(path)
        reducer = NoiseReducer(fr)
        processed = reducer.process(audio, strength=args.strength)

        n = min(audio.size, processed.size)
        dry, wet = audio[:n], processed[:n]

        tail_n = min(n, int(1.0 * fr))
        head_n = min(n, int(0.5 * fr))
        tail_before, tail_after = rms_dbfs(dry[-tail_n:]), rms_dbfs(wet[-tail_n:])
        head_before, head_after = rms_dbfs(dry[:head_n]), rms_dbfs(wet[:head_n])

        print(f"{path.name:45s} {tail_before:14.1f}d {tail_after:12.1f}d {tail_after-tail_before:+10.1f}d"
              f" {head_before:14.1f}d {head_after:12.1f}d {head_after-head_before:+10.1f}d")

        outdir = Path(args.outdir) if args.outdir else path.parent
        outdir.mkdir(parents=True, exist_ok=True)
        save_wav(outdir / f"{path.stem}_A_dry.wav", dry, fr)
        save_wav(outdir / f"{path.stem}_B_nr.wav", wet, fr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
