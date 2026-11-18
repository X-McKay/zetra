"""`agent-registry`: serve, migrate, import, recover, openapi."""

from __future__ import annotations

import argparse
import json
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

from agent_registry.config import Settings


def _store(settings: Settings):
    from agent_registry.store.pg import PgStore

    if not settings.dsn:
        raise SystemExit("REGISTRY_DSN is not set")
    return PgStore(settings.dsn)


def openapi_document() -> dict:
    """The HTTP interface as built from the code. `contract/openapi.json` is this, committed."""
    from agent_registry.api.app import create_app
    from agent_registry.api.auth import StaticVerifier
    from agent_registry.store.memory import MemoryStore

    return create_app(MemoryStore(), Settings(), StaticVerifier({})).openapi()


def _openapi(args: argparse.Namespace) -> int:
    text = json.dumps(openapi_document(), indent=2, sort_keys=True) + "\n"
    if args.write:
        Path(args.write).write_text(text)
    else:
        sys.stdout.write(text)
    return 0


def _migrate(args: argparse.Namespace) -> int:
    from agent_registry.store.pg import migrate

    settings = Settings.from_env()
    if not settings.dsn:
        raise SystemExit("REGISTRY_DSN is not set")
    applied = migrate(settings.dsn)
    print(json.dumps({"applied": applied}))
    return 0


def _import(args: argparse.Namespace) -> int:
    from agent_registry import importer

    store = _store(Settings.from_env())
    changed = importer.apply_feed(store, importer.load_feed(Path(args.feed)))
    report = importer.import_documents(
        store,
        importer.load_directory(Path(args.dir)),
        cluster=args.cluster,
        now=datetime.now(UTC),
    )
    print(
        json.dumps(
            {
                "feedChanged": changed,
                "agents": report.agents,
                "releases": report.releases,
                "created": report.created,
                "updated": report.updated,
                "orphans": report.orphans,
                "skipped": report.skipped,
                "problems": report.problems,
            },
            indent=2,
        )
    )
    return 1 if report.problems else 0


def _recover(args: argparse.Namespace) -> int:
    from agent_registry.modes import enter_recovery

    entered = enter_recovery(
        _store(Settings.from_env()), args.reason, actor="agent-registry recover"
    )
    print(json.dumps({"mode": "recovery", "entered": entered}))
    return 0


def _watch_feed(store, path: Path, stop: threading.Event, every: float = 30.0) -> None:
    """Apply the feed again whenever the mounted file changes."""
    from agent_registry import importer

    seen = None
    while not stop.is_set():
        try:
            text = path.read_text()
            if text != seen:
                importer.apply_feed(store, importer.load_feed(path))
                seen = text
        except Exception as error:  # noqa: BLE001 - keep serving on a bad feed; say so
            print(json.dumps({"feed": str(path), "error": str(error)}), file=sys.stderr)
        stop.wait(every)


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    from agent_registry.api.app import create_app
    from agent_registry.api.jwks import JwksVerifier
    from agent_registry.modes import enter_recovery
    from agent_registry.store.pg import migrate

    settings = Settings.from_env()
    if not settings.dsn:
        raise SystemExit("REGISTRY_DSN is not set")
    migrate(settings.dsn)
    store = _store(settings)
    if settings.start_in_recovery:
        enter_recovery(store, "started with REGISTRY_START_IN_RECOVERY", actor="agent-registry")
    stop = threading.Event()
    if settings.feed_path:
        threading.Thread(
            target=_watch_feed, args=(store, Path(settings.feed_path), stop), daemon=True
        ).start()
    try:
        uvicorn.run(
            create_app(store, settings, JwksVerifier(settings)),
            host=settings.host,
            port=settings.port,
            log_level="info",
        )
    finally:
        stop.set()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-registry")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("serve", help="migrate, then serve the HTTP interface").set_defaults(
        run=_serve
    )
    commands.add_parser("migrate", help="apply migrations and stop").set_defaults(run=_migrate)

    load = commands.add_parser("import", help="apply a feed and index agent manifests")
    load.add_argument("--dir", required=True, help="directory of Agent and AgentRelease YAML")
    load.add_argument("--feed", required=True, help="catalog feed JSON")
    load.add_argument("--cluster", default="kubani")
    load.set_defaults(run=_import)

    recover = commands.add_parser("recover", help="put the registry in recovery")
    recover.add_argument("--reason", required=True)
    recover.set_defaults(run=_recover)

    spec = commands.add_parser("openapi", help="print the HTTP interface")
    spec.add_argument("--write", help="write to this file instead of standard output")
    spec.set_defaults(run=_openapi)

    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    raise SystemExit(main())
