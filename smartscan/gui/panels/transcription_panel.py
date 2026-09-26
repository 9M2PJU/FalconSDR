"""Automatic transcription settings - and, when faster-whisper isn't
installed yet, the one-click optional installer for it (see
transcription_install.py). Three states, not just on/off:
"not installed" (install button shown, checkbox hidden), "installed but
off", "installed and on" - see _refresh_installed_state()."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox, QGroupBox, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout,
)

from ...config import TranscriptionConfig
from ...i18n import tr
from ...playback.transcription_availability import is_transcription_installed
from ...playback.transcription_install import TranscriptionInstallWorker


class TranscriptionPanel(QGroupBox):
    """Automatic Whisper queue toggle in the Options tab."""

    settings_changed = Signal()
    enabled_changed = Signal(bool)
    # Emitted only once an install finished in a way that needs a process
    # restart to take effect (the PEP-668 venv fallback) - MainWindow reuses
    # the same restart flow LanguagePanel already has.
    restart_requested = Signal()
    # Emitted when an install finished WITHOUT needing a restart (installed
    # straight into the running interpreter) - MainWindow uses this to
    # refresh PlaybackPanel's own checkbox/banner immediately, since nothing
    # else would otherwise tell it "it's available now" mid-session.
    installed_changed = Signal()

    def __init__(self, config: TranscriptionConfig, parent=None):
        super().__init__(tr("Transcription"), parent)
        self.config = config
        self._loading = True
        self._install_worker: TranscriptionInstallWorker | None = None

        root = QVBoxLayout(self)

        # -- "not installed yet" state --------------------------------
        self.install_intro_label = QLabel(tr(
            "Optional: automatic speech-to-text for recordings (Whisper), "
            "shown in the Odtwarzacz tab. Not installed - it's a large, "
            "separate download (models + engine), so it's opt-in rather "
            "than bundled with the app."
        ))
        self.install_intro_label.setWordWrap(True)
        root.addWidget(self.install_intro_label)

        self.install_gpu_check = QCheckBox(tr("Also install GPU (NVIDIA/CUDA) support"))
        self.install_gpu_check.setToolTip(tr(
            "Optional and heavier still - only useful if you have an NVIDIA "
            "GPU. Transcription works on CPU either way, just slower."
        ))
        root.addWidget(self.install_gpu_check)

        self.install_btn = QPushButton(tr("Install transcription"))
        self.install_btn.clicked.connect(self._start_install)
        root.addWidget(self.install_btn)

        self.install_log = QPlainTextEdit()
        self.install_log.setReadOnly(True)
        self.install_log.setMaximumHeight(120)
        self.install_log.setVisible(False)
        root.addWidget(self.install_log)

        self.install_status_label = QLabel("")
        self.install_status_label.setWordWrap(True)
        self.install_status_label.setVisible(False)
        root.addWidget(self.install_status_label)

        # -- "installed" state ------------------------------------------
        self.enable_check = QCheckBox(tr("Automatically transcribe recordings"))
        self.enable_check.setToolTip(tr(
            "Turns off the background transcription queue. Re-enabling it "
            "also processes recordings that piled up while it was off."
        ))
        self.enable_check.toggled.connect(self._push_settings)
        root.addWidget(self.enable_check)
        self.backlog_label = QLabel(tr(
            "Once enabled, any backlog of recordings with no transcription "
            "result yet will be queued."
        ))
        root.addWidget(self.backlog_label)

        self.enable_check.setChecked(self.config.enabled)
        self._refresh_installed_state()
        self._loading = False

    def _refresh_installed_state(self) -> None:
        installed = is_transcription_installed()
        self.install_intro_label.setVisible(not installed)
        self.install_gpu_check.setVisible(not installed)
        self.install_btn.setVisible(not installed)
        self.enable_check.setVisible(installed)
        self.backlog_label.setVisible(installed)
        if installed:
            self.install_log.setVisible(False)
            self.install_status_label.setVisible(False)

    def _start_install(self) -> None:
        self.install_btn.setEnabled(False)
        self.install_gpu_check.setEnabled(False)
        self.install_log.clear()
        self.install_log.setVisible(True)
        self.install_status_label.setText(tr("Installing… this can take a few minutes."))
        self.install_status_label.setVisible(True)
        self._install_worker = TranscriptionInstallWorker(with_gpu=self.install_gpu_check.isChecked())
        self._install_worker.output_line.connect(self.install_log.appendPlainText)
        self._install_worker.finished_install.connect(self._on_install_finished)
        self._install_worker.start()

    def _on_install_finished(self, success: bool, needs_restart: bool, error_message: str) -> None:
        self.install_btn.setEnabled(True)
        self.install_gpu_check.setEnabled(True)
        if not success:
            self.install_status_label.setText(tr(
                "Installation failed - see the log above for details. You can try again."
            ))
            return
        if needs_restart:
            self.install_status_label.setText(tr(
                "Installed. Restart SmartScanATC to start using it."
            ))
            self.restart_requested.emit()
            return
        self.install_status_label.setText(tr("Installed."))
        self._refresh_installed_state()
        self.installed_changed.emit()

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
