#!/usr/bin/env python3
"""SmartScanATC entry point."""
import os
import sys
from pathlib import Path

# The transcription tab (smartscan/playback/transcription_engine.py) needs
# faster-whisper, which we don't install into the system Python (PEP 668).
# So "python3 main.py" keeps working without remembering to activate a venv,
# we re-exec once at startup into the .venv interpreter (which also sees the
# system PySide6 thanks to --system-site-packages). If .venv doesn't exist,
# the app just starts on the system Python without transcription.
_VENV_DIR = Path(__file__).resolve().parent / ".venv"
_VENV_PYTHON = _VENV_DIR / "bin" / "python3"
# Note: .venv/bin/python3 is usually a symlink to the system interpreter, so
# comparing sys.executable.resolve() to _VENV_PYTHON.resolve() wouldn't tell
# us anything (both point at the same file). Whether we're actually running
# "inside" this venv is what sys.prefix says - the interpreter itself sets it
# based on the pyvenv.cfg next to whichever binary it was started with.
if _VENV_PYTHON.exists() and Path(sys.prefix).resolve() != _VENV_DIR.resolve():
    os.execv(str(_VENV_PYTHON), [str(_VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])

# Real crashes have been observed (journalctl, when launched from the .desktop
# file - see sdr_backend.py's LAST_CLOSE_FILE comment for the leading theory:
# reopening the RTL-SDR device too soon after a previous process released it)
# that leave *zero* trace: no Python traceback, nothing - consistent with a
# native-level crash (e.g. a segfault inside the SoapySDR/librtlsdr C code),
# which Python can't catch by definition. faulthandler *can* still catch
# that: it installs a low-level OS signal handler for SIGSEGV/SIGABRT/SIGBUS/
# SIGFPE that dumps whatever Python-level stack was running at the moment of
# the crash (e.g. the exact line that called into the C extension) before the
# process dies - the only way to get any diagnostic at all for a crash class
# that otherwise leaves the log completely silent. Written to a real file
# (not stdout) since a .desktop launch's stdout is journald, which a
# mid-crash flush can't be relied on to have captured completely.
import faulthandler
from smartscan.config import CONFIG_DIR, AppConfig
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
_crash_log = (CONFIG_DIR / "crash.log").open("a", buffering=1)
faulthandler.enable(file=_crash_log, all_threads=True)

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from smartscan import i18n
from smartscan.gui.main_window import MainWindow
from smartscan.gui.theme import DARK_STYLESHEET
from smartscan.gui.widgets import install_app_emoji_fallback

ICON_PATH = Path(__file__).resolve().parent / "smartscan" / "gui" / "resources" / "app_icon.svg"


def main() -> int:
    # Set the UI language before building any widget - every tr() call
    # during construction needs the right catalog already loaded (see
    # smartscan/i18n.py; switching language later requires a restart, this
    # is the only place it's read from config).
    i18n.set_language(AppConfig.load().language)

    # "-name" sets the WM_CLASS instance name to "smartscanatc" (otherwise Qt
    # just uses the script's own name, "main.py") - this lets GNOME/KDE match
    # the window to smartscanatc.desktop (StartupWMClass) and show
    # app_icon.svg instead of the default gear icon.
    app = QApplication(sys.argv + ["-name", "smartscanatc"])
    app.setDesktopFileName("smartscanatc")
    # Must run BEFORE setStyleSheet() - see widgets.install_app_emoji_fallback()'s docstring.
    install_app_emoji_fallback(app)
    app.setStyleSheet(DARK_STYLESHEET)
    app.setWindowIcon(QIcon(str(ICON_PATH)))
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
