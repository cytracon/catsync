from __future__ import annotations

import json
import signal
import sys
import time
from pathlib import Path
from typing import Callable

from .auth import AuthError
from .config import Config
from .drive import DriveError
from .engine import StopRequested, SyncEngine
from .state import State

RETRYABLE_ERRORS = (AuthError, DriveError, OSError, TimeoutError)
INITIAL_BACKOFF_SECONDS = 5
MAX_BACKOFF_SECONDS = 300


class CatsyncDaemon:
    def __init__(self, config: Config):
        self.config = config
        self.stop_requested = False

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)

        def write_status(summary, phase="unknown", detail=None, current_operation=None, recent_operations=None):
            _write_status(
                self.config,
                summary,
                phase=phase,
                detail=detail,
                current_operation=current_operation,
                recent_operations=recent_operations,
            )

        backoff = INITIAL_BACKOFF_SECONDS
        while not self.stop_requested:
            engine = SyncEngine(self.config, should_stop=lambda: self.stop_requested)
            try:
                engine.run_forever(write_status)
                return
            except StopRequested:
                write_status({}, phase="stopped")
                return
            except RETRYABLE_ERRORS as exc:
                phase = _retry_phase(exc)
                wait = _retry_wait(exc, backoff)
                write_status(
                    {},
                    phase=phase,
                    detail={"error": str(exc), "retry_in_seconds": wait},
                )
                print(f"catsyncd: {phase}: {_short_error(exc)}; retry in {wait}s", file=sys.stderr, flush=True)
                if not self._sleep(wait):
                    write_status({}, phase="stopped")
                    return
                backoff = min(MAX_BACKOFF_SECONDS, max(wait, backoff) * 2)
                continue
            finally:
                watcher = getattr(engine, "watcher", None)
                if watcher is not None:
                    try:
                        watcher.close()
                    except Exception:
                        pass
                    engine.watcher = None

    def _sleep(self, seconds: int) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self.stop_requested:
                return False
            time.sleep(0.5)
        return not self.stop_requested

    def _stop(self, _signum: int, _frame: object) -> None:
        self.stop_requested = True


def run_once(config: Config, should_stop: Callable[[], bool] | None = None) -> dict[str, int]:
    if config.dry_run or not config.allow_writes:
        state = State(config.db_path)
        try:
            summary = state.plan_summary()
            state.set_meta("last_scan", state.get_meta("last_scan") or "")
            return summary
        finally:
            state.close()

    def write_status(summary, phase="unknown", detail=None, current_operation=None, recent_operations=None):
        _write_status(
            config,
            summary,
            phase=phase,
            detail=detail,
            current_operation=current_operation,
            recent_operations=recent_operations,
        )

    engine = SyncEngine(config, should_stop=should_stop)
    engine.bootstrap(write_status)
    state = State(config.db_path)
    try:
        return state.plan_summary()
    finally:
        state.close()


def _retry_phase(exc: BaseException) -> str:
    if isinstance(exc, AuthError):
        return "auth_required"
    if isinstance(exc, DriveError):
        return "drive_error"
    return "network_error"


def _retry_wait(exc: BaseException, backoff: int) -> int:
    text = str(exc)
    if "CONSUMER_INVALID" in text or "has been deleted" in text:
        return min(15, backoff)
    return backoff


def _short_error(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()[0].strip()
    return text[:300] if text else exc.__class__.__name__


def _write_status(
    config: Config,
    summary: dict[str, int],
    phase: str = "unknown",
    detail: dict | None = None,
    current_operation: dict | None = None,
    recent_operations: list[dict] | None = None,
) -> None:
    status_path = Path(config.db_path).with_name("status.json")
    status_path.parent.mkdir(parents=True, exist_ok=True)
    recent = recent_operations if recent_operations is not None else _previous_recent_activity(config)
    payload = {
        "updated_at": int(time.time()),
        "phase": phase,
        "detail": detail or {},
        "sync_root": str(config.sync_root),
        "dry_run": config.dry_run,
        "allow_writes": config.allow_writes,
        "allow_deletes": config.allow_deletes,
        "daemon_max_actions": config.daemon_max_actions,
        "plan": summary,
        "activity": {
            "current": current_operation,
            "recent": recent,
        },
    }
    status_path.write_text(json.dumps(payload, indent=2) + "\n")


def _previous_recent_activity(config: Config) -> list[dict]:
    status_path = Path(config.db_path).with_name("status.json")
    try:
        data = json.loads(status_path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    activity = data.get("activity")
    if not isinstance(activity, dict):
        return []
    recent = activity.get("recent")
    return recent[:25] if isinstance(recent, list) else []
