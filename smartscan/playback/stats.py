"""Computes statistics over the recording collection (for the Statistics tab
and the frequency panel)."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

from smartscan.i18n import tr
from smartscan.playback.models import MANUAL_FREQ_KHZ, Recording


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
        if self.freq_khz == MANUAL_FREQ_KHZ:
            return tr("Manual recording")
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


# Labels for the most common languages in this kind of listening (see
# transcription_engine.py - what's actually observed is pl/en as real
# content, plus the occasional "exotic" detection misfire on weak audio).
# Anything outside this dict still displays fine (see LanguageStat.label) -
# this is just cosmetics for the most common ones, not a complete ISO 639-1
# table. Deliberately shown in each language's own native name (Deutsch,
# not "German") regardless of the UI language - the standard convention for
# a language picker/list, not something that gets translated.
_LANGUAGE_LABELS = {
    "pl": "Polski", "en": "English", "de": "Deutsch", "fr": "Français",
    "ru": "Русский", "uk": "Українська", "tr": "Türkçe", "ar": "العربية",
    "no": "Norsk", "fi": "Suomi", "sv": "Svenska", "es": "Español",
    "it": "Italiano", "nl": "Nederlands", "cs": "Čeština", "sk": "Slovenčina",
    "pt": "Português", "el": "Ελληνικά", "hu": "Magyar", "ro": "Română",
}
# Sentinel (not None - that would mean "no filter/show everything" elsewhere)
# for recordings with no detected language: no transcript at all, or a
# transcript that ended with a status carrying no language (skipped_short/error).
NO_LANGUAGE = ""


@dataclass
class LanguageStat:
    language: str  # ISO code, or NO_LANGUAGE for "no transcript"
    count: int
    avg_quality: float
    total_duration_sec: float
    last_heard: datetime

    @property
    def label(self) -> str:
        if self.language == NO_LANGUAGE:
            return tr("(no transcript)")
        return _LANGUAGE_LABELS.get(self.language, self.language.upper())


def language_stats(recordings: List[Recording], use_utc: bool = False) -> List[LanguageStat]:
    """Like frequency_stats(), but grouped by detected/forced transcript
    language instead of frequency - see freq_panel.py ("Languages" mode), for
    catching e.g. exotic languages misdetected on weak audio (see
    _resolve_language in transcription_engine.py)."""
    by_lang: Dict[str, List[Recording]] = defaultdict(list)
    for rec in recordings:
        lang = rec.transcript.language if rec.transcript and rec.transcript.language else NO_LANGUAGE
        by_lang[lang].append(rec)

    result = []
    for lang, recs in by_lang.items():
        qualities = [r.quality_score for r in recs if r.quality]
        avg_q = sum(qualities) / len(qualities) if qualities else 0.0
        total_dur = sum(r.duration_sec for r in recs)
        last_heard = max((r.dt_utc if use_utc else r.dt_local) for r in recs)
        result.append(LanguageStat(lang, len(recs), avg_q, total_dur, last_heard))

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


def _weekday_labels() -> list[str]:
    # A function, not a module-level constant: the active language (see
    # smartscan/i18n.py) is only known once set_language() runs, which
    # happens after this module is first imported - a constant evaluated at
    # import time would freeze in whatever language happened to be active
    # then (i.e. always English, since i18n.set_language() hasn't run yet).
    return [tr("Mon"), tr("Tue"), tr("Wed"), tr("Thu"), tr("Fri"), tr("Sat"), tr("Sun")]


def counts_per_weekday(recordings: List[Recording], use_utc: bool = False) -> List[tuple[str, int]]:
    """Mon..Sun, always in that order and always all 7 present (0 where
    nothing was heard) - for spotting e.g. "this one's only active on
    weekdays" at a glance, same idea as counts_per_hour."""
    labels = _weekday_labels()
    counter: Counter[int] = Counter((r.dt_utc if use_utc else r.dt_local).weekday() for r in recordings)
    return [(labels[d], counter.get(d, 0)) for d in range(7)]


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
