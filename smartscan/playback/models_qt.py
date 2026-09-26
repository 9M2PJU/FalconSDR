"""Model tabeli Qt dla listy nagrań: formatowanie, kolorowanie jakości,
filtrowanie i poprawne (numeryczne/czasowe) sortowanie kolumn."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import QModelIndex, QSortFilterProxyModel, Qt, QAbstractTableModel
from PySide6.QtGui import QColor

from smartscan.playback.models import Recording, TranscriptionResult

COLUMNS = ["Data", "Godzina", "Częstotliwość", "Czas trwania", "SNR (dB)", "Jakość", "Rozmiar", "Transkrypcja"]

COL_DATE, COL_TIME, COL_FREQ, COL_DUR, COL_SNR, COL_QUALITY, COL_SIZE, COL_TRANSCRIPT = range(8)

_TRANSCRIPT_SNIPPET_LEN = 90

# Etykiety i kolory dla statusów, które nie są zwykłym tekstem - patrz
# Recording.transcript_status / TRANSCRIPT_STATUSES w playback/models.py.
_TRANSCRIPT_STATUS_LABELS = {
    "pending": "⏳ w kolejce",
    "skipped_short": "– za krótkie",
    "no_speech": "🔇 brak mowy",
    "error": "⚠ błąd transkrypcji",
}


def _transcript_display(rec: Recording) -> str:
    status = rec.transcript_status
    if status in _TRANSCRIPT_STATUS_LABELS:
        return _TRANSCRIPT_STATUS_LABELS[status]
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
    # czerwony (słaba jakość) -> żółty -> zielony (dobra jakość)
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
        # freq_khz -> nazwa z Listy pamieci (zakladka Nasluch/Kanaly), zeby
        # kolumna Czestotliwosc pokazywala np. "128.950 MHz — Rzeszow Wieza"
        # zamiast samej liczby, gdy czestotliwosc jest rozpoznana.
        self._freq_names: dict[int, str] = {}

    def set_freq_names(self, freq_names: dict[int, str]) -> None:
        self._freq_names = freq_names
        if self._recordings:
            top_left = self.index(0, COL_FREQ)
            bottom_right = self.index(self.rowCount() - 1, COL_FREQ)
            self.dataChanged.emit(top_left, bottom_right, [Qt.ItemDataRole.DisplayRole])

    def _freq_display(self, freq_khz: int) -> str:
        name = self._freq_names.get(freq_khz)
        label = f"{freq_khz / 1000:.3f} MHz"
        return f"{label} — {name}" if name else label

    def freq_name(self, freq_khz: int) -> str:
        return self._freq_names.get(freq_khz, "")

    # -- API ------------------------------------------------------------
    def set_recordings(self, recordings: List[Recording]) -> None:
        self.beginResetModel()
        # Kopia, nie referencja: PlaybackPanel trzyma swoją własną listę
        # self._recordings i mutuje ją niezależnie (np. append_recording()
        # przy nowym nagraniu, filtrowanie przy usuwaniu) - dzielenie jednej
        # listy powodowało podwójne wstawienia/usunięcia.
        self._recordings = list(recordings)
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
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section]
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
                    return "uszkodzony" if not rec.quality.readable else f"{rec.snr_db:.1f}"
                if rec.quality is None:
                    return -999.0
                return -999.0 if not rec.quality.readable else rec.snr_db
            if col == COL_QUALITY:
                if role == Qt.ItemDataRole.DisplayRole:
                    if rec.quality is None:
                        return "—"
                    return "⚠ uszkodzony" if not rec.quality.readable else f"{rec.quality_score:.0f}"
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
                return QColor(120, 120, 130, 90)  # szary - plik nieczytelny, nie "słaba jakość"
            return _quality_color(rec.quality_score)
        if role == Qt.ItemDataRole.BackgroundRole and col == COL_TRANSCRIPT:
            return _transcript_color(rec.transcript_status)

        if role == Qt.ItemDataRole.ToolTipRole:
            if col == COL_TRANSCRIPT:
                if rec.transcript is None:
                    return "Nie transkrybowano jeszcze."
                t = rec.transcript
                tip = f"Status: {t.status}\nJęzyk: {t.language} ({t.language_probability * 100:.0f}%)\nSilnik: {t.engine}"
                if t.text:
                    tip += f"\n\n{t.text}"
                return tip
            tip = str(rec.path)
            if rec.quality and not rec.quality.readable:
                tip += "\n\n⚠ Plik uszkodzony lub nieczytelny - nie udało się przeanalizować audio."
            elif rec.quality:
                tip += (
                    f"\nSzum tła: {rec.quality.noise_floor:.0f}"
                    f"\nPoziom sygnału: {rec.quality.signal_level:.0f}"
                    f"\nPrzesterowanie: {rec.quality.clipping_ratio * 100:.1f}%"
                )
            return tip

        return None

    def append_recording(self, rec: Recording) -> None:
        """Wstawia jedno świeżo zakończone nagranie bez resetowania całego
        modelu (a więc bez utraty zaznaczenia/przewinięcia widoku)."""
        row = len(self._recordings)
        self.beginInsertRows(QModelIndex(), row, row)
        self._recordings.append(rec)
        self.endInsertRows()

    def remove_recording(self, path: Path) -> Optional[Recording]:
        """Usuwa nagranie z modelu (np. po skasowaniu pliku z dysku). Zwraca usunięty Recording albo None."""
        for row, rec in enumerate(self._recordings):
            if rec.path == path:
                self.beginRemoveRows(QModelIndex(), row, row)
                del self._recordings[row]
                self.endRemoveRows()
                return rec
        return None

    # -- aktualizacja pojedynczego wiersza (worker w tle kończy jedno nagranie) --
    def update_transcript(self, path: Path, result: TranscriptionResult) -> None:
        for row, rec in enumerate(self._recordings):
            if rec.path == path:
                rec.transcript = result
                idx = self.index(row, COL_TRANSCRIPT)
                self.dataChanged.emit(
                    idx, idx,
                    [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.BackgroundRole, Qt.ItemDataRole.ToolTipRole],
                )
                break


class RecordingsFilterProxy(QSortFilterProxyModel):
    """Filtruje po częstotliwości, zakresie dat, minimalnej jakości i wyszukiwanym tekście."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSortRole(SORT_ROLE)
        self._freq_khz: Optional[int] = None
        self._date_from: Optional[datetime] = None
        self._date_to: Optional[datetime] = None
        self._min_quality: float = 0.0
        self._search_text: str = ""
        self._favorites_only: bool = False
        # None = brak filtra językowego; "" (NO_LANGUAGE, patrz stats.py) =
        # tylko nagrania bez wykrytego języka; kod ISO = tylko ten język.
        # Osobny wymiar od _freq_khz - freq_panel.py czyści ten przy wyborze
        # w trybie Częstotliwości i na odwrót, więc oba filtry nigdy się
        # niespodziewanie nie łączą.
        self._language: Optional[str] = None

    def source(self) -> RecordingsTableModel:
        return self.sourceModel()  # type: ignore[return-value]

    def refresh(self) -> None:
        """Re-evaluates every row's filterAcceptsRow() without any filter
        criteria actually changing - for when a row's own data changed in a
        way that could flip its accept/reject result (e.g. is_favorite
        toggled) instead of a filter setting."""
        self.invalidateRowsFilter()

    def set_frequency_filter(self, freq_khz: Optional[int]) -> None:
        self._freq_khz = freq_khz
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

    def set_favorites_only(self, favorites_only: bool) -> None:
        self._favorites_only = favorites_only
        self.invalidateRowsFilter()

    def set_language_filter(self, language: Optional[str]) -> None:
        self._language = language
        self.invalidateRowsFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        rec = self.source().recording_at(source_row)

        if self._freq_khz is not None and rec.freq_khz != self._freq_khz:
            return False

        if self._language is not None:
            rec_lang = rec.transcript.language if rec.transcript and rec.transcript.language else ""
            if rec_lang != self._language:
                return False

        if self._favorites_only and not rec.is_favorite:
            return False

        if self._min_quality > 0.0:
            if rec.quality is None or rec.quality_score < self._min_quality:
                return False

        if self._date_from is not None and rec.dt_local < self._date_from:
            return False
        if self._date_to is not None and rec.dt_local > self._date_to:
            return False

        if self._search_text:
            haystack = " ".join(
                [
                    rec.path.name.lower(),
                    rec.freq_label.lower(),
                    self.source().freq_name(rec.freq_khz).lower(),
                    rec.dt_local.strftime("%Y-%m-%d %H:%M:%S"),
                    rec.transcript_text.lower(),
                ]
            )
            if self._search_text not in haystack:
                return False

        return True
