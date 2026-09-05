from catsyncd.models import LocalEntry, PlanAction, RemoteEntry
from catsyncd.state import State


def test_conflict_actions_roundtrip_plan_metadata(tmp_path):
    state = State(tmp_path / "state.db")
    local = LocalEntry("a.txt", "file", 5, 1, "local", None)
    remote = RemoteEntry(
        id="rid",
        path="a.txt",
        name="a.txt",
        kind="file",
        mime_type="application/octet-stream",
        parents=("root",),
        trashed=False,
        modified_time="r1",
        md5="remote",
        size=6,
        web_view_link=None,
    )
    state.replace_plan(
        [
            PlanAction("index_update", "same.txt", "unchanged"),
            PlanAction("conflict_copy_local", "a.txt", "both changed", local=local, remote=remote, remote_id="rid"),
        ]
    )

    conflicts = state.conflict_actions()
    selected = state.plan_action("a.txt")
    state.close()

    assert len(conflicts) == 1
    assert conflicts[0].path == "a.txt"
    assert conflicts[0].local == local
    assert conflicts[0].remote == remote
    assert selected == conflicts[0]
