"""Smart recorder: writes audio to disk only for transmissions the scanner
actually stops on (SIGNAL_DETECTED / LINGER), per spec section 4.3.
"""
from __future__ import annotations

import datetime as dt
import wave
from pathlib import Path

import numpy as np

from .config import RecordingConfig


def _marker_tone(audio_rate: int, freq_hz: float = 1200.0, duration_s: float = 0.12) -> np.ndarray:
    t = np.arange(int(audio_rate * duration_s)) / audio_rate
    tone = 0.25 * np.sin(2 * np.pi * freq_hz * t)
    fade = min(200, tone.size // 4)
    if fade:
        env = np.ones_like(tone)
        env[:fade] = np.linspace(0, 1, fade)
        env[-fade:] = np.linspace(1, 0, fade)
        tone *= env
    return tone.astype(np.float32)


class Recorder:
    def __init__(self, config: RecordingConfig, audio_rate: int):
        self.config = config
        self.audio_rate = audio_rate
        self._wav: wave.Wave_write | None = None
        self._current_path: Path | None = None
        self._session_active = False

    @property
    def current_path(self) -> str:
        return str(self._current_path) if self._current_path else ""

    @property
    def is_writing(self) -> bool:
        return self._wav is not None

    def start_session(self) -> None:
        """Called when the scanner starts running."""
        self._session_active = True
        Path(self.config.output_dir).mkdir(parents=True, exist_ok=True)
        if self.config.mode == "single":
            self._open_file(self._single_file_path())

    def stop_session(self) -> None:
        """Called when the scanner is stopped entirely."""
        self._close_file()
        self._session_active = False

    def begin_transmission(self, freq_hz: int) -> None:
        if not self._session_active:
            return
        if self.config.mode == "split":
            self._open_file(self._split_file_path(freq_hz))
        elif self._wav is not None and self.config.insert_marker:
            self._write_raw(_marker_tone(self.audio_rate))

    def write_audio(self, audio: np.ndarray) -> None:
        if self._wav is None:
            return
        self._write_raw(audio)

    def end_transmission(self) -> Path | None:
        """Closes the current per-transmission file (split mode only).
        Returns the finished file's path so the caller can hand it straight
        to the Odtwarzacz library instead of waiting for a periodic rescan.
        """
        if self.config.mode != "split":
            return None
        path = self._current_path
        self._close_file()
        return path

    # -- internals ---------------------------------------------------
    def _single_file_path(self) -> Path:
        stamp = dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        return Path(self.config.output_dir) / f"{stamp}_session.wav"

    def _split_file_path(self, freq_hz: int) -> Path:
        now = dt.datetime.now()
        # Microsecond precision, not just seconds: two separate transmissions
        # on the *same* frequency starting within the same clock-second used
        # to get an identical filename - the second begin_transmission()
        # would silently overwrite the first's (still-open-elsewhere-in-the-
        # library) file via wave.open(..., "wb"), leaving two library rows
        # pointing at what's now one truncated file. Multi-channel scanning
        # (a slot closing and immediately reopening for the same frequency
        # within the same SCANNING pass, no window change needed) made this
        # actually likely instead of a theoretical edge case. Microsecond (not
        # millisecond) resolution makes a collision not just unlikely but
        # actually impossible - no two Python statements execute within the
        # same microsecond. See FILENAME_REGEX / parse_filename() in
        # playback/ for the matching (backward-compatible) parser change.
        stamp = now.strftime("%Y-%m-%d_%H-%M-%S-") + f"{now.microsecond:06d}"
        khz = round(freq_hz / 1000)
        # Written straight into the date folder the Odtwarzacz library
        # expects (see organizer.py) - no more loose files needing a later
        # "organize" pass before they show up.
        date_dir = Path(self.config.output_dir) / now.strftime("%Y-%m-%d")
        return date_dir / f"{stamp}_{khz}.wav"

    def _open_file(self, path: Path) -> None:
        self._close_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        wav = wave.open(str(path), "wb")
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(self.audio_rate)
        self._wav = wav
        self._current_path = path

    def _write_raw(self, audio: np.ndarray) -> None:
        pcm = np.clip(audio, -1.0, 1.0)
        pcm = (pcm * 32767.0).astype(np.int16)
        self._wav.writeframes(pcm.tobytes())

    def _close_file(self) -> None:
        if self._wav is None:
            return
        self._wav.close()
        self._wav = None
        self._current_path = None
