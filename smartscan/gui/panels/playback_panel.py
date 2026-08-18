"""Zakładka Odtwarzacz: przeglądarka nagrań, statystyki i pasek odtwarzania,
przeniesione z osobnego programu ATC Scanner Browser. Działa równolegle do
zakładki Nasłuch - silnik skanera i nagrywanie żyją w MainWindow niezależnie
od tego, która zakładka jest aktualnie widoczna."""
from __future__ import annotations

from datetime import datetime, time, timedelta
from pathlib import Path
from typing import List, Optional

from PyQt6.QtCore import QModelIndex, QTimer, QUrl, Qt, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QIcon, QKeySequence, QShortcut
from PyQt6.QtMultimedia import QMediaPlayer
from PyQt6.QtWidgets import (
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
from ...models import Channel, Modulation
from ...playback.audio_analysis import analyze_wav
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
from ...playback.transcription_cache import TranscriptionCache
from ...playback.transcription_worker import TranscriptionWorker
from ...playback.worker import RefreshWorker
from ..widgets import ClickToSeekSlider

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
    """QTableView, który sygnalizuje nawigację klawiaturą (do auto-odtwarzania) i Delete (do usuwania)."""

    navigated = pyqtSignal()
    deleteRequested = pyqtSignal()

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
    """Zapisuje biezaco wybrana czestotliwosc w Liscie pamieci (zakladka
    Nasluch → Kanaly): nazwa + opcjonalny folder. Jesli czestotliwosc jest
    juz znana, MainWindow nadpisuje jej wpis (patrz PlaybackPanel.channel_saved)
    zamiast tworzyc duplikat."""

    def __init__(self, freq_khz: int, current_name: str, folders: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Zapisz częstotliwość")
        form = QFormLayout(self)
        form.addRow("Częstotliwość:", QLabel(f"{freq_khz / 1000:.3f} MHz"))

        self.name_edit = QLineEdit(current_name)
        self.name_edit.setPlaceholderText("np. Rzeszów Wieża")
        form.addRow("Nazwa:", self.name_edit)

        self.folder_combo = QComboBox()
        self.folder_combo.setEditable(True)
        self.folder_combo.setToolTip(
            "Folder w Liscie pamieci. Wybierz istniejacy albo wpisz nowa nazwe,\n"
            "zeby go od razu utworzyc."
        )
        self.folder_combo.addItem("(brak folderu)", "")
        for f in folders:
            self.folder_combo.addItem(f, f)
        form.addRow("Folder:", self.folder_combo)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self) -> tuple[str, str]:
        folder = self.folder_combo.currentText().strip()
        if folder == "(brak folderu)":
            folder = ""
        return self.name_edit.text().strip(), folder


class PlaybackPanel(QWidget):
    """Zakładka 'Odtwarzacz': przeglądarka nagrań + statystyki + pasek odtwarzania.

    Katalog nagrań jest odczytywany za każdym odświeżeniem z ``AppConfig.recording.output_dir``,
    czyli tego samego ustawienia, którego używa zakładka Nasłuch do zapisu - nie trzeba więc
    synchronizować dwóch osobnych konfiguracji jak w dawnych dwóch programach.
    """

    status_message = pyqtSignal(str, int)  # (tekst, czas wyświetlania w ms; 0 = bez limitu)
    channel_saved = pyqtSignal(object)  # Channel - "Zapisz częstotliwość"; MainWindow scala z Listą pamięci

    def __init__(self, config: AppConfig, parent=None):
        super().__init__(parent)
        self.config = config

        self._recordings: List[Recording] = []
        self._suppress_autoplay = True
        self._worker: Optional[RefreshWorker] = None
        self._last_selected_path = None
        self._selected_recording: Optional[Recording] = None

        # freq_khz -> nazwa z Listy pamieci (zakladka Nasluch/Kanaly) - patrz
        # set_known_frequencies()/set_known_folders(), wolane z MainWindow.
        self._freq_names: dict[int, str] = {}
        self._known_folders: List[str] = []

        self._transcription_cache = TranscriptionCache()
        self._favorites_store = FavoritesStore()
        self._transcription_worker: Optional[TranscriptionWorker] = None
        self._force_transcribe_worker: Optional[TranscriptionWorker] = None

        self.player = AudioPlayer(self)

        self._build_ui()
        self._connect_signals()

        # Nowe nagrania trafiają do biblioteki na bieżąco przez
        # ingest_finished_recording() (patrz MainWindow), więc nie ma już
        # potrzeby okresowego pełnego przeskanowania katalogu - to
        # jednorazowe odświeżenie tylko wczytuje to, co już jest na dysku
        # (i posegregowuje ewentualne luźne pliki sprzed tej zmiany).
        self.trigger_refresh()

    def _recordings_root(self) -> Path:
        return Path(self.config.recording.output_dir)

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)

        outer.addWidget(self._build_toolbar())

        outer.addWidget(self._build_browser_tab(), stretch=1)
        # Statystyki zyje jako wlasna gorna zakladka (patrz MainWindow), ale
        # nadal jest zbudowana i aktualizowana tutaj razem z reszta stanu
        # biblioteki - MainWindow tylko wyciaga ten widget i umieszcza go
        # gdzie indziej, zadne inne okablowanie sie nie zmienia.
        self.stats_tab = StatsTab()

        outer.addWidget(self._build_transcript_panel())
        outer.addWidget(self._build_player_bar())

    def _build_toolbar(self) -> QWidget:
        bar = QFrame()
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)

        self.refresh_btn = QPushButton("⟳ Odśwież")
        self.refresh_btn.setToolTip("Posegreguj nowe nagrania i odśwież listę (F5)")
        layout.addWidget(self.refresh_btn)

        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedWidth(140)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        layout.addSpacing(12)
        self.utc_checkbox = QCheckBox("Czas UTC")
        self.utc_checkbox.setToolTip("Przełącz wyświetlanie czasu: polski (lokalny) / UTC")
        layout.addWidget(self.utc_checkbox)

        layout.addSpacing(12)
        layout.addWidget(QLabel("Data:"))
        self.date_combo = QComboBox()
        self.date_combo.addItems(["Wszystkie", "Dzisiaj", "Ostatnie 24h", "Ostatnie 7 dni", "Ostatnie 30 dni"])
        self.date_combo.insertSeparator(5)
        layout.addWidget(self.date_combo)

        layout.addSpacing(12)
        layout.addWidget(QLabel("Min. jakość:"))
        self.quality_slider = QSlider(Qt.Orientation.Horizontal)
        self.quality_slider.setRange(0, 100)
        self.quality_slider.setFixedWidth(120)
        layout.addWidget(self.quality_slider)
        self.quality_value_label = QLabel("0")
        self.quality_value_label.setFixedWidth(24)
        layout.addWidget(self.quality_value_label)

        layout.addSpacing(12)
        self.transcription_checkbox = QCheckBox("Transkrypcja auto")
        self.transcription_checkbox.setToolTip(
            "Automatyczna transkrypcja nowych nagrań w tle (Whisper, pilot-ATC). "
            "Wynik jest trwale zapisywany w cache - nie liczy się ponownie po restarcie programu."
        )
        layout.addWidget(self.transcription_checkbox)
        self.transcription_progress_label = QLabel("")
        self.transcription_progress_label.setMinimumWidth(100)
        layout.addWidget(self.transcription_progress_label)

        layout.addSpacing(12)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Szukaj (częstotliwość, data, plik, transkrypcja)… [Ctrl+F]")
        layout.addWidget(self.search_edit, stretch=1)

        self.sort_newest_btn = QPushButton("Najnowsze")
        self.sort_quality_btn = QPushButton("Najlepsza jakość")
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
        self.freq_panel.setMaximumWidth(320)
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
        splitter.addWidget(self.table_view)
        splitter.setSizes([260, 1000])

        return widget

    def _build_transcript_panel(self) -> QWidget:
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(6, 4, 6, 4)

        header_row = QHBoxLayout()
        self.transcript_header_label = QLabel("Transkrypcja: wybierz nagranie z listy…")
        header_row.addWidget(self.transcript_header_label, stretch=1)

        self.fr24_btn = QPushButton(" FlightRadar24")
        fr24_icon_path = RESOURCES_DIR / "fr24.svg"
        if fr24_icon_path.exists():
            self.fr24_btn.setIcon(QIcon(str(fr24_icon_path)))
        self.fr24_btn.setToolTip(
            "Otwórz playback FlightRadar24 w przeglądarce, o godzinie tego nagrania (UTC), w okolicy domu"
        )
        self.fr24_btn.setEnabled(False)
        header_row.addWidget(self.fr24_btn)

        self.adsbx_btn = QPushButton(" ADSBExchange")
        adsbx_icon_path = RESOURCES_DIR / "adsbexchange.png"
        if adsbx_icon_path.exists():
            self.adsbx_btn.setIcon(QIcon(str(adsbx_icon_path)))
        self.adsbx_btn.setToolTip(
            "Otwórz playback ADSBExchange w przeglądarce, o godzinie tego nagrania (UTC), w okolicy domu"
        )
        self.adsbx_btn.setEnabled(False)
        header_row.addWidget(self.adsbx_btn)

        self.save_freq_btn = QPushButton("💾 Zapisz częstotliwość")
        self.save_freq_btn.setToolTip(
            "Zapisz tę częstotliwość w Liście pamięci (zakładka Nasłuch → Kanały),\n"
            "z nazwą i opcjonalnym folderem - jeśli jest już tam zapisana, nadpisuje\n"
            "jej nazwę/folder zamiast tworzyć duplikat."
        )
        self.save_freq_btn.setEnabled(False)
        header_row.addWidget(self.save_freq_btn)

        self.favorite_btn = QPushButton("☆ Dodaj do ulubionych")
        self.favorite_btn.setToolTip("Oznacz to nagranie jako ulubione (widoczne potem pod „★ Ulubione” w panelu częstotliwości po lewej)")
        self.favorite_btn.setEnabled(False)
        header_row.addWidget(self.favorite_btn)

        self.transcribe_now_btn = QPushButton("Transkrybuj ponownie")
        self.transcribe_now_btn.setToolTip("Wymuś ponowną transkrypcję zaznaczonego nagrania (pomija cache)")
        self.transcribe_now_btn.setEnabled(False)
        header_row.addWidget(self.transcribe_now_btn)
        layout.addLayout(header_row)

        self.transcript_text_edit = QPlainTextEdit()
        self.transcript_text_edit.setReadOnly(True)
        self.transcript_text_edit.setMaximumHeight(70)
        self.transcript_text_edit.setPlaceholderText("(brak transkrypcji)")
        layout.addWidget(self.transcript_text_edit)

        return frame

    def _build_player_bar(self) -> QWidget:
        bar = QFrame()
        bar.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QHBoxLayout(bar)

        self.prev_btn = QPushButton("⏮")
        self.play_btn = QPushButton("▶")
        self.stop_btn = QPushButton("⏹")
        self.next_btn = QPushButton("⏭")
        for b in (self.prev_btn, self.play_btn, self.stop_btn, self.next_btn):
            b.setFixedWidth(40)
            layout.addWidget(b)

        self.now_playing_label = QLabel("Wybierz nagranie z listy…")
        self.now_playing_label.setMinimumWidth(280)
        layout.addWidget(self.now_playing_label)

        self.position_slider = ClickToSeekSlider(Qt.Orientation.Horizontal)
        self.position_slider.setRange(0, 0)
        layout.addWidget(self.position_slider, stretch=1)

        self.time_label = QLabel("0:00 / 0:00")
        self.time_label.setFixedWidth(90)
        layout.addWidget(self.time_label)

        layout.addWidget(QLabel("🔊"))
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(90)
        self.volume_slider.setFixedWidth(90)
        layout.addWidget(self.volume_slider)

        self.autoplay_checkbox = QCheckBox("Auto-odtwarzanie kolejnych")
        self.autoplay_checkbox.setChecked(True)
        self.autoplay_checkbox.setToolTip(
            "Po zakończeniu nagrania automatycznie odtwórz następne z listy - do szybkiego przeglądu ruchu"
        )
        layout.addWidget(self.autoplay_checkbox)

        return bar

    # ------------------------------------------------------------- sygnały
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
        self.position_slider.sliderMoved.connect(self.player.seek)

    def _focus_search(self) -> None:
        self.search_edit.setFocus()
        self.search_edit.selectAll()

    # ------------------------------------------------------------ odświeżanie
    def trigger_refresh(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        self.refresh_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)
        self.status_message.emit("Segregowanie nowych nagrań i analiza jakości…", 0)

        self._worker = RefreshWorker(self._recordings_root(), self)
        self._worker.progress.connect(self._on_refresh_progress)
        self._worker.finished_ok.connect(self._on_refresh_finished)
        self._worker.failed.connect(self._on_refresh_failed)
        self._worker.start()

    def _on_refresh_progress(self, done: int, total: int) -> None:
        if total:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(done)

    def _on_refresh_finished(self, recordings: List[Recording], moved_count: int) -> None:
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
        msg = f"Załadowano {len(recordings)} nagrań"
        if moved_count:
            msg += f" (posegregowano {moved_count} nowych)"
        msg += f" — {now}"
        self.status_message.emit(msg, 5000)

        self._maybe_start_transcription()

    def _on_refresh_failed(self, message: str) -> None:
        self.refresh_btn.setEnabled(True)
        self.progress_bar.setVisible(False)
        self.status_message.emit("Błąd odświeżania", 5000)
        QMessageBox.warning(self, "Błąd odświeżania", message)

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
        # usuń poprzednie konkretne daty (za separatorem, indeks 5)
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
        """Dodaje jedno właśnie zakończone nagranie do biblioteki od razu
        (wołane z MainWindow, podpięte pod ScannerEngine.signals.recording_finished),
        zamiast czekać na kolejne pełne przeskanowanie katalogu."""
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

        quality_cache = QualityCache()
        key = rec.cache_key()
        quality = quality_cache.get(key)
        if quality is None:
            quality = analyze_wav(path)
            quality_cache.put(key, quality)
            quality_cache.save()
        rec.quality = quality

        self._recordings.append(rec)
        self.table_model.append_recording(rec)
        self.freq_panel.set_recordings(self._recordings)
        self._rebuild_date_combo(self._recordings)
        self.stats_tab.update_stats(self._recordings)

        self._maybe_start_transcription()

    # ------------------------------------------------------------- filtrowanie
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

        if idx == 0:  # Wszystkie
            self.proxy.set_date_range(None, None)
        elif idx == 1:  # Dzisiaj
            self.proxy.set_date_range(today_start, today_start + timedelta(days=1))
        elif idx == 2:  # Ostatnie 24h
            self.proxy.set_date_range(now - timedelta(hours=24), now)
        elif idx == 3:  # Ostatnie 7 dni
            self.proxy.set_date_range(now - timedelta(days=7), now)
        elif idx == 4:  # Ostatnie 30 dni
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
        """Wolane z StatsTab.frequencySelected (dwuklik w wiersz) - filtruje
        Przegladarke do tej czestotliwosci. MainWindow, osobno podpiete pod
        ten sam sygnal, przelacza na gorna zakladke Odtwarzacz, zeby to
        bylo w ogole widoczne (Statystyki to teraz osobna zakladka)."""
        for row in range(self.freq_panel.list_widget.count()):
            item = self.freq_panel.list_widget.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == freq_khz:
                self.freq_panel.list_widget.setCurrentRow(row)
                break

    # ----------------------------------------------------------- transkrypcja
    def _hydrate_transcripts(self, recordings: List[Recording]) -> None:
        """Odtwarza wyniki transkrypcji i status ulubionego z trwałego cache
        na świeżo sparsowanej liście nagrań (scan_recordings za każdym
        odświeżeniem tworzy nowe obiekty Recording, więc bez tego liczylibyśmy
        wszystko od nowa / gubilibyśmy gwiazdki)."""
        for rec in recordings:
            rec.transcript = self._transcription_cache.get(rec.cache_key())
            rec.is_favorite = self._favorites_store.is_favorite(rec.cache_key())

    def _on_freq_panel_selection(self, value) -> None:
        if value == FAVORITES_ITEM:
            self.proxy.set_frequency_filter(ALL_ITEM_FREQ)
            self.proxy.set_favorites_only(True)
        else:
            self.proxy.set_favorites_only(False)
            self.proxy.set_frequency_filter(value)

    def _toggle_favorite(self) -> None:
        rec = self._selected_recording
        if rec is None:
            return
        rec.is_favorite = not rec.is_favorite
        self._favorites_store.set_favorite(rec.cache_key(), rec.is_favorite)
        self._set_favorite_btn_state(rec.is_favorite)
        self.proxy.refresh()
        self.freq_panel.set_recordings(self._recordings)  # refresh the "★ Ulubione (N)" count

    def _set_favorite_btn_state(self, is_favorite: bool) -> None:
        self.favorite_btn.setText("★ W ulubionych" if is_favorite else "☆ Dodaj do ulubionych")

    def _maybe_start_transcription(self, include_errors: bool = True) -> None:
        if not self.config.transcription.enabled:
            self.transcription_progress_label.setText("wyłączona")
            return
        if self._transcription_worker is not None and self._transcription_worker.isRunning():
            return
        self._transcription_worker = TranscriptionWorker(
            list(self._recordings), self.config.transcription, self._transcription_cache,
            include_errors=include_errors, parent=self,
        )
        self._transcription_worker.progress.connect(self._on_transcription_progress)
        self._transcription_worker.item_done.connect(self._on_transcription_item_done)
        self._transcription_worker.finished_batch.connect(self._on_transcription_batch_finished)
        self._transcription_worker.start()

    def _on_transcription_toggled(self, checked: bool) -> None:
        self.config.transcription.enabled = checked
        self.config.save()
        if checked:
            self._maybe_start_transcription()
        elif self._transcription_worker is not None and self._transcription_worker.isRunning():
            self._transcription_worker.request_stop()
            self.transcription_progress_label.setText("wyłączona")

    def _on_transcription_progress(self, done: int, total: int) -> None:
        if total == 0:
            self.transcription_progress_label.setText("gotowe")
        else:
            self.transcription_progress_label.setText(f"🎙 {done}/{total}")

    def _on_transcription_item_done(self, path_str: str, result: TranscriptionResult) -> None:
        path = Path(path_str)
        self.table_model.update_transcript(path, result)
        if self._selected_recording is not None and self._selected_recording.path == path:
            self._update_transcript_panel(self._selected_recording)

    def _on_transcription_batch_finished(self) -> None:
        if self.config.transcription.enabled:
            self.transcription_progress_label.setText("gotowe")
        # Coś mogło dotrzeć przez ingest_finished_recording() w trakcie tego
        # batcha - sprawdź jeszcze raz na kolejnym obiegu pętli zdarzeń (po
        # tym jak wątek na pewno już się zakończył), zamiast czekać na
        # kolejne ręczne odświeżenie. include_errors=False tu jest istotne:
        # bez tego, jeśli cokolwiek w tej paczce skończyło się na "error",
        # ten sam plik natychmiast kwalifikowałby się do kolejnej paczki,
        # która by natychmiast, w kółko, bez żadnej przerwy między próbami
        # (kiedyś realnie obserwowane: nieskończona pętla walenia w
        # GPU/CPU dla tych samych plików). Błędy i tak dostają szansę
        # ponownie przy następnym ręcznym odświeżeniu (F5) albo nowym nagraniu.
        QTimer.singleShot(0, lambda: self._maybe_start_transcription(include_errors=False))

    def _freq_display(self, freq_khz: int) -> str:
        """np. "Rzeszów Wieża (128.950 MHz)" gdy czestotliwosc jest znana z
        Listy pamieci, inaczej po prostu "128.950 MHz"."""
        name = self._freq_names.get(freq_khz)
        freq_text = f"{freq_khz / 1000:.3f} MHz"
        return f"{name} ({freq_text})" if name else freq_text

    def _update_transcript_panel(self, rec: Recording) -> None:
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
            header = "Transkrypcja: " + (
                "oczekuje w kolejce…" if self.config.transcription.enabled else "wyłączona (kliknij „Transkrybuj ponownie”, by spróbować ręcznie)"
            )
            self.transcript_text_edit.setPlainText("")
        elif status == "skipped_short":
            header = "Transkrypcja: nagranie zbyt krótkie, pominięte"
            self.transcript_text_edit.setPlainText("")
        elif status == "no_speech":
            header = "Transkrypcja: nie wykryto mowy (sam szum/trzask squelchu)"
            self.transcript_text_edit.setPlainText("")
        elif status == "error":
            header = "Transkrypcja: błąd (spróbuj ponownie)"
            self.transcript_text_edit.setPlainText("")
        else:
            t = rec.transcript
            conf_note = " — niska pewność, traktuj z rezerwą" if status == "low_confidence" else ""
            header = f"Transkrypcja [{t.language}, {t.language_probability * 100:.0f}%]{conf_note}:"
            self.transcript_text_edit.setPlainText(t.text or "(pusty wynik)")
        self.transcript_header_label.setText(f"[{freq_bit}] {header}")

    def _force_transcribe_selected(self) -> None:
        rec = self._selected_recording
        if rec is None or (self._force_transcribe_worker is not None and self._force_transcribe_worker.isRunning()):
            return
        self.transcribe_now_btn.setEnabled(False)
        self.transcript_header_label.setText("Transkrypcja: trwa ponowne przetwarzanie…")
        worker = TranscriptionWorker(
            [rec], self.config.transcription, self._transcription_cache, ignore_cache=True, parent=self
        )
        worker.item_done.connect(self._on_transcription_item_done)
        worker.finished_batch.connect(lambda: self.transcribe_now_btn.setEnabled(True))
        self._force_transcribe_worker = worker
        worker.start()

    # ------------------------------------------------------------- usuwanie
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
            prompt = (
                f"Trwale usunąć plik?\n\n{rec.path.name}\n{rec.freq_label} — "
                f"{rec.dt_local.strftime('%Y-%m-%d %H:%M:%S')}"
            )
        else:
            shown = "\n".join(r.path.name for r in recs[:10])
            more = f"\n… i {len(recs) - 10} więcej" if len(recs) > 10 else ""
            prompt = f"Trwale usunąć {len(recs)} plików?\n\n{shown}{more}"

        reply = QMessageBox.question(
            self,
            "Usuń nagrania" if len(recs) > 1 else "Usuń nagranie",
            prompt,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        playing_path = self.player.qt_player.source().toLocalFile()
        # Cache jakości/transkrypcji jest ładowany/zapisywany świeżo przy każdym
        # odświeżeniu (patrz RefreshWorker), więc otwieramy go tu tylko na
        # chwilę, żeby usunąć osierocone wpisy od razu, bez czekania na refresh.
        quality_cache = QualityCache()
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
            quality_cache.remove(cache_key)
            self._transcription_cache.remove(cache_key)
            self._favorites_store.remove(cache_key)
            self.table_model.remove_recording(rec.path)
            deleted_paths.append(rec.path)
        quality_cache.save()
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
                self.transcript_header_label.setText("Transkrypcja: wybierz nagranie z listy…")
                self.transcript_text_edit.setPlainText("")
                self.transcribe_now_btn.setEnabled(False)
                self.fr24_btn.setEnabled(False)
                self.adsbx_btn.setEnabled(False)
                self.save_freq_btn.setEnabled(False)
                self.favorite_btn.setEnabled(False)

            if len(deleted_paths) == 1:
                self.status_message.emit(f"Usunięto: {deleted_paths[0].name}", 4000)
            else:
                self.status_message.emit(f"Usunięto {len(deleted_paths)} plik(ów)", 4000)

        if failures:
            QMessageBox.warning(self, "Błąd usuwania", "Nie udało się usunąć:\n" + "\n".join(failures))

    # ---------------------------------------------------- linki zewnętrzne
    def _open_fr24_playback(self) -> None:
        rec = self._selected_recording
        if rec is None:
            return
        dt = rec.dt_utc
        lat = self.config.location.home_lat
        lon = self.config.location.home_lon
        url = f"https://www.flightradar24.com/{dt:%Y-%m-%d}/{dt:%H:%M}/20x/{lat},{lon}/9"
        QDesktopServices.openUrl(QUrl(url))

    def _open_adsbexchange_playback(self) -> None:
        rec = self._selected_recording
        if rec is None:
            return
        dt = rec.dt_utc
        lat = self.config.location.home_lat
        lon = self.config.location.home_lon
        url = f"https://globe.adsbexchange.com/?replay={dt:%Y-%m-%d-%H:%M}&lat={lat}&lon={lon}&zoom=8.8"
        QDesktopServices.openUrl(QUrl(url))

    def _save_frequency(self) -> None:
        rec = self._selected_recording
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

    def _on_playback_state_changed(self, state) -> None:
        self.play_btn.setText("⏸" if state == QMediaPlayer.PlaybackState.PlayingState else "▶")

    def _on_position_changed(self, position_ms: int) -> None:
        if not self.position_slider.isSliderDown():
            self.position_slider.setValue(position_ms)
        self.time_label.setText(f"{_format_time(position_ms)} / {_format_time(self.position_slider.maximum())}")

    def _on_duration_changed(self, duration_ms: int) -> None:
        self.position_slider.setRange(0, duration_ms)

    def _on_media_status_changed(self, status) -> None:
        if status == QMediaPlayer.MediaStatus.EndOfMedia and self.autoplay_checkbox.isChecked():
            self._play_next()

    # -- wywoływane z MainWindow ------------------------------------------
    def set_known_frequencies(self, channels) -> None:
        """Aktualizuje nazwy znanych czestotliwosci (z Listy pamieci) uzywane
        wszedzie tam, gdzie w tej zakladce pokazujemy czestotliwosc - pasek
        boczny, tabela, naglowek transkrypcji, etykieta "teraz odtwarzane"."""
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

    def shutdown(self) -> None:
        """Czeka na wątki robocze - wołane z closeEvent okna głównego."""
        if self._worker is not None and self._worker.isRunning():
            self._worker.wait(2000)
        for worker in (self._transcription_worker, self._force_transcribe_worker):
            if worker is not None and worker.isRunning():
                worker.request_stop()
                worker.wait(5000)
