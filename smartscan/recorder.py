"""Smart recorder: writes audio to disk only for transmissions the scanner
actually stops on (SIGNAL_DETECTED / LINGER), per spec section 4.3.
"""
from __future__ import annotations

import datetime as dt
import wave
from pathlib import Path
from typing import List

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
        # True once *real* (non-silence-substituted - see write_audio()'s
        # ``real`` param) audio has been written to the *current* file -
        # see end_transmission()'s "phantom recording" cleanup below. Reset
        # per-file in _open_file(), not per-session, since a session can span
        # many split-mode files.
        self._has_real_audio = False
        # Silence blocks (write_audio(..., real=False)) are held here rather
        # than written immediately - see write_audio()'s docstring for why:
        # a squelched stretch might turn out to be a genuine mid-conversation
        # pause (more real audio still coming - flushed to disk then, in
        # order) or the trailing tail of the whole transmission with nothing
        # more coming - which the user explicitly wants trimmed, not kept as
        # dead air at the end of the file. Only known which one it was in
        # hindsight, so hold the audio (bounded - at most one LINGER
        # period's worth, a handful of chunks) until either happens.
        self._pending_silence: List[np.ndarray] = []

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

    def write_audio(self, audio: np.ndarray, real: bool = True) -> None:
        """Writes ``audio`` (mono) to the current file - or, for
        ``real=False`` (silence substituted for a squelched, below-threshold
        stretch - see ScannerEngine._play_and_record_slot), holds it instead
        of writing it immediately (see self._pending_silence). A genuine
        mid-conversation pause should read as real silence when more speech
        follows - so once *real* audio arrives, any pending silence is
        flushed first, in order, ahead of it. But if the transmission ends
        (see _close_file()) before real audio ever follows a silence block,
        that pending silence is simply discarded, unwritten - the user
        explicitly wants a squelched trailing tail trimmed, not kept as dead
        air at the end of the file. Only the *middle* case should sound like
        a real pause; the end should just end."""
        if self._wav is None:
            return
        if not real:
            self._pending_silence.append(audio)
            return
        if self._pending_silence:
            for chunk in self._pending_silence:
                self._write_raw(chunk)
            self._pending_silence = []
        self._write_raw(audio)
        self._has_real_audio = True

    def end_transmission(self) -> Path | None:
        """Closes the current per-transmission file (split mode only).
        Returns the finished file's path so the caller can hand it straight
        to the Odtwarzacz library instead of waiting for a periodic rescan.

        Deletes the file and returns None instead if it never received any
        *real* audio (see write_audio()'s ``real`` param) - a real, observed
        case since Squelch (see AppConfig.squelch_enabled) started gating
        blocks below its threshold: a brief above-trigger spike can open a
        slot, then fall back below the squelch threshold before this slot is
        even serviced once (see ScannerEngine._service_active_slots/
        _play_and_record_slot), leaving the slot "open" for the full LINGER
        grace period with nothing but silence ever written. Without this,
        that produced an all-silent WAV that still got catalogued into the
        Odtwarzacz library as a genuine recording - pure clutter with
        nothing to hear."""
        if self.config.mode != "split":
            return None
        path = self._current_path
        had_real_audio = self._has_real_audio
        self._close_file()
        if path is not None and not had_real_audio:
            try:
                path.unlink()
            except OSError:
                pass
            return None
        return path

    # -- internals ---------------------------------------------------
    def _single_file_path(self) -> Path:
        stamp = dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        return Path(self.config.output_dir) / f"{stamp}_session.wav"

    @staticmethod
    def rename_session_file_with_frequency(path: Path, freq_hz: int) -> Path | None:
        """Renames an already-closed "..._session.<ext>" file (single/manual
        mode - see _single_file_path()) to "..._<freq_khz>.<ext>", the same
        naming split-mode auto-recordings use - see
        ScannerEngine.stop_manual_recording(), the only caller: once a
        manual recording is known to have actually captured a real
        frequency (tracked separately - a closed file has no metadata of
        its own to inspect), it should read as a normal frequency-tagged
        recording in the Odtwarzacz library instead of the generic "Manual
        recording" fallback (models.MANUAL_FREQ_KHZ) that name would
        otherwise parse to. Left in the output root rather than moved into
        a date subfolder here - organizer.organize() already does that for
        any recognized loose filename (frequency-tagged or "_session") on
        the next library refresh, so this doesn't need to duplicate that.
        Returns None (leaving the original "_session" name in place) if the
        name doesn't actually contain that marker, or on any filesystem
        error - a failed rename must never be treated as the recording
        itself having failed."""
        khz = round(freq_hz / 1000)
        new_name = path.name.replace("_session", f"_{khz}")
        if new_name == path.name:
            return None
        new_path = path.with_name(new_name)
        try:
            path.rename(new_path)
        except OSError:
            return None
        return new_path

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
        self._has_real_audio = False
        self._pending_silence = []

    def _write_raw(self, audio: np.ndarray) -> None:
        pcm = np.clip(audio, -1.0, 1.0)
        pcm = (pcm * 32767.0).astype(np.int16)
        self._wav.writeframes(pcm.tobytes())

    def _close_file(self) -> None:
        if self._wav is None:
            return
        # Any not-yet-flushed silence (write_audio(..., real=False)) is the
        # trailing tail of this transmission, since nothing - see there -
        # ever asked for it to be flushed by writing more real audio after
        # it. Discard rather than write: the trim the user explicitly wants.
        self._pending_silence = []
        self._wav.close()
        self._wav = None
        self._current_path = None
