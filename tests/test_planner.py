from catsyncd.models import LocalEntry, RemoteEntry, SyncRecord
from catsyncd.planner import build_plan, summarize


def remote(path: str, *, remote_id: str | None = None, md5: str | None = None, size: int | None = None, kind: str = "file", modified: str = "r1") -> RemoteEntry:
    mime = "application/octet-stream"
    if kind == "folder":
        mime = "application/vnd.google-apps.folder"
    elif kind == "symlink":
        mime = "application/vnd.google-apps.shortcut"
    return RemoteEntry(
        id=remote_id or f"id-{path}",
        path=path,
        name=path.rsplit("/", 1)[-1],
        kind=kind,
        mime_type=mime,
        parents=("root",),
        trashed=False,
        modified_time=modified,
        md5=md5,
        size=size,
        web_view_link=None,
    )


def google_doc(path: str, *, remote_id: str = "rid", modified: str = "r1") -> RemoteEntry:
    item = remote(path, remote_id=remote_id, kind="file", modified=modified)
    return RemoteEntry(
        id=item.id,
        path=item.path,
        name=item.name,
        kind=item.kind,
        mime_type="application/vnd.google-apps.document",
        parents=item.parents,
        trashed=item.trashed,
        modified_time=item.modified_time,
        md5=None,
        size=None,
        web_view_link=item.web_view_link,
    )


def local(path: str, *, md5: str | None = None, size: int | None = None, kind: str = "file", mtime: int = 1) -> LocalEntry:
    return LocalEntry(path=path, kind=kind, size=size, mtime_ns=mtime, md5=md5, sha256=None)


def record(path: str, *, remote_id: str = "rid", md5: str | None = "a", size: int | None = 1, kind: str = "file") -> SyncRecord:
    return SyncRecord(
        local_path=path,
        remote_id=remote_id,
        remote_path=path,
        kind=kind,
        local_md5=md5,
        remote_md5=md5,
        local_size=size,
        remote_size=size,
        local_mtime_ns=1,
        remote_modified_time="r1",
    )


def test_remote_only_is_download_create():
    actions = build_plan([], [remote("a.txt")])
    assert summarize(actions) == {"download_create": 1}


def test_local_only_is_upload_create():
    actions = build_plan([local("a.txt")], [])
    assert summarize(actions) == {"upload_create": 1}


def test_local_change_uploads_when_remote_unchanged():
    actions = build_plan(
        [local("a.txt", md5="b", size=1)],
        [remote("a.txt", remote_id="rid", md5="a", size=1)],
        [record("a.txt", remote_id="rid", md5="a", size=1)],
    )
    assert summarize(actions) == {"upload_update": 1}


def test_remote_change_downloads_when_local_unchanged():
    actions = build_plan(
        [local("a.txt", md5="a", size=1)],
        [remote("a.txt", remote_id="rid", md5="b", size=1)],
        [record("a.txt", remote_id="rid", md5="a", size=1)],
    )
    assert summarize(actions) == {"download_update": 1}


def test_both_changed_conflict_copy():
    actions = build_plan(
        [local("a.txt", md5="local", size=1)],
        [remote("a.txt", remote_id="rid", md5="remote", size=1)],
        [record("a.txt", remote_id="rid", md5="base", size=1)],
    )
    assert summarize(actions) == {"conflict_copy_local": 1}


def test_both_changed_prefer_local_uploads():
    actions = build_plan(
        [local("a.txt", md5="local", size=1)],
        [remote("a.txt", remote_id="rid", md5="remote", size=1)],
        [record("a.txt", remote_id="rid", md5="base", size=1)],
        conflict_policy="prefer_local",
    )
    assert summarize(actions) == {"upload_update": 1}


def test_both_changed_prefer_newer_uploads_when_local_is_newer():
    actions = build_plan(
        [local("a.txt", md5="local", size=1, mtime=2_000_000_000)],
        [remote("a.txt", remote_id="rid", md5="remote", size=1, modified="1970-01-01T00:00:01.000Z")],
        [record("a.txt", remote_id="rid", md5="base", size=1)],
        conflict_policy="prefer_newer",
    )
    assert actions[0].action == "upload_update"
    assert actions[0].reason == "both sides changed; local newer"


def test_both_changed_prefer_newer_downloads_when_remote_is_newer():
    actions = build_plan(
        [local("a.txt", md5="local", size=1, mtime=1_000_000_000)],
        [remote("a.txt", remote_id="rid", md5="remote", size=1, modified="1970-01-01T00:00:03.000Z")],
        [record("a.txt", remote_id="rid", md5="base", size=1)],
        conflict_policy="prefer_newer",
    )
    assert actions[0].action == "download_update"
    assert actions[0].reason == "both sides changed; remote newer"


def test_both_changed_prefer_newer_keeps_conflict_when_ambiguous():
    actions = build_plan(
        [local("a.txt", md5="local", size=1, mtime=1_500_000_000)],
        [remote("a.txt", remote_id="rid", md5="remote", size=1, modified="1970-01-01T00:00:01.000Z")],
        [record("a.txt", remote_id="rid", md5="base", size=1)],
        conflict_policy="prefer_newer",
    )
    assert summarize(actions) == {"conflict_copy_local": 1}


def test_untracked_differing_pair_prefer_newer_downloads_remote():
    actions = build_plan(
        [local("a.txt", md5="local", size=1, mtime=1_000_000_000)],
        [remote("a.txt", remote_id="rid", md5="remote", size=1, modified="1970-01-01T00:00:03.000Z")],
        conflict_policy="prefer_newer",
    )
    assert actions[0].action == "download_update"
    assert actions[0].reason == "new local and remote differ; remote newer"


def test_untracked_differing_pair_prefer_local_uploads():
    actions = build_plan(
        [local("a.txt", md5="local", size=1)],
        [remote("a.txt", remote_id="rid", md5="remote", size=1)],
        conflict_policy="prefer_local",
    )
    assert summarize(actions) == {"upload_update": 1}


def test_local_rename_is_not_inferred_from_snapshot():
    # 0.4.3 kernel: snapshot leftovers are not planned as moves.
    actions = build_plan(
        [local("new.txt", md5="a", size=1)],
        [remote("old.txt", remote_id="rid", md5="a", size=1)],
        [record("old.txt", remote_id="rid", md5="a", size=1)],
    )
    assert summarize(actions) == {"download_create": 1}


def test_remote_move_moves_local():
    actions = build_plan(
        [local("old.txt", md5="a", size=1)],
        [remote("new.txt", remote_id="rid", md5="a", size=1)],
        [record("old.txt", remote_id="rid", md5="a", size=1)],
    )
    assert actions[0].action == "local_move"
    assert actions[0].source_path == "old.txt"
    assert actions[0].target_path == "new.txt"


def test_local_delete_trashes_remote_when_inotify_delete():
    actions = build_plan(
        [],
        [remote("a.txt", remote_id="rid", md5="a", size=1)],
        [record("a.txt", remote_id="rid", md5="a", size=1)],
        recent_local_deletes=("a.txt",),
    )
    assert summarize(actions) == {"remote_trash": 1}


def test_remote_delete_deletes_local_when_id_removed():
    actions = build_plan(
        [local("a.txt", md5="a", size=1)],
        [],
        [record("a.txt", remote_id="rid", md5="a", size=1)],
        removed_remote_ids=("rid",),
    )
    assert summarize(actions) == {"local_delete": 1}


def test_matching_folder_needs_no_action():
    actions = build_plan(
        [local("Folder", kind="folder")],
        [remote("Folder", remote_id="rid", kind="folder")],
        [record("Folder", remote_id="rid", kind="folder", md5=None, size=None)],
    )
    assert summarize(actions) == {}


def test_matching_symlink_shortcut_has_index_update():
    actions = build_plan(
        [local("link.txt", kind="symlink", md5="target.txt", size=10)],
        [remote("link.txt", remote_id="rid", kind="symlink", md5="target-id")],
    )
    assert summarize(actions) == {"index_update": 1}


def test_local_google_doc_placeholder_change_is_conflict():
    actions = build_plan(
        [local("Doc.gdoc", md5="changed", size=2)],
        [google_doc("Doc.gdoc", remote_id="rid")],
        [
            SyncRecord(
                local_path="Doc.gdoc",
                remote_id="rid",
                remote_path="Doc.gdoc",
                kind="file",
                local_md5="base",
                remote_md5=None,
                local_size=1,
                remote_size=None,
                local_mtime_ns=1,
                remote_modified_time="r1",
            )
        ],
    )
    assert summarize(actions) == {"conflict_review": 1}
