"""Pure parsing and verification of kube-router's per-pod firewall chains (5.5).

kube-router's chain naming and comment format is a to-confirm item (17.3), so every
pattern this module relies on is a field of `MatchConfig` and can be set per cluster
from the environment without a code change.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from datetime import datetime

VERDICTS = frozenset({"ACCEPT", "DROP", "REJECT"})


@dataclass(frozen=True)
class MatchConfig:
    pod_chain_prefix: str = "KUBE-POD-FW-"
    policy_chain_prefix: str = "KUBE-NWPLCY-"
    reject_targets: tuple[str, ...] = ("REJECT",)
    egress_policy_name: str = "sandbox-egress-gateway-only"
    # When true, the egress policy must be named in a comment on the jump rule or in the
    # policy chain; kube-router writes "run through nw policy <name>" on the jump today.
    require_policy_comment: bool = True

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> MatchConfig:
        e = os.environ if env is None else env
        d = cls()
        return cls(
            pod_chain_prefix=e.get("ATTESTOR_POD_CHAIN_PREFIX", d.pod_chain_prefix),
            policy_chain_prefix=e.get("ATTESTOR_POLICY_CHAIN_PREFIX", d.policy_chain_prefix),
            reject_targets=tuple(
                e.get("ATTESTOR_REJECT_TARGETS", ",".join(d.reject_targets)).split(",")
            ),
            egress_policy_name=e.get("ATTESTOR_EGRESS_POLICY", d.egress_policy_name),
            require_policy_comment=e.get("ATTESTOR_REQUIRE_POLICY_COMMENT", "true") == "true",
        )


@dataclass(frozen=True)
class Rule:
    chain: str
    source: str | None = None
    destination: str | None = None
    target: str | None = None
    comment: str = ""
    match_sets: tuple[str, ...] = ()
    raw: str = field(default="", compare=False)


def _addr(value: str) -> str:
    for suffix in ("/32", "/128"):
        if value.endswith(suffix):
            return value[: -len(suffix)]
    return value


def parse_rule(line: str) -> Rule:
    tokens = shlex.split(line)
    assert tokens[0] == "-A"
    chain, kw = tokens[1], {}
    sets: list[str] = []
    negate = False
    i = 2
    while i < len(tokens):
        tok = tokens[i]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        if tok == "!":
            negate = True
            i += 1
            continue
        if tok in ("-s", "--source") and nxt and not negate:
            kw["source"] = _addr(nxt)
        elif tok in ("-d", "--destination") and nxt and not negate:
            kw["destination"] = _addr(nxt)
        elif tok in ("-j", "--jump", "-g", "--goto") and nxt:
            kw["target"] = nxt
        elif tok == "--comment" and nxt is not None:
            kw["comment"] = nxt
        elif tok == "--match-set" and nxt and not negate:
            sets.append(nxt)
        negate = False
        i += 1
    return Rule(chain=chain, match_sets=tuple(sets), raw=line, **kw)


def parse_filter_table(text: str) -> dict[str, list[Rule]]:
    """Chains of the `*filter` table of `iptables-save` output, rules in order."""
    chains: dict[str, list[Rule]] = {}
    in_filter = False
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("*"):
            in_filter = line == "*filter"
            continue
        if not in_filter:
            continue
        if line == "COMMIT":
            in_filter = False
        elif line.startswith(":"):
            chains.setdefault(line[1:].split()[0], [])
        elif line.startswith("-A "):
            rule = parse_rule(line)
            chains.setdefault(rule.chain, []).append(rule)
    return chains


@dataclass(frozen=True)
class Verdict:
    chain: str | None
    reason: str

    @property
    def ok(self) -> bool:
        return self.reason == "ok"


def pod_chains(chains: dict[str, list[Rule]], ip: str, cfg: MatchConfig) -> list[str]:
    """Per-pod chains that reference `ip`, in their own rules or in a jump into them."""
    found = set()
    for name, rules in chains.items():
        for rule in rules:
            refs_ip = ip in (rule.source, rule.destination)
            if not refs_ip:
                continue
            if name.startswith(cfg.pod_chain_prefix):
                found.add(name)
            if (
                rule.target
                and rule.target.startswith(cfg.pod_chain_prefix)
                and rule.target in chains
            ):
                found.add(rule.target)
    return sorted(found)


def verify_chain(chains: dict[str, list[Rule]], chain: str, cfg: MatchConfig) -> Verdict:
    rules = chains.get(chain, [])
    verdicts = [r for r in rules if r.target in VERDICTS or r.target in cfg.reject_targets]
    if not verdicts or verdicts[-1].target not in cfg.reject_targets:
        return Verdict(chain, "no_final_reject")
    for rule in rules:
        if not (rule.target and rule.target.startswith(cfg.policy_chain_prefix)):
            continue
        policy_rules = chains.get(rule.target, [])
        named = cfg.egress_policy_name in rule.comment or any(
            cfg.egress_policy_name in r.comment for r in policy_rules
        )
        if (named or not cfg.require_policy_comment) and any(r.match_sets for r in policy_rules):
            return Verdict(chain, "ok")
    return Verdict(chain, "no_egress_policy_ipset")


def attest(iptables_save: str, ip: str, cfg: MatchConfig) -> Verdict:
    chains = parse_filter_table(iptables_save)
    candidates = pod_chains(chains, ip, cfg)
    if not candidates:
        return Verdict(None, "no_chain")
    if len(candidates) > 1:
        # A stale chain for a recycled IP is indistinguishable from the pod's own; fail
        # closed until kube-router's next sync removes it.
        return Verdict(None, "ambiguous_chain")
    return verify_chain(chains, candidates[0], cfg)


# ---- annotation ----------------------------------------------------------------------

UNCHANGED = object()


def annotation_value(now: datetime, chain: str) -> str:
    return f"{now.strftime('%Y-%m-%dT%H:%M:%SZ')};{chain}"


def parse_annotation(value: str | None) -> tuple[datetime, str] | None:
    if not value or ";" not in value:
        return None
    stamp, chain = value.split(";", 1)
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"), chain
    except ValueError:
        return None


def decide(current: str | None, verdict: Verdict, now: datetime, refresh_seconds: float):
    """New annotation value, None to remove it, or UNCHANGED."""
    if not verdict.ok:
        return None if current is not None else UNCHANGED
    parsed = parse_annotation(current)
    if parsed is not None:
        stamp, chain = parsed
        age = (now.replace(tzinfo=None) - stamp).total_seconds()
        if chain == verdict.chain and 0 <= age < refresh_seconds:
            return UNCHANGED
    return annotation_value(now, verdict.chain)
