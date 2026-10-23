#!/bin/sh
# FalconSDR one-line installer:
#
#   curl -fsSL https://raw.githubusercontent.com/rafafunt/FalconSDR/master/packaging/install.sh | bash
#
# Picks the right artifact for the machine it runs on:
#   - Debian/Ubuntu  -> official .deb from GitHub Releases
#   - Fedora/RHEL/openSUSE -> official .rpm from GitHub Releases
#   - Arch/CachyOS   -> AUR package `falconsdr` (yay/paru/pacman+makepkg fallback)
#   - anything else  -> the AppImage from GitHub Releases (no root needed)
#   - `--from-source` -> pip install from a git checkout instead (SoapySDR
#     hardware: HackRF/LimeSDR/Airspy/... - not in the packaged builds)
#
# After installing, unplug and replug the RTL-SDR dongle once (udev rule),
# then run `falconsdr` - the UI opens at http://127.0.0.1:8765.
set -eu

REPO="${FALCONSDR_REPO:-rafafunt/FalconSDR}"
VERSION="${FALCONSDR_VERSION:-latest}"
FROM_SOURCE=0
for a in "$@"; do [ "$a" = "--from-source" ] && FROM_SOURCE=1; done

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "need '$1' (install it first)"; }

release_tag() {
  # Resolves "latest" to e.g. v0.9.0 via the GitHub API; a pinned
  # FALCONSDR_VERSION=vX.Y.Z skips the network call.
  if [ "$VERSION" != "latest" ]; then printf '%s' "$VERSION"; return; fi
  need curl
  curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" |
    grep -m1 '"tag_name"' | cut -d'"' -f4
}

have_apt() { command -v apt-get >/dev/null 2>&1; }
have_dnf() { command -v dnf >/dev/null 2>&1; }
have_zypper() { command -v zypper >/dev/null 2>&1; }
have_pacman() { command -v pacman >/dev/null 2>&1; }

install_deb() {
  tag="$1"
  tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT INT TERM
  say "Downloading FalconSDR $tag (.deb)..."
  curl -fsSL -o "$tmp/falconsdr.deb" \
    "https://github.com/$REPO/releases/download/$tag/falconsdr_${tag#v}_amd64.deb"
  say "Installing (sudo apt install)..."
  sudo apt-get update -qq
  sudo apt install -y "$tmp/falconsdr.deb"
}

install_rpm() {
  tag="$1"; ver="${tag#v}"
  tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT INT TERM
  arch="$(uname -m)"
  say "Downloading FalconSDR $tag (.rpm)..."
  curl -fsSL -o "$tmp/falconsdr.rpm" \
    "https://github.com/$REPO/releases/download/$tag/falconsdr-${ver}-1.${arch}.rpm"
  if have_dnf; then sudo dnf install -y "$tmp/falconsdr.rpm"
  elif have_zypper; then sudo zypper --no-confirm install "$tmp/falconsdr.rpm"
  else sudo rpm -i "$tmp/falconsdr.rpm"; fi
}

install_aur() {
  if command -v yay >/dev/null 2>&1; then yay -S --noconfirm falconsdr
  elif command -v paru >/dev/null 2>&1; then paru -S --noconfirm falconsdr
  else
    say "No AUR helper found - building with makepkg..."
    need git; need makepkg; need sudo
    tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT INT TERM
    git clone --depth 1 "https://aur.archlinux.org/falconsdr.git" "$tmp/falconsdr"
    (cd "$tmp/falconsdr" && makepkg -si --noconfirm)
  fi
}

install_appimage() {
  tag="$1"
  arch="$(uname -m)"
  dest="${FALCONSDR_APPIMAGE:-$HOME/Applications/FalconSDR-${tag}-${arch}.AppImage}"
  mkdir -p "$(dirname "$dest")"
  say "Downloading FalconSDR $tag (AppImage)..."
  curl -fsSL -o "$dest" \
    "https://github.com/$REPO/releases/download/$tag/FalconSDR-${tag#v}-${arch}.AppImage"
  chmod +x "$dest"
  say "AppImage saved to $dest"
  if [ "$(id -u)" = "0" ]; then :
  elif [ ! -f /etc/udev/rules.d/60-rtl-sdr.rules ] && [ ! -f /usr/lib/udev/rules.d/60-rtl-sdr.rules ]; then
    cat <<EOF

RTL-SDR USB access needs one manual step (AppImages install nothing):
  sudo curl -fsSL -o /etc/udev/rules.d/60-rtl-sdr.rules \\
    https://raw.githubusercontent.com/$REPO/master/packaging/linux-deb/60-rtl-sdr.rules
  sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=usb
then unplug and replug the dongle once.
EOF
  fi
  say "Run it with: $dest"
}

install_from_source() {
  say "Installing FalconSDR from source (SoapySDR-capable path)..."
  need python3; need git
  if [ ! -d FalconSDR ]; then git clone "https://github.com/$REPO.git"; fi
  cd FalconSDR
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
  cat <<EOF

Done. Start it with:
  .venv/bin/python main.py
(RTL-SDR udev rule: packaging/linux-deb/60-rtl-sdr.rules - see the README.
SoapySDR hardware: install python3-soapysdr + soapysdr-module-<device>.)
EOF
}

main() {
  if [ "$FROM_SOURCE" = "1" ]; then install_from_source; return; fi
  if have_apt; then install_deb "$(release_tag)"
  elif have_dnf || have_zypper; then install_rpm "$(release_tag)"
  elif have_pacman; then install_aur
  else install_appimage "$(release_tag)"; fi
  cat <<EOF

Unplug and replug the RTL-SDR dongle once (USB permission rule), then run:
  falconsdr
The UI opens at http://127.0.0.1:8765.
EOF
}

main
