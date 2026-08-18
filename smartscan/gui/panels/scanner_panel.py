"""Panel Glowny Skanera: device/timing controls, level meter, start/stop/skip."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QIcon
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QPushButton, QScrollArea, QToolButton,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ...config import AppConfig, VfoRangeConfig
from ...military_presets import BAND_PRESETS
from ...models import Channel
from ...sdr_backend import DEFAULT_GAIN_RANGE_DB, SoapyBackend
from ..widgets import LevelMeter, SliderSpin, SpectrumView

RESOURCES_DIR = Path(__file__).resolve().parent.parent / "resources"
_FOLDER_ICON = QIcon(str(RESOURCES_DIR / "folder.svg"))

_MODULATIONS = ["AM", "NFM", "WFM"]
_FOLDER_BG = QColor(70, 110, 170, 130)


class _BandRow(QWidget):
    """One editable row in the band-list editor, bound directly to a
    VfoRangeConfig instance living in AppConfig.bands - edits here mutate it
    in place, so no separate sync step is needed elsewhere.
    """

    def __init__(self, entry: VfoRangeConfig, on_change: Callable[[], None],
                 on_remove: Callable[["_BandRow"], None], parent=None):
        super().__init__(parent)
        self.entry = entry
        self._on_change = on_change
        self._loading = True

        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)

        self.enabled_check = QCheckBox()
        self.enabled_check.setToolTip("Wlacz/wylacz to pasmo bez usuwania go z listy.")
        self.enabled_check.setChecked(entry.enabled)
        self.enabled_check.toggled.connect(self._on_enabled)
        lay.addWidget(self.enabled_check)

        self.label_edit = QLineEdit(entry.label)
        self.label_edit.setPlaceholderText("Nazwa")
        self.label_edit.setMaximumWidth(150)
        self.label_edit.editingFinished.connect(self._on_label)
        lay.addWidget(self.label_edit)

        self.single_check = QCheckBox("1 kanal")
        self.single_check.setToolTip(
            "Pojedyncza czestotliwosc zamiast zakresu, np. 121.500 MHz (guard)\n"
            "obok skanowanego pasma."
        )
        self.single_check.setChecked(entry.start_hz == entry.stop_hz)
        self.single_check.toggled.connect(self._on_single_toggled)
        lay.addWidget(self.single_check)

        self.start_spin = QDoubleSpinBox()
        self.start_spin.setRange(0.01, 6000.0)
        self.start_spin.setDecimals(4)
        self.start_spin.setSuffix(" MHz")
        self.start_spin.setValue(entry.start_hz / 1e6)
        self.start_spin.valueChanged.connect(self._on_start)
        lay.addWidget(self.start_spin)

        self.stop_dash = QLabel("-")
        lay.addWidget(self.stop_dash)

        self.stop_spin = QDoubleSpinBox()
        self.stop_spin.setRange(0.01, 6000.0)
        self.stop_spin.setDecimals(4)
        self.stop_spin.setSuffix(" MHz")
        self.stop_spin.setValue(entry.stop_hz / 1e6)
        self.stop_spin.valueChanged.connect(self._on_stop)
        lay.addWidget(self.stop_spin)

        self.step_spin = QDoubleSpinBox()
        self.step_spin.setRange(0.1, 1000.0)
        self.step_spin.setDecimals(3)
        self.step_spin.setSuffix(" kHz krok")
        self.step_spin.setValue(entry.step_hz / 1e3)
        self.step_spin.valueChanged.connect(self._on_step)
        lay.addWidget(self.step_spin)

        self.mod_combo = QComboBox()
        self.mod_combo.addItems(_MODULATIONS)
        if entry.modulation in _MODULATIONS:
            self.mod_combo.setCurrentText(entry.modulation)
        self.mod_combo.setToolTip(
            "Modulacja dla tego pasma: AM (lotnictwo), NFM (PMR/amatorskie/morskie),\n"
            "WFM (radio FM)."
        )
        self.mod_combo.currentTextChanged.connect(self._on_modulation)
        lay.addWidget(self.mod_combo, 1)

        self.weight_spin = QDoubleSpinBox()
        self.weight_spin.setRange(0.0, 20.0)
        self.weight_spin.setDecimals(2)
        self.weight_spin.setSingleStep(0.5)
        self.weight_spin.setPrefix("W: ")
        self.weight_spin.setValue(entry.weight)
        self.weight_spin.setToolTip(
            "Waga priorytetu skanowania (0 = domyslna, bez wyroznienia). Im wyzsza\n"
            "wzgledem innych wazonych pasm/czestotliwosci, tym czesciej skaner do\n"
            "niego wraca - np. 1 i 2 znaczy \"dwa razy czesciej\". Ustawienie wagi na\n"
            "pojedynczej czestotliwosci z Listy pamieci (ponizej) obok wagi calego\n"
            "pasma pozwala czesciej wracac do niej, nie rezygnujac z reszty pasma."
        )
        self.weight_spin.valueChanged.connect(self._on_weight)
        lay.addWidget(self.weight_spin)

        self.remove_btn = QPushButton("✕")
        self.remove_btn.setFixedWidth(28)
        self.remove_btn.setToolTip("Usun to pasmo z listy.")
        self.remove_btn.clicked.connect(lambda: on_remove(self))
        lay.addWidget(self.remove_btn)

        self._loading = False
        self._apply_single_mode(self.single_check.isChecked())

    def _apply_single_mode(self, single: bool) -> None:
        self.stop_dash.setVisible(not single)
        self.stop_spin.setVisible(not single)
        self.step_spin.setVisible(not single)

    def _on_enabled(self, checked: bool) -> None:
        self.entry.enabled = checked
        self._notify()

    def _on_label(self) -> None:
        self.entry.label = self.label_edit.text().strip()
        self._notify()

    def _on_single_toggled(self, checked: bool) -> None:
        self._apply_single_mode(checked)
        if checked:
            self.entry.stop_hz = self.entry.start_hz
            self.stop_spin.blockSignals(True)
            self.stop_spin.setValue(self.start_spin.value())
            self.stop_spin.blockSignals(False)
        self._notify()

    def _on_start(self, value: float) -> None:
        self.entry.start_hz = int(round(value * 1e6))
        if self.single_check.isChecked():
            self.entry.stop_hz = self.entry.start_hz
            self.stop_spin.blockSignals(True)
            self.stop_spin.setValue(value)
            self.stop_spin.blockSignals(False)
        self._notify()

    def _on_stop(self, value: float) -> None:
        self.entry.stop_hz = int(round(value * 1e6))
        self._notify()

    def _on_step(self, value: float) -> None:
        self.entry.step_hz = int(round(value * 1e3))
        self._notify()

    def _on_modulation(self, text: str) -> None:
        self.entry.modulation = text
        self._notify()

    def _on_weight(self, value: float) -> None:
        self.entry.weight = value
        self._notify()

    def _notify(self) -> None:
        if not self._loading:
            self._on_change()


class _MemoryListRow(QWidget):
    """"Lista pamieci" jako jeszcze jedna pozycja w edytorze pasm: wlasny
    checkbox wlacz/wylacz (dziala jak pasmo - wylaczenie tu wylacza
    skanowanie wszystkich kanalow, nawet tych oznaczonych jako wlaczone
    pojedynczo w zakladce Kanaly), plus rozwijane drzewo aktualnie wlaczonych
    kanalow (z folderami jako podfolderami), gdzie mozna ustawic wage kazdego
    z nich - konkuruje ona z wagami pasm powyzej (patrz _BandRow.weight_spin).
    """

    def __init__(self, config: AppConfig, on_master_toggled: Callable[[], None],
                 on_weight_changed: Callable[[], None], parent=None):
        super().__init__(parent)
        self.config = config
        self._on_master_toggled = on_master_toggled
        self._on_weight_changed = on_weight_changed
        self._channels: list[Channel] = []
        self._loading = True

        root = QVBoxLayout(self)
        root.setContentsMargins(2, 2, 2, 2)
        root.setSpacing(2)

        header = QHBoxLayout()
        self.enabled_check = QCheckBox()
        self.enabled_check.setToolTip(
            "Wlacz/wylacz cala Liste pamieci naraz - dziala jak checkbox pasma.\n"
            "Wylaczenie tu wylacza skanowanie wszystkich kanalow, nawet tych\n"
            "oznaczonych jako wlaczone pojedynczo w zakladce Kanaly."
        )
        self.enabled_check.setChecked(config.memory_list_enabled)
        self.enabled_check.toggled.connect(self._on_enabled_toggled)
        header.addWidget(self.enabled_check)

        self.expand_btn = QToolButton()
        self.expand_btn.setCheckable(True)
        self.expand_btn.setArrowType(Qt.ArrowType.RightArrow)
        self.expand_btn.setToolTip("Rozwin/zwin liste wlaczonych kanalow i ich wag.")
        self.expand_btn.toggled.connect(self._on_expand_toggled)
        header.addWidget(self.expand_btn)

        title = QLabel("Lista pamieci (kanaly)")
        bold = title.font()
        bold.setBold(True)
        title.setFont(bold)
        header.addWidget(title)

        self.count_label = QLabel("")
        self.count_label.setStyleSheet("color: #9aa0a6;")
        header.addWidget(self.count_label, 1)
        root.addLayout(header)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Nazwa / Folder", "Czestotliwosc (MHz)", "Modulacja", "Waga"])
        hdr = self.tree.header()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        # Rozwinieta lista ma miec sporo miejsca od razu - to ekran, na ktorym
        # ustawia sie wagi, nie ma potrzeby sciskac go do malego okienka z
        # wlasnym przewijaniem; ewentualne przewijanie calej zakladki w dol
        # jest tu zupelnie w porzadku.
        self.tree.setMinimumHeight(420)
        self.tree.setVisible(False)
        root.addWidget(self.tree, 1)

        self._loading = False

    def set_channels(self, channels: list[Channel]) -> None:
        self._channels = list(channels)
        self._refresh()

    def _refresh(self) -> None:
        enabled = [c for c in self._channels if c.enabled]
        self.count_label.setText(f"{len(enabled)} wlaczonych" if enabled else "brak wlaczonych kanalow")
        self.tree.clear()

        by_folder: dict[str, list[Channel]] = {}
        top_level: list[Channel] = []
        for c in enabled:
            if c.folder:
                by_folder.setdefault(c.folder, []).append(c)
            else:
                top_level.append(c)

        for folder_name, chans in by_folder.items():
            folder_item = QTreeWidgetItem([folder_name, "", "", ""])
            folder_item.setIcon(0, _FOLDER_ICON)
            bold = folder_item.font(0)
            bold.setBold(True)
            for col in range(4):
                folder_item.setFont(col, bold)
                folder_item.setBackground(col, _FOLDER_BG)
            self.tree.addTopLevelItem(folder_item)
            folder_item.setExpanded(True)
            for c in chans:
                self._add_channel_item(folder_item, c)
        for c in top_level:
            self._add_channel_item(None, c)

    def _add_channel_item(self, parent: QTreeWidgetItem | None, channel: Channel) -> None:
        item = QTreeWidgetItem([
            channel.name or "(bez nazwy)", f"{channel.freq_hz / 1e6:.4f}", channel.modulation.value, "",
        ])
        item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if parent is None:
            self.tree.addTopLevelItem(item)
        else:
            parent.addChild(item)

        spin = QDoubleSpinBox()
        spin.setRange(0.0, 20.0)
        spin.setDecimals(2)
        spin.setSingleStep(0.5)
        spin.setPrefix("W: ")
        spin.setValue(channel.weight)
        spin.setToolTip(
            "Waga priorytetu tej czestotliwosci (0 = domyslna, bez wyroznienia).\n"
            "Konkuruje z wagami calych pasm - patrz podpowiedz przy wadze pasma\n"
            "powyzej."
        )
        spin.valueChanged.connect(lambda value, ch=channel: self._on_channel_weight(ch, value))
        self.tree.setItemWidget(item, 3, spin)

    def _on_channel_weight(self, channel: Channel, value: float) -> None:
        channel.weight = value
        if not self._loading:
            self._on_weight_changed()

    def _on_enabled_toggled(self, checked: bool) -> None:
        self.config.memory_list_enabled = checked
        if not self._loading:
            self._on_master_toggled()

    def _on_expand_toggled(self, checked: bool) -> None:
        self.expand_btn.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)
        self.tree.setVisible(checked)


class ScannerPanel(QGroupBox):
    start_requested = pyqtSignal()
    stop_requested = pyqtSignal()
    skip_requested = pyqtSignal()
    skip_blacklist_requested = pyqtSignal()
    mute_toggled = pyqtSignal(bool)
    settings_changed = pyqtSignal()
    memory_list_requested = pyqtSignal()  # "Lista pamieci" shortcut - handled by MainWindow
    channel_weight_changed = pyqtSignal()  # a channel's weight was edited inline - MainWindow persists/rebuilds

    def __init__(self, config: AppConfig, channels: list[Channel] | None = None, parent=None):
        super().__init__("Skaner", parent)
        self.config = config
        self.channels: list[Channel] = channels or []
        self._loading = True
        self._build_ui()
        self._load_from_config()
        self.memory_list_row.set_channels(self.channels)
        self._loading = False

    def set_channels(self, channels: list[Channel]) -> None:
        """Odswieza podglad Listy pamieci (nazwy/wagi) po zmianie kanalow z
        innej zakladki (Kanaly, albo "Zapisz czestotliwosc" w Odtwarzaczu)."""
        self.channels = channels
        self.memory_list_row.set_channels(self.channels)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        top_row = QHBoxLayout()
        self.device_combo = QComboBox()
        self._populate_devices()
        top_row.addWidget(QLabel("Urzadzenie SDR:"))
        top_row.addWidget(self.device_combo, 1)

        self.bandwidth_spin = QDoubleSpinBox()
        self.bandwidth_spin.setRange(0.25, 3.2)
        self.bandwidth_spin.setDecimals(2)
        self.bandwidth_spin.setSuffix(" MHz przechwytu")
        self.bandwidth_spin.setToolTip(
            "Szerokosc pasma jednorazowo przechwytywanego przez SDR (np. 2.4 MHz dla RTL-SDR).\n"
            "Skaner grupuje caly zakres/liste kanalow w jak najmniej takich okien zamiast\n"
            "przestrajac sie kanal-po-kanale."
        )
        self.bandwidth_spin.valueChanged.connect(self._push_settings)
        top_row.addWidget(self.bandwidth_spin)
        root.addLayout(top_row)

        # Pasma (VFO) i Lista pamieci sa zawsze skanowane razem, w jednej
        # sesji - ulozone jedno pod drugim (jak strona do przewijania), a nie
        # w splitterze: kazda sekcja dostaje tyle miejsca, ile faktycznie
        # potrzebuje, zamiast musiec byc sciskana zeby zrobic miejsce
        # sasiadowi. Caly Nasluch zyje w jednym QScrollArea (patrz
        # MainWindow), wiec przewijanie w dol po wiecej jest zawsze dostepne.
        root.addWidget(self._build_vfo_form())
        root.addWidget(self._build_operate_area())

    def _build_operate_area(self) -> QWidget:
        w = QWidget()
        root = QVBoxLayout(w)
        root.setContentsMargins(0, 0, 0, 0)

        timing_form = QFormLayout()
        self.tuning_slider = SliderSpin(0, 5000, 60, decimals=0, suffix="ms")
        self.tuning_slider.valueChanged.connect(self._push_settings)
        timing_form.addRow("Tuning Time:", self.tuning_slider)

        self.linger_slider = SliderSpin(0, 30000, 1500, decimals=0, suffix="ms")
        self.linger_slider.valueChanged.connect(self._push_settings)
        timing_form.addRow("Linger Time:", self.linger_slider)

        self.trigger_slider = SliderSpin(-90, 0, -30, decimals=0, suffix="dBFS")
        self.trigger_slider.valueChanged.connect(self._on_trigger_changed)
        timing_form.addRow("Trigger Level / Squelch:", self.trigger_slider)

        timing_form.addRow("Gain:", self._build_gain_row())
        timing_form.addRow("Redukcja szumow:", self._build_noise_reduction_row())

        self.wfm_stereo_check = QCheckBox("Stereo (FM WFM)")
        self.wfm_stereo_check.setToolTip(
            "Dekoduje stereo (pilot 19 kHz) przy nasluchu na zywo pasma radia\n"
            "FM (WFM) - nie ma znaczenia dla AM/NFM. Nagrania zawsze zostaja mono."
        )
        self.wfm_stereo_check.toggled.connect(self._on_wfm_stereo_toggled)
        timing_form.addRow("", self.wfm_stereo_check)

        root.addLayout(timing_form)

        self.level_meter = LevelMeter()
        root.addWidget(self.level_meter)

        self.spectrum_view = SpectrumView()
        root.addWidget(self.spectrum_view, 1)

        # Real, *measured* time between two consecutive visits to the same
        # capture window (see ScannerEngine._window_last_visit /
        # cycle_time_changed) - deliberately not a theoretical estimate
        # (num_windows * (tuning+dwell)): weighted lanes and the default
        # lane get visited at very different real rates, so a single
        # formula would be misleading. Subtle by design - this is a tuning
        # aid, not something that needs to grab attention.
        self.cycle_time_label = QLabel("")
        self.cycle_time_label.setStyleSheet("font-size: 11px; color: #6b7078;")
        self.cycle_time_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        root.addWidget(self.cycle_time_label)

        status_row = QHBoxLayout()
        status_col = QVBoxLayout()
        name_row = QHBoxLayout()
        self.name_label = QLabel("")
        self.name_label.setStyleSheet("font-size: 22px; font-weight: bold;")
        self.name_label.setWordWrap(True)
        self.state_label = QLabel("IDLE")
        self.state_label.setStyleSheet("font-size: 14px; color: #9aa0a6;")
        name_row.addWidget(self.name_label, 1)
        name_row.addWidget(self.state_label)
        status_col.addLayout(name_row)
        self.freq_label = QLabel("--- MHz")
        self.freq_label.setStyleSheet("font-size: 14px; color: #9aa0a6;")
        status_col.addWidget(self.freq_label)
        # Jednoczesnie aktywne kanaly poza tym pierwszym (patrz _Slot w
        # scanner_engine.py - jedno okno przechwytu moze obslugiwac dowolnie
        # wiele transmisji naraz, bez sztywnego limitu) - pokazujemy do 2
        # kolejnych tutaj, a przy wiecej niz 3 lacznie ostatni wiersz dostaje
        # dopisek "(+N wiecej)" zamiast dorzucania kolejnych wierszy w
        # nieskonczonosc. Oba ukryte dopoki nie ma czego pokazac.
        self.name_label2 = QLabel("")
        self.name_label2.setStyleSheet("font-size: 15px; font-weight: bold; color: #c9c9cf;")
        self.name_label2.setWordWrap(True)
        self.name_label2.setVisible(False)
        status_col.addWidget(self.name_label2)
        self.freq_label2 = QLabel("")
        self.freq_label2.setStyleSheet("font-size: 12px; color: #9aa0a6;")
        self.freq_label2.setVisible(False)
        status_col.addWidget(self.freq_label2)
        self.name_label3 = QLabel("")
        self.name_label3.setStyleSheet("font-size: 15px; font-weight: bold; color: #c9c9cf;")
        self.name_label3.setWordWrap(True)
        self.name_label3.setVisible(False)
        status_col.addWidget(self.name_label3)
        self.freq_label3 = QLabel("")
        self.freq_label3.setStyleSheet("font-size: 12px; color: #9aa0a6;")
        self.freq_label3.setVisible(False)
        status_col.addWidget(self.freq_label3)
        status_row.addLayout(status_col)
        root.addLayout(status_row)

        btn_row = QHBoxLayout()
        self.start_stop_btn = QPushButton("START")
        self.start_stop_btn.setCheckable(True)
        self.start_stop_btn.setMinimumHeight(40)
        self.start_stop_btn.clicked.connect(self._on_start_stop)
        self.skip_btn = QPushButton("SKIP")
        self.skip_btn.setMinimumHeight(40)
        self.skip_btn.setEnabled(False)
        self.skip_btn.setToolTip(
            "Przechodzi natychmiast do kolejnej czestotliwosci/kanalu, bez\n"
            "czekania na koniec transmisji/linger. NIE dodaje do czarnej listy -\n"
            "ta czestotliwosc zostanie odwiedzona ponownie w kolejnym przebiegu\n"
            "skanowania. Uzyj tego, gdy cos ciekawego dzieje sie na biezacym\n"
            "kanale i chcesz go zostawic w spokoju na przyszlosc, ale isc dalej."
        )
        self.skip_btn.clicked.connect(self.skip_requested.emit)
        self.skip_blacklist_btn = QPushButton("SKIP + BLACKLIST")
        self.skip_blacklist_btn.setMinimumHeight(40)
        self.skip_blacklist_btn.setEnabled(False)
        self.skip_blacklist_btn.setToolTip(
            "Jak SKIP, ale dodatkowo trwale blokuje biezaca czestotliwosc\n"
            "(dodaje do czarnej listy) - skaner juz jej nie odwiedzi. Dziala od\n"
            "razu, bez potwierdzenia; cofniesz to kasujac wpis w zakladce\n"
            "Czarna lista."
        )
        self.skip_blacklist_btn.clicked.connect(self.skip_blacklist_requested.emit)
        self.mute_btn = QPushButton("WYCISZ")
        self.mute_btn.setCheckable(True)
        self.mute_btn.setMinimumHeight(40)
        self.mute_btn.setToolTip(
            "Wycisza tylko dzwiek na zywo w sluchawkach/glosnikach - nagrywanie na\n"
            "dysk (jesli wlaczone) dziala dalej w tle, wiec mozna nadrobic zaleglosci\n"
            "odsluchujac nagrania z zakladki Nagrania podczas gdy skaner pracuje po cichu."
        )
        self.mute_btn.toggled.connect(self._on_mute_toggled)
        btn_row.addWidget(self.start_stop_btn, 2)
        btn_row.addWidget(self.skip_btn, 1)
        btn_row.addWidget(self.skip_blacklist_btn, 1)
        btn_row.addWidget(self.mute_btn, 1)
        root.addLayout(btn_row)
        return w

    def _build_gain_row(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        self.agc_check = QCheckBox("Auto (AGC)")
        self.agc_check.toggled.connect(self._on_agc_toggled)
        lo, hi = DEFAULT_GAIN_RANGE_DB
        self.gain_slider = SliderSpin(lo, hi, 30.0, decimals=1, suffix="dB")
        self.gain_slider.valueChanged.connect(self._on_gain_changed)
        lay.addWidget(self.agc_check)
        lay.addWidget(self.gain_slider, 1)
        return w

    def _build_noise_reduction_row(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        self.nr_check = QCheckBox("Wlacz (inteligentna, adaptacyjna)")
        self.nr_check.setToolTip(
            "Sledzi na biezaco poziom szumu tla w widmie dzwieku i wycisza pasma\n"
            "bliskie temu poziomowi (spectral gating), zostawiajac pasma z mowa\n"
            "wzglednie nietkniete. Pomaga przy slabej antenie/niskim SNR, ale przy\n"
            "bardzo silnym szumie kosztem lekko \"cyfrowego\" brzmienia audio."
        )
        self.nr_check.toggled.connect(self._on_nr_toggled)
        self.nr_strength_slider = SliderSpin(0, 100, 60, decimals=0, suffix="%")
        self.nr_strength_slider.valueChanged.connect(self._on_nr_strength_changed)
        lay.addWidget(self.nr_check)
        lay.addWidget(self.nr_strength_slider, 1)
        return w

    def _build_vfo_form(self) -> QWidget:
        w = QWidget()
        root = QVBoxLayout(w)
        root.setContentsMargins(0, 0, 0, 0)

        hint = QLabel(
            "Źródła do skanowania - dodawaj presety, wlasne zakresy (z wlasnym "
            "start/stop/step) lub pojedyncze czestotliwosci, np. 121.500 MHz "
            "obok skanowanego pasma. Wszystkie wlaczone pozycje ponizej sa "
            "skanowane razem, w jednej sesji, wraz z kazdym wlaczonym kanalem "
            "z Listy pamieci ponizej - nie trzeba miedzy nimi przelaczac."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #9aa0a6;")
        root.addWidget(hint)

        self.bands_container = QWidget()
        self.bands_layout = QVBoxLayout(self.bands_container)
        self.bands_layout.setContentsMargins(0, 0, 0, 0)
        self.bands_layout.setSpacing(2)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.bands_container)
        scroll.setMinimumHeight(190)
        root.addWidget(scroll, 1)

        # Lista pamieci jest tu, tuz pod wybranymi pasmami/kanalami i nad
        # "Dodaj preset" - jest tak samo "zrodlem" do skanowania jak kazde
        # pasmo powyzej, wiec logicznie nalezy do tej samej listy, nie na
        # koncu za formularzami dodawania.
        self.memory_list_row = _MemoryListRow(
            self.config,
            on_master_toggled=self.settings_changed.emit,
            on_weight_changed=self.channel_weight_changed.emit,
        )
        root.addWidget(self.memory_list_row)

        preset_row = QHBoxLayout()
        self.preset_combo = QComboBox()
        for preset in BAND_PRESETS:
            self.preset_combo.addItem(preset.label, preset.id)
            self.preset_combo.setItemData(
                self.preset_combo.count() - 1, preset.tooltip, Qt.ItemDataRole.ToolTipRole
            )
        add_preset_btn = QPushButton("Dodaj preset")
        add_preset_btn.clicked.connect(self._on_add_preset)
        preset_row.addWidget(self.preset_combo, 1)
        preset_row.addWidget(add_preset_btn)
        add_custom_btn = QPushButton("Dodaj wlasny zakres")
        add_custom_btn.clicked.connect(self._on_add_custom_range)
        preset_row.addWidget(add_custom_btn)
        memory_list_btn = QPushButton("Lista pamieci (kanaly)")
        memory_list_btn.setToolTip(
            "Przelacz na zakladke Kanaly, gdzie mozna zarzadzac pojedynczymi\n"
            "czestotliwosciami/folderami. Sa one zawsze skanowane razem z\n"
            "pasmami powyzej, w jednej sesji."
        )
        memory_list_btn.clicked.connect(self.memory_list_requested.emit)
        preset_row.addWidget(memory_list_btn)
        root.addLayout(preset_row)

        freq_row = QHBoxLayout()
        freq_row.addWidget(QLabel("Szybkie dodanie czestotliwosci:"))
        self.quick_freq_spin = QDoubleSpinBox()
        self.quick_freq_spin.setRange(0.01, 6000.0)
        self.quick_freq_spin.setDecimals(4)
        self.quick_freq_spin.setSuffix(" MHz")
        self.quick_freq_spin.setValue(121.500)
        freq_row.addWidget(self.quick_freq_spin)
        add_freq_btn = QPushButton("Dodaj")
        add_freq_btn.setToolTip(
            "Dodaje pojedynczy kanal do listy, np. 121.500 MHz (guard)\n"
            "obok skanowanego pasma."
        )
        add_freq_btn.clicked.connect(self._on_add_frequency)
        freq_row.addWidget(add_freq_btn)
        freq_row.addStretch(1)
        root.addLayout(freq_row)

        return w

    def _populate_devices(self) -> None:
        self.device_combo.addItem("Automatycznie (pierwsze wykryte)", ("auto", ""))
        for dev in SoapyBackend.enumerate_devices():
            label = dev.get("label") or dev.get("driver", "device")
            args = ",".join(f"{k}={v}" for k, v in dev.items())
            self.device_combo.addItem(label, (dev.get("driver", "auto"), args))
        self.device_combo.addItem("Symulacja (brak sprzetu)", ("simulated", ""))

    def _load_from_config(self) -> None:
        self.bandwidth_spin.setValue(self.config.device.sample_rate_hz / 1e6)
        self.tuning_slider.set_value(self.config.timing.tuning_time_ms)
        self.linger_slider.set_value(self.config.timing.linger_time_ms)
        self.trigger_slider.set_value(self.config.trigger_level_dbfs)
        self.level_meter.set_trigger(self.config.trigger_level_dbfs)
        self.agc_check.setChecked(self.config.device.gain_mode == "auto")
        self.gain_slider.set_value(self.config.device.gain_db)
        self.gain_slider.set_enabled_all(self.config.device.gain_mode != "auto")
        self.nr_check.setChecked(self.config.noise_reduction.enabled)
        self.nr_strength_slider.set_value(round(self.config.noise_reduction.strength * 100))
        self.nr_strength_slider.set_enabled_all(self.config.noise_reduction.enabled)
        self.wfm_stereo_check.setChecked(self.config.wfm_stereo_enabled)
        target = (self.config.device.driver, self.config.device.device_args)
        for i in range(self.device_combo.count()):
            if self.device_combo.itemData(i) == target:
                self.device_combo.setCurrentIndex(i)
                break
        self._rebuild_band_rows()

    def _rebuild_band_rows(self) -> None:
        while self.bands_layout.count():
            item = self.bands_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for entry in self.config.bands:
            row = _BandRow(entry, on_change=self._on_band_changed, on_remove=self._on_band_removed)
            self.bands_layout.addWidget(row)
        self.bands_layout.addStretch(1)

    def _on_band_changed(self) -> None:
        if self._loading:
            return
        self.settings_changed.emit()

    def _on_band_removed(self, row: "_BandRow") -> None:
        self.config.bands = [b for b in self.config.bands if b is not row.entry]
        self._rebuild_band_rows()
        self.settings_changed.emit()

    def _on_add_preset(self) -> None:
        preset_id = self.preset_combo.currentData()
        preset = next((p for p in BAND_PRESETS if p.id == preset_id), None)
        if preset is None:
            return
        self.config.bands.append(preset.to_band())
        self._rebuild_band_rows()
        self.settings_changed.emit()

    def _on_add_custom_range(self) -> None:
        self.config.bands.append(VfoRangeConfig(
            start_hz=118_000_000, stop_hz=118_500_000, step_hz=25_000, label="Wlasny zakres",
        ))
        self._rebuild_band_rows()
        self.settings_changed.emit()

    def _on_add_frequency(self) -> None:
        freq_hz = int(round(self.quick_freq_spin.value() * 1e6))
        self.config.bands.append(VfoRangeConfig(
            start_hz=freq_hz, stop_hz=freq_hz, step_hz=25_000,
            label=f"{self.quick_freq_spin.value():.4f} MHz",
        ))
        self._rebuild_band_rows()
        self.settings_changed.emit()

    def _on_trigger_changed(self, value: float) -> None:
        self.level_meter.set_trigger(value)
        if self._loading:
            return
        self.config.trigger_level_dbfs = value
        self.settings_changed.emit()

    def _on_agc_toggled(self, checked: bool) -> None:
        self.gain_slider.set_enabled_all(not checked)
        if self._loading:
            return
        self.config.device.gain_mode = "auto" if checked else "manual"
        self.settings_changed.emit()

    def _on_gain_changed(self, value: float) -> None:
        if self._loading:
            return
        self.config.device.gain_db = value
        self.settings_changed.emit()

    def _on_nr_toggled(self, checked: bool) -> None:
        self.nr_strength_slider.set_enabled_all(checked)
        if self._loading:
            return
        self.config.noise_reduction.enabled = checked
        self.settings_changed.emit()

    def _on_nr_strength_changed(self, value: float) -> None:
        if self._loading:
            return
        self.config.noise_reduction.strength = value / 100.0
        self.settings_changed.emit()

    def _on_wfm_stereo_toggled(self, checked: bool) -> None:
        if self._loading:
            return
        self.config.wfm_stereo_enabled = checked
        self.settings_changed.emit()

    def _push_settings(self, *_args) -> None:
        if self._loading:
            return
        self.config.device.sample_rate_hz = self.bandwidth_spin.value() * 1e6
        self.config.timing.tuning_time_ms = int(self.tuning_slider.value())
        self.config.timing.linger_time_ms = int(self.linger_slider.value())
        driver, args = self.device_combo.currentData() or ("auto", "")
        self.config.device.driver = driver
        self.config.device.device_args = args
        self.settings_changed.emit()

    # -- called by MainWindow ------------------------------------------------
    def set_running(self, running: bool) -> None:
        self.start_stop_btn.setChecked(running)
        self.start_stop_btn.setText("STOP" if running else "START")
        self.skip_btn.setEnabled(running)
        self.skip_blacklist_btn.setEnabled(running)
        self.device_combo.setEnabled(not running)
        self.bandwidth_spin.setEnabled(not running)
        if not running:
            self.cycle_time_label.setText("")

    def _on_mute_toggled(self, checked: bool) -> None:
        self.mute_btn.setText("WYCISZONE" if checked else "WYCISZ")
        self.mute_toggled.emit(checked)

    def _on_start_stop(self, checked: bool) -> None:
        self._push_settings()
        if checked:
            self.start_requested.emit()
        else:
            self.stop_requested.emit()

    def update_level(self, db: float) -> None:
        self.level_meter.set_level(db)

    def update_freq(self, entries: list) -> None:
        """Aktualizuje wyswietlana czestotliwosc/czestotliwosci. ``entries``
        to lista (freq_hz, name) - jeden wpis na kazdy aktualnie aktywny
        kanal (nie ma sztywnego limitu ile naraz - patrz _Slot w
        scanner_engine.py); pierwszy wpis to zawsze ten aktualnie slyszalny
        na zywo. Wyswietlacz pokazuje najwyzej 3 wiersze - przy wiecej niz 3
        aktywnych kanalach ostatni wiersz dostaje dopisek "(+N wiecej)"
        zamiast dorzucania kolejnych wierszy w nieskonczonosc. Jesli nazwa
        jest znana (np. z Listy pamieci), wyswietlamy ja w duzej,
        wytluszczonej czcionce - czytelniej niz samej liczby - a
        czestotliwosc mniejszym drugorzednym tekstem obok/pod nia."""
        if not entries:
            self.name_label.setText("--- MHz")
            self.freq_label.setVisible(False)
            self.name_label2.setVisible(False)
            self.freq_label2.setVisible(False)
            self.name_label3.setVisible(False)
            self.freq_label3.setVisible(False)
            return

        freq_hz, name = entries[0]
        freq_text = f"{freq_hz / 1e6:.4f} MHz"
        if name:
            self.name_label.setText(name)
            self.freq_label.setText(freq_text)
            self.freq_label.setVisible(True)
        else:
            self.name_label.setText(freq_text)
            self.freq_label.setVisible(False)

        secondary_rows = entries[1:3]  # up to 2 more rows (3 shown total)
        overflow = len(entries) - 3
        row_widgets = [(self.name_label2, self.freq_label2), (self.name_label3, self.freq_label3)]
        for idx, (name_label, freq_label) in enumerate(row_widgets):
            if idx >= len(secondary_rows):
                name_label.setVisible(False)
                freq_label.setVisible(False)
                continue
            freq_hz_r, name_r = secondary_rows[idx]
            freq_text_r = f"{freq_hz_r / 1e6:.4f} MHz"
            suffix = f"  (+{overflow} wiecej)" if idx == len(secondary_rows) - 1 and overflow > 0 else ""
            name_label.setText(f"+ {(name_r or freq_text_r)}{suffix}")
            freq_label.setText(freq_text_r)
            name_label.setVisible(True)
            freq_label.setVisible(bool(name_r))

    def update_state(self, state: str) -> None:
        self.state_label.setText(state)
        if state == "RECONNECTING":
            self.state_label.setStyleSheet("font-size: 14px; color: #ff9f0a; font-weight: bold;")
        else:
            self.state_label.setStyleSheet("font-size: 14px; color: #9aa0a6;")

    def update_spectrum(self, freqs_hz, db, trigger_dbfs: float) -> None:
        self.spectrum_view.update_spectrum(freqs_hz, db, trigger_dbfs)

    def update_cycle_time(self, seconds: float) -> None:
        """Real, measured interval since this same capture window was last
        tuned to - see ScannerSignals.cycle_time_changed."""
        self.cycle_time_label.setText(f"Powrót do tego okna: {seconds:.2f}s temu")

    def update_gain_range(self, lo: float, hi: float) -> None:
        self.gain_slider.set_range(lo, hi)
