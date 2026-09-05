from pathlib import Path

from catsyncd.executor import execute_plan
from catsyncd.cli import _newer_resolution_strategy, _resolution_actions
from catsyncd.models import LocalEntry, PlanAction, RemoteEntry


class FakeDrive:
    def __init__(self):
        self.downloads = {"rid": b"remote"}
        self.uploaded = []
        self.updated = []
        self.moved = []
        self.trashed = []
        self.shortcuts = []
        self.created_folders = []

    def download_bytes(self, file_id):
        return self.downloads[file_id]

    def upload_file(self, path, name, parent_id="root"):
        self.uploaded.append((Path(path).read_bytes(), name, parent_id))
        return {"id": "uploaded", "name": name, "parents": [parent_id]}

    def update_file_content(self, file_id, path):
        self.updated.append((file_id, Path(path).read_bytes()))
        return {"id": file_id}

    def create_folder(self, name, parent_id="root"):
        folder_id = f"folder-{name}"
        self.created_folders.append((name, parent_id))
        return {"id": folder_id, "name": name, "parents": [parent_id]}

    def create_shortcut(self, name, target_id, parent_id="root"):
        self.shortcuts.append((name, target_id, parent_id))
        return {"id": "shortcut", "name": name, "parents": [parent_id]}

    def move_file(self, file_id, name, add_parent, remove_parents):
        self.moved.append((file_id, name, add_parent, remove_parents))
        return {"id": file_id}

    def trash_file(self, file_id):
        self.trashed.append(file_id)
        return {"id": file_id, "trashed": True}


def remote(path="a.txt", *, remote_id="rid", kind="file"):
    return RemoteEntry(
        id=remote_id,
        path=path,
        name=path.rsplit("/", 1)[-1],
        kind=kind,
        mime_type="application/octet-stream" if kind == "file" else "application/vnd.google-apps.folder",
        parents=("root",),
        trashed=False,
        modified_time=None,
        md5=None,
        size=None,
        web_view_link=None,
    )


def local(path="a.txt", *, kind="file"):
    return LocalEntry(path=path, kind=kind, size=5, mtime_ns=1, md5=None, sha256=None)


def test_download_create_writes_file(tmp_path):
    drive = FakeDrive()
    action = PlanAction("download_create", "a.txt", "new remote", remote=remote(), remote_id="rid")
    result = execute_plan(tmp_path, drive, [action], [], [remote()])
    assert (tmp_path / "a.txt").read_bytes() == b"remote"
    assert result.errors == []


def test_upload_create_calls_drive(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"local")
    drive = FakeDrive()
    entry = local("a.txt")
    action = PlanAction("upload_create", "a.txt", "new local", local=entry)
    result = execute_plan(tmp_path, drive, [action], [entry], [])
    assert drive.uploaded == [(b"local", "a.txt", "root")]
    assert result.errors == []


def test_upload_create_symlink_creates_drive_shortcut(tmp_path):
    (tmp_path / "target.txt").write_text("target")
    (tmp_path / "link.txt").symlink_to("target.txt")
    drive = FakeDrive()
    entry = local("link.txt", kind="symlink")
    action = PlanAction("upload_create", "link.txt", "new local", local=entry)
    target_remote = remote("target.txt", remote_id="target-id")

    result = execute_plan(tmp_path, drive, [action], [entry], [target_remote])

    assert drive.shortcuts == [("link.txt", "target-id", "root")]
    assert result.errors == []


def test_upload_create_symlink_defers_when_target_is_not_remote(tmp_path):
    (tmp_path / "target.txt").write_text("target")
    (tmp_path / "link.txt").symlink_to("target.txt")
    drive = FakeDrive()
    entry = local("link.txt", kind="symlink")
    action = PlanAction("upload_create", "link.txt", "new local", local=entry)

    result = execute_plan(tmp_path, drive, [action], [entry], [])

    assert drive.shortcuts == []
    assert result.errors == []
    assert result.skipped == ["upload_create_deferred:link.txt"]


def test_local_delete_moves_to_trash_when_allowed(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"local")
    drive = FakeDrive()
    action = PlanAction("local_delete", "a.txt", "remote deleted", local=local("a.txt"), remote_id="rid")
    result = execute_plan(tmp_path, drive, [action], [local("a.txt")], [], allow_deletes=True)
    assert not (tmp_path / "a.txt").exists()
    assert list((tmp_path / ".catsync" / "trash").rglob("a.txt"))
    assert result.errors == []


def test_remote_trash_when_allowed(tmp_path):
    drive = FakeDrive()
    action = PlanAction("remote_trash", "a.txt", "local deleted", remote=remote(), remote_id="rid")
    result = execute_plan(tmp_path, drive, [action], [], [remote()], allow_deletes=True)
    assert drive.trashed == ["rid"]
    assert result.errors == []


def test_keep_both_resolution_copies_remote_then_uploads_local():
    conflict = PlanAction(
        "conflict_copy_local",
        "a.txt",
        "both changed",
        local=local("a.txt"),
        remote=remote("a.txt", remote_id="rid"),
        remote_id="rid",
    )

    actions = _resolution_actions(conflict, "keep-both")

    assert [action.action for action in actions] == ["conflict_copy_local", "upload_update"]
    assert actions[1].remote_id == "rid"


def test_prefer_newer_chooses_local_when_local_mtime_is_newer():
    newer_local = LocalEntry("a.txt", "file", 5, 2_000_000_000, "local", None)
    older_remote = RemoteEntry(
        id="rid",
        path="a.txt",
        name="a.txt",
        kind="file",
        mime_type="application/octet-stream",
        parents=("root",),
        trashed=False,
        modified_time="1970-01-01T00:00:01.000Z",
        md5="remote",
        size=6,
        web_view_link=None,
    )
    conflict = PlanAction("conflict_copy_local", "a.txt", "both changed", local=newer_local, remote=older_remote, remote_id="rid")

    assert _newer_resolution_strategy(conflict) == "prefer-local"
