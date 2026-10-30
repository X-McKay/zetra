from __future__ import annotations

import hashlib

import pytest

from agent_platform import naming

SESSION = "6f1d2c3e-4b5a-4c6d-8e7f-9a0b1c2d3e4f"


def test_sandbox_name():
    digest = hashlib.sha256(SESSION.encode()).hexdigest()[:16]
    assert naming.sandbox_name(SESSION, 1) == f"s-{digest}-g1"
    with pytest.raises(ValueError):
        naming.sandbox_name(SESSION, 0)


def test_identifiers():
    assert naming.workflow_id("tenant-a", "investigator", SESSION) == (
        f"session:tenant-a:investigator:{SESSION}"
    )
    assert naming.task_queue("tenant-a") == "sessions-tenant-a"
    assert naming.client_id("tenant-a", "investigator") == "agent-tenant-a-investigator"
    assert naming.spiffe_id("tenant-a", "investigator") == (
        "spiffe://cluster.local/ns/tenant-a/sa/agent-investigator"
    )
    assert naming.expected_subject("tenant-a", "investigator") == (
        "system:serviceaccount:tenant-a:agent-investigator"
    )
    assert naming.realm_role("kubernetes") == "agent-role-kubernetes-read"
    assert naming.delegation_scope("tenant-a", "x") == "delegation:client:agent-tenant-a-x"


@pytest.mark.parametrize(
    "ns,ok",
    [
        ("tenant-a", True),
        ("tenant-" + "a" * 50, True),
        ("tenant-" + "a" * 51, False),
        ("tenant-", False),
        ("tenant-A", False),
        ("default", False),
        ("xtenant-a", False),
    ],
)
def test_tenant_regex(ns, ok):
    assert naming.is_tenant(ns) is ok


def test_session_id_must_be_lower_case_uuid4():
    naming.require_session_id(SESSION)
    for bad in (SESSION.upper(), "not-a-uuid", "6f1d2c3e-4b5a-1c6d-8e7f-9a0b1c2d3e4f"):
        with pytest.raises(ValueError):
            naming.require_session_id(bad)
