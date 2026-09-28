"""Recording panel: Record Only On Signal toggle, max concurrent recordings
limit, WAV/MP3 format, REC lamp, output dir."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox, QFileDialog, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QRadioButton, QSpinBox, QVBoxLayout,
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

        limit_row = QHBoxLayout()
        self.max_concurrent_spin = QSpinBox()
        self.max_concurrent_spin.setRange(0, 50)
        # Deliberately *not* setSpecialValueText("Unlimited") for 0 - a real
        # user-reported bug: QSpinBox's validator treats the special-value
        # text as the field's literal displayed text, so typing a digit
        # directly (without first selecting-all to clear "Unlimited") is
        # silently rejected as unparseable - confirmed reproducing this
        # exactly (typing "5" at the end of "Unlimited" left the field
        # unchanged; only the tiny spin-arrows/scroll wheel worked, which
        # isn't how most people try to edit a number field first). Plain "0"
        # has no such trap - typing over it always works - and the tooltip
        # below already says what 0 means.
        self.max_concurrent_spin.setToolTip(tr(
            "Caps how many transmissions can be recorded at the same time. "
            "Once this many recordings are already active, a newly detected "
            "transmission is simply not recorded until one of them ends - "
            "useful if a busy band (or one strong signal leaking into "
            "tightly-spaced neighbour channels) opens far more simultaneous "
            "recordings than you actually want. 0 = unlimited. Live "
            "scanning/listening is never capped by this, only recording."
        ))
        self.max_concurrent_spin.valueChanged.connect(self._push_settings)
        # "(0 = unlimited)" lives directly in the label text now, always
        # visible regardless of the current value - real user feedback after
        # a tooltip-only version shipped: "User też nie wie że 0 znaczy bez
        # limitu" (a user has no way to know 0 means unlimited). A separate
        # value-dependent hint label next to the spinbox was tried first and
        # removed again - redundant once the label itself always says it.
        limit_row.addWidget(QLabel(tr("Max simultaneous recordings (0 = unlimited):")))
        limit_row.addWidget(self.max_concurrent_spin)
        limit_row.addStretch(1)
        root.addLayout(limit_row)

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
        self.max_concurrent_spin.setValue(self.config.max_concurrent_recordings)
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
        self.config.max_concurrent_recordings = self.max_concurrent_spin.value()
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
        # Deliberately NOT disabled while running, unlike enable_check/wav_radio/
        # mp3_radio above - a real user-reported bug turned out to actually be
        # "this field is disabled the whole time you're scanning, with no
        # visible reason why, so typing/scrolling silently does nothing" (the
        # widget itself was fine - see ScannerEngine._at_recording_capacity(),
        # already reads self.config.recording.max_concurrent_recordings fresh
        # on every tick, so there's no persisted/opened-file state this could
        # ever corrupt mid-scan, unlike output_format or the enabled toggle
        # itself). Kept live-editable while scanning, the same way Trigger
        # Level/Squelch/Gain/Tuning/Linger already are (see widgets.SliderSpin).
        self.wav_radio.setEnabled(not running)
        self.mp3_radio.setEnabled(not running)
        if not running:
            self.rec_indicator.set_on(False)
            self.file_label.setText("")

    def set_recording(self, active: bool, path: str) -> None:
        self.rec_indicator.set_on(active)
        self.file_label.setText(path if active else "")
