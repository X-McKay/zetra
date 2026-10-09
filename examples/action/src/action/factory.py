from dataclasses import dataclass

from zetra.runtime import Denied, RuntimeDependencies


@dataclass
class TicketAgent:
    dependencies: RuntimeDependencies

    def run(self, request: dict) -> dict:
        if not isinstance(request, dict) or set(request) != {
            "payload",
            "actor",
            "approval",
            "idempotencyKey",
        }:
            raise Denied("Request requires payload, actor, approval and idempotencyKey")
        # actor and approval are trusted service context in production; accepting
        # them in this offline CLI is only a local authorization demonstration.
        return self.dependencies.create_ticket(
            request["payload"],
            actor=request["actor"],
            approval=request["approval"],
            idempotency_key=request["idempotencyKey"],
        )


def create_agent(dependencies: RuntimeDependencies) -> TicketAgent:
    return TicketAgent(dependencies)
