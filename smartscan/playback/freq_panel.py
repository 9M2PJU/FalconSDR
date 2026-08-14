"""Panel boczny z listą częstotliwości - klik = filtr tabeli do tej częstotliwości."""
from __future__ import annotations

from typing import List, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QComboBox, QLineEdit, QListWidget, QListWidgetItem, QVBoxLayout, QWidget

from smartscan.playback.stats import FrequencyStat, frequency_stats

ALL_ITEM_FREQ = None


class FrequencyPanel(QWidget):
    frequencySelected = pyqtSignal(object)  # int (kHz) albo None = wszystkie

    def __init__(self, parent=None):
        super().__init__(parent)
        self._recordings = []
        # freq_khz -> nazwa z Listy pamieci, zeby rozpoznane czestotliwosci
        # byly widoczne po nazwie, nie tylko po liczbie (patrz set_freq_names()).
        self._freq_names: dict[int, str] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.sort_combo = QComboBox()
        self.sort_combo.addItems(["Sortuj: liczba transmisji", "Sortuj: częstotliwość", "Sortuj: jakość"])
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

    def _rebuild(self) -> None:
        previous = self.current_frequency()
        self.list_widget.blockSignals(True)
        self.list_widget.clear()

        total = len(self._recordings)
        all_item = QListWidgetItem(f"Wszystkie  ({total})")
        all_item.setData(Qt.ItemDataRole.UserRole, ALL_ITEM_FREQ)
        self.list_widget.addItem(all_item)

        stats: List[FrequencyStat] = frequency_stats(self._recordings)
        mode = self.sort_combo.currentIndex()
        if mode == 1:
            stats.sort(key=lambda s: s.freq_khz)
        elif mode == 2:
            stats.sort(key=lambda s: s.avg_quality, reverse=True)
        # mode == 0 -> już posortowane wg liczby (frequency_stats default)

        selected_row = 0
        for i, st in enumerate(stats, start=1):
            name = self._freq_names.get(st.freq_khz)
            label = f"{name}  ({st.label})" if name else st.label
            item = QListWidgetItem(f"{label}   ({st.count}, śr. jakość {st.avg_quality:.0f})")
            item.setData(Qt.ItemDataRole.UserRole, st.freq_khz)
            self.list_widget.addItem(item)
            if previous is not None and st.freq_khz == previous:
                selected_row = i

        self.list_widget.blockSignals(False)
        self.list_widget.setCurrentRow(selected_row)
        self._apply_filter(self.search_edit.text())

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            is_all = item.data(Qt.ItemDataRole.UserRole) is ALL_ITEM_FREQ
            item.setHidden(bool(needle) and not is_all and needle not in item.text().lower())

    def current_frequency(self) -> Optional[int]:
        item = self.list_widget.currentItem()
        if item is None:
            return None
        return item.data(Qt.ItemDataRole.UserRole)

    def _on_selection_changed(self, current, _previous) -> None:
        if current is None:
            return
        self.frequencySelected.emit(current.data(Qt.ItemDataRole.UserRole))
