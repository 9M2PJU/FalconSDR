"""Automatic transcription settings."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QCheckBox, QGroupBox, QLabel, QVBoxLayout

from ...config import TranscriptionConfig
from ...i18n import tr


class TranscriptionPanel(QGroupBox):
    """Automatic Whisper queue toggle in the Options tab."""

    settings_changed = Signal()
    enabled_changed = Signal(bool)

    def __init__(self, config: TranscriptionConfig, parent=None):
        super().__init__(tr("Transcription"), parent)
        self.config = config
        self._loading = True

        root = QVBoxLayout(self)
        self.enable_check = QCheckBox(tr("Automatically transcribe recordings"))
        self.enable_check.setToolTip(tr(
            "Turns off the background transcription queue. Re-enabling it "
            "also processes recordings that piled up while it was off."
        ))
        self.enable_check.toggled.connect(self._push_settings)
        root.addWidget(self.enable_check)
        root.addWidget(QLabel(tr(
            "Once enabled, any backlog of recordings with no transcription "
            "result yet will be queued."
        )))

        self.enable_check.setChecked(self.config.enabled)
        self._loading = False

    def _push_settings(self, enabled: bool) -> None:
        if self._loading:
            return
        self.config.enabled = enabled
        self.settings_changed.emit()
        self.enabled_changed.emit(enabled)

    def set_enabled(self, enabled: bool) -> None:
        """Syncs state after the toggle in Odtwarzacz changes."""
        self.enable_check.blockSignals(True)
        self.enable_check.setChecked(enabled)
        self.enable_check.blockSignals(False)
