"""Main application window assembling the panels from spec section 6."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QMainWindow, QMessageBox, QPushButton, QScrollArea, QTabWidget, QVBoxLayout, QWidget,
)

from .. import storage
from ..config import AppConfig
from ..i18n import tr
from ..military_presets import USER_PRESETS_PATH
from ..models import Channel
from ..scanner_engine import ScannerEngine
from .legal_disclaimer import confirm_recording_disclaimer, maybe_show_launch_disclaimer
from .panels.frequency_manager import FrequencyManager
from .panels.language_panel import LanguagePanel
from .panels.location_panel import LocationPanel
from .panels.playback_panel import PlaybackPanel
from .panels.preset_manager_panel import PresetManagerPanel
from .panels.recency_boost_panel import RecencyBoostPanel
from .panels.recording_panel import RecordingPanel
from .panels.scanner_panel import ScannerPanel
from .panels.transcription_panel import TranscriptionPanel

ICON_PATH = Path(__file__).resolve().parent / "resources" / "app_icon.svg"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SmartScanATC")
        self.setWindowIcon(QIcon(str(ICON_PATH)))
        self.resize(1280, 900)

        self.config = AppConfig.load()
        self.channels = storage.load_channels()
        self.blacklist = storage.load_blacklist()
        self.channel_folders: list[str] = list(self.config.channel_folders)
        self.engine: ScannerEngine | None = None

        # Listen behaves like a scrollable page (a QScrollArea wraps the tab)
        # instead of a set of draggable splitters - a section gets exactly as
        # much room as it actually needs. Without this, the scanner
        # (spectrum + level meter + band list + buttons) can force a minimum
        # window size taller than what's available on a smaller (e.g.
        # second) monitor, so the window can't actually maximize
        # (isMaximized() returns True, but the window stays oversized anyway).
        listen_tab = QWidget()
        layout = QVBoxLayout(listen_tab)

        self.scanner_panel = ScannerPanel(self.config, self.channels)
        layout.addWidget(self.scanner_panel)

        self.listen_scroll = QScrollArea()
        self.listen_scroll.setWidgetResizable(True)
        self.listen_scroll.setWidget(listen_tab)

        self.playback_panel = PlaybackPanel(self.config)
        self.playback_panel.set_known_frequencies(self.channels)
        self.playback_panel.set_known_folders(self.channel_folders)

        # Channels (Memory List + Blacklist) is already its own QTabWidget
        # with two sub-tabs of its own, so it becomes a top-level tab without
        # any extra wrapping.
        self.freq_manager = FrequencyManager(
            self.channels, self.blacklist, self.config.default_blacklist_margin_hz, self.channel_folders,
        )

        # Options groups settings not directly tied to running the scanner.
        self.options_tab = QWidget()
        options_layout = QVBoxLayout(self.options_tab)
        self.recording_panel = RecordingPanel(self.config.recording)
        options_layout.addWidget(self.recording_panel)
        self.recency_boost_panel = RecencyBoostPanel(self.config.recency_boost)
        options_layout.addWidget(self.recency_boost_panel)
        self.transcription_panel = TranscriptionPanel(self.config.transcription)
        options_layout.addWidget(self.transcription_panel)
        self.location_panel = LocationPanel(self.config.location)
        options_layout.addWidget(self.location_panel)
        self.preset_manager_panel = PresetManagerPanel()
        options_layout.addWidget(self.preset_manager_panel)
        self.language_panel = LanguagePanel(self.config)
        options_layout.addWidget(self.language_panel)
        options_layout.addStretch(1)
        reset_btn = QPushButton(tr("Reset to defaults…"))
        reset_btn.setToolTip(tr(
            "Resets all settings, the Memory List, blacklist, and any custom/"
            "hidden band presets back to a fresh install. Recordings "
            "themselves are never touched."
        ))
        reset_btn.clicked.connect(self._on_reset_to_defaults)
        options_layout.addWidget(reset_btn)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.listen_scroll, tr("Listen"))
        self.tabs.addTab(self.playback_panel, tr("Playback"))
        self.tabs.addTab(self.freq_manager, tr("Channels"))
        self.tabs.addTab(self.playback_panel.stats_tab, tr("Statistics"))
        self.tabs.addTab(self.options_tab, tr("Options"))
        self.setCentralWidget(self.tabs)

        self.statusBar().showMessage(tr("Ready."))

        self.scanner_panel.start_requested.connect(self._start_scanner)
        self.scanner_panel.stop_requested.connect(self._stop_scanner)
        self.scanner_panel.skip_requested.connect(self._on_skip)
        self.scanner_panel.skip_blacklist_requested.connect(self._on_skip_blacklist)
        self.scanner_panel.mute_toggled.connect(self._on_mute_toggled)
        self.scanner_panel.manual_record_toggled.connect(self._on_manual_record_toggled)
        self.scanner_panel.auto_recording_toggled.connect(self._on_scanner_auto_recording_toggled)
        self.recording_panel.enabled_changed.connect(self._on_options_auto_recording_toggled)
        self.scanner_panel.raw_iq_record_toggled.connect(self._on_raw_iq_record_toggled)
        self.scanner_panel.settings_changed.connect(self._save_config)
        self.scanner_panel.settings_changed.connect(self._maybe_live_rebuild)
        self.scanner_panel.memory_list_requested.connect(self._reveal_memory_list)
        self.scanner_panel.channel_weight_changed.connect(self._on_channel_weight_changed)
        self.scanner_panel.solo_band_changed.connect(self._on_solo_band_changed)
        self.recording_panel.settings_changed.connect(self._save_config)
        self.recency_boost_panel.settings_changed.connect(self._save_config)
        self.transcription_panel.settings_changed.connect(self._save_config)
        self.transcription_panel.enabled_changed.connect(self.playback_panel.set_transcription_enabled)
        self.transcription_panel.restart_requested.connect(self._offer_restart_for_transcription)
        self.transcription_panel.installed_changed.connect(self.playback_panel.refresh_transcription_availability)
        self.playback_panel.transcription_enabled_changed.connect(self.transcription_panel.set_enabled)
        self.preset_manager_panel.presets_changed.connect(self.scanner_panel.reload_preset_combo)
        self.location_panel.settings_changed.connect(self._save_config)
        self.language_panel.settings_changed.connect(self._save_config)
        self.language_panel.restart_requested.connect(self._offer_restart)
        self.freq_manager.channels_changed.connect(self._on_channels_changed)
        self.freq_manager.blacklist_changed.connect(self._on_blacklist_changed)
        self.freq_manager.folders_changed.connect(self._on_folders_changed)
        self.playback_panel.status_message.connect(self.statusBar().showMessage)
        self.playback_panel.channel_saved.connect(self._on_channel_saved)
        self.playback_panel.location_setup_requested.connect(self._reveal_location)
        self.playback_panel.stats_tab.frequencySelected.connect(self._on_stats_freq_selected)

        self._maybe_show_launch_disclaimer()

    # -- persistence -----------------------------------------------------
    def _save_config(self) -> None:
        self.config.save()

    # -- legal notices -----------------------------------------------------
    def _maybe_show_launch_disclaimer(self) -> None:
        if maybe_show_launch_disclaimer(self, self.config.legal_disclaimer):
            self._save_config()

    def _confirm_recording_disclaimer(self) -> bool:
        """Gate shared by every recording-starting action (auto-recording,
        manual Record, raw IQ) - see confirm_recording_disclaimer()'s own
        docstring for why they all funnel through one shared notice/seen-
        language state instead of three separate ones."""
        accepted = confirm_recording_disclaimer(self, self.config.legal_disclaimer)
        if accepted:
            self._save_config()
        return accepted

    def _maybe_live_rebuild(self) -> None:
        """Any change that can affect what the scanner watches (bands, step,
        modulation, weights, the Memory List master toggle, ...) should take
        effect immediately on a running scan - no more Stop/Start required."""
        if self.engine is not None:
            self.engine.request_rebuild()

    def _on_solo_band_changed(self, band) -> None:
        """A band's "advanced" edit mode started/stopped (Scanner tab) -
        restrict (or un-restrict) a running scan to just that band. See
        ScannerPanel.solo_band_changed / ScannerEngine.set_solo_band."""
        if self.engine is not None:
            self.engine.set_solo_band(band)

    def _on_channel_weight_changed(self) -> None:
        """A channel's weight was edited inline from the Scanner tab's Memory
        List row - persist it and apply it to a running scan right away."""
        storage.save_channels(self.channels)
        self.freq_manager.set_channels(self.channels)
        self._maybe_live_rebuild()

    def _on_channels_changed(self, channels: list) -> None:
        self.channels = channels
        storage.save_channels(channels)
        if self.engine is not None:
            self.engine.update_channels(channels)
        self.playback_panel.set_known_frequencies(self.channels)
        self.scanner_panel.set_channels(self.channels)

    def _on_blacklist_changed(self, blacklist: list) -> None:
        self.blacklist = blacklist
        storage.save_blacklist(blacklist)
        if self.engine is not None:
            self.engine.update_blacklist(blacklist)

    def _on_folders_changed(self, folders: list) -> None:
        self.channel_folders = folders
        self.config.channel_folders = folders
        self._save_config()
        self.playback_panel.set_known_folders(folders)

    def _on_stats_freq_selected(self, freq_khz: int) -> None:
        """Statistics is now its own top-level tab, so double-clicking a
        frequency in the stats table (PlaybackPanel already filtered
        Odtwarzacz to that frequency) still needs to switch the visible tab
        to Odtwarzacz by hand, or the result wouldn't be visible at all."""
        self.tabs.setCurrentWidget(self.playback_panel)

    def _reveal_memory_list(self) -> None:
        """"Memory list" shortcut in the Scanner tab: switches to the
        Channels tab (Memory List sub-tab) and focuses its search box."""
        self.tabs.setCurrentWidget(self.freq_manager)
        self.freq_manager.reveal_channels()

    def _reveal_location(self) -> None:
        """PlaybackPanel asked to be sent here because the user clicked
        FlightRadar24/ADSBExchange with no location configured yet - switch
        to Options and put the cursor right on the field that needs filling
        in, instead of just landing on the tab and leaving them to hunt for it."""
        self.tabs.setCurrentWidget(self.options_tab)
        self.location_panel.focus_input()

    def _on_channel_saved(self, channel: Channel) -> None:
        """"Save frequency" in the Odtwarzacz tab: appends a new channel to
        the Memory List, or overwrites the name/folder of an existing one
        (matched by frequency), instead of creating a duplicate."""
        existing = next((c for c in self.channels if c.freq_hz == channel.freq_hz), None)
        if existing is not None:
            existing.name = channel.name
            existing.folder = channel.folder
        else:
            self.channels.append(channel)

        if channel.folder and channel.folder not in self.channel_folders:
            self.channel_folders.append(channel.folder)
            self._on_folders_changed(self.channel_folders)

        storage.save_channels(self.channels)
        if self.engine is not None:
            self.engine.update_channels(self.channels)
        self.freq_manager.set_channels(self.channels)
        self.freq_manager.set_folders(self.channel_folders)
        self.playback_panel.set_known_frequencies(self.channels)
        self.scanner_panel.set_channels(self.channels)
        name_suffix = tr(" as “{name}”.").format(name=channel.name) if channel.name else "."
        self.statusBar().showMessage(
            tr("Saved {freq:.4f} MHz to the Memory List").format(freq=channel.freq_hz / 1e6) + name_suffix, 5000,
        )

    # -- scanner lifecycle -------------------------------------------------
    def _start_scanner(self) -> None:
        if self.engine is not None:
            return
        self._save_config()
        self.engine = ScannerEngine(self.config, self.channels, self.blacklist)
        s = self.engine.signals
        s.state_changed.connect(self.scanner_panel.update_state)
        s.level_changed.connect(self.scanner_panel.update_level)
        s.freq_changed.connect(self.scanner_panel.update_freq)
        s.recording_changed.connect(self.recording_panel.set_recording)
        s.recording_finished.connect(self.playback_panel.ingest_finished_recording)
        s.manual_recording_changed.connect(self._on_manual_recording_changed)
        s.raw_iq_recording_changed.connect(self._on_raw_iq_recording_changed)
        s.blacklisted.connect(self._on_auto_lockout)
        s.spectrum_ready.connect(self.scanner_panel.update_spectrum)
        s.cycle_time_changed.connect(self.scanner_panel.update_cycle_time)
        s.gain_range_changed.connect(self.scanner_panel.update_gain_range)
        s.log.connect(self.statusBar().showMessage)
        s.finished.connect(self._on_engine_finished)

        # Pick up a band already in "advanced" edit mode (Scanner tab), if
        # the user opened it before ever pressing Start this session - a
        # freshly-constructed ScannerEngine otherwise has no idea one was
        # in progress (see ScannerPanel.editing_band).
        self.engine.set_solo_band(self.scanner_panel.editing_band)

        self.scanner_panel.set_running(True)
        self.recording_panel.set_running(True)
        self.engine.set_live_muted(self.scanner_panel.mute_btn.isChecked())
        self.engine.start()

    def _stop_scanner(self) -> None:
        if self.engine is not None:
            self.engine.request_stop()
        self.scanner_panel.set_running(False)

    def _on_skip(self) -> None:
        if self.engine is not None:
            self.engine.request_skip()

    def _on_skip_blacklist(self) -> None:
        if self.engine is not None:
            self.engine.request_skip_and_blacklist()

    def _on_mute_toggled(self, muted: bool) -> None:
        if self.engine is not None:
            self.engine.set_live_muted(muted)

    def _on_manual_record_toggled(self, checked: bool) -> None:
        """Record/Stop recording in the Listen tab - see
        ScannerEngine.start_manual_recording()'s docstring for what this
        actually does (continuous, trigger-independent recording of the
        live channel). Can't work without a running engine to feed it -
        the button itself is disabled then too (see ScannerPanel.set_running()),
        this is just the defensive fallback for the click reaching here anyway."""
        if self.engine is None:
            self.scanner_panel.set_manual_recording_active(False)
            return
        if checked:
            if not self._confirm_recording_disclaimer():
                self.scanner_panel.set_manual_recording_active(False)
                return
            self.engine.start_manual_recording()
        else:
            self.engine.stop_manual_recording()

    def _on_manual_recording_changed(self, active: bool, _path: str) -> None:
        """ScannerEngine stopped (or started) a manual recording on its own
        side - e.g. _teardown() force-stopping it when the scanner stops.
        Keeps the button in sync without it being the one that initiated
        the change."""
        self.scanner_panel.set_manual_recording_active(active)

    def _on_raw_iq_record_toggled(self, checked: bool) -> None:
        """Record raw IQ / Stop recording IQ in the Listen tab (right under
        Mute) - see ScannerEngine.start_raw_iq_recording()'s docstring.
        Can't work without a running engine to feed it - the button itself
        is disabled then too (see ScannerPanel.set_running()), this is just
        the defensive fallback for the click reaching here anyway."""
        if self.engine is None:
            self.scanner_panel.set_raw_iq_recording_active(False)
            return
        if checked:
            if not self._confirm_recording_disclaimer():
                self.scanner_panel.set_raw_iq_recording_active(False)
                return
            self.engine.start_raw_iq_recording()
        else:
            self.engine.stop_raw_iq_recording()

    def _on_raw_iq_recording_changed(self, active: bool, _path: str) -> None:
        """ScannerEngine stopped (or started) raw IQ recording on its own
        side - e.g. _teardown() force-stopping it when the scanner stops."""
        self.scanner_panel.set_raw_iq_recording_active(active)

    def _on_scanner_auto_recording_toggled(self, checked: bool) -> None:
        """The Auto-recording checkbox in the Listen tab changed - keep
        config and the Options-tab copy of the same setting in sync (see
        RecordingPanel.enabled_changed for the reverse direction)."""
        if checked and not self._confirm_recording_disclaimer():
            self.scanner_panel.set_auto_recording(False)
            return
        self.config.recording.enabled = checked
        self.recording_panel.set_enabled(checked)
        self.playback_panel.update_recording_notice()
        self._save_config()

    def _on_options_auto_recording_toggled(self, checked: bool) -> None:
        if checked and not self._confirm_recording_disclaimer():
            self.recording_panel.set_enabled(False)
            return
        self.config.recording.enabled = checked
        self.scanner_panel.set_auto_recording(checked)
        self.playback_panel.update_recording_notice()
        self._save_config()

    def _on_auto_lockout(self, freq_hz: int) -> None:
        self.freq_manager.add_lockout(freq_hz)
        self.statusBar().showMessage(tr("Blacklisted {freq:.4f} MHz (lockout).").format(freq=freq_hz / 1e6), 5000)

    def _on_engine_finished(self) -> None:
        if self.engine is not None:
            self.engine.join(timeout=2.0)
        self.engine = None
        self.scanner_panel.set_running(False)
        self.recording_panel.set_running(False)

    def _shutdown(self) -> None:
        """Stops the scanner (if running) and persists everything - shared by
        closeEvent() and _restart_app(), so restarting to apply a language
        change leaves state exactly as clean as a normal quit would."""
        if self.engine is not None:
            self.engine.request_stop()
            self.engine.join(timeout=3.0)
        self.playback_panel.shutdown()
        self._save_config()
        storage.save_channels(self.channels)
        storage.save_blacklist(self.blacklist)

    def closeEvent(self, event) -> None:
        self._shutdown()
        event.accept()

    def _offer_restart(self, body: str | None = None) -> None:
        """Offers to restart right now instead of leaving the user to notice
        on their own that nothing changed yet and go hunt for how to
        restart. Originally just for LanguagePanel (see i18n.py's module
        docstring for why a restart is needed there at all instead of a live
        retranslation) - also reused by the transcription installer's
        PEP-668 venv fallback (see _offer_restart_for_transcription()),
        which needs a *different* explanation but the exact same restart
        mechanism, hence the overridable ``body``."""
        if body is None:
            body = tr(
                "Restart SmartScanATC now to switch the interface language? "
                "Any recording in progress will be stopped first, same as a "
                "normal Stop."
            )
        reply = QMessageBox.question(
            self,
            tr("Restart to apply changes"),
            body,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._restart_app()

    def _offer_restart_for_transcription(self) -> None:
        self._offer_restart(tr(
            "Transcription was just installed into a separate environment - "
            "restart SmartScanATC now to start using it? Any recording in "
            "progress will be stopped first, same as a normal Stop."
        ))

    def _on_reset_to_defaults(self) -> None:
        """Options -> "Reset to defaults" - a real factory reset (settings,
        Memory List, blacklist, custom/hidden band presets), never the
        recordings themselves - those live in the user's chosen output
        folder on disk, entirely independent of anything this resets.
        Resets in-memory state first, *then* restarts (see _restart_app()/
        _shutdown()) rather than the other way around - _shutdown() itself
        persists whatever's currently in self.config/self.channels/
        self.blacklist, so doing that *before* resetting them would silently
        overwrite the reset with the still-old values right back."""
        reply = QMessageBox.question(
            self, tr("Reset to defaults"),
            tr(
                "This resets every setting, the Memory List, the blacklist, "
                "and any custom or hidden band presets back to a fresh "
                "install. Your recordings themselves are never touched. "
                "This cannot be undone."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.config = AppConfig()
        self.channels = []
        self.blacklist = []
        try:
            USER_PRESETS_PATH.unlink()
        except FileNotFoundError:
            pass
        self._restart_app()

    def _restart_app(self) -> None:
        """Shuts down cleanly, then re-execs this same process - the same
        self-re-exec trick main.py already uses for the venv redirect, so a
        language change applies with one click instead of a manual close-
        and-reopen. os.execv() replaces the process image outright (nothing
        after a successful call ever runs), which is exactly why _shutdown()
        must happen first - there's no "after" to clean up in."""
        self._shutdown()
        try:
            os.execv(sys.executable, [sys.executable, *sys.argv])
        except OSError as exc:
            QMessageBox.warning(
                self, tr("Restart failed"),
                tr("Couldn't restart automatically ({error}). Please close and reopen the app by hand.").format(
                    error=exc,
                ),
            )
