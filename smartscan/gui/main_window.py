"""Main application window assembling the panels from spec section 6."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QMainWindow, QMessageBox, QScrollArea, QTabWidget, QVBoxLayout, QWidget,
)

from .. import storage
from ..config import AppConfig
from ..i18n import tr
from ..models import Channel
from ..scanner_engine import ScannerEngine
from .panels.frequency_manager import FrequencyManager
from .panels.language_panel import LanguagePanel
from .panels.location_panel import LocationPanel
from .panels.playback_panel import PlaybackPanel
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
        self.language_panel = LanguagePanel(self.config)
        options_layout.addWidget(self.language_panel)
        options_layout.addStretch(1)

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
        self.scanner_panel.settings_changed.connect(self._save_config)
        self.scanner_panel.settings_changed.connect(self._maybe_live_rebuild)
        self.scanner_panel.memory_list_requested.connect(self._reveal_memory_list)
        self.scanner_panel.channel_weight_changed.connect(self._on_channel_weight_changed)
        self.scanner_panel.solo_band_changed.connect(self._on_solo_band_changed)
        self.recording_panel.settings_changed.connect(self._save_config)
        self.recency_boost_panel.settings_changed.connect(self._save_config)
        self.transcription_panel.settings_changed.connect(self._save_config)
        self.transcription_panel.enabled_changed.connect(self.playback_panel.set_transcription_enabled)
        self.playback_panel.transcription_enabled_changed.connect(self.transcription_panel.set_enabled)
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

    # -- persistence -----------------------------------------------------
    def _save_config(self) -> None:
        self.config.save()

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

    def _offer_restart(self) -> None:
        """LanguagePanel just changed the language - offer to apply it right
        now instead of leaving the user to notice on their own that nothing
        changed and go hunt for how to restart. See i18n.py's module
        docstring for why a restart is needed at all instead of a live
        retranslation."""
        reply = QMessageBox.question(
            self,
            tr("Restart to apply language"),
            tr(
                "Restart SmartScanATC now to switch the interface language? "
                "Any recording in progress will be stopped first, same as a "
                "normal Stop."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply == QMessageBox.StandardButton.Yes:
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
