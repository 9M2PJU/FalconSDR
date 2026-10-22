"""Shared path computation for the optional .venv this project uses as a
fallback home for heavy, PEP 668-unfriendly extras (currently: transcription/
faster-whisper - see transcription_install.py). main.py's own startup
re-exec check and transcription_install.py both need the exact same
computation, so it lives here once rather than being duplicated.

Deliberately no other imports (this gets imported by main.py before its
own re-exec decision, i.e. before anything heavier -
is safe to import yet).
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VENV_DIR = REPO_ROOT / ".venv"
# Windows venvs use Scripts\python.exe, not bin/python3 - main.py's original
# version only ever checked the POSIX layout, so a .venv created on Windows
# (e.g. by the transcription installer) would have silently never been
# picked up there.
if sys.platform == "win32":
    VENV_PYTHON = VENV_DIR / "Scripts" / "python.exe"
else:
    VENV_PYTHON = VENV_DIR / "bin" / "python3"
