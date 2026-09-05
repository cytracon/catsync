from __future__ import annotations

import json
import mimetypes
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator


DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
FILE_FIELDS = (
    "nextPageToken,files(id,name,mimeType,parents,trashed,modifiedTime,md5Checksum,"
    "size,webViewLink,shortcutDetails,exportLinks)"
)
CHANGE_FIELDS = (
    "nextPageToken,newStartPageToken,changes(fileId,removed,time,file("
    "id,name,mimeType,parents,trashed,modifiedTime,md5Checksum,size,webViewLink,shortcutDetails,exportLinks))"
)
UPLOAD_CHUNK_SIZE = 8 * 1024 * 1024
MULTIPART_UPLOAD_LIMIT = 8 * 1024 * 1024
RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}


class DriveError(RuntimeError):
    pass


class DriveFileNotFound(DriveError):
    pass


class ChangesTokenExpired(DriveError):
    pass


@dataclass
class DriveClient:
    access_token: str
    include_shared_drives: bool = False
    refresh_access_token: Callable[[], str] | None = None

    def iter_files(self) -> Iterator[dict[str, Any]]:
        page_token: str | None = None
        while True:
            params: dict[str, str | int | bool] = {
                "pageSize": 1000,
                "fields": FILE_FIELDS,
                "q": "trashed=false",
                "spaces": "drive",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true" if self.include_shared_drives else "false",
            }
            if page_token:
                params["pageToken"] = page_token
            data = self._get_json("/files", params)
            yield from data.get("files", [])
            page_token = data.get("nextPageToken")
            if not page_token:
                return

    def iter_children(self, folder_id: str) -> Iterator[dict[str, Any]]:
        page_token: str | None = None
        query = f"'{folder_id}' in parents and trashed=false"
        while True:
            params: dict[str, str | int | bool] = {
                "pageSize": 1000,
                "fields": FILE_FIELDS,
                "q": query,
                "spaces": "drive",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true" if self.include_shared_drives else "false",
            }
            if page_token:
                params["pageToken"] = page_token
            data = self._get_json("/files", params)
            yield from data.get("files", [])
            page_token = data.get("nextPageToken")
            if not page_token:
                return

    def get_file(self, file_id: str) -> dict[str, Any]:
        return self._get_json(
            f"/files/{urllib.parse.quote(file_id)}",
            {
                "fields": (
                    "id,name,mimeType,parents,trashed,modifiedTime,md5Checksum,"
                    "size,webViewLink,shortcutDetails"
                ),
                "supportsAllDrives": "true",
            },
        )

    def get_root_id(self) -> str:
        data = self._get_json("/files/root", {"fields": "id", "supportsAllDrives": "true"})
        root_id = data.get("id")
        if not root_id:
            raise DriveError("Drive did not return root folder id")
        return str(root_id)

    def get_start_page_token(self) -> str:
        data = self._get_json("/changes/startPageToken", {"supportsAllDrives": "true"})
        token = data.get("startPageToken")
        if not token:
            raise DriveError("Drive did not return startPageToken")
        return token

    def create_folder(self, name: str, parent_id: str = "root") -> dict[str, Any]:
        metadata = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        }
        return self._json_request("POST", "/files", {"fields": "id,name,mimeType,parents,modifiedTime"}, metadata)

    def create_shortcut(self, name: str, target_id: str, parent_id: str = "root") -> dict[str, Any]:
        metadata = {
            "name": name,
            "mimeType": "application/vnd.google-apps.shortcut",
            "parents": [parent_id],
            "shortcutDetails": {"targetId": target_id},
        }
        return self._json_request(
            "POST",
            "/files",
            {"fields": "id,name,mimeType,parents,modifiedTime,shortcutDetails"},
            metadata,
        )

    def upload_file(self, path: Path, name: str, parent_id: str = "root") -> dict[str, Any]:
        metadata = {"name": name, "parents": [parent_id]}
        if path.stat().st_size <= MULTIPART_UPLOAD_LIMIT:
            return self._multipart_upload("POST", "/files", path, metadata)
        return self._resumable_upload("POST", "/files", path, metadata)

    def update_file_content(self, file_id: str, path: Path) -> dict[str, Any]:
        if path.stat().st_size <= MULTIPART_UPLOAD_LIMIT:
            return self._multipart_upload("PATCH", f"/files/{urllib.parse.quote(file_id)}", path, {})
        return self._resumable_upload("PATCH", f"/files/{urllib.parse.quote(file_id)}", path, {})

    def update_metadata(self, file_id: str, metadata: dict[str, Any], params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._json_request("PATCH", f"/files/{urllib.parse.quote(file_id)}", params or {}, metadata)

    def move_file(self, file_id: str, name: str, add_parent: str, remove_parents: list[str]) -> dict[str, Any]:
        params = {
            "addParents": add_parent,
            "removeParents": ",".join(remove_parents),
            "fields": "id,name,mimeType,parents,modifiedTime,md5Checksum,size,webViewLink",
        }
        return self.update_metadata(file_id, {"name": name}, params)

    def trash_file(self, file_id: str) -> dict[str, Any]:
        return self.update_metadata(file_id, {"trashed": True}, {"fields": "id,trashed"})

    def download_bytes(self, file_id: str) -> bytes:
        try:
            with self._urlopen_authorized(
                lambda: urllib.request.Request(f"{DRIVE_API}/files/{urllib.parse.quote(file_id)}?alt=media"),
                timeout=120,
            ) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise DriveError(f"Drive download HTTP {exc.code}: {detail}") from exc

    def iter_changes(self, page_token: str) -> Iterator[dict[str, Any] | dict[str, str]]:
        token = page_token
        while True:
            params: dict[str, str | int | bool] = {
                "pageToken": token,
                "pageSize": 1000,
                "fields": CHANGE_FIELDS,
                "spaces": "drive",
                "includeRemoved": "true",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true" if self.include_shared_drives else "false",
            }
            data = self._get_json("/changes", params)
            yield from data.get("changes", [])
            next_page = data.get("nextPageToken")
            if next_page:
                token = next_page
                continue
            new_token = data.get("newStartPageToken")
            if new_token:
                yield {"newStartPageToken": new_token}
            return

    def _get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        query = urllib.parse.urlencode(params)
        def make_request() -> urllib.request.Request:
            request = urllib.request.Request(f"{DRIVE_API}{path}?{query}")
            request.add_header("Accept", "application/json")
            return request

        try:
            with self._urlopen_authorized(make_request, timeout=60) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            if exc.code == 404:
                raise DriveFileNotFound(f"Drive file not found: {detail}") from exc
            if exc.code == 410:
                raise ChangesTokenExpired(f"Drive changes token expired: {detail}") from exc
            raise DriveError(f"Drive HTTP {exc.code}: {detail}") from exc

    def _json_request(
        self,
        method: str,
        path: str,
        params: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        query = urllib.parse.urlencode({"supportsAllDrives": "true", **params})
        data = json.dumps(payload).encode()
        def make_request() -> urllib.request.Request:
            request = urllib.request.Request(f"{DRIVE_API}{path}?{query}", data=data, method=method)
            request.add_header("Accept", "application/json")
            request.add_header("Content-Type", "application/json; charset=utf-8")
            return request

        try:
            with self._urlopen_authorized(make_request, timeout=60) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            if exc.code == 404:
                raise DriveFileNotFound(f"Drive {method} not found: {detail}") from exc
            raise DriveError(f"Drive {method} HTTP {exc.code}: {detail}") from exc

    def _multipart_upload(
        self,
        method: str,
        path: str,
        file_path: Path,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        boundary = f"catsync-{uuid.uuid4().hex}"
        query = urllib.parse.urlencode(
            {
                "uploadType": "multipart",
                "supportsAllDrives": "true",
                "fields": "id,name,mimeType,parents,modifiedTime,md5Checksum,size,webViewLink",
            }
        )
        body = b"".join(
            [
                f"--{boundary}\r\n".encode(),
                b"Content-Type: application/json; charset=UTF-8\r\n\r\n",
                json.dumps(metadata).encode(),
                b"\r\n",
                f"--{boundary}\r\n".encode(),
                f"Content-Type: {mime_type}\r\n\r\n".encode(),
                file_path.read_bytes(),
                b"\r\n",
                f"--{boundary}--\r\n".encode(),
            ]
        )

        def make_request() -> urllib.request.Request:
            request = urllib.request.Request(f"{DRIVE_UPLOAD_API}{path}?{query}", data=body, method=method)
            request.add_header("Accept", "application/json")
            request.add_header("Content-Type", f"multipart/related; boundary={boundary}")
            request.add_header("Content-Length", str(len(body)))
            return request

        try:
            with self._urlopen_authorized(make_request, timeout=120) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise DriveError(f"Drive multipart upload {method} HTTP {exc.code}: {detail}") from exc

    def _resumable_upload(
        self,
        method: str,
        path: str,
        file_path: Path,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        size = file_path.stat().st_size
        session_uri = self._start_resumable_upload(method, path, metadata, mime_type, size)
        if size == 0:
            return self._put_resumable_chunk(session_uri, b"", "bytes */0", mime_type)

        start = 0
        stalled_attempts = 0
        with file_path.open("rb") as handle:
            while start < size:
                handle.seek(start)
                chunk = handle.read(min(UPLOAD_CHUNK_SIZE, size - start))
                if not chunk:
                    break
                end = start + len(chunk) - 1
                content_range = f"bytes {start}-{end}/{size}"
                response = self._put_resumable_chunk(session_uri, chunk, content_range, mime_type, start)
                if response.final:
                    return response.payload
                next_start = response.committed_end + 1
                if next_start <= start:
                    stalled_attempts += 1
                    if stalled_attempts > 5:
                        raise DriveError(f"Drive resumable upload made no progress at byte {start}")
                else:
                    stalled_attempts = 0
                start = next_start
        raise DriveError("Drive resumable upload ended without final response")

    def _start_resumable_upload(
        self,
        method: str,
        path: str,
        metadata: dict[str, Any],
        mime_type: str,
        size: int,
    ) -> str:
        query = urllib.parse.urlencode(
            {
                "uploadType": "resumable",
                "supportsAllDrives": "true",
                "fields": "id,name,mimeType,parents,modifiedTime,md5Checksum,size,webViewLink",
            }
        )
        data = json.dumps(metadata).encode()
        def make_request() -> urllib.request.Request:
            request = urllib.request.Request(f"{DRIVE_UPLOAD_API}{path}?{query}", data=data, method=method)
            request.add_header("Accept", "application/json")
            request.add_header("Content-Type", "application/json; charset=utf-8")
            request.add_header("X-Upload-Content-Type", mime_type)
            request.add_header("X-Upload-Content-Length", str(size))
            return request

        try:
            with self._urlopen_authorized(make_request, timeout=60) as response:
                location = response.headers.get("Location")
                if not location:
                    raise DriveError("Drive resumable upload did not return a session URI")
                return location
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise DriveError(f"Drive upload session {method} HTTP {exc.code}: {detail}") from exc

    def _put_resumable_chunk(self, session_uri: str, chunk: bytes, content_range: str, mime_type: str, start: int = 0) -> "_ChunkResponse":
        def make_request() -> urllib.request.Request:
            request = urllib.request.Request(session_uri, data=chunk, method="PUT")
            request.add_header("Content-Type", mime_type)
            request.add_header("Content-Length", str(len(chunk)))
            request.add_header("Content-Range", content_range)
            return request

        try:
            with self._urlopen_authorized(make_request, timeout=120) as response:
                body = response.read().decode()
                payload = json.loads(body) if body else {}
                return _ChunkResponse(final=True, payload=payload, committed_end=None)
        except urllib.error.HTTPError as exc:
            if exc.code == 308:
                exc.read()
                return _ChunkResponse(final=False, payload={}, committed_end=_committed_end_from_range(exc.headers.get("Range"), start))
            detail = exc.read().decode(errors="replace")
            raise DriveError(f"Drive upload chunk HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError:
            return _ChunkResponse(final=False, payload={}, committed_end=start - 1)

    def _urlopen_authorized(self, make_request: Callable[[], urllib.request.Request], timeout: int):
        last_error: Exception | None = None
        for attempt in range(4):
            request = make_request()
            request.add_header("Authorization", f"Bearer {self.access_token}")
            try:
                return urllib.request.urlopen(request, timeout=timeout)
            except urllib.error.HTTPError as exc:
                if exc.code == 401 and self.refresh_access_token is not None and attempt == 0:
                    exc.read()
                    self.access_token = self.refresh_access_token()
                    last_error = exc
                    continue
                if exc.code in RETRYABLE_HTTP_CODES and attempt < 3:
                    exc.read()
                    time.sleep(1.5 * (attempt + 1))
                    last_error = exc
                    continue
                if exc.code == 403 and attempt < 5:
                    detail = exc.read().decode(errors="replace")
                    if "CONSUMER_INVALID" in detail or "has been deleted" in detail:
                        time.sleep(2.0 * (attempt + 1))
                        last_error = DriveError(f"Drive HTTP 403: {detail}")
                        continue
                    raise DriveError(f"Drive HTTP 403: {detail}") from exc
                raise
            except urllib.error.URLError as exc:
                if attempt < 3:
                    time.sleep(1.5 * (attempt + 1))
                    last_error = exc
                    continue
                raise
        if last_error:
            raise last_error
        raise DriveError("Drive request failed without a response")


@dataclass(frozen=True)
class _ChunkResponse:
    final: bool
    payload: dict[str, Any]
    committed_end: int | None


def _committed_end_from_range(range_header: str | None, start: int) -> int:
    if not range_header:
        return start - 1
    prefix = "bytes=0-"
    if not range_header.startswith(prefix):
        return start - 1
    value = range_header[len(prefix) :].strip()
    if not value.isdigit():
        return start - 1
    return int(value)
