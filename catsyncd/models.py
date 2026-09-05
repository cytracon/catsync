from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LocalEntry:
    path: str
    kind: str
    size: int | None
    mtime_ns: int | None
    md5: str | None
    sha256: str | None


@dataclass(frozen=True)
class RemoteEntry:
    id: str
    path: str
    name: str
    kind: str
    mime_type: str
    parents: tuple[str, ...]
    trashed: bool
    modified_time: str | None
    md5: str | None
    size: int | None
    web_view_link: str | None


@dataclass(frozen=True)
class PlanAction:
    action: str
    path: str
    reason: str
    local: LocalEntry | None = None
    remote: RemoteEntry | None = None
    source_path: str | None = None
    target_path: str | None = None
    remote_id: str | None = None


@dataclass(frozen=True)
class SyncRecord:
    local_path: str
    remote_id: str
    remote_path: str
    kind: str
    local_md5: str | None
    remote_md5: str | None
    local_size: int | None
    remote_size: int | None
    local_mtime_ns: int | None
    remote_modified_time: str | None


@dataclass(frozen=True)
class DeleteTombstone:
    path: str
    remote_id: str | None
    remote_md5: str | None
    remote_size: int | None
    remote_modified_time: str | None
    deleted_at: int
