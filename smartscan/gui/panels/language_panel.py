"""UI language selection - see smartscan/i18n.py for how translations are
loaded and how to add a new one. Applying a change still needs a restart
(every widget's text is set once, at construction time - see i18n.py's
module docstring), but the restart itself is offered as one click right
here (restart_requested), not a manual close-and-reopen.
"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox, QFormLayout, QGroupBox, QLabel, QVBoxLayout

from ...config import AppConfig
from ...i18n import AUTO, available_languages, language_display_name, tr


class LanguagePanel(QGroupBox):
    settings_changed = Signal()
    # Emitted right after a real change (not on initial load) - MainWindow
    # offers to restart immediately instead of leaving the user to notice on
    # their own that nothing changed and wonder why.
    restart_requested = Signal()

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
        self.language_combo.addItem(tr("Automatic (system language)"), AUTO)
        for code in available_languages():
            self.language_combo.addItem(language_display_name(code), code)
        self.language_combo.currentIndexChanged.connect(self._push_settings)
        form.addRow(tr("Language:"), self.language_combo)
        root.addLayout(form)

        note = QLabel(tr(
            "Missing your language? Adding one is just a text file - no "
            "programming needed, see smartscan/locale/README.md in the "
            "source for the three-step guide."
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
        new_lang = self.language_combo.currentData()
        changed = new_lang != self.config.language
        self.config.language = new_lang
        self.settings_changed.emit()
        if changed:
            self.restart_requested.emit()
