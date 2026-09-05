from catsyncd.inventory import build_remote_inventory


def test_remote_paths_are_built_from_parent_tree():
    entries = build_remote_inventory(
        [
            {
                "id": "folder1",
                "name": "Folder",
                "mimeType": "application/vnd.google-apps.folder",
                "parents": ["root"],
                "trashed": False,
            },
            {
                "id": "file1",
                "name": "Doc",
                "mimeType": "application/vnd.google-apps.document",
                "parents": ["folder1"],
                "trashed": False,
            },
        ]
    )
    assert [entry.path for entry in entries] == ["Folder", "Folder/Doc.gdoc"]


def test_remote_shortcut_is_symlink_entry():
    entries = build_remote_inventory(
        [
            {
                "id": "target",
                "name": "target.txt",
                "mimeType": "text/plain",
                "parents": ["root"],
                "trashed": False,
            },
            {
                "id": "shortcut",
                "name": "link.txt",
                "mimeType": "application/vnd.google-apps.shortcut",
                "parents": ["root"],
                "trashed": False,
                "shortcutDetails": {"targetId": "target"},
            },
        ]
    )

    shortcut = [entry for entry in entries if entry.path == "link.txt"][0]
    assert shortcut.kind == "symlink"
    assert shortcut.md5 == "target"


def test_scan_local_symlink_stores_target_text(tmp_path):
    from catsyncd.inventory import scan_local

    (tmp_path / "target.txt").write_text("target")
    (tmp_path / "link.txt").symlink_to("target.txt")

    entries = scan_local(tmp_path, 1024, sync_symlinks=True)

    link = [entry for entry in entries if entry.path == "link.txt"][0]
    assert link.kind == "symlink"
    assert link.md5 == "target.txt"


def test_remote_parentless_items_are_not_treated_as_root_items():
    entries = build_remote_inventory(
        [
            {
                "id": "orphan",
                "name": "Orphan Icons",
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [],
                "trashed": False,
            },
            {
                "id": "real",
                "name": "Projects",
                "mimeType": "application/vnd.google-apps.folder",
                "parents": ["drive-root-id"],
                "trashed": False,
            },
        ],
        root_ids=("root", "drive-root-id"),
    )
    assert [entry.path for entry in entries] == ["Projects"]


def test_ignore_patterns_filter_local_and_remote(tmp_path):
    (tmp_path / "keep.txt").write_text("keep")
    (tmp_path / "skip.tmp").write_text("skip")
    (tmp_path / "Cache").mkdir()
    (tmp_path / "Cache" / "data.txt").write_text("skip")

    from catsyncd.inventory import scan_local

    local_paths = [entry.path for entry in scan_local(tmp_path, 1024, ("*.tmp", "Cache/*"))]
    assert set(local_paths) == {"Cache", "keep.txt"}

    remote_entries = build_remote_inventory(
        [
            {"id": "keep", "name": "keep.txt", "mimeType": "text/plain", "parents": ["root"], "trashed": False},
            {"id": "skip", "name": "skip.tmp", "mimeType": "text/plain", "parents": ["root"], "trashed": False},
        ],
        ("*.tmp",),
    )
    assert [entry.path for entry in remote_entries] == ["keep.txt"]


def test_ignore_folder_name_patterns_apply_anywhere(tmp_path):
    (tmp_path / "Project").mkdir()
    (tmp_path / "Project" / "tmp").mkdir()
    (tmp_path / "Project" / "tmp" / "skip.txt").write_text("skip")
    (tmp_path / "Project" / "node_modules").mkdir()
    (tmp_path / "Project" / "node_modules" / "pkg.js").write_text("skip")
    (tmp_path / "Project" / "Cache").mkdir()
    (tmp_path / "Project" / "Cache" / "cached.txt").write_text("skip")
    (tmp_path / "Project" / ".venv").mkdir()
    (tmp_path / "Project" / ".venv" / "python").write_text("skip")
    (tmp_path / "Project" / "keep.txt").write_text("keep")

    from catsyncd.inventory import scan_local

    local_paths = [entry.path for entry in scan_local(tmp_path, 1024, ("tmp", "Cache", ".venv", "node_modules"))]
    assert local_paths == ["Project", "Project/keep.txt"]


def test_selective_sync_filters_to_selected_subfolders(tmp_path):
    (tmp_path / "Clients").mkdir()
    (tmp_path / "Clients" / "Cytracon").mkdir()
    (tmp_path / "Clients" / "Cytracon" / "a.txt").write_text("a")
    (tmp_path / "Clients" / "Other").mkdir()
    (tmp_path / "Clients" / "Other" / "b.txt").write_text("b")

    from catsyncd.inventory import scan_local

    local_paths = [entry.path for entry in scan_local(tmp_path, 1024, selective_sync_paths=("Clients/Cytracon",))]
    assert local_paths == ["Clients", "Clients/Cytracon", "Clients/Cytracon/a.txt"]

    remote_entries = build_remote_inventory(
        [
            {"id": "clients", "name": "Clients", "mimeType": "application/vnd.google-apps.folder", "parents": ["root"], "trashed": False},
            {"id": "cytracon", "name": "Cytracon", "mimeType": "application/vnd.google-apps.folder", "parents": ["clients"], "trashed": False},
            {"id": "a", "name": "a.txt", "mimeType": "text/plain", "parents": ["cytracon"], "trashed": False},
            {"id": "other", "name": "Other", "mimeType": "application/vnd.google-apps.folder", "parents": ["clients"], "trashed": False},
            {"id": "b", "name": "b.txt", "mimeType": "text/plain", "parents": ["other"], "trashed": False},
        ],
        selective_sync_paths=("Clients/Cytracon",),
    )
    assert [entry.path for entry in remote_entries] == ["Clients", "Clients/Cytracon", "Clients/Cytracon/a.txt"]


def test_excluded_sync_paths_unselect_subfolders(tmp_path):
    (tmp_path / "Clients").mkdir()
    (tmp_path / "Clients" / "Cytracon").mkdir()
    (tmp_path / "Clients" / "Cytracon" / "a.txt").write_text("a")
    (tmp_path / "Clients" / "Archive").mkdir()
    (tmp_path / "Clients" / "Archive" / "old.txt").write_text("old")

    from catsyncd.inventory import scan_local

    local_paths = [entry.path for entry in scan_local(tmp_path, 1024, excluded_sync_paths=("Clients/Archive",))]
    assert local_paths == ["Clients", "Clients/Cytracon", "Clients/Cytracon/a.txt"]

    remote_entries = build_remote_inventory(
        [
            {"id": "clients", "name": "Clients", "mimeType": "application/vnd.google-apps.folder", "parents": ["root"], "trashed": False},
            {"id": "cytracon", "name": "Cytracon", "mimeType": "application/vnd.google-apps.folder", "parents": ["clients"], "trashed": False},
            {"id": "a", "name": "a.txt", "mimeType": "text/plain", "parents": ["cytracon"], "trashed": False},
            {"id": "archive", "name": "Archive", "mimeType": "application/vnd.google-apps.folder", "parents": ["clients"], "trashed": False},
            {"id": "old", "name": "old.txt", "mimeType": "text/plain", "parents": ["archive"], "trashed": False},
        ],
        excluded_sync_paths=("Clients/Archive",),
    )
    assert [entry.path for entry in remote_entries] == ["Clients", "Clients/Cytracon", "Clients/Cytracon/a.txt"]


def test_sync_path_allowed_applies_excludes_and_conflict_patterns():
    from catsyncd.inventory import sync_path_allowed

    assert sync_path_allowed(
        "Projects/notes/file.md",
        ignore_patterns=("*.catsync-conflict-*", "*.solidsync-conflict*"),
        excluded_sync_paths=("Projects/work", "Projects/backups"),
    )
    assert not sync_path_allowed(
        "Projects/work/build/file.txt",
        ignore_patterns=("*.catsync-conflict-*", "*.solidsync-conflict*"),
        excluded_sync_paths=("Projects/work", "Projects/backups"),
    )
    assert not sync_path_allowed(
        "Projects/.codex/history.catsync-conflict-remote-20260808.jsonl",
        ignore_patterns=("*.catsync-conflict-*", "*.solidsync-conflict*"),
        excluded_sync_paths=("Projects/work", "Projects/backups"),
    )
    assert not sync_path_allowed(
        "Projects/Plugins/app/icon.png.solidsync-conflict1",
        ignore_patterns=("*.catsync-conflict-*", "*.solidsync-conflict*"),
        excluded_sync_paths=("Projects/work", "Projects/backups"),
    )


def test_scan_local_reuses_previous_hashes_when_file_is_unchanged(tmp_path):
    from catsyncd.inventory import scan_local
    from catsyncd.models import LocalEntry

    target = tmp_path / "unchanged.txt"
    target.write_text("same")
    stat = target.stat()
    previous = [
        LocalEntry(
            path="unchanged.txt",
            kind="file",
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            md5="cached-md5",
            sha256="cached-sha",
        )
    ]

    entries = scan_local(tmp_path, 1024, previous_entries=previous)

    assert len(entries) == 1
    assert entries[0].md5 == "cached-md5"
    assert entries[0].sha256 == "cached-sha"
