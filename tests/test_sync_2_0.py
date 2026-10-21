"""Catsync 2.0 core fix tests (offline, no Drive)."""

from __future__ import annotations

import tempfile
from pathlib import Path

from catsyncd.executor import _conflict_path
from catsyncd.kernel import KernelEvent, plan_local_events
from catsyncd.models import LocalEntry, PlanAction, RemoteEntry, SyncRecord
from catsyncd.planner import _local_changed, _plan_record, records_from_executed
from catsyncd.state import State


def _remote(md5: str, size: int = 20, mid: str = "id1") -> RemoteEntry:
    return RemoteEntry(
        mid, "a.txt", "a.txt", "file", "text/plain", (), False,
        "2026-01-01T00:00:00Z", md5, size, None,
    )


def test_no_ping_pong_after_download() -> None:
    """Nach einem Download muss der Record den neuen Plattenzustand tragen."""
    old_local = LocalEntry("a.txt", "file", 10, 1000, "m1", "s1")
    remote = _remote("m2", 20)
    rec = SyncRecord("a.txt", "id1", "a.txt", "file", "m1", "m2", 10, 20, 1000, "2026-01-01T00:00:00Z")
    actions = [PlanAction("download_update", "a.txt", "remote changed", old_local, remote, remote_id="id1")]
    new_local = LocalEntry("a.txt", "file", 20, 2000, "m2", "s2")
    records = records_from_executed([rec], actions, ["download_update:a.txt"], local_after={"a.txt": new_local})
    rec_after = {r.remote_id: r for r in records}["id1"]
    assert rec_after.local_md5 == "m2"
    assert not _local_changed(rec_after, new_local)


def test_concurrent_edit_keeps_both() -> None:
    """Beide Seiten geaendert + Inhalt verschieden -> Konfliktkopie + Upload."""
    local_new = LocalEntry("a.txt", "file", 30, 3000, "m3", "s3")
    remote_changed = _remote("m9", 90)
    rec = SyncRecord("a.txt", "id1", "a.txt", "file", "m1", "m2", 10, 20, 1000, "2026-01-01T00:00:00Z")
    for policy in ("prefer_newer", "keep_both"):
        acts = _plan_record(rec, local_new, remote_changed, policy)
        names = [a.action for a in acts]
        assert "conflict_copy_local" in names
        assert "upload_update" in names
        assert "download_update" not in names


def test_inotify_plus_remote_change_conflicts() -> None:
    """inotify-Upload darf bei gleichzeitiger Drive-Aenderung nicht blind ueberschreiben."""
    local_new = LocalEntry("a.txt", "file", 30, 3000, "m3", "s3")
    remote_changed = _remote("m9", 90)
    rec = SyncRecord("a.txt", "id1", "a.txt", "file", "m1", "m2", 10, 20, 1000, "2026-01-01T00:00:00Z")
    acts = plan_local_events(
        [KernelEvent("modified", "a.txt")],
        {"a.txt": local_new},
        [remote_changed],
        [rec],
        "keep_both",
    )
    assert acts and acts[0].action == "conflict_copy_local"


def test_plan_actions_persist_remote_id() -> None:
    """remote_id muss die plan_actions-Persistenz ueberleben."""
    db = Path(tempfile.mkdtemp()) / "state.db"
    state = State(db)
    state.replace_plan(
        [
            PlanAction(
                "local_delete",
                "x/y.pdf",
                "changes: id trashed",
                local=LocalEntry("x/y.pdf", "file", 1, 1, "m", "s"),
                remote=None,
                remote_id="DRIVE123",
            )
        ]
    )
    state.close()
    state = State(db)
    try:
        acts = state.list_plan_actions(limit=10)
        assert acts and acts[0].remote_id == "DRIVE123"
    finally:
        state.close()


def test_conflict_names_are_deterministic() -> None:
    """Gleicher Inhalt -> gleicher Konfliktname auf allen Rechnern."""
    p1 = _conflict_path(Path("/tmp/x/bericht.pdf"), "remote", content_hash="abcdef1234567890")
    p2 = _conflict_path(Path("/tmp/x/bericht.pdf"), "remote", content_hash="abcdef1234567890")
    assert p1 == p2
    assert "abcdef1234" in p1.name
