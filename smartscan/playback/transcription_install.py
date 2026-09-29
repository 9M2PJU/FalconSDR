"""Installs the optional transcription extra (faster-whisper, + CUDA libs if
requested) from inside the running app - see TranscriptionPanel, which is
the only caller. Runs on its own QThread since pip installing this can take
a while (ctranslate2/av/tokenizers are sizeable downloads).

Two-tier strategy, in order:
1. Try a plain ``pip install`` straight into whatever interpreter is running
   the app right now. Works, and needs no restart at all (see
   TranscriptionPanel's handling of ``needs_restart=False``), on any install
   that isn't PEP 668 "externally managed" - which in practice is most
   Windows/macOS installs of this app, and any Linux one already running
   inside a venv.
2. Only if that specifically fails with pip's own
   "externally-managed-environment" error (Ubuntu and other distros that
   lock down the system Python) does this fall back to creating
   ``smartscan/venv_paths.VENV_DIR`` (``--system-site-packages``, so it
   still sees this app's own already-installed PySide6/numpy/etc.) and
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

from PySide6.QtCore import QThread, Signal

from ..venv_paths import VENV_DIR, VENV_PYTHON

_PACKAGES = ["faster-whisper>=1.2"]
_GPU_PACKAGES = ["nvidia-cublas-cu12", "nvidia-cudnn-cu12"]
_EXTERNALLY_MANAGED_MARKER = "externally-managed-environment"


class TranscriptionInstallWorker(QThread):
    output_line = Signal(str)
    # success, needs_restart, error_message ("" on success)
    finished_install = Signal(bool, bool, str)

    def __init__(self, with_gpu: bool = False, parent=None):
        super().__init__(parent)
        self._with_gpu = with_gpu

    def _packages(self) -> list[str]:
        return _PACKAGES + (_GPU_PACKAGES if self._with_gpu else [])

    def _run_streamed(self, cmd: list[str]) -> tuple[bool, str]:
        self.output_line.emit("$ " + " ".join(cmd))
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
        except OSError as exc:
            message = str(exc)
            self.output_line.emit(message)
            return False, message
        lines: list[str] = []
        for line in proc.stdout:
            line = line.rstrip("\n")
            lines.append(line)
            self.output_line.emit(line)
        proc.wait()
        return proc.returncode == 0, "\n".join(lines)

    def _pip_install(self, python_exe: str) -> tuple[bool, str]:
        return self._run_streamed([python_exe, "-m", "pip", "install", "--upgrade", *self._packages()])

    def run(self) -> None:
        # A PyInstaller-frozen build's own sys.executable *is* the app
        # itself, not a real Python interpreter - `[sys.executable, "-m",
        # "pip", ...]` wouldn't run pip at all, it would try to re-launch
        # this whole GUI a second time (frozen entry points ignore -m the
        # way a real python3 binary understands it). Fail loudly and
        # immediately here rather than let that happen - see main.py's own
        # matching `sys.frozen` guard on the venv re-exec check, and
        # packaging/linux-deb/README.md for why this is a known, documented
        # gap in the packaged build rather than something silently broken.
        if getattr(sys, "frozen", False):
            message = (
                "Transcription can't be installed from this packaged build - "
                "there's no real Python/pip inside it to install into. Use "
                "the pip/source install (see the project's README) if you "
                "need transcription."
            )
            # TranscriptionPanel._on_install_finished() only ever shows a
            # generic "see the log above" status label on failure (true for
            # every failure path here, not just this one) - the actual
            # explanation always has to go through output_line into the
            # visible install_log box, or there's nothing for that label to
            # be pointing at. A real, reported bug: this branch originally
            # skipped straight to finished_install.emit() with nothing ever
            # written to the log, so the user saw an empty box under a
            # message telling them to read it.
            self.output_line.emit(message)
            self.finished_install.emit(False, False, message)
            return
        # An existing .venv means this machine already needed the fallback
        # once before (or a previous install attempt got this far) - go
        # straight there instead of re-discovering the same PEP 668 error.
        if VENV_PYTHON.exists():
            ok, output = self._pip_install(str(VENV_PYTHON))
            self.finished_install.emit(ok, ok, output if not ok else "")
            return

        ok, output = self._pip_install(sys.executable)
        if ok:
            self.finished_install.emit(True, False, "")
            return
        if _EXTERNALLY_MANAGED_MARKER not in output:
            # A real failure unrelated to PEP 668 - a venv wouldn't fix it.
            self.finished_install.emit(False, False, output)
            return

        self.output_line.emit(
            "-- direct install blocked (externally-managed-environment) - "
            "creating a dedicated venv instead --"
        )
        ok, venv_output = self._run_streamed(
            [sys.executable, "-m", "venv", "--system-site-packages", str(VENV_DIR)]
        )
        if not ok:
            self.finished_install.emit(False, False, venv_output)
            return
        ok, output = self._pip_install(str(VENV_PYTHON))
        self.finished_install.emit(ok, ok, output if not ok else "")
