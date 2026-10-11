from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import date
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from agentctl.diagnostics import Diagnostic, emit_diagnostics, relative_display
from agentctl.files import DataFileError, load_data
from agentctl.risk import TIER_RANK, matrix_tier
from agentctl.validation import EXECUTION_RANK, _validate_spec

SUPPORTED_SUFFIXES = {".json", ".yaml", ".yml"}


def system_schema_path() -> Path:
    return Path(str(files("agentctl").joinpath("schemas/system-spec.schema.json")))


@lru_cache(maxsize=1)
def load_system_schema() -> dict[str, Any]:
    schema = json.loads(system_schema_path().read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return schema


def validate_systems(
    *, root: Path, paths: Sequence[Path], output_format: str = "text"
) -> int:
    root = root.resolve()
    targets = _resolve_system_targets(root, paths)
    diagnostics: list[Diagnostic] = []
    if not targets:
        diagnostics.append(
            Diagnostic(
                "SYSTEM001",
                "no System Specs found under src/<package>/systems/<system>/system.yaml",
                str(root),
            )
        )
    for path in targets:
        diagnostics.extend(validate_system_file(path, root=root))
    return emit_diagnostics(
        diagnostics,
        output_format=output_format,
        heading="Multi-agent system contract validation",
    )


def validate_system_file(path: Path, *, root: Path) -> list[Diagnostic]:
    path = path if path.is_absolute() else root / path
    shown = relative_display(path, root)
    try:
        payload = load_data(path)
    except DataFileError as exc:
        return [Diagnostic("SYSTEM002", str(exc), shown)]

    validator = Draft202012Validator(
        load_system_schema(), format_checker=FormatChecker()
    )
    schema_errors = sorted(validator.iter_errors(payload), key=_schema_error_key)
    if schema_errors:
        return [
            Diagnostic(
                "SYSTEM003",
                f"{_json_path(error.absolute_path)}: {error.message}",
                shown,
            )
            for error in schema_errors
        ]
    return _system_semantic_diagnostics(payload, path=path, root=root)


def explain_system(*, root: Path, path: Path, output_format: str = "text") -> int:
    root = root.resolve()
    resolved = path if path.is_absolute() else root / path
    diagnostics = validate_system_file(resolved, root=root)
    if any(item.severity == "error" for item in diagnostics):
        return emit_diagnostics(
            diagnostics,
            output_format=output_format,
            heading="Multi-agent system contract",
        )
    payload = load_data(resolved)
    if output_format == "json":
        print(
            json.dumps(
                {
                    "ok": True,
                    "path": relative_display(resolved, root),
                    "system": payload,
                    "diagnostics": [asdict(item) for item in diagnostics],
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        metadata = payload["metadata"]
        topology = payload["topology"]
        print(f"System: {payload['name']} {metadata['version']}")
        print(f"Owner: {metadata['owner']}")
        print(
            f"Execution: {metadata['execution_class']} · "
            f"Governance: {metadata['governance_tier']}"
        )
        print(
            f"Topology: {topology['type']} · "
            f"Members: {len(payload['members'])} · "
            f"Interactions: {len(payload['interactions'])}"
        )
        print(f"Dynamic membership: {topology['dynamic_membership']}")
        print(f"Source: {relative_display(resolved, root)}")
        if diagnostics:
            emit_diagnostics(
                diagnostics,
                output_format="text",
                heading="System notices",
            )
    return 0


def graph_system(*, root: Path, path: Path, output_format: str = "text") -> int:
    root = root.resolve()
    resolved = path if path.is_absolute() else root / path
    diagnostics = validate_system_file(resolved, root=root)
    if any(item.severity == "error" for item in diagnostics):
        return emit_diagnostics(
            diagnostics,
            output_format="json" if output_format == "json" else "text",
            heading="Multi-agent system graph",
        )
    payload = load_data(resolved)
    graph = _graph_payload(payload)
    if output_format == "json":
        print(json.dumps(graph, indent=2, sort_keys=True))
    elif output_format == "dot":
        print(f'digraph "{payload["name"]}" {{')
        for member in graph["members"]:
            print(
                f'  "{member["agent"]}" [label="{member["agent"]}\\n{member["role"]}"];'
            )
        orchestrator = graph["orchestrator"]
        print(f'  "{orchestrator}" [shape=box, style=rounded];')
        for edge in graph["interactions"]:
            print(
                f'  "{edge["from"]}" -> "{edge["to"]}" '
                f'[label="{edge["id"]} ({edge["mode"]})"];'
            )
        print("}")
    else:
        print(f"{payload['name']} [{graph['topology']}]")
        print(f"  orchestrator: {graph['orchestrator']}")
        for member in graph["members"]:
            required = "required" if member["required"] else "optional"
            print(
                f"  member: {member['agent']} {member['version']} "
                f"as {member['role']} ({required})"
            )
        for edge in graph["interactions"]:
            print(
                f"  edge: {edge['from']} -> {edge['to']} [{edge['mode']}; {edge['id']}]"
            )
    return 0


def validate_system_risk_file(path: Path, *, root: Path) -> list[Diagnostic]:
    path = path if path.is_absolute() else root / path
    shown = relative_display(path, root)
    try:
        payload = load_data(path)
    except DataFileError as exc:
        return [Diagnostic("SYSRISK002", str(exc), shown)]

    diagnostics: list[Diagnostic] = []
    required_mappings = (
        "assessment",
        "scope",
        "classification",
        "regulatory_screen",
        "acceptance",
        "monitoring",
    )
    for field in required_mappings:
        if not isinstance(payload.get(field), Mapping):
            diagnostics.append(
                Diagnostic("SYSRISK003", f"{field} must be a mapping", shown)
            )
    for field in ("member_assessments", "scenarios"):
        if not isinstance(payload.get(field), list) or not payload[field]:
            diagnostics.append(
                Diagnostic("SYSRISK004", f"{field} must be a non-empty list", shown)
            )
    if diagnostics:
        return diagnostics

    assessment = payload["assessment"]
    classification = payload["classification"]
    regulatory = payload["regulatory_screen"]
    for field in (
        "system",
        "system_version",
        "assessment_version",
        "status",
        "assessed_at",
        "review_by",
        "prepared_by",
    ):
        if assessment.get(field) in (None, "", [], {}):
            diagnostics.append(
                Diagnostic("SYSRISK005", f"assessment.{field} is required", shown)
            )
    tiers = ("low", "medium", "high", "critical")
    for field in (
        "governance_tier",
        "maximum_member_tier",
        "maximum_inherent_tier",
        "maximum_residual_tier",
    ):
        if classification.get(field) not in tiers:
            diagnostics.append(
                Diagnostic(
                    "SYSRISK006",
                    f"classification.{field} must be low, medium, high, or critical",
                    shown,
                )
            )
    if classification.get("decision") not in {"go", "conditional_go", "no_go"}:
        diagnostics.append(
            Diagnostic("SYSRISK007", "classification.decision is invalid", shown)
        )

    member_ids: set[tuple[Any, Any]] = set()
    member_tiers: list[str] = []
    for member in payload["member_assessments"]:
        if not isinstance(member, Mapping):
            diagnostics.append(
                Diagnostic("SYSRISK008", "member assessment must be a mapping", shown)
            )
            continue
        identity = (member.get("agent"), member.get("agent_version"))
        if identity in member_ids:
            diagnostics.append(
                Diagnostic(
                    "SYSRISK009",
                    f"duplicate member assessment {identity[0]!r} {identity[1]!r}",
                    shown,
                )
            )
        member_ids.add(identity)
        tier = member.get("governance_tier")
        if tier in TIER_RANK:
            member_tiers.append(tier)
        else:
            diagnostics.append(
                Diagnostic(
                    "SYSRISK010",
                    f"member {identity[0]!r} has invalid governance tier",
                    shown,
                )
            )
        reference = member.get("assessment")
        if not isinstance(reference, str) or not (root / reference).is_file():
            diagnostics.append(
                Diagnostic(
                    "SYSRISK011",
                    f"member {identity[0]!r} assessment does not exist",
                    shown,
                )
            )

    scenario_ids: set[str] = set()
    inherent_tiers: list[str] = []
    residual_tiers: list[str] = []
    for scenario in payload["scenarios"]:
        if not isinstance(scenario, Mapping):
            diagnostics.append(
                Diagnostic("SYSRISK012", "scenario must be a mapping", shown)
            )
            continue
        scenario_id = scenario.get("id")
        if not isinstance(scenario_id, str) or not scenario_id:
            diagnostics.append(
                Diagnostic("SYSRISK013", "scenario id is required", shown)
            )
            continue
        if scenario_id in scenario_ids:
            diagnostics.append(
                Diagnostic(
                    "SYSRISK014", f"duplicate scenario id {scenario_id!r}", shown
                )
            )
        scenario_ids.add(scenario_id)
        for phase in ("inherent", "residual"):
            score = scenario.get(phase)
            if not isinstance(score, Mapping):
                diagnostics.append(
                    Diagnostic(
                        "SYSRISK015",
                        f"scenario {scenario_id} {phase} must be a mapping",
                        shown,
                    )
                )
                continue
            impact = score.get("impact")
            likelihood = score.get("likelihood")
            tier = score.get("tier")
            if impact not in {1, 2, 3, 4} or likelihood not in {1, 2, 3, 4}:
                diagnostics.append(
                    Diagnostic(
                        "SYSRISK016",
                        f"scenario {scenario_id} {phase} scores must be 1 through 4",
                        shown,
                    )
                )
            elif tier != matrix_tier(impact, likelihood):
                diagnostics.append(
                    Diagnostic(
                        "SYSRISK017",
                        f"scenario {scenario_id} {phase} tier does not match matrix",
                        shown,
                    )
                )
            if tier in TIER_RANK:
                (inherent_tiers if phase == "inherent" else residual_tiers).append(tier)
        inherent = scenario.get("inherent", {})
        residual = scenario.get("residual", {})
        if (
            inherent.get("tier") in TIER_RANK
            and residual.get("tier") in TIER_RANK
            and TIER_RANK[residual["tier"]] < TIER_RANK[inherent["tier"]]
        ):
            verified = any(
                isinstance(control, Mapping)
                and control.get("effectiveness") == "verified"
                and isinstance(control.get("evidence"), list)
                and bool(control["evidence"])
                for control in scenario.get("controls", [])
            )
            if not verified:
                diagnostics.append(
                    Diagnostic(
                        "SYSRISK018",
                        f"scenario {scenario_id} reduces risk without verified evidence",
                        shown,
                    )
                )

    diagnostics.extend(
        _system_risk_summary_diagnostics(
            classification=classification,
            regulatory=regulatory,
            member_tiers=member_tiers,
            inherent_tiers=inherent_tiers,
            residual_tiers=residual_tiers,
            shown=shown,
        )
    )
    if assessment.get("status") != "approved":
        diagnostics.append(
            Diagnostic(
                "SYSRISK101",
                "assessment is not approved and cannot satisfy production readiness",
                shown,
                severity="warning",
            )
        )
    open_questions = payload.get("open_questions")
    if isinstance(open_questions, list) and open_questions:
        diagnostics.append(
            Diagnostic(
                "SYSRISK102",
                f"assessment has {len(open_questions)} open question(s)",
                shown,
                severity="warning",
            )
        )
    review_by = assessment.get("review_by")
    try:
        if isinstance(review_by, str) and date.fromisoformat(review_by) < date.today():
            diagnostics.append(
                Diagnostic("SYSRISK019", "assessment review date has expired", shown)
            )
    except ValueError:
        diagnostics.append(
            Diagnostic("SYSRISK020", "assessment.review_by must be an ISO date", shown)
        )
    return diagnostics


def _system_semantic_diagnostics(
    payload: Mapping[str, Any], *, path: Path, root: Path
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []

    def error(code: str, message: str, target: Path = path) -> None:
        diagnostics.append(Diagnostic(code, message, relative_display(target, root)))

    name = payload["name"]
    metadata = payload["metadata"]
    topology = payload["topology"]
    if name.replace("-", "_") != path.parent.name:
        error("SYSTEM004", "system directory must match the normalized system name")
    if "replace with" in payload["description"].lower():
        error("SYSTEM005", "replace the generated placeholder description")
    if (
        metadata["owner"] == "replace-me"
        or metadata["operational_owner"] == "replace-me"
    ):
        error("SYSTEM006", "metadata owners must name accountable owners")

    execution_class = metadata["execution_class"]
    if execution_class in {"durable", "human_governed"}:
        if metadata["temporal_enabled"] is not True:
            error("SYSTEM007", "durable execution requires temporal_enabled: true")
        package_dir = path.parents[2]
        for directory in (package_dir / "workflows", package_dir / "activities"):
            if not directory.is_dir():
                error(
                    "SYSTEM008", "durable execution requires this directory", directory
                )
    if execution_class == "human_governed":
        approval = metadata.get("approval_policy")
        if not isinstance(approval, str) or not (root / approval).is_file():
            error("SYSTEM009", "human-governed execution requires an approval policy")

    for field, code in (
        ("risk_assessment", "SYSTEM010"),
        ("threat_model", "SYSTEM011"),
        ("evaluation_policy", "SYSTEM012"),
    ):
        reference = metadata[field]
        if not (root / reference).is_file():
            error(code, f"metadata.{field} does not exist", root / reference)
    for reference, code, label in (
        (payload["state"]["artifact_policy"], "SYSTEM013", "artifact policy"),
        (payload["termination"]["policy"], "SYSTEM014", "termination policy"),
    ):
        target = _reference_path(root, path.parent, reference)
        if not target.is_file():
            error(code, f"{label} does not exist", target)
    if topology["dynamic_membership"]:
        for field, code in (
            ("registry", "SYSTEM015"),
            ("admission_policy", "SYSTEM016"),
        ):
            target = _reference_path(root, path.parent, topology[field])
            if not target.is_file():
                error(code, f"topology.{field} does not exist", target)

    members = payload["members"]
    member_names = [item["agent"] for item in members]
    if len(member_names) != len(set(member_names)):
        error("SYSTEM017", "members contain duplicate agent names")
    coordinator = topology.get("coordinator")
    if coordinator is not None and coordinator not in member_names:
        error("SYSTEM018", "topology.coordinator must name a declared member")

    resolved_members: dict[str, Mapping[str, Any]] = {}
    for member in members:
        candidates = sorted(root.glob("src/*/agents/*/agent.yaml"))
        matches: list[tuple[Path, Mapping[str, Any]]] = []
        for candidate in candidates:
            try:
                spec = load_data(candidate)
            except DataFileError:
                continue
            if spec.get("name") == member["agent"]:
                matches.append((candidate, spec))
        if not matches:
            error("SYSTEM019", f"member agent {member['agent']!r} was not found")
            continue
        if len(matches) > 1:
            error("SYSTEM020", f"member agent {member['agent']!r} is ambiguous")
            continue
        agent_path, spec = matches[0]
        diagnostics.extend(_validate_spec(agent_path, root))
        agent_metadata = spec.get("metadata")
        if not isinstance(agent_metadata, Mapping):
            continue
        resolved_members[member["agent"]] = agent_metadata
        if agent_metadata.get("version") != member["version"]:
            error(
                "SYSTEM021",
                f"member {member['agent']!r} version does not match Agent Spec",
            )
        member_class = agent_metadata.get("execution_class")
        if (
            member_class in EXECUTION_RANK
            and EXECUTION_RANK[execution_class] < EXECUTION_RANK[member_class]
        ):
            error(
                "SYSTEM022",
                f"system execution class is below member {member['agent']!r}",
            )
        member_tier = agent_metadata.get("risk_tier")
        if (
            member_tier in TIER_RANK
            and TIER_RANK[metadata["governance_tier"]] < TIER_RANK[member_tier]
        ):
            error(
                "SYSTEM023",
                f"system governance tier is below member {member['agent']!r}",
            )
        restrictions = member.get("restrictions", {})
        for field, metadata_field in (
            ("skills", "enabled_skills"),
            ("toolsets", "enabled_toolsets"),
        ):
            selected = restrictions.get(field, {}).get("include", [])
            allowed = agent_metadata.get(metadata_field, [])
            undeclared = sorted(set(selected) - set(allowed))
            if undeclared:
                error(
                    "SYSTEM024",
                    f"member {member['agent']!r} restrictions expand {field}: {undeclared}",
                )

    interactions = payload["interactions"]
    interaction_ids = [item["id"] for item in interactions]
    if len(interaction_ids) != len(set(interaction_ids)):
        error("SYSTEM025", "interactions contain duplicate IDs")
    allowed_callers = set(member_names) | {topology["orchestrator"]}
    seen_edges: set[tuple[str, str, str]] = set()
    for interaction in interactions:
        if interaction["from"] not in allowed_callers:
            error(
                "SYSTEM026",
                f"interaction {interaction['id']!r} has an undeclared caller",
            )
        if interaction["to"] not in member_names:
            error(
                "SYSTEM027",
                f"interaction {interaction['id']!r} has an undeclared callee",
            )
        edge = (interaction["from"], interaction["to"], interaction["mode"])
        if edge in seen_edges:
            error("SYSTEM028", f"duplicate interaction edge {edge!r}")
        seen_edges.add(edge)
        policy_path = _reference_path(root, path.parent, interaction["policy"])
        if not policy_path.is_file():
            error(
                "SYSTEM029",
                f"interaction {interaction['id']!r} policy does not exist",
                policy_path,
            )
        else:
            try:
                policy = load_data(policy_path)
            except DataFileError as exc:
                error("SYSTEM030", str(exc), policy_path)
            else:
                policy_interactions = policy.get("interactions")
                if (
                    isinstance(policy_interactions, Mapping)
                    and interaction["id"] not in policy_interactions
                ):
                    error(
                        "SYSTEM031",
                        f"policy does not define interaction {interaction['id']!r}",
                        policy_path,
                    )

    limits = payload["limits"]
    required_members = sum(1 for member in members if member["required"])
    if limits["max_agent_runs"] < required_members:
        error("SYSTEM032", "max_agent_runs is below the required member count")
    if limits["max_parallel_agents"] > limits["max_agent_runs"]:
        error("SYSTEM033", "max_parallel_agents cannot exceed max_agent_runs")
    if topology["recursive_delegation"] and limits["max_delegation_depth"] < 1:
        error("SYSTEM034", "recursive delegation requires positive maximum depth")

    risk_reference = metadata["risk_assessment"]
    risk_path = root / risk_reference
    if risk_path.is_file():
        risk_diagnostics = validate_system_risk_file(risk_path, root=root)
        diagnostics.extend(risk_diagnostics)
        if not any(item.severity == "error" for item in risk_diagnostics):
            risk = load_data(risk_path)
            assessment = risk["assessment"]
            classification = risk["classification"]
            scope = risk["scope"]
            if assessment.get("system") != name:
                error("SYSTEM035", "risk assessment system does not match System Spec")
            if assessment.get("system_version") != metadata["version"]:
                error("SYSTEM036", "risk assessment version does not match System Spec")
            if scope.get("execution_class") != execution_class:
                error("SYSTEM037", "risk assessment execution class does not match")
            if classification.get("governance_tier") != metadata["governance_tier"]:
                error("SYSTEM038", "risk assessment governance tier does not match")
            risk_members = {
                (item.get("agent"), item.get("agent_version"))
                for item in risk["member_assessments"]
                if isinstance(item, Mapping)
            }
            spec_members = {(item["agent"], item["version"]) for item in members}
            if risk_members != spec_members:
                error(
                    "SYSTEM039", "risk assessment member set does not match System Spec"
                )
    return diagnostics


def _system_risk_summary_diagnostics(
    *,
    classification: Mapping[str, Any],
    regulatory: Mapping[str, Any],
    member_tiers: list[str],
    inherent_tiers: list[str],
    residual_tiers: list[str],
    shown: str,
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []

    def maximum(tiers: list[str]) -> str | None:
        return max(tiers, key=TIER_RANK.__getitem__) if tiers else None

    expected_member = maximum(member_tiers)
    expected_inherent = maximum(inherent_tiers)
    expected_residual = maximum(residual_tiers)
    for field, expected in (
        ("maximum_member_tier", expected_member),
        ("maximum_inherent_tier", expected_inherent),
        ("maximum_residual_tier", expected_residual),
    ):
        if expected and classification.get(field) != expected:
            diagnostics.append(
                Diagnostic(
                    "SYSRISK021",
                    f"classification.{field} must summarize scenarios or members as {expected}",
                    shown,
                )
            )
    tier = classification.get("governance_tier")
    floor_candidates = [item for item in (expected_member, expected_inherent) if item]
    if tier in TIER_RANK and floor_candidates:
        floor = maximum(floor_candidates)
        if floor and TIER_RANK[tier] < TIER_RANK[floor]:
            diagnostics.append(
                Diagnostic(
                    "SYSRISK022",
                    f"governance tier cannot be below {floor}",
                    shown,
                )
            )
    decision = classification.get("decision")
    if regulatory.get("prohibited_use") is True and decision != "no_go":
        diagnostics.append(
            Diagnostic("SYSRISK023", "prohibited use requires no_go", shown)
        )
    if expected_residual == "critical" and decision != "no_go":
        diagnostics.append(
            Diagnostic("SYSRISK024", "critical residual risk requires no_go", shown)
        )
    return diagnostics


def _graph_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "system": payload["name"],
        "version": payload["metadata"]["version"],
        "topology": payload["topology"]["type"],
        "dynamic_membership": payload["topology"]["dynamic_membership"],
        "orchestrator": payload["topology"]["orchestrator"],
        "members": [
            {
                "agent": item["agent"],
                "version": item["version"],
                "role": item["role"],
                "required": item["required"],
            }
            for item in payload["members"]
        ],
        "interactions": [
            {
                "id": item["id"],
                "from": item["from"],
                "to": item["to"],
                "mode": item["mode"],
                "input_schema": item["input_schema"],
                "output_schema": item["output_schema"],
            }
            for item in payload["interactions"]
        ],
    }


def _reference_path(root: Path, system_dir: Path, reference: str) -> Path:
    raw = reference.split("#", 1)[0]
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate
    local = system_dir / candidate
    return local if local.exists() else root / candidate


def _resolve_system_targets(root: Path, paths: Sequence[Path]) -> list[Path]:
    if paths:
        targets: set[Path] = set()
        for raw in paths:
            path = raw if raw.is_absolute() else root / raw
            if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES:
                targets.add(path.resolve())
            elif path.is_dir():
                targets.update(item.resolve() for item in path.rglob("system.yaml"))
            elif path.suffix.lower() in SUPPORTED_SUFFIXES:
                targets.add(path.resolve())
        return sorted(targets)
    return sorted(root.glob("src/*/systems/*/system.yaml"))


def _schema_error_key(error: Any) -> tuple[str, str]:
    return (_json_path(error.absolute_path), error.message)


def _json_path(parts: Sequence[Any]) -> str:
    rendered = "$"
    for part in parts:
        rendered += f"[{part}]" if isinstance(part, int) else f".{part}"
    return rendered
