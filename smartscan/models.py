"""Data models shared across SmartScanATC (scanner states, channels, blacklist entries)."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ScannerState(str, Enum):
    IDLE = "IDLE"
    CALCULATE_NEXT_FREQ = "CALCULATE_NEXT_FREQ"
    TUNING = "TUNING"
    SCANNING = "SCANNING"
    SIGNAL_DETECTED = "SIGNAL_DETECTED"
    LINGER = "LINGER"
    RECONNECTING = "RECONNECTING"


class ScanMode(str, Enum):
    VFO_RANGE = "vfo_range"
    MEMORY_LIST = "memory_list"


class RecordMode(str, Enum):
    SINGLE_FILE = "single"
    SPLIT_FILES = "split"


class Modulation(str, Enum):
    AM = "AM"
    NFM = "NFM"
    WFM = "WFM"


# Audio-path states in which the recorder/speaker are allowed to be live.
AUDIO_ACTIVE_STATES = frozenset({ScannerState.SIGNAL_DETECTED, ScannerState.LINGER})


@dataclass
class Channel:
    freq_hz: int
    name: str = ""
    modulation: Modulation = Modulation.AM
    enabled: bool = True
    folder: str = ""  # Memory List folder this channel is filed under; "" = top level (no folder)
    # Scan priority weight - 0 (default) means "scan in the usual round-robin
    # order, no special treatment". Anything >0 makes the scanner visit this
    # frequency proportionally more often than other items of the same kind,
    # relative to their own weights (see scanner_engine._WeightedScheduler).
    weight: float = 0.0

    def to_row(self) -> list[str]:
        return [
            str(self.freq_hz), self.name, self.modulation.value, "1" if self.enabled else "0", self.folder,
            repr(self.weight),
        ]

    @staticmethod
    def from_row(row: list[str]) -> "Channel":
        freq_hz = int(float(row[0]))
        name = row[1] if len(row) > 1 else ""
        modulation = Modulation(row[2]) if len(row) > 2 and row[2] in Modulation._value2member_map_ else Modulation.AM
        enabled = row[3] != "0" if len(row) > 3 else True
        folder = row[4] if len(row) > 4 else ""
        try:
            weight = float(row[5]) if len(row) > 5 else 0.0
        except ValueError:
            weight = 0.0
        return Channel(freq_hz=freq_hz, name=name, modulation=modulation, enabled=enabled, folder=folder, weight=weight)


@dataclass
class BlacklistEntry:
    freq_hz: int
    margin_hz: int = 5000
    label: str = ""
    date_added: str = ""

    def to_row(self) -> list[str]:
        return [str(self.freq_hz), str(self.margin_hz), self.label, self.date_added]

    @staticmethod
    def from_row(row: list[str]) -> "BlacklistEntry":
        freq_hz = int(float(row[0]))
        margin_hz = int(float(row[1])) if len(row) > 1 else 5000
        label = row[2] if len(row) > 2 else ""
        date_added = row[3] if len(row) > 3 else ""
        return BlacklistEntry(freq_hz=freq_hz, margin_hz=margin_hz, label=label, date_added=date_added)

    def contains(self, freq_hz: int) -> bool:
        return abs(freq_hz - self.freq_hz) <= self.margin_hz


@dataclass
class ScanStatus:
    """Lightweight snapshot pushed from the engine thread to the GUI thread."""
    state: ScannerState = ScannerState.IDLE
    freq_hz: int = 0
    level_dbfs: float = -120.0
    channel_name: str = ""
    recording: bool = False
    recording_path: str = ""
