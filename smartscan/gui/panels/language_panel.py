"""UI language selection - see smartscan/i18n.py for how translations are
loaded and how to add a new one. Deliberately takes effect on next restart,
not live (see i18n.py's module docstring for why) - this panel just says so
plainly instead of pretending to be more dynamic than it is.
"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox, QFormLayout, QGroupBox, QLabel, QVBoxLayout

from ...config import AppConfig
from ...i18n import available_languages, language_display_name, tr


class LanguagePanel(QGroupBox):
    settings_changed = Signal()

    def __init__(self, config: AppConfig, parent=None):
        super().__init__(tr("Language"), parent)
        self.config = config
        self._loading = True
        self._build_ui()
        self._load_from_config()
        self._loading = False

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        form = QFormLayout()
        self.language_combo = QComboBox()
        for code in available_languages():
            self.language_combo.addItem(language_display_name(code), code)
        self.language_combo.currentIndexChanged.connect(self._push_settings)
        form.addRow(tr("Language:"), self.language_combo)
        root.addLayout(form)

        note = QLabel(tr(
            "Takes effect after restarting the app. Missing your language? "
            "Adding one is just a text file - no programming needed, see "
            "smartscan/locale/README.md in the source for the three-step "
            "guide."
        ))
        note.setWordWrap(True)
        note.setStyleSheet("color: #9aa0a6;")
        root.addWidget(note)

    def _load_from_config(self) -> None:
        for i in range(self.language_combo.count()):
            if self.language_combo.itemData(i) == self.config.language:
                self.language_combo.setCurrentIndex(i)
                return

    def _push_settings(self, *_args) -> None:
        if self._loading:
            return
        self.config.language = self.language_combo.currentData()
        self.settings_changed.emit()
