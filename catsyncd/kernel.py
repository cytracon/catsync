"""Insync-style event kernel.

A synced file is a Drive ID bound to a local path. After the folder is
paired once, CatSync may only invent work from:

* local inotify (create / close-write / move / delete) on that binding
* the Drive Changes feed (or an authoritative GET of that same ID)

A tree snapshot is never a delete or upload signal. Incomplete folder
listings never shrink the object index. No event ⇒ no leftover action.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Iterable

from .models import LocalEntry, PlanAction, RemoteEntry, SyncRecord
from .planner import (
    _local_changed,
    _plan_record,
    _plan_untracked_pair,
    _remote_changed,
    record_from_pair,
)


PAIRED_META_KEY = "paired"
_DELETE_ACTIONS = frozenset({"remote_trash", "local_delete", "index_remove"})
_CONTENT_ACTIONS = frozenset(
    {
        "download_create",
        "download_update",
        "upload_create",
        "upload_update",
        "conflict_copy_local",
    }
)


@dataclass(frozen=True)
class KernelEvent:
    kind: str
    path: str
    src_path: str | None = None
    is_dir: bool = False


def is_paired(meta_value: str | None, records: Iterable[SyncRecord]) -> bool:
    """Existing installs with an object index are already paired."""
    if meta_value and meta_value.strip().lower() in {"1", "true", "yes", "paired"}:
        return True
    return any(True for _ in records)


def pair_once(
    local_entries: Iterable[LocalEntry],
    remote_entries: Iterable[RemoteEntry],
    existing_records: Iterable[SyncRecord] = (),
    conflict_policy: str = "keep_both",
) -> tuple[list[SyncRecord], list[PlanAction]]:
    """First pairing only: path-match, then upload/download the remainder.

    After this returns, the caller must persist ``paired`` and never run
    this leftover pass again.
    """
    local_by_path = {item.path: item for item in local_entries}
    remote_by_path = {item.path: item for item in remote_entries if not item.trashed}
    records = {item.remote_id: item for item in existing_records}
    bound_local = {item.local_path for item in records.values()}
    bound_remote = {item.remote_id for item in records.values()}
    actions: list[PlanAction] = []

    for path in sorted(set(local_by_path) | set(remote_by_path)):
        local = local_by_path.get(path)
        remote = remote_by_path.get(path)
        if local and local.path in bound_local:
            continue
        if remote and remote.id in bound_remote:
            continue
        if local and remote:
            pair_actions = _plan_untracked_pair(path, local, remote, conflict_policy)
            actions.extend(pair_actions)
            if any(item.action == "index_update" for item in pair_actions):
                records[remote.id] = record_from_pair(local, remote)
                bound_local.add(local.path)
                bound_remote.add(remote.id)
            continue
        if remote:
            actions.append(
                PlanAction(
                    "download_create",
                    remote.path,
                    "first pairing: remote only",
                    remote=remote,
                    remote_id=remote.id,
                )
            )
            continue
        if local:
            actions.append(PlanAction("upload_create", local.path, "first pairing: local only", local=local))

    return list(records.values()), _drop_noop_index(actions)


def plan_local_events(
    events: Iterable[KernelEvent],
    local_by_path: dict[str, LocalEntry],
    remote_entries: Iterable[RemoteEntry],
    records: Iterable[SyncRecord],
    conflict_policy: str = "keep_both",
) -> list[PlanAction]:
    """inotify → actions. Unbound leftover paths with no create are ignored."""
    remote_by_path = {item.path: item for item in remote_entries if not item.trashed}
    record_by_local = {item.local_path: item for item in records}
    record_by_id = {item.remote_id: item for item in records}
    actions: list[PlanAction] = []
    seen: set[tuple[str, str]] = set()

    for event in events:
        path = (event.path or "").strip("/")
        src = (event.src_path or "").strip("/") or None
        if not path and event.kind != "moved":
            continue
        key = (event.kind, path)
        if key in seen:
            continue
        seen.add(key)

        if event.kind == "deleted" and path:
            record = record_by_local.get(path)
            if record is None:
                continue
            remote = remote_by_path.get(record.remote_path)
            actions.append(
                PlanAction(
                    "remote_trash",
                    record.remote_path,
                    "inotify delete on bound path",
                    local_by_path.get(path),
                    remote,
                    remote_id=record.remote_id,
                )
            )
            continue

        if event.kind == "moved" and src:
            record = record_by_local.get(src)
            if record is not None:
                remote = next((item for item in remote_entries if item.id == record.remote_id and not item.trashed), None)
                local = local_by_path.get(path)
                actions.append(
                    PlanAction(
                        "remote_move",
                        path,
                        "inotify move on bound path",
                        local,
                        remote,
                        src,
                        path,
                        record.remote_id,
                    )
                )
                continue
            if path:
                event = KernelEvent("created", path, is_dir=event.is_dir)
            else:
                continue

        if event.kind in {"created", "moved"} and path:
            local = local_by_path.get(path)
            if local is None:
                continue
            record = record_by_local.get(path)
            if record is not None:
                remote = next((item for item in remote_entries if item.id == record.remote_id and not item.trashed), None)
                if remote is None:
                    actions.append(PlanAction("upload_create", path, "inotify recreate; bound id gone", local=local))
                    continue
                if local.kind == "file" and _local_changed(record, local):
                    if _remote_changed(record, remote):
                        # Concurrent edit on Drive: never blind-overwrite.
                        actions.extend(_plan_record(record, local, remote, conflict_policy))
                    else:
                        actions.append(
                            PlanAction(
                                "upload_update",
                                path,
                                "inotify create/replace on bound file",
                                local,
                                remote,
                                remote_id=remote.id,
                            )
                        )
                continue
            remote_at = remote_by_path.get(path)
            if remote_at is not None and remote_at.id not in record_by_id:
                actions.extend(_plan_untracked_pair(path, local, remote_at, conflict_policy))
                continue
            actions.append(PlanAction("upload_create", path, "inotify create", local=local))
            continue

        if event.kind == "modified" and path:
            local = local_by_path.get(path)
            record = record_by_local.get(path)
            if local is None or record is None:
                continue
            remote = next((item for item in remote_entries if item.id == record.remote_id and not item.trashed), None)
            if remote is None or local.kind != "file":
                continue
            if _local_changed(record, local):
                if _remote_changed(record, remote):
                    # Concurrent edit on Drive: never blind-overwrite.
                    actions.extend(_plan_record(record, local, remote, conflict_policy))
                else:
                    actions.append(
                        PlanAction(
                            "upload_update",
                            path,
                            "inotify close-write on bound file",
                            local,
                            remote,
                            remote_id=remote.id,
                        )
                    )

    return _drop_noop_index(actions)


def plan_remote_changes(
    *,
    removed_ids: Iterable[str],
    changed_ids: Iterable[str],
    remote_entries: Iterable[RemoteEntry],
    local_by_path: dict[str, LocalEntry],
    records: Iterable[SyncRecord],
    conflict_policy: str = "keep_both",
) -> list[PlanAction]:
    """Changes API → actions for those IDs only."""
    removed = {item for item in removed_ids if item}
    changed = {item for item in changed_ids if item}
    remote_by_id = {item.id: item for item in remote_entries if not item.trashed}
    record_by_id = {item.remote_id: item for item in records}
    actions: list[PlanAction] = []

    for file_id in sorted(removed):
        record = record_by_id.get(file_id)
        if record is None:
            continue
        local = local_by_path.get(record.local_path)
        if local is None:
            actions.append(PlanAction("index_remove", record.local_path, "changes: id gone, local already gone", remote_id=file_id))
            continue
        actions.append(
            PlanAction(
                "local_delete",
                record.local_path,
                "changes: id trashed or removed",
                local,
                None,
                remote_id=file_id,
            )
        )

    new_ids = changed - set(record_by_id)
    actions.extend(
        plan_new_remote_ids(
            remote_entries,
            records,
            local_by_path,
            new_ids=new_ids,
            conflict_policy=conflict_policy,
        )
    )

    for file_id in sorted(changed - removed):
        record = record_by_id.get(file_id)
        remote = remote_by_id.get(file_id)
        if record is None or remote is None:
            continue
        local = local_by_path.get(record.local_path)
        if local is None:
            actions.append(
                PlanAction(
                    "download_create",
                    remote.path,
                    "changes: bound id still live, local missing",
                    None,
                    remote,
                    remote_id=remote.id,
                )
            )
            continue
        if _remote_changed(record, remote) or remote.path != record.remote_path:
            actions.extend(_plan_record(record, local, remote, conflict_policy, removed_remote_ids=removed))

    return _drop_noop_index(actions)


def plan_new_remote_ids(
    remote_entries: Iterable[RemoteEntry],
    records: Iterable[SyncRecord],
    local_by_path: dict[str, LocalEntry],
    new_ids: Iterable[str],
    conflict_policy: str = "keep_both",
) -> list[PlanAction]:
    """Download (or bind) IDs that the change feed / full list just introduced."""
    wanted = {item for item in new_ids if item}
    if not wanted:
        return []
    known = {item.remote_id for item in records}
    actions: list[PlanAction] = []
    for remote in remote_entries:
        if remote.trashed or remote.id in known or remote.id not in wanted:
            continue
        local = local_by_path.get(remote.path)
        if local is None:
            actions.append(
                PlanAction(
                    "download_create",
                    remote.path,
                    "new remote id",
                    remote=remote,
                    remote_id=remote.id,
                )
            )
            continue
        actions.extend(_plan_untracked_pair(remote.path, local, remote, conflict_policy))
    return _drop_noop_index(actions)


def plan_bound_objects(
    records: Iterable[SyncRecord],
    local_entries: Iterable[LocalEntry],
    remote_entries: Iterable[RemoteEntry],
    *,
    removed_ids: Iterable[str] = (),
    confirmed_missing_ids: Iterable[str] = (),
    recent_local_deletes: Iterable[str] = (),
    conflict_policy: str = "keep_both",
) -> list[PlanAction]:
    """Bound IDs only. Unknown (not in cache, not GET-confirmed gone) is a no-op."""
    local_by_path = {item.path: item for item in local_entries}
    remote_by_id = {item.id: item for item in remote_entries if not item.trashed}
    gone = {item for item in removed_ids if item} | {item for item in confirmed_missing_ids if item}
    recent_deletes = {path.strip("/") for path in recent_local_deletes if path and path.strip("/")}
    actions: list[PlanAction] = []

    for record in records:
        local = local_by_path.get(record.local_path)
        remote = remote_by_id.get(record.remote_id)
        if remote is None and record.remote_id not in gone:
            continue
        if local is None and remote is not None and (
            remote.path in recent_deletes or record.local_path in recent_deletes
        ):
            continue
        planned = _plan_record(
            record,
            local,
            remote,
            conflict_policy,
            removed_remote_ids=gone,
            recent_local_deletes=recent_deletes,
        )
        for action in planned:
            if action.action == "index_update" and action.reason in {"unchanged", "both sides moved to same path"}:
                continue
            actions.append(action)
    return actions


def overflow_creates(
    local_by_path: dict[str, LocalEntry],
    records: Iterable[SyncRecord],
    remote_entries: Iterable[RemoteEntry],
    tombstones: Iterable = (),
) -> list[KernelEvent]:
    """Recover CREATE events lost to overflow, downtime, or bootstrap.

    Unbound locals in an already-bound parent folder are new objects.
    Never synthesize deletes. Tombstoned paths are not re-uploaded.
    """
    bound_local = {item.local_path for item in records}
    bound_folders = {item.local_path for item in records if item.kind == "folder"}
    remote_paths = {item.path for item in remote_entries if not item.trashed}
    tombstoned = {item.path for item in tombstones if getattr(item, "path", None)}
    events: list[KernelEvent] = []
    for path, entry in sorted(local_by_path.items()):
        if path in bound_local or path in remote_paths or path in tombstoned:
            continue
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        if parent and parent not in bound_folders:
            continue
        events.append(KernelEvent("created", path, is_dir=entry.kind == "folder"))
    return events


DISCOVER_CURSOR_META = "discover_folder_cursor"
DISCOVER_LISTED_EMPTY_META = "discover_listed_empty"


def folder_child_names(folder_path: str, paths: Iterable[str]) -> set[str]:
    prefix = f"{folder_path.rstrip('/')}/"
    names: set[str] = set()
    for path in paths:
        if path.startswith(prefix):
            names.add(path[len(prefix) :].split("/", 1)[0])
    return names


def choose_discover_folders(
    folders: Iterable[SyncRecord],
    remote_paths: Iterable[str],
    *,
    limit: int,
    cursor: int = 0,
    listed_empty_ids: Iterable[str] = (),
) -> tuple[list[SyncRecord], int]:
    """Pick bound folders that still need a Drive children listing.

    A bound folder with zero remote children in the event cache is unlisted,
    not "in sync with an empty local folder". Those are listed first.
    Remaining folders rotate so depth-2 albums are not starved forever by
    the same 80 shallow parents. A listing never implies a delete.
    """
    folder_list = [item for item in folders if getattr(item, "kind", None) == "folder"]
    if limit <= 0 or not folder_list:
        return [], cursor
    remote_paths = list(remote_paths)
    listed_empty = {item for item in listed_empty_ids if item}
    unlisted: list[SyncRecord] = []
    rest: list[SyncRecord] = []
    for record in folder_list:
        kids = folder_child_names(record.remote_path, remote_paths)
        if not kids and record.remote_id not in listed_empty:
            unlisted.append(record)
        else:
            rest.append(record)
    chosen: list[SyncRecord] = []
    seen: set[str] = set()

    def take(record: SyncRecord) -> None:
        if record.remote_id in seen:
            return
        seen.add(record.remote_id)
        chosen.append(record)

    for record in unlisted:
        take(record)
        if len(chosen) >= limit:
            return chosen, cursor

    if rest and len(chosen) < limit:
        start = cursor % len(rest)
        offset = 0
        while len(chosen) < limit and offset < len(rest):
            take(rest[(start + offset) % len(rest)])
            offset += 1
        cursor = (start + offset) % len(rest)
    return chosen, cursor


def coalesce_actions(*groups: Iterable[PlanAction]) -> list[PlanAction]:
    """Deletes win. Duplicate-path remotes become conflict_review."""
    actions = [item for group in groups for item in group]
    delete_ids = {item.remote_id for item in actions if item.action in _DELETE_ACTIONS and item.remote_id}
    delete_paths = {item.path for item in actions if item.action in _DELETE_ACTIONS}
    kept: list[PlanAction] = []
    seen: set[tuple[str, str, str | None]] = set()
    for action in actions:
        key = (action.action, action.path, action.remote_id)
        if key in seen:
            continue
        if action.action in _CONTENT_ACTIONS and (
            (action.remote_id and action.remote_id in delete_ids) or action.path in delete_paths
        ):
            continue
        seen.add(key)
        kept.append(action)

    # A real duplicate is two DIFFERENT Drive files claiming the same path.
    # Two actions for the same remote_id (e.g. keep-both conflict_copy +
    # upload_update) are the same object and must not trigger conflict_review.
    seen_remote_paths: dict[str, str] = {}
    for item in kept:
        if item.remote and item.action != "conflict_review":
            remote_id = item.remote.id
            if item.path in seen_remote_paths and seen_remote_paths[item.path] != remote_id:
                kept.append(PlanAction("conflict_review", item.path, "duplicate remote path"))
            else:
                seen_remote_paths[item.path] = remote_id
    return kept


def _drop_noop_index(actions: Iterable[PlanAction]) -> list[PlanAction]:
    return [
        action
        for action in actions
        if action.action != "index_update"
        or action.reason not in {"unchanged", "both sides moved to same path"}
    ]
