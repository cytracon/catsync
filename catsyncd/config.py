from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


APP_NAME = "catsyncd"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_NAME
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / APP_NAME
DEFAULT_IGNORE_PATTERNS = (
    "*.tmp",
    "*.temp",
    "*.part",
    "*.crdownload",
    "*.swp",
    "*.swo",
    "*.bak",
    "*.catsync-conflict-*",
    "*.orig",
    "*.rej",
    "*.pyc",
    "*.pyo",
    "*.log",
    "*.pid",
    "*.lock",
    "*.insyncdl",
    "*.sock",
    "title_refresh_idx",
    "last_recap_main_turn",
    "*.grok/sessions/*/*/terminal",
    "*.grok/sessions/*/*/terminal/*",
    "*.grok/sessions/*/terminal",
    "*.grok/sessions/*/terminal/*",
    "*.db-wal",
    "*.db-shm",
    "*.sqlite-wal",
    "*.sqlite-shm",
    "*.solidsync-conflict*",
    "*~",
    ".~lock.*",
    ".nfs*",
    ".DS_Store",
    "desktop.ini",
    "ehthumbs.db",
    "Thumbs.db",
    ".env",
    ".env.*",
    ".coverage",
    "tmp",
    "temp",
    "cache",
    "Cache",
    ".tmp",
    ".temp",
    ".cache",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    ".venv",
    "venv",
    ".parcel-cache",
    ".turbo",
    ".vite",
    ".pnpm-store",
    ".next/cache",
    ".nuxt",
    "coverage",
    "htmlcov",
    "node_modules",
)


@dataclass(frozen=True)
class Config:
    sync_root: Path
    client_secret_path: Path
    token_path: Path
    db_path: Path
    poll_interval: int = 60
    allow_writes: bool = False
    allow_deletes: bool = False
    dry_run: bool = True
    include_shared_drives: bool = False
    hash_limit_bytes: int = 64 * 1024 * 1024
    launch_daemon_at_login: bool = True
    launch_tray_at_login: bool = True
    desktop_notifications: bool = True
    conflict_policy: str = "prefer_newer"
    google_docs_mode: str = "placeholder"
    proxy_mode: str = "system"
    proxy_host: str = ""
    proxy_port: int = 0
    upload_limit_kbps: int = 0
    download_limit_kbps: int = 0
    ignore_patterns: tuple[str, ...] = ()
    selective_sync_paths: tuple[str, ...] = ()
    excluded_sync_paths: tuple[str, ...] = ()
    sync_symlinks: bool = False
    daemon_max_actions: int = 100


def default_config_path() -> Path:
    return CONFIG_DIR / "config.json"


def default_config() -> Config:
    return Config(
        sync_root=Path.home() / "Google Drive",
        client_secret_path=CONFIG_DIR / "client_secret.json",
        token_path=CONFIG_DIR / "token.json",
        db_path=STATE_DIR / "state.db",
        ignore_patterns=DEFAULT_IGNORE_PATTERNS,
    )


def _expand(value: str | os.PathLike[str]) -> Path:
    return Path(value).expanduser().resolve()


def load_config(path: Path | None = None) -> Config:
    base = default_config()
    path = path or default_config_path()
    if not path.exists():
        return base
    data = json.loads(path.read_text())
    return Config(
        sync_root=_expand(data.get("sync_root", str(base.sync_root))),
        client_secret_path=_expand(data.get("client_secret_path", str(base.client_secret_path))),
        token_path=_expand(data.get("token_path", str(base.token_path))),
        db_path=_expand(data.get("db_path", str(base.db_path))),
        poll_interval=int(data.get("poll_interval", base.poll_interval)),
        allow_writes=bool(data.get("allow_writes", base.allow_writes)),
        allow_deletes=bool(data.get("allow_deletes", base.allow_deletes)),
        dry_run=bool(data.get("dry_run", base.dry_run)),
        include_shared_drives=bool(data.get("include_shared_drives", base.include_shared_drives)),
        hash_limit_bytes=int(data.get("hash_limit_bytes", base.hash_limit_bytes)),
        launch_daemon_at_login=bool(data.get("launch_daemon_at_login", base.launch_daemon_at_login)),
        launch_tray_at_login=bool(data.get("launch_tray_at_login", base.launch_tray_at_login)),
        desktop_notifications=bool(data.get("desktop_notifications", base.desktop_notifications)),
        conflict_policy=str(data.get("conflict_policy", base.conflict_policy)),
        google_docs_mode=str(data.get("google_docs_mode", base.google_docs_mode)),
        proxy_mode=str(data.get("proxy_mode", base.proxy_mode)),
        proxy_host=str(data.get("proxy_host", base.proxy_host)),
        proxy_port=int(data.get("proxy_port", base.proxy_port)),
        upload_limit_kbps=int(data.get("upload_limit_kbps", base.upload_limit_kbps)),
        download_limit_kbps=int(data.get("download_limit_kbps", base.download_limit_kbps)),
        ignore_patterns=tuple(str(item) for item in data.get("ignore_patterns", base.ignore_patterns)),
        selective_sync_paths=tuple(str(item).strip("/") for item in data.get("selective_sync_paths", base.selective_sync_paths) if str(item).strip("/")),
        excluded_sync_paths=tuple(str(item).strip("/") for item in data.get("excluded_sync_paths", base.excluded_sync_paths) if str(item).strip("/")),
        sync_symlinks=bool(data.get("sync_symlinks", base.sync_symlinks)),
        daemon_max_actions=max(0, int(data.get("daemon_max_actions", base.daemon_max_actions))),
    )


def write_default_config(path: Path | None = None, force: bool = False) -> Path:
    cfg = default_config()
    path = path or default_config_path()
    if path.exists() and not force:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    cfg.sync_root.mkdir(parents=True, exist_ok=True)
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "sync_root": str(cfg.sync_root),
        "client_secret_path": str(cfg.client_secret_path),
        "token_path": str(cfg.token_path),
        "db_path": str(cfg.db_path),
        "poll_interval": cfg.poll_interval,
        "allow_writes": cfg.allow_writes,
        "allow_deletes": cfg.allow_deletes,
        "dry_run": cfg.dry_run,
        "include_shared_drives": cfg.include_shared_drives,
        "hash_limit_bytes": cfg.hash_limit_bytes,
        "launch_daemon_at_login": cfg.launch_daemon_at_login,
        "launch_tray_at_login": cfg.launch_tray_at_login,
        "desktop_notifications": cfg.desktop_notifications,
        "conflict_policy": cfg.conflict_policy,
        "google_docs_mode": cfg.google_docs_mode,
        "proxy_mode": cfg.proxy_mode,
        "proxy_host": cfg.proxy_host,
        "proxy_port": cfg.proxy_port,
        "upload_limit_kbps": cfg.upload_limit_kbps,
        "download_limit_kbps": cfg.download_limit_kbps,
        "ignore_patterns": list(cfg.ignore_patterns),
        "selective_sync_paths": list(cfg.selective_sync_paths),
        "excluded_sync_paths": list(cfg.excluded_sync_paths),
        "sync_symlinks": cfg.sync_symlinks,
        "daemon_max_actions": cfg.daemon_max_actions,
    }
    path.write_text(json.dumps(data, indent=2) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def write_config(config: Config, path: Path | None = None) -> Path:
    path = path or default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    config.sync_root.mkdir(parents=True, exist_ok=True)
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "sync_root": str(config.sync_root),
        "client_secret_path": str(config.client_secret_path),
        "token_path": str(config.token_path),
        "db_path": str(config.db_path),
        "poll_interval": config.poll_interval,
        "allow_writes": config.allow_writes,
        "allow_deletes": config.allow_deletes,
        "dry_run": config.dry_run,
        "include_shared_drives": config.include_shared_drives,
        "hash_limit_bytes": config.hash_limit_bytes,
        "launch_daemon_at_login": config.launch_daemon_at_login,
        "launch_tray_at_login": config.launch_tray_at_login,
        "desktop_notifications": config.desktop_notifications,
        "conflict_policy": config.conflict_policy,
        "google_docs_mode": config.google_docs_mode,
        "proxy_mode": config.proxy_mode,
        "proxy_host": config.proxy_host,
        "proxy_port": config.proxy_port,
        "upload_limit_kbps": config.upload_limit_kbps,
        "download_limit_kbps": config.download_limit_kbps,
        "ignore_patterns": list(config.ignore_patterns),
        "selective_sync_paths": list(config.selective_sync_paths),
        "excluded_sync_paths": list(config.excluded_sync_paths),
        "sync_symlinks": config.sync_symlinks,
        "daemon_max_actions": config.daemon_max_actions,
    }
    path.write_text(json.dumps(data, indent=2) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path
