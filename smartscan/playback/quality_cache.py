"""Trwały cache wyników analizy jakości, żeby nie przeliczać jej za każdym
uruchomieniem programu dla nagrań, które się nie zmieniły."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict

from smartscan.playback.config import QUALITY_CACHE_FILE
from smartscan.playback.models import QualityMetrics, Recording

logger = logging.getLogger(__name__)


class QualityCache:
    def __init__(self, cache_file: Path = QUALITY_CACHE_FILE):
        self.cache_file = cache_file
        self._data: Dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if self.cache_file.exists():
            try:
                self._data = json.loads(self.cache_file.read_text())
            except (json.JSONDecodeError, OSError):
                logger.warning("Nie udało się wczytać cache jakości, zaczynam od nowa")
                self._data = {}

    def save(self) -> None:
        try:
            self.cache_file.write_text(json.dumps(self._data))
        except OSError as exc:
            logger.warning("Nie udało się zapisać cache jakości: %s", exc)

    def get(self, key: str) -> QualityMetrics | None:
        raw = self._data.get(key)
        if raw is None:
            return None
        return QualityMetrics(**raw)

    def put(self, key: str, quality: QualityMetrics) -> None:
        self._data[key] = {
            "noise_floor": quality.noise_floor,
            "signal_level": quality.signal_level,
            "snr_db": quality.snr_db,
            "clipping_ratio": quality.clipping_ratio,
            "quality_score": quality.quality_score,
            "duration_sec": quality.duration_sec,
            "readable": quality.readable,
        }

    def prune(self, valid_keys: set[str]) -> None:
        """Usuwa z cache wpisy dla plików, których już nie ma (przeniesione/usunięte)."""
        stale = set(self._data) - valid_keys
        for key in stale:
            del self._data[key]

    def remove(self, key: str) -> None:
        """Usuwa pojedynczy wpis (np. tuż po ręcznym skasowaniu nagrania przez użytkownika)."""
        self._data.pop(key, None)
