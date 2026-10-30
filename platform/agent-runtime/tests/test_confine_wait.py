import enum
import os
import signal
import subprocess
import sys
import time
import types

from agent_runtime import confine


class FSAccess(enum.IntFlag):
    EXECUTE = 1
    READ_FILE = 4
    READ_DIR = 8


def fake_landlock(calls: list, fail: bool = False) -> types.ModuleType:
    class Ruleset:
        def allow(self, *paths, rules=None):
            calls.append(("allow", paths, rules))

        def apply(self):
            if fail:
                raise OSError("unsupported kernel")
            calls.append(("apply",))

    return types.SimpleNamespace(Ruleset=Ruleset, FSAccess=FSAccess)


def test_confine_read_only_app_and_read_write_sandbox(monkeypatch, tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "sandbox").mkdir()
    monkeypatch.setenv("AGENT_APP_DIR", str(tmp_path / "app"))
    monkeypatch.setenv("AGENT_SANDBOX_DIR", str(tmp_path / "sandbox"))
    calls = []
    monkeypatch.setitem(sys.modules, "landlock", fake_landlock(calls))
    assert confine.apply() is True
    (_, ro, ro_rules), (_, rw, rw_rules), applied = calls
    assert str(tmp_path / "app") in ro and "/usr" in ro and "/etc" in ro
    assert ro_rules == FSAccess.READ_FILE | FSAccess.READ_DIR | FSAccess.EXECUTE
    assert rw == (str(tmp_path / "sandbox"), "/tmp") and rw_rules is None
    assert applied == ("apply",)


def test_confine_failure_is_a_warning(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "landlock", fake_landlock([], fail=True))
    assert confine.apply() is False
    assert "landlock not applied: OSError" in caplog.text


def test_confine_without_module_is_a_warning(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "landlock", None)
    assert confine.apply() is False
    assert "unavailable" in caplog.text


def test_wait_blocks_until_sigterm():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = subprocess.Popen([sys.executable, "-m", "agent_runtime.wait"], cwd=root)
    time.sleep(1.0)
    assert p.poll() is None
    p.send_signal(signal.SIGTERM)
    assert p.wait(timeout=10) == 0
