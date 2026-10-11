from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agentctl.diagnostics import Diagnostic, emit_diagnostics, relative_display
from agentctl.files import DataFileError, load_data
from agentctl.risk import validate_risk_file

EXECUTION_CLASSES = ("ephemeral", "durable", "human_governed")
EXECUTION_RANK = {name: rank for rank, name in enumerate(EXECUTION_CLASSES)}
RISK_TIERS = {"low", "medium", "high", "critical"}
EFFECT_MINIMUM = {
    "read": "ephemeral",
    "write_reversible": "durable",
    "write_consequential": "human_governed",
}
RETRY_SAFETY = {"safe", "idempotency_key_required", "unsafe"}
NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SEMVER_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
REQUIRED_BUDGETS = {
    "max_requests",
    "max_input_tokens",
    "max_output_tokens",
    "max_tool_calls",
    "max_cost_usd",
}


def validate_project(root: Path, *, output_format: str = "text") -> int:
    root = root.resolve()
    specs = sorted(root.glob("src/*/agents/*/agent.yaml"))
    diagnostics: list[Diagnostic] = []
    if not specs:
        diagnostics.append(
            Diagnostic(
                "AGENT001",
                "no Agent Specs found under src/<package>/agents/<agent>/agent.yaml",
                str(root),
            )
        )
    for path in specs:
        diagnostics.extend(_validate_spec(path, root))
    return emit_diagnostics(
        diagnostics, output_format=output_format, heading="Agent contract validation"
    )


def _validate_spec(path: Path, root: Path) -> list[Diagnostic]:
    shown = relative_display(path, root)
    try:
        spec = load_data(path)
    except DataFileError as exc:
        return [Diagnostic("AGENT002", str(exc), shown)]

    errors: list[Diagnostic] = []

    def error(code: str, message: str, target: Path = path) -> None:
        errors.append(Diagnostic(code, message, relative_display(target, root)))

    name = spec.get("name")
    if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
        error("AGENT003", "name must be lowercase hyphenated text")
    elif name.replace("-", "_") != path.parent.name:
        error("AGENT034", "agent directory must match the normalized agent name")
    description = spec.get("description")
    if not isinstance(description, str) or not description.strip():
        error("AGENT004", "description is required")
    elif "replace with" in description.lower():
        error("AGENT005", "replace the generated placeholder description")
    instructions = spec.get("instructions")
    if (
        not isinstance(instructions, list)
        or not instructions
        or not all(isinstance(item, str) and item.strip() for item in instructions)
    ):
        error("AGENT006", "instructions must be a non-empty list of strings")
    retries = spec.get("retries")
    if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
        error("AGENT029", "retries must be a non-negative integer")
    timeout = spec.get("tool_timeout")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or timeout <= 0
    ):
        error("AGENT030", "tool_timeout must be greater than zero")
    if spec.get("instrument") is not True:
        error("AGENT031", "instrument must be true for production agents")

    metadata = spec.get("metadata")
    if not isinstance(metadata, Mapping):
        error("AGENT007", "metadata must be a mapping")
        return errors

    for field in (
        "contract_version",
        "owner",
        "version",
        "execution_class",
        "risk_tier",
        "risk_assessment",
        "data_classification",
        "model_policy",
        "enabled_skills",
        "enabled_toolsets",
        "budgets",
    ):
        if field not in metadata:
            error("AGENT008", f"metadata.{field} is required")

    owner = metadata.get("owner")
    if not isinstance(owner, str) or not owner.strip() or owner == "replace-me":
        error("AGENT009", "metadata.owner must name an accountable owner")
    contract_version = metadata.get("contract_version")
    if (
        isinstance(contract_version, bool)
        or not isinstance(contract_version, int)
        or contract_version <= 0
    ):
        error("AGENT032", "metadata.contract_version must be a positive integer")
    version = metadata.get("version")
    if not isinstance(version, str) or not SEMVER_PATTERN.fullmatch(version):
        error("AGENT010", "metadata.version must be semantic version text")
    execution_class = metadata.get("execution_class")
    if execution_class not in EXECUTION_CLASSES:
        error(
            "AGENT011", f"metadata.execution_class must be one of {EXECUTION_CLASSES}"
        )
    risk_tier = metadata.get("risk_tier")
    if risk_tier not in RISK_TIERS:
        error("AGENT012", "metadata.risk_tier must be low, medium, high, or critical")
    risk_assessment = metadata.get("risk_assessment")
    if not _nonempty_string(risk_assessment):
        error("AGENT035", "metadata.risk_assessment must reference an assessment")
    else:
        risk_path = root / risk_assessment
        if not risk_path.is_file():
            error("AGENT036", "risk assessment does not exist", risk_path)
        else:
            risk_diagnostics = validate_risk_file(risk_path, root=root)
            errors.extend(risk_diagnostics)
            if not any(item.severity == "error" for item in risk_diagnostics):
                risk = load_data(risk_path)
                assessment = risk["assessment"]
                classification = risk["classification"]
                scope = risk["scope"]
                if assessment["agent"] != name:
                    error("AGENT037", "risk assessment agent does not match Agent Spec")
                if assessment["agent_version"] != version:
                    error(
                        "AGENT038",
                        "risk assessment agent_version does not match Agent Spec",
                    )
                if scope["execution_class"] != execution_class:
                    error(
                        "AGENT039",
                        "risk assessment execution_class does not match Agent Spec",
                    )
                if classification["governance_tier"] != risk_tier:
                    error(
                        "AGENT040",
                        "risk assessment governance_tier does not match metadata.risk_tier",
                    )
    if not _nonempty_string(metadata.get("model_policy")):
        error("AGENT013", "metadata.model_policy must be a versioned logical policy")
    elif not re.search(r"-v\d+$", metadata["model_policy"]):
        error("AGENT033", "metadata.model_policy must end with a version such as -v1")
    if not _nonempty_string(metadata.get("data_classification")):
        error("AGENT014", "metadata.data_classification is required")

    budgets = metadata.get("budgets")
    if not isinstance(budgets, Mapping):
        error("AGENT015", "metadata.budgets must be a mapping")
    else:
        missing = REQUIRED_BUDGETS - set(budgets)
        for field in sorted(missing):
            error("AGENT016", f"metadata.budgets.{field} is required")
        for field in REQUIRED_BUDGETS & set(budgets):
            value = budgets[field]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or value <= 0
            ):
                error("AGENT017", f"metadata.budgets.{field} must be greater than zero")

    package_dir = path.parents[2]
    agent_dir = path.parent
    if execution_class in {"durable", "human_governed"}:
        if metadata.get("temporal_enabled") is not True:
            error(
                "AGENT018", "durable execution requires metadata.temporal_enabled: true"
            )
        for directory in (package_dir / "workflows", package_dir / "activities"):
            if not directory.is_dir():
                error(
                    "AGENT019", "durable execution requires this directory", directory
                )
    if execution_class == "human_governed":
        approval = metadata.get("approval_policy")
        if not _nonempty_string(approval):
            error(
                "AGENT020", "human-governed execution requires metadata.approval_policy"
            )
        elif not (root / approval).is_file():
            error("AGENT021", "approval policy does not exist", root / approval)

    if risk_tier in {"medium", "high", "critical"}:
        threat_model = metadata.get("threat_model", f"docs/threat-models/{name}.md")
        if not isinstance(threat_model, str) or not (root / threat_model).is_file():
            error(
                "AGENT022",
                "medium-, high-, and critical-risk agents require a threat model",
            )

    policy = metadata.get("evaluation_policy")
    if not _nonempty_string(policy):
        error("AGENT023", "metadata.evaluation_policy is required")
    elif not (root / policy).is_file():
        error("AGENT024", "evaluation policy does not exist", root / policy)

    errors.extend(
        _validate_capabilities(
            root=root,
            package_dir=package_dir,
            agent_dir=agent_dir,
            metadata=metadata,
            execution_class=execution_class,
            spec_path=path,
        )
    )
    return errors


def _validate_capabilities(
    *,
    root: Path,
    package_dir: Path,
    agent_dir: Path,
    metadata: Mapping[str, Any],
    execution_class: Any,
    spec_path: Path,
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    shown = relative_display(spec_path, root)
    skills = metadata.get("enabled_skills")
    toolsets = metadata.get("enabled_toolsets")
    if not _string_list(skills):
        diagnostics.append(
            Diagnostic("AGENT025", "enabled_skills must be a list of strings", shown)
        )
        skills = []
    elif len(skills) != len(set(skills)):
        diagnostics.append(
            Diagnostic("AGENT035", "enabled_skills contains duplicates", shown)
        )
    if not _string_list(toolsets):
        diagnostics.append(
            Diagnostic("AGENT026", "enabled_toolsets must be a list of strings", shown)
        )
        toolsets = []
    elif len(toolsets) != len(set(toolsets)):
        diagnostics.append(
            Diagnostic("AGENT036", "enabled_toolsets contains duplicates", shown)
        )

    for skill in skills:
        candidates = (
            agent_dir / "skills" / skill / "SKILL.md",
            package_dir / "skills" / skill / "SKILL.md",
        )
        if not any(candidate.is_file() for candidate in candidates):
            diagnostics.append(
                Diagnostic("AGENT027", f"enabled skill {skill!r} was not found", shown)
            )

    for toolset in toolsets:
        candidates = (
            agent_dir / "tools" / toolset,
            package_dir / "tools" / toolset,
        )
        directory = next(
            (candidate for candidate in candidates if candidate.is_dir()), None
        )
        if directory is None:
            diagnostics.append(
                Diagnostic(
                    "AGENT028", f"enabled toolset {toolset!r} was not found", shown
                )
            )
            continue
        manifest = directory / "tool.yaml"
        if not manifest.is_file():
            diagnostics.append(
                Diagnostic(
                    "TOOL001",
                    "enabled toolset requires tool.yaml contract",
                    relative_display(directory, root),
                )
            )
            continue
        diagnostics.extend(_validate_tool_manifest(manifest, root, execution_class))
    return diagnostics


def _validate_tool_manifest(
    path: Path, root: Path, execution_class: Any
) -> list[Diagnostic]:
    shown = relative_display(path, root)
    try:
        manifest = load_data(path)
    except DataFileError as exc:
        return [Diagnostic("TOOL002", str(exc), shown)]
    diagnostics: list[Diagnostic] = []
    for field in (
        "name",
        "owner",
        "effect",
        "retry_safety",
        "authorization_scopes",
        "timeout_seconds",
        "data_classification",
        "max_output_bytes",
    ):
        if field not in manifest:
            diagnostics.append(Diagnostic("TOOL003", f"{field} is required", shown))
    if not isinstance(manifest.get("name"), str) or not NAME_PATTERN.fullmatch(
        manifest["name"]
    ):
        diagnostics.append(
            Diagnostic("TOOL010", "name must be lowercase hyphenated text", shown)
        )
    if not _nonempty_string(manifest.get("owner")):
        diagnostics.append(Diagnostic("TOOL011", "owner is required", shown))
    if not _nonempty_string(manifest.get("data_classification")):
        diagnostics.append(
            Diagnostic("TOOL012", "data_classification is required", shown)
        )
    effect = manifest.get("effect")
    if effect not in EFFECT_MINIMUM:
        diagnostics.append(Diagnostic("TOOL004", "effect has an invalid value", shown))
    elif execution_class in EXECUTION_RANK:
        minimum = EFFECT_MINIMUM[effect]
        if EXECUTION_RANK[execution_class] < EXECUTION_RANK[minimum]:
            diagnostics.append(
                Diagnostic(
                    "TOOL005",
                    f"{effect} requires execution class {minimum} or higher",
                    shown,
                )
            )
    retry = manifest.get("retry_safety")
    if retry not in RETRY_SAFETY:
        diagnostics.append(
            Diagnostic("TOOL006", "retry_safety has an invalid value", shown)
        )
    if effect in {"write_reversible", "write_consequential"} and retry == "safe":
        diagnostics.append(
            Diagnostic(
                "TOOL007",
                "writes must declare idempotency_key_required or unsafe retry safety",
                shown,
            )
        )
    for field in ("timeout_seconds", "max_output_bytes"):
        value = manifest.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            diagnostics.append(
                Diagnostic("TOOL008", f"{field} must be greater than zero", shown)
            )
    scopes = manifest.get("authorization_scopes")
    if not _string_list(scopes):
        diagnostics.append(
            Diagnostic(
                "TOOL009", "authorization_scopes must be a list of strings", shown
            )
        )
    return diagnostics


def _string_list(value: Any) -> bool:
    return isinstance(value, list) and all(
        isinstance(item, str) and bool(item.strip()) for item in value
    )


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())
