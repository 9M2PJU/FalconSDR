"""Data structures representing a single recording."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..i18n import tr

# recorder.py's "single" mode (manual recording - see scanner_engine.py's
# start_manual_recording - and the global Single/Split setting's "Single
# File" option) never ties one file to one fixed frequency the way split
# mode does (see _single_file_path() - literally "..._session.wav", no
# frequency at all), since the recorded channel can change mid-file (manual
# recording mirrors whatever's currently primary; "Single File" mode reuses
# one file across multiple detections). 0 is never a real radio frequency,
# so it's a safe, unambiguous sentinel - see organizer.parse_filename(),
# which is the only place that ever produces this value.
MANUAL_FREQ_KHZ = 0


@dataclass
class QualityMetrics:
    noise_floor: float          # background (noise) amplitude - RMS, 0-32768 scale
    signal_level: float         # signal amplitude (transmission peaks) - RMS
    snr_db: float                # how many dB the signal is above the noise
    clipping_ratio: float        # fraction of clipped samples (0-1)
    quality_score: float         # overall 0-100 score, for sorting
    duration_sec: float
    readable: bool = True        # False = file corrupt/unreadable, the metrics above are placeholder zeros


TRANSCRIPT_STATUSES = ("ok", "low_confidence", "no_speech", "error", "skipped_short")


@dataclass
class TranscriptionResult:
    text: str
    language: str                # ISO code of the detected/used language, e.g. "en", "pl"
    language_probability: float
    status: str                  # see TRANSCRIPT_STATUSES
    engine: str                  # identifier of the model used (for debugging/future changes)
    transcribed_at: str          # ISO timestamp


@dataclass
class Recording:
    path: Path
    dt_local: datetime           # local time (from the scanner), tz-aware
    dt_utc: datetime
    freq_khz: int
    quality: Optional[QualityMetrics] = None
    transcript: Optional[TranscriptionResult] = None
    is_favorite: bool = False

    @property
    def transcript_text(self) -> str:
        return self.transcript.text if self.transcript else ""

    @property
    def transcript_status(self) -> str:
        """'pending' = haven't tried transcribing yet (distinguishes it from the real statuses)."""
        return self.transcript.status if self.transcript else "pending"

    @property
    def freq_mhz(self) -> float:
        return self.freq_khz / 1000.0

    @property
    def freq_label(self) -> str:
        if self.freq_khz == MANUAL_FREQ_KHZ:
            return tr("Manual recording")
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
