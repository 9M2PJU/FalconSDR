"""Ustawienia automatycznej transkrypcji nagrań."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QCheckBox, QGroupBox, QLabel, QVBoxLayout

from ...config import TranscriptionConfig


class TranscriptionPanel(QGroupBox):
    """Przełącznik automatycznej kolejki Whispera w zakładce Opcje."""

    settings_changed = Signal()
    enabled_changed = Signal(bool)

    def __init__(self, config: TranscriptionConfig, parent=None):
        super().__init__("Transkrypcja", parent)
        self.config = config
        self._loading = True

        root = QVBoxLayout(self)
        self.enable_check = QCheckBox("Automatycznie transkrybuj nagrania")
        self.enable_check.setToolTip(
            "Wyłącza kolejkę transkrypcji w tle. Po ponownym włączeniu "
            "program przetworzy także nagrania oczekujące z czasu wyłączenia."
        )
        self.enable_check.toggled.connect(self._push_settings)
        root.addWidget(self.enable_check)
        root.addWidget(QLabel(
            "Po włączeniu zaległe nagrania bez wyniku transkrypcji zostaną "
            "dodane do kolejki."
        ))

        self.enable_check.setChecked(self.config.enabled)
        self._loading = False

    def _push_settings(self, enabled: bool) -> None:
        if self._loading:
            return
        self.config.enabled = enabled
        self.settings_changed.emit()
        self.enabled_changed.emit(enabled)

    def set_enabled(self, enabled: bool) -> None:
        """Synchronizuje stan po zmianie przełącznika w Odtwarzaczu."""
        self.enable_check.blockSignals(True)
        self.enable_check.setChecked(enabled)
        self.enable_check.blockSignals(False)
