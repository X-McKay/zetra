"""Entry point: one pass with --once, otherwise a loop until SIGTERM."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Mapping

from .cycle import Worker
from .evaluate import Runtime
from .registry import HttpRegistry, RegistryPort, make_http, make_tokens
from .sessions import HttpSessions, SessionsPort
from .settings import Settings, SettingsError

log = logging.getLogger("registry_evaluator")


def run_loop(step: Callable[[], bool], poll: float, stop: threading.Event) -> None:
    """Run again at once after an evaluation; wait when there was nothing to do."""
    while not stop.is_set():
        try:
            worked = step()
        except Exception:
            log.exception("the pass failed")
            worked = False
        if not worked:
            stop.wait(poll)


def main(
    argv: list[str] | None = None,
    *,
    registry: RegistryPort | None = None,
    sessions: SessionsPort | None = None,
    environ: Mapping[str, str] | None = None,
    stop: threading.Event | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    parser = argparse.ArgumentParser(prog="registry-evaluator")
    parser.add_argument("--once", action="store_true", help="run one pass and exit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    try:
        settings = Settings.from_env(os.environ if environ is None else environ)
        if registry is None or sessions is None:
            http = make_http(settings)
            tokens = make_tokens(settings, http)
            registry = registry or HttpRegistry.from_settings(settings, http, tokens)
            sessions = sessions or HttpSessions.from_settings(settings, http, tokens)
    except SettingsError as exc:
        print(f"registry-evaluator: {exc}", file=sys.stderr)
        return 2

    own_stop = stop is None
    stop = stop or threading.Event()
    run = Runtime(sessions, clock, time.sleep)
    worker = Worker(settings.name, registry, run)
    if args.once:
        return 0 if worker.run_pass() else 1

    previous = []
    if own_stop and threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous.append((sig, signal.signal(sig, lambda *_: stop.set())))
    try:
        run_loop(worker.run_pass, settings.poll_seconds, stop)
    finally:
        for sig, handler in previous:
            signal.signal(sig, handler)
    return 0


if __name__ == "__main__":
    sys.exit(main())
