from __future__ import annotations

import ctypes
import ctypes.util
import os
import queue
import select
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .inventory import sync_path_allowed


IN_CLOSE_WRITE = 0x00000008
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_Q_OVERFLOW = 0x00004000
IN_ISDIR = 0x40000000
IN_NONBLOCK = 0x00000800
IN_CLOEXEC = 0x00080000

_WATCH_MASK = (
    IN_CLOSE_WRITE
    | IN_MOVED_FROM
    | IN_MOVED_TO
    | IN_CREATE
    | IN_DELETE
    | IN_DELETE_SELF
    | IN_MOVE_SELF
    | IN_Q_OVERFLOW
)
_EVENT_FMT = "iIII"
_EVENT_SIZE = struct.calcsize(_EVENT_FMT)
_SKIP_DIR_NAMES = {".catsync", ".git", "__pycache__", ".venv", "venv", "node_modules"}


@dataclass(frozen=True)
class FsEvent:
    kind: str
    path: str
    src_path: str | None = None
    is_dir: bool = False
    overflow: bool = False


class InotifyWatcher:
    def __init__(
        self,
        root: Path,
        ignore_patterns: Iterable[str] = (),
        selective_sync_paths: Iterable[str] = (),
        excluded_sync_paths: Iterable[str] = (),
    ):
        self.root = root.expanduser().resolve()
        self.ignore_patterns = tuple(ignore_patterns)
        self.selective_sync_paths = tuple(selective_sync_paths)
        self.excluded_sync_paths = tuple(excluded_sync_paths)
        self.events: queue.Queue[FsEvent] = queue.Queue()
        self._wd_to_dir: dict[int, Path] = {}
        self._dir_to_wd: dict[Path, int] = {}
        self._pending_moves: dict[int, tuple[Path, bool, float]] = {}
        self._libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        self._libc.inotify_init1.restype = ctypes.c_int
        self._libc.inotify_add_watch.restype = ctypes.c_int
        self._libc.inotify_rm_watch.restype = ctypes.c_int
        self.fd = int(self._libc.inotify_init1(IN_NONBLOCK | IN_CLOEXEC))
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="catsync-inotify", daemon=True)

    def start(self) -> int:
        added = self.add_tree(self.root)
        self._thread.start()
        return added

    def close(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)
        try:
            os.close(self.fd)
        except OSError:
            pass

    def allowed(self, rel: str) -> bool:
        return sync_path_allowed(rel, self.ignore_patterns, self.selective_sync_paths, self.excluded_sync_paths)

    def add_tree(self, path: Path) -> int:
        added = 0
        path = path.resolve()
        for dirpath, dirnames, _filenames in os.walk(path, followlinks=False):
            current = Path(dirpath)
            rel = self._rel(current)
            keep: list[str] = []
            for name in dirnames:
                if name in _SKIP_DIR_NAMES:
                    continue
                child_rel = f"{rel}/{name}".strip("/") if rel else name
                if self.allowed(child_rel):
                    keep.append(name)
            dirnames[:] = keep
            if rel and not self.allowed(rel):
                dirnames[:] = []
                continue
            if self.add_dir(current):
                added += 1
        return added

    def add_dir(self, path: Path) -> bool:
        path = path.resolve()
        if path in self._dir_to_wd:
            return False
        wd = int(self._libc.inotify_add_watch(self.fd, os.fsencode(str(path)), _WATCH_MASK))
        if wd < 0:
            return False
        self._wd_to_dir[wd] = path
        self._dir_to_wd[path] = wd
        return True

    def remove_dir(self, path: Path) -> None:
        wd = self._dir_to_wd.pop(path, None)
        if wd is None:
            return
        self._wd_to_dir.pop(wd, None)
        self._libc.inotify_rm_watch(self.fd, wd)

    def has_events(self) -> bool:
        return not self.events.empty()

    def drain(self) -> list[FsEvent]:
        items: list[FsEvent] = []
        while True:
            try:
                items.append(self.events.get_nowait())
            except queue.Empty:
                return items

    def _rel(self, path: Path) -> str:
        try:
            rel = path.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return ""
        return "" if rel == "." else rel

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                ready, _wp, _xp = select.select([self.fd], [], [], 0.5)
            except (OSError, ValueError):
                return
            self._expire_pending_moves()
            if not ready:
                continue
            try:
                data = os.read(self.fd, 65536)
            except BlockingIOError:
                continue
            except OSError:
                return
            for event in _parse_inotify_buffer(data):
                self._handle(event)

    def _expire_pending_moves(self) -> None:
        # GUI-Loeschungen (Move in den Papierkorb) erzeugen nur ein
        # MOVED_FROM im Sync-Root; das MOVED_TO liegt im Papierkorb ausserhalb
        # des Watch-Baums und kommt nie an. Ohne Ablauf wuerde die Datei
        # unbemerkt fehlen und spaeter aus Drive wiederhergestellt werden.
        now = time.monotonic()
        for cookie, (src, is_dir, ts) in list(self._pending_moves.items()):
            if now - ts > 2.0:
                del self._pending_moves[cookie]
                rel = self._rel(src)
                self.events.put(FsEvent("deleted", rel, is_dir=is_dir))
                if is_dir:
                    self.remove_dir(src)

    def _handle(self, raw: _RawEvent) -> None:
        if raw.mask & IN_Q_OVERFLOW:
            self.events.put(FsEvent("overflow", "", overflow=True))
            return
        directory = self._wd_to_dir.get(raw.wd)
        if directory is None:
            return
        is_dir = bool(raw.mask & IN_ISDIR)
        target = directory / raw.name if raw.name else directory
        rel = self._rel(target)
        if rel and not self.allowed(rel) and not (raw.mask & (IN_DELETE | IN_MOVED_FROM | IN_DELETE_SELF)):
            return
        if raw.mask & IN_CREATE and is_dir:
            self.add_tree(target)
            self.events.put(FsEvent("created", rel, is_dir=True))
            return
        if raw.mask & IN_DELETE_SELF:
            self.remove_dir(directory)
            if rel:
                self.events.put(FsEvent("deleted", rel, is_dir=True))
            return
        if raw.mask & IN_MOVED_FROM:
            self._pending_moves[raw.cookie] = (target, is_dir, time.monotonic())
            return
        if raw.mask & IN_MOVED_TO:
            src = self._pending_moves.pop(raw.cookie, None)
            if src is not None:
                src_path, _src_is_dir, _ts = src
                self.events.put(FsEvent("moved", rel, src_path=self._rel(src_path), is_dir=is_dir))
                if is_dir:
                    self.remove_dir(src_path)
                    self.add_tree(target)
            else:
                self.events.put(FsEvent("created", rel, is_dir=is_dir))
                if is_dir:
                    self.add_tree(target)
            return
        if raw.mask & IN_DELETE:
            self.events.put(FsEvent("deleted", rel, is_dir=is_dir))
            if is_dir:
                self.remove_dir(target)
            return
        if raw.mask & IN_CLOSE_WRITE:
            self.events.put(FsEvent("modified", rel, is_dir=False))


@dataclass(frozen=True)
class _RawEvent:
    wd: int
    mask: int
    cookie: int
    name: str


def _parse_inotify_buffer(data: bytes) -> list[_RawEvent]:
    events: list[_RawEvent] = []
    offset = 0
    while offset + _EVENT_SIZE <= len(data):
        wd, mask, cookie, length = struct.unpack_from(_EVENT_FMT, data, offset)
        offset += _EVENT_SIZE
        name = data[offset : offset + length].split(b"\x00", 1)[0].decode("utf-8", "surrogateescape")
        offset += length
        events.append(_RawEvent(wd, mask, cookie, name))
    return events
