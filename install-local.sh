#!/usr/bin/env bash
# Install Catsync for the current user on Omarchy (Arch). No root required
# unless optional packages are missing. Does not copy ~/.config/catsyncd.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICON_BASE="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor"
LIB_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/catsyncd"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
CFG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/catsyncd"

echo "==> Catsync (Omarchy / Arch, user-local)"
mkdir -p "$BIN_DIR" "$APP_DIR" "$LIB_DIR" "$LIB_DIR/icons" "$UNIT_DIR"

rsync -a --delete \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude 'client_secret.json' \
  --exclude 'token.json' \
  --exclude 'config.json' \
  "$ROOT/catsyncd/" "$LIB_DIR/catsyncd/"

if [[ -d "$ROOT/data/icons" ]]; then
  cp -f "$ROOT/data/icons/"*.svg "$LIB_DIR/icons/" 2>/dev/null || true
fi

cat > "$BIN_DIR/catsyncd" <<EOF
#!/usr/bin/env bash
set -euo pipefail
LIB_LOCAL="$LIB_DIR"
export PYTHONPATH="\${LIB_LOCAL}\${PYTHONPATH:+:\$PYTHONPATH}"
exec python3 -m catsyncd "\$@"
EOF
chmod 0755 "$BIN_DIR/catsyncd"

cat > "$BIN_DIR/catsync-tray" <<EOF
#!/usr/bin/env bash
set -euo pipefail
LIB_LOCAL="$LIB_DIR"
export PYTHONPATH="\${LIB_LOCAL}\${PYTHONPATH:+:\$PYTHONPATH}"
exec python3 -m catsyncd.tray "\$@"
EOF
chmod 0755 "$BIN_DIR/catsync-tray"

cat > "$BIN_DIR/catsync" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
cmd="${1:-start}"
case "$cmd" in
  start|tray)
    exec "${HOME}/.local/bin/catsync-tray"
    ;;
  settings)
    exec "${HOME}/.local/bin/catsync-tray" --settings
    ;;
  setup)
    exec "${HOME}/.local/bin/catsync-tray" --setup
    ;;
  open-cloud)
    shift
    if [ "$#" -gt 0 ]; then
      exec xdg-open "$1"
    fi
    exec "${HOME}/.local/bin/catsync-tray" --settings
    ;;
  *)
    exec "${HOME}/.local/bin/catsyncd" "$@"
    ;;
esac
EOF
chmod 0755 "$BIN_DIR/catsync"

for svg in catsync.svg catsyncd-tray.svg catsyncd-tray-busy.svg catsyncd-tray-stopped.svg; do
  src="$ROOT/data/icons/$svg"
  if [[ -f "$src" ]]; then
    install -D -m 644 "$src" "$ICON_BASE/scalable/apps/$svg"
  fi
done
# App id alias
if [[ -f "$ROOT/data/icons/catsync.svg" ]]; then
  install -D -m 644 "$ROOT/data/icons/catsync.svg" "$ICON_BASE/scalable/apps/com.bbachmann.catsync.svg"
fi

install -m 644 "$ROOT/data/systemd/catsyncd.service" "$UNIT_DIR/catsyncd.service"
install -m 644 "$ROOT/data/systemd/catsync-tray.service" "$UNIT_DIR/catsync-tray.service"

install -m 644 "$ROOT/data/catsync.desktop" "$APP_DIR/catsync.desktop"
install -m 644 "$ROOT/data/catsync-settings.desktop" "$APP_DIR/catsync-settings.desktop"
install -m 644 "$ROOT/data/catsync-helper.desktop" "$APP_DIR/catsync-helper.desktop"
sed -i "s|^Exec=catsync-tray|Exec=$BIN_DIR/catsync-tray|" "$APP_DIR/catsync.desktop" "$APP_DIR/catsync-settings.desktop"
sed -i "s|^TryExec=catsync-tray|TryExec=$BIN_DIR/catsync-tray|" "$APP_DIR/catsync.desktop" "$APP_DIR/catsync-settings.desktop"
sed -i "s|^Exec=catsync |Exec=$BIN_DIR/catsync |" "$APP_DIR/catsync-helper.desktop"
sed -i "s|^TryExec=catsync|TryExec=$BIN_DIR/catsync|" "$APP_DIR/catsync-helper.desktop"

need=()
python3 -c "import gi; gi.require_version('Gtk','3.0')" 2>/dev/null || need+=(python-gobject gtk3)
python3 -c "import gi; gi.require_version('AyatanaAppIndicator3','0.1')" 2>/dev/null \
  || need+=(libayatana-appindicator)

if ((${#need[@]})); then
  echo "==> Missing packages: ${need[*]}"
  if command -v omarchy >/dev/null 2>&1; then
    echo "    Installing with omarchy pkg add (sudo may be requested)…"
    omarchy pkg add "${need[@]}"
  else
    echo "    Install with: sudo pacman -S --needed ${need[*]}"
  fi
fi

if command -v update-desktop-database >/dev/null; then
  update-desktop-database "$APP_DIR" 2>/dev/null || true
fi
if command -v gtk-update-icon-cache >/dev/null; then
  gtk-update-icon-cache -f -t "$ICON_BASE" 2>/dev/null || true
fi

systemctl --user daemon-reload

if [[ -f "$CFG_DIR/config.json" ]]; then
  echo "==> Keeping existing user config $CFG_DIR (not overwritten)"
else
  echo "==> No config yet. Copy data/config.json.example to $CFG_DIR/config.json and fill in locally."
  echo "    Then: catsyncd import-oauth-client <client_secret.json> && catsyncd auth"
fi

if [[ -f "$CFG_DIR/token.json" ]]; then
  systemctl --user enable catsyncd.service
  systemctl --user restart catsyncd.service || systemctl --user start catsyncd.service || true
  echo "==> Enabled systemd --user catsyncd.service"
else
  systemctl --user enable catsyncd.service >/dev/null 2>&1 || true
  echo "==> Daemon unit installed (starts after Google auth: catsyncd auth)"
fi

# Bar plugin: status + start/stop. Idempotent if already added from the marketplace.
PLUGIN_ID="io.github.cytracon.catsync"
PLUGIN_URL="https://github.com/cytracon/omarchy-catsync.git"
PLUGIN_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/plugins/${PLUGIN_ID}"
if command -v omarchy >/dev/null 2>&1; then
  if [[ -d "$PLUGIN_DIR" ]]; then
    omarchy plugin enable "$PLUGIN_ID" --section right >/dev/null 2>&1 || true
    echo "==> Bar plugin $PLUGIN_ID enabled"
  elif omarchy plugin add "$PLUGIN_URL" --enable --yes; then
    omarchy bar move "$PLUGIN_ID" --section right >/dev/null 2>&1 || true
    echo "==> Bar plugin $PLUGIN_ID installed"
  fi
fi

# Tray is optional on Omarchy (bar plugin is the primary UI). Enable if already wanted.
if systemctl --user is-enabled catsync-tray.service >/dev/null 2>&1; then
  systemctl --user restart catsync-tray.service || true
fi

# Leftover Ubuntu /usr/bin wrappers
WRAP="$ROOT/omarchy/catsync-usr-wrapper"
if [[ -x "$WRAP" ]]; then
  names=()
  for name in catsync catsyncd catsync-tray; do
    if [[ -e "/usr/bin/$name" ]] && ! grep -q 'Omarchy build' "/usr/bin/$name" 2>/dev/null; then
      names+=("$name")
    fi
  done
  if ((${#names[@]})); then
    echo "==> Replacing leftover Ubuntu ${names[*]} under /usr…"
    if pkexec bash -c "for n in ${names[*]}; do install -m 755 '$WRAP' \"/usr/bin/\$n\"; done"; then
      echo "==> /usr/bin/{${names[*]}} now launch ~/.local"
    else
      echo "==> skipped (no polkit auth). sudo install -m 755 $WRAP /usr/bin/<name>"
    fi
  fi
fi

echo
echo "Installed:"
echo "  binary : $BIN_DIR/catsyncd  $BIN_DIR/catsync-tray  $BIN_DIR/catsync"
echo "  package: $LIB_DIR"
echo "  desktop: $APP_DIR/catsync.desktop"
echo "Start with:  catsyncd --status"
echo "Settings:    catsync-tray --settings"
