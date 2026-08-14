"""Main application window assembling the three panels from spec section 6."""
from __future__ import annotations

from pathlib import Path

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QMainWindow, QMessageBox, QScrollArea, QTabWidget, QVBoxLayout, QWidget,
)

from .. import storage
from ..config import AppConfig
from ..models import Channel
from ..scanner_engine import ScannerEngine
from .panels.frequency_manager import FrequencyManager
from .panels.playback_panel import PlaybackPanel
from .panels.recording_panel import RecordingPanel
from .panels.scanner_panel import ScannerPanel

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

        # Nasluch dziala jak strona do przewijania (jedno QScrollArea ponizej
        # obejmuje cala zakladke), a nie jak zestaw splitterow do przeciagania
        # - kazda sekcja dostaje tyle miejsca ile faktycznie potrzebuje
        # (Lista pamieci potrafi byc spora po rozwinieciu), zamiast musiec byc
        # sciskana zeby zrobic miejsce sasiadowi. Pierwsze sekcje (urzadzenie,
        # pasma, widmo, przyciski Start/Stop) zostaja w miare widoczne od razu;
        # od Listy pamieci w dol przewija sie w miare potrzeby.
        listen_tab = QWidget()
        layout = QVBoxLayout(listen_tab)

        self.scanner_panel = ScannerPanel(self.config, self.channels)
        self.freq_manager = FrequencyManager(
            self.channels, self.blacklist, self.config.default_blacklist_margin_hz, self.channel_folders,
        )
        self.freq_manager.setMinimumHeight(420)
        self.recording_panel = RecordingPanel(self.config.recording)

        layout.addWidget(self.scanner_panel)
        layout.addWidget(self.freq_manager)
        layout.addWidget(self.recording_panel)

        # The stacked panels' combined minimum height (spectrum view, level
        # meter, band list, recording controls, ...) comfortably fits a tall
        # primary monitor but can exceed a shorter secondary one - without a
        # scroll area, that forces the whole window's minimum size beyond the
        # smaller screen's available height, so it can't actually maximize
        # (isMaximized() reports True but the window stays oversized).
        self.listen_scroll = QScrollArea()
        self.listen_scroll.setWidgetResizable(True)
        self.listen_scroll.setWidget(listen_tab)

        self.playback_panel = PlaybackPanel(self.config)
        self.playback_panel.set_known_frequencies(self.channels)
        self.playback_panel.set_known_folders(self.channel_folders)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.listen_scroll, "Nasłuch")
        self.tabs.addTab(self.playback_panel, "Odtwarzacz")
        self.setCentralWidget(self.tabs)

        self.statusBar().showMessage("Gotowy.")

        self.scanner_panel.start_requested.connect(self._start_scanner)
        self.scanner_panel.stop_requested.connect(self._stop_scanner)
        self.scanner_panel.skip_requested.connect(self._on_skip)
        self.scanner_panel.skip_blacklist_requested.connect(self._on_skip_blacklist)
        self.scanner_panel.mute_toggled.connect(self._on_mute_toggled)
        self.scanner_panel.settings_changed.connect(self._save_config)
        self.scanner_panel.settings_changed.connect(self._maybe_live_rebuild)
        self.scanner_panel.memory_list_requested.connect(self._reveal_memory_list)
        self.scanner_panel.channel_weight_changed.connect(self._on_channel_weight_changed)
        self.recording_panel.settings_changed.connect(self._save_config)
        self.freq_manager.channels_changed.connect(self._on_channels_changed)
        self.freq_manager.blacklist_changed.connect(self._on_blacklist_changed)
        self.freq_manager.folders_changed.connect(self._on_folders_changed)
        self.playback_panel.status_message.connect(self.statusBar().showMessage)
        self.playback_panel.channel_saved.connect(self._on_channel_saved)

    # -- persistence -----------------------------------------------------
    def _save_config(self) -> None:
        self.config.save()

    def _maybe_live_rebuild(self) -> None:
        """Any change that can affect what the scanner watches (bands, step,
        modulation, weights, the Memory List master toggle, ...) should take
        effect immediately on a running scan - no more Stop/Start required."""
        if self.engine is not None:
            self.engine.request_rebuild()

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

    def _reveal_memory_list(self) -> None:
        """"Lista pamieci" w zakladce Skaner: przewija zakladke Nasluch do
        panelu Kanaly i ustawia tam fokus na wyszukiwarce."""
        self.tabs.setCurrentIndex(0)
        self.freq_manager.reveal_channels()
        self.listen_scroll.ensureWidgetVisible(self.freq_manager)

    def _on_channel_saved(self, channel: Channel) -> None:
        """"Zapisz czestotliwosc" w zakladce Odtwarzacz: dopisuje nowy kanal
        do Listy pamieci albo nadpisuje nazwe/folder istniejacego (dopasowanie
        po czestotliwosci), zamiast tworzyc duplikat."""
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
        self.statusBar().showMessage(
            f"Zapisano {channel.freq_hz / 1e6:.4f} MHz w Liście pamięci" +
            (f" jako „{channel.name}”." if channel.name else "."), 5000,
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
        s.gain_range_changed.connect(self.scanner_panel.update_gain_range)
        s.log.connect(self.statusBar().showMessage)
        s.finished.connect(self._on_engine_finished)

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
        self.statusBar().showMessage(f"Zablokowano {freq_hz / 1e6:.4f} MHz (lockout).", 5000)

    def _on_engine_finished(self) -> None:
        if self.engine is not None:
            self.engine.join(timeout=2.0)
        self.engine = None
        self.scanner_panel.set_running(False)
        self.recording_panel.set_running(False)

    def closeEvent(self, event) -> None:
        if self.engine is not None:
            self.engine.request_stop()
            self.engine.join(timeout=3.0)
        self.playback_panel.shutdown()
        self._save_config()
        storage.save_channels(self.channels)
        storage.save_blacklist(self.blacklist)
        event.accept()
