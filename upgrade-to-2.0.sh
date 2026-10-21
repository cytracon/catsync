#!/usr/bin/env bash
# Catsync 2.0 Upgrade fuer bestehende 0.5.x-Installationen.
# Stoppt den Daemon, sichert, installiert 2.0 aus GitHub, passt die config an,
# entfernt tote Drive-Bindungen und startet den Daemon wieder.
#
# Aufruf auf dem Zielrechner:
#   curl -fsSL https://raw.githubusercontent.com/cytracon/catsync/main/upgrade-to-2.0.sh -o /tmp/upgrade-catsync.sh
#   bash /tmp/upgrade-catsync.sh
set -euo pipefail

echo "==> Catsync 2.0 Upgrade"

command -v git >/dev/null 2>&1 || { echo "FEHLER: git fehlt. Installieren mit: sudo pacman -S git"; exit 1; }
command -v rsync >/dev/null 2>&1 || { echo "FEHLER: rsync fehlt. Installieren mit: sudo pacman -S rsync"; exit 1; }

echo ""
echo "==> 1/6 Daemon stoppen"
systemctl --user stop catsyncd catsync-tray 2>/dev/null || true

echo "==> 2/6 Backup anlegen"
DEST="$HOME/catsync-rescue-backup-$(hostname)"
mkdir -p "$DEST"
cp -a ~/.config/catsyncd "$DEST/config-catsyncd"
cp -a ~/.local/state/catsyncd "$DEST/state-catsyncd"
echo "    Backup liegt in: $DEST"

echo "==> 3/6 catsync 2.0 aus GitHub installieren"
rm -rf /tmp/catsync2
git clone --depth 1 https://github.com/cytracon/catsync.git /tmp/catsync2
bash /tmp/catsync2/install-local.sh

echo "==> 4/6 config.json anpassen"
python3 - <<'EOF'
import json
from pathlib import Path
p = Path.home() / ".config/catsyncd/config.json"
data = json.loads(p.read_text())
data["allow_deletes"] = True
data["conflict_policy"] = "keep_both"
data["daemon_max_actions"] = 200
excl = set(data.get("excluded_sync_paths", []))
excl.add("BBachmann/AI/.deepcode")
data["excluded_sync_paths"] = sorted(excl)
ign = set(data.get("ignore_patterns", []))
ign.add("**/apktool/**")
data["ignore_patterns"] = sorted(ign)
p.write_text(json.dumps(data, indent=2) + "\n")
p.chmod(0o600)
print("    allow_deletes=true, conflict_policy=keep_both, daemon_max_actions=200")
EOF

echo "==> 5/6 tote Drive-Bindungen entfernen"
cd /tmp/catsync2
python3 resolve-backlog.py

echo "==> 6/6 Daemon wieder starten"
systemctl --user start catsyncd catsync-tray
sleep 3

echo ""
echo "==> Kontrolle:"
systemctl --user is-active catsyncd catsync-tray || true
"$HOME/.local/bin/catsyncd" --version || true

echo ""
echo "Fertig. Der Daemon arbeitet jetzt seinen Rueckstand ab (kann Stunden dauern)"
echo "und geht danach auf 'idle' mit leerem Plan."
