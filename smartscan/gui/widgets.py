"""Small reusable custom-painted widgets for the SmartScanATC GUI."""
from __future__ import annotations

import numpy as np
from PyQt6.QtCore import Qt, QRectF, pyqtSignal
from PyQt6.QtGui import QColor, QLinearGradient, QPainter, QPen
from PyQt6.QtWidgets import (
    QDoubleSpinBox,
    QHBoxLayout,
    QSlider,
    QSpinBox,
    QStyle,
    QStyleOptionSlider,
    QWidget,
)

try:
    import pyqtgraph as pg
    HAVE_PYQTGRAPH = True
except ImportError:
    HAVE_PYQTGRAPH = False


class ClickToSeekSlider(QSlider):
    """QSlider gdzie kliknięcie w dowolne miejsce paska (nie tylko przeciąganie
    uchwytu) od razu przeskakuje do tej pozycji - tak jak w typowych
    odtwarzaczach internetowych (YouTube itp.), zamiast domyślnego zachowania
    Qt (krok strony po kliknięciu obok uchwytu)."""

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            opt = QStyleOptionSlider()
            self.initStyleOption(opt)
            groove = self.style().subControlRect(
                QStyle.ComplexControl.CC_Slider, opt, QStyle.SubControl.SC_SliderGroove, self
            )
            handle = self.style().subControlRect(
                QStyle.ComplexControl.CC_Slider, opt, QStyle.SubControl.SC_SliderHandle, self
            )
            pos = int(event.position().x()) if hasattr(event, "position") else event.x()
            value = QStyle.sliderValueFromPosition(
                self.minimum(), self.maximum(), pos - groove.x(), groove.width() - handle.width()
            )
            self.setValue(value)
            self.sliderMoved.emit(value)
            event.accept()
            return
        super().mousePressEvent(event)


class LevelMeter(QWidget):
    """Horizontal dBFS bar with a marker line for the trigger threshold."""

    def __init__(self, min_db: float = -80.0, max_db: float = 0.0, parent=None):
        super().__init__(parent)
        self.min_db = min_db
        self.max_db = max_db
        self._level = min_db
        self._trigger = -30.0
        self.setMinimumHeight(34)

    def set_level(self, db: float) -> None:
        self._level = max(self.min_db, min(self.max_db, db))
        self.update()

    def set_trigger(self, db: float) -> None:
        self._trigger = max(self.min_db, min(self.max_db, db))
        self.update()

    def _x_for(self, db: float, width: float) -> float:
        frac = (db - self.min_db) / (self.max_db - self.min_db)
        return frac * width

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(0, 0, self.width(), self.height())

        painter.fillRect(rect, QColor("#12141a"))

        level_w = self._x_for(self._level, rect.width())
        grad = QLinearGradient(0, 0, rect.width(), 0)
        grad.setColorAt(0.0, QColor("#2ecc71"))
        grad.setColorAt(0.65, QColor("#f1c40f"))
        grad.setColorAt(1.0, QColor("#e74c3c"))
        painter.fillRect(QRectF(0, 0, level_w, rect.height()), grad)

        trigger_x = self._x_for(self._trigger, rect.width())
        pen = QPen(QColor("#ff3b30"), 2)
        painter.setPen(pen)
        painter.drawLine(int(trigger_x), 0, int(trigger_x), int(rect.height()))

        painter.setPen(QColor("#e0e0e0"))
        painter.drawText(rect.adjusted(6, 0, -6, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                          f"{self._level:.1f} dBFS")
        painter.drawRect(rect.adjusted(0, 0, -1, -1))


class SliderSpin(QWidget):
    """A QSlider tied to a spin box, both updating live as you drag (no need
    to release or press Enter) - used for Trigger Level/Squelch, Gain, Tuning
    Time and Linger Time so they're all easy to nudge by ear while scanning.
    """
    valueChanged = pyqtSignal(float)

    def __init__(self, minimum: float, maximum: float, value: float, decimals: int = 0,
                 suffix: str = "", parent=None):
        super().__init__(parent)
        self._scale = 10 ** decimals
        self._decimals = decimals
        self._updating = False

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(int(round(minimum * self._scale)), int(round(maximum * self._scale)))

        if decimals > 0:
            self.spin = QDoubleSpinBox()
            self.spin.setDecimals(decimals)
            self.spin.setRange(minimum, maximum)
        else:
            self.spin = QSpinBox()
            self.spin.setRange(int(round(minimum)), int(round(maximum)))
        self.spin.setSuffix(f" {suffix}" if suffix else "")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.slider, 1)
        layout.addWidget(self.spin)

        self.set_value(value)
        self.slider.valueChanged.connect(self._on_slider)
        self.spin.valueChanged.connect(self._on_spin)

    def _spin_value(self, value: float):
        return int(round(value)) if self._decimals == 0 else value

    def _on_slider(self, raw: int) -> None:
        if self._updating:
            return
        self._updating = True
        value = raw / self._scale
        self.spin.setValue(self._spin_value(value))
        self._updating = False
        self.valueChanged.emit(value)

    def _on_spin(self, value) -> None:
        if self._updating:
            return
        self._updating = True
        self.slider.setValue(int(round(float(value) * self._scale)))
        self._updating = False
        self.valueChanged.emit(float(value))

    def value(self) -> float:
        return float(self.spin.value())

    def set_value(self, value: float) -> None:
        """Set without emitting valueChanged - use during config load."""
        self._updating = True
        self.spin.setValue(self._spin_value(value))
        self.slider.setValue(int(round(value * self._scale)))
        self._updating = False

    def set_range(self, minimum: float, maximum: float) -> None:
        self.spin.setRange(minimum, maximum)
        self.slider.setRange(int(round(minimum * self._scale)), int(round(maximum * self._scale)))

    def set_enabled_all(self, enabled: bool) -> None:
        self.slider.setEnabled(enabled)
        self.spin.setEnabled(enabled)


_TRIGGER_HEADROOM_DB = 15.0  # clear space always kept above the trigger line - see update_spectrum()


class SpectrumView(QWidget):
    """Live mini-spectrum of the currently captured window, with a red line
    marking the trigger/squelch threshold so it's obvious why the scanner
    stopped (or didn't) on a given channel.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._plot = None
        self._curve = None
        self._trigger_line = None
        if HAVE_PYQTGRAPH:
            pg.setConfigOption("background", "#12141a")
            pg.setConfigOption("foreground", "#9aa0a6")
            self._plot = pg.PlotWidget()
            self._plot.setMinimumHeight(220)
            self._plot.showGrid(x=True, y=True, alpha=0.2)
            self._plot.setLabel("bottom", "MHz")
            self._plot.setLabel("left", "dB")
            # To jest czysty podglad, nie narzedzie do eksploracji widma -
            # przypadkowe przeciagniecie/scroll myszka potrafilo
            # przesunac/przybilzyc widok bez zadnego prostego sposobu na
            # powrot do domyslnego. Wylaczamy cala interakcje myszka
            # (przeciaganie, scroll-zoom, menu PPM) - wykres zawsze zostaje w
            # automatycznie dopasowanym widoku, tak jak przy starcie programu.
            self._plot.getPlotItem().getViewBox().setMouseEnabled(x=False, y=False)
            self._plot.setMenuEnabled(False)
            self._curve = self._plot.plot(pen=pg.mkPen("#2ecc71", width=1.5))
            self._trigger_line = pg.InfiniteLine(angle=0, pen=pg.mkPen("#ff3b30", width=1, style=Qt.PenStyle.DashLine))
            self._plot.addItem(self._trigger_line)
            layout.addWidget(self._plot)
        else:
            from PyQt6.QtWidgets import QLabel
            layout.addWidget(QLabel("pyqtgraph niedostepny - podglad widma wylaczony"))

    @property
    def available(self) -> bool:
        return self._plot is not None

    def update_spectrum(self, freqs_hz, db, trigger_dbfs: float) -> None:
        if not self.available:
            return
        self._curve.setData(freqs_hz / 1e6, db)
        self._trigger_line.setValue(trigger_dbfs)
        # Autorange alone tends to hug the trigger line right against the
        # top edge whenever nothing currently on screen is above it (the
        # common case) - there's nowhere to actually *see* a signal once it
        # does cross the trigger, it just gets clipped at the very top.
        # Explicitly keep _TRIGGER_HEADROOM_DB of clear space above the
        # trigger line at all times (autorange still takes over and grows
        # further if a real signal peak needs more room than that).
        if db.size:
            data_min = float(np.min(db))
            data_max = float(np.max(db))
        else:
            data_min = data_max = trigger_dbfs
        y_min = min(data_min, trigger_dbfs) - 3.0
        y_max = max(data_max, trigger_dbfs + _TRIGGER_HEADROOM_DB)
        self._plot.setYRange(y_min, y_max, padding=0.02)


class RecIndicator(QWidget):
    """Small circular REC lamp: solid red while writing, dim grey otherwise."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._on = False
        self.setFixedSize(18, 18)

    def set_on(self, on: bool) -> None:
        if on != self._on:
            self._on = on
            self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor("#ff3b30") if self._on else QColor("#3a3d44")
        painter.setBrush(color)
        painter.setPen(QColor("#000000"))
        painter.drawEllipse(1, 1, self.width() - 2, self.height() - 2)
