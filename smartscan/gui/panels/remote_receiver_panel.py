"""Settings and one-click download for a headless LAN receiver."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QCheckBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout

from ...config import RemoteReceiverConfig
from ...i18n import tr
from ...remote_client import RemoteClient


class RemoteReceiverPanel(QGroupBox):
    settings_changed = Signal()
    sync_requested = Signal()

    def __init__(self, config: RemoteReceiverConfig, parent=None):
        super().__init__(tr("Remote RTL-SDR receiver"), parent)
        self.config = config
        self._loading = True
        layout = QVBoxLayout(self)
        hint = QLabel(tr(
            "Use the always-on receiver for scanning and recording. Finished recordings are copied here for playback and local transcription."
        ))
        hint.setWordWrap(True)
        layout.addWidget(hint)
        form = QFormLayout()
        self.enabled = QCheckBox(tr("Use remote receiver"))
        self.enabled.setChecked(config.enabled)
        self.url = QLineEdit(config.url)
        self.url.setPlaceholderText("http://receiver.local:8765")
        self.token = QLineEdit(config.token)
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.delete_synced = QCheckBox(tr("Delete synced recordings from the receiver"))
        self.delete_synced.setChecked(config.delete_synced)
        self.delete_synced.setToolTip(tr(
            "Once a recording has been fully downloaded here, delete it from the receiver "
            "- only after the download is confirmed complete, never during a transfer."
        ))
        form.addRow("", self.enabled)
        form.addRow(tr("Server address:"), self.url)
        form.addRow(tr("Access token:"), self.token)
        form.addRow("", self.delete_synced)
        layout.addLayout(form)
        buttons = QHBoxLayout()
        self.test_btn = QPushButton(tr("Test connection"))
        self.sync_btn = QPushButton(tr("Download recordings"))
        self.status = QLabel("")
        self.status.setWordWrap(True)
        buttons.addWidget(self.test_btn)
        buttons.addWidget(self.sync_btn)
        buttons.addWidget(self.status, 1)
        layout.addLayout(buttons)
        self.enabled.toggled.connect(self._save)
        self.url.editingFinished.connect(self._save)
        self.token.editingFinished.connect(self._save)
        self.delete_synced.toggled.connect(self._save)
        self.test_btn.clicked.connect(self._test)
        self.sync_btn.clicked.connect(self.sync_requested.emit)
        self._loading = False

    def _save(self) -> None:
        if self._loading:
            return
        self.config.enabled = self.enabled.isChecked()
        self.config.url = self.url.text().strip().rstrip("/")
        self.config.token = self.token.text().strip()
        self.config.delete_synced = self.delete_synced.isChecked()
        self.settings_changed.emit()

    def _test(self) -> None:
        self._save()
        try:
            status = RemoteClient(self.config).status(timeout=1.5)  # GUI thread - keep the freeze short
            state = status.get("state", "IDLE")
            self.status.setText(tr("Connected — {state}").format(state=state))
        except Exception as exc:  # noqa: BLE001 - show the server's useful error verbatim
            self.status.setText(str(exc))

    def set_sync_status(self, message: str) -> None:
        self.status.setText(message)
