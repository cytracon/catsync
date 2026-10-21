from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

from .auth import AuthError, refresh_access_token, valid_access_token
from .config import Config
from .drive import ChangesTokenExpired, DriveClient, DriveError, DriveFileNotFound
from .executor import execute_plan
from .inventory import (
    apply_local_fs_event,
    apply_remote_changes,
    child_item_to_entry,
    drop_remote_ids,
    local_entry_from_path,
    remote_from_api_item,
    scan_local,
    sync_path_allowed,
    upsert_remote_entry,
)
from .kernel import (
    DISCOVER_CURSOR_META,
    DISCOVER_LISTED_EMPTY_META,
    PAIRED_META_KEY,
    KernelEvent,
    choose_discover_folders,
    coalesce_actions,
    is_paired,
    overflow_creates,
    pair_once,
    plan_bound_objects,
    plan_local_events,
    plan_new_remote_ids,
    plan_remote_changes,
)
from .models import DeleteTombstone, LocalEntry, RemoteEntry, SyncRecord
from .planner import (
    checkpoint_local_entries,
    record_from_pair,
    records_from_executed,
    summarize,
    tombstones_from_actions,
)
from .state import State
from .watch import FsEvent, InotifyWatcher


class StopRequested(RuntimeError):
    pass


RECONCILE_SECONDS = 1800
GET_MISSING_LIMIT = 40
DISCOVER_FOLDER_LIMIT = 80


class SyncEngine:
    def __init__(self, config: Config, should_stop: Callable[[], bool] | None = None):
        self.config = config
        self.should_stop = should_stop
        self.local_by_path: dict[str, LocalEntry] = {}
        self.remote_entries: list[RemoteEntry] = []
        self.records: list[SyncRecord] = []
        self.tombstones: list[DeleteTombstone] = []
        self.changes_token: str | None = None
        self.root_ids: tuple[str, ...] = ("root",)
        self.watcher: InotifyWatcher | None = None
        self.next_remote_poll = 0.0
        self.next_reconcile = 0.0
        self.recent_activity: list[dict] = []
        self.busy_paths: set[str] = set()
        self.recent_local_creates: set[str] = set()
        self.recent_local_deletes: set[str] = set()
        self.removed_remote_ids: set[str] = set()
        self.changed_remote_ids: set[str] = set()
        self.confirmed_missing_ids: set[str] = set()
        self.authoritative_remote = False
        self.paired = False
        self.pending_events: list[KernelEvent] = []
        self.drive: DriveClient | None = None
        self.discover_cursor = 0
        self.listed_empty_ids: set[str] = set()

    def run_forever(self, write_status: Callable) -> None:
        write_status({}, phase="starting", detail={"mode": "watch-first"})
        watches = self.start_watcher()
        write_status({}, phase="starting", detail={"watches": watches})
        try:
            self.bootstrap(write_status)
            write_status({}, phase="idle", detail={"watches": watches, "mode": "event" if self.paired else "pairing"})
            while not self._stopped():
                self._wait_for_work()
                if self._stopped():
                    break
                self.sync_tick(write_status)
        finally:
            if self.watcher is not None:
                self.watcher.close()
                self.watcher = None

    def bootstrap(self, write_status: Callable) -> None:
        write_status({}, phase="starting")
        self._raise_if_stop()
        state = State(self.config.db_path)
        try:
            previous_local = state.get_local_entries()
            previous_remote = [item for item in state.get_remote_entries() if not item.trashed]
            self.remote_entries = list(previous_remote)
            self.records = state.get_sync_records()
            self.tombstones = state.get_delete_tombstones()
            self.changes_token = state.get_meta("changes_page_token")
            self.paired = is_paired(state.get_meta(PAIRED_META_KEY), self.records)
            try:
                recent_deletes = json.loads(state.get_meta("recent_local_deletes") or "[]")
                self.recent_local_deletes = {str(item) for item in recent_deletes}
            except (TypeError, json.JSONDecodeError):
                self.recent_local_deletes = set()
            try:
                self.discover_cursor = int(state.get_meta(DISCOVER_CURSOR_META) or "0")
            except ValueError:
                self.discover_cursor = 0
            try:
                listed = json.loads(state.get_meta(DISCOVER_LISTED_EMPTY_META) or "[]")
                self.listed_empty_ids = {str(item) for item in listed}
            except (TypeError, json.JSONDecodeError):
                self.listed_empty_ids = set()
        finally:
            state.close()
        write_status({}, phase="scan_local")
        locals_ = scan_local(
            self.config.sync_root,
            self.config.hash_limit_bytes,
            self.config.ignore_patterns,
            self.config.selective_sync_paths,
            self.config.excluded_sync_paths,
            self.config.sync_symlinks,
            previous_local,
        )
        self.local_by_path = {item.path: item for item in locals_}
        if self.watcher is not None:
            events = self.watcher.drain()
            if events and not any(event.overflow for event in events):
                self._apply_events(events)
        self.drive = _drive_client(self.config)
        self.root_ids = ("root", self.drive.get_root_id())
        if not self.remote_entries:
            write_status({}, phase="scan_remote", detail={"local_entries": len(self.local_by_path), "mode": "full"})
            self.changes_token = self.drive.get_start_page_token()
            from .inventory import build_remote_inventory

            self.remote_entries = build_remote_inventory(
                self.drive.iter_files(),
                self.config.ignore_patterns,
                self.config.selective_sync_paths,
                self.config.excluded_sync_paths,
                root_ids=self.root_ids,
            )
            self.authoritative_remote = True
            self._pull_remote_changes()
        else:
            write_status({}, phase="scan_remote", detail={"local_entries": len(self.local_by_path), "mode": "changes"})
            self._pull_remote_changes()
        if self.paired:
            write_status({}, phase="confirm_ids", detail={"records": len(self.records)})
            self._confirm_missing_bound_ids()
            self._discover_bound_folder_children()
        self._raise_if_stop()
        self.sync_tick(write_status, force=True)
        now = time.time()
        self.next_remote_poll = now + self.config.poll_interval
        self.next_reconcile = now + RECONCILE_SECONDS

    def start_watcher(self) -> int:
        self.watcher = InotifyWatcher(
            self.config.sync_root,
            self.config.ignore_patterns,
            self.config.selective_sync_paths,
            self.config.excluded_sync_paths,
        )
        return self.watcher.start()

    def sync_tick(self, write_status: Callable, force: bool = False, previous_local=None, previous_remote=None) -> dict[str, int]:
        del previous_local, previous_remote
        overflow = False
        local_dirty = bool(self.pending_events)
        if self.watcher is not None:
            events = self.watcher.drain()
            overflow = any(event.overflow for event in events)
            if events and not overflow:
                local_dirty = self._apply_events(events) or local_dirty
        now = time.time()
        remote_due = force or now >= self.next_remote_poll
        reconcile_due = force or overflow or now >= self.next_reconcile
        if overflow:
            write_status({}, phase="scan_local", detail={"reason": "inotify_overflow"})
            locals_ = scan_local(
                self.config.sync_root,
                self.config.hash_limit_bytes,
                self.config.ignore_patterns,
                self.config.selective_sync_paths,
                self.config.excluded_sync_paths,
                self.config.sync_symlinks,
                list(self.local_by_path.values()),
            )
            self.local_by_path = {item.path: item for item in locals_}
            self.pending_events.extend(
                overflow_creates(self.local_by_path, self.records, self.remote_entries, self.tombstones)
            )
            local_dirty = True
        elif reconcile_due and self.paired:
            write_status({}, phase="scan_local", detail={"reason": "bound_reconcile"})
            locals_ = scan_local(
                self.config.sync_root,
                self.config.hash_limit_bytes,
                self.config.ignore_patterns,
                self.config.selective_sync_paths,
                self.config.excluded_sync_paths,
                self.config.sync_symlinks,
                list(self.local_by_path.values()),
            )
            self.local_by_path = {item.path: item for item in locals_}
            self.pending_events.extend(
                overflow_creates(self.local_by_path, self.records, self.remote_entries, self.tombstones)
            )
            self._confirm_missing_bound_ids()
            self._discover_bound_folder_children()
            self.next_reconcile = time.time() + RECONCILE_SECONDS
            local_dirty = True
        if remote_due:
            write_status({}, phase="scan_remote", detail={"mode": "changes", "local_entries": len(self.local_by_path)})
            self._pull_remote_changes()
            self.next_remote_poll = time.time() + self.config.poll_interval
        if not force and not local_dirty and not remote_due and not reconcile_due:
            return {}
        if overflow or reconcile_due:
            self.next_reconcile = time.time() + RECONCILE_SECONDS
        self._raise_if_stop()
        return self._plan_and_apply(write_status)

    def _apply_events(self, events: list[FsEvent]) -> bool:
        dirty = False
        for event in events:
            if event.overflow:
                continue
            # Never drop deletes: a user delete during a download pass must
            # still trash the bound Drive ID. Skip only writes we caused.
            if event.kind not in {"deleted"} and (
                event.path in self.busy_paths or (event.src_path or "") in self.busy_paths
            ):
                continue
            apply_local_fs_event(
                self.config.sync_root,
                event.kind,
                event.path,
                self.local_by_path,
                self.config.hash_limit_bytes,
                self.config.sync_symlinks,
                event.src_path,
            )
            self.pending_events.append(
                KernelEvent(event.kind, event.path, src_path=event.src_path, is_dir=event.is_dir)
            )
            if event.kind in {"created", "moved"} and event.path:
                self.recent_local_creates.add(event.path)
            if event.kind == "deleted" and event.path:
                self.recent_local_creates.discard(event.path)
                self.recent_local_deletes.add(event.path)
            dirty = True
        return dirty

    def _pull_remote_changes(self) -> None:
        assert self.drive is not None
        if not self.changes_token:
            self.changes_token = self.drive.get_start_page_token()
            return
        try:
            changes = list(self.drive.iter_changes(self.changes_token))
        except ChangesTokenExpired:
            from .inventory import build_remote_inventory

            self.changes_token = self.drive.get_start_page_token()
            self.remote_entries = build_remote_inventory(
                self.drive.iter_files(),
                self.config.ignore_patterns,
                self.config.selective_sync_paths,
                self.config.excluded_sync_paths,
                root_ids=self.root_ids,
            )
            self.authoritative_remote = True
            live_ids = {item.id for item in self.remote_entries if not item.trashed}
            self.removed_remote_ids.update(record.remote_id for record in self.records if record.remote_id not in live_ids)
            self.changed_remote_ids.update(live_ids)
            self._persist_changes_token()
            return
        changed_ids = set()
        for change in changes:
            if change.get("newStartPageToken"):
                continue
            file_id = change.get("fileId") or (change.get("file") or {}).get("id")
            if file_id:
                changed_ids.add(str(file_id))
        self.remote_entries, new_token, removed_ids = apply_remote_changes(
            self.remote_entries,
            changes,
            self.config.ignore_patterns,
            self.config.selective_sync_paths,
            self.config.excluded_sync_paths,
            root_ids=self.root_ids,
        )
        self.removed_remote_ids.update(removed_ids)
        self.changed_remote_ids.update(changed_ids - removed_ids)
        if new_token:
            self.changes_token = new_token
        self._persist_changes_token()

    def _persist_changes_token(self) -> None:
        """Persist the Changes page token as soon as it advances.

        If the daemon dies between the pull and the next full persist, the
        token is already safe and changes are not replayed or lost.
        """
        if not self.changes_token:
            return
        state = State(self.config.db_path)
        try:
            state.set_meta("changes_page_token", self.changes_token)
        finally:
            state.close()

    def _confirm_missing_bound_ids(self) -> None:
        assert self.drive is not None
        remote_by_id = {item.id: item for item in self.remote_entries if not item.trashed}
        missing = [record for record in self.records if record.remote_id not in remote_by_id]
        missing.sort(key=lambda record: 0 if record.local_path in self.local_by_path else 1)
        found: list[RemoteEntry] = []
        for record in missing[:GET_MISSING_LIMIT]:
            if self._stopped():
                break
            try:
                item = self.drive.get_file(record.remote_id)
            except DriveFileNotFound:
                self.confirmed_missing_ids.add(record.remote_id)
                self.removed_remote_ids.add(record.remote_id)
                continue
            except DriveError:
                continue
            if item.get("trashed"):
                self.confirmed_missing_ids.add(record.remote_id)
                self.removed_remote_ids.add(record.remote_id)
                continue
            found.append(remote_from_api_item(item, record.remote_path))
        for entry in found:
            self.remote_entries = upsert_remote_entry(self.remote_entries, entry)
        if self.confirmed_missing_ids:
            self.remote_entries = drop_remote_ids(self.remote_entries, self.confirmed_missing_ids)

    def _confirm_bound_local_paths(self) -> set[str]:
        """Gebundene Pfade, die auf der Platte fehlen.

        Nur Pfade mit einem echten vorherigen inotify-DELETE gelten als
        geloescht (remote_trash). Alle anderen fehlenden Pfade werden von
        plan_bound_objects wiederhergestellt (download_create). Damit kann
        ein Kaskaden-Move oder eine unvollstaendige lokale Kopie keine
        Drive-Dateien loeschen, echte User-Deletes ueberleben aber auch
        einen Daemon-Neustart (die Menge wird in meta persistiert).
        """
        root = self.config.sync_root.expanduser().resolve()
        missing: list[str] = []
        for record in self.records:
            target = root / record.local_path
            try:
                target.lstat()
            except FileNotFoundError:
                self.local_by_path.pop(record.local_path, None)
                missing.append(record.local_path)
                continue
            except OSError:
                continue
            if record.local_path not in self.local_by_path:
                entry = local_entry_from_path(
                    root,
                    record.local_path,
                    self.config.hash_limit_bytes,
                    self.local_by_path,
                    self.config.sync_symlinks,
                )
                if entry is not None:
                    self.local_by_path[record.local_path] = entry
        return {path for path in missing if path in self.recent_local_deletes}

    def _discover_bound_folder_children(self) -> None:
        """Find new Drive IDs under already-bound folders.

        Listing is discovery only. Children missing from the listing are
        never treated as deletes; that authority is Changes/GET on the ID.
        """
        assert self.drive is not None
        remote_by_id = {item.id: item for item in self.remote_entries if not item.trashed}
        folders = [record for record in self.records if record.kind == "folder"]
        remote_paths = [item.path for item in self.remote_entries if not item.trashed]
        chosen, self.discover_cursor = choose_discover_folders(
            folders,
            remote_paths,
            limit=DISCOVER_FOLDER_LIMIT,
            cursor=self.discover_cursor,
            listed_empty_ids=self.listed_empty_ids,
        )
        for record in chosen:
            if self._stopped():
                break
            folder = remote_by_id.get(record.remote_id)
            if folder is None:
                folder = remote_from_api_item(
                    {
                        "id": record.remote_id,
                        "name": record.remote_path.rsplit("/", 1)[-1],
                        "mimeType": "application/vnd.google-apps.folder",
                        "parents": (),
                        "trashed": False,
                    },
                    record.remote_path,
                )
            try:
                children = list(self.drive.iter_children(record.remote_id))
            except (DriveError, DriveFileNotFound):
                continue
            if children:
                self.listed_empty_ids.discard(record.remote_id)
            else:
                self.listed_empty_ids.add(record.remote_id)
            for item in children:
                if not item.get("id"):
                    continue
                entry = child_item_to_entry(folder, item)
                if not sync_path_allowed(
                    entry.path,
                    self.config.ignore_patterns,
                    self.config.selective_sync_paths,
                    self.config.excluded_sync_paths,
                ):
                    continue
                if entry.id not in remote_by_id:
                    self.changed_remote_ids.add(entry.id)
                self.remote_entries = upsert_remote_entry(self.remote_entries, entry)
                remote_by_id[entry.id] = entry

    def _plan_and_apply(self, write_status: Callable) -> dict[str, int]:
        assert self.drive is not None
        self.records = [
            record
            for record in self.records
            if sync_path_allowed(
                record.local_path,
                self.config.ignore_patterns,
                self.config.selective_sync_paths,
                self.config.excluded_sync_paths,
            )
            and sync_path_allowed(
                record.remote_path,
                self.config.ignore_patterns,
                self.config.selective_sync_paths,
                self.config.excluded_sync_paths,
            )
        ]
        gone = self._confirm_bound_local_paths()
        if gone:
            self.recent_local_deletes |= gone
            for path in sorted(gone):
                self.pending_events.append(KernelEvent("deleted", path))
        local_entries = list(self.local_by_path.values())
        events = list(self.pending_events)
        self.pending_events = []
        if not self.paired:
            self.records, actions = pair_once(
                local_entries,
                self.remote_entries,
                self.records,
                self.config.conflict_policy,
            )
        else:
            new_ids = set(self.changed_remote_ids)
            known = {record.remote_id for record in self.records}
            bound_folders = {record.local_path for record in self.records if record.kind == "folder"}
            if self.authoritative_remote:
                new_ids.update(item.id for item in self.remote_entries if not item.trashed and item.id not in known)
            else:
                for item in self.remote_entries:
                    if item.trashed or item.id in known:
                        continue
                    parent = item.path.rsplit("/", 1)[0] if "/" in item.path else ""
                    if parent in bound_folders:
                        new_ids.add(item.id)
            actions = coalesce_actions(
                plan_local_events(
                    events,
                    self.local_by_path,
                    self.remote_entries,
                    self.records,
                    self.config.conflict_policy,
                ),
                plan_remote_changes(
                    removed_ids=self.removed_remote_ids,
                    changed_ids=self.changed_remote_ids,
                    remote_entries=self.remote_entries,
                    local_by_path=self.local_by_path,
                    records=self.records,
                    conflict_policy=self.config.conflict_policy,
                ),
                plan_new_remote_ids(
                    self.remote_entries,
                    self.records,
                    self.local_by_path,
                    new_ids=new_ids,
                    conflict_policy=self.config.conflict_policy,
                ),
                plan_bound_objects(
                    self.records,
                    local_entries,
                    self.remote_entries,
                    removed_ids=self.removed_remote_ids,
                    confirmed_missing_ids=self.confirmed_missing_ids,
                    recent_local_deletes=self.recent_local_deletes,
                    conflict_policy=self.config.conflict_policy,
                ),
            )
        write_status(
            summarize(actions),
            phase="planning",
            detail={
                "local_entries": len(local_entries),
                "remote_entries": len(self.remote_entries),
                "mode": "pairing" if not self.paired else "event",
                "paired": self.paired,
            },
        )
        if self.config.allow_writes and not self.config.dry_run:
            max_actions = self.config.daemon_max_actions or None
            write_status(summarize(actions), phase="applying", detail={"max_actions": max_actions})

            def progress(result, executed_count, total_executable, action, stage) -> None:
                current = _operation_payload(action, stage, executed_count, total_executable)
                if stage in {"executed", "skipped", "failed"}:
                    if stage == "failed" and result.errors:
                        current["error"] = result.errors[-1]
                    self.recent_activity.insert(0, current)
                    del self.recent_activity[25:]
                write_status(
                    summarize(actions),
                    phase="applying",
                    detail={
                        "executed": len(result.executed),
                        "errors": len(result.errors),
                        "skipped": len(result.skipped),
                        "executed_count": executed_count,
                        "max_actions": max_actions,
                        "total_executable": total_executable,
                        "current_action": action.action,
                        "current_path": action.path,
                        "current_stage": stage,
                        "mode": "pairing" if not self.paired else "event",
                    },
                    current_operation=current if stage == "started" else None,
                    recent_operations=self.recent_activity,
                )

            self.busy_paths = {action.path for action in actions if action.path}
            try:
                result = execute_plan(
                    self.config.sync_root,
                    self.drive,
                    actions,
                    local_entries,
                    self.remote_entries,
                    allow_deletes=self.config.allow_deletes,
                    max_actions=max_actions,
                    should_stop=self.should_stop,
                    progress_callback=progress,
                )
            finally:
                self.busy_paths = set()
            self._bind_created(result.created)
            local_after = self._stat_executed_local_paths(result.executed)
            self.records = records_from_executed(self.records, actions, result.executed, local_after=local_after)
            still_local = {record.remote_id for record in self.records if record.local_path in self.local_by_path}
            self.removed_remote_ids &= still_local
            self.confirmed_missing_ids &= still_local
            removed = {action.remote_id for action in actions if action.action == "index_remove" and action.remote_id}
            if removed:
                self.records = [record for record in self.records if record.remote_id not in removed]
            self.tombstones = tombstones_from_actions(self.tombstones, actions, result.executed)
            executed_local = checkpoint_local_entries(local_entries, actions, result.executed)
            self.local_by_path.update({item.path: item for item in executed_local})
            for marker in result.executed:
                if marker.startswith("local_delete:") or marker.startswith("remote_trash:"):
                    path = marker.split(":", 1)[1]
                    if marker.startswith("local_delete:"):
                        self.local_by_path.pop(path, None)
            uploaded = {marker.split(":", 1)[1] for marker in result.executed if marker.startswith("upload_create:")}
            self.recent_local_creates -= uploaded
            trashed = {marker.split(":", 1)[1] for marker in result.executed if marker.startswith("remote_trash:")}
            self.recent_local_deletes -= trashed
            # Wegen max_actions zurueckgestellte Creates nicht verwerfen:
            # sonst warten ungebundene Dateien bis zum naechsten 30-min-Reconcile.
            for marker in result.skipped:
                if not marker.startswith("max_actions_deferred:"):
                    continue
                parts = marker.split(":", 2)
                if len(parts) != 3:
                    continue
                deferred_action, deferred_path = parts[1], parts[2]
                if deferred_action == "upload_create" and deferred_path in self.local_by_path:
                    self.pending_events.append(KernelEvent("created", deferred_path))
                elif deferred_action == "download_create":
                    for action in actions:
                        if action.action == "download_create" and action.path == deferred_path and action.remote_id:
                            self.changed_remote_ids.add(action.remote_id)
            if any(marker.startswith(("upload_create:", "download_create:", "remote_trash:")) for marker in result.executed):
                self._pull_remote_changes()
            if not self.paired:
                self.paired = True
            self.authoritative_remote = False
            self.changed_remote_ids = set()
        elif not self.paired:
            self.paired = True
        self._persist(actions)
        summary = summarize(actions)
        write_status(
            summary,
            phase="idle",
            detail={
                "local_entries": len(self.local_by_path),
                "remote_entries": len(self.remote_entries),
                "mode": "event" if self.paired else "pairing",
            },
        )
        return summary

    def _stat_executed_local_paths(self, executed: list[str]) -> dict[str, LocalEntry]:
        """Fresh on-disk state for paths we just wrote.

        Records must reflect the actual file after a download/upload, otherwise
        the next tick detects a phantom local change and re-uploads (ping-pong).
        """
        paths = {
            marker.split(":", 1)[1]
            for marker in executed
            if marker.startswith(("download_create:", "download_update:", "upload_create:", "upload_update:"))
        }
        out: dict[str, LocalEntry] = {}
        for path in paths:
            entry = local_entry_from_path(
                self.config.sync_root,
                path,
                self.config.hash_limit_bytes,
                {},
                self.config.sync_symlinks,
            )
            if entry is not None:
                out[path] = entry
        return out

    def _bind_created(self, created: dict[str, dict]) -> None:
        for path, item in created.items():
            if not item.get("id"):
                continue
            entry = remote_from_api_item(item, path)
            self.remote_entries = upsert_remote_entry(self.remote_entries, entry)
            local = self.local_by_path.get(path)
            if local is None:
                continue
            self.records = [
                record for record in self.records if record.remote_id != entry.id and record.local_path != path
            ]
            self.records.append(record_from_pair(local, entry))
            if entry.kind == "folder":
                # New local folder uploaded: immediately enqueue its children.
                # They were scanned on the create event but had no events of
                # their own; without this they wait for the 30-min reconcile.
                prefix = f"{path}/"
                for child_path in sorted(self.local_by_path):
                    if child_path.startswith(prefix):
                        child = self.local_by_path[child_path]
                        self.pending_events.append(
                            KernelEvent("created", child_path, is_dir=child.kind == "folder")
                        )

    def _persist(self, actions) -> None:
        state = State(self.config.db_path)
        try:
            state.replace_local(list(self.local_by_path.values()))
            state.replace_remote(self.remote_entries)
            state.replace_plan(actions)
            removed = {action.remote_id for action in actions if action.action == "index_remove" and action.remote_id}
            if removed:
                state.delete_sync_records(removed)
            state.upsert_sync_records(self.records)
            state.replace_delete_tombstones(self.tombstones)
            if self.changes_token:
                state.set_meta("changes_page_token", self.changes_token)
            state.set_meta("last_scan", str(int(time.time())))
            if self.paired:
                state.set_meta(PAIRED_META_KEY, "1")
            state.set_meta(DISCOVER_CURSOR_META, str(self.discover_cursor))
            state.set_meta(DISCOVER_LISTED_EMPTY_META, json.dumps(sorted(self.listed_empty_ids)))
            state.set_meta("recent_local_deletes", json.dumps(sorted(self.recent_local_deletes)[-2000:]))
            for key, value in summarize(actions).items():
                state.set_meta(f"last_plan_{key}", str(value))
        finally:
            state.close()

    def _wait_for_work(self) -> None:
        while not self._stopped():
            if self.watcher is not None and self.watcher.has_events():
                time.sleep(0.25)
                return
            if time.time() >= min(self.next_remote_poll, self.next_reconcile):
                return
            time.sleep(0.2)

    def _stopped(self) -> bool:
        return bool(self.should_stop and self.should_stop())

    def _raise_if_stop(self) -> None:
        if self._stopped():
            raise StopRequested()


def _operation_payload(action, stage: str, executed_count: int, total_executable: int) -> dict:
    return {
        "updated_at": int(time.time()),
        "stage": stage,
        "action": action.action,
        "label": action.action.replace("_", " "),
        "path": action.path,
        "source_path": action.source_path,
        "target_path": action.target_path,
        "remote_id": action.remote_id,
        "kind": (action.local.kind if action.local else action.remote.kind if action.remote else None),
        "size": (action.local.size if action.local else action.remote.size if action.remote else None),
        "executed_count": executed_count,
        "total_executable": total_executable,
    }


def _drive_client(config: Config) -> DriveClient:
    token = valid_access_token(config.client_secret_path, config.token_path)

    def refresh() -> str:
        fresh = refresh_access_token(config.client_secret_path, config.token_path)
        token_value = fresh.get("access_token")
        if not token_value:
            raise AuthError("Token refresh did not return an access_token")
        return str(token_value)

    return DriveClient(token, include_shared_drives=config.include_shared_drives, refresh_access_token=refresh)
