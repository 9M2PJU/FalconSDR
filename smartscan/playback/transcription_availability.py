"""Whether faster-whisper is importable right now - the one thing the whole
optional-transcription UI (TranscriptionPanel, PlaybackPanel's notice,
TranscriptionWorker's guard) needs to agree on, so it lives in one place
instead of every caller re-implementing its own check.
"""
from __future__ import annotations

import importlib
import importlib.util


def is_transcription_installed() -> bool:
    """``find_spec`` (not a real import) - cheap, and doesn't trigger
    faster-whisper's own (non-trivial) module init just to answer "is it
    there". ``invalidate_caches()`` first: a package installed by a pip
    subprocess *after* this process started wouldn't otherwise be seen -
    Python's path-based finders cache directory listings, and nothing else
    in this process would ever normally add a new top-level package to
    site-packages while running (see transcription_install.py, whose whole
    point is to make exactly that happen without a restart when possible)."""
    importlib.invalidate_caches()
    return importlib.util.find_spec("faster_whisper") is not None
