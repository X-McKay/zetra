"""What a proposed tool contract does to the agents that use the tool server.

A tool server's contract changes in a pull request on the cluster repository, and no agent
release goes with it. This rule says which agents the change breaks, from the store and the
proposed file alone. It writes nothing. Cases: `contract/impact-cases.json`; the text is
`contract/dependencies.md` section 3.
"""

from __future__ import annotations

from typing import Any

from agent_registry import derive
from agent_registry.models import Deployment, Record, Release
from agent_registry.rules import Refusal
from agent_registry.store import Reads

BLOCKING = frozenset({"tool-removed", "effect-widened", "input-incompatible"})
TOOL_KEYS = ("name", "description", "effect", "inputSchema")


class Finding(Record):
    deployment: str
    agent: str
    tenant: str | None
    authority: str
    tool: str
    finding: str
    blocks: bool
    action: str


class Impact(Record):
    """The answer of section 3.3. `reevaluated` holds agent names."""

    server: str
    backend: str | None
    blocked: bool
    findings: list[Finding]
    reevaluated: list[str]
    users: int


def check_contract(server: str, proposed: Any) -> None:
    """Refuse a body that is not a contract file for `server`."""
    if not isinstance(proposed, dict):
        raise Refusal("invalid-arguments", "contract is not an object")
    if proposed.get("server") != server:
        raise Refusal("invalid-arguments", f"contract is not for the tool server {server}")
    tools = proposed.get("tools")
    if not isinstance(tools, list):
        raise Refusal("invalid-arguments", "contract has no tools list")
    for tool in tools:
        if not isinstance(tool, dict) or any(key not in tool for key in TOOL_KEYS):
            raise Refusal(
                "invalid-arguments", f"every tool needs {', '.join(TOOL_KEYS)} in the contract"
            )
        if not isinstance(tool["name"], str):
            raise Refusal("invalid-arguments", "a tool name is not a string")
    for key in ("image", "configDigest"):
        if not isinstance(proposed.get(key), str):
            raise Refusal("invalid-arguments", f"contract lacks {key}")


def _released(store: Reads, deployment: Deployment) -> list[Release]:
    """The releases an agent is judged by: that of its current revision, and that of its
    newest revision unless it was refused or superseded."""
    revisions = store.revisions(deployment.id)
    chosen = [r for r in revisions if r.number == deployment.current_revision]
    if revisions and revisions[-1].phase not in ("refused", "superseded"):
        chosen.append(revisions[-1])
    found: dict[str, Release] = {}
    for revision in chosen:
        release = store.release(revision.release_id)
        if release is not None:
            found[release.id] = release
    return list(found.values())


def _users(store: Reads, cluster: str, backend: str | None) -> list[tuple[Deployment, set[str]]]:
    """The agent deployments that require tools of `backend`, by id, with the tools they
    require."""
    users: list[tuple[Deployment, set[str]]] = []
    if backend is None:
        return users
    for deployment in sorted(store.deployments(kind="agent", cluster=cluster), key=lambda d: d.id):
        names: set[str] = set()
        required = False
        for release in _released(store, deployment):
            for row in store.requirements(release.id, type="tool", target=backend):
                required = True
                names.update(row.names)
        if required:
            users.append((deployment, names))
    return users


def _properties(schema: Any) -> dict[str, Any]:
    found = schema.get("properties") if isinstance(schema, dict) else None
    return found if isinstance(found, dict) else {}


def _required(schema: Any) -> set[str]:
    found = schema.get("required") if isinstance(schema, dict) else None
    return {str(name) for name in found} if isinstance(found, list) else set()


def _type_of(schema: Any) -> Any:
    return schema.get("type") if isinstance(schema, dict) else None


def incompatible(recorded: Any, proposed: Any) -> bool:
    """Whether callers written for the recorded input schema can break on the proposed one:
    a new required argument, a dropped argument, or an argument of another type. Only the top
    level of each schema is compared."""
    was, now = _properties(recorded), _properties(proposed)
    if _required(proposed) - _required(recorded):
        return True
    if any(name not in now for name in was):
        return True
    return any(_type_of(was[name]) != _type_of(now[name]) for name in was)


def _finding(recorded: dict[str, Any] | None, proposed: dict[str, Any] | None) -> str | None:
    if proposed is None:
        return "tool-removed"
    if proposed["effect"] != "remote_read":
        return "effect-widened"
    if recorded is None:
        return None
    if incompatible(recorded["inputSchema"], proposed["inputSchema"]):
        return "input-incompatible"
    if recorded["inputSchema"] != proposed["inputSchema"]:
        return "input-changed"
    if recorded["description"] != proposed["description"]:
        return "description-changed"
    if recorded["outputSchema"] != proposed["outputSchema"]:
        return "output-changed"
    return None


def _action(finding: str, tool: str, backend: str, agent: str, authority: str) -> str:
    if finding == "tool-removed":
        return f"keep {tool} on {backend}, or release {agent} without it first"
    if finding == "input-incompatible":
        return f"keep the arguments of {tool} on {backend} as they were, or release {agent} first"
    if finding == "effect-widened":
        return f"{tool} on {backend} is no longer read-only and {agent} must stop using it"
    if authority == "registry":
        return f"{tool} changed on {backend}: {agent} is evaluated again after the change"
    return f"{tool} changed on {backend}: check {agent} by hand"


def tool_contract(reads: Reads, cluster: str, server: str, proposed: dict[str, Any]) -> Impact:
    """The agents of `cluster` that the contract `proposed` breaks or changes for `server`.

    Raises `Refusal` `not-found` for an unknown cluster or server and `invalid-arguments` for a
    `proposed` that is not a contract file for that server.
    """
    if reads.cluster(cluster) is None:
        raise Refusal("not-found", f"there is no cluster {cluster}")
    stored = next(
        (d for d in reads.deployments(kind="tool-server", cluster=cluster) if d.item == server),
        None,
    )
    if stored is None:
        raise Refusal("not-found", f"there is no tool server {server} on {cluster}")
    check_contract(server, proposed)

    backend = stored.document.get("backend") if stored.document.get("registered") else None
    recorded = (stored.document.get("contract") or {}).get("tools")
    offered = (derive.contract_record(proposed) or {})["tools"]

    users = _users(reads, cluster, backend)
    findings: list[Finding] = []
    for user, names in users:
        for tool in sorted(names):
            kind = _finding(recorded.get(tool) if recorded else None, offered.get(tool))
            if kind is not None:
                findings.append(
                    Finding(
                        deployment=user.id,
                        agent=user.item,
                        tenant=user.tenant,
                        authority=user.authority,
                        tool=tool,
                        finding=kind,
                        blocks=kind in BLOCKING,
                        action=_action(kind, tool, str(backend), user.item, user.authority),
                    )
                )

    owned = {f.deployment: f.agent for f in findings if f.authority == "registry"}
    blocking = {f.deployment for f in findings if f.blocks}
    return Impact(
        server=server,
        backend=backend,
        blocked=bool(blocking),
        findings=findings,
        reevaluated=[owned[d] for d in sorted(owned) if d not in blocking],
        users=len(users),
    )
