#!/usr/bin/env bash
# Installs the Malayalam Handwriting Linux app (Linux Mint / Ubuntu, X11 session):
# needed packages, an entry in the applications menu and a shortcut on the Desktop.
#   ./install.sh              install
#   ./install.sh --autostart  install, and also start the app (in the tray) at every login
#   ./install.sh --uninstall  remove the menu entry, the Desktop shortcut and the login entry
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$HERE/desktop/ml-handwriting.py"
MENU_FILE="$HOME/.local/share/applications/ml-handwriting.desktop"
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
DESKTOP_FILE="$DESKTOP_DIR/ml-handwriting.desktop"
AUTOSTART_FILE="$HOME/.config/autostart/ml-handwriting.desktop"

if [ "${1:-}" = "--uninstall" ]; then
    rm -f "$MENU_FILE" "$DESKTOP_FILE" "$AUTOSTART_FILE"
    echo "Removed the menu entry, the Desktop shortcut and the login entry. The app folder was left in place."
    exit 0
fi

[ -f "$APP" ] || { echo "Cannot find $APP. Run this script from inside the app folder." >&2; exit 1; }

# 1. Packages (only asks for sudo when something is missing)
PKGS=(python3 python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.1 python3-xlib gstreamer1.0-plugins-good)
if systemctl --user is-active --quiet pipewire 2>/dev/null; then
    PKGS+=(gstreamer1.0-pipewire)
else
    PKGS+=(gstreamer1.0-pulseaudio)
fi
MISSING=()
for p in "${PKGS[@]}"; do
    dpkg -s "$p" >/dev/null 2>&1 || MISSING+=("$p")
done
if [ "${#MISSING[@]}" -gt 0 ]; then
    echo "Installing: ${MISSING[*]}"
    sudo apt-get install -y "${MISSING[@]}"
else
    echo "All required packages are already installed."
fi

# 2. Check that Python can load everything
python3 - <<'PY'
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gtk, WebKit2  # noqa: F401
from Xlib import display  # noqa: F401
PY

# 3. Menu entry and Desktop shortcut
ENTRY="[Desktop Entry]
Type=Application
Name=Malayalam Handwriting
Comment=Handwriting and voice input that types into the window you were using
Exec=python3 \"$APP\"
Icon=$HERE/icon-512.png
Terminal=false
Categories=Utility;
StartupNotify=false"

mkdir -p "$(dirname "$MENU_FILE")"
printf '%s\n' "$ENTRY" > "$MENU_FILE"
echo "Menu entry:   $MENU_FILE"

if [ -d "$DESKTOP_DIR" ]; then
    printf '%s\n' "$ENTRY" > "$DESKTOP_FILE"
    chmod +x "$DESKTOP_FILE"
    gio set "$DESKTOP_FILE" metadata::trusted true 2>/dev/null || true   # lets Nemo launch it without asking
    echo "Desktop icon: $DESKTOP_FILE"
else
    echo "No Desktop folder found, so no Desktop shortcut was made."
fi

if [ "${1:-}" = "--autostart" ]; then
    mkdir -p "$(dirname "$AUTOSTART_FILE")"
    cat > "$AUTOSTART_FILE" <<AUTO
[Desktop Entry]
Type=Application
Name=Malayalam Handwriting
Comment=Handwriting and voice input (starts in the tray)
Exec=python3 "$APP" --hidden
Icon=$HERE/icon-512.png
Terminal=false
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=5
AUTO
    echo "Login entry:  $AUTOSTART_FILE (starts hidden in the tray; click its icon to show or hide the window)"
fi

if [ "${XDG_SESSION_TYPE:-x11}" = "wayland" ]; then
    echo "Note: this is a Wayland session. The app needs an X11 session to paste into other windows." >&2
fi

echo
echo "Done. Start 'Malayalam Handwriting' from the menu or the Desktop."
echo "First time only: open Voice settings in the app, paste your Gemini API key (Ctrl+V) and Save."
