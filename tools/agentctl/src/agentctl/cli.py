from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from rich.console import Console
from rich_argparse import RichHelpFormatter

from agentctl import __version__
from agentctl.evals import run_eval_command
from agentctl.release import check_release
from agentctl.risk import explain_risk_assessment, validate_risk_assessments
from agentctl.scaffold import scaffold_agent
from agentctl.skills import validate_skills
from agentctl.system_scaffold import scaffold_system
from agentctl.systems import explain_system, graph_system, validate_systems
from agentctl.ui import configure, console_options, error, json_error
from agentctl.validation import validate_project

_help_no_color = False
_help_force_color = False


def _resolve_color_preferences(raw_args: list[str]) -> tuple[bool, bool]:
    if "--no-color" in raw_args:
        return True, False
    if "--force-color" in raw_args:
        return False, True
    if "NO_COLOR" in os.environ:
        return True, False
    force_color = os.environ.get("FORCE_COLOR")
    return False, force_color not in (None, "", "0")


def _rich_formatter(prog: str) -> RichHelpFormatter:
    return RichHelpFormatter(
        prog,
        console=Console(
            **console_options(no_color=_help_no_color, force_color=_help_force_color)
        ),
    )


class AgentArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("formatter_class", _rich_formatter)
        super().__init__(*args, **kwargs)


def build_parser() -> argparse.ArgumentParser:
    RichHelpFormatter.styles.update(
        {
            "argparse.args": "bold bright_cyan",
            "argparse.groups": "bold bright_magenta",
            "argparse.help": "white",
            "argparse.metavar": "bright_blue",
            "argparse.text": "dim",
        }
    )
    parser = AgentArgumentParser(
        prog="agentctl",
        description="Build and verify agents and multi-agent systems.",
        epilog=(
            "Examples: agentctl scaffold support-agent --owner platform · "
            "agentctl system validate · agentctl skills validate skills"
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    appearance = parser.add_mutually_exclusive_group()
    appearance.add_argument(
        "--no-color", action="store_true", help="disable ANSI color output"
    )
    appearance.add_argument(
        "--force-color", action="store_true", help="emit ANSI colors when redirected"
    )
    parser.add_argument(
        "--no-animations", action="store_true", help="disable interactive spinners"
    )
    commands = parser.add_subparsers(
        dest="command", required=True, parser_class=AgentArgumentParser
    )

    scaffold = commands.add_parser(
        "scaffold", help="create a golden-path agent package"
    )
    scaffold.add_argument("name", help="lowercase hyphenated agent name")
    scaffold.add_argument("--root", type=Path, default=Path.cwd())
    scaffold.add_argument("--package", default="acme_agents")
    scaffold.add_argument(
        "--execution-class",
        choices=("ephemeral", "durable", "human_governed"),
        default="ephemeral",
    )
    scaffold.add_argument(
        "--risk-tier",
        choices=("low", "medium", "high", "critical"),
        default="low",
    )
    scaffold.add_argument("--owner", default="replace-me")
    scaffold.add_argument("--with-temporal", action="store_true")
    scaffold.set_defaults(with_evals=True)
    scaffold.add_argument("--force", action="store_true")

    validate = commands.add_parser("validate", help="validate agent contracts")
    validate.add_argument("--root", type=Path, default=Path.cwd())
    validate.add_argument("--format", choices=("text", "json"), default="text")

    system = commands.add_parser("system", help="work with multi-agent systems")
    system_commands = system.add_subparsers(
        dest="system_command", required=True, parser_class=AgentArgumentParser
    )
    system_scaffold = system_commands.add_parser(
        "scaffold", help="create a golden-path multi-agent system package"
    )
    system_scaffold.add_argument("name", help="lowercase hyphenated system name")
    system_scaffold.add_argument("--root", type=Path, default=Path.cwd())
    system_scaffold.add_argument("--package", default="acme_agents")
    system_scaffold.add_argument("--owner", default="replace-me")
    system_scaffold.add_argument("--operational-owner", default="replace-me")
    system_scaffold.add_argument(
        "--execution-class",
        choices=("ephemeral", "durable", "human_governed"),
        default="durable",
    )
    system_scaffold.add_argument(
        "--risk-tier",
        choices=("low", "medium", "high", "critical"),
        default="medium",
    )
    system_scaffold.add_argument(
        "--topology",
        choices=(
            "programmatic_pipeline",
            "supervisor_worker",
            "fanout_gather",
            "producer_reviewer",
            "handoff",
            "quorum",
            "hierarchical",
            "blackboard",
            "peer_mesh",
            "recursive_task_tree",
            "dynamic_membership",
            "market",
            "event_driven",
            "hybrid",
        ),
        default="supervisor_worker",
    )
    system_scaffold.add_argument(
        "--member",
        action="append",
        default=[],
        metavar="AGENT:ROLE",
        help="pinned agent and role; repeat at least twice",
    )
    system_scaffold.add_argument("--dynamic-membership", action="store_true")
    system_scaffold.add_argument("--recursive-delegation", action="store_true")
    system_scaffold.add_argument("--with-temporal", action="store_true")
    system_scaffold.add_argument("--force", action="store_true")

    system_validate = system_commands.add_parser(
        "validate", help="validate System Specs and referenced contracts"
    )
    system_validate.add_argument("paths", nargs="*", type=Path)
    system_validate.add_argument("--root", type=Path, default=Path.cwd())
    system_validate.add_argument("--format", choices=("text", "json"), default="text")
    system_explain = system_commands.add_parser(
        "explain", help="show a validated system summary"
    )
    system_explain.add_argument("path", type=Path)
    system_explain.add_argument("--root", type=Path, default=Path.cwd())
    system_explain.add_argument("--format", choices=("text", "json"), default="text")
    system_graph = system_commands.add_parser(
        "graph", help="render the declared member and interaction graph"
    )
    system_graph.add_argument("path", type=Path)
    system_graph.add_argument("--root", type=Path, default=Path.cwd())
    system_graph.add_argument(
        "--format", choices=("text", "json", "dot"), default="text"
    )

    skills = commands.add_parser("skills", help="work with Agent Skills packages")
    skills_commands = skills.add_subparsers(
        dest="skills_command", required=True, parser_class=AgentArgumentParser
    )
    skills_validate = skills_commands.add_parser(
        "validate", help="validate Agent Skills packages"
    )
    skills_validate.add_argument("paths", nargs="*", type=Path)
    skills_validate.add_argument("--root", type=Path, default=Path.cwd())
    skills_validate.add_argument("--format", choices=("text", "json"), default="text")
    skills_validate.add_argument("--run-skills-ref", action="store_true")

    risk = commands.add_parser("risk", help="assess and inspect agent risk")
    risk_commands = risk.add_subparsers(
        dest="risk_command", required=True, parser_class=AgentArgumentParser
    )
    risk_validate = risk_commands.add_parser(
        "validate", help="validate risk assessment artifacts"
    )
    risk_validate.add_argument("paths", nargs="*", type=Path)
    risk_validate.add_argument("--root", type=Path, default=Path.cwd())
    risk_validate.add_argument("--format", choices=("text", "json"), default="text")
    risk_explain = risk_commands.add_parser(
        "explain", help="show a risk assessment summary"
    )
    risk_explain.add_argument("path", type=Path)
    risk_explain.add_argument("--root", type=Path, default=Path.cwd())
    risk_explain.add_argument("--format", choices=("text", "json"), default="text")

    eval_parser = commands.add_parser("eval", help="run standardized evaluations")
    eval_commands = eval_parser.add_subparsers(
        dest="eval_command", required=True, parser_class=AgentArgumentParser
    )
    eval_run = eval_commands.add_parser("run", help="run an eval adapter")
    eval_run.add_argument("agent")
    eval_run.add_argument(
        "--system",
        action="store_true",
        help="evaluate a multi-agent system instead of an individual agent",
    )
    eval_run.add_argument("--root", type=Path, default=Path.cwd())
    eval_run.add_argument("--report", type=Path)
    eval_run.add_argument(
        "--command",
        dest="eval_argv",
        nargs=argparse.REMAINDER,
        help="adapter command; defaults to evals/<agent>/run.py",
    )

    release = commands.add_parser("release", help="evaluate release policy")
    release_commands = release.add_subparsers(
        dest="release_command", required=True, parser_class=AgentArgumentParser
    )
    release_check = release_commands.add_parser("check", help="apply release gates")
    release_check.add_argument("--report", type=Path, required=True)
    release_check.add_argument("--policy", type=Path, required=True)
    release_check.add_argument("--baseline", type=Path)
    release_check.add_argument("--format", choices=("text", "json"), default="text")

    return parser


def main(argv: list[str] | None = None) -> int:
    global _help_force_color, _help_no_color
    raw_args = list(sys.argv[1:] if argv is None else argv)
    _help_no_color, _help_force_color = _resolve_color_preferences(raw_args)
    args = build_parser().parse_args(raw_args)
    configure(
        no_color=_help_no_color,
        force_color=_help_force_color,
        no_animations=args.no_animations,
    )
    try:
        if args.command == "scaffold":
            return scaffold_agent(args)
        if args.command == "validate":
            return validate_project(args.root, output_format=args.format)
        if args.command == "system" and args.system_command == "scaffold":
            return scaffold_system(args)
        if args.command == "system" and args.system_command == "validate":
            return validate_systems(
                root=args.root, paths=args.paths, output_format=args.format
            )
        if args.command == "system" and args.system_command == "explain":
            return explain_system(
                root=args.root, path=args.path, output_format=args.format
            )
        if args.command == "system" and args.system_command == "graph":
            return graph_system(
                root=args.root, path=args.path, output_format=args.format
            )
        if args.command == "skills" and args.skills_command == "validate":
            return validate_skills(
                root=args.root,
                paths=args.paths,
                output_format=args.format,
                run_reference_validator=args.run_skills_ref,
            )
        if args.command == "risk" and args.risk_command == "validate":
            return validate_risk_assessments(
                root=args.root,
                paths=args.paths,
                output_format=args.format,
            )
        if args.command == "risk" and args.risk_command == "explain":
            return explain_risk_assessment(
                root=args.root,
                path=args.path,
                output_format=args.format,
            )
        if args.command == "eval" and args.eval_command == "run":
            return run_eval_command(args)
        if args.command == "release" and args.release_command == "check":
            return check_release(
                report_path=args.report,
                policy_path=args.policy,
                baseline_path=args.baseline,
                output_format=args.format,
            )
    except (OSError, TypeError, ValueError) as exc:
        if getattr(args, "format", "text") == "json":
            json_error(str(exc))
        else:
            error(str(exc))
        return 2
    return 2
