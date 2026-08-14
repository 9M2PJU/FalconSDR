#!/usr/bin/env python3
"""SmartScanATC entry point."""
import os
import sys
from pathlib import Path

# Zakładka transkrypcji (smartscan/playback/transcription_engine.py) potrzebuje
# faster-whisper, którego nie instalujemy do systemowego Pythona (PEP 668).
# Żeby "python3 main.py" dalej działało bez pamiętania o aktywacji venv,
# przekierowujemy się raz na start do interpretera z .venv (który widzi też
# systemowe PyQt6 dzięki --system-site-packages). Jeśli .venv nie istnieje,
# program po prostu wystartuje na systemowym Pythonie bez transkrypcji.
_VENV_DIR = Path(__file__).resolve().parent / ".venv"
_VENV_PYTHON = _VENV_DIR / "bin" / "python3"
# Uwaga: .venv/bin/python3 to zwykle symlink do systemowego interpretera, więc
# porównywanie sys.executable.resolve() z _VENV_PYTHON.resolve() nic by nie dało
# (oba wskazują na ten sam plik). To, czy faktycznie działamy "w" tym venv,
# mówi sys.prefix - ustawia go sam interpreter na podstawie pyvenv.cfg obok
# binarki, z którą został uruchomiony.
if _VENV_PYTHON.exists() and Path(sys.prefix).resolve() != _VENV_DIR.resolve():
    os.execv(str(_VENV_PYTHON), [str(_VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from smartscan.gui.main_window import MainWindow
from smartscan.gui.theme import DARK_STYLESHEET

ICON_PATH = Path(__file__).resolve().parent / "smartscan" / "gui" / "resources" / "app_icon.svg"


def main() -> int:
    # "-name" ustawia instance name w WM_CLASS na "smartscanatc" (inaczej Qt
    # bierze tu po prostu nazwę skryptu, "main.py") - to pozwala powłoce
    # GNOME/KDE dopasować okno do smartscanatc.desktop (StartupWMClass) i
    # pokazać app_icon.svg zamiast domyślnej ikonki koła zębatego.
    app = QApplication(sys.argv + ["-name", "smartscanatc"])
    app.setDesktopFileName("smartscanatc")
    app.setStyleSheet(DARK_STYLESHEET)
    app.setWindowIcon(QIcon(str(ICON_PATH)))
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
