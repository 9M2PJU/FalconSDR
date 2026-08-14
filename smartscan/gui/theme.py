"""Minimalist dark theme stylesheet (spec section 6)."""

DARK_STYLESHEET = """
QWidget {
    background-color: #1b1d22;
    color: #e8e8ea;
    font-size: 13px;
}
QMainWindow, QDialog {
    background-color: #1b1d22;
}
QGroupBox {
    border: 1px solid #33363f;
    border-radius: 6px;
    margin-top: 10px;
    padding-top: 10px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: #9aa0a6;
}
QPushButton {
    background-color: #2a2d35;
    border: 1px solid #40444e;
    border-radius: 5px;
    padding: 6px 12px;
}
QPushButton:hover { background-color: #34383f; }
QPushButton:pressed { background-color: #20232a; }
QPushButton:checked {
    background-color: #c0392b;
    border-color: #e74c3c;
}
QPushButton:disabled { color: #666a72; border-color: #2c2f36; }
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {
    background-color: #14161a;
    border: 1px solid #33363f;
    border-radius: 4px;
    padding: 3px 6px;
}
QTableWidget {
    background-color: #14161a;
    gridline-color: #2a2d35;
    border: 1px solid #33363f;
}
QHeaderView::section {
    background-color: #23262d;
    border: none;
    padding: 4px;
}
QTabWidget::pane { border: 1px solid #33363f; }
QTabBar::tab {
    background: #23262d;
    padding: 6px 14px;
    border: 1px solid #33363f;
    border-bottom: none;
}
QTabBar::tab:selected { background: #2f333c; }
QCheckBox, QRadioButton { spacing: 6px; }
"""
