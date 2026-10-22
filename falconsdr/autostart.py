"""Start FalconSDR when the user logs in - off by default (Settings → Advanced).

The OS's own login item *is* the state: no config flag that could disagree
with it after a reinstall, a move of the program or a manual removal.
Windows: HKCU Run value; Linux: XDG autostart .desktop; macOS: LaunchAgent
(written to spec, not yet tested on a real Mac). Starts with --no-browser -
a browser window popping up at every login is exactly what nobody wants;
the UI opens from the menu shortcut as usual.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

NAME = "FalconSDR"
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_MAC_LABEL = "app.falconsdr"


def command() -> list[str]:
    """What the login item runs: the frozen app itself, or this checkout's
    main.py with the running interpreter (pythonw on Windows - no console)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--no-browser"]
    exe = Path(sys.executable)
    if sys.platform == "win32" and exe.with_name("pythonw.exe").exists():
        exe = exe.with_name("pythonw.exe")
    return [str(exe), str(Path(__file__).resolve().parent.parent / "main.py"), "--no-browser"]


def _desktop_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "autostart" / "falconsdr.desktop"


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_MAC_LABEL}.plist"


def _desktop_exec(args: list[str]) -> str:
    # Desktop Entry spec quoting: double quotes, with " ` $ \ escaped inside.
    def quote(a: str) -> str:
        if not any(c in a for c in ' \t"`$\\\'><~|&;*?#()'):
            return a
        return '"' + "".join("\\" + c if c in '"`$\\' else c for c in a) + '"'
    return " ".join(quote(a) for a in args)


def supported() -> bool:
    return sys.platform in ("win32", "darwin") or sys.platform.startswith("linux")


def is_enabled() -> bool:
    if sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
                winreg.QueryValueEx(key, NAME)
            return True
        except OSError:
            return False
    if sys.platform == "darwin":
        return _plist_path().exists()
    return _desktop_path().exists()


def set_enabled(enabled: bool) -> None:
    cmd = command()
    if sys.platform == "win32":
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, NAME, 0, winreg.REG_SZ, subprocess.list2cmdline(cmd))
            else:
                try:
                    winreg.DeleteValue(key, NAME)
                except FileNotFoundError:
                    pass
        return
    path = _plist_path() if sys.platform == "darwin" else _desktop_path()
    if not enabled:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        path.write_bytes(plistlib.dumps({"Label": _MAC_LABEL, "ProgramArguments": cmd, "RunAtLoad": True}))
    else:
        path.write_text(
            "[Desktop Entry]\nType=Application\nName=FalconSDR\n"
            f"Exec={_desktop_exec(cmd)}\nTerminal=false\nX-GNOME-Autostart-enabled=true\n",
            encoding="utf-8",
        )
