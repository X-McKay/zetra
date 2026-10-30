from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from netpol_attestor import main as attestor
from netpol_attestor.parse import (
    UNCHANGED,
    MatchConfig,
    Verdict,
    attest,
    decide,
    parse_filter_table,
    parse_rule,
)

TABLE = (Path(__file__).parent / "fixtures" / "kube-router-filter.txt").read_text()
CFG = MatchConfig()
GOOD_CHAIN = "KUBE-POD-FW-4UFKVO2M3QBH7CNO"
NOW = datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)


def test_parser_reads_only_the_filter_table():
    chains = parse_filter_table(TABLE)
    assert "KUBE-SERVICES" not in chains
    assert len(chains[GOOD_CHAIN]) == 10
    assert chains["KUBE-NWPLCY-N6WQEXRFPUYZEN4F"] == []


def test_rule_fields():
    rule = parse_rule(
        '-A KUBE-POD-FW-X -s 10.42.1.23/32 -m comment --comment "run through nw policy a b" '
        "-m set --match-set KUBE-SRC-A src -m mark ! --mark 0x10000/0x10000 -j KUBE-NWPLCY-Y"
    )
    assert (rule.source, rule.target, rule.comment) == (
        "10.42.1.23",
        "KUBE-NWPLCY-Y",
        "run through nw policy a b",
    )
    assert rule.match_sets == ("KUBE-SRC-A",)


def test_attested_pod_chain():
    assert attest(TABLE, "10.42.1.23", CFG) == Verdict(GOOD_CHAIN, "ok")


def test_chain_without_reject_is_not_attested():
    # kube-router has created the chain but not yet programmed its final reject rule.
    assert attest(TABLE, "10.42.1.24", CFG).reason == "no_final_reject"


def test_chain_without_egress_policy_ipset_is_not_attested():
    assert attest(TABLE, "10.42.1.230", CFG).reason == "no_egress_policy_ipset"


def test_ip_match_is_exact():
    assert attest(TABLE, "10.42.1.2", CFG).reason == "no_chain"
    assert attest(TABLE, "10.42.1.99", CFG).reason == "no_chain"


def test_nat_table_jumps_are_ignored():
    filter_only = TABLE.split("*filter", 1)[1]
    assert attest("*filter" + filter_only, "10.42.1.23", CFG) == attest(TABLE, "10.42.1.23", CFG)


def test_accept_after_reject_fails():
    table = TABLE.replace(
        f"-A {GOOD_CHAIN} -j MARK --set-xmark 0x0/0x10000",
        f"-A {GOOD_CHAIN} -j MARK --set-xmark 0x0/0x10000\n-A {GOOD_CHAIN} -j ACCEPT",
    )
    assert attest(table, "10.42.1.23", CFG).reason == "no_final_reject"


def test_stale_chain_for_recycled_ip_is_ambiguous():
    extra = (
        ":KUBE-POD-FW-STALESTALESTALE - [0:0]\n"
        "-A KUBE-POD-FW-STALESTALESTALE -d 10.42.1.23/32 -j ACCEPT\nCOMMIT"
    )
    table = TABLE.replace("COMMIT\n# Completed on Thu Oct 30 12:00:00 2026\n", "", 1)
    table = table[::-1].replace("TIMMOC", extra[::-1], 1)[::-1]
    assert attest(table, "10.42.1.23", CFG).reason == "ambiguous_chain"


def test_chain_naming_is_configurable():
    renamed = TABLE.replace("KUBE-POD-FW-", "KUBE-POD-FIREWALL-").replace("KUBE-NWPLCY-", "KR-NP-")
    assert attest(renamed, "10.42.1.23", CFG).reason == "no_chain"
    cfg = MatchConfig.from_env(
        {
            "ATTESTOR_POD_CHAIN_PREFIX": "KUBE-POD-FIREWALL-",
            "ATTESTOR_POLICY_CHAIN_PREFIX": "KR-NP-",
        }
    )
    assert attest(renamed, "10.42.1.23", cfg) == Verdict(GOOD_CHAIN.replace("FW", "FIREWALL"), "ok")


def test_policy_comment_requirement_is_configurable():
    uncommented = re.sub(r"sandbox-egress-gateway-only", "pol-1", TABLE)
    assert attest(uncommented, "10.42.1.23", CFG).reason == "no_egress_policy_ipset"
    relaxed = MatchConfig(require_policy_comment=False)
    assert attest(uncommented, "10.42.1.23", relaxed).ok


# ---- annotation decisions ------------------------------------------------------------

OK = Verdict(GOOD_CHAIN, "ok")


def test_decide_sets_refreshes_and_removes():
    value = f"2026-10-30T12:00:00Z;{GOOD_CHAIN}"
    assert decide(None, OK, NOW, 30) == value
    assert decide(value, OK, NOW + timedelta(seconds=29), 30) is UNCHANGED
    assert (
        decide(value, OK, NOW + timedelta(seconds=30), 30) == f"2026-10-30T12:00:30Z;{GOOD_CHAIN}"
    )
    assert decide(value, Verdict("KUBE-POD-FW-OTHER", "ok"), NOW, 30).endswith(";KUBE-POD-FW-OTHER")
    assert decide(value, Verdict(None, "no_chain"), NOW, 30) is None
    assert decide(None, Verdict(None, "no_chain"), NOW, 30) is UNCHANGED
    assert decide("garbage", OK, NOW, 30) == value
    # A future timestamp (clock skew or forgery) is rewritten, not trusted.
    assert decide(f"2027-01-01T00:00:00Z;{GOOD_CHAIN}", OK, NOW, 30) == value


def test_annotation_format_is_rfc3339_then_chain():
    value = decide(None, OK, NOW, 30)
    stamp, chain = value.split(";")
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", stamp) and chain == GOOD_CHAIN


# ---- one pass against a fake API -----------------------------------------------------


def _pod(name, ip, ns="tenant-a", annotations=None, phase="Running", deleting=False):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name,
            namespace=ns,
            annotations=annotations,
            deletion_timestamp=NOW if deleting else None,
        ),
        status=SimpleNamespace(pod_ip=ip, phase=phase),
    )


class FakeCore:
    def __init__(self, pods):
        self.pods = pods
        self.patches = []

    def list_pod_for_all_namespaces(self, field_selector, label_selector):
        assert field_selector == "spec.nodeName=node-1"
        assert label_selector == "agentplatform.io/sandbox=true"
        return SimpleNamespace(items=self.pods)

    def patch_namespaced_pod(self, name, namespace, body):
        self.patches.append((namespace, name, body["metadata"]["annotations"][attestor.ANNOTATION]))


def test_run_pass(monkeypatch):
    stale = {attestor.ANNOTATION: f"2026-01-01T00:00:00Z;{GOOD_CHAIN}"}
    core = FakeCore(
        [
            _pod("good", "10.42.1.23"),
            _pod("no-reject", "10.42.1.24", annotations=stale),
            _pod("pending", None),
            _pod("system", "10.42.1.23", ns="kube-system"),
            _pod("done", "10.42.1.230", annotations=stale, phase="Succeeded"),
        ]
    )
    monkeypatch.setattr(attestor, "read_filter_table", lambda cmd: TABLE)
    attestor.run_pass(core, "node-1", "iptables-save", CFG, 30)
    assert [(n, v is not None and v.endswith(GOOD_CHAIN)) for _, n, v in core.patches] == [
        ("good", True),
        ("no-reject", False),
        ("done", False),
    ]
    assert core.patches[1][2] is None and core.patches[2][2] is None


def test_unreadable_table_withdraws_attestations(monkeypatch):
    def broken(cmd):
        raise FileNotFoundError(cmd)

    core = FakeCore([_pod("good", "10.42.1.23", annotations={attestor.ANNOTATION: "x;y"})])
    monkeypatch.setattr(attestor, "read_filter_table", broken)
    attestor.run_pass(core, "node-1", "iptables-save", CFG, 30)
    assert core.patches == [("tenant-a", "good", None)]


@pytest.fixture(autouse=True)
def no_tokens_in_logs(caplog):
    yield
    assert not re.search(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}", caplog.text)
    assert not re.search(r"Bearer [A-Za-z0-9._~+/=-]{20,}", caplog.text)
