"""Offline trusted-reviewer demonstration; writes only to a temporary SQLite DB."""

import tempfile
from pathlib import Path

from action.factory import create_agent

from zetra.runtime import ActionStore, RuntimeDependencies

with tempfile.TemporaryDirectory() as directory:
    state = Path(directory)
    receiver = ActionStore(state / "receiver.db")
    payload = {"title": "Review incident", "body": "Investigate the reference example."}
    # The authenticated approval service owns this call in production.
    approval = receiver.issue(payload, actor="reviewer:alice")
    deps = RuntimeDependencies(
        frozenset({"ticket.write"}), 3, 30, 0.05, state / "STOP", action_store=receiver
    )
    agent = create_agent(deps)
    request = {
        "payload": payload,
        "actor": "reviewer:alice",
        "approval": approval,
        "idempotencyKey": "incident-1",
    }
    print("First attempt:", agent.run(request))
    print("Retry:", agent.run(request))
