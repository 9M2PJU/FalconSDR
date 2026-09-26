"""Zakładka Statystyki: podsumowanie, tabela częstotliwości, wykresy ruchu -
ogólne (wszystkie nagrania) domyślnie, albo dla jednej wybranej częstotliwości
(klik w tabelę) - godziny doby i dni tygodnia, żeby było widać np. "ten kanał
żyje tylko w dni robocze rano" na pierwszy rzut oka."""
from __future__ import annotations

from typing import List, Optional

import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from smartscan.playback.models import Recording
from smartscan.playback.stats import counts_per_day, counts_per_hour, counts_per_weekday, frequency_stats, summarize

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
    frequencySelected = Signal(int)  # dwuklik w wiersz - "przejdz do Przegladarki"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._recordings: List[Recording] = []
        self._freq_names: dict[int, str] = {}
        self._use_utc = False
        self._charts: dict[str, pg.PlotWidget] = {}  # named slots - see _set_chart()

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
        self.freq_table.setToolTip(
            "Klik: pokaż specyfikę tej częstotliwości obok (godziny/dni tygodnia).\n"
            "Podwójny klik: przejdź do niej w zakładce Odtwarzacz."
        )
        self.freq_table.cellDoubleClicked.connect(self._on_freq_row_activated)
        self.freq_table.itemSelectionChanged.connect(self._on_freq_selection_changed)
        splitter.addWidget(self.freq_table)

        self.charts_stack = QStackedWidget()
        splitter.addWidget(self.charts_stack)
        splitter.setSizes([420, 680])

        self.charts_stack.addWidget(self._build_overview_widget())
        self.charts_stack.addWidget(self._build_detail_widget())

    # -- ogólny widok (domyślny, nic nie wybrane) ---------------------------
    def _build_overview_widget(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("<b>Liczba nagrań dziennie</b>"))
        self.day_chart_container = QVBoxLayout()
        layout.addLayout(self.day_chart_container)
        self.hour_chart_title = QLabel("<b>Ruch wg godziny doby (czas polski)</b>")
        layout.addWidget(self.hour_chart_title)
        self.hour_chart_container = QVBoxLayout()
        layout.addLayout(self.hour_chart_container)
        return widget

    # -- widok jednej czestotliwosci (klik w tabele) -------------------------
    def _build_detail_widget(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)

        back_btn = QPushButton("← Widok ogólny")
        back_btn.setToolTip("Odznacz wybraną częstotliwość i wróć do statystyk całej biblioteki")
        back_btn.clicked.connect(self._show_overview)
        layout.addWidget(back_btn)

        self.detail_header = QLabel("")
        self.detail_header.setStyleSheet("font-size: 16px; font-weight: bold;")
        self.detail_header.setWordWrap(True)
        layout.addWidget(self.detail_header)

        self.detail_stats_label = QLabel("")
        self.detail_stats_label.setStyleSheet("color: #9aa0a6;")
        self.detail_stats_label.setWordWrap(True)
        layout.addWidget(self.detail_stats_label)

        self.detail_hour_title = QLabel("<b>Wg godziny doby (czas polski)</b>")
        layout.addWidget(self.detail_hour_title)
        self.detail_hour_container = QVBoxLayout()
        layout.addLayout(self.detail_hour_container)

        layout.addWidget(QLabel("<b>Wg dnia tygodnia</b>"))
        self.detail_weekday_container = QVBoxLayout()
        layout.addLayout(self.detail_weekday_container)
        return widget

    def _show_overview(self) -> None:
        self.freq_table.clearSelection()
        self.charts_stack.setCurrentIndex(0)

    def _on_freq_row_activated(self, row: int, _col: int) -> None:
        item = self.freq_table.item(row, 0)
        if item is None:
            return
        freq_khz = item.data(Qt.ItemDataRole.UserRole)
        if freq_khz is not None:
            self.frequencySelected.emit(freq_khz)

    def _on_freq_selection_changed(self) -> None:
        items = self.freq_table.selectedItems()
        if not items:
            self.charts_stack.setCurrentIndex(0)
            return
        freq_item = self.freq_table.item(items[0].row(), 0)
        freq_khz = freq_item.data(Qt.ItemDataRole.UserRole) if freq_item is not None else None
        if freq_khz is None:
            self.charts_stack.setCurrentIndex(0)
            return
        self._show_frequency_detail(freq_khz)

    def _show_frequency_detail(self, freq_khz: int) -> None:
        recs = [r for r in self._recordings if r.freq_khz == freq_khz]
        if not recs:
            self.charts_stack.setCurrentIndex(0)
            return

        freq_text = f"{freq_khz / 1000:.3f} MHz"
        name = self._freq_names.get(freq_khz)
        self.detail_header.setText(f"{name}  ({freq_text})" if name else freq_text)

        qualities = [r.quality_score for r in recs if r.quality]
        avg_q = sum(qualities) / len(qualities) if qualities else 0.0
        total_min = sum(r.duration_sec for r in recs) / 60.0
        last_heard = max((r.dt_utc if self._use_utc else r.dt_local) for r in recs)
        self.detail_stats_label.setText(
            f"{len(recs)} nagrań  •  śr. jakość {avg_q:.0f}/100  •  łącznie {total_min:.1f} min  •  "
            f"ostatnio słyszana {last_heard:%Y-%m-%d %H:%M}"
        )

        hour_counts = counts_per_hour(recs, use_utc=self._use_utc)
        self._set_chart(
            "detail_hour", self.detail_hour_container,
            [f"{h:02d}" for h, _ in hour_counts], [c for _, c in hour_counts], "#ffb454",
        )
        weekday_counts = counts_per_weekday(recs, use_utc=self._use_utc)
        self._set_chart(
            "detail_weekday", self.detail_weekday_container,
            [d for d, _ in weekday_counts], [c for _, c in weekday_counts], "#6fcf97",
        )
        self.charts_stack.setCurrentIndex(1)

    # -- wspolne -------------------------------------------------------------
    def _set_chart(self, key: str, container: QVBoxLayout, labels: List[str], values: List[float], color: str) -> None:
        old = self._charts.get(key)
        if old is not None:
            old.setParent(None)
        plot = _bar_chart(labels, values, color=color)
        self._charts[key] = plot
        container.addWidget(plot)

    def set_freq_names(self, freq_names: dict[int, str]) -> None:
        self._freq_names = freq_names
        if self.charts_stack.currentIndex() == 1:
            self._on_freq_selection_changed()

    def set_use_utc(self, use_utc: bool) -> None:
        self._use_utc = use_utc
        self.hour_chart_title.setText(f"<b>Ruch wg godziny doby (czas {'UTC' if use_utc else 'polski'})</b>")
        self.detail_hour_title.setText(f"<b>Wg godziny doby (czas {'UTC' if use_utc else 'polski'})</b>")

    def update_stats(self, recordings: List[Recording]) -> None:
        self._recordings = recordings
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
        self._set_chart("day", self.day_chart_container, [d for d, _ in day_counts], [c for _, c in day_counts], "#4f9cff")

        hour_counts = counts_per_hour(recordings, use_utc=self._use_utc)
        self._set_chart(
            "hour", self.hour_chart_container,
            [f"{h:02d}" for h, _ in hour_counts], [c for _, c in hour_counts], "#ffb454",
        )

        # setRowCount()/repopulating the table drops the previous selection,
        # so a detail view that was showing falls back to the overview here
        # rather than pointing at stale data - simplest correct behaviour,
        # re-selecting the same frequency post-refresh isn't worth the
        # bookkeeping for how rarely it'd actually matter.
        self._on_freq_selection_changed()
