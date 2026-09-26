"""Recency boost: controls for RecencyBoostConfig (see config.py and
ScannerEngine._due_recency_boost_locked/_register_recency_boost) - after a
transmission ends naturally, its window is visited more often than usual for
a while, in case another message follows on the same frequency within the
next several/dozens of seconds (typical for ATC). Unlike simply raising
linger_time_ms, this never blocks noticing the conversation move to a
different frequency - the scanner keeps circling the rest of the schedule as
normal the whole time.
"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QCheckBox, QDoubleSpinBox, QFormLayout, QGroupBox, QLabel, QVBoxLayout

from ...config import RecencyBoostConfig
from ...i18n import tr


class RecencyBoostPanel(QGroupBox):
    settings_changed = Signal()

    def __init__(self, config: RecencyBoostConfig, parent=None):
        super().__init__(tr("Recency boost (more frequent revisits after a transmission)"), parent)
        self.config = config
        self._loading = True
        self._build_ui()
        self._load_from_config()
        self._loading = False

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        hint = QLabel(tr(
            "After a transmission ends naturally (not via a manual Skip), its "
            "window is visited more often than usual for a while - in case "
            "another message follows within the next several/dozens of "
            "seconds on the same frequency. The scanner still keeps circling "
            "the other channels normally the whole time, so a conversation "
            "moving to a different frequency is still detected - unlike "
            "simply raising the linger time."
        ))
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #9aa0a6;")
        root.addWidget(hint)

        self.enable_check = QCheckBox(tr("Enable recency boost"))
        self.enable_check.toggled.connect(self._push_settings)
        root.addWidget(self.enable_check)

        form = QFormLayout()
        self.duration_spin = QDoubleSpinBox()
        self.duration_spin.setRange(5.0, 600.0)
        self.duration_spin.setDecimals(0)
        self.duration_spin.setSuffix(" s")
        self.duration_spin.setSingleStep(5.0)
        self.duration_spin.valueChanged.connect(self._push_settings)
        form.addRow(tr("How long to boost after a transmission:"), self.duration_spin)

        self.interval_spin = QDoubleSpinBox()
        self.interval_spin.setRange(1.0, 120.0)
        self.interval_spin.setDecimals(0)
        self.interval_spin.setSuffix(" s")
        self.interval_spin.setSingleStep(1.0)
        self.interval_spin.valueChanged.connect(self._push_settings)
        form.addRow(tr("How often to revisit during the boost:"), self.interval_spin)

        root.addLayout(form)

    def _load_from_config(self) -> None:
        self.enable_check.setChecked(self.config.enabled)
        self.duration_spin.setValue(self.config.duration_s)
        self.interval_spin.setValue(self.config.interval_s)
        self._sync_enabled_state()

    def _sync_enabled_state(self) -> None:
        enabled = self.enable_check.isChecked()
        self.duration_spin.setEnabled(enabled)
        self.interval_spin.setEnabled(enabled)

    def _push_settings(self, *_args) -> None:
        if self._loading:
            return
        self.config.enabled = self.enable_check.isChecked()
        self.config.duration_s = self.duration_spin.value()
        self.config.interval_s = self.interval_spin.value()
        self._sync_enabled_state()
        self.settings_changed.emit()
