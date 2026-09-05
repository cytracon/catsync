import urllib.error
import urllib.request
from email.message import Message
from io import BytesIO

from catsyncd.drive import DriveClient, DriveError, MULTIPART_UPLOAD_LIMIT, _ChunkResponse, _committed_end_from_range


def test_committed_end_from_range_parses_google_308_header():
    assert _committed_end_from_range("bytes=0-8388607", 0) == 8388607


def test_committed_end_from_range_retries_same_chunk_when_missing():
    assert _committed_end_from_range(None, 1024) == 1023
    assert _committed_end_from_range("invalid", 1024) == 1023


def test_resumable_upload_resends_from_partial_range(tmp_path):
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"0123456789")
    client = DriveClient("token")
    calls = []

    def fake_start(*_args):
        return "session-uri"

    def fake_put(_session_uri, chunk, content_range, _mime_type, start=0):
        calls.append((start, chunk, content_range))
        if len(calls) == 1:
            return _ChunkResponse(final=False, payload={}, committed_end=3)
        return _ChunkResponse(final=True, payload={"id": "file-id"}, committed_end=None)

    client._start_resumable_upload = fake_start
    client._put_resumable_chunk = fake_put

    result = client._resumable_upload("POST", "/files", payload, {"name": "payload.bin"})

    assert result == {"id": "file-id"}
    assert calls == [
        (0, b"0123456789", "bytes 0-9/10"),
        (4, b"456789", "bytes 4-9/10"),
    ]


def test_put_resumable_chunk_parses_real_http_308_range():
    client = DriveClient("token")
    original_urlopen = urllib.request.urlopen
    headers = Message()
    headers["Range"] = "bytes=0-3"

    def fake_urlopen(_request, timeout=0):
        raise urllib.error.HTTPError("session", 308, "Resume Incomplete", headers, BytesIO(b""))

    urllib.request.urlopen = fake_urlopen
    try:
        response = client._put_resumable_chunk("https://upload.example/session", b"0123456789", "bytes 0-9/10", "text/plain", start=0)
    finally:
        urllib.request.urlopen = original_urlopen

    assert response.final is False
    assert response.committed_end == 3


def test_put_resumable_chunk_retries_same_chunk_on_urlerror():
    client = DriveClient("token")
    original_urlopen = urllib.request.urlopen

    def fake_urlopen(_request, timeout=0):
        raise urllib.error.URLError("temporary reset")

    urllib.request.urlopen = fake_urlopen
    try:
        response = client._put_resumable_chunk("https://upload.example/session", b"0123456789", "bytes 10-19/20", "text/plain", start=10)
    finally:
        urllib.request.urlopen = original_urlopen

    assert response.final is False
    assert response.committed_end == 9


def test_resumable_upload_aborts_after_repeated_no_progress(tmp_path):
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"0123456789")
    client = DriveClient("token")

    client._start_resumable_upload = lambda *_args: "session-uri"
    client._put_resumable_chunk = lambda *_args, start=0: _ChunkResponse(final=False, payload={}, committed_end=start - 1)

    try:
        client._resumable_upload("POST", "/files", payload, {"name": "payload.bin"})
    except DriveError as exc:
        assert "made no progress" in str(exc)
    else:
        raise AssertionError("expected DriveError")


def test_small_upload_uses_multipart_upload(tmp_path):
    payload = tmp_path / "small.txt"
    payload.write_text("small")
    client = DriveClient("token")
    calls = []

    client._multipart_upload = lambda method, path, file_path, metadata: calls.append((method, path, file_path, metadata)) or {"id": "small-id"}
    client._resumable_upload = lambda *_args: (_ for _ in ()).throw(AssertionError("resumable should not be used"))

    result = client.upload_file(payload, "small.txt", "parent-id")

    assert result == {"id": "small-id"}
    assert calls == [("POST", "/files", payload, {"name": "small.txt", "parents": ["parent-id"]})]


def test_large_upload_uses_resumable_upload(tmp_path):
    payload = tmp_path / "large.bin"
    payload.write_bytes(b"0" * (MULTIPART_UPLOAD_LIMIT + 1))
    client = DriveClient("token")
    calls = []

    client._multipart_upload = lambda *_args: (_ for _ in ()).throw(AssertionError("multipart should not be used"))
    client._resumable_upload = lambda method, path, file_path, metadata: calls.append((method, path, file_path, metadata)) or {"id": "large-id"}

    result = client.upload_file(payload, "large.bin", "parent-id")

    assert result == {"id": "large-id"}
    assert calls == [("POST", "/files", payload, {"name": "large.bin", "parents": ["parent-id"]})]
