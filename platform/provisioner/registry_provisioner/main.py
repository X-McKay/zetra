"""Entry point: one pass with --once, otherwise a loop until SIGTERM."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from collections.abc import Mapping
from pathlib import Path

from .cycle import Clock, run_pass, utc_now
from .kube import ApiServer, KubePort
from .registry import HttpRegistry, RegistryPort
from .settings import Settings, SettingsError

log = logging.getLogger("registry_provisioner")


def _touch(path: str | None) -> None:
    if not path:
        return
    try:
        Path(path).touch()
        os.utime(path)
    except OSError as exc:
        log.warning("cannot touch the heartbeat file: %s", type(exc).__name__)


def pass_with_heartbeat(
    kube: KubePort, registry: RegistryPort, settings: Settings, clock: Clock
) -> bool:
    """One pass that never raises; the heartbeat is touched whatever the result."""
    try:
        ok = run_pass(kube, registry, namespace=settings.namespace, clock=clock)
    except Exception:
        log.exception("the pass failed")
        ok = False
    _touch(settings.heartbeat_file)
    return ok


def run_loop(step, interval: float, stop: threading.Event) -> None:
    while not stop.is_set():
        step()
        stop.wait(interval)


def main(
    argv: list[str] | None = None,
    *,
    kube: KubePort | None = None,
    registry: RegistryPort | None = None,
    environ: Mapping[str, str] | None = None,
    stop: threading.Event | None = None,
    clock: Clock = utc_now,
) -> int:
    parser = argparse.ArgumentParser(prog="registry-provisioner")
    parser.add_argument("--once", action="store_true", help="run one pass and exit")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    try:
        settings = Settings.from_env(os.environ if environ is None else environ)
        if registry is None:
            registry = HttpRegistry.from_settings(settings)
    except SettingsError as exc:
        print(f"registry-provisioner: {exc}", file=sys.stderr)
        return 2
    kube = kube or ApiServer()

    def step() -> bool:
        return pass_with_heartbeat(kube, registry, settings, clock)

    if args.once:
        return 0 if step() else 1

    own_stop = stop is None
    stop = stop or threading.Event()
    previous = []
    if own_stop and threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous.append((sig, signal.signal(sig, lambda *_: stop.set())))
    try:
        run_loop(step, settings.interval_seconds, stop)
    finally:
        for sig, handler in previous:
            signal.signal(sig, handler)
    return 0


if __name__ == "__main__":
    sys.exit(main())
