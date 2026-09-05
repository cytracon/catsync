from __future__ import annotations

import time
from collections import Counter, defaultdict
from datetime import datetime
from typing import Iterable

from .inventory import GOOGLE_DOC_EXTENSIONS
from .models import DeleteTombstone, LocalEntry, PlanAction, RemoteEntry, SyncRecord


def build_plan(
    local_entries: Iterable[LocalEntry],
    remote_entries: Iterable[RemoteEntry],
    sync_records: Iterable[SyncRecord] = (),
    conflict_policy: str = "keep_both",
    previous_local_entries: Iterable[LocalEntry] = (),
    previous_remote_entries: Iterable[RemoteEntry] = (),
    tombstones: Iterable[DeleteTombstone] = (),
    recent_local_creates: Iterable[str] = (),
    confirmed_remote_folders: Iterable[str] = (),
    allow_untracked_uploads: bool = True,
    removed_remote_ids: Iterable[str] = (),
    recent_local_deletes: Iterable[str] = (),
) -> list[PlanAction]:
    """Compatibility wrapper around the event kernel.

    Snapshot leftovers are no longer planned. Untracked local files only
    upload on first pairing (no records) or when ``recent_local_creates``
    carries an inotify create. Listing confirmation is ignored: deletes
    require a bound ID in ``removed_remote_ids``.
    """
    from .kernel import (
        KernelEvent,
        coalesce_actions,
        pair_once,
        plan_bound_objects,
        plan_local_events,
    )

    del previous_local_entries, previous_remote_entries, tombstones, confirmed_remote_folders
    local_list = list(local_entries)
    remote_list = [item for item in remote_entries if not item.trashed]
    records = list(sync_records)
    if not records and allow_untracked_uploads:
        _bound, actions = pair_once(local_list, remote_list, records, conflict_policy)
        return actions
    events = [KernelEvent("created", path) for path in recent_local_creates if path]
    events.extend(KernelEvent("deleted", path) for path in recent_local_deletes if path)
    return coalesce_actions(
        plan_local_events(events, {item.path: item for item in local_list}, remote_list, records, conflict_policy),
        plan_bound_objects(
            records,
            local_list,
            remote_list,
            removed_ids=removed_remote_ids,
            confirmed_missing_ids=removed_remote_ids,
            recent_local_deletes=recent_local_deletes,
            conflict_policy=conflict_policy,
        ),
    )


def summarize(actions: Iterable[PlanAction]) -> dict[str, int]:
    return dict(Counter(action.action for action in actions))


def build_records_from_current(
    local_entries: Iterable[LocalEntry],
    remote_entries: Iterable[RemoteEntry],
) -> list[SyncRecord]:
    local_by_path = {e.path: e for e in local_entries}
    records: list[SyncRecord] = []
    for remote in remote_entries:
        if remote.trashed:
            continue
        local = local_by_path.get(remote.path)
        if not local or local.kind != remote.kind:
            continue
        if _same_content(local, remote) or local.kind == "folder" or _is_google_workspace(remote):
            records.append(record_from_pair(local, remote))
    return records


def retain_delete_records(
    current_records: Iterable[SyncRecord],
    previous_records: Iterable[SyncRecord],
    actions: Iterable[PlanAction],
) -> list[SyncRecord]:
    records = list(current_records)
    current_ids = {record.remote_id for record in records}
    previous_by_id = {record.remote_id: record for record in previous_records}
    for action in actions:
        if action.action not in {"local_delete", "remote_trash"} or not action.remote_id:
            continue
        if action.remote_id in current_ids:
            continue
        previous = previous_by_id.get(action.remote_id) or record_from_delete_action(action)
        if previous is None:
            continue
        records.append(previous)
        current_ids.add(previous.remote_id)
    return records


def record_from_delete_action(action: PlanAction) -> SyncRecord | None:
    if not action.remote_id:
        return None
    local = action.local
    remote = action.remote
    path = action.path or (local.path if local else None) or (remote.path if remote else None)
    if not path:
        return None
    return SyncRecord(
        local_path=local.path if local else path,
        remote_id=action.remote_id,
        remote_path=remote.path if remote else path,
        kind=(remote.kind if remote else None) or (local.kind if local else None) or "file",
        local_md5=local.md5 if local else None,
        remote_md5=remote.md5 if remote else None,
        local_size=local.size if local else None,
        remote_size=remote.size if remote else None,
        local_mtime_ns=local.mtime_ns if local else None,
        remote_modified_time=remote.modified_time if remote else None,
    )


def checkpoint_local_entries(
    local_entries: Iterable[LocalEntry],
    actions: Iterable[PlanAction],
    executed: Iterable[str] = (),
) -> list[LocalEntry]:
    """Keep last-seen local paths, including files just downloaded this pass."""
    by_path = {entry.path: entry for entry in local_entries}
    executed_set = set(executed)
    for action in actions:
        if action.action not in {"download_create", "download_update"}:
            continue
        marker = f"{action.action}:{action.path}"
        if executed_set and marker not in executed_set:
            continue
        if action.path in by_path:
            continue
        remote = action.remote
        if remote is None:
            continue
        by_path[action.path] = LocalEntry(
            path=action.path,
            kind=remote.kind,
            size=remote.size,
            mtime_ns=None,
            md5=remote.md5,
            sha256=None,
        )
    return list(by_path.values())


def last_seen_local_for_deletes(
    post_scan: Iterable[LocalEntry],
    checkpoint: Iterable[LocalEntry],
    remote_entries: Iterable[RemoteEntry],
) -> list[LocalEntry]:
    """Remember local files that vanished while the remote copy is still there."""
    remote_paths = {entry.path for entry in remote_entries if not entry.trashed}
    by_path = {entry.path: entry for entry in post_scan}
    for entry in checkpoint:
        if entry.path not in by_path and entry.path in remote_paths:
            by_path[entry.path] = entry
    return list(by_path.values())


def last_seen_remote_for_deletes(
    post_scan: Iterable[RemoteEntry],
    checkpoint: Iterable[RemoteEntry],
    local_entries: Iterable[LocalEntry],
) -> list[RemoteEntry]:
    """Remember remote files that vanished while the local copy is still there."""
    local_paths = {entry.path for entry in local_entries}
    by_id = {entry.id: entry for entry in post_scan if not entry.trashed}
    for entry in checkpoint:
        if entry.trashed:
            continue
        if entry.id not in by_id and entry.path in local_paths:
            by_id[entry.id] = entry
    return list(by_id.values())


def records_from_executed(
    records: Iterable[SyncRecord],
    actions: Iterable[PlanAction],
    executed: Iterable[str] = (),
) -> list[SyncRecord]:
    """Bind Drive IDs as soon as a pair is known, not after the next full scan."""
    by_id = {record.remote_id: record for record in records}
    executed_set = set(executed)
    for action in actions:
        if action.action not in {"download_create", "download_update", "upload_update", "index_update"}:
            continue
        marker = f"{action.action}:{action.path}"
        if action.action != "index_update" and executed_set and marker not in executed_set:
            continue
        remote = action.remote
        if remote is None:
            continue
        local = action.local or LocalEntry(
            path=action.path,
            kind=remote.kind,
            size=remote.size,
            mtime_ns=None,
            md5=remote.md5,
            sha256=None,
        )
        by_id[remote.id] = record_from_pair(local, remote)
    return list(by_id.values())


def tombstones_from_actions(
    existing: Iterable[DeleteTombstone],
    actions: Iterable[PlanAction],
    executed: Iterable[str] | None = None,
    now: int | None = None,
) -> list[DeleteTombstone]:
    """Keep deleted paths so the same Drive file cannot come back as a 'new' download."""
    by_path = {item.path: item for item in existing}
    executed_set = None if executed is None else set(executed)
    stamp = int(time.time()) if now is None else now
    for action in actions:
        if action.action in {"remote_trash", "local_delete"}:
            remote = action.remote
            local = action.local
            by_path[action.path] = DeleteTombstone(
                path=action.path,
                remote_id=action.remote_id or (remote.id if remote else None),
                remote_md5=(remote.md5 if remote else None) or (local.md5 if local else None),
                remote_size=(remote.size if remote else None) or (local.size if local else None),
                remote_modified_time=remote.modified_time if remote else None,
                deleted_at=stamp,
            )
            continue
        if action.action not in {"download_create", "download_update", "upload_create", "upload_update"}:
            continue
        marker = f"{action.action}:{action.path}"
        if executed_set is not None and marker not in executed_set:
            continue
        by_path.pop(action.path, None)
    return list(by_path.values())


def _parent_confirmed_folder(path: str, confirmed_folders: set[str]) -> bool:
    if "/" not in path:
        return False
    return path.rsplit("/", 1)[0] in confirmed_folders


def _parent_is_remote_folder(path: str, remote_by_path: dict[str, RemoteEntry]) -> bool:
    if "/" not in path:
        return False
    parent = remote_by_path.get(path.rsplit("/", 1)[0])
    return parent is not None and parent.kind == "folder"


def _local_matches_tombstone(local: LocalEntry, tombstone: DeleteTombstone) -> bool:
    if local.kind == "folder":
        return True
    if local.md5 and tombstone.remote_md5:
        return local.md5 == tombstone.remote_md5
    if local.size is not None and tombstone.remote_size is not None:
        return local.size == tombstone.remote_size
    return True


def _tombstone_covers(remote: RemoteEntry, tombstone: DeleteTombstone) -> bool:
    if tombstone.remote_id and remote.id == tombstone.remote_id:
        return True
    if remote.md5 and tombstone.remote_md5:
        return remote.md5 == tombstone.remote_md5
    return False


def record_from_pair(local: LocalEntry, remote: RemoteEntry) -> SyncRecord:
    return SyncRecord(
        local_path=local.path,
        remote_id=remote.id,
        remote_path=remote.path,
        kind=remote.kind,
        local_md5=local.md5,
        remote_md5=remote.md5,
        local_size=local.size,
        remote_size=remote.size,
        local_mtime_ns=local.mtime_ns,
        remote_modified_time=remote.modified_time,
    )


def _plan_record(
    record: SyncRecord,
    local: LocalEntry | None,
    remote: RemoteEntry | None,
    conflict_policy: str = "keep_both",
    confirmed_folders: set[str] | frozenset[str] = frozenset(),
    removed_remote_ids: set[str] | frozenset[str] = frozenset(),
    recent_local_deletes: set[str] | frozenset[str] = frozenset(),
) -> list[PlanAction]:
    if local is None and remote is None:
        return [PlanAction("index_remove", record.local_path, "deleted on both sides", remote_id=record.remote_id)]

    local_moved = local is not None and local.path != record.local_path
    remote_moved = remote is not None and remote.path != record.remote_path
    local_changed = _local_changed(record, local)
    remote_changed = _remote_changed(record, remote)

    if remote is None:
        if local is None:
            return [PlanAction("index_remove", record.local_path, "remote and local missing", remote_id=record.remote_id)]
        if local.kind == "folder":
            if record.remote_id in removed_remote_ids or _parent_confirmed_folder(local.path, set(confirmed_folders)):
                return [PlanAction("local_delete", local.path, "remote deleted", local, None, remote_id=record.remote_id)]
            return []
        remote_gone = record.remote_id in removed_remote_ids
        if not remote_gone:
            return []
        if local_changed:
            if conflict_policy == "prefer_remote":
                return [PlanAction("local_delete", local.path, "remote deleted; prefer remote", local, None, remote_id=record.remote_id)]
            if conflict_policy == "keep_both" or _looks_like_google_placeholder(local.path):
                return [PlanAction("conflict_review", local.path, "remote deleted while local changed", local, None, remote_id=record.remote_id)]
            return [PlanAction("upload_create", local.path, "local updated; remote deleted", local, None, remote_id=record.remote_id)]
        return [PlanAction("local_delete", local.path, "remote deleted", local, None, remote_id=record.remote_id)]

    if local is None:
        if remote.path not in recent_local_deletes and record.local_path not in recent_local_deletes:
            return [PlanAction("download_create", remote.path, "missing local; take remote", None, remote, remote_id=remote.id)]
        if remote_changed:
            if conflict_policy == "prefer_local":
                return [PlanAction("remote_trash", record.remote_path, "local deleted; prefer local", None, remote, remote_id=remote.id)]
            if conflict_policy == "keep_both":
                return [PlanAction("conflict_review", remote.path, "local deleted while remote changed", None, remote, remote_id=remote.id)]
            return [PlanAction("download_create", remote.path, "remote updated; local deleted", None, remote, remote_id=remote.id)]
        return [PlanAction("remote_trash", record.remote_path, "local deleted", None, remote, remote_id=remote.id)]

    if local_moved and remote_moved and local.path != remote.path:
        return [PlanAction("conflict_review", local.path, "both sides moved to different paths", local, remote, record.local_path, remote.path, remote.id)]
    if _is_google_workspace(remote) and local_changed and not remote_changed:
        return [PlanAction("conflict_review", local.path, "local Google Workspace placeholder changed", local, remote, remote_id=remote.id)]
    if local_changed and remote_changed:
        if _same_content(local, remote):
            return [PlanAction("index_update", local.path, "both sides changed to same content", local, remote, remote_id=remote.id)]
        if conflict_policy == "prefer_local" and local.kind == "file" and not _is_google_workspace(remote):
            return [PlanAction("upload_update", local.path, "both sides changed; prefer local", local, remote, remote_id=remote.id)]
        if conflict_policy == "prefer_remote":
            return [PlanAction("download_update", remote.path, "both sides changed; prefer remote", local, remote, remote_id=remote.id)]
        if conflict_policy == "prefer_newer" and local.kind == "file" and not _is_google_workspace(remote):
            newer = _newer_side(local, remote)
            if newer == "local":
                return [PlanAction("upload_update", local.path, "both sides changed; local newer", local, remote, remote_id=remote.id)]
            if newer == "remote":
                return [PlanAction("download_update", remote.path, "both sides changed; remote newer", local, remote, remote_id=remote.id)]
        return [PlanAction("conflict_copy_local", local.path, "both sides changed", local, remote, remote_id=remote.id)]
    if local_moved and not remote_moved:
        return [PlanAction("remote_move", local.path, "local moved", local, remote, record.remote_path, local.path, remote.id)]
    if remote_moved and not local_moved:
        return [PlanAction("local_move", remote.path, "remote moved", local, remote, record.local_path, remote.path, remote.id)]
    if local_changed:
        return [PlanAction("upload_update", local.path, "local changed", local, remote, remote_id=remote.id)]
    if remote_changed:
        return [PlanAction("download_update", remote.path, "remote changed", local, remote, remote_id=remote.id)]
    if local_moved and remote_moved and local.path == remote.path:
        return [PlanAction("index_update", local.path, "both sides moved to same path", local, remote, remote_id=remote.id)]
    return [PlanAction("index_update", local.path, "unchanged", local, remote, remote_id=remote.id)]


def _plan_untracked_pair(
    path: str,
    local: LocalEntry,
    remote: RemoteEntry,
    conflict_policy: str = "keep_both",
) -> list[PlanAction]:
    if local.kind != remote.kind:
        return [PlanAction("conflict_review", path, "new local/remote type mismatch", local, remote, remote_id=remote.id)]
    if local.kind == "folder" or _is_google_workspace(remote) or _same_content(local, remote):
        return [PlanAction("index_update", path, "new matching pair", local, remote, remote_id=remote.id)]
    if conflict_policy == "prefer_local" and local.kind == "file":
        return [PlanAction("upload_update", path, "new local and remote differ; prefer local", local, remote, remote_id=remote.id)]
    if conflict_policy == "prefer_remote":
        return [PlanAction("download_update", path, "new local and remote differ; prefer remote", local, remote, remote_id=remote.id)]
    if conflict_policy == "prefer_newer" and local.kind == "file" and not _is_google_workspace(remote):
        newer = _newer_side(local, remote)
        if newer == "local":
            return [PlanAction("upload_update", path, "new local and remote differ; local newer", local, remote, remote_id=remote.id)]
        if newer == "remote":
            return [PlanAction("download_update", path, "new local and remote differ; remote newer", local, remote, remote_id=remote.id)]
    return [PlanAction("conflict_copy_local", path, "new local and remote differ", local, remote, remote_id=remote.id)]


def _local_entry_changed(previous: LocalEntry, current: LocalEntry) -> bool:
    if previous.kind != current.kind:
        return True
    if current.kind == "folder":
        return False
    if previous.md5 and current.md5:
        return previous.md5 != current.md5
    return previous.size != current.size or previous.mtime_ns != current.mtime_ns


def _remote_entry_changed(previous: RemoteEntry, current: RemoteEntry) -> bool:
    if previous.kind != current.kind:
        return True
    if current.kind == "folder":
        return False
    if _is_google_workspace(current):
        return current.modified_time != previous.modified_time
    if previous.md5 and current.md5:
        return previous.md5 != current.md5
    return previous.size != current.size or previous.modified_time != current.modified_time


def _looks_like_google_placeholder(path: str) -> bool:
    return path.endswith(tuple(GOOGLE_DOC_EXTENSIONS.values()))


def _local_changed(record: SyncRecord, local: LocalEntry | None) -> bool:
    if local is None:
        return False
    if local.kind != record.kind:
        return True
    if local.kind == "folder":
        return False
    if local.md5 and record.local_md5:
        return local.md5 != record.local_md5
    return local.size != record.local_size or local.mtime_ns != record.local_mtime_ns


def _remote_changed(record: SyncRecord, remote: RemoteEntry | None) -> bool:
    if remote is None:
        return False
    if remote.kind != record.kind:
        return True
    if remote.kind == "folder":
        return False
    if _is_google_workspace(remote):
        return remote.modified_time != record.remote_modified_time
    if remote.md5 and record.remote_md5:
        return remote.md5 != record.remote_md5
    return remote.size != record.remote_size or remote.modified_time != record.remote_modified_time


def _same_content(local: LocalEntry, remote: RemoteEntry) -> bool:
    if local.kind != remote.kind:
        return False
    if local.kind == "symlink":
        return True
    if local.kind == "folder":
        return True
    if _is_google_workspace(remote):
        return local.kind == "file"
    if local.md5 and remote.md5:
        return local.md5 == remote.md5
    if local.size is not None and remote.size is not None:
        return local.size == remote.size
    return False


def _newer_side(local: LocalEntry, remote: RemoteEntry) -> str | None:
    if local.mtime_ns is None or not remote.modified_time:
        return None
    try:
        local_ts = local.mtime_ns / 1_000_000_000
        remote_ts = datetime.fromisoformat(remote.modified_time.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    if abs(local_ts - remote_ts) < 1:
        return None
    return "local" if local_ts > remote_ts else "remote"


def _is_google_workspace(remote: RemoteEntry) -> bool:
    return remote.mime_type in GOOGLE_DOC_EXTENSIONS


def _local_move_candidates(local_entries: Iterable[LocalEntry], records: Iterable[SyncRecord]) -> dict[tuple[str, int | None], list[LocalEntry]]:
    indexed_paths = {r.local_path for r in records}
    candidates: dict[tuple[str, int | None], list[LocalEntry]] = defaultdict(list)
    for local in local_entries:
        if local.path in indexed_paths or local.kind != "file" or not local.md5:
            continue
        candidates[(local.md5, local.size)].append(local)
    return candidates


def _pick_local_move(
    record: SyncRecord,
    candidates: dict[tuple[str, int | None], list[LocalEntry]],
    used_local: set[str],
) -> LocalEntry | None:
    if not record.local_md5:
        return None
    matches = [item for item in candidates.get((record.local_md5, record.local_size), []) if item.path not in used_local]
    return matches[0] if len(matches) == 1 else None
