"""Options tab: manage the band-preset catalogue (the "Add preset" dropdown
in the Listen tab's band-list editor) - add, edit or delete ANY preset,
built-in ones included, and import/export presets as a plain JSON file to
back up or share with someone else.

Editing or deleting a built-in preset never touches the file it shipped in
(every app update would just restore that anyway) - see
military_presets.get_all_presets()'s own docstring for exactly how an
edited/deleted built-in is represented purely in the user's own file
instead. The practical effect, by explicit request: the moment you edit or
delete a built-in, it's yours from then on, indistinguishable from (and
managed exactly like) a preset you created from scratch.
"""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout,
)

from ...i18n import tr
from ...military_presets import (
    BandPreset, get_all_presets, hide_builtin_preset, is_user_owned_preset,
    load_user_presets, save_user_presets,
)

_ID_ROLE = Qt.ItemDataRole.UserRole
_IS_BUILTIN_ROLE = Qt.ItemDataRole.UserRole + 1


def _slugify(label: str) -> str:
    slug = "".join(c.lower() if c.isalnum() else "_" for c in label).strip("_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug or "preset"


def _unique_id(label: str, existing_ids: set[str]) -> str:
    base = _slugify(label)
    candidate = base
    i = 2
    while candidate in existing_ids:
        candidate = f"{base}_{i}"
        i += 1
    return candidate


class _PresetEditDialog(QDialog):
    """Add/edit form for one user preset - MHz/kHz in the UI (matching the
    rest of this app's band editor), converted to Hz on the BandPreset
    itself (same unit BandPreset/VfoRangeConfig already use everywhere)."""

    def __init__(self, preset: BandPreset | None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Edit preset") if preset is not None else tr("Add preset"))
        self._editing_id = preset.id if preset is not None else None

        form = QFormLayout(self)
        self.label_edit = QLineEdit(preset.label if preset else "")
        form.addRow(tr("Label:"), self.label_edit)
        self.tooltip_edit = QLineEdit(preset.tooltip if preset else "")
        form.addRow(tr("Description (tooltip):"), self.tooltip_edit)

        self.start_spin = QDoubleSpinBox()
        self.start_spin.setDecimals(5)
        self.start_spin.setRange(0.01, 6000.0)
        self.start_spin.setSuffix(" MHz")
        self.start_spin.setValue((preset.start_hz if preset else 118_000_000) / 1e6)
        form.addRow(tr("Start:"), self.start_spin)

        self.stop_spin = QDoubleSpinBox()
        self.stop_spin.setDecimals(5)
        self.stop_spin.setRange(0.01, 6000.0)
        self.stop_spin.setSuffix(" MHz")
        self.stop_spin.setValue((preset.stop_hz if preset else 137_000_000) / 1e6)
        form.addRow(tr("Stop:"), self.stop_spin)

        self.step_spin = QDoubleSpinBox()
        self.step_spin.setDecimals(3)
        self.step_spin.setRange(0.001, 1000.0)
        self.step_spin.setSuffix(" kHz")
        self.step_spin.setValue((preset.step_hz if preset else 25_000) / 1e3)
        form.addRow(tr("Step:"), self.step_spin)

        self.modulation_combo = QComboBox()
        self.modulation_combo.addItems(["AM", "NFM", "WFM"])
        self.modulation_combo.setCurrentText(preset.modulation if preset else "AM")
        form.addRow(tr("Modulation:"), self.modulation_combo)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def result_preset(self, existing_ids: set[str]) -> BandPreset:
        label = self.label_edit.text().strip() or tr("Custom range")
        preset_id = self._editing_id or _unique_id(label, existing_ids)
        return BandPreset(
            id=preset_id,
            label=label,
            tooltip=self.tooltip_edit.text().strip(),
            start_hz=round(self.start_spin.value() * 1e6),
            stop_hz=round(self.stop_spin.value() * 1e6),
            step_hz=round(self.step_spin.value() * 1e3),
            modulation=self.modulation_combo.currentText(),
        )


class PresetManagerPanel(QGroupBox):
    """Options tab section - see module docstring."""

    # Emitted whenever the *user* preset file actually changed on disk (add/
    # edit/delete/import) - ScannerPanel's dropdown listens for this to
    # refresh itself without needing a restart (see get_all_presets()'s own
    # docstring on why it's a function, not a cached list).
    presets_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(tr("Band presets"), parent)
        root = QVBoxLayout(self)

        self.list_widget = QListWidget()
        self.list_widget.currentItemChanged.connect(self._update_button_states)
        root.addWidget(self.list_widget)

        info = QLabel(tr(
            "Editing or deleting a built-in preset makes it yours from then "
            "on - it won't be reset back by an app update. Your own presets "
            "are plain JSON, easy to back up or share - see Export/Import below."
        ))
        info.setWordWrap(True)
        info.setStyleSheet("color: #9aa0a6;")
        root.addWidget(info)

        btn_row = QHBoxLayout()
        self.add_btn = QPushButton(tr("Add…"))
        self.edit_btn = QPushButton(tr("Edit…"))
        self.delete_btn = QPushButton(tr("Delete"))
        self.export_btn = QPushButton(tr("Export…"))
        self.import_btn = QPushButton(tr("Import…"))
        self.add_btn.clicked.connect(self._on_add)
        self.edit_btn.clicked.connect(self._on_edit)
        self.delete_btn.clicked.connect(self._on_delete)
        self.export_btn.clicked.connect(self._on_export)
        self.import_btn.clicked.connect(self._on_import)
        for b in (self.add_btn, self.edit_btn, self.delete_btn, self.export_btn, self.import_btn):
            btn_row.addWidget(b)
        root.addLayout(btn_row)

        self._reload_list()

    # -- internals ---------------------------------------------------
    def _reload_list(self) -> None:
        self.list_widget.clear()
        for preset in get_all_presets():
            is_builtin = not is_user_owned_preset(preset.id)
            suffix = " (" + tr("built-in") + ")" if is_builtin else ""
            item = QListWidgetItem(preset.label + suffix)
            item.setData(_ID_ROLE, preset.id)
            item.setData(_IS_BUILTIN_ROLE, is_builtin)
            self.list_widget.addItem(item)
        self._update_button_states()

    def _update_button_states(self, *_args) -> None:
        # Every preset is editable/deletable now, built-in or not - see the
        # module docstring. The "(built-in)" suffix in the list is purely
        # informational (it tells you editing/deleting will "claim" it),
        # not a restriction.
        has_selection = self.list_widget.currentItem() is not None
        self.edit_btn.setEnabled(has_selection)
        self.delete_btn.setEnabled(has_selection)

    def _current_preset(self) -> BandPreset | None:
        item = self.list_widget.currentItem()
        if item is None:
            return None
        preset_id = item.data(_ID_ROLE)
        return next((p for p in get_all_presets() if p.id == preset_id), None)

    def _persist(self, user_presets: list[BandPreset]) -> None:
        save_user_presets(user_presets)
        self._reload_list()
        self.presets_changed.emit()

    def _on_add(self) -> None:
        dlg = _PresetEditDialog(None, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        existing_ids = {p.id for p in get_all_presets()}
        new_preset = dlg.result_preset(existing_ids)
        user_presets = load_user_presets()
        user_presets.append(new_preset)
        self._persist(user_presets)

    def _on_edit(self) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        dlg = _PresetEditDialog(preset, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        existing_ids = {p.id for p in get_all_presets()} - {preset.id}
        updated = dlg.result_preset(existing_ids)
        # Editing a still-pure built-in has no existing entry to replace -
        # it's *added* to the user list under the same id, which is exactly
        # what shadows the built-in from now on (see get_all_presets()).
        user_presets = load_user_presets()
        if any(p.id == preset.id for p in user_presets):
            user_presets = [updated if p.id == preset.id else p for p in user_presets]
        else:
            user_presets.append(updated)
        self._persist(user_presets)

    def _on_delete(self) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        reply = QMessageBox.question(
            self, tr("Delete preset"),
            tr("Delete preset “{label}”? This only removes it from the dropdown - "
               "any band you already added from it stays as it is.").format(label=preset.label),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        if is_user_owned_preset(preset.id):
            user_presets = [p for p in load_user_presets() if p.id != preset.id]
            self._persist(user_presets)
        else:
            # A still-pure built-in - nothing in the user list to remove,
            # record the id as hidden instead (see hide_builtin_preset()'s
            # own docstring for why the built-in file itself is never
            # touched).
            hide_builtin_preset(preset.id)
            self._reload_list()
            self.presets_changed.emit()

    def _on_export(self) -> None:
        user_presets = load_user_presets()
        if not user_presets:
            QMessageBox.information(self, tr("Export presets"), tr("You don't have any custom presets to export yet."))
            return
        path_str, _ = QFileDialog.getSaveFileName(
            self, tr("Export presets"), "band_presets.json", "JSON (*.json)",
        )
        if not path_str:
            return
        path = Path(path_str)
        try:
            path.write_text(
                json.dumps([p.to_dict() for p in user_presets], indent=2, ensure_ascii=False), encoding="utf-8",
            )
        except OSError as exc:
            QMessageBox.warning(self, tr("Export failed"), str(exc))

    def _on_import(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(self, tr("Import presets"), "", "JSON (*.json)")
        if not path_str:
            return
        try:
            raw = json.loads(Path(path_str).read_text(encoding="utf-8"))
            imported = [BandPreset.from_dict(d) for d in raw]
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            QMessageBox.warning(self, tr("Import failed"), str(exc))
            return
        user_presets = load_user_presets()
        existing_ids = {p.id for p in get_all_presets()}
        added = 0
        for preset in imported:
            preset_id = preset.id
            if preset_id in existing_ids:
                # Never silently overwrite an existing preset (built-in or
                # user's own) - a same-named import gets a fresh, distinct
                # id instead, so nothing already relied upon changes under
                # anyone's feet.
                preset_id = _unique_id(preset.label, existing_ids)
                preset = BandPreset(
                    id=preset_id, label=preset.label, tooltip=preset.tooltip,
                    start_hz=preset.start_hz, stop_hz=preset.stop_hz, step_hz=preset.step_hz,
                    modulation=preset.modulation,
                )
            existing_ids.add(preset_id)
            user_presets.append(preset)
            added += 1
        self._persist(user_presets)
        QMessageBox.information(
            self, tr("Import presets"), tr("Imported {count} preset(s).").format(count=added),
        )
