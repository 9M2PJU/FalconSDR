"""Transkrypcja nagrań w tle, w osobnym wątku Qt, żeby nie blokować GUI -
ładowanie modeli Whisper i sama transkrypcja mogą trwać długo (to celowe,
patrz transcription_engine.py - liczy się jakość, nie szybkość)."""
from __future__ import annotations

import logging
import time
from typing import List, Optional

from PyQt6.QtCore import QThread, pyqtSignal

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
    item_done = pyqtSignal(str, object)   # ścieżka pliku (str), TranscriptionResult
    progress = pyqtSignal(int, int)       # zrobione, razem (tylko z tego przebiegu)
    finished_batch = pyqtSignal()

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
        # Tylko dla ręcznego "Transkrybuj ponownie" z wybranym językiem w
        # Odtwarzaczu (zawsze wtedy z dokładnie jednym nagraniem w batchu) -
        # automatyczna kolejka w tle nigdy tego nie ustawia, więc jej
        # zachowanie (detekcja języka) zostaje dokładnie takie jak było.
        self._forced_language = forced_language
        self._stop_requested = False

    def request_stop(self) -> None:
        self._stop_requested = True

    def _cached_result(self, rec: Recording) -> Optional[TranscriptionResult]:
        """self._cache.get(rec.cache_key()), tolerant of a recording whose
        plik zniknął (skasowany przez uzytkownika, przeniesiony przez
        organize()...) między zbudowaniem tej listy a teraz - potraktowane
        jak "brak wyniku w cache", nie jak twardy błąd (patrz też run())."""
        try:
            return self._cache.get(rec.cache_key())
        except OSError:
            return None

    def run(self) -> None:
        if self._ignore_cache:
            todo = list(self._recordings)
        elif self._include_errors:
            # status "error" nie liczy się jako trwały wynik - to zwykle coś
            # przejściowego (np. chwilowy brak VRAM) albo błąd środowiska, więc
            # inaczej niż "ok"/"low_confidence"/"no_speech" chcemy spróbować
            # ponownie same z siebie, bez klikania "Transkrybuj ponownie" za
            # każdym razem.
            todo = [
                r for r in self._recordings
                if (cached := self._cached_result(r)) is None or cached.status == "error"
            ]
        else:
            # Automatyczna kontynuacja zaraz po zakończonej paczce (patrz
            # playback_panel._on_transcription_batch_finished) - łapie tylko
            # naprawdę nowe nagrania, nie odpytuje ponownie tych "error" bez
            # żadnej przerwy (patrz include_errors tam).
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
                # Zaobserwowane naprawdę (crash.log/journalctl): rec.path bywa
                # skasowany/przeniesiony w trakcie transkrypcji (np. przez
                # użytkownika klikającego "Usuń" na tym samym nagraniu, albo
                # organize() z kolejnego odświeżenia) - cache_key() wtedy
                # rzuca FileNotFoundError. transcribe_recording() sam w sobie
                # już łapie własne błędy (patrz jego docstring), więc to co
                # tu ucieka to zawsze coś poza jego kontrolą. Bez tego
                # try/except wyjątek uciekał z QThread.run() nieobsłużony -
                # a PyQt6 domyślnie ubija wtedy CAŁY proces, nie tylko ten
                # wątek (potwierdzone: crash zawsze tuż po takim tracebacku
                # w logu, żadnego innego śladu błędu) - jeden zniknięty plik
                # nie może już zabijać reszty programu.
                logger.exception("Transkrypcja: pominięto %s (błąd przetwarzania)", rec.path)
            self.progress.emit(i + 1, total)
        if dirty:
            # Whatever was put() since the last periodic save (including a
            # stop mid-batch) must still land on disk - only the *frequency*
            # of saves is throttled, nothing here is allowed to be lost.
            self._cache.save()
        self.finished_batch.emit()
