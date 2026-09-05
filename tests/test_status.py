from catsyncd.cli import _compact_status_payload
from catsyncd.config import Config


def test_compact_status_has_no_paths_or_secrets(tmp_path):
    cfg = Config(
        sync_root=tmp_path / "Drive",
        client_secret_path=tmp_path / "client_secret.json",
        token_path=tmp_path / "token.json",
        db_path=tmp_path / "state.db",
        dry_run=True,
        allow_writes=False,
        allow_deletes=False,
    )
    payload = _compact_status_payload(cfg)
    blob = str(payload)
    assert "client_secret" not in blob
    assert "token.json" not in blob
    assert str(tmp_path) not in blob
    assert "Drive" not in blob
    assert payload["version"]
    assert payload["configured"] is False
    assert payload["daemon_active"] is False
    assert "pending" in payload
    assert "conflicts" in payload
    assert "phase" in payload
