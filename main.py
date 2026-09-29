#!/usr/bin/env python3
"""SmartScanATC entry point."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# A PyInstaller-frozen build (see packaging/linux-deb/) has no real Python
# installation to re-exec into or later pip-install a venv alongside - the
# whole point of freezing is that there's no on-disk site-packages/pip left
# to talk about, and the bundle is typically installed somewhere a normal
# user can't write to anyway (e.g. /opt/smartscanatc via the .deb). Skipping
# this check under `sys.frozen` avoids depending on VENV_PYTHON simply
# happening not to exist inside the bundle - the transcription install flow
# (transcription_install.py) has this exact same guard for the same reason;
# the in-app "Install transcription" button in a frozen build reports a
# clear error instead of silently trying to create a venv it can't use.
if not getattr(sys, "frozen", False):
    from smartscan.venv_paths import VENV_DIR, VENV_PYTHON
else:
    VENV_DIR = VENV_PYTHON = None

# The transcription tab (smartscan/playback/transcription_engine.py) needs
# faster-whisper. On a plain (non-externally-managed) Python it's installed
# straight into whatever's running this file - see
# smartscan/playback/transcription_install.py, which tries that first. This
# .venv is only the fallback for platforms/installs where a direct pip
# install is blocked (PEP 668 - Ubuntu and other distros that mark their
# system Python "externally managed"): the installer creates it with
# --system-site-packages (so it still sees the system PySide6/numpy/etc.),
# and this re-exec is what makes "python3 main.py" transparently pick it up
# afterwards without the user needing to remember to activate anything.
# If .venv doesn't exist, the app just starts normally without transcription.
# Note: .venv/bin/python3 (or Scripts\python.exe on Windows) is usually a
# symlink/copy of the system interpreter, so comparing sys.executable to
# VENV_PYTHON wouldn't tell us anything - whether we're actually running
# "inside" this venv is what sys.prefix says instead, set by the interpreter
# itself from the pyvenv.cfg next to whichever binary it was started with.
if VENV_PYTHON is not None and VENV_PYTHON.exists() and Path(sys.prefix).resolve() != VENV_DIR.resolve():
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])

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
