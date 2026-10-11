from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import yaml

from agentctl.diagnostics import Diagnostic, emit_diagnostics, relative_display

NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
LINK_PATTERN = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
ALLOWED_FRONTMATTER = {
    "name",
    "description",
    "license",
    "compatibility",
    "metadata",
    "allowed-tools",
}


def validate_skills(
    *,
    root: Path,
    paths: list[Path],
    output_format: str = "text",
    run_reference_validator: bool = False,
) -> int:
    root = root.resolve()
    packages = _discover(root, paths)
    diagnostics: list[Diagnostic] = []
    if not packages:
        diagnostics.append(
            Diagnostic("SKILL001", "no SKILL.md packages found", str(root))
        )
    names: dict[str, Path] = {}
    for package in packages:
        package_diagnostics, name = _validate_package(package, root)
        diagnostics.extend(package_diagnostics)
        if name:
            previous = names.get(name)
            if previous is not None and previous != package:
                diagnostics.append(
                    Diagnostic(
                        "SKILL002",
                        f"duplicate skill name; first found at {relative_display(previous, root)}",
                        relative_display(package, root),
                    )
                )
            names[name] = package
        if run_reference_validator:
            diagnostics.extend(_run_skills_ref(package, root))
    return emit_diagnostics(
        diagnostics, output_format=output_format, heading="Agent Skills validation"
    )


def _discover(root: Path, paths: list[Path]) -> list[Path]:
    if paths:
        packages: set[Path] = set()
        for raw in paths:
            path = raw if raw.is_absolute() else root / raw
            if path.is_file() and path.name.lower() == "skill.md":
                packages.add(path.parent.resolve())
            elif (path / "SKILL.md").is_file():
                packages.add(path.resolve())
            elif path.is_dir():
                packages.update(
                    item.parent.resolve() for item in path.rglob("SKILL.md")
                )
        return sorted(packages)
    return sorted(item.parent.resolve() for item in root.rglob("SKILL.md"))


def _validate_package(package: Path, root: Path) -> tuple[list[Diagnostic], str | None]:
    manifest = package / "SKILL.md"
    shown = relative_display(manifest, root)
    diagnostics: list[Diagnostic] = []
    try:
        text = manifest.read_text(encoding="utf-8")
    except OSError as exc:
        return [Diagnostic("SKILL003", f"cannot read SKILL.md: {exc}", shown)], None
    lines = text.splitlines()
    if len(lines) > 500:
        diagnostics.append(Diagnostic("SKILL004", "SKILL.md exceeds 500 lines", shown))
    frontmatter, body, parse_error = _parse_frontmatter(text)
    if parse_error:
        diagnostics.append(Diagnostic("SKILL005", parse_error, shown))
        return diagnostics, None
    name = frontmatter.get("name")
    description = frontmatter.get("description")
    unknown = set(frontmatter) - ALLOWED_FRONTMATTER
    for field in sorted(unknown):
        diagnostics.append(
            Diagnostic("SKILL020", f"unsupported frontmatter field: {field}", shown)
        )
    if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
        diagnostics.append(
            Diagnostic("SKILL006", "name is not valid lowercase hyphenated text", shown)
        )
        name = None
    elif name != package.name:
        diagnostics.append(
            Diagnostic("SKILL007", "name must match the parent directory", shown)
        )
    if not isinstance(description, str) or not description.strip():
        diagnostics.append(Diagnostic("SKILL008", "description is required", shown))
    elif len(description) > 1024:
        diagnostics.append(
            Diagnostic("SKILL009", "description exceeds 1024 characters", shown)
        )
    elif not re.search(r"\buse\b|\bwhen\b", description, re.IGNORECASE):
        diagnostics.append(
            Diagnostic(
                "SKILL010",
                "description should explain when the skill applies",
                shown,
                severity="warning",
            )
        )
    compatibility = frontmatter.get("compatibility")
    if compatibility is not None and (
        not isinstance(compatibility, str)
        or not compatibility.strip()
        or len(compatibility) > 500
    ):
        diagnostics.append(
            Diagnostic(
                "SKILL021",
                "compatibility must be non-empty text of at most 500 characters",
                shown,
            )
        )
    license_value = frontmatter.get("license")
    if license_value is not None and (
        not isinstance(license_value, str) or not license_value.strip()
    ):
        diagnostics.append(
            Diagnostic("SKILL025", "license must be non-empty text", shown)
        )
    metadata = frontmatter.get("metadata")
    if metadata is not None and (
        not isinstance(metadata, dict)
        or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in metadata.items()
        )
    ):
        diagnostics.append(
            Diagnostic("SKILL022", "metadata must map strings to strings", shown)
        )
    allowed_tools = frontmatter.get("allowed-tools")
    if allowed_tools is not None and (
        not isinstance(allowed_tools, str) or not allowed_tools.strip()
    ):
        diagnostics.append(
            Diagnostic("SKILL023", "allowed-tools must be a non-empty string", shown)
        )
    if not body.strip():
        diagnostics.append(Diagnostic("SKILL011", "instruction body is empty", shown))
    diagnostics.extend(_validate_links(body, package, root, shown))
    diagnostics.extend(_validate_openai_metadata(package, root))
    return diagnostics, name


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str, str | None]:
    text = text.replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return {}, text, "SKILL.md must start with YAML frontmatter"
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, "", "YAML frontmatter is not terminated"
    try:
        value = yaml.safe_load(text[4:end])
    except yaml.YAMLError as exc:
        return {}, "", f"invalid YAML frontmatter: {exc}"
    if not isinstance(value, dict):
        return {}, "", "frontmatter must be a mapping"
    return value, text[end + 5 :], None


def _validate_links(
    body: str, package: Path, root: Path, shown: str
) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    for raw_target in LINK_PATTERN.findall(body):
        target = raw_target.strip().split("#", 1)[0]
        if not target or "://" in target or target.startswith("mailto:"):
            continue
        if target.startswith("/") or ".." in Path(target).parts:
            diagnostics.append(
                Diagnostic(
                    "SKILL012", f"reference must stay within the skill: {target}", shown
                )
            )
            continue
        if not (package / target).exists():
            diagnostics.append(
                Diagnostic("SKILL013", f"missing referenced file: {target}", shown)
            )
        if len(Path(target).parts) > 2:
            diagnostics.append(
                Diagnostic(
                    "SKILL014",
                    f"keep references one level deep: {target}",
                    shown,
                    severity="warning",
                )
            )
    return diagnostics


def _validate_openai_metadata(package: Path, root: Path) -> list[Diagnostic]:
    path = package / "agents" / "openai.yaml"
    if not path.exists():
        return []
    shown = relative_display(path, root)
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return [Diagnostic("SKILL015", f"invalid OpenAI metadata: {exc}", shown)]
    interface = value.get("interface") if isinstance(value, dict) else None
    if not isinstance(interface, dict):
        return [Diagnostic("SKILL016", "interface mapping is required", shown)]
    diagnostics: list[Diagnostic] = []
    for field in ("display_name", "short_description"):
        if not isinstance(interface.get(field), str) or not interface[field].strip():
            diagnostics.append(
                Diagnostic("SKILL017", f"interface.{field} is required", shown)
            )
    if "default_prompt" in interface and (
        not isinstance(interface["default_prompt"], str)
        or not interface["default_prompt"].strip()
    ):
        diagnostics.append(
            Diagnostic("SKILL024", "interface.default_prompt must be non-empty", shown)
        )
    return diagnostics


def _run_skills_ref(package: Path, root: Path) -> list[Diagnostic]:
    executable = shutil.which("skills-ref")
    if executable is None:
        return [
            Diagnostic(
                "SKILL018",
                "skills-ref is not installed; omit --run-skills-ref or install it",
                relative_display(package, root),
            )
        ]
    result = subprocess.run(
        [executable, "validate", str(package)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return []
    detail = (
        result.stderr or result.stdout
    ).strip() or "skills-ref rejected the package"
    return [Diagnostic("SKILL019", detail, relative_display(package, root))]
