"""Recording panel: Record Only On Signal toggle, Single/Split mode, REC lamp, output dir."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox, QFileDialog, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QRadioButton, QVBoxLayout,
)

from ...audio_transcode import ffmpeg_available
from ...config import RecordingConfig
from ...i18n import tr
from ..widgets import RecIndicator


class RecordingPanel(QGroupBox):
    settings_changed = Signal()
    # Emitted only when the Auto-recording checkbox itself changes (not the
    # Single/Split mode or output folder) - mirrors TranscriptionPanel's
    # enabled_changed, so ScannerPanel's own copy of this same toggle (in
    # the Listen tab) can stay in sync without a signal loop - see
    # MainWindow.
    enabled_changed = Signal(bool)

    def __init__(self, config: RecordingConfig, parent=None):
        super().__init__(tr("Recording"), parent)
        self.config = config
        self._loading = True
        self._build_ui()
        self._load_from_config()
        self._loading = False

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        self.enable_check = QCheckBox(tr("Auto-recording (record automatically while a signal is detected)"))
        self.enable_check.setToolTip(tr(
            "Off by default - a fresh install doesn't write anything to disk "
            "until you turn this on. The same toggle is also available in "
            "the Listen tab, right next to the manual Record button."
        ))
        self.enable_check.toggled.connect(self._push_settings)
        root.addWidget(self.enable_check)

        mode_row = QHBoxLayout()
        self.single_radio = QRadioButton(tr("Single File (one continuous file)"))
        self.split_radio = QRadioButton(tr("Split Files (a separate file per transmission)"))
        self.single_radio.toggled.connect(self._push_settings)
        mode_row.addWidget(self.single_radio)
        mode_row.addWidget(self.split_radio)
        root.addLayout(mode_row)

        format_row = QHBoxLayout()
        self.wav_radio = QRadioButton(tr("WAV (uncompressed)"))
        self.mp3_radio = QRadioButton(tr("MP3 (compressed, requires ffmpeg)"))
        self.wav_radio.toggled.connect(self._push_settings)
        format_row.addWidget(self.wav_radio)
        format_row.addWidget(self.mp3_radio)
        root.addLayout(format_row)

        self.format_warning_label = QLabel()
        self.format_warning_label.setStyleSheet("color: #e8a33d;")
        self.format_warning_label.setWordWrap(True)
        self.format_warning_label.setVisible(False)
        root.addWidget(self.format_warning_label)

        dir_row = QHBoxLayout()
        self.dir_edit = QLineEdit()
        self.dir_edit.setReadOnly(True)
        browse_btn = QPushButton(tr("Choose folder..."))
        browse_btn.clicked.connect(self._browse_dir)
        dir_row.addWidget(QLabel(tr("Output folder:")))
        dir_row.addWidget(self.dir_edit, 1)
        dir_row.addWidget(browse_btn)
        root.addLayout(dir_row)

        status_row = QHBoxLayout()
        self.rec_indicator = RecIndicator()
        self.rec_label = QLabel("REC")
        self.file_label = QLabel("")
        self.file_label.setStyleSheet("color: #9aa0a6;")
        status_row.addWidget(self.rec_indicator)
        status_row.addWidget(self.rec_label)
        status_row.addWidget(self.file_label, 1)
        root.addLayout(status_row)

    def _load_from_config(self) -> None:
        self.enable_check.setChecked(self.config.enabled)
        self.single_radio.setChecked(self.config.mode == "single")
        self.split_radio.setChecked(self.config.mode == "split")
        self.wav_radio.setChecked(self.config.output_format != "mp3")
        self.mp3_radio.setChecked(self.config.output_format == "mp3")
        self.dir_edit.setText(self.config.output_dir)
        self._update_format_warning()

    def _update_format_warning(self) -> None:
        if self.mp3_radio.isChecked() and not ffmpeg_available():
            self.format_warning_label.setText(tr(
                "ffmpeg was not found on this system - recordings will be "
                "saved as WAV until it's installed."
            ))
            self.format_warning_label.setVisible(True)
        else:
            self.format_warning_label.setVisible(False)

    def _browse_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, tr("Recordings output folder"), self.config.output_dir)
        if path:
            self.dir_edit.setText(path)
            self.config.output_dir = path
            self.settings_changed.emit()

    def _push_settings(self, *_args) -> None:
        if self._loading:
            return
        enabled = self.enable_check.isChecked()
        changed = enabled != self.config.enabled
        self.config.enabled = enabled
        self.config.mode = "single" if self.single_radio.isChecked() else "split"
        self.config.output_format = "mp3" if self.mp3_radio.isChecked() else "wav"
        self._update_format_warning()
        self.settings_changed.emit()
        if changed:
            self.enabled_changed.emit(enabled)

    def set_enabled(self, enabled: bool) -> None:
        """Syncs state after the Auto-recording toggle changes in the Listen tab."""
        self.enable_check.blockSignals(True)
        self.enable_check.setChecked(enabled)
        self.enable_check.blockSignals(False)
        self.config.enabled = enabled

    def set_running(self, running: bool) -> None:
        self.enable_check.setEnabled(not running)
        self.single_radio.setEnabled(not running)
        self.split_radio.setEnabled(not running)
        self.wav_radio.setEnabled(not running)
        self.mp3_radio.setEnabled(not running)
        if not running:
            self.rec_indicator.set_on(False)
            self.file_label.setText("")

    def set_recording(self, active: bool, path: str) -> None:
        self.rec_indicator.set_on(active)
        self.file_label.setText(path if active else "")
