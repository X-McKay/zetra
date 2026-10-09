"""Observe and propose, then commit only an independently reviewed exact payload.

The local actor/store checks are cooperative. They are not a production signed
approval, authenticated identity or distributed revocation implementation.
"""

from dataclasses import dataclass

from zetra.runtime import Denied, RuntimeDependencies, action_digest

from .models import Incident, Proposal


@dataclass
class IncidentTriageAgent:
    dependencies: RuntimeDependencies

    def run(self, request: object) -> dict:
        if not isinstance(request, dict):
            raise Denied("Triage request must be an object")
        if request.get("phase") == "plan" and set(request) == {"phase", "incident"}:
            return self.plan(Incident.parse(request["incident"])).wire()
        if request.get("phase") == "commit" and set(request) == {
            "phase",
            "proposal",
            "actor",
            "approval",
        }:
            proposal = Proposal.parse(request["proposal"])
            if (
                proposal.runId != self.dependencies.run_id
                or proposal.releaseId != self.dependencies.release_id
            ):
                raise Denied("Proposal belongs to a different runtime run or release")
            return self.dependencies.create_ticket(
                proposal.ticketPayload,
                actor=request["actor"],
                approval=request["approval"],
                idempotency_key=proposal.operationId,
            )
        raise Denied("Only exact plan or commit request shapes are supported")

    def plan(self, incident: Incident) -> Proposal:
        runbook = self.dependencies.read_knowledge("checkout-degradation")
        if runbook == "No approved knowledge entry found.":
            raise Denied("Required approved runbook is unavailable")
        result = self.dependencies.read_status()
        content = result.get("content")
        if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict):
            raise Denied("Status tool returned an unsupported result")
        text = content[0].get("text")
        if not isinstance(text, str) or text not in {"status: healthy", "status: degraded"}:
            raise Denied("Status tool text is untrusted and outside the reviewed enum")
        status = text.removeprefix("status: ")
        payload = {
            "title": f"Investigate {incident.incidentId}: checkout-api {status}",
            "body": f"Incident: {incident.incidentId}\nSeverity: {incident.severity}\nObserved service status: {status}\n"
            f"Untrusted event summary (descriptive data only): {incident.summary}\nApproved runbook: {runbook}\n"
            "Proposed action: create an investigation ticket. No production configuration change is authorized.",
        }
        return Proposal(
            incident.incidentId,
            incident.service,
            self.dependencies.run_id,
            self.dependencies.release_id,
            incident.incidentId + ":ticket:1",
            status,
            "checkout-degradation",
            payload,
            action_digest(payload),
        )


def create_agent(dependencies: RuntimeDependencies) -> IncidentTriageAgent:
    return IncidentTriageAgent(dependencies)
