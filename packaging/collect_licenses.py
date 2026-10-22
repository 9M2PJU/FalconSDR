"""Writes THIRD-PARTY-LICENSES.txt for a packaged build: the license texts of
every package frozen into it (BSD/MIT/Apache require them alongside a binary
distribution), the Python runtime's, and the bundled ffmpeg's version, build
configuration and where its source is (GPL). Run with the *build* venv's
Python - it reports what is installed there:

    python packaging/collect_licenses.py <output file>

Shared by packaging/windows/build.ps1 and packaging/linux-deb/build.sh.
"""
from __future__ import annotations

import importlib.metadata as md
import subprocess
import sys
from pathlib import Path

# Build tools in the build venv that never end up inside the app.
_BUILD_ONLY = {"pyinstaller", "pyinstaller-hooks-contrib", "altgraph", "pefile", "pywin32-ctypes",
               "macholib", "setuptools", "wheel"}
_LICENSE_WORDS = ("licen", "copying", "notice", "authors")


def _section(title: str, body: str) -> str:
    return f"{'=' * 78}\n{title}\n{'=' * 78}\n\n{body.strip()}\n\n"


def main(out: Path) -> None:
    parts = [
        "FalconSDR is licensed under the GNU General Public License v3.0 or later\n"
        "(LICENSE.txt). This build also contains the third-party software below,\n"
        "each under its own license.\n\n"
    ]
    for dist in sorted(md.distributions(), key=lambda d: d.metadata["Name"].lower()):
        name = dist.metadata["Name"]
        if name.lower() in _BUILD_ONLY:
            continue
        declared = dist.metadata.get("License-Expression") or (dist.metadata.get("License") or "").splitlines()[0:1]
        declared = declared if isinstance(declared, str) else " ".join(declared)
        texts = []
        for f in dist.files or []:
            if any(w in f.name.lower() for w in _LICENSE_WORDS) and not f.name.endswith((".py", ".pyc")):
                try:
                    texts.append(f"--- {f}\n{Path(dist.locate_file(f)).read_text(encoding='utf-8', errors='replace')}")
                except OSError:
                    pass
        home = dist.metadata.get("Home-page") or next(
            (u.split(",", 1)[1].strip() for u in dist.metadata.get_all("Project-URL") or [] if "," in u), "")
        body = f"License: {declared or 'see below'}\n{('Project: ' + home) if home else ''}\n\n" + (
            "\n".join(texts) or "(no license file shipped in the package - see the project page)")
        parts.append(_section(f"{name} {dist.version}", body))

    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        python_license = Path(sys.base_prefix) / "lib" / f"python{sys.version_info[0]}.{sys.version_info[1]}" / "LICENSE.txt"
    if python_license.is_file():
        parts.append(_section(f"Python {sys.version.split()[0]} runtime", python_license.read_text(encoding="utf-8", errors="replace")))

    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        info = subprocess.run([exe, "-hide_banner", "-L"], capture_output=True, text=True, errors="replace", timeout=30).stdout
        version = subprocess.run([exe, "-version"], capture_output=True, text=True, errors="replace", timeout=30).stdout
        parts.append(_section(
            f"FFmpeg ({Path(exe).name}, shipped by imageio-ffmpeg)",
            "FFmpeg is free software; its source code is available at https://ffmpeg.org/download.html\n"
            "(the exact release and configure flags of this binary are listed below), and the build\n"
            "scripts of this binary at https://github.com/imageio/imageio-binaries.\n\n"
            + version + "\n" + info,
        ))
    except Exception as exc:  # no ffmpeg bundled on this platform
        print(f"note: no bundled ffmpeg ({exc})", file=sys.stderr)

    out.write_text("".join(parts), encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
