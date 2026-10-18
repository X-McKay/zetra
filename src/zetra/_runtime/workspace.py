"""Bounded, link-free snapshots of a quiescent disposable workspace.

The trusted supervisor owns writer leases. Unmanaged background processes are
outside the admitted tool profile; a process/container stop is required before
restoring after execution loss. File stability checks are additional detection,
not a substitute for stopping writers.
"""

import base64
import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from .records import canonical_bytes


class WorkspaceError(ValueError):
    pass


class Workspace:
    def __init__(self, root, store, *, max_bytes=256 * 1024 * 1024, max_files=10000):
        self.root = Path(root)
        self.store = store
        self.max_bytes = max_bytes
        self.max_files = max_files
        self.writers = 0
        if self.root.is_symlink() or not self.root.is_dir():
            raise WorkspaceError("workspace must be an existing real directory")

    @contextmanager
    def writer(self):
        if self.writers:
            raise WorkspaceError("overlapping writer")
        self.writers += 1
        try:
            yield
        finally:
            self.writers -= 1

    def capture(self):
        if self.writers:
            raise WorkspaceError("workspace has active writers")
        files = []
        directories = []
        total = 0
        paths = sorted(self.root.rglob("*"))
        if len(paths) > self.max_files:
            raise WorkspaceError("too many paths")
        for path in paths:
            before = path.lstat()
            name = path.relative_to(self.root).as_posix()
            if stat.S_ISDIR(before.st_mode):
                directories.append(name)
                continue
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise WorkspaceError("links and special files are forbidden")
            total += before.st_size
            if total > self.max_bytes:
                raise WorkspaceError("expanded workspace too large")
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as stream:
                data = stream.read(self.max_bytes - total + before.st_size + 1)
                after = os.fstat(stream.fileno())

            def identity(st):
                return (
                    st.st_dev,
                    st.st_ino,
                    st.st_mode,
                    st.st_nlink,
                    st.st_size,
                    st.st_mtime_ns,
                    st.st_ctime_ns,
                )

            if (
                identity(before) != identity(after)
                or identity(before) != identity(path.lstat())
                or len(data) != before.st_size
            ):
                raise WorkspaceError("workspace changed during capture")
            files.append({"path": name, "data": base64.b64encode(data).decode("ascii")})
        if paths != sorted(self.root.rglob("*")):
            raise WorkspaceError("workspace changed during capture")
        return self.store.put(
            canonical_bytes({"version": 1, "files": files, "directories": directories})
        )

    def restore(self, ref):
        if self.writers or any(self.root.iterdir()):
            raise WorkspaceError("restore requires an empty quiescent workspace")
        directories, decoded = validate_snapshot(
            self.store.get(ref), max_bytes=self.max_bytes, max_files=self.max_files
        )
        # Fully validate before writing; never use tar extraction or follow links.
        for p in directories:
            (self.root / p).mkdir(parents=True, exist_ok=True)
        for p, data in decoded:
            path = self.root / p
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)


def validate_snapshot(data, *, max_bytes=256 * 1024 * 1024, max_files=10000):
    value = json.loads(data)
    if set(value) != {"version", "files", "directories"} or value["version"] != 1:
        raise WorkspaceError("invalid snapshot")
    if len(value["files"]) + len(value["directories"]) > max_files:
        raise WorkspaceError("too many paths")
    seen = set()
    decoded = []
    total = 0

    def validate(name):
        if not isinstance(name, str):
            raise WorkspaceError("invalid path")
        p = PurePosixPath(name)
        if (
            p.is_absolute()
            or ".." in p.parts
            or not p.parts
            or p.as_posix() != name
            or "\\" in name
            or "\x00" in name
            or len(name.encode("utf-8")) > 1024
            or len(p.parts) > 32
            or any(len(part.encode("utf-8")) > 255 for part in p.parts)
        ):
            raise WorkspaceError("unsafe path")
        if name in seen:
            raise WorkspaceError("duplicate path")
        seen.add(name)
        return p

    directories = [validate(name) for name in value["directories"]]
    for entry in value["files"]:
        if set(entry) != {"path", "data"}:
            raise WorkspaceError("invalid file entry")
        p = validate(entry["path"])
        try:
            data = base64.b64decode(entry["data"], validate=True)
        except (ValueError, TypeError) as exc:
            raise WorkspaceError("invalid file bytes") from exc
        total += len(data)
        if total > max_bytes:
            raise WorkspaceError("expanded workspace too large")
        decoded.append((p, data))
    file_names = {p.as_posix() for p, _ in decoded}
    for p in directories + [p for p, _ in decoded]:
        if any(parent.as_posix() in file_names for parent in p.parents):
            raise WorkspaceError("file/directory collision")
    expanded_names = set(seen)
    for name in seen:
        expanded_names.update(p.as_posix() for p in PurePosixPath(name).parents if p.parts)
    if len(expanded_names) > max_files:
        raise WorkspaceError("too many materialized paths")
    return directories, decoded
