#!/usr/bin/env python3
"""Backlog-Aufloesung beim Upgrade auf Catsync 2.0.

Laeuft auf einem Rechner, dessen Daemon gestoppt ist. Entfernt alle toten
Drive-Bindungen (sync_records, deren remote_id nicht mehr in remote_entries
steht), leert Tombstones und den Plan. Danach werden die betroffenen lokalen
Dateien beim naechsten Daemon-Start als neue Objekte nach Drive hochgeladen
statt geloescht.

Aufruf:
    systemctl --user stop catsyncd catsync-tray
    python3 resolve-backlog.py
    systemctl --user start catsyncd catsync-tray
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catsyncd.config import load_config  # noqa: E402


def main() -> int:
    cfg = load_config()
    db = sqlite3.connect(cfg.db_path)
    try:
        before = db.execute("SELECT COUNT(*) FROM sync_records").fetchone()[0]
        removed = db.execute(
            "DELETE FROM sync_records WHERE remote_id NOT IN (SELECT id FROM remote_entries)"
        ).rowcount
        db.execute("DELETE FROM delete_tombstones")
        db.execute("DELETE FROM plan_actions")
        db.commit()
        after = db.execute("SELECT COUNT(*) FROM sync_records").fetchone()[0]
    finally:
        db.close()

    print(f"sync_records: {before} -> {after} ({removed} tote Bindungen entfernt)")
    print("Tombstones und Plan geleert. Daemon wieder starten;")
    print("entbundene lokale Dateien werden neu hochgeladen.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
