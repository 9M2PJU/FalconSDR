"""Zakładka Statystyki: podsumowanie, tabela częstotliwości, wykresy ruchu."""
from __future__ import annotations

from typing import List

import pyqtgraph as pg
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from smartscan.playback.models import Recording
from smartscan.playback.stats import counts_per_day, counts_per_hour, frequency_stats, summarize

pg.setConfigOption("background", (30, 30, 34))
pg.setConfigOption("foreground", (220, 220, 220))


class _NumericItem(QTableWidgetItem):
    """QTableWidgetItem that sorts by an actual numeric value instead of the
    displayed text, so e.g. "9" doesn't sort after "10" lexicographically."""

    def __init__(self, text: str, value: float):
        super().__init__(text)
        self._value = value

    def __lt__(self, other) -> bool:
        if isinstance(other, _NumericItem):
            return self._value < other._value
        return super().__lt__(other)


def _bar_chart(categories: List[str], values: List[float], color="#4f9cff", rotate=False) -> pg.PlotWidget:
    plot = pg.PlotWidget()
    plot.showGrid(x=False, y=True, alpha=0.25)
    xs = list(range(len(categories)))
    bars = pg.BarGraphItem(x=xs, height=values, width=0.7, brush=color)
    plot.addItem(bars)
    axis = plot.getAxis("bottom")
    step = max(1, len(categories) // 20)
    axis.setTicks([[(i, cat) for i, cat in enumerate(categories) if i % step == 0]])
    if rotate:
        axis.setStyle(tickTextOffset=10)
    plot.setMouseEnabled(x=True, y=False)
    return plot


class StatsTab(QWidget):
    frequencySelected = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)

        self.summary_box = QGroupBox("Podsumowanie")
        self.summary_layout = QGridLayout(self.summary_box)
        self.summary_labels = {}
        for i, key in enumerate(
            ["Nagrań", "Łączny czas", "Zakres dat", "Śr. jakość", "Częstotliwości", "Najaktywniejsza"]
        ):
            self.summary_layout.addWidget(QLabel(f"<b>{key}:</b>"), i // 2, (i % 2) * 2)
            value_label = QLabel("—")
            self.summary_labels[key] = value_label
            self.summary_layout.addWidget(value_label, i // 2, (i % 2) * 2 + 1)
        outer.addWidget(self.summary_box)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(splitter, stretch=1)

        self.freq_table = QTableWidget(0, 4)
        self.freq_table.setHorizontalHeaderLabels(["Częstotliwość", "Liczba", "Śr. jakość", "Ostatnio słyszana"])
        self.freq_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.freq_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.freq_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.freq_table.setSortingEnabled(True)
        self.freq_table.setToolTip("Kliknij dwukrotnie wiersz, aby przejść do tej częstotliwości w zakładce Przeglądarka.")
        self.freq_table.cellDoubleClicked.connect(self._on_freq_row_activated)
        splitter.addWidget(self.freq_table)

        charts_widget = QWidget()
        charts_layout = QVBoxLayout(charts_widget)
        charts_layout.addWidget(QLabel("<b>Liczba nagrań dziennie</b>"))
        self.day_chart_container = QVBoxLayout()
        charts_layout.addLayout(self.day_chart_container)
        self.hour_chart_title = QLabel("<b>Ruch wg godziny doby (czas polski)</b>")
        charts_layout.addWidget(self.hour_chart_title)
        self.hour_chart_container = QVBoxLayout()
        charts_layout.addLayout(self.hour_chart_container)
        splitter.addWidget(charts_widget)
        splitter.setSizes([420, 680])

        self.day_plot: pg.PlotWidget | None = None
        self.hour_plot: pg.PlotWidget | None = None
        self._use_utc = False

    def _on_freq_row_activated(self, row: int, _col: int) -> None:
        item = self.freq_table.item(row, 0)
        if item is None:
            return
        freq_khz = item.data(Qt.ItemDataRole.UserRole)
        if freq_khz is not None:
            self.frequencySelected.emit(freq_khz)

    def set_use_utc(self, use_utc: bool) -> None:
        self._use_utc = use_utc
        self.hour_chart_title.setText(f"<b>Ruch wg godziny doby (czas {'UTC' if use_utc else 'polski'})</b>")

    def update_stats(self, recordings: List[Recording]) -> None:
        summary = summarize(recordings, use_utc=self._use_utc)

        hours = summary.total_duration_sec / 3600.0
        date_range = "—"
        if summary.date_from and summary.date_to:
            date_range = f"{summary.date_from:%Y-%m-%d} → {summary.date_to:%Y-%m-%d}"

        self.summary_labels["Nagrań"].setText(str(summary.total_count))
        self.summary_labels["Łączny czas"].setText(f"{hours:.1f} h")
        self.summary_labels["Zakres dat"].setText(date_range)
        self.summary_labels["Śr. jakość"].setText(f"{summary.avg_quality:.0f} / 100")
        self.summary_labels["Częstotliwości"].setText(str(summary.unique_frequencies))
        self.summary_labels["Najaktywniejsza"].setText(
            f"{summary.busiest_freq.label} ({summary.busiest_freq.count})" if summary.busiest_freq else "—"
        )

        freq_stats = frequency_stats(recordings, use_utc=self._use_utc)
        self.freq_table.setSortingEnabled(False)
        self.freq_table.setRowCount(len(freq_stats))
        for row, st in enumerate(freq_stats):
            freq_item = _NumericItem(st.label, st.freq_khz)
            freq_item.setData(Qt.ItemDataRole.UserRole, st.freq_khz)
            self.freq_table.setItem(row, 0, freq_item)
            self.freq_table.setItem(row, 1, _NumericItem(str(st.count), st.count))
            self.freq_table.setItem(row, 2, _NumericItem(f"{st.avg_quality:.0f}", st.avg_quality))
            self.freq_table.setItem(
                row, 3, _NumericItem(st.last_heard.strftime("%Y-%m-%d %H:%M"), st.last_heard.timestamp())
            )
        self.freq_table.setSortingEnabled(True)

        day_counts = counts_per_day(recordings, use_utc=self._use_utc)
        if self.day_plot is not None:
            self.day_plot.setParent(None)
        labels = [d for d, _ in day_counts]
        values = [c for _, c in day_counts]
        self.day_plot = _bar_chart(labels, values, color="#4f9cff")
        self.day_chart_container.addWidget(self.day_plot)

        hour_counts = counts_per_hour(recordings, use_utc=self._use_utc)
        if self.hour_plot is not None:
            self.hour_plot.setParent(None)
        h_labels = [f"{h:02d}" for h, _ in hour_counts]
        h_values = [c for _, c in hour_counts]
        self.hour_plot = _bar_chart(h_labels, h_values, color="#ffb454")
        self.hour_chart_container.addWidget(self.hour_plot)
