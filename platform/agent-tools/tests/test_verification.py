import os

from zetra_agents.verification import replace_installed_file


def test_installed_override_never_changes_hardlinked_cache_or_other_environments(tmp_path):
    cache, peer, target = (tmp_path / name for name in ("cache.py", "peer.py", "step.py"))
    cache.write_bytes(b"pinned SDK source\n")
    cache.chmod(0o644)
    os.link(cache, peer)
    os.link(cache, target)
    original_inode = cache.stat().st_ino
    replace_installed_file(target, b"fixture override\n")
    assert target.read_bytes() == b"fixture override\n"
    assert cache.read_bytes() == peer.read_bytes() == b"pinned SDK source\n"
    assert cache.stat().st_ino == peer.stat().st_ino == original_inode
    assert target.stat().st_ino != original_inode
    assert target.stat().st_mode & 0o777 == 0o644
