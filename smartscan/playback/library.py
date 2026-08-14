"""Budowanie indeksu nagrań (parsowanie nazw + doklejanie wyników analizy jakości)."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, List, Optional

from smartscan.playback.audio_analysis import analyze_wav
from smartscan.playback.config import SMARTSCAN_DIR, UTC_TZ
from smartscan.playback.models import Recording
from smartscan.playback.organizer import iter_all_recordings, parse_filename
from smartscan.playback.quality_cache import QualityCache

logger = logging.getLogger(__name__)

ProgressCallback = Optional[Callable[[int, int], None]]


def scan_recordings(root: Path = SMARTSCAN_DIR) -> List[Recording]:
    """Parsuje nazwy wszystkich plików .wav (bez liczenia jakości)."""
    recordings: List[Recording] = []
    for path in iter_all_recordings(root):
        parsed = parse_filename(path.name)
        if parsed is None:
            logger.debug("Pomijam nierozpoznany plik: %s", path)
            continue
        recordings.append(
            Recording(
                path=path,
                dt_local=parsed.dt_local,
                dt_utc=parsed.dt_local.astimezone(UTC_TZ),
                freq_khz=parsed.freq_khz,
            )
        )
    return recordings


def attach_quality(
    recordings: List[Recording],
    cache: QualityCache,
    progress_cb: ProgressCallback = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> None:
    """Uzupełnia recording.quality, korzystając z cache gdy to możliwe."""
    total = len(recordings)
    for i, rec in enumerate(recordings):
        if cancel_check is not None and cancel_check():
            return
        key = rec.cache_key()
        cached = cache.get(key)
        if cached is None:
            cached = analyze_wav(rec.path)
            cache.put(key, cached)
        rec.quality = cached
        if progress_cb is not None:
            progress_cb(i + 1, total)
    cache.prune({rec.cache_key() for rec in recordings})
    cache.save()


def load_library(
    root: Path = SMARTSCAN_DIR,
    progress_cb: ProgressCallback = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> List[Recording]:
    """Skanuje katalog i zwraca listę nagrań z uzupełnioną jakością (z cache lub przeliczoną)."""
    recordings = scan_recordings(root)
    cache = QualityCache()
    attach_quality(recordings, cache, progress_cb=progress_cb, cancel_check=cancel_check)
    return recordings
