#!/usr/bin/env python3
"""End-to-End-Test fuer catsync 2.0 gegen einen dedizierten Drive-Testordner.

Testet die Kern-Fixes mit echten Drive-Operationen, aber ausschliesslich in
einem frisch angelegten Ordner "catsync2-e2e-test-<ts>" (wird am Ende getrasht).
Das Live-System (~/Google Drive) wird nicht angefasst.

Szenarien:
  1. Erst-Pairing: lokal -> Drive (Upload), Drive -> lokal (Download)
  2. Remote-Edit -> Download; Folge-Tick darf NICHT zurueck-uploaden (Ping-Pong-Fix)
  3. Lokaler Edit -> Upload
  4. Gleichzeitiger Edit (lokal + remote ohne Sync) -> Keep-Both:
     Remote-Version als deterministische Konfliktkopie, lokale Version als Main
  5. Lokale Loeschung -> remote_trash (Drive-Papierkorb)
  6. Remote-Trash -> local_delete (lokale .catsync/trash)
  7. plan_actions speichert remote_id (kein ID-Verlust)
"""

from __future__ import annotations

import hashlib
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catsyncd.auth import refresh_access_token, valid_access_token  # noqa: E402
from catsyncd.config import Config  # noqa: E402
from catsyncd.drive import DriveClient, DriveFileNotFound  # noqa: E402
from catsyncd.engine import SyncEngine  # noqa: E402
from catsyncd.state import State  # noqa: E402

HOME = Path.home()
CLIENT_SECRET = HOME / ".config/catsyncd/client_secret.json"
TOKEN = HOME / ".config/catsyncd/token.json"
BASE = HOME / "catsync2-e2e-root"
NAME = f"catsync2-e2e-test-{int(time.time())}"
DB_PATH = HOME / ".local/state/catsync2-e2e" / "state.db"
FOLDER_ID: str | None = None


def noop(*_args, **_kwargs):
    return None


def make_drive() -> DriveClient:
    token = valid_access_token(CLIENT_SECRET, TOKEN)

    def refresh() -> str:
        fresh = refresh_access_token(CLIENT_SECRET, TOKEN)
        return str(fresh["access_token"])

    return DriveClient(token, refresh_access_token=refresh)


def read(p: Path) -> str:
    return p.read_text()


def write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def tick_until(engine: SyncEngine, predicate, tries: int = 8, sleep: float = 2.5) -> dict:
    """sync_tick(force=True) wiederholen, bis predicate(summary) wahr ist."""
    summary: dict = {}
    for _ in range(tries):
        summary = engine.sync_tick(noop, force=True)
        if predicate(summary):
            return summary
        time.sleep(sleep)
    return summary


def remote_children(drive: DriveClient, folder_id: str) -> dict[str, dict]:
    return {item.get("name"): item for item in drive.iter_children(folder_id) if item.get("id")}


def main() -> int:
    global FOLDER_ID
    drive = make_drive()

    # ---------------------------------------------------------------- Setup
    shutil.rmtree(BASE, ignore_errors=True)
    shutil.rmtree(DB_PATH.parent, ignore_errors=True)
    (BASE / NAME / "docs").mkdir(parents=True)
    write(BASE / NAME / "hello.txt", "hello v1")
    write(BASE / NAME / "docs" / "notes.md", "notes v1")

    FOLDER_ID = drive.create_folder(NAME, "root")["id"]
    remote_only_src = Path("/tmp/catsync2-remote-only.txt")
    remote_only_src.write_text("remote v1")
    drive.upload_file(remote_only_src, "remote-only.txt", FOLDER_ID)
    print(f"Setup: Drive-Testordner {NAME} angelegt ({FOLDER_ID})")

    cfg = Config(
        sync_root=BASE,
        client_secret_path=CLIENT_SECRET,
        token_path=TOKEN,
        db_path=DB_PATH,
        poll_interval=60,
        allow_writes=True,
        allow_deletes=True,
        dry_run=False,
        include_shared_drives=False,
        hash_limit_bytes=64 * 1024 * 1024,
        launch_daemon_at_login=False,
        launch_tray_at_login=False,
        desktop_notifications=False,
        conflict_policy="keep_both",
        google_docs_mode="placeholder",
        proxy_mode="system",
        proxy_host="",
        proxy_port=0,
        upload_limit_kbps=0,
        download_limit_kbps=0,
        ignore_patterns=("*.tmp",),
        selective_sync_paths=(NAME,),
        excluded_sync_paths=(),
        sync_symlinks=False,
        daemon_max_actions=200,
    )
    engine = SyncEngine(cfg, should_stop=lambda: False)
    engine.bootstrap(noop)  # bootstrap fuehrt intern bereits den ersten sync_tick aus

    # ---------------------------------------------------------------- 1. Erst-Pairing
    children = remote_children(drive, FOLDER_ID)
    assert "hello.txt" in children, f"hello.txt fehlt auf Drive: {children.keys()}"
    assert "docs" in children, f"docs fehlt auf Drive: {children.keys()}"
    assert (BASE / NAME / "remote-only.txt").exists(), "remote-only.txt wurde nicht heruntergeladen"
    assert read(BASE / NAME / "remote-only.txt") == "remote v1"
    print("1 ok: Erst-Pairing (Upload + Download)")

    # ---------------------------------------------------------------- 2. Remote-Edit -> Download, kein Ping-Pong
    hello_id = remote_children(drive, FOLDER_ID)["hello.txt"]["id"]
    v2 = Path("/tmp/catsync2-hello-v2.txt")
    v2.write_text("hello v2")
    drive.update_file_content(hello_id, v2)

    def hello_has_v2(_s):
        return read(BASE / NAME / "hello.txt") == "hello v2"

    tick_until(engine, hello_has_v2)
    assert read(BASE / NAME / "hello.txt") == "hello v2", "Remote-Edit kam nicht lokal an"

    summary2 = engine.sync_tick(noop, force=True)
    assert summary2.get("upload_update", 0) == 0, f"Ping-Pong: Re-Upload nach Download geplant: {summary2}"
    assert summary2.get("download_update", 0) == 0, f"Unnoetiger Re-Download: {summary2}"
    print("2 ok: Remote-Edit -> Download, kein Ping-Pong")

    # ---------------------------------------------------------------- 3. Lokaler Edit -> Upload
    write(BASE / NAME / "hello.txt", "hello v3")
    summary3 = tick_until(engine, lambda s: s.get("upload_update", 0) >= 1)
    assert drive.download_bytes(hello_id) == b"hello v3", "Lokaler Edit kam nicht auf Drive an"
    print("3 ok: Lokaler Edit -> Upload")

    # ---------------------------------------------------------------- 4. Gleichzeitiger Edit -> Keep-Both
    write(BASE / NAME / "hello.txt", "hello local-v4")
    v4 = Path("/tmp/catsync2-hello-v4.txt")
    v4.write_text("hello remote-v4")
    drive.update_file_content(hello_id, v4)

    def conflict_done(_s):
        return any(
            p.name.startswith("hello.catsync-conflict-remote-")
            for p in (BASE / NAME).iterdir()
            if p.is_file()
        )

    tick_until(engine, conflict_done, tries=10)
    assert read(BASE / NAME / "hello.txt") == "hello local-v4", "Lokale Version wurde ueberschrieben"
    assert drive.download_bytes(hello_id) == b"hello local-v4", "Lokale Version wurde nicht Main auf Drive"
    conflict_files = [p for p in (BASE / NAME).iterdir() if p.name.startswith("hello.catsync-conflict-remote-")]
    assert len(conflict_files) == 1, f"Konfliktkopie fehlt oder mehrfach: {conflict_files}"
    assert conflict_files[0].read_text() == "hello remote-v4", "Konfliktkopie enthaelt falschen Inhalt"
    remote_md5 = hashlib.md5(b"hello remote-v4").hexdigest()
    assert remote_md5[:10] in conflict_files[0].name, "Konfliktname nicht deterministisch (md5-basiert)"
    print("4 ok: Gleichzeitiger Edit -> Keep-Both, deterministische Konfliktkopie")

    # Konfliktkopie wird im naechsten Tick als neues Drive-Objekt hochgeladen
    def conflict_on_drive(_s):
        return any(k.startswith("hello.catsync-conflict-remote-") for k in remote_children(drive, FOLDER_ID))

    tick_until(engine, conflict_on_drive, tries=10)
    assert conflict_on_drive({}), "Konfliktkopie wurde nicht auf Drive hochgeladen"
    print("4b ok: Konfliktkopie auf Drive hochgeladen (beide Versionen im Umlauf)")

    # ---------------------------------------------------------------- 5. Lokale Loeschung -> remote_trash
    (BASE / NAME / "remote-only.txt").unlink()
    summary5 = tick_until(engine, lambda s: s.get("remote_trash", 0) >= 1)
    assert "remote-only.txt" not in remote_children(drive, FOLDER_ID), "remote_trash wurde nicht ausgefuehrt"
    print("5 ok: Lokale Loeschung -> remote_trash (Drive-Papierkorb)")

    # ---------------------------------------------------------------- 6. Remote-Trash -> local_delete
    notes_id = remote_children(drive, FOLDER_ID)["docs"]["id"]
    notes_children = {i.get("name"): i for i in drive.iter_children(notes_id)}
    drive.trash_file(notes_children["notes.md"]["id"])

    def notes_gone_local(_s):
        return not (BASE / NAME / "docs" / "notes.md").exists()

    tick_until(engine, notes_gone_local, tries=10)
    assert not (BASE / NAME / "docs" / "notes.md").exists(), "local_delete hat notes.md nicht entfernt"
    trash_hits = list((BASE / ".catsync" / "trash").rglob("notes.md"))
    assert trash_hits, "notes.md liegt nicht in .catsync/trash"
    print("6 ok: Remote-Trash -> local_delete (lokale .catsync/trash)")

    # ---------------------------------------------------------------- 7. remote_id-Persistenz
    state = State(DB_PATH)
    try:
        plan_rows = state.list_plan_actions(limit=500)
        delete_actions = [a for a in plan_rows if a.action in {"local_delete", "remote_trash"}]
        assert delete_actions, "Keine Delete-Aktion im Plan gespeichert"
        assert all(a.remote_id for a in delete_actions), f"remote_id fehlt im Plan: {delete_actions}"
    finally:
        state.close()
    print("7 ok: plan_actions speichert remote_id (kein ID-Verlust)")

    # ---------------------------------------------------------------- Cleanup
    drive.trash_file(FOLDER_ID)
    shutil.rmtree(BASE, ignore_errors=True)
    shutil.rmtree(DB_PATH.parent, ignore_errors=True)
    print(f"\nALLE E2E-TESTS OK — Testordner {NAME} wurde in die Drive-Trash verschoben.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
