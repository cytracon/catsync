from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import __version__
from .auth import AuthError, authenticate, refresh_access_token, valid_access_token
from .config import default_config_path, load_config, write_default_config
from .daemon import CatsyncDaemon, run_once
from .drive import DriveClient, DriveError
from .executor import ExecuteError, execute_plan
from .inventory import build_remote_inventory, scan_local
from .models import PlanAction
from .kernel import PAIRED_META_KEY, is_paired, pair_once
from .planner import (
    build_plan,
    build_records_from_current,
    checkpoint_local_entries,
    records_from_executed,
    retain_delete_records,
    summarize,
    tombstones_from_actions,
)
from .state import State


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="catsyncd")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--version", action="version", version=f"catsyncd {__version__}")
    parser.add_argument(
        "--status",
        action="store_true",
        help="compact JSON for the Omarchy bar plugin (no secrets or paths)",
    )
    sub = parser.add_subparsers(dest="cmd", required=False)

    init_p = sub.add_parser("init", help="write a default config")
    init_p.add_argument("--force", action="store_true")

    auth_p = sub.add_parser("auth", help="authorize Google Drive")
    auth_p.add_argument("--readonly", action="store_true", help="request readonly Drive scope instead of full sync scope")

    import_p = sub.add_parser("import-oauth-client", help="install a downloaded Google OAuth client JSON")
    import_p.add_argument("json_path", type=Path)

    sub.add_parser("doctor", help="check config/token/db paths")
    sub.add_parser("scan-local", help="scan local sync root")
    sub.add_parser("scan-remote", help="scan Google Drive metadata")
    plan_p = sub.add_parser("plan", help="build a dry-run sync plan")
    plan_p.add_argument("--summary-only", action="store_true", help="print only action counts")
    sync_p = sub.add_parser("sync", help="run one sync pass")
    sync_p.add_argument("--apply", action="store_true", help="execute planned upload/download/move actions")
    sync_p.add_argument("--allow-deletes", action="store_true", help="also execute local trash and remote trash actions in this CLI run")
    sync_p.add_argument("--no-deletes", action="store_true", help="block delete/trash actions in this CLI run")
    sync_p.add_argument("--max-actions", type=int, default=None, help="maximum number of write actions to execute in this pass")
    sync_p.add_argument("--summary-only", action="store_true", help="print only action counts and execution result")
    sub.add_parser("index", help="accept current matching local/remote state into the sync index")
    conflicts_p = sub.add_parser("conflicts", help="list stored conflict actions")
    conflicts_p.add_argument("--summary-only", action="store_true", help="print only conflict counts")
    resolve_p = sub.add_parser("resolve-conflict", help="resolve one stored conflict action")
    resolve_p.add_argument("path")
    resolve_p.add_argument(
        "--strategy",
        choices=("keep-both", "prefer-local", "prefer-remote", "prefer-newer"),
        required=True,
        help="how to resolve conflict_copy_local actions",
    )
    sub.add_parser("daemon", help="run polling daemon in foreground")
    sub.add_parser("status", help="print stored plan summary")

    args = parser.parse_args(argv)
    try:
        if args.status:
            cfg = load_config(args.config)
            _print_json(_compact_status_payload(cfg))
            return 0
        if not args.cmd:
            parser.error("command required")
        if args.cmd == "init":
            path = write_default_config(args.config or default_config_path(), force=args.force)
            print(path)
            return 0

        cfg = load_config(args.config)

        if args.cmd == "auth":
            path = authenticate(cfg.client_secret_path, cfg.token_path, full_access=not args.readonly)
            print(path)
            return 0
        if args.cmd == "import-oauth-client":
            path = _import_oauth_client(args.json_path, cfg.client_secret_path)
            print(path)
            return 0
        if args.cmd == "doctor":
            return _doctor(cfg)
        if args.cmd == "scan-local":
            previous = _previous_local_entries(cfg)
            entries = scan_local(
                cfg.sync_root,
                cfg.hash_limit_bytes,
                cfg.ignore_patterns,
                cfg.selective_sync_paths,
                cfg.excluded_sync_paths,
                cfg.sync_symlinks,
                previous,
            )
            _print_json([e.__dict__ for e in entries])
            return 0
        if args.cmd == "scan-remote":
            remote = _scan_remote(cfg)
            _print_json([e.__dict__ | {"parents": list(e.parents)} for e in remote])
            return 0
        if args.cmd == "plan":
            previous = _previous_local_entries(cfg)
            local = scan_local(cfg.sync_root, cfg.hash_limit_bytes, cfg.ignore_patterns, cfg.selective_sync_paths, cfg.excluded_sync_paths, cfg.sync_symlinks, previous)
            remote = _scan_remote(cfg)
            state = State(cfg.db_path)
            try:
                records = state.get_sync_records()
                previous_remote = state.get_remote_entries()
                tombstones = state.get_delete_tombstones()
                if is_paired(state.get_meta(PAIRED_META_KEY), records):
                    actions = build_plan(local, remote, records, cfg.conflict_policy, previous, previous_remote, tombstones)
                else:
                    records, actions = pair_once(local, remote, records, cfg.conflict_policy)
                    state.set_meta(PAIRED_META_KEY, "1")
                state.replace_local(local)
                state.replace_remote(remote)
                state.replace_plan(actions)
                state.replace_delete_tombstones(tombstones_from_actions(tombstones, actions))
                state.upsert_sync_records(records)
            finally:
                state.close()
            payload = {"summary": summarize(actions)}
            if not args.summary_only:
                payload["actions"] = [_action_dict(a) for a in actions]
            _print_json(payload)
            return 0
        if args.cmd == "sync":
            previous = _previous_local_entries(cfg)
            local = scan_local(cfg.sync_root, cfg.hash_limit_bytes, cfg.ignore_patterns, cfg.selective_sync_paths, cfg.excluded_sync_paths, cfg.sync_symlinks, previous)
            drive = _drive_client(cfg)
            remote = build_remote_inventory(
                drive.iter_files(),
                cfg.ignore_patterns,
                cfg.selective_sync_paths,
                cfg.excluded_sync_paths,
                root_ids=("root", drive.get_root_id()),
            )
            state = State(cfg.db_path)
            try:
                records = state.get_sync_records()
                previous_remote = state.get_remote_entries()
                tombstones = state.get_delete_tombstones()
                actions = build_plan(local, remote, records, cfg.conflict_policy, previous, previous_remote, tombstones)
                result = None
                if args.apply:
                    if not cfg.allow_writes:
                        raise ExecuteError("config allow_writes is false; set it true before using sync --apply")
                    allow_deletes = bool(args.allow_deletes and not args.no_deletes)
                    result = execute_plan(
                        cfg.sync_root,
                        drive,
                        actions,
                        local,
                        remote,
                        allow_deletes=allow_deletes,
                        max_actions=args.max_actions,
                    )
                    records = records_from_executed(records, actions, result.executed)
                    tombstones = tombstones_from_actions(tombstones, actions, result.executed)
                    previous = checkpoint_local_entries(local, actions, result.executed)
                    previous_remote = list(remote)
                    state.replace_local(previous)
                    state.replace_sync_records(retain_delete_records(records, records, actions))
                    state.replace_delete_tombstones(tombstones)
                    local = scan_local(cfg.sync_root, cfg.hash_limit_bytes, cfg.ignore_patterns, cfg.selective_sync_paths, cfg.excluded_sync_paths, cfg.sync_symlinks, local)
                    remote = build_remote_inventory(
                        drive.iter_files(),
                        cfg.ignore_patterns,
                        cfg.selective_sync_paths,
                        cfg.excluded_sync_paths,
                        root_ids=("root", drive.get_root_id()),
                    )
                    actions = build_plan(local, remote, records, cfg.conflict_policy, previous, previous_remote, tombstones)
                    tombstones = tombstones_from_actions(tombstones, actions)
                state.replace_local(local)
                state.replace_remote(remote)
                state.replace_plan(actions)
                state.replace_sync_records(retain_delete_records(build_records_from_current(local, remote), records, actions))
                state.replace_delete_tombstones(tombstones)
                if is_paired(state.get_meta(PAIRED_META_KEY), records):
                    state.set_meta(PAIRED_META_KEY, "1")
            finally:
                state.close()
            payload = {"summary": summarize(actions)}
            if not args.summary_only:
                payload["actions"] = [_action_dict(a) for a in actions]
            if result:
                payload["result"] = result.__dict__
            _print_json(payload)
            return 0
        if args.cmd == "index":
            previous = _previous_local_entries(cfg)
            local = scan_local(cfg.sync_root, cfg.hash_limit_bytes, cfg.ignore_patterns, cfg.selective_sync_paths, cfg.excluded_sync_paths, cfg.sync_symlinks, previous)
            remote = _scan_remote(cfg)
            records = build_records_from_current(local, remote)
            state = State(cfg.db_path)
            try:
                state.replace_local(local)
                state.replace_remote(remote)
                state.replace_sync_records(records)
            finally:
                state.close()
            _print_json({"indexed": len(records)})
            return 0
        if args.cmd == "conflicts":
            state = State(cfg.db_path)
            try:
                actions = state.conflict_actions()
            finally:
                state.close()
            payload = {"summary": summarize(actions)}
            if not args.summary_only:
                payload["actions"] = [_action_dict(action) for action in actions]
            _print_json(payload)
            return 0
        if args.cmd == "resolve-conflict":
            _print_json(_resolve_conflict(cfg, args.path, args.strategy))
            return 0
        if args.cmd == "daemon":
            CatsyncDaemon(cfg).run_forever()
            return 0
        if args.cmd == "status":
            _print_json(_status_payload(cfg))
            return 0
    except (AuthError, DriveError, ExecuteError, OSError, ValueError, sqlite3.Error) as exc:
        print(f"catsyncd: {exc}", file=sys.stderr)
        return 2
    return 1


def _scan_remote(cfg):
    drive = _drive_client(cfg)
    return build_remote_inventory(
        drive.iter_files(),
        cfg.ignore_patterns,
        cfg.selective_sync_paths,
        cfg.excluded_sync_paths,
        root_ids=("root", drive.get_root_id()),
    )


def _previous_local_entries(cfg):
    state = State(cfg.db_path)
    try:
        return state.get_local_entries()
    finally:
        state.close()


def _drive_client(cfg) -> DriveClient:
    token = valid_access_token(cfg.client_secret_path, cfg.token_path)

    def refresh() -> str:
        fresh = refresh_access_token(cfg.client_secret_path, cfg.token_path)
        token_value = fresh.get("access_token")
        if not token_value:
            raise AuthError("Token refresh did not return an access_token")
        return str(token_value)

    return DriveClient(token, include_shared_drives=cfg.include_shared_drives, refresh_access_token=refresh)


def _resolve_conflict(cfg, path: str, strategy: str) -> dict:
    if not cfg.allow_writes:
        raise ExecuteError("config allow_writes is false; enable writes before resolving conflicts")
    phase = _daemon_phase(cfg)
    if phase not in {"idle", "stopped", "unknown"}:
        raise ExecuteError(f"daemon is busy ({phase}); resolve conflicts when Catsync is idle")
    state = State(cfg.db_path)
    try:
        conflict = state.plan_action(path)
    finally:
        state.close()
    if conflict is None:
        raise ExecuteError(f"no stored conflict action for path: {path}")
    if conflict.action != "conflict_copy_local":
        raise ExecuteError(f"{conflict.action} requires manual review: {path}")
    if conflict.local is None or conflict.remote is None:
        raise ExecuteError(f"conflict is missing local or remote metadata: {path}")
    if conflict.local.kind != "file" or conflict.remote.kind != "file":
        raise ExecuteError(f"only file conflicts can be auto-resolved: {path}")
    if strategy == "prefer-newer":
        strategy = _newer_resolution_strategy(conflict)
    if conflict.remote.mime_type.startswith("application/vnd.google-apps") and strategy in {"keep-both", "prefer-local"}:
        raise ExecuteError(f"Google Workspace conflicts cannot be overwritten from a local placeholder: {path}")

    drive = _drive_client(cfg)
    actions = _resolution_actions(conflict, strategy)
    result = execute_plan(
        cfg.sync_root,
        drive,
        actions,
        [conflict.local],
        [conflict.remote],
        allow_deletes=False,
        max_actions=len(actions),
    )
    if result.errors:
        raise ExecuteError("; ".join(result.errors))
    state = State(cfg.db_path)
    try:
        state.delete_plan_action(conflict.action, conflict.path)
    finally:
        state.close()
    return {
        "path": path,
        "strategy": strategy,
        "executed": result.executed,
        "skipped": result.skipped,
        "errors": result.errors,
    }


def _resolution_actions(conflict: PlanAction, strategy: str) -> list[PlanAction]:
    if strategy == "prefer-local":
        return [
            PlanAction(
                "upload_update",
                conflict.path,
                "resolved conflict by preferring local",
                local=conflict.local,
                remote=conflict.remote,
                remote_id=conflict.remote_id,
            )
        ]
    if strategy == "prefer-remote":
        return [
            PlanAction(
                "download_update",
                conflict.path,
                "resolved conflict by preferring remote",
                local=conflict.local,
                remote=conflict.remote,
                remote_id=conflict.remote_id,
            )
        ]
    if strategy == "keep-both":
        return [
            PlanAction(
                "conflict_copy_local",
                conflict.path,
                "resolved conflict by keeping remote copy",
                local=conflict.local,
                remote=conflict.remote,
                remote_id=conflict.remote_id,
            ),
            PlanAction(
                "upload_update",
                conflict.path,
                "resolved conflict by uploading local after keeping copy",
                local=conflict.local,
                remote=conflict.remote,
                remote_id=conflict.remote_id,
            ),
        ]
    raise ExecuteError(f"unknown conflict resolution strategy: {strategy}")


def _newer_resolution_strategy(conflict: PlanAction) -> str:
    if conflict.local is None or conflict.remote is None:
        raise ExecuteError(f"conflict is missing local or remote metadata: {conflict.path}")
    if conflict.local.mtime_ns is None or not conflict.remote.modified_time:
        raise ExecuteError(f"newer side is unknown: {conflict.path}")
    try:
        local_ts = conflict.local.mtime_ns / 1_000_000_000
        remote_ts = datetime.fromisoformat(conflict.remote.modified_time.replace("Z", "+00:00")).timestamp()
    except ValueError as exc:
        raise ExecuteError(f"remote modified time is invalid: {conflict.path}") from exc
    if abs(local_ts - remote_ts) < 1:
        raise ExecuteError(f"newer side is ambiguous: {conflict.path}")
    return "prefer-local" if local_ts > remote_ts else "prefer-remote"


def _daemon_phase(cfg) -> str:
    status_path = Path(cfg.db_path).with_name("status.json")
    if not status_path.exists():
        return "unknown"
    try:
        data = json.loads(status_path.read_text())
    except json.JSONDecodeError:
        return "unknown"
    return str(data.get("phase") or "unknown")


def _daemon_active() -> bool:
    return subprocess.run(
        ["systemctl", "--user", "is-active", "--quiet", "catsyncd.service"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0


def _compact_status_payload(cfg) -> dict:
    """Bar-plugin snapshot. No host, token, client secret, or file paths."""
    status_path = Path(cfg.db_path).with_name("status.json")
    phase = "stopped"
    updated_at = ""
    if status_path.exists():
        try:
            daemon = json.loads(status_path.read_text())
        except json.JSONDecodeError:
            daemon = {}
        phase = str(daemon.get("phase") or phase)
        if daemon.get("updated_at") is not None:
            updated_at = str(daemon.get("updated_at"))
    pending = 0
    conflicts = 0
    last_scan = ""
    try:
        state = State(cfg.db_path)
        try:
            summary = state.plan_summary() or {}
            pending = sum(int(v) for v in summary.values() if isinstance(v, int))
            conflicts = int(summary.get("conflict_copy_local") or 0) + int(summary.get("conflict_review") or 0)
            last_scan = str(state.get_meta("last_scan") or "")
        finally:
            state.close()
    except (OSError, sqlite3.Error):
        pass
    oauth = cfg.client_secret_path.exists()
    authorized = cfg.token_path.exists()
    active = _daemon_active()
    if active and phase in {"stopped", "unknown", ""}:
        phase = "running"
    elif not active and phase not in {"auth_required", "drive_error", "network_error"}:
        phase = "stopped"
    return {
        "version": __version__,
        "configured": bool(oauth and authorized),
        "oauth_client": bool(oauth),
        "authorized": bool(authorized),
        "daemon_active": bool(active),
        "phase": phase,
        "dry_run": bool(cfg.dry_run),
        "allow_writes": bool(cfg.allow_writes),
        "allow_deletes": bool(cfg.allow_deletes),
        "pending": pending,
        "conflicts": conflicts,
        "last_scan": last_scan,
        "updated_at": updated_at,
    }


def _status_payload(cfg) -> dict:
    status_path = Path(cfg.db_path).with_name("status.json")
    daemon = {}
    if status_path.exists():
        try:
            daemon = json.loads(status_path.read_text())
        except json.JSONDecodeError:
            daemon = {"phase": "unknown", "error": "invalid status.json"}
    state = State(cfg.db_path)
    try:
        payload = {
            "summary": state.plan_summary(),
            "last_scan": state.get_meta("last_scan"),
            "daemon": daemon,
        }
        actions = state.list_plan_actions(_visible_status_actions(), limit=200)
        payload["actions"] = [_action_dict(action) for action in actions]
        return payload
    finally:
        state.close()


def _visible_status_actions() -> tuple[str, ...]:
    return (
        "download_create",
        "download_update",
        "upload_create",
        "upload_update",
        "local_move",
        "remote_move",
        "local_delete",
        "remote_trash",
        "conflict_copy_local",
        "conflict_review",
    )


def _doctor(cfg) -> int:
    checks = {
        "config": str(default_config_path()),
        "sync_root": str(cfg.sync_root),
        "client_secret_exists": cfg.client_secret_path.exists(),
        "token_exists": cfg.token_path.exists(),
        "db_path": str(cfg.db_path),
        "dry_run": cfg.dry_run,
        "allow_writes": cfg.allow_writes,
        "allow_deletes": cfg.allow_deletes,
    }
    _print_json(checks)
    return 0 if checks["client_secret_exists"] else 1


def _import_oauth_client(source: Path, target: Path) -> Path:
    if not source.exists():
        raise ValueError(f"OAuth client JSON does not exist: {source}")
    data = json.loads(source.read_text())
    section = data.get("installed") or data.get("web")
    if not section or not section.get("client_id"):
        raise ValueError("OAuth client JSON must contain installed.client_id or web.client_id")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    target.chmod(0o600)
    return target


def _action_dict(action):
    return {
        "action": action.action,
        "path": action.path,
        "reason": action.reason,
        "local": action.local.__dict__ if action.local else None,
        "remote": (action.remote.__dict__ | {"parents": list(action.remote.parents)}) if action.remote else None,
        "source_path": action.source_path,
        "target_path": action.target_path,
        "remote_id": action.remote_id,
    }


def _print_json(data) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))
