"""Qt table model for the recordings list: formatting, quality coloring,
filtering, and correct (numeric/time-based) column sorting."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import QModelIndex, QSortFilterProxyModel, Qt, QAbstractTableModel
from PySide6.QtGui import QColor

from smartscan.i18n import tr
from smartscan.playback.models import MANUAL_FREQ_KHZ, Recording, TranscriptionResult

COL_DATE, COL_TIME, COL_FREQ, COL_DUR, COL_SNR, COL_QUALITY, COL_SIZE, COL_TRANSCRIPT = range(8)


def _columns() -> list[str]:
    # A function, not a module-level constant - see stats.py's
    # _weekday_labels() for why (the active language is only known once
    # i18n.set_language() runs, after this module is first imported).
    return [
        tr("Date"), tr("Time"), tr("Frequency"), tr("Duration"), tr("SNR (dB)"),
        tr("Quality"), tr("Size"), tr("Transcript"),
    ]


_TRANSCRIPT_SNIPPET_LEN = 90


def _transcript_status_labels() -> dict[str, str]:
    # Labels and colors for statuses that aren't plain text - see
    # Recording.transcript_status / TRANSCRIPT_STATUSES in playback/models.py.
    return {
        "pending": tr("⏳ queued"),
        "skipped_short": tr("– too short"),
        "no_speech": tr("\U0001f507 no speech"),
        "error": tr("⚠ transcription error"),
    }


def _transcript_display(rec: Recording) -> str:
    status = rec.transcript_status
    labels = _transcript_status_labels()
    if status in labels:
        return labels[status]
    text = rec.transcript_text
    snippet = text if len(text) <= _TRANSCRIPT_SNIPPET_LEN else text[:_TRANSCRIPT_SNIPPET_LEN].rstrip() + "…"
    return f"❓ {snippet}" if status == "low_confidence" else snippet


def _transcript_color(status: str) -> Optional[QColor]:
    if status in ("no_speech", "error"):
        return QColor(210, 70, 70, 90)
    if status == "low_confidence":
        return QColor(210, 180, 60, 90)
    if status == "ok":
        return QColor(70, 180, 100, 60)
    return None

SORT_ROLE = Qt.ItemDataRole.UserRole + 1


def _format_duration(sec: float) -> str:
    if sec >= 60:
        m, s = divmod(int(round(sec)), 60)
        return f"{m}:{s:02d}"
    return f"{sec:.1f} s"


def _format_size(num_bytes: int) -> str:
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.2f} MB"
    return f"{num_bytes / 1024:.0f} KB"


def _quality_color(score: float) -> QColor:
    score = max(0.0, min(100.0, score))
    # red (poor quality) -> yellow -> green (good quality)
    if score < 50:
        t = score / 50.0
        r, g, b = 210, int(60 + t * (200 - 60)), 60
    else:
        t = (score - 50) / 50.0
        r, g, b = int(210 - t * (210 - 60)), 200, 60
    return QColor(r, g, b, 110)


class RecordingsTableModel(QAbstractTableModel):
    def __init__(self, recordings: Optional[List[Recording]] = None, parent=None):
        super().__init__(parent)
        self._recordings: List[Recording] = recordings or []
        self._use_utc = False
        # freq_khz -> name from the Memory List (Listen/Channels tab), so the
        # Frequency column shows e.g. "128.950 MHz — Downtown Tower" instead
        # of just the number, when the frequency is recognized.
        self._freq_names: dict[int, str] = {}
        # Precomputed, lowercased "search blob" per recording (see
        # search_blob()/RecordingsFilterProxy) - rebuilt only when a
        # recording's own searchable fields actually change (new file,
        # transcript arrives, channel renamed), never per keystroke. Real,
        # measured cause of a reported search lag at 18k+ recordings:
        # rebuilding this (path.name.lower() + freq_label.lower() +
        # freq_name().lower() + strftime() + transcript_text.lower(), per
        # row) on every single textChanged was 115ms/keystroke at 18101
        # rows - purely Python string work, no I/O. Looking it up here
        # instead drops that to ~5ms.
        self._search_cache: dict[Path, str] = {}

    def set_freq_names(self, freq_names: dict[int, str]) -> None:
        self._freq_names = freq_names
        if self._recordings:
            top_left = self.index(0, COL_FREQ)
            bottom_right = self.index(self.rowCount() - 1, COL_FREQ)
            self.dataChanged.emit(top_left, bottom_right, [Qt.ItemDataRole.DisplayRole])
        # Channel names feed into the search blob - a rename must be
        # reflected there too. Rare (user editing Memory List), so an O(n)
        # full rebuild here (unlike per-keystroke) is a non-issue.
        self._rebuild_search_cache()

    def _build_search_blob(self, rec: Recording) -> str:
        return " ".join(
            [
                rec.path.name.lower(),
                rec.freq_label.lower(),
                self._freq_names.get(rec.freq_khz, "").lower(),
                rec.dt_local.strftime("%Y-%m-%d %H:%M:%S"),
                rec.transcript_text.lower(),
            ]
        )

    def _rebuild_search_cache(self) -> None:
        self._search_cache = {rec.path: self._build_search_blob(rec) for rec in self._recordings}

    def search_blob(self, rec: Recording) -> str:
        """Lowercased, pre-joined text to substring-search against - see
        RecordingsFilterProxy.filterAcceptsRow(). Falls back to building it
        on the spot for a recording that somehow missed a cache-maintaining
        call below (defensive only - every real path keeps this in sync)."""
        cached = self._search_cache.get(rec.path)
        return cached if cached is not None else self._build_search_blob(rec)

    def _freq_display(self, freq_khz: int) -> str:
        if freq_khz == MANUAL_FREQ_KHZ:
            return tr("Manual recording")
        name = self._freq_names.get(freq_khz)
        label = f"{freq_khz / 1000:.3f} MHz"
        return f"{label} — {name}" if name else label

    def freq_name(self, freq_khz: int) -> str:
        return self._freq_names.get(freq_khz, "")

    # -- API ------------------------------------------------------------
    def set_recordings(self, recordings: List[Recording]) -> None:
        self.beginResetModel()
        # A copy, not a reference: PlaybackPanel keeps its own
        # self._recordings list and mutates it independently (e.g.
        # append_recording() for a new recording, filtering on delete) -
        # sharing one list caused double insertions/removals.
        self._recordings = list(recordings)
        self._rebuild_search_cache()
        self.endResetModel()

    def set_use_utc(self, use_utc: bool) -> None:
        if self._use_utc == use_utc:
            return
        self._use_utc = use_utc
        if self._recordings:
            top_left = self.index(0, COL_DATE)
            bottom_right = self.index(self.rowCount() - 1, COL_TIME)
            self.dataChanged.emit(top_left, bottom_right)

    def recording_at(self, row: int) -> Recording:
        return self._recordings[row]

    def _dt(self, rec: Recording) -> datetime:
        return rec.dt_utc if self._use_utc else rec.dt_local

    # -- QAbstractTableModel ---------------------------------------------
    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._recordings)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(_columns())

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return _columns()[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        rec = self._recordings[index.row()]
        col = index.column()

        if role in (Qt.ItemDataRole.DisplayRole, SORT_ROLE):
            dt = self._dt(rec)
            if col == COL_DATE:
                return dt.strftime("%Y-%m-%d") if role == Qt.ItemDataRole.DisplayRole else dt.timestamp()
            if col == COL_TIME:
                if role == Qt.ItemDataRole.DisplayRole:
                    suffix = " UTC" if self._use_utc else ""
                    return dt.strftime("%H:%M:%S") + suffix
                return dt.timestamp()
            if col == COL_FREQ:
                return self._freq_display(rec.freq_khz) if role == Qt.ItemDataRole.DisplayRole else rec.freq_khz
            if col == COL_DUR:
                return _format_duration(rec.duration_sec) if role == Qt.ItemDataRole.DisplayRole else rec.duration_sec
            if col == COL_SNR:
                if role == Qt.ItemDataRole.DisplayRole:
                    if rec.quality is None:
                        return "—"
                    return tr("corrupt") if not rec.quality.readable else f"{rec.snr_db:.1f}"
                if rec.quality is None:
                    return -999.0
                return -999.0 if not rec.quality.readable else rec.snr_db
            if col == COL_QUALITY:
                if role == Qt.ItemDataRole.DisplayRole:
                    if rec.quality is None:
                        return "—"
                    return tr("⚠ corrupt") if not rec.quality.readable else f"{rec.quality_score:.0f}"
                if rec.quality is None:
                    return -1.0
                return -2.0 if not rec.quality.readable else rec.quality_score
            if col == COL_SIZE:
                return _format_size(rec.size_bytes) if role == Qt.ItemDataRole.DisplayRole else rec.size_bytes
            if col == COL_TRANSCRIPT:
                if role == Qt.ItemDataRole.DisplayRole:
                    return _transcript_display(rec)
                return rec.transcript_text

        if role == Qt.ItemDataRole.TextAlignmentRole and col not in (COL_DATE, COL_TRANSCRIPT):
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter

        if role == Qt.ItemDataRole.BackgroundRole and col == COL_QUALITY and rec.quality:
            if not rec.quality.readable:
                return QColor(120, 120, 130, 90)  # gray - file unreadable, not "poor quality"
            return _quality_color(rec.quality_score)
        if role == Qt.ItemDataRole.BackgroundRole and col == COL_TRANSCRIPT:
            return _transcript_color(rec.transcript_status)

        if role == Qt.ItemDataRole.ToolTipRole:
            if col == COL_TRANSCRIPT:
                if rec.transcript is None:
                    return tr("Not transcribed yet.")
                t = rec.transcript
                tip = tr("Status: {status}\nLanguage: {language} ({probability:.0f}%)\nEngine: {engine}").format(
                    status=t.status, language=t.language, probability=t.language_probability * 100, engine=t.engine,
                )
                if t.text:
                    tip += f"\n\n{t.text}"
                return tip
            tip = str(rec.path)
            if rec.quality and not rec.quality.readable:
                tip += "\n\n" + tr("⚠ File is corrupt or unreadable - couldn't analyze the audio.")
            elif rec.quality:
                tip += tr("\nNoise floor: {noise_floor:.0f}\nSignal level: {signal_level:.0f}\nClipping: {clipping:.1f}%").format(
                    noise_floor=rec.quality.noise_floor, signal_level=rec.quality.signal_level,
                    clipping=rec.quality.clipping_ratio * 100,
                )
            return tip

        return None

    def append_recording(self, rec: Recording) -> None:
        """Inserts one freshly-finished recording without resetting the whole
        model (so it doesn't lose the current selection/scroll position)."""
        row = len(self._recordings)
        self.beginInsertRows(QModelIndex(), row, row)
        self._recordings.append(rec)
        self._search_cache[rec.path] = self._build_search_blob(rec)
        self.endInsertRows()

    def remove_recording(self, path: Path) -> Optional[Recording]:
        """Removes a recording from the model (e.g. after deleting the file
        from disk). Returns the removed Recording, or None."""
        for row, rec in enumerate(self._recordings):
            if rec.path == path:
                self.beginRemoveRows(QModelIndex(), row, row)
                del self._recordings[row]
                self._search_cache.pop(rec.path, None)
                self.endRemoveRows()
                return rec
        return None

    # -- updating a single row (a background worker finishes one recording) --
    def update_transcript(self, path: Path, result: TranscriptionResult) -> None:
        for row, rec in enumerate(self._recordings):
            if rec.path == path:
                rec.transcript = result
                self._search_cache[rec.path] = self._build_search_blob(rec)
                idx = self.index(row, COL_TRANSCRIPT)
                self.dataChanged.emit(
                    idx, idx,
                    [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.BackgroundRole, Qt.ItemDataRole.ToolTipRole],
                )
                break


# Selection keys (see freq_panel.py): a recording is shown when it matches ANY
# key in the selection - union semantics, like a multi-select in a file
# manager. An empty selection or ALL_KEY shows everything.
ALL_KEY = ("all",)
FAV_KEY = ("fav",)


def freq_key(freq_khz: int) -> tuple:
    return ("freq", freq_khz)


def band_key(start_hz: int, stop_hz: int) -> tuple:
    return ("band", start_hz, stop_hz)


def lang_key(language: str) -> tuple:
    return ("lang", language)


def _recording_language(rec) -> str:
    return rec.transcript.language if rec.transcript and rec.transcript.language else ""


class RecordingsFilterProxy(QSortFilterProxyModel):
    """Filters by selection (frequencies / bands / languages / favorites),
    date range, minimum quality and search text."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSortRole(SORT_ROLE)
        self._selection: set = set()
        self._date_from: Optional[datetime] = None
        self._date_to: Optional[datetime] = None
        self._min_quality: float = 0.0
        self._search_text: str = ""

    def source(self) -> RecordingsTableModel:
        return self.sourceModel()  # type: ignore[return-value]

    def refresh(self) -> None:
        """Re-evaluates every row's filterAcceptsRow() without any filter
        criteria actually changing - for when a row's own data changed in a
        way that could flip its accept/reject result (e.g. is_favorite
        toggled) instead of a filter setting."""
        self.invalidateRowsFilter()

    def set_selection(self, keys) -> None:
        self._selection = set(keys) if keys else set()
        self.invalidateRowsFilter()

    def set_date_range(self, dt_from: Optional[datetime], dt_to: Optional[datetime]) -> None:
        self._date_from = dt_from
        self._date_to = dt_to
        self.invalidateRowsFilter()

    def set_min_quality(self, min_quality: float) -> None:
        self._min_quality = min_quality
        self.invalidateRowsFilter()

    def set_search_text(self, text: str) -> None:
        self._search_text = text.strip().lower()
        self.invalidateRowsFilter()

    def _matches_selection(self, rec) -> bool:
        if not self._selection or ALL_KEY in self._selection:
            return True
        for key in self._selection:
            kind = key[0]
            if kind == "fav" and rec.is_favorite:
                return True
            if kind == "freq" and rec.freq_khz == key[1]:
                return True
            if kind == "band" and key[1] <= rec.freq_khz * 1000 <= key[2]:
                return True
            if kind == "lang" and _recording_language(rec) == key[1]:
                return True
        return False

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        rec = self.source().recording_at(source_row)

        if not self._matches_selection(rec):
            return False

        if self._min_quality > 0.0:
            if rec.quality is None or rec.quality_score < self._min_quality:
                return False

        if self._date_from is not None and rec.dt_local < self._date_from:
            return False
        if self._date_to is not None and rec.dt_local > self._date_to:
            return False

        if self._search_text and self._search_text not in self.source().search_blob(rec):
            return False

        return True
