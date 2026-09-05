from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .drive import DriveClient, DriveFileNotFound
from .inventory import GOOGLE_DOC_EXTENSIONS, google_doc_placeholder, remote_entry_to_item
from .models import LocalEntry, PlanAction, RemoteEntry


class ExecuteError(RuntimeError):
    pass


@dataclass
class ExecuteResult:
    executed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    created: dict[str, dict] = field(default_factory=dict)


def execute_plan(
    sync_root: Path,
    drive: DriveClient,
    actions: list[PlanAction],
    local_entries: list[LocalEntry],
    remote_entries: list[RemoteEntry],
    allow_deletes: bool = False,
    max_actions: int | None = None,
    should_stop: Callable[[], bool] | None = None,
    progress_callback: Callable[[ExecuteResult, int, int, PlanAction, str], None] | None = None,
) -> ExecuteResult:
    root = sync_root.expanduser().resolve()
    local_by_path = {item.path: item for item in local_entries}
    remote_by_id = {item.id: item for item in remote_entries if not item.trashed}
    remote_by_path = {item.path: item for item in remote_entries if not item.trashed}
    remote_folder_ids = {item.path: item.id for item in remote_entries if item.kind == "folder" and not item.trashed}
    result = ExecuteResult()

    executed_count = 0
    actions = _prioritize_actions(actions)
    total_executable = sum(1 for action in actions if _would_execute(action, allow_deletes))
    for action in actions:
        would_execute = _would_execute(action, allow_deletes)
        if should_stop and should_stop():
            result.skipped.append("stop_requested")
            return result
        if max_actions is not None and executed_count >= max_actions and would_execute:
            result.skipped.append(f"max_actions_deferred:{action.action}:{action.path}")
            if progress_callback:
                progress_callback(result, executed_count, total_executable, action, "skipped")
            continue
        before_executed = executed_count
        before_skipped = len(result.skipped)
        before_errors = len(result.errors)
        if progress_callback and would_execute:
            progress_callback(result, executed_count, total_executable, action, "started")
        try:
            if action.action == "index_update" or action.action == "index_remove":
                result.skipped.append(f"{action.action}:{action.path}")
            elif action.action == "download_create":
                _download(root, drive, action.remote, replace=False)
                result.executed.append(f"download_create:{action.path}")
                executed_count += 1
            elif action.action == "download_update":
                _download(root, drive, action.remote, replace=True)
                result.executed.append(f"download_update:{action.path}")
                executed_count += 1
            elif action.action == "upload_create":
                if action.local is None:
                    raise ExecuteError("upload_create missing local entry")
                created = _upload_create(root, drive, action.local, remote_folder_ids, remote_by_path)
                if created:
                    result.executed.append(f"upload_create:{action.path}")
                    result.created[action.path] = created
                    executed_count += 1
                else:
                    result.skipped.append(f"upload_create_deferred:{action.path}")
            elif action.action == "upload_update":
                if action.local is None or action.remote_id is None:
                    raise ExecuteError("upload_update missing local entry or remote id")
                _upload_update(root, drive, action.local, action.remote_id)
                result.executed.append(f"upload_update:{action.path}")
                executed_count += 1
            elif action.action == "local_move":
                _local_move(root, action.source_path, action.target_path)
                result.executed.append(f"local_move:{action.source_path}->{action.target_path}")
                executed_count += 1
            elif action.action == "remote_move":
                if action.remote_id is None or action.target_path is None:
                    raise ExecuteError("remote_move missing remote id or target path")
                remote = remote_by_id.get(action.remote_id)
                if remote is None:
                    raise ExecuteError(f"remote_move target no longer exists: {action.remote_id}")
                _remote_move(drive, action.remote_id, action.target_path, remote, remote_folder_ids)
                result.executed.append(f"remote_move:{action.source_path}->{action.target_path}")
                executed_count += 1
            elif action.action == "local_delete":
                if not allow_deletes:
                    result.skipped.append(f"local_delete_blocked:{action.path}")
                else:
                    _local_trash(root, action.path)
                    result.executed.append(f"local_delete:{action.path}")
                    executed_count += 1
            elif action.action == "remote_trash":
                if not allow_deletes:
                    result.skipped.append(f"remote_trash_blocked:{action.path}")
                elif action.remote_id:
                    try:
                        drive.trash_file(action.remote_id)
                    except DriveFileNotFound:
                        pass
                    result.executed.append(f"remote_trash:{action.path}")
                    executed_count += 1
            elif action.action == "conflict_copy_local":
                _write_remote_conflict_copy(root, drive, action.remote)
                result.executed.append(f"conflict_copy_local:{action.path}")
                executed_count += 1
            elif action.action == "conflict_review":
                result.skipped.append(f"conflict_review:{action.path}")
            else:
                result.skipped.append(f"unknown:{action.action}:{action.path}")
        except Exception as exc:  # keep daemon alive and report all attempted actions
            result.errors.append(f"{action.action}:{action.path}:{exc}")
        finally:
            if progress_callback and would_execute:
                if len(result.errors) > before_errors:
                    stage = "failed"
                elif executed_count > before_executed:
                    stage = "executed"
                elif len(result.skipped) > before_skipped:
                    stage = "skipped"
                else:
                    stage = "skipped"
                progress_callback(result, executed_count, total_executable, action, stage)
    return result


def _prioritize_actions(actions: list[PlanAction]) -> list[PlanAction]:
    deletes = [action for action in actions if action.action in {"remote_trash", "local_delete"}]
    others = [action for action in actions if action.action not in {"remote_trash", "local_delete"}]
    return deletes + others


def _would_execute(action: PlanAction, allow_deletes: bool) -> bool:
    if action.action in {
        "download_create",
        "download_update",
        "upload_create",
        "upload_update",
        "local_move",
        "remote_move",
        "conflict_copy_local",
    }:
        return True
    if action.action in {"local_delete", "remote_trash"}:
        return allow_deletes
    return False


def _download(root: Path, drive: DriveClient, remote: RemoteEntry | None, replace: bool) -> None:
    if remote is None:
        raise ExecuteError("download missing remote entry")
    target = _safe_target(root, remote.path)
    if remote.kind == "folder":
        target.mkdir(parents=True, exist_ok=True)
        return
    if target.exists() and not replace:
        # Already on disk; bind the ID. Do not write a conflict copy just
        # because the local scan missed the path this tick.
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if remote.mime_type in GOOGLE_DOC_EXTENSIONS:
        data = google_doc_placeholder(remote).encode()
    else:
        data = drive.download_bytes(remote.id)
    _atomic_write(target, data)


def _upload_create(
    root: Path,
    drive: DriveClient,
    local: LocalEntry,
    remote_folder_ids: dict[str, str],
    remote_by_path: dict[str, RemoteEntry],
) -> dict | None:
    existing = remote_by_path.get(local.path)
    if existing is not None:
        if local.kind == "file" and existing.kind == "file":
            if not (local.md5 and existing.md5 and local.md5 == existing.md5):
                drive.update_file_content(existing.id, _safe_target(root, local.path))
            return remote_entry_to_item(existing)
        if local.kind == "folder" and existing.kind == "folder":
            remote_folder_ids[local.path] = existing.id
            return remote_entry_to_item(existing)
        return remote_entry_to_item(existing)
    parent_path, name = _split_parent(local.path)
    parent_id = _ensure_remote_folder_path(drive, parent_path, remote_folder_ids)
    if local.kind == "folder":
        created = drive.create_folder(name, parent_id)
        remote_folder_ids[local.path] = created["id"]
        return created
    if local.kind == "file":
        return drive.upload_file(_safe_target(root, local.path), name, parent_id)
    if local.kind == "symlink":
        target_path = _symlink_target_relpath(root, local.path)
        if not target_path:
            return None
        target = remote_by_path.get(target_path)
        if target is None:
            return None
        return drive.create_shortcut(name, target.id, parent_id)
    raise ExecuteError(f"unsupported local kind for upload: {local.kind}")


def _upload_update(root: Path, drive: DriveClient, local: LocalEntry, remote_id: str) -> None:
    if local.kind != "file":
        return
    drive.update_file_content(remote_id, _safe_target(root, local.path))


def _local_move(root: Path, source_path: str | None, target_path: str | None) -> None:
    if not source_path or not target_path:
        raise ExecuteError("local_move missing source or target path")
    source = _safe_target(root, source_path)
    target = _safe_target(root, target_path)
    if not source.exists():
        return
    if target.exists():
        target = _conflict_path(target, "move")
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, target)


def _remote_move(
    drive: DriveClient,
    remote_id: str,
    target_path: str,
    remote: RemoteEntry,
    remote_folder_ids: dict[str, str],
) -> None:
    parent_path, name = _split_parent(target_path)
    parent_id = _ensure_remote_folder_path(drive, parent_path, remote_folder_ids)
    remove_parents = list(remote.parents) or ["root"]
    drive.move_file(remote_id, name, parent_id, remove_parents)


def _local_trash(root: Path, rel_path: str) -> None:
    source = _safe_target(root, rel_path)
    if not source.exists():
        return
    target = root / ".catsync" / "trash" / time.strftime("%Y%m%d-%H%M%S") / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, target)


def _write_remote_conflict_copy(root: Path, drive: DriveClient, remote: RemoteEntry | None) -> None:
    if remote is None or remote.kind == "folder":
        return
    target = _conflict_path(_safe_target(root, remote.path), "remote")
    if remote.mime_type in GOOGLE_DOC_EXTENSIONS:
        data = google_doc_placeholder(remote).encode()
    else:
        data = drive.download_bytes(remote.id)
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(target, data)


def _ensure_remote_folder_path(drive: DriveClient, parent_path: str, remote_folder_ids: dict[str, str]) -> str:
    if not parent_path:
        return "root"
    current_parent = "root"
    built = []
    for part in parent_path.split("/"):
        built.append(part)
        current_path = "/".join(built)
        if current_path not in remote_folder_ids:
            remote_folder_ids[current_path] = drive.create_folder(part, current_parent)["id"]
        current_parent = remote_folder_ids[current_path]
    return current_parent


def _safe_target(root: Path, rel_path: str) -> Path:
    target = (root / rel_path).resolve()
    if root not in target.parents and target != root:
        raise ExecuteError(f"path escapes sync root: {rel_path}")
    return target


def _split_parent(path: str) -> tuple[str, str]:
    if "/" not in path:
        return "", path
    parent, name = path.rsplit("/", 1)
    return parent, name


def _symlink_target_relpath(root: Path, rel_path: str) -> str | None:
    link = _safe_link_path(root, rel_path)
    try:
        target = link.readlink()
    except OSError:
        return None
    if target.is_absolute():
        resolved = target.resolve()
    else:
        resolved = (link.parent / target).resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return None


def _safe_link_path(root: Path, rel_path: str) -> Path:
    target = root / rel_path
    parent = target.parent.resolve()
    if root not in parent.parents and parent != root:
        raise ExecuteError(f"path escapes sync root: {rel_path}")
    return target


def _atomic_write(target: Path, data: bytes) -> None:
    tmp = target.parent / f".{target.name}.catsync-tmp"
    tmp.write_bytes(data)
    os.replace(tmp, target)


def _conflict_path(path: Path, side: str) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return path.with_name(f"{path.stem}.catsync-conflict-{side}-{stamp}{path.suffix}")
