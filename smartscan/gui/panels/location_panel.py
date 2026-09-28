"""Receiver location settings - used only to build the FlightRadar24/
ADSBExchange playback links in Odtwarzacz (see LocationConfig). Deliberately
its own small panel rather than a couple of stray fields bolted onto another
one - PlaybackPanel needs a place to send the user when they click FR24/
ADSBExchange with no location set yet (see MainWindow._reveal_location), and
that only works if there's a single, findable panel to land on.
"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QDoubleSpinBox, QFormLayout, QGroupBox, QLabel, QVBoxLayout

from ...config import LocationConfig
from ...i18n import tr

# Sentinel values one notch below/above the real valid range, shown via
# QDoubleSpinBox.specialValueText instead of a separate "enabled" checkbox -
# a spin box's minimum already has to be some concrete number, so re-using it
# as the "not set" marker needs no extra widget/state to keep in sync.
_LAT_UNSET = -91.0
_LON_UNSET = -181.0


class LocationPanel(QGroupBox):
    settings_changed = Signal()

    def __init__(self, config: LocationConfig, parent=None):
        super().__init__(tr("Location"), parent)
        self.config = config
        self._loading = True
        self._build_ui()
        self._load_from_config()
        self._loading = False

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        hint = QLabel(tr(
            "Approximate coordinates of the receiver - used only to build "
            "the FlightRadar24/ADSBExchange playback links in Playback "
            "(“what was flying near here at this recording's time”). "
            "Nothing is sent anywhere else; this never leaves your machine "
            "except when you click one of those two buttons, which opens "
            "your browser at that flight-tracker site. A nearby town/city is "
            "plenty precise - you don't need your exact address."
        ))
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #9aa0a6;")
        root.addWidget(hint)

        form = QFormLayout()
        self.lat_spin = QDoubleSpinBox()
        self.lat_spin.setRange(_LAT_UNSET, 90.0)
        self.lat_spin.setDecimals(4)
        self.lat_spin.setSpecialValueText(tr("(not set)"))
        self.lat_spin.valueChanged.connect(self._push_settings)
        form.addRow(tr("Latitude:"), self.lat_spin)

        self.lon_spin = QDoubleSpinBox()
        self.lon_spin.setRange(_LON_UNSET, 180.0)
        self.lon_spin.setDecimals(4)
        self.lon_spin.setSpecialValueText(tr("(not set)"))
        self.lon_spin.valueChanged.connect(self._push_settings)
        form.addRow(tr("Longitude:"), self.lon_spin)

        root.addLayout(form)

    def _load_from_config(self) -> None:
        self.lat_spin.setValue(self.config.home_lat if self.config.home_lat is not None else _LAT_UNSET)
        self.lon_spin.setValue(self.config.home_lon if self.config.home_lon is not None else _LON_UNSET)

    def _push_settings(self, *_args) -> None:
        if self._loading:
            return
        self.config.home_lat = None if self.lat_spin.value() == _LAT_UNSET else self.lat_spin.value()
        self.config.home_lon = None if self.lon_spin.value() == _LON_UNSET else self.lon_spin.value()
        self.settings_changed.emit()

    def focus_input(self) -> None:
        """Called when PlaybackPanel redirects here after a FR24/ADSBExchange
        click with no location set - puts the cursor where the user actually
        needs to act instead of just switching tabs and leaving them to find
        it themselves."""
        self.lat_spin.setFocus()
        self.lat_spin.selectAll()
