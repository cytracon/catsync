from pathlib import Path

from catsyncd.config import Config, DEFAULT_IGNORE_PATTERNS, default_config, load_config, write_config


def test_default_config_has_common_ignore_patterns():
    cfg = default_config()
    assert "tmp" in cfg.ignore_patterns
    assert "*.tmp" in cfg.ignore_patterns
    assert ".env" in cfg.ignore_patterns
    assert "*.pyc" in cfg.ignore_patterns
    assert ".venv" in cfg.ignore_patterns
    assert "node_modules" in cfg.ignore_patterns
    assert cfg.ignore_patterns == DEFAULT_IGNORE_PATTERNS


def test_write_and_load_extended_config(tmp_path):
    path = tmp_path / "config.json"
    cfg = Config(
        sync_root=tmp_path / "Catsync",
        client_secret_path=tmp_path / "client_secret.json",
        token_path=tmp_path / "token.json",
        db_path=tmp_path / "state.db",
        poll_interval=120,
        allow_writes=True,
        allow_deletes=True,
        dry_run=False,
        include_shared_drives=True,
        hash_limit_bytes=32 * 1024 * 1024,
        launch_daemon_at_login=False,
        launch_tray_at_login=True,
        desktop_notifications=False,
        conflict_policy="review",
        google_docs_mode="skip",
        proxy_mode="manual",
        proxy_host="proxy.local",
        proxy_port=8080,
        upload_limit_kbps=1000,
        download_limit_kbps=2000,
        ignore_patterns=("*.tmp", "Cache/*"),
        selective_sync_paths=("Clients/Cytracon", "Backups"),
        excluded_sync_paths=("Clients/Cytracon/Archive",),
        sync_symlinks=True,
        daemon_max_actions=42,
    )

    write_config(cfg, path)
    loaded = load_config(path)

    assert loaded.sync_root == Path(tmp_path / "Catsync").resolve()
    assert loaded.allow_writes is True
    assert loaded.allow_deletes is True
    assert loaded.dry_run is False
    assert loaded.conflict_policy == "review"
    assert loaded.google_docs_mode == "skip"
    assert loaded.proxy_mode == "manual"
    assert loaded.proxy_host == "proxy.local"
    assert loaded.proxy_port == 8080
    assert loaded.ignore_patterns == ("*.tmp", "Cache/*")
    assert loaded.selective_sync_paths == ("Clients/Cytracon", "Backups")
    assert loaded.excluded_sync_paths == ("Clients/Cytracon/Archive",)
    assert loaded.sync_symlinks is True
    assert loaded.daemon_max_actions == 42
