"""The catalog feed (`contract/import.md` sections 1 and 2)."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_registry import canonical, derive
from agent_registry.models import (
    Alias,
    Cluster,
    Deployment,
    Item,
    Release,
    Revocation,
    Tenant,
    TrustedRepository,
    deployment_id,
    release_id,
    release_slug,
)
from agent_registry.rules import revocation as revocations
from agent_registry.store import Store, Tx

VERSION = 1

_KEYS: dict[str, tuple[str, ...]] = {
    "clusters": ("name", "trustDomain", "gateway", "fluxInventories"),
    "tenants": ("cluster", "name", "namespace", "policy"),
    "trustedRepositories": ("repository", "cluster", "tenant", "issuer", "subjectRegExp", "agents"),
    "toolServers": (
        "name",
        "cluster",
        "namespace",
        "owner",
        "description",
        "source",
        "workload",
        "image",
        "pinnedByDigest",
        "registered",
        "backend",
    ),
    "modelEngines": (
        "name",
        "cluster",
        "namespace",
        "owner",
        "description",
        "source",
        "workload",
        "servedName",
        "servedNameFrom",
        "backend",
    ),
    "aliases": ("cluster", "alias", "engine", "backend"),
}

_REVOCATION_KEYS = (
    "schemaVersion",
    "id",
    "kind",
    "item",
    "release",
    "reason",
    "sessions",
    "summary",
    "revokedBy",
    "revokedAt",
)
_REVOCATION_REQUIRED = tuple(k for k in _REVOCATION_KEYS if k not in ("sessions", "summary"))
_REVOCATION_KINDS = ("agent", "tool-server", "model")


def load_feed(path: Path) -> dict[str, Any]:
    """Read and check a feed file. `ValueError` when it is not a valid feed."""
    try:
        feed = json.loads(Path(path).read_text())
    except json.JSONDecodeError as error:
        raise ValueError(f"{path}: not JSON: {error}") from error
    validate_feed(feed)
    return feed


def validate_feed(feed: Any) -> None:
    if not isinstance(feed, dict):
        raise ValueError("the feed is not a JSON object")
    if feed.get("version") != VERSION:
        raise ValueError(f"feed version {feed.get('version')!r} is not supported; want {VERSION}")
    for section, keys in _KEYS.items():
        entries = feed.get(section, [])
        if not isinstance(entries, list):
            raise ValueError(f"{section} is not a list")
        for position, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise ValueError(f"{section}[{position}] is not an object")
            missing = [key for key in keys if key not in entry]
            if missing:
                raise ValueError(f"{section}[{position}] lacks {', '.join(missing)}")
    clusters = {e["name"] for e in feed.get("clusters", [])}
    tenants = {(e["cluster"], e["name"]) for e in feed.get("tenants", [])}
    engines = {(e["cluster"], e["name"]) for e in feed.get("modelEngines", [])}
    for section in ("tenants", "trustedRepositories", "toolServers", "modelEngines", "aliases"):
        for entry in feed.get(section, []):
            if entry["cluster"] not in clusters:
                raise ValueError(f"{section}: cluster {entry['cluster']!r} is not in clusters")
    for entry in feed.get("trustedRepositories", []):
        if (entry["cluster"], entry["tenant"]) not in tenants:
            raise ValueError(
                f"trustedRepositories: tenant {entry['tenant']!r} is not in tenants "
                f"of cluster {entry['cluster']!r}"
            )
    for entry in feed.get("aliases", []):
        if (entry["cluster"], entry["engine"]) not in engines:
            raise ValueError(
                f"aliases: engine {entry['engine']!r} is not in modelEngines "
                f"of cluster {entry['cluster']!r}"
            )
    _validate_revocations(feed.get("revocations", []))


def _revocation_time(text: Any, where: str) -> datetime:
    try:
        at = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{where}: revokedAt is not a time: {text!r}") from error
    return at if at.tzinfo else at.replace(tzinfo=UTC)


def _validate_revocations(entries: Any) -> None:
    """Section 8.1: every field, and ids unique and in id order."""
    if not isinstance(entries, list):
        raise ValueError("revocations is not a list")
    seen: list[str] = []
    for position, entry in enumerate(entries):
        where = f"revocations[{position}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{where} is not an object")
        missing = [key for key in _REVOCATION_REQUIRED if key not in entry]
        if missing:
            raise ValueError(f"{where} lacks {', '.join(missing)}")
        extra = sorted(set(entry) - set(_REVOCATION_KEYS))
        if extra:
            raise ValueError(f"{where} has unknown keys {', '.join(extra)}")
        if entry["schemaVersion"] != 1:
            raise ValueError(f"{where}: schemaVersion {entry['schemaVersion']!r} is not 1")
        for key in ("id", "item", "release", "revokedBy"):
            if not isinstance(entry[key], str) or not entry[key]:
                raise ValueError(f"{where}: {key} is not a non-empty string")
        if entry["kind"] not in _REVOCATION_KINDS:
            raise ValueError(
                f"{where}: kind {entry['kind']!r} is not one of agent, tool-server, model"
            )
        if entry["reason"] not in ("security", "quality"):
            raise ValueError(f"{where}: reason {entry['reason']!r} is not security or quality")
        if entry.get("sessions", "cancel") not in ("cancel", "finish"):
            raise ValueError(f"{where}: sessions {entry['sessions']!r} is not cancel or finish")
        if not isinstance(entry.get("summary", ""), str):
            raise ValueError(f"{where}: summary is not a string")
        _revocation_time(entry["revokedAt"], where)
        seen.append(entry["id"])
    if len(set(seen)) != len(seen):
        raise ValueError("revocations: ids are not unique")
    if seen != sorted(seen):
        raise ValueError("revocations: not in id order")


def _revocation_of(entry: dict[str, Any], now: datetime) -> Revocation:
    return Revocation(
        id=entry["id"],
        kind=entry["kind"],
        item=entry["item"],
        release=entry["release"],
        reason=entry["reason"],
        sessions=entry.get("sessions") or ("cancel" if entry["reason"] == "security" else "finish"),
        summary=entry.get("summary", ""),
        revoked_by=entry["revokedBy"],
        revoked_at=_revocation_time(entry["revokedAt"], entry["id"]),
        indexed_at=now,
    )


def _image_release_name(image: str) -> str:
    """The tag of an image reference, else its digest, as a release name."""
    bare = image.split("@", 1)[0]
    colon = bare.rfind(":")
    if colon > bare.rfind("/"):
        return release_slug(bare[colon + 1 :])
    return release_slug(derive.image_digest(image) or "latest")


def apply_feed(
    store: Store, feed: dict[str, Any], *, now: datetime | None = None, actor: str = "feed"
) -> bool:
    """Write the feed's clusters, tenants, trusted repositories, tool servers, model engines
    and aliases. Returns True when anything changed."""
    validate_feed(feed)
    now = now or datetime.now(UTC)
    changed: list[str] = []
    with store.transaction(actor) as tx:
        for entry in feed.get("clusters", []):
            cluster = Cluster(
                name=entry["name"],
                trust_domain=entry["trustDomain"],
                gateway=entry["gateway"],
                flux_inventories=entry["fluxInventories"],
            )
            if tx.cluster(cluster.name) != cluster:
                tx.put_cluster(cluster)
                changed.append(f"cluster:{cluster.name}")
        for entry in feed.get("tenants", []):
            tenant = Tenant(
                cluster=entry["cluster"],
                name=entry["name"],
                namespace=entry["namespace"],
                policy_revision=canonical.digest(entry),
                document=copy.deepcopy(entry),
            )
            if tx.tenant(tenant.cluster, tenant.name) != tenant:
                tx.put_tenant(tenant)
                changed.append(f"tenant:{tenant.cluster}/{tenant.name}")
        have_repositories = {r.repository: r for r in tx.trusted_repositories()}
        for entry in feed.get("trustedRepositories", []):
            repository = TrustedRepository(
                repository=entry["repository"],
                cluster=entry["cluster"],
                tenant=entry["tenant"],
                issuer=entry["issuer"],
                subject_reg_exp=entry["subjectRegExp"],
                agents=entry["agents"],
            )
            if have_repositories.get(repository.repository) != repository:
                tx.put_trusted_repository(repository)
                changed.append(f"trusted-repository:{repository.repository}")
        for entry in feed.get("toolServers", []):
            name = _image_release_name(entry["image"])
            adopted = Release(
                id=release_id("tool-server", entry["name"], name),
                kind="tool-server",
                item=entry["name"],
                name=name,
                digest=derive.image_digest(entry["image"]),
                origin="adopted",
                lineage={"source": entry["source"]},
                document={"image": entry["image"]},
                recorded_at=now,
            )
            document = {
                key: copy.deepcopy(entry[key])
                for key in ("namespace", "workload", "image", "pinnedByDigest", "registered")
            } | {"backend": entry["backend"]}
            _infrastructure(tx, changed, "tool-server", entry, document, adopted)
        for entry in feed.get("modelEngines", []):
            name = release_slug(entry["servedName"])
            adopted = Release(
                id=release_id("model", entry["name"], name),
                kind="model",
                item=entry["name"],
                name=name,
                digest="",
                origin="adopted",
                document={"servedName": entry["servedName"]},
                recorded_at=now,
            )
            document = {
                key: copy.deepcopy(entry[key])
                for key in ("namespace", "workload", "servedName", "servedNameFrom", "backend")
            }
            _infrastructure(tx, changed, "model", entry, document, adopted)
        have_aliases = {(a.cluster, a.tenant, a.alias): a for a in tx.aliases()}
        for entry in feed.get("aliases", []):
            alias = Alias(
                cluster=entry["cluster"],
                tenant=None,
                alias=entry["alias"],
                deployment_id=deployment_id("model", entry["cluster"], None, entry["engine"]),
                backend=entry["backend"],
            )
            if have_aliases.get((alias.cluster, None, alias.alias)) != alias:
                tx.put_alias(alias)
                changed.append(f"alias:{alias.cluster}/{alias.alias}")
        for kind, section in (("tool-server", "toolServers"), ("model", "modelEngines")):
            named = {entry["name"] for entry in feed.get(section, [])}
            for item in tx.items(kind):
                if item.name not in named and not item.retired:
                    tx.put_item(item.model_copy(update={"retired": True}))
                    changed.append(f"{kind}:{item.name}")
        for entry in feed.get("revocations", []):
            revocation = _revocation_of(entry, now)
            stored = tx.revocation(revocation.id)
            if stored is None:
                tx.add_revocation(revocation)
                revocations.apply(tx, revocation, now=now)
                changed.append(f"revocation:{revocation.id}")
            elif stored.model_copy(update={"indexed_at": now}) != revocation:
                raise ValueError(
                    f"revocations: {revocation.id} is stored differently; a revocation is "
                    "never edited"
                )
        if changed:
            tx.event("feed-applied", detail={"changed": changed})
    return bool(changed)


def _infrastructure(
    tx: Tx,
    changed: list[str],
    kind: str,
    entry: dict[str, Any],
    document: dict[str, Any],
    adopted: Release,
) -> None:
    """A tool server or model engine: its item, deployment and adopted release."""
    name = entry["name"]
    item = Item(
        kind=kind,
        name=name,
        owner=entry["owner"],
        source=entry["source"],
        description=entry["description"],
    )
    did = deployment_id(kind, entry["cluster"], None, name)
    have = tx.deployment(did)
    deployment = (
        have
        if have is not None
        else Deployment(
            id=did,
            kind=kind,
            item=name,
            cluster=entry["cluster"],
            namespace=entry["namespace"],
        )
    ).model_copy(
        update={"namespace": entry["namespace"], "authority": "pull-request", "document": document}
    )
    touched = False
    if tx.item(kind, name) != item:
        tx.put_item(item)
        touched = True
    if have != deployment:
        tx.put_deployment(deployment)
        touched = True
    if tx.release(adopted.id) is None:
        tx.add_release(adopted)
        touched = True
    if touched:
        changed.append(f"{kind}:{name}")
