"""Sidebar of the Playback tab - pick what to browse, click or multi-select.

Three ways to group the same recordings, switched by the buttons at the top:
- "Frequencies": one row per frequency (default).
- "Bands": one row per band preset from the Options tab (e.g. Civil aviation
  118-137 MHz) that has recordings in it - a recording belongs to every band
  whose range contains its frequency.
- "Languages" (only when transcription is installed - see
  set_language_mode_available()): one row per detected/forced transcript
  language, useful for fixing recordings where weak audio confused detection.

Multi-select works like a file manager: Ctrl+click adds/removes one row,
Shift+click selects a range. Selecting several rows shows the union of their
recordings; "All" selected on its own (or with others) shows everything."""
from __future__ import annotations

from typing import Callable, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
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

from smartscan.i18n import tr
from smartscan.playback.stats import FrequencyStat, LanguageStat, frequency_stats, language_stats
from smartscan.playback.models_qt import ALL_KEY, FAV_KEY, band_key, freq_key, lang_key

_MODE_FREQ = "freq"
_MODE_BANDS = "bands"
_MODE_LANGS = "langs"


class FrequencyPanel(QWidget):
    # Emitted with a list of selection keys (see models_qt.py). An empty list
    # means "everything" (same as selecting All).
    selectionChanged = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._recordings = []
        # freq_khz -> name from the Memory List, so recognized frequencies
        # show up by name, not just by number (see set_freq_names()).
        self._freq_names: dict[int, str] = {}
        self._mode = _MODE_FREQ
        # Last selection we emitted - used to tell what the user just added
        # vs. what was already there (for the All-is-exclusive rule below),
        # and to restore the selection across rebuilds.
        self._last_keys: set = {ALL_KEY}
        self._syncing = False
        self._band_provider: Callable[[], list] = lambda: []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        mode_row = QHBoxLayout()
        self.freq_mode_btn = QPushButton(tr("Frequencies"))
        self.bands_mode_btn = QPushButton(tr("Bands"))
        self.lang_mode_btn = QPushButton(tr("Languages"))
        self._mode_buttons = {
            _MODE_FREQ: self.freq_mode_btn,
            _MODE_BANDS: self.bands_mode_btn,
            _MODE_LANGS: self.lang_mode_btn,
        }
        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)
        for mode, btn in self._mode_buttons.items():
            btn.setCheckable(True)
            self._mode_group.addButton(btn)
            btn.clicked.connect(lambda _checked=False, m=mode: self._set_mode(m))
            mode_row.addWidget(btn)
        self.freq_mode_btn.setChecked(True)
        # Hidden until transcription is actually installed - see
        # set_language_mode_available().
        self.lang_mode_btn.setVisible(False)
        layout.addLayout(mode_row)

        self.sort_combo = QComboBox()
        self.sort_combo.addItems(self._sort_options())
        self.sort_combo.currentIndexChanged.connect(self._rebuild)
        layout.addWidget(self.sort_combo)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(tr("Search frequencies..."))
        self.search_edit.textChanged.connect(self._apply_filter)
        layout.addWidget(self.search_edit)

        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list_widget.itemSelectionChanged.connect(self._on_selection_changed)
        layout.addWidget(self.list_widget)

    # ------------------------------------------------------------ public API
    def set_recordings(self, recordings) -> None:
        self._recordings = recordings
        self._rebuild()

    def set_freq_names(self, freq_names: dict[int, str]) -> None:
        self._freq_names = freq_names
        self._rebuild()

    def set_band_provider(self, provider: Callable[[], list]) -> None:
        """``provider()`` returns the current band presets (BandPreset list).
        Called on every rebuild rather than cached, so presets edited in
        the Options tab show up without a restart."""
        self._band_provider = provider

    def set_language_mode_available(self, available: bool) -> None:
        self.lang_mode_btn.setVisible(available)
        if not available and self._mode == _MODE_LANGS:
            self._set_mode(_MODE_FREQ)

    def switch_to_frequency_mode(self) -> None:
        self._set_mode(_MODE_FREQ)

    def select_frequency(self, freq_khz: int) -> None:
        """Selects exactly this frequency (switching to Frequencies mode
        first if needed) - used by the Statistics tab's double-click jump."""
        self._set_mode(_MODE_FREQ)
        self._select_only(freq_key(freq_khz))

    # ----------------------------------------------------------- mode/rebuild
    @staticmethod
    def _sort_options_for(mode: str) -> list[str]:
        if mode == _MODE_LANGS:
            return [tr("Sort: transmission count"), tr("Sort: quality"), tr("Sort: language")]
        return [tr("Sort: transmission count"), tr("Sort: frequency"), tr("Sort: quality")]

    def _sort_options(self) -> list[str]:
        return self._sort_options_for(self._mode)

    def _set_mode(self, mode: str) -> None:
        if self._mode == mode and self._mode_buttons[mode].isChecked():
            return
        self._mode = mode
        self._mode_buttons[mode].setChecked(True)
        self.sort_combo.blockSignals(True)
        self.sort_combo.clear()
        self.sort_combo.addItems(self._sort_options())
        self.sort_combo.blockSignals(False)
        self.search_edit.setPlaceholderText({
            _MODE_FREQ: tr("Search frequencies..."),
            _MODE_BANDS: tr("Search bands..."),
            _MODE_LANGS: tr("Search languages..."),
        }[mode])
        # A mode switch starts a fresh selection - keys from one grouping
        # don't mean anything in another.
        self._last_keys = {ALL_KEY}
        self._rebuild()

    def _rebuild(self) -> None:
        if self._mode == _MODE_LANGS:
            rows = self._language_rows()
        elif self._mode == _MODE_BANDS:
            rows = self._band_rows()
        else:
            rows = self._frequency_rows()
        self._repopulate(rows)

    def _repopulate(self, rows: list[tuple[str, tuple]]) -> None:
        """rows: (label, selection key) in display order - the All row is
        always first. Restores the previous selection where it still exists,
        falling back to All otherwise."""
        previous = set(self._last_keys)
        self._syncing = True
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        for label, key in rows:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.list_widget.addItem(item)
        self.list_widget.blockSignals(False)

        restored = False
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item.data(Qt.ItemDataRole.UserRole) in previous and previous != {ALL_KEY}:
                item.setSelected(True)
                restored = True
        if not restored:
            self.list_widget.item(0).setSelected(True)
        self._syncing = False
        self._apply_filter(self.search_edit.text())
        self._on_selection_changed()

    def _all_row(self) -> tuple[str, tuple]:
        return (tr("All") + f"  ({len(self._recordings)})", ALL_KEY)

    def _favorites_row(self) -> tuple[str, tuple]:
        fav_count = sum(1 for r in self._recordings if r.is_favorite)
        return (tr("★ Favorites") + f"  ({fav_count})", FAV_KEY)

    def _frequency_rows(self) -> list[tuple[str, tuple]]:
        rows = [self._all_row(), self._favorites_row()]
        stats: List[FrequencyStat] = frequency_stats(self._recordings)
        mode = self.sort_combo.currentIndex()
        if mode == 1:
            stats.sort(key=lambda s: s.freq_khz)
        elif mode == 2:
            stats.sort(key=lambda s: s.avg_quality, reverse=True)
        # mode == 0 -> already sorted by count (frequency_stats default)
        for st in stats:
            name = self._freq_names.get(st.freq_khz)
            label = f"{name}  ({st.label})" if name else st.label
            rows.append((f"{label}   ({st.count}, {tr('avg. quality')} {st.avg_quality:.0f})",
                         freq_key(st.freq_khz)))
        return rows

    def _band_rows(self) -> list[tuple[str, tuple]]:
        rows = [self._all_row(), self._favorites_row()]
        bands = []
        for preset in self._band_provider():
            members = [
                r for r in self._recordings
                if preset.start_hz <= r.freq_khz * 1000 <= preset.stop_hz
            ]
            if not members:
                continue
            avg_q = sum(r.quality_score for r in members if r.quality is not None)
            with_q = sum(1 for r in members if r.quality is not None)
            avg_quality = avg_q / with_q if with_q else 0.0
            bands.append((preset, len(members), avg_quality))
        mode = self.sort_combo.currentIndex()
        if mode == 1:
            bands.sort(key=lambda b: b[0].start_hz)
        elif mode == 2:
            bands.sort(key=lambda b: b[2], reverse=True)
        else:
            bands.sort(key=lambda b: b[1], reverse=True)
        for preset, count, avg_quality in bands:
            rows.append((f"{preset.label}   ({count}, {tr('avg. quality')} {avg_quality:.0f})",
                         band_key(preset.start_hz, preset.stop_hz)))
        return rows

    def _language_rows(self) -> list[tuple[str, tuple]]:
        rows = [self._all_row()]
        stats: List[LanguageStat] = language_stats(self._recordings)
        mode = self.sort_combo.currentIndex()
        if mode == 1:
            stats.sort(key=lambda s: s.avg_quality, reverse=True)
        elif mode == 2:
            stats.sort(key=lambda s: s.label)
        for st in stats:
            rows.append((f"{st.label}   ({st.count}, {tr('avg. quality')} {st.avg_quality:.0f})",
                         lang_key(st.language)))
        return rows

    # ----------------------------------------------------------- selection
    def _select_only(self, key: tuple) -> None:
        self._syncing = True
        self.list_widget.blockSignals(True)
        self.list_widget.clearSelection()
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == key:
                item.setSelected(True)
                self.list_widget.setCurrentItem(item)
                break
        self.list_widget.blockSignals(False)
        self._syncing = False
        self._on_selection_changed()

    def _selected_keys(self) -> set:
        return {item.data(Qt.ItemDataRole.UserRole) for item in self.list_widget.selectedItems()}

    def _on_selection_changed(self) -> None:
        if self._syncing:
            return
        keys = self._selected_keys()
        if not keys:
            # Nothing selected at all reads as "everything" - keep All lit
            # rather than leaving the list visually empty.
            self._syncing = True
            self.list_widget.blockSignals(True)
            if self.list_widget.count():
                self.list_widget.item(0).setSelected(True)
            self.list_widget.blockSignals(False)
            self._syncing = False
            keys = {ALL_KEY}
        newly_added = keys - self._last_keys
        if ALL_KEY in keys and len(keys) > 1:
            if ALL_KEY in newly_added:
                # Adding All to others: All wins - drop everything else.
                keys = {ALL_KEY}
            else:
                # Adding something to All: All is exclusive - drop All.
                keys.discard(ALL_KEY)
            self._sync_visual(keys)
        self._last_keys = set(keys)
        self.selectionChanged.emit(list(keys - {ALL_KEY}) if ALL_KEY not in keys else [])

    def _sync_visual(self, keys: set) -> None:
        self._syncing = True
        self.list_widget.blockSignals(True)
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            item.setSelected(item.data(Qt.ItemDataRole.UserRole) in keys)
        self.list_widget.blockSignals(False)
        self._syncing = False

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            pinned = item.data(Qt.ItemDataRole.UserRole) in (ALL_KEY, FAV_KEY)
            item.setHidden(bool(needle) and not pinned and needle not in item.text().lower())
