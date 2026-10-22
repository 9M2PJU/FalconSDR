"""Installs the optional transcription extra (faster-whisper, + CUDA libs if
requested) from inside the running app - see worker.py, which runs it on
whichever computer was asked to (Settings → Transcription & computers).
pip installing this can take a while (ctranslate2/av/tokenizers are
sizeable downloads).

Two-tier strategy, in order:
1. Try a plain ``pip install`` straight into whatever interpreter is running
   the app right now. Works, and needs no restart at all, on any install
   that isn't PEP 668 "externally managed" - which in practice is most
   Windows/macOS installs of this app, and any Linux one already running
   inside a venv.
2. Only if that specifically fails with pip's own
   "externally-managed-environment" error (Ubuntu and other distros that
   lock down the system Python) does this fall back to creating
   ``falconsdr/venv_paths.VENV_DIR`` (``--system-site-packages``, so it
   still sees this app's own already-installed numpy/etc.) and
   installing there instead - the same venv main.py's own startup check
   already knows how to transparently re-exec into. That case does need a
   restart (a *different* interpreter process has to start seeing it).
A genuine failure (network down, disk full, ...) at either step is reported
as-is - falling back to a venv wouldn't fix an unrelated error, so this only
takes the venv branch on the specific error it's a workaround for.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Callable


from ..venv_paths import VENV_DIR, VENV_PYTHON

# faster-whisper 1.2.1 still calls av.open(..., metadata_errors=...), an
# argument PyAV removed outright in 19.0.0 - faster-whisper itself only
# declares "av>=11" with no upper bound, so an unpinned install grabs the
# newest av and every transcription fails at decode_audio() with
# "TypeError: open() got an unexpected keyword argument 'metadata_errors'".
# Pinning <19 here is what actually avoids it; PyAV's manylinux wheels are
# abi3 (cp311+), so this doesn't force a source build on a newer Python.
_PACKAGES = ["faster-whisper>=1.2", "av<19"]
_GPU_PACKAGES = ["nvidia-cublas-cu12", "nvidia-cudnn-cu12"]
_EXTERNALLY_MANAGED_MARKER = "externally-managed-environment"


def install_transcription(with_gpu: bool, on_line: Callable[[str], None]) -> tuple[bool, bool, str]:
    """Returns (success, needs_restart, error_message - "" on success);
    every line of pip's output goes to ``on_line`` as it arrives."""
    packages = _PACKAGES + (_GPU_PACKAGES if with_gpu else [])

    def run_streamed(cmd: list[str]) -> tuple[bool, str]:
        on_line("$ " + " ".join(cmd))
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
        except OSError as exc:
            on_line(str(exc))
            return False, str(exc)
        lines: list[str] = []
        for line in proc.stdout:
            line = line.rstrip("\n")
            lines.append(line)
            on_line(line)
        proc.wait()
        return proc.returncode == 0, "\n".join(lines)

    def pip_install(python_exe: str) -> tuple[bool, str]:
        return run_streamed([python_exe, "-m", "pip", "install", "--upgrade", *packages])

    # A PyInstaller-frozen build's own sys.executable *is* the app itself,
    # not a real Python interpreter - `[sys.executable, "-m", "pip", ...]`
    # would re-launch the app instead of running pip. Fail with a clear
    # message (through on_line too - that log is all the user sees).
    if getattr(sys, "frozen", False):
        message = (
            "Transcription can't be installed from this packaged build - "
            "there's no real Python/pip inside it to install into. Use "
            "the pip/source install (see the project's README) if you "
            "need transcription."
        )
        on_line(message)
        return False, False, message
    in_venv = Path(sys.prefix).resolve() == VENV_DIR.resolve()
    # An existing .venv means this machine already needed the fallback
    # once before - go straight there instead of re-discovering PEP 668.
    if VENV_PYTHON.exists():
        ok, output = pip_install(str(VENV_PYTHON))
        return ok, ok and not in_venv, "" if ok else output

    ok, output = pip_install(sys.executable)
    if ok:
        return True, False, ""
    if _EXTERNALLY_MANAGED_MARKER not in output:
        return False, False, output  # a real failure unrelated to PEP 668 - a venv wouldn't fix it
    on_line(
        "-- direct install blocked (externally-managed-environment) - "
        "creating a dedicated venv instead --"
    )
    ok, venv_output = run_streamed([sys.executable, "-m", "venv", "--system-site-packages", str(VENV_DIR)])
    if not ok:
        return False, False, venv_output
    ok, output = pip_install(str(VENV_PYTHON))
    return ok, ok, "" if ok else output
