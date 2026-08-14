"""Wczytywanie/odświeżanie biblioteki nagrań w osobnym wątku, żeby GUI się nie zacinało."""
from __future__ import annotations

from pathlib import Path
from typing import List

from PyQt6.QtCore import QThread, pyqtSignal

from smartscan.playback.config import SMARTSCAN_DIR
from smartscan.playback.library import attach_quality, scan_recordings
from smartscan.playback.models import Recording
from smartscan.playback.organizer import organize
from smartscan.playback.quality_cache import QualityCache


class RefreshWorker(QThread):
    progress = pyqtSignal(int, int)
    finished_ok = pyqtSignal(list, int)
    failed = pyqtSignal(str)

    def __init__(self, root: Path = SMARTSCAN_DIR, parent=None):
        super().__init__(parent)
        self.root = root

    def run(self) -> None:
        try:
            moved = organize(self.root)
            recordings: List[Recording] = scan_recordings(self.root)
            cache = QualityCache()
            attach_quality(recordings, cache, progress_cb=lambda i, t: self.progress.emit(i, t))
            self.finished_ok.emit(recordings, len(moved))
        except Exception as exc:  # noqa: BLE001 - chcemy pokazać błąd w GUI
            self.failed.emit(str(exc))
