"""Background transcription of recordings, on its own Qt thread so the GUI
doesn't block - loading Whisper models and transcribing itself can take a
while (deliberately - see transcription_engine.py: quality matters, not speed)."""
from __future__ import annotations

import logging
import time
from typing import List, Optional

from PySide6.QtCore import QThread, Signal

from smartscan.config import TranscriptionConfig
from smartscan.playback.models import Recording, TranscriptionResult
from smartscan.playback.transcription_cache import TranscriptionCache
from smartscan.playback.transcription_engine import transcribe_recording

logger = logging.getLogger(__name__)

# TranscriptionCache.save() rewrites the *entire* cache file (json.dumps of
# every recording ever transcribed - see transcription_cache.py) - fine as an
# occasional cost, but run() used to call it after every single item, making
# a batch of n newly-transcribed files against an archive of N accumulated
# ones cost O(n*N) total disk I/O/serialization instead of O(n) - i.e. it got
# slower (per new file) the longer/more the program had already been used,
# exactly backwards from "shouldn't slow down the longer it runs". Throttling
# to at most once per this many seconds (plus always once at the very end)
# keeps the same practical durability - a crash mid-batch loses at most this
# many seconds of already-transcribed work, same order of magnitude as
# QualityCache's own debounced save in playback_panel.py - while making the
# save frequency scale with wall-clock time instead of archive size. Whisper
# transcription itself typically takes seconds per file anyway, so this
# rarely changes save frequency for a single slow (GPU-bound or CPU-bound)
# item - it only matters (positively) for a big backlog of quick/no-op items
# (e.g. mostly-cached re-scans, or very short recordings).
_SAVE_INTERVAL_S = 5.0


class TranscriptionWorker(QThread):
    item_done = Signal(str, object)   # file path (str), TranscriptionResult
    progress = Signal(int, int)       # done, total (for this run only)
    finished_batch = Signal()

    def __init__(
        self,
        recordings: List[Recording],
        config: TranscriptionConfig,
        cache: TranscriptionCache,
        ignore_cache: bool = False,
        include_errors: bool = True,
        forced_language: Optional[str] = None,
        parent=None,
    ):
        super().__init__(parent)
        self._recordings = recordings
        self._config = config
        self._cache = cache
        self._ignore_cache = ignore_cache
        self._include_errors = include_errors
        # Only for a manual "Re-transcribe" with a chosen language in
        # Odtwarzacz (always exactly one recording in the batch then) - the
        # automatic background queue never sets this, so its behavior
        # (language detection) stays exactly as it was.
        self._forced_language = forced_language
        self._stop_requested = False

    def request_stop(self) -> None:
        self._stop_requested = True

    def _cached_result(self, rec: Recording) -> Optional[TranscriptionResult]:
        """self._cache.get(rec.cache_key()), tolerant of a recording whose
        file has disappeared (deleted by the user, moved by organize()...)
        between building this list and now - treated as "no cached result",
        not a hard error (see also run())."""
        try:
            return self._cache.get(rec.cache_key())
        except OSError:
            return None

    def run(self) -> None:
        if self._ignore_cache:
            todo = list(self._recordings)
        elif self._include_errors:
            # An "error" status doesn't count as a durable result - it's
            # usually something transient (e.g. a momentary lack of VRAM) or
            # an environment issue, so unlike "ok"/"low_confidence"/
            # "no_speech" we want to retry it on our own, without clicking
            # "Re-transcribe" every time.
            todo = [
                r for r in self._recordings
                if (cached := self._cached_result(r)) is None or cached.status == "error"
            ]
        else:
            # Automatic continuation right after a finished batch (see
            # playback_panel._on_transcription_batch_finished) - only picks
            # up genuinely new recordings, doesn't re-query "error" ones
            # again with no pause at all (see include_errors there).
            todo = [r for r in self._recordings if self._cached_result(r) is None]
        total = len(todo)
        self.progress.emit(0, total)
        last_save = time.monotonic()
        dirty = False
        for i, rec in enumerate(todo):
            if self._stop_requested:
                break
            try:
                result = transcribe_recording(rec, self._config, forced_language=self._forced_language)
                self._cache.put(rec.cache_key(), result)
                dirty = True
                now = time.monotonic()
                if now - last_save >= _SAVE_INTERVAL_S:
                    self._cache.save()
                    last_save = now
                    dirty = False
                self.item_done.emit(str(rec.path), result)
            except Exception:
                # Actually observed (crash.log/journalctl): rec.path can get
                # deleted/moved mid-transcription (e.g. the user clicking
                # "Delete" on that same recording, or organize() from another
                # refresh) - cache_key() then raises FileNotFoundError.
                # transcribe_recording() already catches its own errors (see
                # its docstring), so whatever escapes here is always
                # something outside its control. Without this try/except the
                # exception would escape QThread.run() unhandled - and
                # PySide6 kills the WHOLE process by default then, not just
                # this thread (confirmed: the crash always follows right
                # after such a traceback in the log, no other trace of a
                # cause) - one disappeared file can no longer take the rest
                # of the program down with it.
                logger.exception("Transcription: skipped %s (processing error)", rec.path)
            self.progress.emit(i + 1, total)
        if dirty:
            # Whatever was put() since the last periodic save (including a
            # stop mid-batch) must still land on disk - only the *frequency*
            # of saves is throttled, nothing here is allowed to be lost.
            self._cache.save()
        self.finished_batch.emit()
