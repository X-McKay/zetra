import importlib.util


def test_client_module_exists():
    assert importlib.util.find_spec("zetra.client") is not None


import pytest

from zetra.client import normalized_result, session_directory, session_lock


def test_success_requires_confirmed_cleanup():
    assert (
        normalized_result({"status": "SUCCEEDED", "cleanup": "REMEDIATION_REQUIRED"})["status"]
        == "REMEDIATION_REQUIRED"
    )
    assert normalized_result({"status": "SUCCEEDED", "cleanup": "DELETED"})["status"] == "SUCCEEDED"


def test_session_path_and_worker_exclusion(tmp_path):
    with pytest.raises(ValueError):
        session_directory(tmp_path, "../escape")
    root = session_directory(tmp_path, "one")
    with session_lock(root), pytest.raises(ValueError, match="already"), session_lock(root):
        pass


@pytest.mark.asyncio
async def test_terminal_cleanup_remains_separate_from_effect_outcome():
    from types import SimpleNamespace

    from zetra.client import terminal_result

    class Handle:
        async def describe(self):
            return SimpleNamespace(status=SimpleNamespace(name="TERMINATED"))

    class Remote:
        async def reconcile_closed(self, client):
            return {"cleanup": "DELETED"}

    result = await terminal_result(Handle(), Remote(), object())
    assert result == {
        "status": "TERMINATED",
        "cleanup": "DELETED",
        "effect_status": "OUTCOME_UNKNOWN",
    }

    class FailedRemote:
        async def reconcile_closed(self, client):
            raise RuntimeError("gateway unavailable")

    result = await terminal_result(Handle(), FailedRemote(), object())
    assert result["status"] == "REMEDIATION_REQUIRED"
    assert result["execution_status"] == "TERMINATED"
    assert result["cleanup"] == "NOT_CONFIRMED"


def test_gateway_identity_pins_control_plane_and_client_identity(tmp_path, monkeypatch):
    from zetra.client import gateway_identity

    monkeypatch.setenv("OPENSHELL_TLS_DIR", str(tmp_path))
    monkeypatch.setenv("OPENSHELL_ADDRESS", "127.0.0.1:8000")
    (tmp_path / "ca.crt").write_text("first CA")
    (tmp_path / "tls.crt").write_text("first client")
    identity = gateway_identity()
    monkeypatch.setenv("OPENSHELL_ADDRESS", "127.0.0.1:9000")
    assert gateway_identity() != identity
    monkeypatch.setenv("OPENSHELL_ADDRESS", "127.0.0.1:8000")
    (tmp_path / "ca.crt").write_text("other CA")
    assert gateway_identity() != identity
