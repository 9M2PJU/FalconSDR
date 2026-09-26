"""Persistent set of favorite recordings - the same `cache_key()`
(path+size+mtime) as the other caches in this module, so one file just holds
keys (a star is a binary fact, there's nothing to recompute like in the
quality cache)."""
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
                logger.warning("Failed to load the favorites list, starting fresh")
                self._keys = set()

    def save(self) -> None:
        try:
            self.favorites_file.write_text(json.dumps(sorted(self._keys)))
        except OSError as exc:
            logger.warning("Failed to save the favorites list: %s", exc)

    def is_favorite(self, key: str) -> bool:
        return key in self._keys

    def set_favorite(self, key: str, favorite: bool) -> None:
        if favorite:
            self._keys.add(key)
        else:
            self._keys.discard(key)
        self.save()

    def remove(self, key: str) -> None:
        """Removes a single entry (e.g. right after the user manually deletes a recording)."""
        if key in self._keys:
            self._keys.discard(key)
            self.save()
