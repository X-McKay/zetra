"""One pass, as projection.md section 5."""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime

from .kube import KubePort
from .observe import observed_objects, observed_subjects
from .provision import (
    Inventory,
    InventoryError,
    Result,
    error_text,
    project_object,
    read_inventories,
    read_last_generation,
    store_last_generation,
)
from .registry import RegistryError, RegistryPort
from .sessions import SessionPort

log = logging.getLogger("registry_provisioner")

Clock = Callable[[], datetime]
WRITTEN = ("applied", "unchanged")  # the live object now equals the desired one


def utc_now() -> datetime:
    return datetime.now(UTC)


def run_pass(
    kube: KubePort,
    registry: RegistryPort,
    *,
    namespace: str,
    clock: Clock = utc_now,
    sessions: SessionPort | None = None,
) -> bool:
    """True when the registry answered and the report was accepted."""
    stored = read_last_generation(kube, namespace)
    try:
        desired = registry.desired(stored)
    except RegistryError as exc:
        log.warning("the registry gave no answer, nothing changed: %s", exc)
        return False
    manager = desired["fieldManager"]
    last = store_last_generation(kube, namespace, stored, int(desired["generation"]), manager)

    namespaces = list(desired["namespaces"])
    inventory: Inventory | None
    try:
        inventory = read_inventories(kube, desired["fluxInventories"])
        inventory_error = ""
    except InventoryError as exc:
        inventory, inventory_error = None, str(exc)
        log.warning("nothing is written this pass: %s", exc)

    results: list[Result] = []
    if desired["mode"] == "recovery":
        log.info("the registry is in recovery: nothing is written, observations continue")
    else:
        for dep in desired.get("deployments", []):
            for entry in dep["objects"]:
                if inventory is None:
                    results.append(_unwritten(dep["deployment"], entry, inventory_error))
                else:
                    results.append(
                        project_object(
                            kube,
                            entry,
                            dep["deployment"],
                            namespaces=set(namespaces),
                            inventory=inventory,
                            field_manager=manager,
                        )
                    )

    if desired["mode"] != "recovery":
        cancel_revoked(sessions, desired.get("revocations") or [], results)

    holds = None
    if "holds" in desired:  # a registry that does not know the key is never sent it
        holds = (
            []
            if desired["mode"] == "recovery"
            else observe_holds(sessions, desired["holds"] or [], results)
        )

    objects, complete = observed_objects(kube, namespaces, inventory)
    body = {
        "observedAt": clock().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "lastGeneration": last,
        "complete": complete,
        "objects": objects,
        "subjects": observed_subjects(kube, desired.get("observe", [])),
        "projections": [r.wire() for r in results],
    }
    if holds is not None:
        body["holds"] = holds
    _log_results(results)
    try:
        registry.report(body)
    except RegistryError as exc:
        log.warning("the report was not accepted, it will be sent again: %s", exc)
        return False
    return True


def cancel_revoked(
    sessions: SessionPort | None, revocations: list[dict], results: list[Result]
) -> None:
    """Delivery section 8.3: cancel a revoked release's sessions, only after this pass has
    written its AgentRelease. A failure is logged and tried again next pass."""
    written = {
        (r.namespace, r.name) for r in results if r.kind == "AgentRelease" and r.result in WRITTEN
    }
    for rev in revocations:
        who = f"revocation {rev['id']} release {rev['release']}"
        if (rev["namespace"], rev["release"]) not in written:
            log.info("%s: not cancelled: the release was not written this pass", who)
        elif sessions is None:
            log.info("%s: not cancelled: no session interface configured", who)
        else:
            try:
                count = sessions.cancel_release(
                    rev["tenant"], rev["agent"], rev["release"], f"release revoked: {rev['id']}"
                )
            except Exception as exc:
                log.warning("%s: cancel failed, will be tried again: %s", who, error_text(exc))
            else:
                log.info("%s: cancelled %d session(s)", who, count)


def observe_holds(
    sessions: SessionPort | None, holds: list[dict], results: list[Result]
) -> list[dict]:
    """Dependencies section 7.3: for each hold whose Agent is written as asked, cancel the
    task sessions when it says so, then count those still running. A hold that fails is left
    out, logged and tried again next pass; it never stops the others."""
    written = {(r.namespace, r.name) for r in results if r.kind == "Agent" and r.result in WRITTEN}
    reported = []
    for hold in holds:
        who = f"hold {hold['move']} deployment {hold['deployment']}"
        if (hold["namespace"], hold["agent"]) not in written:
            log.info("%s: not counted: the Agent was not written this pass", who)
        elif sessions is None:
            log.info("%s: not counted: no session interface configured", who)
        else:
            try:
                if hold.get("sessions") == "cancel":
                    count = sessions.cancel_sessions(
                        hold["tenant"], hold["agent"], "task", f"alias move {hold['move']}"
                    )
                    log.info("%s: cancelled %d session(s)", who, count)
                running = sum(
                    1
                    for s in sessions.list_running(hold["tenant"], hold["agent"])
                    if s.get("purpose") == "task"
                )
            except Exception as exc:
                log.warning("%s: not counted, will be tried again: %s", who, error_text(exc))
            else:
                reported.append(
                    {"move": hold["move"], "deployment": hold["deployment"], "running": running}
                )
    return reported


def _unwritten(deployment: str, entry: dict, why: str) -> Result:
    return Result(
        deployment,
        str(entry.get("kind")),
        str(entry.get("namespace")),
        str(entry.get("name")),
        entry.get("generation", 0),
        "error",
        why,
    )


def _log_results(results: list[Result]) -> None:
    counts = Counter(r.result for r in results)
    summary = " ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no objects"
    log.info("pass: %s", summary)
    for r in results:
        if r.result != "unchanged":
            log.info("%s %s/%s: %s %s", r.kind, r.namespace, r.name, r.result, r.message)
