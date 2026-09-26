"""Odtwarzacz tab: recording browser, statistics and player bar, carried over
from a separate ATC Scanner Browser program. Runs alongside the Listen tab -
the scanner engine and recording live in MainWindow independently of which
tab is currently visible."""
from __future__ import annotations

import subprocess
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import QModelIndex, QPoint, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication, QIcon, QKeySequence, QShortcut
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSlider,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ...config import AppConfig
from ...i18n import tr
from ...models import Channel, Modulation
from ...playback.audio_analysis import analyze_wav, compute_waveform_peaks
from ...playback.audio_player import AudioPlayer
from ...playback.config import POLAND_TZ, UTC_TZ
from ...playback.favorites_store import FavoritesStore
from ...playback.freq_panel import ALL_ITEM_FREQ, FAVORITES_ITEM, FrequencyPanel
from ...playback.models import Recording, TranscriptionResult
from ...playback.models_qt import COL_DATE, COL_QUALITY, RecordingsFilterProxy, RecordingsTableModel
from ...playback.organizer import parse_filename
from ...playback.quality_cache import QualityCache
from ...playback.stats import counts_per_day
from ...playback.stats_tab import StatsTab
from ...playback.transcription_availability import is_transcription_installed
from ...playback.transcription_cache import TranscriptionCache
from ...playback.transcription_worker import TranscriptionWorker
from ...playback.worker import RefreshWorker
from ..widgets import WaveformView, apply_emoji_font

RESOURCES_DIR = Path(__file__).resolve().parent.parent / "resources"

NAV_KEYS = {
    Qt.Key.Key_Up,
    Qt.Key.Key_Down,
    Qt.Key.Key_PageUp,
    Qt.Key.Key_PageDown,
    Qt.Key.Key_Home,
    Qt.Key.Key_End,
}


class RecordingsTableView(QTableView):
    """QTableView that also signals keyboard navigation (for auto-play) and Delete (for deletion)."""

    navigated = Signal()
    deleteRequested = Signal()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Delete:
            self.deleteRequested.emit()
            return
        super().keyPressEvent(event)
        if event.key() in NAV_KEYS:
            self.navigated.emit()


def _format_time(ms: int) -> str:
    total_sec = max(0, ms) // 1000
    m, s = divmod(total_sec, 60)
    return f"{m}:{s:02d}"


class _SaveFrequencyDialog(QDialog):
    """Saves the currently selected frequency to the Memory List (Listen ->
    Channels tab): name + optional folder. If the frequency is already
    known, MainWindow overwrites its entry (see PlaybackPanel.channel_saved)
    instead of creating a duplicate."""

    def __init__(self, freq_khz: int, current_name: str, folders: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Save frequency"))
        form = QFormLayout(self)
        form.addRow(tr("Frequency:"), QLabel(f"{freq_khz / 1000:.3f} MHz"))

        self.name_edit = QLineEdit(current_name)
        self.name_edit.setPlaceholderText(tr("e.g. Downtown Tower"))
        apply_emoji_font(self.name_edit)  # e.g. "🚒 KRG 1" - see apply_emoji_font()
        form.addRow(tr("Name:"), self.name_edit)

        self.folder_combo = QComboBox()
        self.folder_combo.setEditable(True)
        self.folder_combo.setToolTip(tr(
            "Folder in the Memory List. Pick an existing one or type a new "
            "name to create it right away."
        ))
        self.folder_combo.addItem(tr("(no folder)"), "")
        for f in folders:
            self.folder_combo.addItem(f, f)
        form.addRow(tr("Folder:"), self.folder_combo)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self) -> tuple[str, str]:
        folder = self.folder_combo.currentText().strip()
        if folder == tr("(no folder)"):
            folder = ""
        return self.name_edit.text().strip(), folder


class PlaybackPanel(QWidget):
    """The "Odtwarzacz" tab: recording browser + statistics + player bar.

    The recordings folder is re-read from ``AppConfig.recording.output_dir``
    on every refresh - the same setting the Listen tab uses for writing - so
    there's no need to keep two separate configs in sync like the old,
    separate two-program setup required.
    """

    status_message = Signal(str, int)  # (text, display duration in ms; 0 = no limit)
    channel_saved = Signal(object)  # Channel - "Save frequency"; MainWindow merges it into the Memory List
    transcription_enabled_changed = Signal(bool)
    # Emitted when the user clicks FlightRadar24/ADSBExchange with no
    # receiver location configured yet - MainWindow switches to Options and
    # focuses the field that needs filling in (see LocationPanel).
    location_setup_requested = Signal()

    def __init__(self, config: AppConfig, parent=None):
        super().__init__(parent)
        self.config = config

        self._recordings: List[Recording] = []
        self._suppress_autoplay = True
        self._worker: Optional[RefreshWorker] = None
        self._last_selected_path = None
        self._waveform_path = None  # last path the waveform was computed for - see _on_playback_started
        self._selected_recording: Optional[Recording] = None
        # Tracks a manual edit of the transcript field - see _flush_transcript_edit().
        # "target" is the Recording transcript_text_edit's contents currently
        # refer to; "baseline" is the text we last put there programmatically
        # (from a transcription result/cache), to tell "the user changed
        # something" apart from "still the same, nothing to save".
        self._transcript_edit_target: Optional[Recording] = None
        self._transcript_edit_baseline: str = ""

        # freq_khz -> name from the Memory List (Listen/Channels tab) - see
        # set_known_frequencies()/set_known_folders(), called from MainWindow.
        self._freq_names: dict[int, str] = {}
        self._known_folders: List[str] = []

        self._transcription_cache = TranscriptionCache()
        self._favorites_store = FavoritesStore()
        # Loaded once and kept in memory, unlike RefreshWorker's own
        # QualityCache() (which is created fresh inside its background
        # thread each full refresh - that one genuinely needs to re-read
        # from disk to see what a previous session/refresh already cached).
        # ingest_finished_recording() used to construct + immediately
        # save() a brand new QualityCache() on the GUI thread for *every
        # single* completed transmission - with a real ~2.4MB/7800-entry
        # cache file (grows with the whole recording archive, unbounded),
        # that's a full JSON load+parse+dump+write measured at ~55-60ms of
        # GUI-thread stall, on every transmission end, getting slower as the
        # archive grows - a direct, reproducible cause of stutter spikes
        # timed with scanner activity. See _flush_quality_cache() and
        # ingest_finished_recording() below.
        self._quality_cache = QualityCache()
        self._quality_cache_dirty = False
        self._quality_cache_save_timer = QTimer(self)
        self._quality_cache_save_timer.setSingleShot(True)
        self._quality_cache_save_timer.timeout.connect(self._flush_quality_cache)

        # Same idea as the quality cache above, for a different stall:
        # ingest_finished_recording() used to also call freq_panel.set_recordings()
        # + _rebuild_date_combo() + stats_tab.update_stats() synchronously for
        # *every* completed transmission - with a real ~10k-recording archive,
        # stats_tab.update_stats() alone (two pyqtgraph PlotWidgets rebuilt from
        # scratch, plus the frequency table) measured ~60-75ms of GUI-thread
        # stall every single time a channel finished, on top of the frequency
        # list/date combo rebuilds - a second, separate cause of stutter
        # spikes timed with scanner activity. Debounced the same way, except
        # capped (started only if not already pending, never pushed back) so
        # a continuously-busy scan can't defer it forever while someone's
        # actually looking at Statistics.
        self._stats_dirty = False
        self._stats_refresh_timer = QTimer(self)
        self._stats_refresh_timer.setSingleShot(True)
        self._stats_refresh_timer.timeout.connect(self._flush_stats_refresh)
        self._transcription_worker: Optional[TranscriptionWorker] = None
        self._force_transcribe_worker: Optional[TranscriptionWorker] = None

        self.player = AudioPlayer(self)

        self._build_ui()
        self._connect_signals()
        self.update_recording_notice()
        self.refresh_transcription_availability()

        # New recordings are added to the library on the fly via
        # ingest_finished_recording() (see MainWindow), so there's no more
        # need for a periodic full directory rescan - this one-time refresh
        # just loads whatever's already on disk (and sorts any stray loose
        # files left over from before this change).
        self.trigger_refresh()

    def _recordings_root(self) -> Path:
        return Path(self.config.recording.output_dir)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)

        outer.addWidget(self._build_toolbar())

        # Shown/hidden by _update_recording_notice() - purely informational,
        # so a user staring at an empty (or not-growing) library understands
        # why instead of wondering if something's broken. Auto-recording is
        # off by default (see RecordingConfig.enabled), so this is the
        # common first-run state, not an edge case.
        self.recording_disabled_label = QLabel()
        self.recording_disabled_label.setWordWrap(True)
        self.recording_disabled_label.setStyleSheet(
            "background: #3a2f12; color: #ffb454; padding: 6px 10px; border-radius: 4px;"
        )
        self.recording_disabled_label.setVisible(False)
        outer.addWidget(self.recording_disabled_label)

        # Shown/hidden by refresh_transcription_availability() - discoverable
        # from right here (not just Options -> Transcription), since this is
        # exactly where someone would notice recordings never get a
        # transcript and wonder why - see transcription_install.py.
        self.transcription_unavailable_label = QLabel()
        self.transcription_unavailable_label.setWordWrap(True)
        self.transcription_unavailable_label.setStyleSheet(
            "background: #3a2f12; color: #ffb454; padding: 6px 10px; border-radius: 4px;"
        )
        self.transcription_unavailable_label.setVisible(False)
        outer.addWidget(self.transcription_unavailable_label)

        outer.addWidget(self._build_browser_tab(), stretch=1)
        # Statistics lives as its own top-level tab (see MainWindow), but is
        # still built and updated here alongside the rest of the library
        # state - MainWindow just pulls this widget out and places it
        # elsewhere, no other wiring changes.
        self.stats_tab = StatsTab()

        outer.addWidget(self._build_transcript_panel())
        outer.addWidget(self._build_player_bar())

    def _build_toolbar(self) -> QWidget:
        bar = QFrame()
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)

        self.refresh_btn = QPushButton(tr("⟳ Refresh"))
        self.refresh_btn.setToolTip(tr("Sort new recordings and refresh the list (F5)"))
        layout.addWidget(self.refresh_btn)

        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedWidth(140)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        layout.addSpacing(12)
        self.utc_checkbox = QCheckBox(tr("UTC time"))
        self.utc_checkbox.setToolTip(tr("Toggle displayed time: local / UTC"))
        layout.addWidget(self.utc_checkbox)

        layout.addSpacing(12)
        layout.addWidget(QLabel(tr("Date:")))
        self.date_combo = QComboBox()
        self.date_combo.addItems([
            tr("All"), tr("Today"), tr("Last 24h"), tr("Last 7 days"), tr("Last 30 days"),
        ])
        self.date_combo.insertSeparator(5)
        layout.addWidget(self.date_combo)

        layout.addSpacing(12)
        layout.addWidget(QLabel(tr("Min. quality:")))
        self.quality_slider = QSlider(Qt.Orientation.Horizontal)
        self.quality_slider.setRange(0, 100)
        self.quality_slider.setFixedWidth(120)
        layout.addWidget(self.quality_slider)
        self.quality_value_label = QLabel("0")
        self.quality_value_label.setFixedWidth(24)
        layout.addWidget(self.quality_value_label)

        layout.addSpacing(12)
        self.transcription_checkbox = QCheckBox(tr("Auto-transcribe"))
        self.transcription_checkbox.setToolTip(tr(
            "Automatic background transcription of recordings (Whisper, "
            "pilot-ATC). The main switch is also in the Options tab; "
            "re-enabling it processes any backlog."
        ))
        layout.addWidget(self.transcription_checkbox)
        self.transcription_progress_label = QLabel("")
        self.transcription_progress_label.setMinimumWidth(100)
        layout.addWidget(self.transcription_progress_label)

        layout.addSpacing(12)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(tr("Search (frequency, date, file, transcript)… [Ctrl+F]"))
        layout.addWidget(self.search_edit, stretch=1)

        self.sort_newest_btn = QPushButton(tr("Newest"))
        self.sort_quality_btn = QPushButton(tr("Best quality"))
        layout.addWidget(self.sort_newest_btn)
        layout.addWidget(self.sort_quality_btn)

        return bar

    def _build_browser_tab(self) -> QWidget:
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(splitter)

        self.freq_panel = FrequencyPanel()
        self.freq_panel.setMinimumWidth(220)
        # Deliberately no setMaximumWidth: the user may want to stretch this
        # panel wider (e.g. for the language view's longer labels) -
        # QSplitter won't let it take more than the whole available width
        # anyway, so an upper limit here only ever restricted the user, never
        # did anything useful.
        splitter.addWidget(self.freq_panel)

        self.table_model = RecordingsTableModel()
        self.proxy = RecordingsFilterProxy()
        self.proxy.setSourceModel(self.table_model)

        self.table_view = RecordingsTableView()
        self.table_view.setModel(self.proxy)
        self.table_view.setSortingEnabled(True)
        self.table_view.sortByColumn(COL_DATE, Qt.SortOrder.DescendingOrder)
        self.table_view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table_view.setAlternatingRowColors(True)
        self.table_view.verticalHeader().setVisible(False)
        self.table_view.horizontalHeader().setStretchLastSection(True)
        self.table_view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        splitter.addWidget(self.table_view)
        splitter.setSizes([260, 1000])

        return widget

    def _build_transcript_panel(self) -> QWidget:
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(6, 4, 6, 4)

        header_row = QHBoxLayout()
        self.transcript_header_label = QLabel(tr("Transcript: select a recording from the list…"))
        header_row.addWidget(self.transcript_header_label, stretch=1)

        self.fr24_btn = QPushButton(" FlightRadar24")
        fr24_icon_path = RESOURCES_DIR / "fr24.svg"
        if fr24_icon_path.exists():
            self.fr24_btn.setIcon(QIcon(str(fr24_icon_path)))
        self.fr24_btn.setToolTip(tr(
            "Open FlightRadar24 playback in your browser, at this recording's "
            "time (UTC), around the configured receiver location"
        ))
        self.fr24_btn.setEnabled(False)
        header_row.addWidget(self.fr24_btn)

        self.adsbx_btn = QPushButton(" ADSBExchange")
        adsbx_icon_path = RESOURCES_DIR / "adsbexchange.png"
        if adsbx_icon_path.exists():
            self.adsbx_btn.setIcon(QIcon(str(adsbx_icon_path)))
        self.adsbx_btn.setToolTip(tr(
            "Open ADSBExchange playback in your browser, at this recording's "
            "time (UTC), around the configured receiver location"
        ))
        self.adsbx_btn.setEnabled(False)
        header_row.addWidget(self.adsbx_btn)

        # Plain text, no emoji: Qt/fontconfig on some systems doesn't render
        # most color emoji even with NotoColorEmoji installed (observed
        # for real, see this change's history) - it comes out as an empty
        # box instead of an icon, worse than no icon at all.
        self.save_freq_btn = QPushButton(tr("Save frequency"))
        self.save_freq_btn.setToolTip(tr(
            "Save this frequency to the Memory List (Listen → Channels), "
            "with a name and optional folder - if it's already saved, this "
            "overwrites its name/folder instead of creating a duplicate."
        ))
        self.save_freq_btn.setEnabled(False)
        header_row.addWidget(self.save_freq_btn)

        self.favorite_btn = QPushButton(tr("☆ Add to favorites"))
        self.favorite_btn.setToolTip(tr(
            "Mark this recording as a favorite (shows up under "
            "“★ Favorites” in the frequency panel on the left)"
        ))
        self.favorite_btn.setEnabled(False)
        header_row.addWidget(self.favorite_btn)

        header_row.addWidget(QLabel(tr("Language:")))
        self.transcribe_language_combo = QComboBox()
        self.transcribe_language_combo.setEditable(True)
        # Don't permanently insert manually-typed codes into the list - this
        # is a one-off, ad-hoc override for this one click, not something
        # that should clutter the dropdown with one-time entries.
        self.transcribe_language_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.transcribe_language_combo.setToolTip(tr(
            "Language used for the next “Re-transcribe” click on "
            "this recording. “Auto (detect)” is the normal behavior "
            "(and what the background auto-transcription queue always uses "
            "- this setting doesn't affect it). Useful when weak audio "
            "confuses language detection and Whisper picks an exotic "
            "language instead of the real one - you can also type any other "
            "ISO code (e.g. \"de\")."
        ))
        for label, code in ((tr("Auto (detect)"), None), (tr("Polish"), "pl"), ("English", "en")):
            self.transcribe_language_combo.addItem(label, code)
        header_row.addWidget(self.transcribe_language_combo)

        self.transcribe_now_btn = QPushButton(tr("Re-transcribe"))
        self.transcribe_now_btn.setToolTip(tr(
            "Force re-transcription of the selected recording (bypasses the "
            "cache), with the language selected on the left. Also overwrites "
            "any manually typed text below, if there is any."
        ))
        self.transcribe_now_btn.setEnabled(False)
        header_row.addWidget(self.transcribe_now_btn)
        layout.addLayout(header_row)

        self.transcript_text_edit = QPlainTextEdit()
        self.transcript_text_edit.setMaximumHeight(70)
        self.transcript_text_edit.setPlaceholderText(tr("(no transcript - you can also type text by hand)"))
        self.transcript_text_edit.setToolTip(tr(
            "You can edit this directly - a change is saved automatically "
            "when you move to another recording. “Re-transcribe” "
            "overwrites whatever's typed here with a fresh Whisper result."
        ))
        layout.addWidget(self.transcript_text_edit)

        return frame

    def _build_player_bar(self) -> QWidget:
        bar = QFrame()
        bar.setFrameShape(QFrame.Shape.StyledPanel)
        outer = QVBoxLayout(bar)

        # The waveform replaces a plain progress bar (see WaveformView) -
        # same role (click/drag = seek), plus it immediately shows where in
        # the recording there's silence (e.g. a gap trimmed by squelch - see
        # ScannerEngine._play_and_record_slot) versus real content. A
        # separate full-width row, because at the height needed for the bars
        # to show anything meaningful, squeezing it into the same row as the
        # 40px buttons would look cluttered.
        self.waveform_view = WaveformView()
        outer.addWidget(self.waveform_view)

        controls = QHBoxLayout()
        self.prev_btn = QPushButton("⏮")
        self.play_btn = QPushButton("▶")
        self.stop_btn = QPushButton("⏹")
        self.next_btn = QPushButton("⏭")
        for b in (self.prev_btn, self.play_btn, self.stop_btn, self.next_btn):
            b.setFixedWidth(40)
            controls.addWidget(b)

        self.now_playing_label = QLabel(tr("Select a recording from the list…"))
        self.now_playing_label.setMinimumWidth(280)
        controls.addWidget(self.now_playing_label, stretch=1)

        self.time_label = QLabel("0:00 / 0:00")
        self.time_label.setFixedWidth(90)
        controls.addWidget(self.time_label)

        controls.addWidget(QLabel("\U0001f50a"))
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(90)
        self.volume_slider.setFixedWidth(90)
        controls.addWidget(self.volume_slider)

        self.autoplay_checkbox = QCheckBox(tr("Auto-play next"))
        self.autoplay_checkbox.setChecked(True)
        self.autoplay_checkbox.setToolTip(tr(
            "Automatically play the next recording in the list when one "
            "finishes - for quickly reviewing traffic"
        ))
        controls.addWidget(self.autoplay_checkbox)
        outer.addLayout(controls)

        return bar

    # ------------------------------------------------------------- signals
    def _connect_signals(self) -> None:
        self.refresh_btn.clicked.connect(self.trigger_refresh)
        QShortcut(QKeySequence("F5"), self, activated=self.trigger_refresh)
        QShortcut(QKeySequence(Qt.Key.Key_Space), self, activated=self.player.toggle_pause)
        QShortcut(QKeySequence("Ctrl+F"), self, activated=self._focus_search)

        self.utc_checkbox.toggled.connect(self._on_utc_toggled)
        self.date_combo.currentIndexChanged.connect(self._apply_date_filter)
        self.quality_slider.valueChanged.connect(self._on_quality_slider_changed)
        self.search_edit.textChanged.connect(self.proxy.set_search_text)
        self.sort_newest_btn.clicked.connect(
            lambda: self.table_view.sortByColumn(COL_DATE, Qt.SortOrder.DescendingOrder)
        )
        self.sort_quality_btn.clicked.connect(
            lambda: self.table_view.sortByColumn(COL_QUALITY, Qt.SortOrder.DescendingOrder)
        )

        self.freq_panel.frequencySelected.connect(self._on_freq_panel_selection)
        self.freq_panel.languageSelected.connect(self._on_language_panel_selection)
        self.stats_tab.frequencySelected.connect(self._jump_to_frequency)

        self.transcription_checkbox.setChecked(self.config.transcription.enabled)
        self.transcription_checkbox.toggled.connect(self._on_transcription_toggled)
        self.transcribe_now_btn.clicked.connect(self._force_transcribe_selected)

        self.fr24_btn.clicked.connect(self._open_fr24_playback)
        self.adsbx_btn.clicked.connect(self._open_adsbexchange_playback)
        self.save_freq_btn.clicked.connect(self._save_frequency)
        self.favorite_btn.clicked.connect(self._toggle_favorite)

        self.table_view.clicked.connect(self._play_proxy_index)
        self.table_view.navigated.connect(lambda: self._play_proxy_index(self.table_view.currentIndex()))
        self.table_view.deleteRequested.connect(self._delete_selected)
        self.table_view.customContextMenuRequested.connect(self._show_table_context_menu)

        self.play_btn.clicked.connect(self.player.toggle_pause)
        self.stop_btn.clicked.connect(self.player.stop)
        self.prev_btn.clicked.connect(self._play_previous)
        self.next_btn.clicked.connect(self._play_next)
        self.volume_slider.valueChanged.connect(lambda v: self.player.set_volume(v / 100.0))

        self.player.started.connect(self._on_playback_started)
        self.player.stateChanged.connect(self._on_playback_state_changed)
        self.player.qt_player.positionChanged.connect(self._on_position_changed)
        self.player.qt_player.durationChanged.connect(self._on_duration_changed)
        self.player.qt_player.mediaStatusChanged.connect(self._on_media_status_changed)
        self.waveform_view.seekRequested.connect(self.player.seek)

    def _focus_search(self) -> None:
        self.search_edit.setFocus()
        self.search_edit.selectAll()

    # ------------------------------------------------------------ refreshing
    def trigger_refresh(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        # Make sure any pending ingest_finished_recording() writes hit disk
        # before RefreshWorker reads the cache file fresh from its own
        # thread - otherwise it wouldn't see whatever's still only in our
        # in-memory copy and would just redundantly re-analyze those files
        # (harmless, but no reason to).
        self._flush_quality_cache()
        # scan_recordings() rebuilds every Recording object from scratch -
        # any not-yet-flushed manual transcript edit would otherwise target
        # an object about to be discarded and never get saved.
        self._flush_transcript_edit()
        self.refresh_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)
        self.status_message.emit(tr("Sorting new recordings and analyzing quality…"), 0)

        self._worker = RefreshWorker(self._recordings_root(), self)
        self._worker.progress.connect(self._on_refresh_progress)
        self._worker.finished_ok.connect(self._on_refresh_finished)
        self._worker.failed.connect(self._on_refresh_failed)
        self._worker.start()

    def _on_refresh_progress(self, done: int, total: int) -> None:
        if total:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(done)

    def _flush_quality_cache(self) -> None:
        """Writes self._quality_cache to disk if anything changed since the
        last flush - debounced (see ingest_finished_recording) so a burst of
        several transmissions ending close together costs one write, not
        one per transmission."""
        self._quality_cache_save_timer.stop()
        if self._quality_cache_dirty:
            self._quality_cache.save()
            self._quality_cache_dirty = False

    def _schedule_stats_refresh(self) -> None:
        """Marks the frequency list/date combo/Statistics tab stale and
        arranges for _flush_stats_refresh() to run soon - see the comment on
        self._stats_refresh_timer in __init__. Deliberately doesn't restart
        an already-running timer: that would turn a busy scan into an
        indefinite delay instead of a bounded one."""
        self._stats_dirty = True
        if not self._stats_refresh_timer.isActive():
            self._stats_refresh_timer.start(1000)

    def _flush_stats_refresh(self) -> None:
        self._stats_refresh_timer.stop()
        if not self._stats_dirty:
            return
        self._stats_dirty = False
        self.freq_panel.set_recordings(self._recordings)
        self._rebuild_date_combo(self._recordings)
        self.stats_tab.update_stats(self._recordings)

    def _on_refresh_finished(self, recordings: List[Recording], moved_count: int) -> None:
        # RefreshWorker just wrote its own (possibly newer/pruned) copy of
        # the quality cache from its background thread - reload rather than
        # keep using ours, so a stale in-memory copy here can't clobber it
        # on the next debounced save. Anything of ours not yet flushed was
        # for files RefreshWorker just analyzed anyway (attach_quality()
        # only skips files already in *its* freshly-loaded copy), so this
        # never loses real data, only occasionally repeats a cheap
        # (~1-10ms) re-analysis.
        self._flush_quality_cache()
        self._quality_cache = QualityCache()
        self._hydrate_transcripts(recordings)
        self._recordings = recordings
        self._suppress_autoplay = True

        self.table_model.set_recordings(recordings)
        self._restore_selection()
        self.freq_panel.set_recordings(recordings)
        self._rebuild_date_combo(recordings)
        self.stats_tab.update_stats(recordings)

        self._suppress_autoplay = False
        self.refresh_btn.setEnabled(True)
        self.progress_bar.setVisible(False)

        now = datetime.now(POLAND_TZ).strftime("%H:%M:%S")
        msg = tr("Loaded {count} recordings").format(count=len(recordings))
        if moved_count:
            msg += " " + tr("({count} new, sorted)").format(count=moved_count)
        msg += f" — {now}"
        self.status_message.emit(msg, 5000)

        self._maybe_start_transcription()

    def _on_refresh_failed(self, message: str) -> None:
        self.refresh_btn.setEnabled(True)
        self.progress_bar.setVisible(False)
        self.status_message.emit(tr("Refresh failed"), 5000)
        QMessageBox.warning(self, tr("Refresh failed"), message)

    def _restore_selection(self) -> None:
        if self._last_selected_path is None:
            return
        for row in range(self.table_model.rowCount()):
            if self.table_model.recording_at(row).path == self._last_selected_path:
                src_index = self.table_model.index(row, 0)
                proxy_index = self.proxy.mapFromSource(src_index)
                if proxy_index.isValid():
                    self.table_view.selectRow(proxy_index.row())
                break

    def _rebuild_date_combo(self, recordings: List[Recording]) -> None:
        current_data = self.date_combo.currentData()
        current_text = self.date_combo.currentText()
        # remove previous specific dates (after the separator, index 5)
        while self.date_combo.count() > 6:
            self.date_combo.removeItem(6)
        for day_str, count in sorted(counts_per_day(recordings), reverse=True):
            self.date_combo.addItem(f"{day_str}  ({count})", day_str)
        idx = self.date_combo.findText(current_text)
        if idx == -1 and current_data:
            idx = self.date_combo.findData(current_data)
        if idx != -1:
            self.date_combo.setCurrentIndex(idx)

    def ingest_finished_recording(self, path_str: str) -> None:
        """Adds one just-finished recording to the library right away
        (called from MainWindow, hooked up to
        ScannerEngine.signals.recording_finished), instead of waiting for the
        next full directory rescan."""
        path = Path(path_str)
        parsed = parse_filename(path.name)
        if parsed is None:
            return

        rec = Recording(
            path=path,
            dt_local=parsed.dt_local,
            dt_utc=parsed.dt_local.astimezone(UTC_TZ),
            freq_khz=parsed.freq_khz,
        )
        rec.transcript = self._transcription_cache.get(rec.cache_key())
        rec.is_favorite = self._favorites_store.is_favorite(rec.cache_key())

        key = rec.cache_key()
        quality = self._quality_cache.get(key)
        if quality is None:
            quality = analyze_wav(path)
            self._quality_cache.put(key, quality)
            self._quality_cache_dirty = True
            # Debounced, not immediate: coalesces a burst of transmissions
            # ending close together into one disk write instead of one per
            # transmission - see the comment on self._quality_cache in
            # __init__ for why the old immediate save() here was a measured
            # ~55-60ms GUI-thread stall on every single recording.
            self._quality_cache_save_timer.start(5000)
        rec.quality = quality

        self._recordings.append(rec)
        self.table_model.append_recording(rec)
        self._schedule_stats_refresh()

        self._maybe_transcribe_new(rec)

    # ------------------------------------------------------------- filtering
    def _on_utc_toggled(self, checked: bool) -> None:
        self.table_model.set_use_utc(checked)
        self.stats_tab.set_use_utc(checked)
        self.stats_tab.update_stats(self._recordings)

    def _on_quality_slider_changed(self, value: int) -> None:
        self.quality_value_label.setText(str(value))
        self.proxy.set_min_quality(float(value))

    def _apply_date_filter(self) -> None:
        idx = self.date_combo.currentIndex()
        now = datetime.now(POLAND_TZ)
        today_start = datetime.combine(now.date(), time.min, tzinfo=POLAND_TZ)

        if idx == 0:  # All
            self.proxy.set_date_range(None, None)
        elif idx == 1:  # Today
            self.proxy.set_date_range(today_start, today_start + timedelta(days=1))
        elif idx == 2:  # Last 24h
            self.proxy.set_date_range(now - timedelta(hours=24), now)
        elif idx == 3:  # Last 7 days
            self.proxy.set_date_range(now - timedelta(days=7), now)
        elif idx == 4:  # Last 30 days
            self.proxy.set_date_range(now - timedelta(days=30), now)
        else:
            day_str = self.date_combo.currentData()
            if not day_str:
                self.proxy.set_date_range(None, None)
                return
            day = datetime.strptime(day_str, "%Y-%m-%d").date()
            start = datetime.combine(day, time.min, tzinfo=POLAND_TZ)
            self.proxy.set_date_range(start, start + timedelta(days=1))

    def _jump_to_frequency(self, freq_khz: int) -> None:
        """Called from StatsTab.frequencySelected (double-click on a row) -
        filters the browser to this frequency. MainWindow, separately hooked
        to the same signal, switches to the top-level Odtwarzacz tab so this
        is actually visible (Statistics is now its own tab)."""
        self.freq_panel.switch_to_frequency_mode()
        for row in range(self.freq_panel.list_widget.count()):
            item = self.freq_panel.list_widget.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == freq_khz:
                self.freq_panel.list_widget.setCurrentRow(row)
                break

    # ----------------------------------------------------------- transcription
    def _hydrate_transcripts(self, recordings: List[Recording]) -> None:
        """Restores transcription results and favorite status from the
        persistent cache onto a freshly-parsed recording list (scan_recordings
        creates new Recording objects on every refresh, so without this
        everything would be recomputed from scratch / favorite stars lost)."""
        for rec in recordings:
            rec.transcript = self._transcription_cache.get(rec.cache_key())
            rec.is_favorite = self._favorites_store.is_favorite(rec.cache_key())

    def _on_freq_panel_selection(self, value) -> None:
        # A selection in Frequency mode is independent of Language mode -
        # clear any language filter so the two never unexpectedly combine
        # (see freq_panel.py).
        self.proxy.set_language_filter(None)
        if value == FAVORITES_ITEM:
            self.proxy.set_frequency_filter(ALL_ITEM_FREQ)
            self.proxy.set_favorites_only(True)
        else:
            self.proxy.set_favorites_only(False)
            self.proxy.set_frequency_filter(value)

    def _on_language_panel_selection(self, value) -> None:
        self.proxy.set_favorites_only(False)
        self.proxy.set_frequency_filter(ALL_ITEM_FREQ)
        self.proxy.set_language_filter(value)

    def _toggle_favorite(self) -> None:
        rec = self._selected_recording
        if rec is None:
            return
        rec.is_favorite = not rec.is_favorite
        self._favorites_store.set_favorite(rec.cache_key(), rec.is_favorite)
        self._set_favorite_btn_state(rec.is_favorite)
        self.proxy.refresh()
        self.freq_panel.set_recordings(self._recordings)  # refresh the "★ Favorites (N)" count

    def _set_favorite_btn_state(self, is_favorite: bool) -> None:
        self.favorite_btn.setText(tr("★ In favorites") if is_favorite else tr("☆ Add to favorites"))

    def _maybe_start_transcription(self, include_errors: bool = True) -> None:
        """Full catch-up pass: (re)scans *every* recording currently in the
        library against the transcription cache to build its "needs
        transcribing" queue - see TranscriptionWorker.run(). Right for a
        rare, whole-archive event (startup/manual refresh, or catching up on
        whatever arrived while a previous batch was busy - see
        _on_transcription_batch_finished), but each such scan costs one
        stat() + cache lookup per recording *ever accumulated* - see
        _maybe_transcribe_new() for the single-new-recording fast path that
        avoids paying this on every transmission end."""
        if not self.config.transcription.enabled:
            self.transcription_progress_label.setText(tr("disabled"))
            return
        self._launch_transcription_worker(list(self._recordings), include_errors=include_errors)

    def _maybe_transcribe_new(self, rec: Recording) -> None:
        """Queues just this one freshly-finished recording (called from
        ingest_finished_recording()), instead of _maybe_start_transcription()'s
        whole-library rescan.

        That rescan's cost (one stat() + cache lookup per recording in
        self._recordings - see TranscriptionWorker.run()'s ``todo`` list
        comprehension) was being paid again in full on *every single*
        transmission end, the overwhelmingly common case here (worker idle,
        one new file to check) - scaling with the entire archive's size
        forever, worse the longer/more the program has already been used,
        purely to conclude "only the one new file needs it". If a worker is
        already busy, this new recording is simply left for
        _on_transcription_batch_finished's own catch-up rescan instead of
        being queued twice - by then at most a handful of arrivals piled up
        while busy, not a per-file cost.
        """
        if not self.config.transcription.enabled:
            return
        self._launch_transcription_worker([rec])

    def _launch_transcription_worker(self, recordings: List[Recording], include_errors: bool = True) -> None:
        if self._transcription_worker is not None and self._transcription_worker.isRunning():
            return
        if not is_transcription_installed():
            # Not an error - just not installed. Without this check every
            # recording would get queued, fail with a plain ImportError deep
            # inside transcribe_recording(), get cached as status "error",
            # and then be retried forever (see TranscriptionWorker.run()'s
            # own "error" isn't a durable result" comment) - wasted work on
            # every single batch for as long as it stays uninstalled.
            return
        self._transcription_worker = TranscriptionWorker(
            recordings, self.config.transcription, self._transcription_cache,
            include_errors=include_errors, parent=self,
        )
        self._transcription_worker.progress.connect(self._on_transcription_progress)
        self._transcription_worker.item_done.connect(self._on_transcription_item_done)
        self._transcription_worker.finished_batch.connect(self._on_transcription_batch_finished)
        self._transcription_worker.start()

    def set_transcription_enabled(self, enabled: bool) -> None:
        """Enables/disables the automatic queue and syncs both toggles.

        Stopping is cooperative: a Whisper call already in progress may run
        to completion, but no further files are picked up. Once re-enabled,
        ``_maybe_start_transcription`` builds a queue from every recording
        that still has no cache entry, so it catches up on the backlog.
        """
        self.config.transcription.enabled = enabled
        self.config.save()
        self.transcription_checkbox.blockSignals(True)
        self.transcription_checkbox.setChecked(enabled)
        self.transcription_checkbox.blockSignals(False)
        if enabled:
            self._maybe_start_transcription()
        else:
            self.transcription_progress_label.setText(tr("disabled"))
            if self._transcription_worker is not None and self._transcription_worker.isRunning():
                self._transcription_worker.request_stop()
        self.transcription_enabled_changed.emit(enabled)

    def _on_transcription_toggled(self, checked: bool) -> None:
        self.set_transcription_enabled(checked)

    def _on_transcription_progress(self, done: int, total: int) -> None:
        if total == 0:
            self.transcription_progress_label.setText(tr("done"))
        else:
            self.transcription_progress_label.setText(f"{done}/{total}")

    def _on_transcription_item_done(self, path_str: str, result: TranscriptionResult) -> None:
        path = Path(path_str)
        self.table_model.update_transcript(path, result)
        if self._selected_recording is not None and self._selected_recording.path == path:
            self._update_transcript_panel(self._selected_recording)

    def _on_transcription_batch_finished(self) -> None:
        if self.config.transcription.enabled:
            self.transcription_progress_label.setText(tr("done"))
        # Something may have arrived via ingest_finished_recording() while
        # this batch was running - check again on the next event loop pass
        # (once the thread has definitely finished), instead of waiting for
        # the next manual refresh. include_errors=False here matters: without
        # it, if anything in this batch ended in "error", that same file
        # would immediately qualify for another batch, which would
        # immediately, endlessly, with no pause between attempts (once
        # actually observed: an infinite loop hammering GPU/CPU for the same
        # files). Errors still get another chance at the next manual refresh
        # (F5) or new recording.
        QTimer.singleShot(0, lambda: self._maybe_start_transcription(include_errors=False))

    def _freq_display(self, freq_khz: int) -> str:
        """e.g. "Downtown Tower (128.950 MHz)" when the frequency is known
        from the Memory List, otherwise just "128.950 MHz"."""
        name = self._freq_names.get(freq_khz)
        freq_text = f"{freq_khz / 1000:.3f} MHz"
        return f"{name} ({freq_text})" if name else freq_text

    def _update_transcript_panel(self, rec: Recording) -> None:
        # Save any pending manual edit for the PREVIOUS recording before we
        # overwrite the field with the new one - but only when actually
        # switching targets (e.g. a re-transcription finishing for the still-
        # selected recording also goes through here, and there we
        # specifically want to overwrite the old text with the fresh result,
        # not "rescue" it as an edit - see the flush docstring).
        if self._transcript_edit_target is not None and self._transcript_edit_target is not rec:
            self._flush_transcript_edit()

        self._selected_recording = rec
        self.transcribe_now_btn.setEnabled(
            self._force_transcribe_worker is None or not self._force_transcribe_worker.isRunning()
        )
        self.fr24_btn.setEnabled(True)
        self.adsbx_btn.setEnabled(True)
        self.save_freq_btn.setEnabled(True)
        self.favorite_btn.setEnabled(True)
        self._set_favorite_btn_state(rec.is_favorite)
        freq_bit = self._freq_display(rec.freq_khz)
        status = rec.transcript_status
        if status == "pending":
            header = tr("Transcript:") + " " + (
                tr("waiting in the queue…") if self.config.transcription.enabled
                else tr("disabled (click “Re-transcribe” to try manually)")
            )
            self.transcript_text_edit.setPlainText("")
        elif status == "skipped_short":
            header = tr("Transcript: recording too short, skipped")
            self.transcript_text_edit.setPlainText("")
        elif status == "no_speech":
            header = tr("Transcript: no speech detected (just noise/squelch crackle)")
            self.transcript_text_edit.setPlainText("")
        elif status == "error":
            header = tr("Transcript: error (try again)")
            self.transcript_text_edit.setPlainText("")
        else:
            t = rec.transcript
            if t.engine == "manual":
                header = tr("Transcript (manually edited):")
            else:
                conf_note = " " + tr("— low confidence, take with a grain of salt") if status == "low_confidence" else ""
                header = f"{tr('Transcript')} [{t.language}, {t.language_probability * 100:.0f}%]{conf_note}:"
            self.transcript_text_edit.setPlainText(t.text)
        self.transcript_header_label.setText(f"[{freq_bit}] {header}")

        # New baseline: what we just inserted programmatically - see
        # _flush_transcript_edit().
        self._transcript_edit_target = rec
        self._transcript_edit_baseline = self.transcript_text_edit.toPlainText()

    def _flush_transcript_edit(self) -> None:
        """Saves a manual edit of the transcript field to the cache/Recording,
        if anything actually changed relative to what we put there
        programmatically. An empty field reverts to "no transcript" (once
        again eligible for the automatic queue); a non-empty one is saved as
        a "manual" result, visible right away in the table and cache like any
        other transcription."""
        rec = self._transcript_edit_target
        if rec is None:
            return
        text = self.transcript_text_edit.toPlainText()
        if text == self._transcript_edit_baseline:
            return
        key = rec.cache_key()
        if text.strip():
            prev = rec.transcript
            result = TranscriptionResult(
                text=text,
                language=prev.language if prev else "",
                language_probability=prev.language_probability if prev else 0.0,
                status="ok",
                engine="manual",
                transcribed_at=datetime.now(timezone.utc).isoformat(),
            )
            rec.transcript = result
            self._transcription_cache.put(key, result)
        else:
            rec.transcript = None
            self._transcription_cache.remove(key)
        self._transcription_cache.save()
        self.table_model.update_transcript(rec.path, rec.transcript)

    def _selected_transcribe_language(self) -> Optional[str]:
        """ISO code selected in transcribe_language_combo, or None for "Auto
        (detect)" - see its tooltip. Also handles a manually-typed code
        (the combo is editable), not just the three built-in entries."""
        idx = self.transcribe_language_combo.currentIndex()
        text = self.transcribe_language_combo.currentText().strip()
        if idx >= 0 and self.transcribe_language_combo.itemText(idx) == text:
            return self.transcribe_language_combo.itemData(idx)
        return text.lower() or None

    def _force_transcribe_selected(self) -> None:
        rec = self._selected_recording
        if rec is None or (self._force_transcribe_worker is not None and self._force_transcribe_worker.isRunning()):
            return
        self.transcribe_now_btn.setEnabled(False)
        self.transcript_header_label.setText(tr("Transcript: re-processing…"))
        worker = TranscriptionWorker(
            [rec], self.config.transcription, self._transcription_cache, ignore_cache=True,
            forced_language=self._selected_transcribe_language(), parent=self,
        )
        worker.item_done.connect(self._on_transcription_item_done)
        worker.finished_batch.connect(lambda: self.transcribe_now_btn.setEnabled(True))
        self._force_transcribe_worker = worker
        worker.start()

    # ------------------------------------------------------------- deletion
    def _delete_selected(self) -> None:
        rows = sorted({idx.row() for idx in self.table_view.selectionModel().selectedRows()})
        if not rows:
            return
        recs = []
        for row in rows:
            source_index = self.proxy.mapToSource(self.proxy.index(row, 0))
            recs.append(self.table_model.recording_at(source_index.row()))

        if len(recs) == 1:
            rec = recs[0]
            prompt = tr("Permanently delete this file?") + (
                f"\n\n{rec.path.name}\n{rec.freq_label} — "
                f"{rec.dt_local.strftime('%Y-%m-%d %H:%M:%S')}"
            )
        else:
            shown = "\n".join(r.path.name for r in recs[:10])
            more = "\n" + tr("… and {count} more").format(count=len(recs) - 10) if len(recs) > 10 else ""
            prompt = tr("Permanently delete {count} files?").format(count=len(recs)) + f"\n\n{shown}{more}"

        reply = QMessageBox.question(
            self,
            tr("Delete recordings") if len(recs) > 1 else tr("Delete recording"),
            prompt,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        playing_path = self.player.qt_player.source().toLocalFile()
        # self._quality_cache (not a fresh QualityCache()): the recording
        # being deleted might be the very one ingest_finished_recording()
        # just added, whose disk write is still pending in the debounced
        # _quality_cache_save_timer (see __init__) - a freshly disk-loaded
        # cache wouldn't know about it yet, so remove() on it would be a
        # silent no-op, and the timer's later save would then resurrect an
        # entry for a file that no longer exists. Operating on this same
        # in-memory instance avoids that.
        deleted_paths = []
        failures = []
        for rec in recs:
            if str(rec.path) == playing_path:
                self.player.stop()
                playing_path = ""
            cache_key = rec.cache_key()
            try:
                rec.path.unlink()
            except OSError as exc:
                failures.append(f"{rec.path.name}: {exc}")
                continue
            self._quality_cache.remove(cache_key)
            self._quality_cache_dirty = True
            self._transcription_cache.remove(cache_key)
            self._favorites_store.remove(cache_key)
            self.table_model.remove_recording(rec.path)
            deleted_paths.append(rec.path)
        self._flush_quality_cache()
        self._transcription_cache.save()

        if deleted_paths:
            deleted_set = set(deleted_paths)
            self._recordings = [r for r in self._recordings if r.path not in deleted_set]
            self.freq_panel.set_recordings(self._recordings)
            self._rebuild_date_combo(self._recordings)
            self.stats_tab.update_stats(self._recordings)

            if self._last_selected_path in deleted_set:
                self._last_selected_path = None
            if self._selected_recording is not None and self._selected_recording.path in deleted_set:
                self._selected_recording = None
                self._transcript_edit_target = None  # file deleted - nothing to save here
                self.transcript_header_label.setText(tr("Transcript: select a recording from the list…"))
                self.transcript_text_edit.setPlainText("")
                self.transcribe_now_btn.setEnabled(False)
                self.fr24_btn.setEnabled(False)
                self.adsbx_btn.setEnabled(False)
                self.save_freq_btn.setEnabled(False)
                self.favorite_btn.setEnabled(False)

            if len(deleted_paths) == 1:
                self.status_message.emit(tr("Deleted: {name}").format(name=deleted_paths[0].name), 4000)
            else:
                self.status_message.emit(tr("Deleted {count} file(s)").format(count=len(deleted_paths)), 4000)

        if failures:
            QMessageBox.warning(self, tr("Delete failed"), tr("Failed to delete:") + "\n" + "\n".join(failures))

    # ---------------------------------------------------- external links
    def _open_fr24_playback(self) -> None:
        self._open_fr24_for(self._selected_recording)

    def _location_ready(self, rec: Optional[Recording]) -> bool:
        """Common guard for both flight-tracker buttons: they need a
        configured receiver location to build a meaningful link (see
        LocationConfig - deliberately no baked-in default location shipped
        with the app, see its docstring). Redirects to Options instead of
        silently opening a link built from nothing (e.g. 0,0)."""
        if rec is None:
            return False
        loc = self.config.location
        if loc.home_lat is None or loc.home_lon is None:
            QMessageBox.information(
                self,
                tr("Location not set"),
                tr(
                    "Set your receiver's approximate location in Options first "
                    "- FlightRadar24/ADSBExchange need it to know where on the "
                    "map to jump to."
                ),
            )
            self.location_setup_requested.emit()
            return False
        return True

    def _open_fr24_for(self, rec: Optional[Recording]) -> None:
        if not self._location_ready(rec):
            return
        dt = rec.dt_utc
        lat = self.config.location.home_lat
        lon = self.config.location.home_lon
        url = f"https://www.flightradar24.com/{dt:%Y-%m-%d}/{dt:%H:%M}/20x/{lat},{lon}/9"
        QDesktopServices.openUrl(QUrl(url))

    def _open_adsbexchange_playback(self) -> None:
        self._open_adsbexchange_for(self._selected_recording)

    def _open_adsbexchange_for(self, rec: Optional[Recording]) -> None:
        if not self._location_ready(rec):
            return
        dt = rec.dt_utc
        lat = self.config.location.home_lat
        lon = self.config.location.home_lon
        url = f"https://globe.adsbexchange.com/?replay={dt:%Y-%m-%d-%H:%M}&lat={lat}&lon={lon}&zoom=8.8"
        QDesktopServices.openUrl(QUrl(url))

    def _save_frequency(self) -> None:
        self._save_frequency_for(self._selected_recording)

    def _save_frequency_for(self, rec: Optional[Recording]) -> None:
        if rec is None:
            return
        current_name = self._freq_names.get(rec.freq_khz, "")
        dlg = _SaveFrequencyDialog(rec.freq_khz, current_name, self._known_folders, parent=self)
        if dlg.exec():
            name, folder = dlg.values()
            channel = Channel(
                freq_hz=rec.freq_khz * 1000, name=name, modulation=Modulation.AM, enabled=True, folder=folder,
            )
            self.channel_saved.emit(channel)

    # ------------------------------------------------------- context menu
    def _show_table_context_menu(self, pos: QPoint) -> None:
        index = self.table_view.indexAt(pos)
        if not index.isValid():
            return
        selection_model = self.table_view.selectionModel()
        if not selection_model.isRowSelected(index.row(), QModelIndex()):
            # Right-click outside the current selection: select just this
            # row, like typical file browsers do, instead of acting on some
            # unrelated previous selection.
            self.table_view.selectRow(index.row())

        rows = sorted({idx.row() for idx in self.table_view.selectionModel().selectedRows()})
        recs = [
            self.table_model.recording_at(self.proxy.mapToSource(self.proxy.index(row, 0)).row())
            for row in rows
        ]
        if not recs:
            return

        # Only ▶/☆/★ as prefixes - the only symbols confirmed to
        # render correctly on every tested system (see this change's
        # history); the rest of the "colorful" emoji came out as empty boxes
        # even with NotoColorEmoji installed - Qt doesn't render most of its
        # glyphs. Plain text without an icon looks better than a broken box.
        menu = QMenu(self)
        if len(recs) == 1:
            rec = recs[0]
            menu.addAction(tr("▶ Play"), lambda: self._play_proxy_index(index))
            menu.addAction(tr("Show in file manager"), lambda: self._reveal_in_file_manager(rec.path))
            menu.addAction(tr("Copy file path"), lambda: self._copy_paths_to_clipboard([rec]))
            menu.addSeparator()
            fav_label = tr("★ Remove from favorites") if rec.is_favorite else tr("☆ Add to favorites")
            menu.addAction(fav_label, lambda: self._set_favorites([rec], not rec.is_favorite))
            menu.addAction(tr("Save frequency…"), lambda: self._save_frequency_for(rec))
            menu.addAction(tr("Re-transcribe"), lambda: self._force_transcribe_for(rec))
            menu.addSeparator()
            menu.addAction("FlightRadar24", lambda: self._open_fr24_for(rec))
            menu.addAction("ADSBExchange", lambda: self._open_adsbexchange_for(rec))
            menu.addSeparator()
            menu.addAction(tr("Delete"), self._delete_selected)
        else:
            n = len(recs)
            menu.addAction(tr("Show in file manager"), lambda: self._reveal_in_file_manager(recs[0].path))
            menu.addAction(tr("Copy file paths ({count})").format(count=n), lambda: self._copy_paths_to_clipboard(recs))
            menu.addSeparator()
            menu.addAction(tr("☆ Add to favorites"), lambda: self._set_favorites(recs, True))
            menu.addAction(tr("★ Remove from favorites"), lambda: self._set_favorites(recs, False))
            menu.addSeparator()
            menu.addAction(tr("Delete selected ({count})").format(count=n), self._delete_selected)

        menu.exec(self.table_view.viewport().mapToGlobal(pos))

    def _reveal_in_file_manager(self, path: Path) -> None:
        """Opens the file manager with this SPECIFIC file already selected,
        via the standard freedesktop org.freedesktop.FileManager1.ShowItems
        service (D-Bus) - supported by Nautilus (GNOME/Ubuntu's default),
        Nemo, Dolphin and most other Linux file managers. Qt itself has no
        portable "select this file" call - hence D-Bus, not
        QDesktopServices. If the service isn't available (a different
        environment without it, dbus-send missing) - falls back to just
        opening the containing folder (old behavior, still works, just
        without the selection)."""
        uri = QUrl.fromLocalFile(str(path)).toString()
        try:
            result = subprocess.run(
                [
                    "dbus-send", "--session", "--type=method_call",
                    "--dest=org.freedesktop.FileManager1",
                    "/org/freedesktop/FileManager1",
                    "org.freedesktop.FileManager1.ShowItems",
                    f"array:string:{uri}", "string:",
                ],
                capture_output=True, timeout=3,
            )
            if result.returncode == 0:
                return
        except (OSError, subprocess.TimeoutExpired):
            pass
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))

    def _copy_paths_to_clipboard(self, recs: List[Recording]) -> None:
        text = "\n".join(str(rec.path) for rec in recs)
        QGuiApplication.clipboard().setText(text)
        self.status_message.emit(
            tr("Copied file path") if len(recs) == 1 else tr("Copied {count} file paths").format(count=len(recs)),
            3000,
        )

    def _set_favorites(self, recs: List[Recording], is_favorite: bool) -> None:
        for rec in recs:
            rec.is_favorite = is_favorite
            self._favorites_store.set_favorite(rec.cache_key(), is_favorite)
        if self._selected_recording is not None and self._selected_recording in recs:
            self._set_favorite_btn_state(self._selected_recording.is_favorite)
        self.proxy.refresh()
        self.freq_panel.set_recordings(self._recordings)  # refresh the "★ Favorites (N)" count

    def _force_transcribe_for(self, rec: Optional[Recording]) -> None:
        if rec is None or (self._force_transcribe_worker is not None and self._force_transcribe_worker.isRunning()):
            return
        if rec is self._selected_recording:
            self._force_transcribe_selected()
            return
        worker = TranscriptionWorker(
            [rec], self.config.transcription, self._transcription_cache, ignore_cache=True,
            forced_language=self._selected_transcribe_language(), parent=self,
        )
        worker.item_done.connect(self._on_transcription_item_done)
        self._force_transcribe_worker = worker
        worker.start()

    # --------------------------------------------------------------- playback
    def _play_proxy_index(self, proxy_index: QModelIndex) -> None:
        if self._suppress_autoplay or not proxy_index.isValid():
            return
        source_index = self.proxy.mapToSource(proxy_index)
        rec = self.table_model.recording_at(source_index.row())
        self._last_selected_path = rec.path
        self.player.play_file(rec.path)
        self._update_transcript_panel(rec)

    def _step_row(self, delta: int) -> None:
        current = self.table_view.currentIndex()
        if not current.isValid():
            if self.proxy.rowCount() > 0:
                self.table_view.selectRow(0)
            return
        new_row = current.row() + delta
        if 0 <= new_row < self.proxy.rowCount():
            self.table_view.selectRow(new_row)
            self._play_proxy_index(self.proxy.index(new_row, 0))

    def _play_next(self) -> None:
        self._step_row(1)

    def _play_previous(self) -> None:
        self._step_row(-1)

    def _on_playback_started(self, path) -> None:
        parsed = parse_filename(path.name)
        if parsed is not None:
            self.now_playing_label.setText(f"{path.name}  —  {self._freq_display(parsed.freq_khz)}")
        else:
            self.now_playing_label.setText(path.name)
        if path != self._waveform_path:
            # Guarded on the path actually changing - set_known_frequencies()
            # also calls this (just to refresh the label text after a
            # channel-name edit, same file still playing), which would
            # otherwise redo this pointlessly. Synchronous, not a background
            # worker (unlike the library-wide quality scan) - these files
            # are individual recordings, typically seconds to tens of
            # seconds, so reading+reducing one is a handful of vectorized
            # numpy calls, fast enough not to need one - see
            # compute_waveform_peaks()'s own docstring.
            self._waveform_path = path
            self.waveform_view.set_waveform(compute_waveform_peaks(path))

    def _on_playback_state_changed(self, state) -> None:
        self.play_btn.setText("⏸" if state == QMediaPlayer.PlaybackState.PlayingState else "▶")

    def _on_position_changed(self, position_ms: int) -> None:
        if not self.waveform_view.isSliderDown():
            self.waveform_view.set_position(position_ms)
        self.time_label.setText(f"{_format_time(position_ms)} / {_format_time(self.waveform_view.duration())}")

    def _on_duration_changed(self, duration_ms: int) -> None:
        self.waveform_view.set_duration(duration_ms)

    def _on_media_status_changed(self, status) -> None:
        if status == QMediaPlayer.MediaStatus.EndOfMedia and self.autoplay_checkbox.isChecked():
            self._play_next()

    # -- called from MainWindow ------------------------------------------
    def set_known_frequencies(self, channels) -> None:
        """Updates the known-frequency names (from the Memory List) used
        everywhere this tab shows a frequency - the sidebar, the table, the
        transcript header, the "now playing" label."""
        self._freq_names = {round(c.freq_hz / 1000): c.name for c in channels if c.name}
        self.table_model.set_freq_names(self._freq_names)
        self.freq_panel.set_freq_names(self._freq_names)
        self.stats_tab.set_freq_names(self._freq_names)
        if self._selected_recording is not None:
            self._update_transcript_panel(self._selected_recording)
        current_path = self.player.qt_player.source().toLocalFile()
        if current_path:
            self._on_playback_started(Path(current_path))

    def set_known_folders(self, folders: List[str]) -> None:
        self._known_folders = list(folders)

    def update_recording_notice(self) -> None:
        """Shows/hides the "auto-recording is off" banner - called once at
        construction and again by MainWindow whenever config.recording.enabled
        changes (from either the Listen tab's or the Options tab's copy of
        that same toggle - see MainWindow._on_scanner_auto_recording_toggled/
        _on_options_auto_recording_toggled)."""
        if self.config.recording.enabled:
            self.recording_disabled_label.setVisible(False)
            return
        self.recording_disabled_label.setText(tr(
            "Auto-recording is currently off - the scanner is listening but "
            "not saving anything to disk. Turn it on from the Listen tab or "
            "Options to start building this library."
        ))
        self.recording_disabled_label.setVisible(True)

    def refresh_transcription_availability(self) -> None:
        """Shows/hides both the banner and the toolbar's Auto-transcribe
        checkbox/progress label based on whether faster-whisper is actually
        installed right now - called at construction, and again by
        MainWindow after TranscriptionPanel's installer finishes without
        needing a restart (the common case - see
        TranscriptionInstallWorker's docstring)."""
        installed = is_transcription_installed()
        self.transcription_checkbox.setVisible(installed)
        self.transcription_progress_label.setVisible(installed)
        self.transcribe_now_btn.setVisible(installed)
        self.transcribe_language_combo.setVisible(installed)
        if installed:
            self.transcription_unavailable_label.setVisible(False)
            return
        self.transcription_unavailable_label.setText(tr(
            "Transcription isn't installed - it's an optional, separate "
            "download. Install it from the Options tab to get automatic "
            "speech-to-text for recordings."
        ))
        self.transcription_unavailable_label.setVisible(True)

    def shutdown(self) -> None:
        """Waits for background workers - called from the main window's closeEvent."""
        self._flush_quality_cache()
        self._flush_transcript_edit()
        if self._worker is not None and self._worker.isRunning():
            self._worker.wait(2000)
        for worker in (self._transcription_worker, self._force_transcribe_worker):
            if worker is not None and worker.isRunning():
                worker.request_stop()
                worker.wait(5000)
