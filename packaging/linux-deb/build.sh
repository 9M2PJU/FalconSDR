#!/usr/bin/env bash
# Builds a self-contained ("fat") .deb for SmartScanATC: a PyInstaller
# --onedir bundle (its own Python + PySide6/numpy/scipy/pyqtgraph/
# pyrtlsdrlib/sounddevice, all frozen in) wrapped in a Debian package, so an
# end user needs nothing beyond `sudo apt install ./smartscanatc_*.deb` -
# no system python3-pyside6, no pip, no venv.
#
# Deliberately built in a *throwaway* venv (packaging/linux-deb/build-venv,
# gitignored, never the project's own .venv) so nothing from an existing dev
# environment leaks into the bundle by accident - see this directory's own
# README.md for why that distinction matters and how to actually verify it.
#
# What this build does NOT include, on purpose (see README.md's "Known
# limitations" section for the full explanation):
#   - SoapySDR support (HackRF/LimeSDR/Airspy/SDRplay/USRP/...) - RTL-SDR via
#     the bundled pyrtlsdrlib native backend is the only supported hardware
#     in this build. SoapySDR is a system C library with its own plugin
#     architecture, not something that freezes cleanly into a single bundle.
#   - The in-app "Install transcription" button - it needs a real, writable
#     Python/pip environment to install into, which a frozen build isn't.
#   - MP3 recording output - still works, but only if the user separately
#     has `ffmpeg` on PATH (a system binary this build can't bundle either).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BUILD="$SCRIPT_DIR/build"
VENV="$SCRIPT_DIR/build-venv"
ARCH="$(dpkg --print-architecture)"
VERSION="$(date +%Y.%m.%d)"
PKG=smartscanatc

echo "== SmartScanATC .deb build =="
echo "repo root: $REPO_ROOT"
echo "version:   $VERSION"
echo "arch:      $ARCH"

echo "-- 1/5: fresh build venv (not the project's dev .venv) --"
rm -rf "$VENV"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip --quiet
"$VENV/bin/pip" install --quiet -r "$REPO_ROOT/requirements.txt" pyinstaller

echo "-- 2/5: PyInstaller (--onedir) --"
rm -rf "$BUILD"
mkdir -p "$BUILD"
(
  cd "$REPO_ROOT"
  # Absolute source paths for --add-data - PyInstaller resolves a relative
  # source against the .spec file's own directory (--specpath), not the
  # invocation cwd, which silently pointed at the wrong place here once
  # --specpath stopped being the repo root.
  #
  # --collect-data tzdata: tzdata (requirements.txt: "Windows has no system
  # time zone database - zoneinfo needs this package there") ships only data
  # files, no Python code a normal import touches - PyInstaller's static
  # import analysis doesn't see zoneinfo's own runtime fallback to it and
  # drops it by default. Confirmed by a real clean-container test: without
  # this flag, the frozen build raised ZoneInfoNotFoundError as soon as
  # anything touched a timezone - exactly the failure tzdata was added to
  # fix in the first place, silently reopened by freezing it.
  "$VENV/bin/pyinstaller" \
    --name "$PKG" \
    --onedir \
    --windowed \
    --noconfirm \
    --add-data "$REPO_ROOT/smartscan/locale/pl.po:smartscan/locale" \
    --add-data "$REPO_ROOT/smartscan/presets/band_presets.json:smartscan/presets" \
    --add-data "$REPO_ROOT/smartscan/gui/resources:smartscan/gui/resources" \
    --collect-data tzdata \
    --distpath "$BUILD/dist" \
    --workpath "$BUILD/work" \
    --specpath "$BUILD" \
    main.py
)

echo "-- 3/5: assembling the .deb tree --"
DEBROOT="$BUILD/deb"
rm -rf "$DEBROOT"
mkdir -p "$DEBROOT/DEBIAN"
mkdir -p "$DEBROOT/opt/$PKG"
mkdir -p "$DEBROOT/usr/bin"
mkdir -p "$DEBROOT/usr/share/applications"
mkdir -p "$DEBROOT/usr/share/icons/hicolor/scalable/apps"
mkdir -p "$DEBROOT/usr/share/doc/$PKG"

cp -r "$BUILD/dist/$PKG/." "$DEBROOT/opt/$PKG/"

cat > "$DEBROOT/usr/bin/$PKG" <<WRAPPER
#!/bin/sh
exec /opt/$PKG/$PKG "\$@"
WRAPPER
chmod 755 "$DEBROOT/usr/bin/$PKG"

cp "$SCRIPT_DIR/smartscanatc.desktop" "$DEBROOT/usr/share/applications/"
cp "$REPO_ROOT/smartscan/gui/resources/app_icon.svg" \
   "$DEBROOT/usr/share/icons/hicolor/scalable/apps/smartscanatc.svg"
cp "$SCRIPT_DIR/README.md" "$DEBROOT/usr/share/doc/$PKG/packaging-README.md"

INSTALLED_SIZE_KB="$(du -sk "$DEBROOT/opt/$PKG" | cut -f1)"

cat > "$DEBROOT/DEBIAN/control" <<CONTROL
Package: $PKG
Version: $VERSION
Section: hamradio
Priority: optional
Architecture: $ARCH
Installed-Size: $INSTALLED_SIZE_KB
Depends: libgl1, libegl1, libxkbcommon-x11-0, libxcb-cursor0, libasound2t64 | libasound2
Maintainer: SmartScanATC <noreply@example.com>
Description: Wideband SDR ATC/radio scanner (self-contained build)
 Scans a radio band via RTL-SDR and records audio only for transmissions
 the scanner actually stops on.
 .
 This is a self-contained ("fat") build: its own Python runtime and all
 pip-installable dependencies (PySide6, numpy, scipy, pyqtgraph,
 pyrtlsdrlib, sounddevice) are bundled in /opt/$PKG - nothing from the
 system Python is required. RTL-SDR hardware works out of the box.
 .
 Not included in this build: SoapySDR-based hardware (HackRF, LimeSDR,
 Airspy, SDRplay, USRP - needs a normal pip/source install instead), the
 in-app transcription installer, and MP3 output (needs a separately
 installed system ffmpeg). See /usr/share/doc/$PKG/packaging-README.md.
CONTROL

echo "-- 4/5: building the package --"
mkdir -p "$SCRIPT_DIR/out"
OUT="$SCRIPT_DIR/out/${PKG}_${VERSION}_${ARCH}.deb"
dpkg-deb --root-owner-group --build "$DEBROOT" "$OUT"

echo "-- 5/5: done --"
echo "Package: $OUT"
echo "Install with:   sudo apt install $OUT"
echo "Remove with:    sudo apt remove $PKG"
ls -lh "$OUT"
