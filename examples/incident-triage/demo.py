"""One continuous offline triage run; writes only a temporary SQLite ticket.

PYTHONPATH=src:examples/incident-triage/src .venv/bin/python examples/incident-triage/demo.py
No shared database, socket, model call, Kubernetes mutation or production ticket.
"""

import hashlib
import json
import tempfile
from pathlib import Path

from incident_triage.factory import create_agent
from incident_triage.fixture import INCIDENT, RUN_ID, RUNBOOK, fixture_client

from zetra.analysis import risk
from zetra.evidence import fingerprint
from zetra.manifest import load
from zetra.runtime import ActionStore, Denied, RuntimeDependencies


def demonstrate() -> dict:
    root = Path(__file__).resolve().parent
    manifest = load(root)
    release_id = fingerprint(root)
    trace = []

    def emit(event):
        trace.append(
            {
                "sequence": len(trace) + 1,
                "runId": RUN_ID,
                "releaseId": release_id,
                "incidentId": INCIDENT["incidentId"],
                "businessFunction": manifest.business_function,
                **event,
            }
        )

    gateway, _ = fixture_client(emit)
    with tempfile.TemporaryDirectory(prefix="zetra-triage-") as directory:
        state = Path(directory)
        receiver = ActionStore(state / "receiver.db")
        deps = RuntimeDependencies(
            frozenset(manifest.capabilities),
            manifest.max_steps,
            manifest.max_seconds,
            manifest.max_cost_usd,
            state / "STOP",
            knowledge={"checkout-degradation": RUNBOOK},
            action_store=receiver,
            gateway_client=gateway,
            release_id=release_id,
            run_id=RUN_ID,
        )
        agent = create_agent(deps)
        emit({"event": "incident.accepted", "dataClassification": "internal", "input": INCIDENT})
        proposal = agent.run({"phase": "plan", "incident": INCIDENT})
        emit(
            {
                "event": "ticket.proposed",
                "capability": "ticket.write",
                "operationId": proposal["operationId"],
                "actionDigest": proposal["actionDigest"],
                "payload": proposal["ticketPayload"],
                "writes": 0,
            }
        )
        actor = "reviewer:alice"
        # Independent LOCAL demo controller, not the agent. Production requires
        # an authenticated approval service and signed/fenced receiver contract.
        approval = receiver.issue(proposal["ticketPayload"], actor=actor, ttl_seconds=300)
        emit(
            {
                "event": "approval.issued",
                "actor": actor,
                "approvalRef": hashlib.sha256(approval.encode()).hexdigest()[:16],
                "binding": "local actor+payload digest+expiry+single-use",
                "ttlSeconds": 300,
            }
        )
        request = {"phase": "commit", "proposal": proposal, "actor": actor, "approval": approval}
        receipt = agent.run(request)
        emit(
            {
                "event": "ticket.committed",
                "operationId": proposal["operationId"],
                "receipt": receipt,
            }
        )
        # Reconstruct the receiver to demonstrate a process restart after the
        # atomic effect/receipt transaction, using the same durable database.
        deps.action_store = ActionStore(state / "receiver.db")
        retried = agent.run(request)
        assert receipt == retried
        emit(
            {
                "event": "delivery.retried",
                "operationId": proposal["operationId"],
                "receipt": retried,
            }
        )
        try:
            receiver.commit_ticket(
                proposal["ticketPayload"],
                actor=actor,
                approval=approval,
                idempotency_key="INC-2026-0042:ticket:2",
            )
        except Denied:
            emit(
                {
                    "event": "approval.reuse.denied",
                    "reason": "Consumed grant cannot authorize a new operation",
                }
            )
        else:
            raise AssertionError("Consumed approval unexpectedly authorized another ticket")
        deps.stop_file.touch()
        try:
            agent.run(request)
        except Denied:
            emit(
                {
                    "event": "stop.denied",
                    "scope": "cooperative local STOP file",
                    "committedEffectsUndone": False,
                }
            )
        else:
            raise AssertionError("Stopped runtime unexpectedly invoked the receiver")
        with receiver.connect() as connection:
            writes = connection.execute("SELECT count(*) FROM tickets").fetchone()[0]
        assert writes == 1
        assert approval not in json.dumps(trace)
    return {
        "scope": "offline-deterministic-wire-fixture-and-temporary-SQLite",
        "productionQualified": False,
        "risk": risk(manifest),
        "trace": trace,
        "ticketRows": writes,
        "databaseRemoved": True,
        "modelCalls": 0,
        "networkConnections": 0,
        "limits": [
            "No live MCP authentication in this fixture",
            "Local actor/payload grants are not production signed authorization",
            "Budget and STOP helpers are cooperative per runtime instance",
            "Temporal write worker remains rejected",
        ],
    }


if __name__ == "__main__":
    print(json.dumps(demonstrate(), indent=2))
