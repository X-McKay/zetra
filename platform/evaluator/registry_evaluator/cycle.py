"""One pass of the loop: post what is waiting to be posted, else claim and run."""

from __future__ import annotations

import logging

from . import __version__
from .evaluate import Runtime, evaluation_id, run_suite
from .registry import CLAIM_LEASE_SECONDS, RegistryError, RegistryPort

log = logging.getLogger("registry_evaluator")


class Worker:
    """Holds the one result that the registry has not yet accepted."""

    def __init__(self, name: str, registry: RegistryPort, run: Runtime) -> None:
        self.name = name
        self.registry = registry
        self.run = run
        self.unposted: tuple[dict, dict] | None = None  # (evaluation, result body)

    def run_pass(self) -> bool:
        """True when an evaluation was handled, so the next pass need not wait."""
        if self.unposted is not None:
            return self._post(*self.unposted)
        try:
            ev = self.registry.claim(self.name, CLAIM_LEASE_SECONDS)
        except RegistryError as exc:
            log.warning("the registry gave no answer to the claim: %s", exc)
            return False
        if ev is None:
            return False
        verdict = run_suite(ev, self.run)
        body = {"evaluator": self.name, "evaluatorVersion": __version__, **verdict}
        log.info("evaluation %s: %s: %s", evaluation_id(ev), body["verdict"], body["summary"])
        self._post(ev, body)
        return True

    def _post(self, ev: dict, body: dict) -> bool:
        try:
            answer = self.registry.result(ev["deployment"], ev["revision"], ev["attempt"], body)
        except RegistryError as exc:
            if exc.transient:
                log.warning("the result of %s is kept to post again: %s", evaluation_id(ev), exc)
                self.unposted = (ev, body)
                return False
            log.warning("the result of %s was refused and is dropped: %s", evaluation_id(ev), exc)
            self.unposted = None
            return True
        self.unposted = None
        if answer.get("recorded") is False:
            log.info("evaluation %s was not recorded: %s", evaluation_id(ev), answer)
        return True
