import importlib.util


def test_cli_module_exists():
    assert importlib.util.find_spec("zetra.cli") is not None


def test_terminal_execution_is_nonzero_even_after_confirmed_deletion(monkeypatch, capsys):
    import sys

    from zetra import cli, manifest
    from zetra.client import SessionClient

    async def terminated(self, *args, **kwargs):
        return {"status": "TERMINATED", "cleanup": "DELETED"}

    monkeypatch.setattr(SessionClient, "run", terminated)
    monkeypatch.setattr(manifest, "load_release", lambda path: object())
    monkeypatch.setattr(
        sys, "argv", ["zetra", "run", "release.json", "--session", "one", "--input", "task"]
    )
    assert cli.main() == 1
    assert "TERMINATED" in capsys.readouterr().out
