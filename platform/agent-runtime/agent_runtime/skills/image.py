"""An agent's pinned skills on their way into an image, and in it (section 12 of
`platform/registry/contract/skills.md`).

    python -m agent_runtime.skills.image stage --agent agents/<name> --skills skills --out <dir>
    python -m agent_runtime.skills.image verify --app /app

`stage` runs where the repository is: it fails when the lock is stale and otherwise writes
the listed skills and the lock under `<dir>`, laid out as the app directory keeps them.
`verify` runs in the finished image: the lock pins what is there, and every interpreter a
skill names is found. Each prints one line per problem and exits 1 when there is one.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import yaml

from agent_runtime.skills import format as fmt
from agent_runtime.skills import launcher

SKILLS_DIR = "skills"  # under the app directory, and at the root of an agent repository
MANIFEST = "agent.yaml"


def _listed(directory: Path) -> list[str]:
    try:
        manifest = yaml.safe_load((directory / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise fmt.SkillError(f"{MANIFEST} cannot be read") from exc
    return fmt.listed(manifest)


def stage(agent_dir: Path, skills_dir: Path, out: Path) -> list[str]:
    """Write `out/skills/<name>/` for each skill the agent lists and `out/skills.lock`, or
    say why not. With no skills listed, `out/skills/` is made and left empty, so a build can
    copy `out` whether or not the agent has skills. Returns the problems; [] when written."""
    agent_dir, skills_dir, out = Path(agent_dir), Path(skills_dir), Path(out)
    try:
        names = _listed(agent_dir)
    except fmt.SkillError as exc:
        return [str(exc)]
    problems = fmt.lock_problems(names, skills_dir, agent_dir / fmt.LOCK)
    if problems:
        return problems
    target = out / SKILLS_DIR
    if target.exists() and any(target.iterdir()):
        return [f"{target} is not empty"]
    target.mkdir(parents=True, exist_ok=True)
    for name in names:
        for relative, source in fmt.files(skills_dir / name):
            copy = target / name / relative
            copy.parent.mkdir(parents=True, exist_ok=True)
            copy.write_bytes(source.read_bytes())
            copy.chmod(0o755 if source.stat().st_mode & 0o100 else 0o644)
    if names:
        (out / fmt.LOCK).write_bytes((agent_dir / fmt.LOCK).read_bytes())
    return fmt.lock_problems(names, target, out / fmt.LOCK) if names else []


def verify(app: Path, *, interpreter_path: str = launcher.INTERPRETER_PATH) -> list[str]:
    """Why the skills under an app directory are not what its manifest and lock say, or
    cannot run their scripts there. [] when they are and can."""
    app = Path(app)
    try:
        names = _listed(app)
    except fmt.SkillError as exc:
        return [str(exc)]
    directory = app / SKILLS_DIR
    problems = fmt.lock_problems(names, directory, app / fmt.LOCK)
    if directory.is_dir():
        problems += [
            f"the image carries {entry.name} under {SKILLS_DIR}/, which the agent does not list"
            for entry in sorted(directory.iterdir())
            if entry.name not in names
        ]
    if problems:
        return problems
    for name in names:
        sidecar = fmt.read(directory / name).sidecar
        for interpreter in sorted({s.interpreter for s in sidecar.scripts} if sidecar else ()):
            if shutil.which(interpreter, path=interpreter_path) is None:
                problems.append(
                    f"skill {name} names the interpreter {interpreter}, which is not in "
                    f"the image (looked in {interpreter_path})"
                )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent_runtime.skills.image")
    commands = parser.add_subparsers(dest="command", required=True)
    staging = commands.add_parser("stage", help="write the pinned skills for the image build")
    staging.add_argument("--agent", type=Path, required=True, help="the agent's directory")
    staging.add_argument("--skills", type=Path, required=True, help="the repository's skills")
    staging.add_argument("--out", type=Path, required=True, help="where to write")
    verifying = commands.add_parser("verify", help="check the skills of an app directory")
    verifying.add_argument("--app", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "stage":
        problems = stage(args.agent, args.skills, args.out)
    else:
        problems = verify(args.app)
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems and args.command == "stage":  # the build's message names the command too
        print(f"{fmt.LOCK} is written by `zetra-agents render`", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
