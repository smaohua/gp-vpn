#!/usr/bin/env bash
# Install OpenConnect GUI for the current user (no root needed).
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_PY="$SRC_DIR/openconnect_gui.py"

BIN_DIR="$HOME/.local/bin"
APP_DIR="$HOME/.local/share/openconnect-gui"
DESKTOP_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
LAUNCHER="$BIN_DIR/openconnect-gui"

echo "==> Checking dependencies"
missing=()
command -v openconnect >/dev/null 2>&1 || missing+=("openconnect")
python3 -c "import PyQt6" 2>/dev/null || missing+=("python-pyqt6")
if ((${#missing[@]})); then
  echo "Missing packages: ${missing[*]}"
  echo "Install them with:  sudo pacman -S --needed ${missing[*]}"
  exit 1
fi

echo "==> Installing application files"
mkdir -p "$BIN_DIR" "$APP_DIR" "$DESKTOP_DIR" "$ICON_DIR"
install -m 0755 "$APP_PY" "$APP_DIR/openconnect_gui.py"

cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
exec python3 "$APP_DIR/openconnect_gui.py" "\$@"
EOF
chmod 0755 "$LAUNCHER"

# A simple scalable icon.
cat > "$ICON_DIR/openconnect-gui.svg" <<'SVG'
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <circle cx="32" cy="32" r="26" fill="#2861d4" stroke="#1e1e1e" stroke-width="3"/>
  <text x="32" y="42" font-family="sans-serif" font-size="30"
        font-weight="bold" fill="#fff" text-anchor="middle">V</text>
</svg>
SVG

echo "==> Installing desktop entry"
sed -e "s|__EXEC__|$LAUNCHER|" -e "s|__ICON__|openconnect-gui|" \
    "$SRC_DIR/openconnect-gui.desktop" > "$DESKTOP_DIR/openconnect-gui.desktop"
chmod 0644 "$DESKTOP_DIR/openconnect-gui.desktop"
update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true

echo
echo "Done. Launch it from your app menu ('OpenConnect VPN') or run:"
echo "    $LAUNCHER"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "Note: $BIN_DIR is not on your PATH; add it or use the full path." ;;
esac
