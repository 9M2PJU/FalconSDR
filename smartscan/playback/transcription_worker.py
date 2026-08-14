"""Transkrypcja nagrań w tle, w osobnym wątku Qt, żeby nie blokować GUI -
ładowanie modeli Whisper i sama transkrypcja mogą trwać długo (to celowe,
patrz transcription_engine.py - liczy się jakość, nie szybkość)."""
from __future__ import annotations

from typing import List

from PyQt6.QtCore import QThread, pyqtSignal

from smartscan.config import TranscriptionConfig
from smartscan.playback.models import Recording
from smartscan.playback.transcription_cache import TranscriptionCache
from smartscan.playback.transcription_engine import transcribe_recording


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
        parent=None,
    ):
        super().__init__(parent)
        self._recordings = recordings
        self._config = config
        self._cache = cache
        self._ignore_cache = ignore_cache
        self._include_errors = include_errors
        self._stop_requested = False

    def request_stop(self) -> None:
        self._stop_requested = True

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
                if (cached := self._cache.get(r.cache_key())) is None or cached.status == "error"
            ]
        else:
            # Automatyczna kontynuacja zaraz po zakończonej paczce (patrz
            # playback_panel._on_transcription_batch_finished) - łapie tylko
            # naprawdę nowe nagrania, nie odpytuje ponownie tych "error" bez
            # żadnej przerwy (patrz include_errors tam).
            todo = [r for r in self._recordings if self._cache.get(r.cache_key()) is None]
        total = len(todo)
        self.progress.emit(0, total)
        for i, rec in enumerate(todo):
            if self._stop_requested:
                break
            result = transcribe_recording(rec, self._config)
            self._cache.put(rec.cache_key(), result)
            self._cache.save()
            self.item_done.emit(str(rec.path), result)
            self.progress.emit(i + 1, total)
        self.finished_batch.emit()
