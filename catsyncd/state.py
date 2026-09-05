from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Iterable

from .models import DeleteTombstone, LocalEntry, PlanAction, RemoteEntry, SyncRecord


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS local_entries (
  path TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  size INTEGER,
  mtime_ns INTEGER,
  md5 TEXT,
  sha256 TEXT,
  scan_time INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS remote_entries (
  id TEXT PRIMARY KEY,
  path TEXT NOT NULL,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,
  mime_type TEXT NOT NULL,
  parents_json TEXT NOT NULL,
  trashed INTEGER NOT NULL,
  modified_time TEXT,
  md5 TEXT,
  size INTEGER,
  web_view_link TEXT,
  scan_time INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_remote_entries_path ON remote_entries(path);
CREATE TABLE IF NOT EXISTS plan_actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  action TEXT NOT NULL,
  path TEXT NOT NULL,
  reason TEXT NOT NULL,
  local_json TEXT,
  remote_json TEXT,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_records (
  remote_id TEXT PRIMARY KEY,
  local_path TEXT NOT NULL,
  remote_path TEXT NOT NULL,
  kind TEXT NOT NULL,
  local_md5 TEXT,
  remote_md5 TEXT,
  local_size INTEGER,
  remote_size INTEGER,
  local_mtime_ns INTEGER,
  remote_modified_time TEXT,
  updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sync_records_local_path ON sync_records(local_path);
CREATE TABLE IF NOT EXISTS delete_tombstones (
  path TEXT PRIMARY KEY,
  remote_id TEXT,
  remote_md5 TEXT,
  remote_size INTEGER,
  remote_modified_time TEXT,
  deleted_at INTEGER NOT NULL
);
"""


class State:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()

    def close(self) -> None:
        self.db.close()

    def _migrate(self) -> None:
        self._ensure_column("local_entries", "md5", "TEXT")

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        rows = self.db.execute(f"PRAGMA table_info({table})").fetchall()
        if column not in {row["name"] for row in rows}:
            self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            self.db.commit()

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.db.commit()

    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def replace_local(self, entries: Iterable[LocalEntry]) -> None:
        now = int(time.time())
        with self.db:
            self.db.execute("DELETE FROM local_entries")
            self.db.executemany(
                """
                INSERT INTO local_entries(path,kind,size,mtime_ns,md5,sha256,scan_time)
                VALUES(?,?,?,?,?,?,?)
                """,
                [(e.path, e.kind, e.size, e.mtime_ns, e.md5, e.sha256, now) for e in entries],
            )

    def replace_remote(self, entries: Iterable[RemoteEntry]) -> None:
        now = int(time.time())
        with self.db:
            self.db.execute("DELETE FROM remote_entries")
            self.db.executemany(
                """
                INSERT INTO remote_entries(id,path,name,kind,mime_type,parents_json,trashed,modified_time,md5,size,web_view_link,scan_time)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        e.id,
                        e.path,
                        e.name,
                        e.kind,
                        e.mime_type,
                        json.dumps(e.parents),
                        int(e.trashed),
                        e.modified_time,
                        e.md5,
                        e.size,
                        e.web_view_link,
                        now,
                    )
                    for e in entries
                ],
            )

    def replace_plan(self, actions: Iterable[PlanAction]) -> None:
        now = int(time.time())
        with self.db:
            self.db.execute("DELETE FROM plan_actions")
            self.db.executemany(
                """
                INSERT INTO plan_actions(action,path,reason,local_json,remote_json,created_at)
                VALUES(?,?,?,?,?,?)
                """,
                [
                    (
                        a.action,
                        a.path,
                        a.reason,
                        _json_dataclass(a.local),
                        _json_dataclass(a.remote),
                        now,
                    )
                    for a in actions
                ],
            )

    def plan_summary(self) -> dict[str, int]:
        rows = self.db.execute("SELECT action, COUNT(*) AS count FROM plan_actions GROUP BY action").fetchall()
        return {row["action"]: int(row["count"]) for row in rows}

    def conflict_actions(self) -> list[PlanAction]:
        rows = self.db.execute(
            """
            SELECT action,path,reason,local_json,remote_json
            FROM plan_actions
            WHERE action IN ('conflict_copy_local', 'conflict_review')
            ORDER BY action,path
            """
        ).fetchall()
        return [_plan_action_from_row(row) for row in rows]

    def plan_action(self, path: str, actions: tuple[str, ...] = ("conflict_copy_local", "conflict_review")) -> PlanAction | None:
        placeholders = ",".join("?" for _ in actions)
        row = self.db.execute(
            f"""
            SELECT action,path,reason,local_json,remote_json
            FROM plan_actions
            WHERE path=? AND action IN ({placeholders})
            ORDER BY id
            LIMIT 1
            """,
            (path, *actions),
        ).fetchone()
        return _plan_action_from_row(row) if row else None

    def list_plan_actions(self, actions: tuple[str, ...] | None = None, limit: int = 500) -> list[PlanAction]:
        params: list[object] = []
        where = ""
        if actions:
            placeholders = ",".join("?" for _ in actions)
            where = f"WHERE action IN ({placeholders})"
            params.extend(actions)
        params.append(limit)
        rows = self.db.execute(
            f"""
            SELECT action,path,reason,local_json,remote_json
            FROM plan_actions
            {where}
            ORDER BY id
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [_plan_action_from_row(row) for row in rows]

    def delete_plan_action(self, action: str, path: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM plan_actions WHERE action=? AND path=?", (action, path))

    def remote_folder_paths(self) -> list[str]:
        rows = self.db.execute(
            """
            SELECT path, parents_json
            FROM remote_entries
            WHERE kind='folder' AND trashed=0 AND path != ''
            ORDER BY path
            """
        ).fetchall()
        parentless_top_level = {
            row["path"]
            for row in rows
            if "/" not in row["path"] and _json_list_empty(row["parents_json"])
        }
        paths: list[str] = []
        for row in rows:
            path = row["path"]
            top_level = path.split("/", 1)[0]
            if top_level in parentless_top_level:
                continue
            paths.append(path)
        return paths

    def get_sync_records(self) -> list[SyncRecord]:
        rows = self.db.execute(
            """
            SELECT local_path,remote_id,remote_path,kind,local_md5,remote_md5,local_size,remote_size,local_mtime_ns,remote_modified_time
            FROM sync_records
            """
        ).fetchall()
        return [
            SyncRecord(
                local_path=row["local_path"],
                remote_id=row["remote_id"],
                remote_path=row["remote_path"],
                kind=row["kind"],
                local_md5=row["local_md5"],
                remote_md5=row["remote_md5"],
                local_size=row["local_size"],
                remote_size=row["remote_size"],
                local_mtime_ns=row["local_mtime_ns"],
                remote_modified_time=row["remote_modified_time"],
            )
            for row in rows
        ]

    def get_remote_entries(self) -> list[RemoteEntry]:
        rows = self.db.execute(
            """
            SELECT id,path,name,kind,mime_type,parents_json,trashed,modified_time,md5,size,web_view_link
            FROM remote_entries
            """
        ).fetchall()
        return [
            RemoteEntry(
                id=row["id"],
                path=row["path"],
                name=row["name"],
                kind=row["kind"],
                mime_type=row["mime_type"],
                parents=tuple(_json_list(row["parents_json"])),
                trashed=bool(row["trashed"]),
                modified_time=row["modified_time"],
                md5=row["md5"],
                size=row["size"],
                web_view_link=row["web_view_link"],
            )
            for row in rows
        ]

    def get_local_entries(self) -> list[LocalEntry]:
        rows = self.db.execute(
            """
            SELECT path,kind,size,mtime_ns,md5,sha256
            FROM local_entries
            """
        ).fetchall()
        return [
            LocalEntry(
                path=row["path"],
                kind=row["kind"],
                size=row["size"],
                mtime_ns=row["mtime_ns"],
                md5=row["md5"],
                sha256=row["sha256"],
            )
            for row in rows
        ]

    def get_delete_tombstones(self) -> list[DeleteTombstone]:
        rows = self.db.execute(
            """
            SELECT path,remote_id,remote_md5,remote_size,remote_modified_time,deleted_at
            FROM delete_tombstones
            """
        ).fetchall()
        return [
            DeleteTombstone(
                path=row["path"],
                remote_id=row["remote_id"],
                remote_md5=row["remote_md5"],
                remote_size=row["remote_size"],
                remote_modified_time=row["remote_modified_time"],
                deleted_at=int(row["deleted_at"]),
            )
            for row in rows
        ]

    def replace_delete_tombstones(self, tombstones: Iterable[DeleteTombstone]) -> None:
        with self.db:
            self.db.execute("DELETE FROM delete_tombstones")
            self.db.executemany(
                """
                INSERT INTO delete_tombstones(
                  path,remote_id,remote_md5,remote_size,remote_modified_time,deleted_at
                )
                VALUES(?,?,?,?,?,?)
                """,
                [
                    (
                        item.path,
                        item.remote_id,
                        item.remote_md5,
                        item.remote_size,
                        item.remote_modified_time,
                        item.deleted_at,
                    )
                    for item in tombstones
                ],
            )

    def upsert_sync_records(self, records: Iterable[SyncRecord]) -> None:
        now = int(time.time())
        with self.db:
            self.db.executemany(
                """
                INSERT INTO sync_records(
                  remote_id,local_path,remote_path,kind,local_md5,remote_md5,local_size,remote_size,local_mtime_ns,remote_modified_time,updated_at
                )
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(remote_id) DO UPDATE SET
                  local_path=excluded.local_path,
                  remote_path=excluded.remote_path,
                  kind=excluded.kind,
                  local_md5=excluded.local_md5,
                  remote_md5=excluded.remote_md5,
                  local_size=excluded.local_size,
                  remote_size=excluded.remote_size,
                  local_mtime_ns=excluded.local_mtime_ns,
                  remote_modified_time=excluded.remote_modified_time,
                  updated_at=excluded.updated_at
                """,
                [
                    (
                        r.remote_id,
                        r.local_path,
                        r.remote_path,
                        r.kind,
                        r.local_md5,
                        r.remote_md5,
                        r.local_size,
                        r.remote_size,
                        r.local_mtime_ns,
                        r.remote_modified_time,
                        now,
                    )
                    for r in records
                ],
            )

    def delete_sync_records(self, remote_ids: Iterable[str]) -> None:
        ids = list(remote_ids)
        if not ids:
            return
        with self.db:
            self.db.executemany("DELETE FROM sync_records WHERE remote_id=?", [(item,) for item in ids])

    def replace_sync_records(self, records: Iterable[SyncRecord]) -> None:
        now = int(time.time())
        with self.db:
            self.db.execute("DELETE FROM sync_records")
            self.db.executemany(
                """
                INSERT INTO sync_records(
                  remote_id,local_path,remote_path,kind,local_md5,remote_md5,local_size,remote_size,local_mtime_ns,remote_modified_time,updated_at
                )
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        r.remote_id,
                        r.local_path,
                        r.remote_path,
                        r.kind,
                        r.local_md5,
                        r.remote_md5,
                        r.local_size,
                        r.remote_size,
                        r.local_mtime_ns,
                        r.remote_modified_time,
                        now,
                    )
                    for r in records
                ],
            )


def _json_dataclass(value: object | None) -> str | None:
    if value is None:
        return None
    data = dict(value.__dict__)
    if "parents" in data:
        data["parents"] = list(data["parents"])
    return json.dumps(data, sort_keys=True)


def _plan_action_from_row(row) -> PlanAction:
    local = _local_from_json(row["local_json"])
    remote = _remote_from_json(row["remote_json"])
    return PlanAction(
        action=row["action"],
        path=row["path"],
        reason=row["reason"],
        local=local,
        remote=remote,
        remote_id=remote.id if remote else None,
    )


def _local_from_json(value: str | None) -> LocalEntry | None:
    if not value:
        return None
    data = json.loads(value)
    return LocalEntry(
        path=data["path"],
        kind=data["kind"],
        size=data.get("size"),
        mtime_ns=data.get("mtime_ns"),
        md5=data.get("md5"),
        sha256=data.get("sha256"),
    )


def _remote_from_json(value: str | None) -> RemoteEntry | None:
    if not value:
        return None
    data = json.loads(value)
    return RemoteEntry(
        id=data["id"],
        path=data["path"],
        name=data["name"],
        kind=data["kind"],
        mime_type=data["mime_type"],
        parents=tuple(data.get("parents") or ()),
        trashed=bool(data.get("trashed")),
        modified_time=data.get("modified_time"),
        md5=data.get("md5"),
        size=data.get("size"),
        web_view_link=data.get("web_view_link"),
    )


def _json_list(value: str | None) -> list:
    if not value:
        return []
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _json_list_empty(value: str) -> bool:
    return _json_list(value) == []
