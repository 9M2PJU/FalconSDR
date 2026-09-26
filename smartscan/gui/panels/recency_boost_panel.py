"""Recency boost: kontrolki dla RecencyBoostConfig (patrz config.py oraz
ScannerEngine._due_recency_boost_locked/_register_recency_boost) - po
naturalnym zakonczeniu transmisji jej okno jest przez chwile odwiedzane
czesciej niz zwykle, w razie gdyby za kilkanascie/kilkadziesiat sekund byl
kolejny komunikat na tej samej czestotliwosci (typowe w ATC). W odroznieniu
od zwyklego wydluzania linger_time_ms, to nigdy nie blokuje wykrycia
przejscia rozmowy na inna czestotliwosc - skaner cala reszte czasu dalej
normalnie krazy po innych kanalach.
"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QCheckBox, QDoubleSpinBox, QFormLayout, QGroupBox, QLabel, QVBoxLayout

from ...config import RecencyBoostConfig


class RecencyBoostPanel(QGroupBox):
    settings_changed = Signal()

    def __init__(self, config: RecencyBoostConfig, parent=None):
        super().__init__("Recency boost (czestsze powroty po transmisji)", parent)
        self.config = config
        self._loading = True
        self._build_ui()
        self._load_from_config()
        self._loading = False

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        hint = QLabel(
            "Po naturalnym zakonczeniu transmisji (nie po recznym Skip) jej okno "
            "jest przez chwile odwiedzane czesciej niz zwykle - w razie gdyby za "
            "kilkanascie/kilkadziesiat sekund pojawil sie kolejny komunikat na tej "
            "samej czestotliwosci. Skaner nadal normalnie krazy po innych kanalach "
            "przez caly ten czas, wiec przejscie rozmowy na inna czestotliwosc "
            "wciaz zostanie wykryte - w odroznieniu od zwyklego wydluzania czasu lingera."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #9aa0a6;")
        root.addWidget(hint)

        self.enable_check = QCheckBox("Wlacz recency boost")
        self.enable_check.toggled.connect(self._push_settings)
        root.addWidget(self.enable_check)

        form = QFormLayout()
        self.duration_spin = QDoubleSpinBox()
        self.duration_spin.setRange(5.0, 600.0)
        self.duration_spin.setDecimals(0)
        self.duration_spin.setSuffix(" s")
        self.duration_spin.setSingleStep(5.0)
        self.duration_spin.valueChanged.connect(self._push_settings)
        form.addRow("Jak dlugo boostowac po transmisji:", self.duration_spin)

        self.interval_spin = QDoubleSpinBox()
        self.interval_spin.setRange(1.0, 120.0)
        self.interval_spin.setDecimals(0)
        self.interval_spin.setSuffix(" s")
        self.interval_spin.setSingleStep(1.0)
        self.interval_spin.valueChanged.connect(self._push_settings)
        form.addRow("Co ile odwiedzac w trakcie boosta:", self.interval_spin)

        root.addLayout(form)

    def _load_from_config(self) -> None:
        self.enable_check.setChecked(self.config.enabled)
        self.duration_spin.setValue(self.config.duration_s)
        self.interval_spin.setValue(self.config.interval_s)
        self._sync_enabled_state()

    def _sync_enabled_state(self) -> None:
        enabled = self.enable_check.isChecked()
        self.duration_spin.setEnabled(enabled)
        self.interval_spin.setEnabled(enabled)

    def _push_settings(self, *_args) -> None:
        if self._loading:
            return
        self.config.enabled = self.enable_check.isChecked()
        self.config.duration_s = self.duration_spin.value()
        self.config.interval_s = self.interval_spin.value()
        self._sync_enabled_state()
        self.settings_changed.emit()
