"""Optional WAV -> MP3 transcode, run only after a recording has been fully
written and closed (see recorder.py / scanner_engine.py's finish-line call
sites) - never on the real-time recording path itself, per this project's own
long-standing rule (a transcode is a whole-file operation; doing it per-chunk
would be both pointless and slow).

``ffmpeg`` is a system dependency, not a pip package (same reasoning as
SoapySDR/PyAudio - see requirements.txt), so this degrades gracefully: if
it's missing, the WAV is simply left in place rather than the recording being
lost or the scanner failing.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def transcode_to_mp3(wav_path: Path, bitrate_kbps: int = 96) -> Path | None:
    """Transcodes ``wav_path`` to an .mp3 alongside it and deletes the
    original WAV on success. Returns the new path, or ``None`` (leaving the
    WAV untouched) if ffmpeg is missing or the transcode fails for any
    reason - a failed/missing transcode should never lose the recording
    itself."""
    if not ffmpeg_available():
        return None
    mp3_path = wav_path.with_suffix(".mp3")
    # Written under a non-.mp3 name first and renamed when complete, so the
    # library (which lists *.wav/*.mp3) never shows a half-written file.
    partial = wav_path.with_suffix(".mp3.part")
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", str(wav_path),
                "-codec:a", "libmp3lame", "-b:a", f"{bitrate_kbps}k",
                "-f", "mp3", str(partial),
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )
        partial.replace(mp3_path)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        partial.unlink(missing_ok=True)
        return None
    try:
        wav_path.unlink()
    except OSError:
        pass
    return mp3_path
