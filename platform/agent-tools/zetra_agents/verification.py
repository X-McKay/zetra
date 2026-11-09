"""Safe local installed-fixture overrides; uv files may share cache hardlinks."""

import os
import stat
import tempfile
from pathlib import Path


def replace_installed_file(path: Path, content: bytes) -> None:
    """Replace the directory entry, never write through an installed file's inode.

    Use an isolated UV_CACHE_DIR and UV_LINK_MODE=copy as well. This extra guard
    prevents a local verification override from altering a shared pinned SDK.
    """
    if path.is_symlink() or not path.is_file() or not isinstance(content, bytes):
        raise ValueError("override requires an existing regular file and bytes")
    mode = stat.S_IMODE(path.stat().st_mode)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
