"""Agents of the pull request path, from documents (`contract/import.md` sections 3 and 4)."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from agent_registry import canonical, derive
from agent_registry.importer.report import ImportReport
from agent_registry.importer.skills import record_skills
from agent_registry.models import (
    Approval,
    Deployment,
    Item,
    Revision,
    Tenant,
    deployment_id,
    revision_id,
)
from agent_registry.store import Store, StoreError, Tx

API_GROUP = "agentplatform.io/"
API_VERSION = "agentplatform.io/v1alpha1"
KINDS = ("Agent", "AgentRelease")
SUFFIXES = (".yaml", ".yml")


class Documents(list):
    """What `load_directory` returns: the documents, and `problems`, the files that could not
    be read. `import_documents` adds those to its report."""

    def __init__(self, documents: list[dict[str, Any]], problems: list[str]):
        super().__init__(documents)
        self.problems = problems


def load_directory(path: Path) -> list[dict[str, Any]]:
    """Every `Agent` and `AgentRelease` document in the YAML files under `path`. Other kinds
    (kustomizations, RBAC) are ignored. A file that cannot be read is a problem, named by its
    path in the result's `problems`."""
    documents: list[dict[str, Any]] = []
    problems: list[str] = []
    for file in sorted(Path(path).rglob("*")):
        if file.suffix not in SUFFIXES or not file.is_file():
            continue
        try:
            found = list(yaml.safe_load_all(file.read_text()))
        except (yaml.YAMLError, OSError, UnicodeDecodeError) as error:
            problems.append(f"{file}: cannot be read: {' '.join(str(error).split())}")
            continue
        documents.extend(
            d
            for d in found
            if isinstance(d, dict)
            and d.get("kind") in KINDS
            and str(d.get("apiVersion") or "").startswith(API_GROUP)
        )
    return Documents(documents, problems)


def _key(document: dict[str, Any]) -> tuple[str, str]:
    meta = document.get("metadata") or {}
    return str(meta.get("namespace") or ""), str(meta.get("name") or "")


def _time(value: Any) -> datetime:
    if not isinstance(value, datetime):
        value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _approval_block(release: dict[str, Any]) -> dict[str, Any] | None:
    """`spec.approval` when it says approved, else None."""
    block = (release.get("spec") or {}).get("approval")
    return block if isinstance(block, dict) and block.get("approved") is True else None


def _check_block(block: dict[str, Any]) -> None:
    _time(block["approvedAt"])
    if not isinstance(block.get("approvedBy"), str) or not block["approvedBy"]:
        raise ValueError("approvedBy is missing")


def import_documents(
    store: Store,
    documents: list[dict[str, Any]],
    *,
    cluster: str,
    now: datetime,
    actor: str = "importer",
) -> ImportReport:
    """Index agents owned by the pull request path from their documents."""
    return _import(store, documents, cluster=cluster, now=now, actor=actor, observed=False)


def index_observations(
    store: Store, cluster: str, *, now: datetime, actor: str = "indexer"
) -> ImportReport:
    """The same, with the documents taken from the cluster's last observations, so that the
    index follows what pull requests change. Called after each observation report."""
    documents = []
    for kind, subject in (("Agent", "agent"), ("AgentRelease", "agentrelease")):
        for seen in store.observations(cluster, subject_kind=subject):
            if not seen.present:
                continue
            document = copy.deepcopy(seen.document)
            document["apiVersion"], document["kind"] = API_VERSION, kind
            document["metadata"] = {
                **(document.get("metadata") or {}),
                "name": seen.name,
                "namespace": seen.namespace,
            }
            documents.append(document)
    return _import(store, documents, cluster=cluster, now=now, actor=actor, observed=True)


def _import(
    store: Store,
    documents: list[dict[str, Any]],
    *,
    cluster: str,
    now: datetime,
    actor: str,
    observed: bool,
) -> ImportReport:
    report = ImportReport(problems=list(getattr(documents, "problems", [])))
    agents: dict[tuple[str, str], dict[str, Any]] = {}
    releases: dict[tuple[str, str], dict[str, Any]] = {}
    for document in documents:
        kind = document.get("kind")
        if kind not in KINDS:
            continue
        table = agents if kind == "Agent" else releases
        if kind == "Agent":
            report.agents += 1
        else:
            report.releases += 1
        key = _key(document)
        if not all(key):
            report.problems.append(f"{kind} without a name and namespace")
        elif key in table and table[key] != document:
            report.problems.append(f"{kind} {key[0]}/{key[1]} appears twice with different content")
        else:
            table.setdefault(key, document)

    # With statuses, the controller's verdict decides which release can be current.
    with_status = observed or any(r.get("status") for r in releases.values())
    by_agent: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for (namespace, name), release in sorted(releases.items()):
        agent = str((release.get("spec") or {}).get("agent") or "")
        if (namespace, agent) in agents:
            by_agent.setdefault((namespace, agent), []).append(release)
        else:
            report.orphans.append(f"{namespace}/{name}")

    tenants = {t.namespace: t for t in store.tenants(cluster)}
    for (namespace, name), agent in sorted(agents.items()):
        tenant = tenants.get(namespace)
        if tenant is None:
            report.problems.append(
                f"Agent {namespace}/{name}: no tenant of cluster {cluster} has that namespace"
            )
            continue
        created: list[str] = []
        updated: list[str] = []
        problems: list[str] = []
        try:
            with store.transaction(actor) as tx:
                skipped = _import_agent(
                    tx,
                    cluster=cluster,
                    tenant=tenant,
                    agent=agent,
                    releases=by_agent.get((namespace, name), []),
                    now=now,
                    with_status=with_status,
                    created=created,
                    updated=updated,
                    problems=problems,
                )
                if skipped is None and (created or updated):
                    tx.event(
                        "agent-imported",
                        deployment_id=deployment_id("agent", cluster, tenant.name, name),
                        detail={"created": created, "updated": updated},
                    )
        except StoreError as error:
            report.problems.append(f"Agent {namespace}/{name}: {error}")
            continue
        report.problems.extend(problems)
        if skipped is not None:
            report.skipped.append(skipped)
        else:
            report.created.extend(created)
            report.updated.extend(updated)
    return report


def _import_agent(
    tx: Tx,
    *,
    cluster: str,
    tenant: Tenant,
    agent: dict[str, Any],
    releases: list[dict[str, Any]],
    now: datetime,
    with_status: bool,
    created: list[str],
    updated: list[str],
    problems: list[str],
) -> str | None:
    """One agent's records, in the caller's transaction. Returns the deployment id when the
    registry owns it and nothing was written."""
    namespace, name = _key(agent)
    did = deployment_id("agent", cluster, tenant.name, name)
    have = tx.deployment(did)
    if have is not None and have.authority == "registry":
        return did

    document = canonical.agent_document(agent)
    spec = document["spec"]
    pin = derive.pin_for(tx, cluster, tenant.name, name)
    item = Item(
        kind="agent",
        name=name,
        owner=spec.get("owner") or "",
        source=pin.repository if pin else "",
        description=spec.get("description") or "",
    )
    have_item = tx.item("agent", name)
    if have_item != item:
        tx.put_item(item)
        (created if have_item is None else updated).append(f"agent:{name}")

    deployment = (
        have
        if have is not None
        else Deployment(id=did, kind="agent", item=name, cluster=cluster, namespace=namespace)
    ).model_copy(
        update={
            "tenant": tenant.name,
            "authority": "pull-request",
            "enabled": spec.get("enabled") is not False,
            "document": {"agent": document},
        }
    )
    if have != deployment:
        tx.put_deployment(deployment)
    if have is None:
        created.append(did)

    policy_revision = tx.tenant(cluster, tenant.name).policy_revision
    approved_at: dict[str, datetime] = {}
    by_name = {_key(r)[1]: r for r in releases}

    def approve(number: int, release: dict[str, Any], revision: Revision) -> None:
        block = _approval_block(release) or {}
        approval = Approval(
            id=f"imported:{did}@{number}",
            deployment_id=did,
            revision=number,
            authority_digest=revision.authority_digest,
            base_authority_digest="",
            policy_revision=policy_revision,
            approved_by=block["approvedBy"],
            approved_at=_time(block["approvedAt"]),
            ticket=block.get("ticket"),
            origin="imported",
            evidence={"approval": copy.deepcopy(block)},
        )
        tx.add_approval(approval)
        created.append(approval.id)

    # A revision that was imported unapproved and has since been approved in git.
    for revision in tx.revisions(did):
        release = tx.release(revision.release_id)
        document_of = by_name.get(release.name) if release else None
        block = _approval_block(document_of) if document_of else None
        if revision.approval_id is not None or block is None:
            continue
        try:
            _check_block(block)
        except (KeyError, ValueError, TypeError) as error:
            problems.append(f"AgentRelease {namespace}/{release.name}: approval: {error}")
            continue
        approve(revision.number, document_of, revision)
        tx.set_revision_progress(
            did,
            revision.number,
            phase="activated",
            approval_id=f"imported:{did}@{revision.number}",
            reason="",
            activated_at=_time(block["approvedAt"]),
        )

    taken = {r.release_id for r in tx.revisions(did)}
    pending = []
    for release in releases:
        record = derive.agent_release_record(release, recorded_at=now)
        if record.id in taken:
            continue
        block = _approval_block(release)
        if block is not None:
            try:
                _check_block(block)
            except (KeyError, ValueError, TypeError) as error:
                problems.append(f"AgentRelease {namespace}/{record.name}: approval: {error}")
                continue
            approved_at[record.name] = _time(block["approvedAt"])
        stored = tx.release(record.id)
        if stored is not None and stored.model_dump(exclude={"recorded_at"}) != record.model_dump(
            exclude={"recorded_at"}
        ):
            problems.append(
                f"AgentRelease {namespace}/{record.name}: stored with a different document"
            )
            continue
        pending.append((block, record, release, stored))
    pending.sort(
        key=lambda p: (p[0] is None, approved_at.get(p[1].name, datetime.min.replace(tzinfo=UTC)))
        + (p[1].name,)
    )

    authority = canonical.authority_of(document)
    for block, record, release, stored in pending:
        if stored is None:
            tx.add_release(record)
            created.append(record.id)
        created.extend(record_skills(tx, record))
        requirements = derive.requirements_of(
            record.id, document, record.document["spec"].get("skills")
        )
        tx.add_requirements(record.id, requirements)
        number = len(tx.revisions(did)) + 1
        revision = Revision(
            id=revision_id(did, number),
            deployment_id=did,
            number=number,
            release_id=record.id,
            origin="imported",
            agent_document=document,
            authority=authority,
            authority_digest=canonical.authority_digest(authority),
            policy_revision=policy_revision,
            created_at=now,
            phase="awaiting-runtime" if block is None else "activated",
            reason="imported unapproved" if block is None else "",
            activated_at=None if block is None else approved_at[record.name],
        )
        tx.add_revision(revision)
        created.append(revision.id)
        tx.add_resolutions(
            revision.id, derive.resolutions_of(tx, cluster, did, number, requirements)
        )
        if block is not None:
            approve(number, release, revision)
            tx.set_revision_progress(did, number, approval_id=f"imported:{did}@{number}")

    deployment = tx.deployment(did)
    if deployment.pinned_revision is None:
        counted = [r for r in tx.revisions(did) if r.approval_id is not None]
        if with_status:
            counted = [r for r in counted if _ready(tx, by_name, r)]
        if counted:
            number = max(r.number for r in counted)
            if number != deployment.current_revision:
                deployment = deployment.model_copy(update={"current_revision": number})
                tx.put_deployment(deployment)
    if have is not None and have != deployment:
        updated.append(did)
    return None


def _ready(tx: Tx, by_name: dict[str, dict[str, Any]], revision: Revision) -> bool:
    release = tx.release(revision.release_id)
    document = by_name.get(release.name) if release else None
    return document is not None and derive.condition(document, "Ready").get("status") == "True"
