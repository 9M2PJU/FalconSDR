"""Obliczanie statystyk zbioru nagrań (do zakładki Statystyki oraz panelu częstotliwości)."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

from smartscan.playback.models import Recording


@dataclass
class FrequencyStat:
    freq_khz: int
    count: int
    avg_quality: float
    total_duration_sec: float
    last_heard: datetime

    @property
    def freq_mhz(self) -> float:
        return self.freq_khz / 1000.0

    @property
    def label(self) -> str:
        return f"{self.freq_mhz:.3f} MHz"


def frequency_stats(recordings: List[Recording], use_utc: bool = False) -> List[FrequencyStat]:
    by_freq: Dict[int, List[Recording]] = defaultdict(list)
    for rec in recordings:
        by_freq[rec.freq_khz].append(rec)

    result = []
    for freq_khz, recs in by_freq.items():
        qualities = [r.quality_score for r in recs if r.quality]
        avg_q = sum(qualities) / len(qualities) if qualities else 0.0
        total_dur = sum(r.duration_sec for r in recs)
        last_heard = max((r.dt_utc if use_utc else r.dt_local) for r in recs)
        result.append(FrequencyStat(freq_khz, len(recs), avg_q, total_dur, last_heard))

    result.sort(key=lambda s: s.count, reverse=True)
    return result


def counts_per_day(recordings: List[Recording], use_utc: bool = False) -> List[tuple[str, int]]:
    counter: Counter[str] = Counter(
        (r.dt_utc if use_utc else r.dt_local).strftime("%Y-%m-%d") for r in recordings
    )
    return sorted(counter.items())


def counts_per_hour(recordings: List[Recording], use_utc: bool = False) -> List[tuple[int, int]]:
    counter: Counter[int] = Counter((r.dt_utc if use_utc else r.dt_local).hour for r in recordings)
    return [(h, counter.get(h, 0)) for h in range(24)]


@dataclass
class Summary:
    total_count: int
    total_duration_sec: float
    date_from: Optional[datetime]
    date_to: Optional[datetime]
    avg_quality: float
    busiest_freq: Optional[FrequencyStat]
    unique_frequencies: int


def summarize(recordings: List[Recording], use_utc: bool = False) -> Summary:
    if not recordings:
        return Summary(0, 0.0, None, None, 0.0, None, 0)

    qualities = [r.quality_score for r in recordings if r.quality]
    freq_st = frequency_stats(recordings, use_utc=use_utc)
    times = [(r.dt_utc if use_utc else r.dt_local) for r in recordings]
    return Summary(
        total_count=len(recordings),
        total_duration_sec=sum(r.duration_sec for r in recordings),
        date_from=min(times),
        date_to=max(times),
        avg_quality=sum(qualities) / len(qualities) if qualities else 0.0,
        busiest_freq=freq_st[0] if freq_st else None,
        unique_frequencies=len(freq_st),
    )
