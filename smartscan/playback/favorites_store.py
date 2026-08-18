"""Trwały zbiór ulubionych nagrań - to samo `cache_key()` (ścieżka+rozmiar+
mtime) co reszta cache'y w tym module, żeby jeden plik trzymał tylko klucze
(gwiazdka to fakt binarny, nie ma tu nic do przeliczania jak w cache jakości)."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Set

from smartscan.playback.config import FAVORITES_FILE

logger = logging.getLogger(__name__)


class FavoritesStore:
    def __init__(self, favorites_file: Path = FAVORITES_FILE):
        self.favorites_file = favorites_file
        self._keys: Set[str] = set()
        self._load()

    def _load(self) -> None:
        if self.favorites_file.exists():
            try:
                self._keys = set(json.loads(self.favorites_file.read_text()))
            except (json.JSONDecodeError, OSError):
                logger.warning("Nie udało się wczytać listy ulubionych, zaczynam od nowa")
                self._keys = set()

    def save(self) -> None:
        try:
            self.favorites_file.write_text(json.dumps(sorted(self._keys)))
        except OSError as exc:
            logger.warning("Nie udało się zapisać listy ulubionych: %s", exc)

    def is_favorite(self, key: str) -> bool:
        return key in self._keys

    def set_favorite(self, key: str, favorite: bool) -> None:
        if favorite:
            self._keys.add(key)
        else:
            self._keys.discard(key)
        self.save()

    def remove(self, key: str) -> None:
        """Usuwa pojedynczy wpis (np. tuż po ręcznym skasowaniu nagrania przez użytkownika)."""
        if key in self._keys:
            self._keys.discard(key)
            self.save()
