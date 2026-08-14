"""Trwały cache wyników transkrypcji, żeby nie przepuszczać ponownie przez
Whisper nagrań, które już raz przetworzyliśmy (transkrypcja jest wielokrotnie
wolniejsza od analizy jakości, więc cache jest tu jeszcze ważniejszy niż w
QualityCache)."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict

from smartscan.playback.config import TRANSCRIPTION_CACHE_FILE
from smartscan.playback.models import TranscriptionResult

logger = logging.getLogger(__name__)


class TranscriptionCache:
    def __init__(self, cache_file: Path = TRANSCRIPTION_CACHE_FILE):
        self.cache_file = cache_file
        self._data: Dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if self.cache_file.exists():
            try:
                self._data = json.loads(self.cache_file.read_text())
            except (json.JSONDecodeError, OSError):
                logger.warning("Nie udało się wczytać cache transkrypcji, zaczynam od nowa")
                self._data = {}

    def save(self) -> None:
        try:
            self.cache_file.write_text(json.dumps(self._data, ensure_ascii=False))
        except OSError as exc:
            logger.warning("Nie udało się zapisać cache transkrypcji: %s", exc)

    def get(self, key: str) -> TranscriptionResult | None:
        raw = self._data.get(key)
        if raw is None:
            return None
        return TranscriptionResult(**raw)

    def put(self, key: str, result: TranscriptionResult) -> None:
        self._data[key] = {
            "text": result.text,
            "language": result.language,
            "language_probability": result.language_probability,
            "status": result.status,
            "engine": result.engine,
            "transcribed_at": result.transcribed_at,
        }

    def prune(self, valid_keys: set[str]) -> None:
        """Usuwa z cache wpisy dla plików, których już nie ma (przeniesione/usunięte)."""
        stale = set(self._data) - valid_keys
        for key in stale:
            del self._data[key]

    def remove(self, key: str) -> None:
        """Usuwa pojedynczy wpis (np. tuż po ręcznym skasowaniu nagrania przez użytkownika)."""
        self._data.pop(key, None)
