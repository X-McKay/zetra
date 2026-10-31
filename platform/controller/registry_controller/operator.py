"""kopf wiring: events and a resync timer call the Reconciler; nothing else lives here."""

from __future__ import annotations

import logging
from pathlib import Path

import kopf

from agent_platform import naming
from agent_platform.constants import API_GROUP, API_VERSION, LABEL_TENANT
from registry_controller.config import ControllerConfig
from registry_controller.idp import KeycloakAdmin
from registry_controller.kube import ApiServer
from registry_controller.reconciler import Reconciler
from registry_controller.signature import CosignVerifier, KeylessVerifier, SignatureVerifier

log = logging.getLogger(__name__)
CFG = ControllerConfig.from_env()
_reconciler: Reconciler | None = None


def reconciler() -> Reconciler:
    global _reconciler
    if _reconciler is None:
        idp = KeycloakAdmin(
            base_url=CFG.keycloak_url,
            realm=CFG.keycloak_realm,
            client_id=CFG.keycloak_client_id,
            client_secret=Path(CFG.keycloak_secret_file).read_text().strip(),
            idp_alias=CFG.idp_alias,
            template=CFG.client_template(),
        )
        verifier: SignatureVerifier
        if CFG.signature_profile == "keyless":
            verifier = KeylessVerifier(CFG.trusted_root, rekor_url=CFG.rekor_url)
        else:
            verifier = CosignVerifier(CFG.cosign_key, profile=CFG.signature_profile)
        _reconciler = Reconciler(ApiServer(), idp, verifier, CFG)
    return _reconciler


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_) -> None:
    # The CRD status schemas do not preserve unknown fields; keep kopf's own state in
    # annotations so it is never pruned and never mixed into the status of 6.1/6.2.
    settings.persistence.progress_storage = kopf.AnnotationsProgressStorage(prefix=API_GROUP)
    settings.persistence.diffbase_storage = kopf.AnnotationsDiffBaseStorage(
        prefix=API_GROUP, key="last-handled-configuration"
    )
    # One replica with a Recreate strategy; kopf peering is not used (standalone).
    settings.posting.enabled = False


# -- Agent --------------------------------------------------------------------------


@kopf.on.resume(API_GROUP, API_VERSION, "agents")
@kopf.on.create(API_GROUP, API_VERSION, "agents")
@kopf.on.update(API_GROUP, API_VERSION, "agents")
def agent_changed(body, **_) -> None:
    reconciler().reconcile_agent(dict(body))


@kopf.timer(API_GROUP, API_VERSION, "agents", interval=CFG.resync_seconds, idle=5)
def agent_resync(body, **_) -> None:
    reconciler().reconcile_agent(dict(body))


@kopf.on.delete(API_GROUP, API_VERSION, "agents")
def agent_deleted(body, **_) -> None:
    reconciler().delete_agent(dict(body))


# -- AgentRelease -------------------------------------------------------------------


@kopf.on.resume(API_GROUP, API_VERSION, "agentreleases")
@kopf.on.create(API_GROUP, API_VERSION, "agentreleases")
@kopf.on.update(API_GROUP, API_VERSION, "agentreleases")
def release_changed(body, **_) -> None:
    reconciler().reconcile_release(dict(body))


@kopf.timer(API_GROUP, API_VERSION, "agentreleases", interval=CFG.resync_seconds, idle=5)
def release_resync(body, **_) -> None:
    reconciler().reconcile_release(dict(body))


@kopf.on.delete(API_GROUP, API_VERSION, "agentreleases", optional=True)
def release_deleted(**_) -> None:
    reconciler().regenerate_ratelimit()


# -- drift on owned namespace objects (T28: reverted well within 60 s) ----------------


@kopf.on.event("networking.k8s.io", "v1", "networkpolicies")
def netpol_event(namespace, **_) -> None:
    if namespace and naming.is_tenant(namespace) and reconciler().agents_in(namespace):
        reconciler().reconcile_tenant(namespace)


@kopf.on.event("v1", "namespaces", labels={LABEL_TENANT: kopf.PRESENT})
def namespace_event(name, type, **_) -> None:
    if type != "DELETED" and naming.is_tenant(name) and reconciler().agents_in(name):
        reconciler().reconcile_tenant(name)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    kopf.run(clusterwide=True, standalone=True, liveness_endpoint="http://0.0.0.0:8080/healthz")
