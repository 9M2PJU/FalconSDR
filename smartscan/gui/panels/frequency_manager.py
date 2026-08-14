"""Menedzer Czestotliwosci: Memory List tab (drzewo z folderami, checkboxy,
przeciaganie) + Blacklist Manager tab (spec 4.4/6)."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QDoubleSpinBox, QDialog, QDialogButtonBox,
    QFormLayout, QHBoxLayout, QHeaderView, QInputDialog, QLineEdit, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QTabWidget, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ...military_presets import FIRE_CHANNELS, MILITARY_CHANNELS
from ...models import BlacklistEntry, Channel, Modulation

RESOURCES_DIR = Path(__file__).resolve().parent.parent / "resources"
_FOLDER_ICON = QIcon(str(RESOURCES_DIR / "folder.svg"))

COL_NAME, COL_FREQ, COL_MOD, COL_WEIGHT, COL_ENABLED = range(5)
ROLE_CHANNEL = Qt.ItemDataRole.UserRole
ROLE_IS_FOLDER = Qt.ItemDataRole.UserRole + 1
ROLE_FOLDER_NAME = Qt.ItemDataRole.UserRole + 2  # raw folder name (display text has no icon prefix, icon is separate)
_FOLDER_BG = QColor(70, 110, 170, 130)


class _DeletableTable(QTableWidget):
    """QTableWidget that also triggers deletion via the Delete key, not just
    the "Usun" button - deleting a channel/blacklist entry is not a
    destructive-enough action here to need a confirmation dialog (unlike
    recordings), so this just needs to be fast.
    """
    deleteRequested = pyqtSignal()

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.deleteRequested.emit()
            return
        super().keyPressEvent(event)


class _ChannelTree(QTreeWidget):
    """QTreeWidget for the Memory List: folders as parent nodes (bulk
    enable/disable via a tri-state checkbox), channels as draggable leaves.
    Also triggers deletion via the Delete key, same rationale as _DeletableTable.
    """
    deleteRequested = pyqtSignal()
    dropped = pyqtSignal()

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.deleteRequested.emit()
            return
        super().keyPressEvent(event)

    def dropEvent(self, event) -> None:
        super().dropEvent(event)
        self.dropped.emit()


class _ChannelDialog(QDialog):
    def __init__(self, folders: list[str], channel: Channel | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Kanal")
        form = QFormLayout(self)

        self.freq_spin = QDoubleSpinBox()
        self.freq_spin.setRange(0.01, 6000.0)
        self.freq_spin.setDecimals(4)
        self.freq_spin.setSuffix(" MHz")
        form.addRow("Czestotliwosc:", self.freq_spin)

        self.name_edit = QLineEdit()
        form.addRow("Nazwa:", self.name_edit)

        self.mod_combo = QComboBox()
        for m in Modulation:
            self.mod_combo.addItem(m.value, m)
        form.addRow("Modulacja:", self.mod_combo)

        self.folder_combo = QComboBox()
        self.folder_combo.setEditable(True)
        self.folder_combo.setToolTip(
            "Folder w Liscie pamieci (do organizacji/grupowego wlaczania-wylaczania).\n"
            "Wybierz istniejacy albo wpisz nowa nazwe, zeby go od razu utworzyc."
        )
        self.folder_combo.addItem("(brak folderu)", "")
        for f in folders:
            self.folder_combo.addItem(f, f)
        form.addRow("Folder:", self.folder_combo)

        self.weight_spin = QDoubleSpinBox()
        self.weight_spin.setRange(0.0, 20.0)
        self.weight_spin.setDecimals(2)
        self.weight_spin.setSingleStep(0.5)
        self.weight_spin.setToolTip(
            "Waga priorytetu skanowania (0 = domyslna, bez wyroznienia). Mozna ja\n"
            "tez ustawic/podejrzec wygodniej w zakladce Skaner, w rozwijanej\n"
            "Liscie pamieci obok kazdego kanalu."
        )
        form.addRow("Waga:", self.weight_spin)

        self._existing_enabled = True
        if channel is not None:
            self.freq_spin.setValue(channel.freq_hz / 1e6)
            self.name_edit.setText(channel.name)
            self.mod_combo.setCurrentIndex(self.mod_combo.findData(channel.modulation))
            self.weight_spin.setValue(channel.weight)
            self._existing_enabled = channel.enabled
            if channel.folder:
                idx = self.folder_combo.findData(channel.folder)
                if idx == -1:
                    self.folder_combo.addItem(channel.folder, channel.folder)
                    idx = self.folder_combo.count() - 1
                self.folder_combo.setCurrentIndex(idx)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def result_channel(self) -> Channel:
        folder = self.folder_combo.currentText().strip()
        if folder == "(brak folderu)":
            folder = ""
        return Channel(
            freq_hz=int(round(self.freq_spin.value() * 1e6)),
            name=self.name_edit.text().strip(),
            modulation=self.mod_combo.currentData(),
            enabled=self._existing_enabled,
            folder=folder,
            weight=self.weight_spin.value(),
        )


class _BlacklistDialog(QDialog):
    def __init__(self, default_margin_hz: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Dodaj do czarnej listy")
        form = QFormLayout(self)

        self.freq_spin = QDoubleSpinBox()
        self.freq_spin.setRange(0.01, 6000.0)
        self.freq_spin.setDecimals(4)
        self.freq_spin.setSuffix(" MHz")
        form.addRow("Czestotliwosc:", self.freq_spin)

        self.margin_spin = QDoubleSpinBox()
        self.margin_spin.setRange(0.1, 5000.0)
        self.margin_spin.setDecimals(2)
        self.margin_spin.setSuffix(" kHz")
        self.margin_spin.setValue(default_margin_hz / 1000)
        form.addRow("Margines (+/-):", self.margin_spin)

        self.label_edit = QLineEdit()
        form.addRow("Opis:", self.label_edit)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def result_entry(self) -> BlacklistEntry:
        return BlacklistEntry(
            freq_hz=int(round(self.freq_spin.value() * 1e6)),
            margin_hz=int(round(self.margin_spin.value() * 1000)),
            label=self.label_edit.text().strip(),
            date_added=dt.date.today().isoformat(),
        )


class FrequencyManager(QTabWidget):
    channels_changed = pyqtSignal(list)
    blacklist_changed = pyqtSignal(list)
    folders_changed = pyqtSignal(list)

    def __init__(self, channels: list[Channel], blacklist: list[BlacklistEntry], default_margin_hz: int,
                 folders: list[str] | None = None, parent=None):
        super().__init__(parent)
        self.channels = list(channels)
        self.blacklist = list(blacklist)
        self.default_margin_hz = default_margin_hz
        self.folders: list[str] = list(folders or [])
        self._sync_folders_from_channels()
        self._build_channels_tab()
        self._build_blacklist_tab()

        search_shortcut = QShortcut(QKeySequence("Ctrl+F"), self)
        search_shortcut.activated.connect(self._focus_search)

    def _focus_search(self) -> None:
        self.reveal_channels()

    def reveal_channels(self) -> None:
        """Przewija do zakladki Kanaly i ustawia fokus na wyszukiwarce - wolane
        z zakladki Skaner (przycisk "Lista pamieci") oraz ze skrotu Ctrl+F."""
        self.setCurrentIndex(0)
        self.channel_search.setFocus()
        self.channel_search.selectAll()

    def _sync_folders_from_channels(self) -> None:
        """Dopisuje do self.folders kazdy folder, na ktory wskazuje jakis
        kanal, ale ktorego jeszcze nie znamy (np. wczytany ze starszego pliku
        konfiguracji) - tak zeby zawsze byl widoczny jako wezel w drzewie."""
        known = set(self.folders)
        for c in self.channels:
            if c.folder and c.folder not in known:
                self.folders.append(c.folder)
                known.add(c.folder)

    # -- Memory list tab -------------------------------------------------
    def _build_channels_tab(self) -> None:
        w = QWidget()
        lay = QVBoxLayout(w)

        self.channel_search = QLineEdit()
        self.channel_search.setPlaceholderText("Szukaj (czestotliwosc, nazwa, modulacja, folder)... [Ctrl+F]")
        self.channel_search.textChanged.connect(self._apply_channel_filter)
        lay.addWidget(self.channel_search)

        self.channel_tree = _ChannelTree()
        self.channel_tree.setColumnCount(5)
        self.channel_tree.setHeaderLabels(["Nazwa / Folder", "Czestotliwosc (MHz)", "Modulacja", "Waga", "Wlaczony"])
        header = self.channel_tree.header()
        header.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(COL_FREQ, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_MOD, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_WEIGHT, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_ENABLED, QHeaderView.ResizeMode.ResizeToContents)
        self.channel_tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.channel_tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.channel_tree.setDragEnabled(True)
        self.channel_tree.setAcceptDrops(True)
        self.channel_tree.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.channel_tree.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.channel_tree.setDropIndicatorShown(True)
        self.channel_tree.deleteRequested.connect(self._delete_selected)
        self.channel_tree.dropped.connect(self._on_tree_dropped)
        self.channel_tree.itemChanged.connect(self._on_tree_item_changed)
        lay.addWidget(self.channel_tree)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("Dodaj")
        edit_btn = QPushButton("Edytuj")
        edit_btn.setToolTip("Edytuj zaznaczony kanal, albo zmien nazwe zaznaczonego folderu.")
        del_btn = QPushButton("Usun")
        new_folder_btn = QPushButton("Nowy folder")
        new_folder_btn.setToolTip(
            "Tworzy pusty folder, do ktorego mozna od razu przeciagac czestotliwosci\n"
            "z listy ponizej (albo wybrac go przy dodawaniu/edycji kanalu)."
        )
        military_btn = QPushButton("Wczytaj wojskowe (znane czestotliwosci)")
        military_btn.setToolTip(
            "Dopisuje sprawdzone, publicznie znane czestotliwosci wojskowe: guard,\n"
            "tankowanie w powietrzu (cysterny), sektory OAT Warszawa oraz wieze/\n"
            "podejscia baz QRA/Baltic Air Policing (Malbork, Lask, Minsk Mazowiecki,\n"
            "Powidz, Swidwin, Miroslawiec). Nie nadpisuje istniejacej listy."
        )
        military_btn.clicked.connect(self._load_military_channels)
        fire_btn = QPushButton("Wczytaj straz pozarna (znane czestotliwosci)")
        fire_btn.setToolTip(
            "Dopisuje ogolnopolskie kanaly ratowniczo-gasnicze PSP (KRG 1-8) oraz\n"
            "czestotliwosci alarmowania syren OSP (148.6625-149.3375 MHz, NFM).\n"
            "Wiele OSP nadal pracuje analogowo mimo migracji PSP do DMR. Nie\n"
            "nadpisuje istniejacej listy."
        )
        fire_btn.clicked.connect(self._load_fire_channels)
        add_btn.clicked.connect(self._add_channel)
        edit_btn.clicked.connect(self._edit_channel)
        del_btn.clicked.connect(self._delete_selected)
        new_folder_btn.clicked.connect(self._add_folder)
        for b in (add_btn, edit_btn, del_btn, new_folder_btn, military_btn, fire_btn):
            btn_row.addWidget(b)
        lay.addLayout(btn_row)

        self.addTab(w, "Kanaly (Memory List)")
        self._refresh_channel_tree()

    def _refresh_channel_tree(self) -> None:
        self._sync_folders_from_channels()
        was_blocked = self.channel_tree.blockSignals(True)
        if self.channel_tree.topLevelItemCount() == 0:
            expanded = set(self.folders)  # first build: start with every folder open
        else:
            expanded = {
                self.channel_tree.topLevelItem(i).data(0, ROLE_FOLDER_NAME)
                for i in range(self.channel_tree.topLevelItemCount())
                if self.channel_tree.topLevelItem(i).data(0, ROLE_IS_FOLDER)
                and self.channel_tree.topLevelItem(i).isExpanded()
            }
        self.channel_tree.clear()

        folder_items: dict[str, QTreeWidgetItem] = {}
        for name in self.folders:
            # Folders are meant to stand out at a glance - a folder icon, bold
            # + slightly larger name, and a tinted background across the row.
            item = QTreeWidgetItem([name, "", "", "", ""])
            item.setIcon(COL_NAME, _FOLDER_ICON)
            item.setData(0, ROLE_IS_FOLDER, True)
            item.setData(0, ROLE_FOLDER_NAME, name)
            item.setFlags(
                Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsDropEnabled
            )
            font = item.font(COL_NAME)
            font.setBold(True)
            font.setPointSize(font.pointSize() + 1)
            for col in range(self.channel_tree.columnCount()):
                item.setFont(col, font)
                item.setBackground(col, _FOLDER_BG)
            self.channel_tree.addTopLevelItem(item)
            item.setExpanded(name in expanded)
            folder_items[name] = item

        for ch in self.channels:
            weight_text = "" if ch.weight <= 0 else f"{ch.weight:g}"
            item = QTreeWidgetItem(["", f"{ch.freq_hz / 1e6:.4f}", ch.modulation.value, weight_text, ""])
            item.setText(COL_NAME, ch.name or "(bez nazwy)")
            item.setTextAlignment(COL_FREQ, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            item.setTextAlignment(COL_WEIGHT, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            item.setData(0, ROLE_CHANNEL, ch)
            item.setData(0, ROLE_IS_FOLDER, False)
            item.setFlags(
                Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsDragEnabled
            )
            item.setCheckState(COL_ENABLED, Qt.CheckState.Checked if ch.enabled else Qt.CheckState.Unchecked)
            parent = folder_items.get(ch.folder) if ch.folder else None
            if parent is not None:
                parent.addChild(item)
            else:
                self.channel_tree.addTopLevelItem(item)

        for item in folder_items.values():
            self._update_folder_checkstate(item)

        self.channel_tree.blockSignals(was_blocked)
        self._apply_channel_filter(self.channel_search.text())

    def _channel_matches(self, item: QTreeWidgetItem, needle: str) -> bool:
        if not needle:
            return True
        ch: Channel = item.data(0, ROLE_CHANNEL)
        haystack = f"{ch.freq_hz / 1e6:.4f} {ch.name} {ch.modulation.value} {ch.folder}".lower()
        return needle in haystack

    def _apply_channel_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for i in range(self.channel_tree.topLevelItemCount()):
            top = self.channel_tree.topLevelItem(i)
            if top.data(0, ROLE_IS_FOLDER):
                any_visible = False
                for j in range(top.childCount()):
                    child = top.child(j)
                    match = self._channel_matches(child, needle)
                    child.setHidden(bool(needle) and not match)
                    any_visible = any_visible or match
                folder_name_matches = needle in top.data(0, ROLE_FOLDER_NAME).lower()
                top.setHidden(bool(needle) and not any_visible and not folder_name_matches)
                if needle and any_visible:
                    top.setExpanded(True)
            else:
                top.setHidden(bool(needle) and not self._channel_matches(top, needle))

    # -- checkbox handling (folder = bulk toggle, not a stored property) ---
    def _on_tree_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if column != COL_ENABLED:
            return
        if item.data(0, ROLE_IS_FOLDER):
            state = item.checkState(COL_ENABLED)
            if state == Qt.CheckState.PartiallyChecked:
                return
            enabled = state == Qt.CheckState.Checked
            blocked = self.channel_tree.blockSignals(True)
            for i in range(item.childCount()):
                child = item.child(i)
                child.setCheckState(COL_ENABLED, Qt.CheckState.Checked if enabled else Qt.CheckState.Unchecked)
                ch = child.data(0, ROLE_CHANNEL)
                if ch is not None:
                    ch.enabled = enabled
            self.channel_tree.blockSignals(blocked)
            self.channels_changed.emit(self.channels)
        else:
            ch = item.data(0, ROLE_CHANNEL)
            if ch is None:
                return
            ch.enabled = item.checkState(COL_ENABLED) == Qt.CheckState.Checked
            parent = item.parent()
            if parent is not None:
                self._update_folder_checkstate(parent)
            self.channels_changed.emit(self.channels)

    def _update_folder_checkstate(self, folder_item: QTreeWidgetItem) -> None:
        n = folder_item.childCount()
        if n == 0:
            state = Qt.CheckState.Unchecked
        else:
            checked = sum(
                1 for i in range(n) if folder_item.child(i).checkState(COL_ENABLED) == Qt.CheckState.Checked
            )
            if checked == 0:
                state = Qt.CheckState.Unchecked
            elif checked == n:
                state = Qt.CheckState.Checked
            else:
                state = Qt.CheckState.PartiallyChecked
        blocked = self.channel_tree.blockSignals(True)
        folder_item.setCheckState(COL_ENABLED, state)
        self.channel_tree.blockSignals(blocked)

    # -- drag & drop between folders ---------------------------------------
    def _on_tree_dropped(self) -> None:
        # Folder items aren't draggable, but guard against odd nesting anyway
        # (e.g. a channel dropped exactly on a folder's edge) by flattening
        # any folder-kind item that ended up as a child back to top level.
        for i in range(self.channel_tree.topLevelItemCount()):
            top = self.channel_tree.topLevelItem(i)
            if not top.data(0, ROLE_IS_FOLDER):
                continue
            for j in reversed(range(top.childCount())):
                child = top.child(j)
                if child.data(0, ROLE_IS_FOLDER):
                    self.channel_tree.addTopLevelItem(top.takeChild(j))

        changed = False
        for i in range(self.channel_tree.topLevelItemCount()):
            top = self.channel_tree.topLevelItem(i)
            if top.data(0, ROLE_IS_FOLDER):
                folder_name = top.data(0, ROLE_FOLDER_NAME)
                for j in range(top.childCount()):
                    ch = top.child(j).data(0, ROLE_CHANNEL)
                    if ch is not None and ch.folder != folder_name:
                        ch.folder = folder_name
                        changed = True
                self._update_folder_checkstate(top)
            else:
                ch = top.data(0, ROLE_CHANNEL)
                if ch is not None and ch.folder != "":
                    ch.folder = ""
                    changed = True
        if changed:
            self.channels_changed.emit(self.channels)

    # -- add/edit/delete channels & folders ---------------------------------
    def _selected_channel_item(self) -> QTreeWidgetItem | None:
        for item in self.channel_tree.selectedItems():
            if not item.data(0, ROLE_IS_FOLDER):
                return item
        return None

    def _add_channel(self) -> None:
        dlg = _ChannelDialog(self.folders, parent=self)
        if dlg.exec():
            ch = dlg.result_channel()
            self.channels.append(ch)
            self._maybe_add_new_folder(ch.folder)
            self._refresh_channel_tree()
            self.channels_changed.emit(self.channels)

    def _edit_channel(self) -> None:
        folder_item = next((it for it in self.channel_tree.selectedItems() if it.data(0, ROLE_IS_FOLDER)), None)
        if folder_item is not None:
            self._rename_folder(folder_item)
            return
        item = self._selected_channel_item()
        if item is None:
            return
        ch = item.data(0, ROLE_CHANNEL)
        dlg = _ChannelDialog(self.folders, ch, parent=self)
        if dlg.exec():
            new_ch = dlg.result_channel()
            idx = next((i for i, c in enumerate(self.channels) if c is ch), None)
            if idx is None:
                return
            self.channels[idx] = new_ch
            self._maybe_add_new_folder(new_ch.folder)
            self._refresh_channel_tree()
            self.channels_changed.emit(self.channels)

    def _rename_folder(self, folder_item: QTreeWidgetItem) -> None:
        old_name = folder_item.data(0, ROLE_FOLDER_NAME)
        new_name, ok = QInputDialog.getText(self, "Zmien nazwe folderu", "Nazwa folderu:", text=old_name)
        new_name = new_name.strip()
        if not ok or not new_name or new_name == old_name:
            return
        if new_name in self.folders:
            QMessageBox.information(self, "Zmien nazwe folderu", f"Folder „{new_name}” juz istnieje.")
            return
        if old_name in self.folders:
            self.folders[self.folders.index(old_name)] = new_name
        else:
            self.folders.append(new_name)
        for c in self.channels:
            if c.folder == old_name:
                c.folder = new_name
        self._refresh_channel_tree()
        self.folders_changed.emit(self.folders)
        self.channels_changed.emit(self.channels)

    def _maybe_add_new_folder(self, folder: str) -> None:
        if folder and folder not in self.folders:
            self.folders.append(folder)
            self.folders_changed.emit(self.folders)

    def _delete_selected(self) -> None:
        items = self.channel_tree.selectedItems()
        if not items:
            return
        channel_objs = [
            it.data(0, ROLE_CHANNEL) for it in items
            if not it.data(0, ROLE_IS_FOLDER) and it.data(0, ROLE_CHANNEL) is not None
        ]
        folder_names = [it.data(0, ROLE_FOLDER_NAME) for it in items if it.data(0, ROLE_IS_FOLDER)]

        if channel_objs:
            remove_ids = {id(c) for c in channel_objs}
            self.channels = [c for c in self.channels if id(c) not in remove_ids]
        for name in folder_names:
            for c in self.channels:
                if c.folder == name:
                    c.folder = ""
            if name in self.folders:
                self.folders.remove(name)

        if folder_names:
            self.folders_changed.emit(self.folders)
        self._refresh_channel_tree()
        self.channels_changed.emit(self.channels)

    def _add_folder(self) -> None:
        name, ok = QInputDialog.getText(self, "Nowy folder", "Nazwa folderu:")
        name = name.strip()
        if not ok or not name:
            return
        if name in self.folders:
            QMessageBox.information(self, "Nowy folder", f"Folder „{name}” juz istnieje.")
            return
        self.folders.append(name)
        self._refresh_channel_tree()
        self.folders_changed.emit(self.folders)

    def _load_military_channels(self) -> None:
        self._load_curated_channels(MILITARY_CHANNELS)

    def _load_fire_channels(self) -> None:
        self._load_curated_channels(FIRE_CHANNELS)

    def _load_curated_channels(self, curated: list[Channel]) -> None:
        existing = {c.freq_hz for c in self.channels}
        added = 0
        for ch in curated:
            if ch.freq_hz in existing:
                continue
            self.channels.append(Channel(freq_hz=ch.freq_hz, name=ch.name, modulation=ch.modulation, enabled=ch.enabled))
            existing.add(ch.freq_hz)
            added += 1
        if added:
            self._refresh_channel_tree()
            self.channels_changed.emit(self.channels)

    # -- external sync (called by MainWindow, e.g. after "Zapisz czestotliwosc"
    # in the Odtwarzacz tab, or on load) ------------------------------------
    def set_channels(self, channels: list[Channel]) -> None:
        self.channels = list(channels)
        self._refresh_channel_tree()

    def set_folders(self, folders: list[str]) -> None:
        self.folders = list(folders)
        self._refresh_channel_tree()

    # -- Blacklist tab -----------------------------------------------------
    def _build_blacklist_tab(self) -> None:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.blacklist_table = _DeletableTable(0, 4)
        self.blacklist_table.setHorizontalHeaderLabels(["Czestotliwosc (MHz)", "Margines (kHz)", "Opis", "Dodano"])
        self.blacklist_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.blacklist_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.blacklist_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.blacklist_table.deleteRequested.connect(self._delete_blacklist_entry)
        lay.addWidget(self.blacklist_table)

        btn_row = QHBoxLayout()
        add_btn = QPushButton("Dodaj recznie")
        del_btn = QPushButton("Usun")
        add_btn.clicked.connect(self._add_blacklist_entry)
        del_btn.clicked.connect(self._delete_blacklist_entry)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(del_btn)
        lay.addLayout(btn_row)

        self.addTab(w, "Czarna lista (Blacklist)")
        self._refresh_blacklist_table()

    def _refresh_blacklist_table(self) -> None:
        self.blacklist_table.setRowCount(len(self.blacklist))
        for row, e in enumerate(self.blacklist):
            self.blacklist_table.setItem(row, 0, QTableWidgetItem(f"{e.freq_hz / 1e6:.4f}"))
            self.blacklist_table.setItem(row, 1, QTableWidgetItem(f"{e.margin_hz / 1000:.2f}"))
            self.blacklist_table.setItem(row, 2, QTableWidgetItem(e.label))
            self.blacklist_table.setItem(row, 3, QTableWidgetItem(e.date_added))

    def _add_blacklist_entry(self) -> None:
        dlg = _BlacklistDialog(self.default_margin_hz, parent=self)
        if dlg.exec():
            self.blacklist.append(dlg.result_entry())
            self._refresh_blacklist_table()
            self.blacklist_changed.emit(self.blacklist)

    def _delete_blacklist_entry(self) -> None:
        rows = sorted({i.row() for i in self.blacklist_table.selectedIndexes()}, reverse=True)
        if not rows:
            return
        for row in rows:
            del self.blacklist[row]
        self._refresh_blacklist_table()
        self.blacklist_changed.emit(self.blacklist)

    def add_lockout(self, freq_hz: int, label: str = "Auto-lockout (SKIP)") -> None:
        self.blacklist.append(BlacklistEntry(
            freq_hz=freq_hz, margin_hz=self.default_margin_hz, label=label,
            date_added=dt.date.today().isoformat(),
        ))
        self._refresh_blacklist_table()
        self.blacklist_changed.emit(self.blacklist)
