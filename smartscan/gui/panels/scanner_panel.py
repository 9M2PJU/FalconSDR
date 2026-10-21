"""Main Scanner panel: device/timing controls, level meter, start/stop/skip."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QIcon
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QPushButton, QScrollArea, QToolButton,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ...config import AppConfig, VfoRangeConfig
from ...i18n import tr
from ...military_presets import get_all_presets
from ...models import Channel
from ...sdr_backend import DEFAULT_GAIN_RANGE_DB, enumerate_devices
from ..widgets import BAND_OVERRIDE_ACCENT, LevelMeter, SliderSpin, SpectrumView, apply_emoji_font

RESOURCES_DIR = Path(__file__).resolve().parent.parent / "resources"
_FOLDER_ICON = QIcon(str(RESOURCES_DIR / "folder.svg"))

_MODULATIONS = ["AM", "NFM", "WFM", "SSB"]
_FOLDER_BG = QColor(70, 110, 170, 130)


class _BandRow(QWidget):
    """One editable row in the band-list editor, bound directly to a
    VfoRangeConfig instance living in AppConfig.bands - edits here mutate it
    in place, so no separate sync step is needed elsewhere.
    """

    def __init__(self, entry: VfoRangeConfig, on_change: Callable[[], None],
                 on_remove: Callable[["_BandRow"], None],
                 on_advanced: Callable[["_BandRow"], None], parent=None):
        super().__init__(parent)
        self.entry = entry
        self._on_change = on_change
        self._loading = True

        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)

        self.enabled_check = QCheckBox()
        self.enabled_check.setToolTip(tr("Enable/disable this band without removing it from the list."))
        self.enabled_check.setChecked(entry.enabled)
        self.enabled_check.toggled.connect(self._on_enabled)
        lay.addWidget(self.enabled_check)

        self.label_edit = QLineEdit(entry.label)
        self.label_edit.setPlaceholderText(tr("Name"))
        self.label_edit.setMaximumWidth(150)
        apply_emoji_font(self.label_edit)
        self.label_edit.editingFinished.connect(self._on_label)
        lay.addWidget(self.label_edit)

        self.single_check = QCheckBox(tr("1 channel"))
        self.single_check.setToolTip(tr(
            "A single frequency instead of a range, e.g. 121.500 MHz (guard)\n"
            "alongside the scanned band."
        ))
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
        self.step_spin.setSuffix(tr(" kHz step"))
        self.step_spin.setValue(entry.step_hz / 1e3)
        self.step_spin.valueChanged.connect(self._on_step)
        lay.addWidget(self.step_spin)

        self.mod_combo = QComboBox()
        self.mod_combo.addItems(_MODULATIONS)
        if entry.modulation in _MODULATIONS:
            self.mod_combo.setCurrentText(entry.modulation)
        self.mod_combo.setToolTip(tr(
            "Modulation for this band: AM (aviation), NFM (PMR/ham/marine),\n"
            "WFM (FM broadcast radio)."
        ))
        self.mod_combo.currentTextChanged.connect(self._on_modulation)
        lay.addWidget(self.mod_combo, 1)

        self.weight_spin = QDoubleSpinBox()
        self.weight_spin.setRange(0.0, 20.0)
        self.weight_spin.setDecimals(2)
        self.weight_spin.setSingleStep(0.5)
        self.weight_spin.setPrefix("W: ")
        self.weight_spin.setValue(entry.weight)
        self.weight_spin.setToolTip(tr(
            "Scan priority weight (0 = default, no special treatment). The "
            "higher it is relative to other weighted bands/frequencies, the "
            "more often the scanner returns to it - e.g. 1 and 2 means "
            "\"twice as often\". Setting a weight on a single frequency from "
            "the Memory List (below), alongside a weight on the whole band, "
            "lets you return to it more often without giving up on the rest "
            "of the band."
        ))
        self.weight_spin.valueChanged.connect(self._on_weight)
        lay.addWidget(self.weight_spin)

        # The gear glyph has always rendered correctly as a color glyph
        # (NotoColorEmoji does cover U+2699 - confirmed directly in the
        # font), the problem was elsewhere: a global QPushButton { padding:
        # 6px 12px; } from theme.py's DARK_STYLESHEET combined with
        # setFixedWidth(28) leaves ~2px for the glyph itself (measured:
        # QPushButton("✕").sizeHint() under this style is QSize(37, 32),
        # i.e. 9px wider than the button) - hence it looks like a dot,
        # regardless of which character we put there. A local padding
        # override (below) gives the glyph room within that same 28px width
        # instead of changing the character.
        _ICON_BTN_STYLE = "QPushButton { padding: 2px; }"

        self.advanced_btn = QPushButton("⚙")
        self.advanced_btn.setFixedWidth(28)
        self.advanced_btn.setStyleSheet(_ICON_BTN_STYLE)
        self.advanced_btn.setCheckable(True)
        self.advanced_btn.setToolTip(tr(
            "Advanced - a trigger level, tuning/linger time, gain and noise\n"
            "reduction of its own for just this band, independent of the\n"
            "rest of the scanned bands/channels (see the sliders below once clicked)."
        ))
        self.advanced_btn.clicked.connect(lambda: on_advanced(self))
        lay.addWidget(self.advanced_btn)

        self.remove_btn = QPushButton("✕")
        self.remove_btn.setFixedWidth(28)
        self.remove_btn.setStyleSheet(_ICON_BTN_STYLE)
        self.remove_btn.setToolTip(tr("Remove this band from the list."))
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
    """The Memory List as one more entry in the band editor: its own
    enable/disable checkbox (behaves like a band - disabling it here disables
    scanning of every channel, even ones individually marked enabled in the
    Channels tab), plus an expandable tree of the currently enabled channels
    (with folders as sub-folders), where each one's weight can be set - it
    competes with the band weights above (see _BandRow.weight_spin).
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
        self.enabled_check.setToolTip(tr(
            "Enable/disable the whole Memory List at once - behaves like a "
            "band's checkbox.\nDisabling it here disables scanning of every "
            "channel, even ones\nindividually marked enabled in the Channels tab."
        ))
        self.enabled_check.setChecked(config.memory_list_enabled)
        self.enabled_check.toggled.connect(self._on_enabled_toggled)
        header.addWidget(self.enabled_check)

        self.expand_btn = QToolButton()
        self.expand_btn.setCheckable(True)
        self.expand_btn.setArrowType(Qt.ArrowType.RightArrow)
        self.expand_btn.setToolTip(tr("Expand/collapse the list of enabled channels and their weights."))
        self.expand_btn.toggled.connect(self._on_expand_toggled)
        header.addWidget(self.expand_btn)

        title = QLabel(tr("Memory List (channels)"))
        bold = title.font()
        bold.setBold(True)
        title.setFont(bold)
        header.addWidget(title)

        self.count_label = QLabel("")
        self.count_label.setStyleSheet("color: #9aa0a6;")
        header.addWidget(self.count_label, 1)
        root.addLayout(header)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([tr("Name / Folder"), tr("Frequency (MHz)"), tr("Modulation"), tr("Weight")])
        hdr = self.tree.header()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        # The expanded list should have plenty of room right away - this is
        # the screen where weights get set, no need to squeeze it into a
        # small box with its own scrolling; scrolling the whole tab further
        # down is perfectly fine here.
        self.tree.setMinimumHeight(420)
        self.tree.setVisible(False)
        root.addWidget(self.tree, 1)

        self._loading = False

    def set_channels(self, channels: list[Channel]) -> None:
        self._channels = list(channels)
        self._refresh()

    def _refresh(self) -> None:
        enabled = [c for c in self._channels if c.enabled]
        self.count_label.setText(
            tr("{count} enabled").format(count=len(enabled)) if enabled else tr("no channels enabled")
        )
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
            channel.name or tr("(unnamed)"), f"{channel.freq_hz / 1e6:.4f}", channel.modulation.value, "",
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
        spin.setToolTip(tr(
            "Priority weight for this frequency (0 = default, no special\n"
            "treatment). Competes with whole-band weights - see the weight\n"
            "tooltip above."
        ))
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
    start_requested = Signal()
    stop_requested = Signal()
    skip_requested = Signal()
    skip_blacklist_requested = Signal()
    mute_toggled = Signal(bool)
    # Manual, trigger-independent continuous recording - see
    # ScannerEngine.start_manual_recording()/stop_manual_recording(). True =
    # start, False = stop. Only enabled while the scanner is running (see
    # set_running()).
    manual_record_toggled = Signal(bool)
    # Same underlying config.recording.enabled as RecordingPanel's own
    # checkbox (Options tab) - MainWindow keeps the two in sync so either
    # one reflects a change made in the other.
    auto_recording_toggled = Signal(bool)
    # Raw IQ Record/Stop button - a manual, momentary action (like
    # manual_record_toggled above), never persisted to config and never
    # synced anywhere else, so this is just "user clicked it" -> "ask
    # MainWindow to start/stop ScannerEngine.start_raw_iq_recording()". Lives
    # here (Listen tab, right under Mute) rather than in RecordingPanel
    # (Options) by explicit request - a technical but still one-click,
    # in-the-moment action belongs next to the other live scanning controls,
    # not buried in settings.
    raw_iq_record_toggled = Signal(bool)
    settings_changed = Signal()
    memory_list_requested = Signal()  # "Memory list" shortcut - handled by MainWindow
    channel_weight_changed = Signal()  # a channel's weight was edited inline - MainWindow persists/rebuilds
    # Emitted (VfoRangeConfig or None) when a band's "advanced" edit mode
    # starts/stops - MainWindow forwards this straight to
    # ScannerEngine.set_solo_band() so a running scan restricts itself to
    # just that band while it's being tuned in isolation.
    solo_band_changed = Signal(object)

    def __init__(self, config: AppConfig, channels: list[Channel] | None = None, parent=None):
        super().__init__(tr("Scanner"), parent)
        self.config = config
        self.channels: list[Channel] = channels or []
        self._loading = True
        # "Advanced" per-band edit mode state (Scanner tab band list) - see
        # _start_editing_band/_stop_editing_band.
        self._editing_band: VfoRangeConfig | None = None
        self._editing_row: "_BandRow | None" = None
        self._band_rows: list["_BandRow"] = []
        self._build_ui()
        self._load_from_config()
        self.memory_list_row.set_channels(self.channels)
        self._loading = False

    @property
    def editing_band(self) -> VfoRangeConfig | None:
        """Read by MainWindow._start_scanner() to pick up an edit already in
        progress when Start is pressed (e.g. tuning a band's settings before
        ever starting the scan for the first time this session)."""
        return self._editing_band

    def set_channels(self, channels: list[Channel]) -> None:
        """Refreshes the Memory List preview (names/weights) after channels
        change from another tab (Channels, or "Save frequency" in Odtwarzacz)."""
        self.channels = channels
        self.memory_list_row.set_channels(self.channels)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        top_row = QHBoxLayout()
        self.device_combo = QComboBox()
        self._populate_devices()
        self.device_combo.setToolTip(tr(
            "Locked while the scanner is running - switching hardware means "
            "closing and reopening the SDR stream, which needs a Stop first."
        ))
        top_row.addWidget(QLabel(tr("SDR device:")))
        top_row.addWidget(self.device_combo, 1)

        self.bandwidth_spin = QDoubleSpinBox()
        # Upper bound is a generous, round "covers the common consumer/
        # prosumer SDR range" figure (LimeSDR's own max sample rate is
        # 61.44 MSPS), not a hardware limit this app enforces - SoapyBackend
        # itself is generic and just passes whatever's set here straight to
        # the device (see AGENTS.md's hardware-compatibility notes). The
        # previous 3.2 MHz cap was an RTL-SDR-only leftover that silently
        # capped HackRF/LimeSDR/Airspy/SDRplay/USRP users below their
        # device's real capability for no reason - if someone's hardware
        # genuinely needs more than this, raise it further, it's not a
        # meaningful safety limit.
        self.bandwidth_spin.setRange(0.25, 61.44)
        self.bandwidth_spin.setDecimals(2)
        self.bandwidth_spin.setSuffix(tr(" MHz capture"))
        self.bandwidth_spin.setToolTip(tr(
            "Bandwidth captured by the SDR in one shot (e.g. 2.4 MHz for RTL-SDR,\n"
            "up to tens of MHz for HackRF/LimeSDR/Airspy/SDRplay/USRP).\n"
            "The scanner groups the whole range/channel list into as few such windows\n"
            "as possible instead of retuning channel by channel.\n"
            "Locked while the scanner is running - it needs a Stop first."
        ))
        self.bandwidth_spin.valueChanged.connect(self._push_settings)
        top_row.addWidget(self.bandwidth_spin)
        root.addLayout(top_row)

        # Bands (VFO) and the Memory List are always scanned together, in one
        # session - stacked one under the other (like a page to scroll)
        # rather than in a splitter: each section gets exactly as much room
        # as it actually needs, instead of having to be squeezed to make
        # room for its neighbor. The whole Listen tab lives in one
        # QScrollArea (see MainWindow), so scrolling down for more is always
        # available.
        root.addWidget(self._build_vfo_form())
        root.addWidget(self._build_operate_area())

    def _build_operate_area(self) -> QWidget:
        w = QWidget()
        root = QVBoxLayout(w)
        root.setContentsMargins(0, 0, 0, 0)

        timing_form = QFormLayout()
        self.tuning_slider = SliderSpin(0, 5000, 60, decimals=0, suffix="ms")
        self.tuning_slider.valueChanged.connect(self._push_settings)
        timing_form.addRow(tr("Tuning Time:"), self.tuning_slider)

        self.linger_slider = SliderSpin(0, 30000, 1500, decimals=0, suffix="ms")
        self.linger_slider.valueChanged.connect(self._push_settings)
        timing_form.addRow(tr("Linger Time:"), self.linger_slider)

        self.trigger_slider = SliderSpin(-90, 0, -30, decimals=0, suffix="dBFS")
        self.trigger_slider.valueChanged.connect(self._on_trigger_changed)
        timing_form.addRow(tr("Trigger Level:"), self.trigger_slider)

        timing_form.addRow(tr("Squelch (mute noise):"), self._build_squelch_row())
        timing_form.addRow(tr("Gain:"), self._build_gain_row())
        timing_form.addRow(tr("Noise reduction:"), self._build_noise_reduction_row())

        self.wfm_stereo_check = QCheckBox(tr("Stereo (FM WFM)"))
        self.wfm_stereo_check.setToolTip(tr(
            "Decodes stereo (19 kHz pilot) for live monitoring of FM broadcast\n"
            "radio (WFM) - has no effect on AM/NFM. Recordings always stay mono."
        ))
        self.wfm_stereo_check.toggled.connect(self._on_wfm_stereo_toggled)
        timing_form.addRow("", self.wfm_stereo_check)

        root.addLayout(timing_form)

        # Only visible while editing a band's own "advanced" settings (see
        # _BandRow.advanced_btn / _start_editing_band) - the sliders above
        # then control that one band (in amber, see BAND_OVERRIDE_ACCENT)
        # instead of the global settings.
        band_edit_row = QHBoxLayout()
        self.save_band_btn = QPushButton(tr("Save"))
        self.save_band_btn.setToolTip(tr(
            "Saves the current settings (trigger/timing/gain/noise reduction)\n"
            "as this band's own and returns to normal editing."
        ))
        self.save_band_btn.clicked.connect(self._on_save_band_clicked)
        self.reset_band_btn = QPushButton(tr("Reset to defaults"))
        self.reset_band_btn.setToolTip(tr(
            "Removes every advanced setting from this band (reverts to the\n"
            "global ones) and saves right away."
        ))
        self.reset_band_btn.clicked.connect(self._on_reset_band_clicked)
        band_edit_row.addWidget(self.save_band_btn)
        band_edit_row.addWidget(self.reset_band_btn)
        self.save_band_btn.setVisible(False)
        self.reset_band_btn.setVisible(False)
        root.addLayout(band_edit_row)

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
        # Simultaneously active channels besides this first one (see _Slot in
        # scanner_engine.py - one capture window can service any number of
        # transmissions at once, no hard limit) - we show up to 2 more here,
        # and past 3 total the last row gets a "(+N more)" suffix instead of
        # piling on more rows forever. Both hidden until there's something to show.
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
        self.start_stop_btn = QPushButton(tr("START"))
        self.start_stop_btn.setCheckable(True)
        self.start_stop_btn.setMinimumHeight(40)
        self.start_stop_btn.clicked.connect(self._on_start_stop)
        self.skip_btn = QPushButton(tr("SKIP"))
        self.skip_btn.setMinimumHeight(40)
        self.skip_btn.setEnabled(False)
        self.skip_btn.setToolTip(tr(
            "Moves on to the next frequency/channel immediately, without\n"
            "waiting for the transmission/linger to end. Does NOT add it to\n"
            "the blacklist - this frequency will be visited again on the\n"
            "next scan pass. Use this when something interesting is\n"
            "happening on the current channel and you want to leave it alone\n"
            "for later, but move on."
        ))
        self.skip_btn.clicked.connect(self.skip_requested.emit)
        self.skip_blacklist_btn = QPushButton(tr("SKIP + BLACKLIST"))
        self.skip_blacklist_btn.setMinimumHeight(40)
        self.skip_blacklist_btn.setEnabled(False)
        self.skip_blacklist_btn.setToolTip(tr(
            "Like SKIP, but also permanently blocks the current frequency\n"
            "(adds it to the blacklist) - the scanner won't visit it again.\n"
            "Takes effect immediately, with no confirmation; undo it by\n"
            "deleting the entry in the Blacklist tab."
        ))
        self.skip_blacklist_btn.clicked.connect(self.skip_blacklist_requested.emit)
        self.mute_btn = QPushButton(tr("MUTE"))
        self.mute_btn.setCheckable(True)
        self.mute_btn.setMinimumHeight(40)
        self.mute_btn.setToolTip(tr(
            "Mutes only the live audio in your headphones/speakers -\n"
            "recording to disk (if enabled) keeps running in the background,\n"
            "so you can catch up later by listening to recordings from the\n"
            "Recordings tab while the scanner keeps working quietly."
        ))
        self.mute_btn.toggled.connect(self._on_mute_toggled)
        btn_row.addWidget(self.start_stop_btn, 2)
        btn_row.addWidget(self.skip_btn, 1)
        btn_row.addWidget(self.skip_blacklist_btn, 1)
        btn_row.addWidget(self.mute_btn, 1)
        root.addLayout(btn_row)

        record_row = QHBoxLayout()
        self.manual_record_btn = QPushButton(tr("● Record"))
        self.manual_record_btn.setCheckable(True)
        self.manual_record_btn.setMinimumHeight(36)
        self.manual_record_btn.setEnabled(False)  # only while the scanner is running - see set_running()
        self.manual_record_btn.setToolTip(tr(
            "Continuously records everything audible on the current live\n"
            "channel - silence included - independent of whether the scanner\n"
            "currently considers a signal detected. Starts a new file now,\n"
            "stops it when you click again (or when the scanner itself stops)."
        ))
        self.manual_record_btn.toggled.connect(self._on_manual_record_toggled)
        record_row.addWidget(self.manual_record_btn, 2)

        self.auto_recording_check = QCheckBox(tr("Auto-recording"))
        self.auto_recording_check.setToolTip(tr(
            "Automatically records every transmission the scanner detects -\n"
            "the same setting as in the Options tab. Off by default."
        ))
        self.auto_recording_check.toggled.connect(self._on_auto_recording_toggled)
        record_row.addWidget(self.auto_recording_check, 1)

        # Right of Auto-recording, roughly under MUTE (the rightmost button
        # in btn_row above) - moved here from its own dedicated row by
        # explicit request, so it reads as "another live scanning control
        # next to Record", not a separate section.
        self.raw_iq_btn = QPushButton(tr("Record raw IQ"))
        self.raw_iq_btn.setCheckable(True)
        self.raw_iq_btn.setEnabled(False)  # only while the scanner is running - see set_running()
        self.raw_iq_btn.setToolTip(tr(
            "Advanced/technical: records the actual wideband complex samples "
            "this app demodulates from, in the SigMF format (readable by GNU "
            "Radio, Inspectrum, SDR++, ...) - independent of any specific "
            "channel or trigger. Saved to a 'raw_iq' subfolder, separate from "
            "the Playback library. Warning: uncompressed IQ is large - "
            "roughly sample_rate x 8 bytes/second (~19 MB/s at this app's "
            "default 2.4 MSPS capture bandwidth)."
        ))
        self.raw_iq_btn.toggled.connect(self.raw_iq_record_toggled.emit)
        record_row.addWidget(self.raw_iq_btn, 1)
        root.addLayout(record_row)
        return w

    def _build_gain_row(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        self.gain_mode_combo = QComboBox()
        self.gain_mode_combo.addItem(tr("Manual"), "manual")
        self.gain_mode_combo.addItem(tr("Auto (hardware AGC)"), "auto")
        self.gain_mode_combo.addItem(tr("Auto (software AGC)"), "sw_auto")
        self.gain_mode_combo.setToolTip(tr(
            "Hardware AGC: the tuner's own onboard AGC - fast, but can\n"
            "overdrive on a strong station (measured: +0.7dBFS, past full\n"
            "scale, with spurious extra detections on adjacent channels from\n"
            "the resulting spectral leakage).\n\n"
            "Software AGC: a slower, cleaner control loop driven from this\n"
            "app's own measurement of the captured signal's peak - never\n"
            "overdrives, but reacts slower and settles at a less aggressive\n"
            "gain, so it can miss some marginal/edge-of-band signals hardware\n"
            "AGC would have pushed over the trigger. Try both."
        ))
        self.gain_mode_combo.currentIndexChanged.connect(self._on_gain_mode_changed)
        lo, hi = DEFAULT_GAIN_RANGE_DB
        self.gain_slider = SliderSpin(lo, hi, 30.0, decimals=1, suffix="dB")
        self.gain_slider.valueChanged.connect(self._on_gain_changed)
        lay.addWidget(self.gain_mode_combo)
        lay.addWidget(self.gain_slider, 1)
        return w

    def _build_squelch_row(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        self.squelch_check = QCheckBox(tr("Enable"))
        self.squelch_check.setToolTip(tr(
            "Mutes the live audio AND skips writing to the recording, once the\n"
            "level drops at least this offset BELOW Trigger Level - only\n"
            "during LINGER (after the transmission ends, before the channel\n"
            "closes). Fixes the typical \"ear-blasting\" burst after releasing\n"
            "PTT on NFM: once the carrier disappears, the FM demodulator gets\n"
            "pure receiver noise, and noise without a carrier sounds very\n"
            "loud despite low RF power (the FM threshold effect) - this isn't\n"
            "a bug. Thanks to this, a recording ends where the signal actually\n"
            "ended, without a loud noise \"tail\". The offset always stays\n"
            "below the trigger (never above it), so it can never mute/cut\n"
            "audio while the scanner considers the channel active\n"
            "(SIGNAL_DETECTED). Leave a margin (e.g. -6 dB) so whatever's\n"
            "still audible/recorded during LINGER below the trigger (thanks\n"
            "to it) isn't cut off immediately."
        ))
        self.squelch_check.toggled.connect(self._on_squelch_toggled)
        self.squelch_offset_slider = SliderSpin(-40, 0, -6, decimals=0, suffix=tr("dB from trigger"))
        self.squelch_offset_slider.valueChanged.connect(self._on_squelch_offset_changed)
        lay.addWidget(self.squelch_check)
        lay.addWidget(self.squelch_offset_slider, 1)
        return w

    def _build_noise_reduction_row(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        self.nr_check = QCheckBox(tr("Enable (adaptive, intelligent)"))
        self.nr_check.setToolTip(tr(
            "Continuously tracks the background noise level in the audio\n"
            "spectrum and attenuates bands close to that level (spectral\n"
            "gating), leaving speech bands relatively untouched. Helps with a\n"
            "weak antenna/low SNR, but at very high noise levels costs a\n"
            "slightly \"digital\" sound."
        ))
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

        hint = QLabel(tr(
            "Sources to scan - add presets, custom ranges (with your own "
            "start/stop/step) or single frequencies, e.g. 121.500 MHz "
            "alongside the scanned band. Every entry enabled below is "
            "scanned together, in one session, along with every enabled "
            "channel from the Memory List below - no need to switch between them."
        ))
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

        # The Memory List sits here, right below the selected bands/channels
        # and above "Add preset" - it's just as much a scan "source" as any
        # band above, so it logically belongs in the same list, not at the
        # end past the add-forms.
        self.memory_list_row = _MemoryListRow(
            self.config,
            on_master_toggled=self.settings_changed.emit,
            on_weight_changed=self.channel_weight_changed.emit,
        )
        root.addWidget(self.memory_list_row)

        preset_row = QHBoxLayout()
        self.preset_combo = QComboBox()
        self.reload_preset_combo()
        add_preset_btn = QPushButton(tr("Add preset"))
        add_preset_btn.clicked.connect(self._on_add_preset)
        preset_row.addWidget(self.preset_combo, 1)
        preset_row.addWidget(add_preset_btn)
        add_custom_btn = QPushButton(tr("Add custom range"))
        add_custom_btn.clicked.connect(self._on_add_custom_range)
        preset_row.addWidget(add_custom_btn)
        memory_list_btn = QPushButton(tr("Memory list (channels)"))
        memory_list_btn.setToolTip(tr(
            "Switch to the Channels tab, where individual\n"
            "frequencies/folders can be managed. They're always scanned\n"
            "together with the bands above, in one session."
        ))
        memory_list_btn.clicked.connect(self.memory_list_requested.emit)
        preset_row.addWidget(memory_list_btn)
        root.addLayout(preset_row)

        freq_row = QHBoxLayout()
        freq_row.addWidget(QLabel(tr("Quick-add a frequency:")))
        self.quick_freq_spin = QDoubleSpinBox()
        self.quick_freq_spin.setRange(0.01, 6000.0)
        self.quick_freq_spin.setDecimals(4)
        self.quick_freq_spin.setSuffix(" MHz")
        self.quick_freq_spin.setValue(121.500)
        freq_row.addWidget(self.quick_freq_spin)
        add_freq_btn = QPushButton(tr("Add"))
        add_freq_btn.setToolTip(tr(
            "Adds a single channel to the list, e.g. 121.500 MHz (guard)\n"
            "alongside the scanned band."
        ))
        add_freq_btn.clicked.connect(self._on_add_frequency)
        freq_row.addWidget(add_freq_btn)
        freq_row.addStretch(1)
        root.addLayout(freq_row)

        return w

    def _populate_devices(self) -> None:
        self.device_combo.addItem(tr("Automatic (first detected)"), ("auto", ""))
        for dev in enumerate_devices():
            label = dev.get("label") or dev.get("driver", "device")
            args = ",".join(f"{k}={v}" for k, v in dev.items())
            self.device_combo.addItem(label, (dev.get("driver", "auto"), args))
        # "Simulation" is deliberately hidden from real users by default (see
        # AppConfig.debug_mode's own comment) - a real RTL-SDR owner
        # shouldn't be able to accidentally select it and wonder why the
        # scanner is hearing fake bursts instead of their actual hardware.
        if self.config.debug_mode:
            self.device_combo.addItem(tr("Simulation (no hardware)"), ("simulated", ""))

    def refresh_device_list(self) -> None:
        """Re-populates the device dropdown after `config.debug_mode`
        changes in Options (MainWindow wires this to the checkbox there) -
        preserves the current selection if it's still in the list, falls
        back to "Automatic" if not (e.g. debug mode just turned off while
        "Simulation" was selected)."""
        current = self.device_combo.currentData()
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        self._populate_devices()
        matched = False
        for i in range(self.device_combo.count()):
            if self.device_combo.itemData(i) == current:
                self.device_combo.setCurrentIndex(i)
                matched = True
                break
        if not matched:
            self.device_combo.setCurrentIndex(0)
        self.device_combo.blockSignals(False)

    def _load_from_config(self) -> None:
        self.bandwidth_spin.setValue(self.config.device.sample_rate_hz / 1e6)
        self._load_global_sliders()
        self.wfm_stereo_check.setChecked(self.config.wfm_stereo_enabled)
        self.auto_recording_check.setChecked(self.config.recording.enabled)
        # Global-only, unlike trigger_slider above - never swapped for a
        # per-band value during "advanced" band editing (see
        # _band_edit_sliders()), so it's loaded here rather than in
        # _load_global_sliders() (which _stop_editing_band() also calls).
        self.squelch_check.setChecked(self.config.squelch_enabled)
        self.squelch_offset_slider.set_value(self.config.squelch_offset_db)
        self.squelch_offset_slider.set_enabled_all(self.config.squelch_enabled)
        target = (self.config.device.driver, self.config.device.device_args)
        for i in range(self.device_combo.count()):
            if self.device_combo.itemData(i) == target:
                self.device_combo.setCurrentIndex(i)
                break
        self._rebuild_band_rows()

    def _load_global_sliders(self) -> None:
        """The Trigger/Tuning/Linger/Gain/Noise-reduction controls, set from
        the global Scanner-tab config - shared by _load_from_config() and
        _stop_editing_band() (which needs to put them back to this after
        showing one band's own override values while editing it)."""
        self.tuning_slider.set_value(self.config.timing.tuning_time_ms)
        self.linger_slider.set_value(self.config.timing.linger_time_ms)
        self.trigger_slider.set_value(self.config.trigger_level_dbfs)
        self.level_meter.set_trigger(self.config.trigger_level_dbfs)
        self._set_gain_mode_combo(self.config.device.gain_mode)
        self.gain_slider.set_value(self.config.device.gain_db)
        self.gain_slider.set_enabled_all(self.config.device.gain_mode == "manual")
        self.nr_check.setChecked(self.config.noise_reduction.enabled)
        self.nr_strength_slider.set_value(round(self.config.noise_reduction.strength * 100))
        self.nr_strength_slider.set_enabled_all(self.config.noise_reduction.enabled)

    def _rebuild_band_rows(self) -> None:
        while self.bands_layout.count():
            item = self.bands_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._band_rows = []
        for entry in self.config.bands:
            row = _BandRow(
                entry, on_change=self._on_band_changed, on_remove=self._on_band_removed,
                on_advanced=self._on_advanced_clicked,
            )
            self.bands_layout.addWidget(row)
            self._band_rows.append(row)
        self.bands_layout.addStretch(1)
        if self._editing_band is not None:
            # Rows were just rebuilt from scratch - _editing_row (if any)
            # points at a now-deleted widget, even though the underlying
            # VfoRangeConfig (self._editing_band) is unaffected by a
            # rebuild. Re-resolve by identity (not `in`/`==` -
            # VfoRangeConfig's dataclass equality compares *values*, so two
            # bands with identical settings would otherwise look
            # interchangeable) to the fresh row for that same band, or - if
            # it's simply gone (e.g. "Remove") - stop editing cleanly.
            self._editing_row = next((r for r in self._band_rows if r.entry is self._editing_band), None)
            if self._editing_row is None:
                self._stop_editing_band()
                return
        self._update_advanced_buttons()

    def _on_band_changed(self) -> None:
        if self._loading:
            return
        self.settings_changed.emit()

    def _on_band_removed(self, row: "_BandRow") -> None:
        self.config.bands = [b for b in self.config.bands if b is not row.entry]
        self._rebuild_band_rows()
        self.settings_changed.emit()

    # -- per-band "advanced" edit mode -----------------------------------
    def _update_advanced_buttons(self) -> None:
        for r in self._band_rows:
            r.advanced_btn.setChecked(r is self._editing_row)

    def _band_edit_sliders(self) -> tuple[SliderSpin, ...]:
        return (self.trigger_slider, self.tuning_slider, self.linger_slider,
                self.gain_slider, self.nr_strength_slider)

    def _on_advanced_clicked(self, row: "_BandRow") -> None:
        self._start_editing_band(row)

    def _start_editing_band(self, row: "_BandRow") -> None:
        if self._editing_row is row:
            return
        self._editing_row = row
        self._editing_band = band = row.entry

        self._loading = True
        self.trigger_slider.set_value(
            band.trigger_level_dbfs if band.trigger_level_dbfs is not None else self.config.trigger_level_dbfs
        )
        self.level_meter.set_trigger(self.trigger_slider.value())
        self.tuning_slider.set_value(
            band.tuning_time_ms if band.tuning_time_ms is not None else self.config.timing.tuning_time_ms
        )
        self.linger_slider.set_value(
            band.linger_time_ms if band.linger_time_ms is not None else self.config.timing.linger_time_ms
        )
        gain_mode = band.gain_mode if band.gain_mode is not None else self.config.device.gain_mode
        gain_db = band.gain_db if band.gain_db is not None else self.config.device.gain_db
        self._set_gain_mode_combo(gain_mode)
        self.gain_slider.set_value(gain_db)
        self.gain_slider.set_enabled_all(gain_mode == "manual")
        nr_enabled = (
            band.noise_reduction_enabled if band.noise_reduction_enabled is not None
            else self.config.noise_reduction.enabled
        )
        nr_strength = (
            band.noise_reduction_strength if band.noise_reduction_strength is not None
            else self.config.noise_reduction.strength
        )
        self.nr_check.setChecked(nr_enabled)
        self.nr_strength_slider.set_value(round(nr_strength * 100))
        self.nr_strength_slider.set_enabled_all(nr_enabled)
        self._loading = False

        for w in self._band_edit_sliders():
            w.set_accent(BAND_OVERRIDE_ACCENT)
        self.save_band_btn.setVisible(True)
        self.reset_band_btn.setVisible(True)
        self._update_advanced_buttons()
        self.solo_band_changed.emit(band)

    def _stop_editing_band(self) -> None:
        self._editing_row = None
        self._editing_band = None

        self._loading = True
        self._load_global_sliders()
        self._loading = False

        for w in self._band_edit_sliders():
            w.set_accent(None)
        self.save_band_btn.setVisible(False)
        self.reset_band_btn.setVisible(False)
        self._update_advanced_buttons()
        self.solo_band_changed.emit(None)

    def _on_save_band_clicked(self) -> None:
        band = self._editing_band
        if band is None:
            return
        # Explicit, unconditional write (not just relying on whatever the
        # per-tick slider handlers already wrote while dragging) - clicking
        # Save always "pins" exactly what's currently shown, even if the
        # user never actually moved anything after opening this band.
        band.trigger_level_dbfs = self.trigger_slider.value()
        band.tuning_time_ms = int(self.tuning_slider.value())
        band.linger_time_ms = int(self.linger_slider.value())
        band.gain_mode = self.gain_mode_combo.currentData()
        band.gain_db = self.gain_slider.value()
        band.noise_reduction_enabled = self.nr_check.isChecked()
        band.noise_reduction_strength = self.nr_strength_slider.value() / 100.0
        self._stop_editing_band()
        self.settings_changed.emit()

    def _on_reset_band_clicked(self) -> None:
        band = self._editing_band
        if band is None:
            return
        band.trigger_level_dbfs = None
        band.tuning_time_ms = None
        band.linger_time_ms = None
        band.gain_mode = None
        band.gain_db = None
        band.noise_reduction_enabled = None
        band.noise_reduction_strength = None
        self._stop_editing_band()
        self.settings_changed.emit()

    def reload_preset_combo(self) -> None:
        """(Re)populates the preset dropdown from get_all_presets() - called
        at construction and again whenever the Options preset editor saves a
        change (see MainWindow), so an added/edited/deleted user preset
        shows up here immediately, no restart needed."""
        current_id = self.preset_combo.currentData() if self.preset_combo.count() else None
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        for preset in get_all_presets():
            self.preset_combo.addItem(tr(preset.label), preset.id)
            self.preset_combo.setItemData(
                self.preset_combo.count() - 1, tr(preset.tooltip), Qt.ItemDataRole.ToolTipRole
            )
        if current_id is not None:
            idx = self.preset_combo.findData(current_id)  # plain str id - findData() is fine here (gotcha 2 is about tuple/object payloads)
            if idx >= 0:
                self.preset_combo.setCurrentIndex(idx)
        self.preset_combo.blockSignals(False)

    def _on_add_preset(self) -> None:
        preset_id = self.preset_combo.currentData()
        preset = next((p for p in get_all_presets() if p.id == preset_id), None)
        if preset is None:
            return
        band = preset.to_band()
        band.label = tr(preset.label)  # translated, not the preset's raw English source
        self.config.bands.append(band)
        self._rebuild_band_rows()
        self.settings_changed.emit()

    def _on_add_custom_range(self) -> None:
        self.config.bands.append(VfoRangeConfig(
            start_hz=118_000_000, stop_hz=118_500_000, step_hz=25_000, label=tr("Custom range"),
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
        if self._editing_band is not None:
            self._editing_band.trigger_level_dbfs = value
        else:
            self.config.trigger_level_dbfs = value
        self.settings_changed.emit()

    def _set_gain_mode_combo(self, mode: str) -> None:
        idx = self.gain_mode_combo.findData(mode)
        self.gain_mode_combo.setCurrentIndex(idx if idx >= 0 else 0)

    def _on_gain_mode_changed(self, _index: int) -> None:
        mode = self.gain_mode_combo.currentData()
        self.gain_slider.set_enabled_all(mode == "manual")
        if self._loading:
            return
        if self._editing_band is not None:
            self._editing_band.gain_mode = mode
        else:
            self.config.device.gain_mode = mode
        self.settings_changed.emit()

    def _on_gain_changed(self, value: float) -> None:
        if self._loading:
            return
        if self._editing_band is not None:
            self._editing_band.gain_db = value
        else:
            self.config.device.gain_db = value
        self.settings_changed.emit()

    def _on_nr_toggled(self, checked: bool) -> None:
        self.nr_strength_slider.set_enabled_all(checked)
        if self._loading:
            return
        if self._editing_band is not None:
            self._editing_band.noise_reduction_enabled = checked
        else:
            self.config.noise_reduction.enabled = checked
        self.settings_changed.emit()

    def _on_nr_strength_changed(self, value: float) -> None:
        if self._loading:
            return
        if self._editing_band is not None:
            self._editing_band.noise_reduction_strength = value / 100.0
        else:
            self.config.noise_reduction.strength = value / 100.0
        self.settings_changed.emit()

    def _on_wfm_stereo_toggled(self, checked: bool) -> None:
        if self._loading:
            return
        self.config.wfm_stereo_enabled = checked
        self.settings_changed.emit()

    def _on_squelch_toggled(self, checked: bool) -> None:
        # Global-only (see squelch_check in _load_from_config) - no
        # self._editing_band branch, unlike e.g. _on_nr_toggled.
        self.squelch_offset_slider.set_enabled_all(checked)
        if self._loading:
            return
        self.config.squelch_enabled = checked
        self.settings_changed.emit()

    def _on_squelch_offset_changed(self, value: float) -> None:
        if self._loading:
            return
        self.config.squelch_offset_db = value
        self.settings_changed.emit()

    def _push_settings(self, *_args) -> None:
        if self._loading:
            return
        self.config.device.sample_rate_hz = self.bandwidth_spin.value() * 1e6
        if self._editing_band is not None:
            self._editing_band.tuning_time_ms = int(self.tuning_slider.value())
            self._editing_band.linger_time_ms = int(self.linger_slider.value())
        else:
            self.config.timing.tuning_time_ms = int(self.tuning_slider.value())
            self.config.timing.linger_time_ms = int(self.linger_slider.value())
        driver, args = self.device_combo.currentData() or ("auto", "")
        self.config.device.driver = driver
        self.config.device.device_args = args
        self.settings_changed.emit()

    # -- called by MainWindow ------------------------------------------------
    def set_running(self, running: bool) -> None:
        self.start_stop_btn.setChecked(running)
        self.start_stop_btn.setText(tr("STOP") if running else tr("START"))
        self.skip_btn.setEnabled(running)
        self.skip_blacklist_btn.setEnabled(running)
        self.device_combo.setEnabled(not running)
        self.bandwidth_spin.setEnabled(not running)
        self.manual_record_btn.setEnabled(running)
        self.raw_iq_btn.setEnabled(running)
        if not running:
            self.cycle_time_label.setText("")
            # A manual recording can't outlive the scanner that feeds it -
            # ScannerEngine._teardown() already stops it on its own side,
            # this just makes sure the button reflects that instead of
            # staying stuck showing "Stop recording" for a scanner that no
            # longer exists.
            self.set_manual_recording_active(False)
            self.set_raw_iq_recording_active(False)

    def set_manual_recording_active(self, active: bool) -> None:
        """Syncs the Record button's checked state/label without re-emitting
        manual_record_toggled - called both from set_running() above and
        from MainWindow in response to ScannerEngine's own
        manual_recording_changed signal (e.g. a recording that was stopped
        from the engine side, not by clicking this button)."""
        self.manual_record_btn.blockSignals(True)
        self.manual_record_btn.setChecked(active)
        self.manual_record_btn.blockSignals(False)
        self.manual_record_btn.setText(tr("■ Stop recording") if active else tr("● Record"))

    def set_raw_iq_recording_active(self, active: bool) -> None:
        self.raw_iq_btn.blockSignals(True)
        self.raw_iq_btn.setChecked(active)
        self.raw_iq_btn.blockSignals(False)
        self.raw_iq_btn.setText(tr("■ Stop recording IQ") if active else tr("Record raw IQ"))

    def set_auto_recording(self, enabled: bool) -> None:
        """Syncs state after the Auto-recording toggle changes in the Options tab."""
        self.auto_recording_check.blockSignals(True)
        self.auto_recording_check.setChecked(enabled)
        self.auto_recording_check.blockSignals(False)

    def _on_manual_record_toggled(self, checked: bool) -> None:
        self.manual_record_btn.setText(tr("■ Stop recording") if checked else tr("● Record"))
        self.manual_record_toggled.emit(checked)

    def _on_auto_recording_toggled(self, checked: bool) -> None:
        if self._loading:
            return
        self.auto_recording_toggled.emit(checked)

    def _on_mute_toggled(self, checked: bool) -> None:
        self.mute_btn.setText(tr("MUTED") if checked else tr("MUTE"))
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
        """Updates the displayed frequency/frequencies. ``entries`` is a
        list of (freq_hz, name) - one entry per currently active channel (no
        hard limit on how many at once - see _Slot in scanner_engine.py); the
        first entry is always whichever one is currently audible live. The
        display shows at most 3 rows - past 3 active channels the last row
        gets a "(+N more)" suffix instead of piling on more rows forever. If
        the name is known (e.g. from the Memory List), we show it in a
        large, bold font - more readable than just the number - with the
        frequency in smaller, secondary text next to/below it."""
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
            suffix = (
                "  " + tr("(+{count} more)").format(count=overflow)
                if idx == len(secondary_rows) - 1 and overflow > 0 else ""
            )
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

    def update_spectrum(self, freqs_hz, db, trigger_dbfs: float, custom: bool = False) -> None:
        self.spectrum_view.update_spectrum(freqs_hz, db, trigger_dbfs, custom)

    def update_cycle_time(self, seconds: float) -> None:
        """Real, measured interval since this same capture window was last
        tuned to - see ScannerSignals.cycle_time_changed."""
        self.cycle_time_label.setText(tr("Back to this window: {seconds:.2f}s ago").format(seconds=seconds))

    def update_gain_range(self, lo: float, hi: float) -> None:
        self.gain_slider.set_range(lo, hi)
