"""Struktury danych reprezentujące pojedyncze nagranie."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional


@dataclass
class QualityMetrics:
    noise_floor: float          # amplituda tła (szumu) - RMS, skala 0-32768
    signal_level: float         # amplituda sygnału (szczyty transmisji) - RMS
    snr_db: float                # o ile dB sygnał jest ponad szumem
    clipping_ratio: float        # udział próbek przesterowanych (0-1)
    quality_score: float         # zbiorczy wynik 0-100 do sortowania
    duration_sec: float
    readable: bool = True        # False = plik uszkodzony/nieczytelny, metryki powyżej są zerami-zastępczymi


TRANSCRIPT_STATUSES = ("ok", "low_confidence", "no_speech", "error", "skipped_short")


@dataclass
class TranscriptionResult:
    text: str
    language: str                # kod ISO wykrytego/użytego języka, np. "en", "pl"
    language_probability: float
    status: str                  # patrz TRANSCRIPT_STATUSES
    engine: str                  # identyfikator użytego modelu (do debugowania/przyszłych zmian)
    transcribed_at: str          # ISO timestamp


@dataclass
class Recording:
    path: Path
    dt_local: datetime           # czas polski (ze skanera), tz-aware
    dt_utc: datetime
    freq_khz: int
    quality: Optional[QualityMetrics] = None
    transcript: Optional[TranscriptionResult] = None

    @property
    def transcript_text(self) -> str:
        return self.transcript.text if self.transcript else ""

    @property
    def transcript_status(self) -> str:
        """'pending' = jeszcze nie próbowaliśmy transkrybować (odróżnia od realnych statusów)."""
        return self.transcript.status if self.transcript else "pending"

    @property
    def freq_mhz(self) -> float:
        return self.freq_khz / 1000.0

    @property
    def freq_label(self) -> str:
        return f"{self.freq_mhz:.3f} MHz"

    @property
    def size_bytes(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    @property
    def duration_sec(self) -> float:
        if self.quality is not None:
            return self.quality.duration_sec
        return 0.0

    @property
    def snr_db(self) -> float:
        return self.quality.snr_db if self.quality else float("nan")

    @property
    def quality_score(self) -> float:
        return self.quality.quality_score if self.quality else float("nan")

    def cache_key(self) -> str:
        st = self.path.stat()
        return f"{self.path}|{st.st_size}|{int(st.st_mtime)}"
