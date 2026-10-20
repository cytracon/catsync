from __future__ import annotations

import hashlib
import json
import os
import re
from fnmatch import fnmatch, translate
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from .models import LocalEntry, RemoteEntry


FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
GOOGLE_DOC_EXTENSIONS = {
    "application/vnd.google-apps.document": ".gdoc",
    "application/vnd.google-apps.spreadsheet": ".gsheet",
    "application/vnd.google-apps.presentation": ".gslides",
    "application/vnd.google-apps.form": ".gform",
    "application/vnd.google-apps.drawing": ".gdraw",
    "application/vnd.google-apps.script": ".gscript",
    "application/vnd.google-apps.site": ".gsite",
}


def scan_local(
    root: Path,
    hash_limit_bytes: int,
    ignore_patterns: Iterable[str] = (),
    selective_sync_paths: Iterable[str] = (),
    excluded_sync_paths: Iterable[str] = (),
    sync_symlinks: bool = False,
    previous_entries: Iterable[LocalEntry] = (),
) -> list[LocalEntry]:
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    entries: list[LocalEntry] = []
    patterns = tuple(ignore_patterns)
    selected = _normalize_selected(selective_sync_paths)
    excluded = _normalize_selected(excluded_sync_paths)
    previous = {entry.path: entry for entry in previous_entries}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        if rel_dir == ".":
            rel_dir = ""
        keep: list[str] = []
        for name in dirnames:
            child = f"{rel_dir}/{name}".strip("/") if rel_dir else name
            if _ignored(child, patterns) or not _selected(child, selected) or _excluded(child, excluded):
                continue
            keep.append(name)
        dirnames[:] = keep
        if rel_dir:
            folder = local_entry_from_path(root, rel_dir, hash_limit_bytes, previous, sync_symlinks)
            if folder is not None:
                entries.append(folder)
        for name in filenames:
            rel = f"{rel_dir}/{name}".strip("/") if rel_dir else name
            if _ignored(rel, patterns) or not _selected(rel, selected) or _excluded(rel, excluded):
                continue
            entry = local_entry_from_path(root, rel, hash_limit_bytes, previous, sync_symlinks)
            if entry is not None:
                entries.append(entry)
    return entries


def local_entry_from_path(
    root: Path,
    rel: str,
    hash_limit_bytes: int,
    previous_entries: dict[str, LocalEntry] | Iterable[LocalEntry] = (),
    sync_symlinks: bool = False,
) -> LocalEntry | None:
    root = root.expanduser().resolve()
    path = root / rel
    previous = previous_entries if isinstance(previous_entries, dict) else {entry.path: entry for entry in previous_entries}
    try:
        stat = path.lstat()
    except FileNotFoundError:
        return None
    if path.is_symlink() and sync_symlinks:
        target = _symlink_target_text(path)
        return LocalEntry(rel, "symlink", len(target), stat.st_mtime_ns, target, None)
    if path.is_symlink():
        return None
    if path.is_dir():
        return LocalEntry(rel, "folder", None, stat.st_mtime_ns, None, None)
    if not path.is_file():
        return None
    prev = previous.get(rel)
    if (
        prev
        and prev.kind == "file"
        and prev.size == stat.st_size
        and prev.mtime_ns == stat.st_mtime_ns
        and prev.md5
        and prev.sha256
    ):
        md5 = prev.md5
        sha = prev.sha256
    else:
        md5 = _digest(path, "md5") if stat.st_size <= hash_limit_bytes else None
        sha = _digest(path, "sha256") if stat.st_size <= hash_limit_bytes else None
    return LocalEntry(rel, "file", stat.st_size, stat.st_mtime_ns, md5, sha)


def drop_local_tree(local_by_path: dict[str, LocalEntry], rel: str) -> None:
    rel = rel.strip("/")
    for path in [key for key in local_by_path if key == rel or key.startswith(f"{rel}/")]:
        local_by_path.pop(path, None)


def apply_local_fs_event(
    root: Path,
    event_kind: str,
    rel: str,
    local_by_path: dict[str, LocalEntry],
    hash_limit_bytes: int,
    sync_symlinks: bool = False,
    src_path: str | None = None,
) -> None:
    rel = (rel or "").strip("/")
    if event_kind == "deleted" and rel:
        drop_local_tree(local_by_path, rel)
        return
    if event_kind == "moved" and src_path:
        drop_local_tree(local_by_path, src_path.strip("/"))
    if event_kind in {"created", "modified", "moved"} and rel:
        entry = local_entry_from_path(root, rel, hash_limit_bytes, local_by_path, sync_symlinks)
        if entry is None:
            drop_local_tree(local_by_path, rel)
            return
        local_by_path[rel] = entry
        if entry.kind == "folder":
            for child in scan_local(root / rel, hash_limit_bytes, (), (), (), sync_symlinks, ()):
                local_by_path[f"{rel}/{child.path}"] = LocalEntry(
                    f"{rel}/{child.path}",
                    child.kind,
                    child.size,
                    child.mtime_ns,
                    child.md5,
                    child.sha256,
                )


def remote_from_api_item(item: dict, path: str) -> RemoteEntry:
    mime = item.get("mimeType") or "application/octet-stream"
    if mime == FOLDER_MIME:
        kind = "folder"
    elif mime == SHORTCUT_MIME:
        kind = "symlink"
    else:
        kind = "file"
    size = int(item["size"]) if str(item.get("size", "")).isdigit() else None
    md5 = item.get("md5Checksum")
    if kind == "symlink":
        md5 = (item.get("shortcutDetails") or {}).get("targetId")
    return RemoteEntry(
        id=str(item["id"]),
        path=path,
        name=item.get("name") or path.rsplit("/", 1)[-1],
        kind=kind,
        mime_type=mime,
        parents=tuple(item.get("parents") or ()),
        trashed=bool(item.get("trashed")),
        modified_time=item.get("modifiedTime"),
        md5=md5,
        size=size,
        web_view_link=item.get("webViewLink"),
    )


def upsert_remote_entry(existing: Iterable[RemoteEntry], entry: RemoteEntry) -> list[RemoteEntry]:
    by_id = {item.id: item for item in existing if not item.trashed or item.id == entry.id}
    by_id[entry.id] = entry
    return sorted((item for item in by_id.values() if not item.trashed), key=lambda item: item.path)


def drop_remote_ids(existing: Iterable[RemoteEntry], remote_ids: Iterable[str]) -> list[RemoteEntry]:
    gone = {item for item in remote_ids if item}
    return [item for item in existing if item.id not in gone]


def remote_entry_to_item(entry: RemoteEntry) -> dict:
    item: dict = {
        "id": entry.id,
        "name": entry.name,
        "mimeType": entry.mime_type,
        "parents": list(entry.parents),
        "trashed": entry.trashed,
        "modifiedTime": entry.modified_time,
        "size": None if entry.size is None else str(entry.size),
        "webViewLink": entry.web_view_link,
    }
    if entry.kind == "symlink":
        item["shortcutDetails"] = {"targetId": entry.md5}
    else:
        item["md5Checksum"] = entry.md5
    return item


def child_item_to_entry(folder: RemoteEntry, item: dict) -> RemoteEntry:
    mime = item.get("mimeType") or "application/octet-stream"
    if mime == FOLDER_MIME:
        kind = "folder"
    elif mime == SHORTCUT_MIME:
        kind = "symlink"
    else:
        kind = "file"
    name = _remote_name(item)
    path = f"{folder.path}/{name}".strip("/")
    size = int(item["size"]) if str(item.get("size", "")).isdigit() else None
    md5 = item.get("md5Checksum")
    if kind == "symlink":
        md5 = (item.get("shortcutDetails") or {}).get("targetId")
    return RemoteEntry(
        id=item["id"],
        path=path,
        name=item.get("name") or item["id"],
        kind=kind,
        mime_type=mime,
        parents=tuple(item.get("parents") or (folder.id,)),
        trashed=bool(item.get("trashed")),
        modified_time=item.get("modifiedTime"),
        md5=md5,
        size=size,
        web_view_link=item.get("webViewLink"),
    )


def replace_remote_folder_children(
    existing: Iterable[RemoteEntry],
    folder: RemoteEntry,
    child_items: Iterable[dict],
) -> list[RemoteEntry]:
    """Replace *direct* children of ``folder``. Nested descendants of surviving
    child folders are kept so a parent listing cannot wipe Kruger-style trees.
    """
    prefix = f"{folder.path.rstrip('/')}/"
    children = [child_item_to_entry(folder, item) for item in child_items if item.get("id")]
    surviving = {entry.path[len(prefix):] for entry in children if entry.path.startswith(prefix)}
    kept: list[RemoteEntry] = []
    for entry in existing:
        if entry.id == folder.id:
            kept.append(entry)
            continue
        if not entry.path.startswith(prefix):
            kept.append(entry)
            continue
        rest = entry.path[len(prefix):]
        direct = rest.split("/", 1)[0]
        if direct not in surviving:
            continue
        if "/" not in rest:
            continue
        kept.append(entry)
    by_id = {entry.id: entry for entry in kept}
    for child in children:
        by_id[child.id] = child
    return sorted(by_id.values(), key=lambda item: item.path)


def apply_remote_changes(
    existing: Iterable[RemoteEntry],
    changes: Iterable[dict],
    ignore_patterns: Iterable[str] = (),
    selective_sync_paths: Iterable[str] = (),
    excluded_sync_paths: Iterable[str] = (),
    root_ids: Iterable[str] = ("root",),
) -> tuple[list[RemoteEntry], str | None, set[str]]:
    raw = {entry.id: remote_entry_to_item(entry) for entry in existing if not entry.trashed}
    new_token: str | None = None
    removed_ids: set[str] = set()
    for change in changes:
        if change.get("newStartPageToken"):
            new_token = str(change["newStartPageToken"])
            continue
        file_id = change.get("fileId") or (change.get("file") or {}).get("id")
        item = change.get("file") or {}
        removed = bool(change.get("removed")) or bool(item.get("trashed"))
        if removed and file_id:
            file_id = str(file_id)
            raw.pop(file_id, None)
            removed_ids.add(file_id)
            continue
        if item.get("id"):
            raw[item["id"]] = item
    rebuilt = build_remote_inventory(
        raw.values(),
        ignore_patterns,
        selective_sync_paths,
        excluded_sync_paths,
        root_ids=root_ids,
    )
    return rebuilt, new_token, removed_ids


def build_remote_inventory(
    files: Iterable[dict],
    ignore_patterns: Iterable[str] = (),
    selective_sync_paths: Iterable[str] = (),
    excluded_sync_paths: Iterable[str] = (),
    root_ids: Iterable[str] = ("root",),
) -> list[RemoteEntry]:
    by_id = {item["id"]: item for item in files if item.get("id")}
    roots = tuple(root_ids) or ("root",)
    path_cache: dict[str, str | None] = {root_id: "" for root_id in roots}
    patterns = tuple(ignore_patterns)
    selected = _normalize_selected(selective_sync_paths)
    excluded = _normalize_selected(excluded_sync_paths)

    def remote_path(file_id: str, seen: frozenset[str] = frozenset()) -> str | None:
        if file_id in path_cache:
            return path_cache[file_id]
        item = by_id.get(file_id)
        if not item:
            return None
        if file_id in seen:
            return _safe_name(item.get("name") or file_id)
        parents = tuple(item.get("parents") or ())
        if not parents:
            path_cache[file_id] = None
            return None
        parent_path = remote_path(parents[0], seen | {file_id})
        if parent_path is None:
            path_cache[file_id] = None
            return None
        name = _remote_name(item)
        path = f"{parent_path}/{name}".strip("/")
        path_cache[file_id] = path
        return path

    entries: list[RemoteEntry] = []
    for item in by_id.values():
        path = remote_path(item["id"])
        if path is None:
            continue
        if _ignored(path, patterns) or not _selected(path, selected) or _excluded(path, excluded):
            continue
        mime = item.get("mimeType") or "application/octet-stream"
        if mime == FOLDER_MIME:
            kind = "folder"
        elif mime == SHORTCUT_MIME:
            kind = "symlink"
        else:
            kind = "file"
        size = int(item["size"]) if str(item.get("size", "")).isdigit() else None
        md5 = item.get("md5Checksum")
        if kind == "symlink":
            md5 = (item.get("shortcutDetails") or {}).get("targetId")
        entries.append(
            RemoteEntry(
                id=item["id"],
                path=path,
                name=item.get("name") or item["id"],
                kind=kind,
                mime_type=mime,
                parents=tuple(item.get("parents") or ()),
                trashed=bool(item.get("trashed")),
                modified_time=item.get("modifiedTime"),
                md5=md5,
                size=size,
                web_view_link=item.get("webViewLink"),
            )
        )
    return sorted(entries, key=lambda item: item.path)


def google_doc_placeholder(entry: RemoteEntry) -> str:
    payload = {
        "id": entry.id,
        "name": entry.name,
        "mimeType": entry.mime_type,
        "url": entry.web_view_link,
    }
    return json.dumps(payload, indent=2) + "\n"


def _remote_name(item: dict) -> str:
    name = _safe_name(item.get("name") or item.get("id") or "unnamed")
    mime = item.get("mimeType") or ""
    ext = GOOGLE_DOC_EXTENSIONS.get(mime)
    if ext and not name.endswith(ext):
        return f"{name}{ext}"
    return name


def _safe_name(name: str) -> str:
    return name.replace("/", "_").replace("\x00", "")


def _symlink_target_text(path: Path) -> str:
    try:
        return path.readlink().as_posix()
    except OSError:
        return ""


def _ignored(rel: str, patterns: Iterable[str] = ()) -> bool:
    parts = rel.split("/")
    if ".catsync" in parts or ".git" in parts:
        return True
    components, suffixes = _compiled_ignore_patterns(tuple(patterns))
    if components is not None:
        if components.match(os.path.normcase(rel)) or any(
            components.match(os.path.normcase(part)) for part in parts
        ):
            return True
    return suffixes is not None and any(
        suffixes.match(os.path.normcase("/".join(parts[index:])))
        for index in range(len(parts))
    )


@lru_cache(maxsize=32)
def _compiled_ignore_patterns(patterns: tuple[str, ...]):
    """Compile once per configuration, preserving fnmatch and suffix semantics."""
    components, suffixes = [], []
    for pattern in patterns:
        pattern = pattern.strip().strip("/")
        if pattern:
            target = suffixes if "/" in pattern else components
            target.append(translate(os.path.normcase(pattern)))
    return tuple(re.compile("|".join(group)) if group else None
                 for group in (components, suffixes))


def sync_path_allowed(
    rel: str,
    ignore_patterns: Iterable[str] = (),
    selective_sync_paths: Iterable[str] = (),
    excluded_sync_paths: Iterable[str] = (),
) -> bool:
    rel = rel.strip("/")
    return (
        not _ignored(rel, tuple(ignore_patterns))
        and _selected(rel, _normalize_selected(selective_sync_paths))
        and not _excluded(rel, _normalize_selected(excluded_sync_paths))
    )


def _pattern_matches(rel: str, parts: list[str], pattern: str) -> bool:
    pattern = pattern.strip().strip("/")
    if not pattern:
        return False
    if "/" in pattern:
        return fnmatch(rel, pattern) or any(fnmatch("/".join(parts[index:]), pattern) for index in range(len(parts)))
    return fnmatch(rel, pattern) or any(fnmatch(part, pattern) for part in parts)


def _normalize_selected(paths: Iterable[str]) -> tuple[str, ...]:
    return tuple(path.strip().strip("/") for path in paths if path.strip().strip("/"))


def _selected(rel: str, selected_paths: tuple[str, ...]) -> bool:
    if not selected_paths:
        return True
    rel = rel.strip("/")
    return any(rel == selected or rel.startswith(f"{selected}/") or selected.startswith(f"{rel}/") for selected in selected_paths)


def _excluded(rel: str, excluded_paths: tuple[str, ...]) -> bool:
    rel = rel.strip("/")
    return any(rel == excluded or rel.startswith(f"{excluded}/") for excluded in excluded_paths)


def _digest(path: Path, name: str) -> str:
    digest = hashlib.new(name)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
