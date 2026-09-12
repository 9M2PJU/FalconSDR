"""Panel boczny z listą częstotliwości - klik = filtr tabeli do tej częstotliwości.

Ma też drugi tryb, przełączany prostym przełącznikiem u góry: "Języki" grupuje
te same nagrania wg wykrytej/wymuszonej transkrypcji zamiast częstotliwości -
przydatne np. do wyłapania nagrań, którym słabe audio pomyliło detekcję
języka na coś egzotycznego (patrz _resolve_language w transcription_engine.py)
i poprawienia ich ręcznie. Tryb "Częstotliwości" jest domyślny i celowo
zachowuje dokładnie swoje dotychczasowe zachowanie - _rebuild_frequency_mode()
to niezmieniona treść dawnego _rebuild()."""
from __future__ import annotations

from typing import List, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from smartscan.playback.stats import FrequencyStat, LanguageStat, frequency_stats, language_stats

ALL_ITEM_FREQ = None
# Sentinel distinguishing "show favorites only" from a real freq_khz (always
# a large positive int) or ALL_ITEM_FREQ (None) - emitted through the same
# frequencySelected signal so the sidebar stays a single-selection list
# (picking "Ulubione" naturally deselects whatever frequency was chosen, and
# vice versa, for free, via QListWidget's own single-selection behaviour).
FAVORITES_ITEM = "__favorites__"

_FREQ_SORT_OPTIONS = ["Sortuj: liczba transmisji", "Sortuj: częstotliwość", "Sortuj: jakość"]
_LANG_SORT_OPTIONS = ["Sortuj: liczba transmisji", "Sortuj: jakość", "Sortuj: język"]


class FrequencyPanel(QWidget):
    frequencySelected = pyqtSignal(object)  # int (kHz), None = wszystkie, albo FAVORITES_ITEM
    languageSelected = pyqtSignal(object)   # kod ISO, "" (NO_LANGUAGE) = bez transkrypcji, None = wszystkie

    def __init__(self, parent=None):
        super().__init__(parent)
        self._recordings = []
        # freq_khz -> nazwa z Listy pamieci, zeby rozpoznane czestotliwosci
        # byly widoczne po nazwie, nie tylko po liczbie (patrz set_freq_names()).
        self._freq_names: dict[int, str] = {}
        self._language_mode = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        mode_row = QHBoxLayout()
        self.freq_mode_btn = QPushButton("Częstotliwości")
        self.lang_mode_btn = QPushButton("Języki")
        for btn in (self.freq_mode_btn, self.lang_mode_btn):
            btn.setCheckable(True)
        self.freq_mode_btn.setChecked(True)
        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)
        self._mode_group.addButton(self.freq_mode_btn)
        self._mode_group.addButton(self.lang_mode_btn)
        self.lang_mode_btn.toggled.connect(self._on_mode_toggled)
        mode_row.addWidget(self.freq_mode_btn)
        mode_row.addWidget(self.lang_mode_btn)
        layout.addLayout(mode_row)

        self.sort_combo = QComboBox()
        self.sort_combo.addItems(_FREQ_SORT_OPTIONS)
        self.sort_combo.currentIndexChanged.connect(self._rebuild)
        layout.addWidget(self.sort_combo)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Szukaj częstotliwości...")
        self.search_edit.textChanged.connect(self._apply_filter)
        layout.addWidget(self.search_edit)

        self.list_widget = QListWidget()
        self.list_widget.currentItemChanged.connect(self._on_selection_changed)
        layout.addWidget(self.list_widget)

    def set_recordings(self, recordings) -> None:
        self._recordings = recordings
        self._rebuild()

    def set_freq_names(self, freq_names: dict[int, str]) -> None:
        self._freq_names = freq_names
        self._rebuild()

    def switch_to_frequency_mode(self) -> None:
        """Wołane z PlaybackPanel._jump_to_frequency (dwuklik w Statystykach) -
        ten skok ma sens tylko w trybie Częstotliwości, więc przełącza z
        powrotem, jeśli aktualnie pokazywane są Języki."""
        if self._language_mode:
            self.freq_mode_btn.setChecked(True)

    def _on_mode_toggled(self, checked: bool) -> None:
        self._language_mode = checked
        self.sort_combo.blockSignals(True)
        self.sort_combo.clear()
        self.sort_combo.addItems(_LANG_SORT_OPTIONS if checked else _FREQ_SORT_OPTIONS)
        self.sort_combo.blockSignals(False)
        self.search_edit.setPlaceholderText("Szukaj języka..." if checked else "Szukaj częstotliwości...")
        self._rebuild()

    def _rebuild(self) -> None:
        if self._language_mode:
            self._rebuild_language_mode()
        else:
            self._rebuild_frequency_mode()

    def _rebuild_frequency_mode(self) -> None:
        previous = self.current_frequency()
        self.list_widget.blockSignals(True)
        self.list_widget.clear()

        total = len(self._recordings)
        all_item = QListWidgetItem(f"Wszystkie  ({total})")
        all_item.setData(Qt.ItemDataRole.UserRole, ALL_ITEM_FREQ)
        self.list_widget.addItem(all_item)

        fav_count = sum(1 for r in self._recordings if r.is_favorite)
        fav_item = QListWidgetItem(f"★ Ulubione  ({fav_count})")
        fav_item.setData(Qt.ItemDataRole.UserRole, FAVORITES_ITEM)
        self.list_widget.addItem(fav_item)

        stats: List[FrequencyStat] = frequency_stats(self._recordings)
        mode = self.sort_combo.currentIndex()
        if mode == 1:
            stats.sort(key=lambda s: s.freq_khz)
        elif mode == 2:
            stats.sort(key=lambda s: s.avg_quality, reverse=True)
        # mode == 0 -> już posortowane wg liczby (frequency_stats default)

        selected_row = 1 if previous == FAVORITES_ITEM else 0
        for i, st in enumerate(stats, start=2):
            name = self._freq_names.get(st.freq_khz)
            label = f"{name}  ({st.label})" if name else st.label
            item = QListWidgetItem(f"{label}   ({st.count}, śr. jakość {st.avg_quality:.0f})")
            item.setData(Qt.ItemDataRole.UserRole, st.freq_khz)
            self.list_widget.addItem(item)
            if previous is not None and previous != FAVORITES_ITEM and st.freq_khz == previous:
                selected_row = i

        self.list_widget.blockSignals(False)
        self.list_widget.setCurrentRow(selected_row)
        self._apply_filter(self.search_edit.text())

    def _rebuild_language_mode(self) -> None:
        previous = self.current_frequency()  # w tym trybie: poprzednio wybrany kod jezyka (albo None)
        self.list_widget.blockSignals(True)
        self.list_widget.clear()

        total = len(self._recordings)
        all_item = QListWidgetItem(f"Wszystkie  ({total})")
        all_item.setData(Qt.ItemDataRole.UserRole, ALL_ITEM_FREQ)
        self.list_widget.addItem(all_item)

        stats: List[LanguageStat] = language_stats(self._recordings)
        mode = self.sort_combo.currentIndex()
        if mode == 1:
            stats.sort(key=lambda s: s.avg_quality, reverse=True)
        elif mode == 2:
            stats.sort(key=lambda s: s.label)
        # mode == 0 -> już posortowane wg liczby (language_stats default)

        selected_row = 0
        for i, st in enumerate(stats, start=1):
            item = QListWidgetItem(f"{st.label}   ({st.count}, śr. jakość {st.avg_quality:.0f})")
            item.setData(Qt.ItemDataRole.UserRole, st.language)
            self.list_widget.addItem(item)
            if previous is not None and st.language == previous:
                selected_row = i

        self.list_widget.blockSignals(False)
        self.list_widget.setCurrentRow(selected_row)
        self._apply_filter(self.search_edit.text())

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            data = item.data(Qt.ItemDataRole.UserRole)
            pinned = data is ALL_ITEM_FREQ or data == FAVORITES_ITEM
            item.setHidden(bool(needle) and not pinned and needle not in item.text().lower())

    def current_frequency(self) -> Optional[int]:
        item = self.list_widget.currentItem()
        if item is None:
            return None
        return item.data(Qt.ItemDataRole.UserRole)

    def _on_selection_changed(self, current, _previous) -> None:
        if current is None:
            return
        value = current.data(Qt.ItemDataRole.UserRole)
        if self._language_mode:
            self.languageSelected.emit(value)
        else:
            self.frequencySelected.emit(value)
