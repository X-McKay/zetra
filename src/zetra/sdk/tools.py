"""``@tool`` and ``@approval``, static checks, discovery and the toolset the agent gets.

A tool is one decorated function per file under ``tools/``. The capability's operation
names the effect (``read``, ``write`` or ``exec``). A write body is a pure payload builder:
the SDK commits the payload through the receiver named by the capability's resource,
after approval when ``@approval`` is present. Keyword-only parameters annotated with the
agent's ``Input`` or with ``RunDeps`` are filled by the SDK and hidden from the model.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset

from .contracts import Contract, RunDeps
from .errors import CompileError, Denied
from .receiver import Grant, Receiver, payload_digest

CAPABILITY = re.compile(r"^([a-z][a-z0-9_-]*)\.(read|write|exec)$")
FORBIDDEN_IN_WRITE_BODY = {
    "open",
    "print",
    "input",
    "exec",
    "eval",
    "__import__",
    "datetime",
    "time",
    "random",
    "uuid",
    "os",
    "subprocess",
    "requests",
    "httpx",
    "socket",
    "pathlib",
    "secrets",
    "asyncio",
}


def tool(capability: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register the function as the tool for ``capability`` (``resource.operation``)."""
    if not isinstance(capability, str) or not CAPABILITY.fullmatch(capability):
        raise CompileError(
            "ZT-TOOL-001", f"capability must be resource.(read|write|exec), got {capability!r}"
        )

    def mark(function: Callable[..., Any]) -> Callable[..., Any]:
        marked: Any = function
        marked.__zetra_tool__ = {"capability": capability, "approval": False, "ttl": None}
        return function

    return mark


def approval(function: Callable[..., Any] | None = None, *, ttl: str | None = None) -> Any:
    """Require a human approval before the tool runs. Stack it above ``@tool``."""
    if ttl is not None and not re.fullmatch(r"P(T?\d+[HMS])+|P\d+D", ttl):
        raise CompileError(
            "ZT-TOOL-003", f"ttl must be an ISO 8601 duration such as PT10M, got {ttl!r}"
        )

    def mark(f: Callable[..., Any]) -> Callable[..., Any]:
        meta = getattr(f, "__zetra_tool__", None)
        if meta is None:
            raise CompileError("ZT-TOOL-002", "@approval must be applied above @tool")
        meta["approval"], meta["ttl"] = True, ttl
        return f

    return mark(function) if function is not None else mark


def split_capability(capability: str) -> tuple[str, str]:
    """Split a validated ``resource.operation`` capability into its two parts."""
    match = CAPABILITY.fullmatch(capability)
    if match is None:
        raise CompileError("ZT-TOOL-001", f"invalid capability {capability!r}")
    resource, effect = match.groups()
    return resource, effect


@dataclass(frozen=True)
class ToolDecl:
    name: str
    capability: str
    resource: str
    effect: str
    approval: bool
    ttl: str | None
    injects: dict[str, str]  # parameter name -> "input" | "deps"
    function: Callable[..., Any] = field(compare=False, repr=False)
    docstring: str = ""

    @property
    def is_write(self) -> bool:
        return self.effect != "read"


# ---- static checks (no imports) -------------------------------------------------------------


def _const_str(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _root_name(node: ast.AST) -> str:
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else ""


def check_tool_file(path: Path) -> dict[str, Any]:
    """Return the declaration read from source; raise CompileError on any rule violation."""
    where = str(path)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=where)
    decorated = [
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.decorator_list
    ]
    if len(decorated) != 1:
        raise CompileError(
            "ZT-TOOL-010", "exactly one decorated function per tool file", where=where
        )
    fn = decorated[0]
    if fn.name != path.stem:
        raise CompileError(
            "ZT-TOOL-011",
            f"function {fn.name!r} must match the file name {path.stem!r}",
            where=where,
        )
    decl: dict[str, Any] = {"name": fn.name, "approval": False, "ttl": None}
    for deco in fn.decorator_list:
        call = deco if isinstance(deco, ast.Call) else None
        name = _root_name(call.func if call else deco)
        if name == "tool":
            cap = _const_str(call.args[0]) if call and call.args else None
            if cap is None or not CAPABILITY.fullmatch(cap):
                raise CompileError(
                    "ZT-TOOL-012",
                    "@tool needs a literal 'resource.operation' capability",
                    where=where,
                )
            decl["capability"] = cap
        elif name == "approval":
            decl["approval"] = True
            for kw in call.keywords if call else []:
                if kw.arg != "ttl" or _const_str(kw.value) is None:
                    raise CompileError(
                        "ZT-TOOL-013", "@approval accepts only a literal ttl=", where=where
                    )
                decl["ttl"] = _const_str(kw.value)
        else:
            raise CompileError(
                "ZT-TOOL-014",
                f"unknown decorator {name!r}; only @tool and @approval are allowed",
                where=where,
            )
    if "capability" not in decl:
        raise CompileError("ZT-TOOL-015", "@tool decorator is required", where=where)
    effect = split_capability(decl["capability"])[1]
    decl["effect"] = effect
    if effect != "read":
        if isinstance(fn, ast.AsyncFunctionDef):
            raise CompileError(
                "ZT-TOOL-004", "a write tool is a synchronous payload builder", where=where
            )
        for node in ast.walk(fn):
            if isinstance(
                node,
                ast.Await | ast.Import | ast.ImportFrom | ast.Global | ast.Yield | ast.YieldFrom,
            ):
                raise CompileError(
                    "ZT-TOOL-004",
                    "a write body may only build its payload from its arguments",
                    where=where,
                )
            if isinstance(node, ast.Call) and _root_name(node.func) in FORBIDDEN_IN_WRITE_BODY:
                raise CompileError(
                    "ZT-TOOL-004",
                    f"{fn.name} is a write tool but its body calls {_root_name(node.func)}; "
                    "a write body is re-run to recompute the approved digest, so no time, randomness or I/O",
                    where=f"{where}:{node.lineno}",
                )
    return decl


# ---- discovery ------------------------------------------------------------------------------


def discover_tools(package_dir: Path, input_type: type[Contract]) -> list[ToolDecl]:
    tools_dir = package_dir / "tools"
    if not tools_dir.is_dir():
        return []
    decls: list[ToolDecl] = []
    for path in sorted(tools_dir.iterdir()):
        if path.name == "__init__.py" or path.name == "__pycache__":
            continue
        if path.suffix != ".py" or not path.is_file():
            raise CompileError(
                "ZT-TOOL-016", "only one-tool .py files belong in tools/", where=str(path)
            )
        static = check_tool_file(path)
        module = importlib.import_module(f"{package_dir.name}.tools.{path.stem}")
        function = getattr(module, static["name"], None)
        meta = getattr(function, "__zetra_tool__", None)
        if function is None or meta is None or meta["capability"] != static["capability"]:
            raise CompileError(
                "ZT-TOOL-017", "decorator metadata does not match the source", where=str(path)
            )
        injects: dict[str, str] = {}
        for pname, param in inspect.signature(function).parameters.items():
            if param.kind is inspect.Parameter.KEYWORD_ONLY:
                if param.annotation is input_type:
                    injects[pname] = "input"
                elif param.annotation is RunDeps:
                    injects[pname] = "deps"
                else:
                    raise CompileError(
                        "ZT-TOOL-018",
                        f"keyword-only parameter {pname!r} must be annotated Input or RunDeps",
                        where=str(path),
                    )
        resource, effect = split_capability(static["capability"])
        decls.append(
            ToolDecl(
                name=static["name"],
                capability=static["capability"],
                resource=resource,
                effect=effect,
                approval=bool(meta["approval"]),
                ttl=meta["ttl"],
                injects=injects,
                function=function,
                docstring=inspect.getdoc(function) or "",
            )
        )
    return decls


def ensure_importable(package_dir: Path) -> None:
    parent = str(package_dir.parent.resolve())
    if parent not in sys.path:
        sys.path.insert(0, parent)


# ---- runtime toolset ------------------------------------------------------------------------


class Bindings:
    """What the worker supplies: receivers by resource name and the tool activity timeout."""

    def __init__(
        self,
        receivers: dict[str, Receiver] | None = None,
        *,
        tool_timeout: timedelta = timedelta(seconds=30),
    ):
        self.receivers = dict(receivers or {})
        self.tool_timeout = tool_timeout

    def receiver(self, resource: str) -> Receiver:
        try:
            return self.receivers[resource]
        except KeyError:
            raise Denied(f"no receiver is bound for resource {resource!r}") from None


def activity_config(decl: ToolDecl, timeout: timedelta) -> Any:
    """Bounded Temporal activity options; None when temporalio is absent.

    Writes retry too: a repeated operation id returns the receiver's stored receipt, so a
    retry after a crash between commit and completion cannot commit twice.
    """
    try:
        from temporalio.common import RetryPolicy
    except ImportError:  # pragma: no cover - temporal extra not installed
        return None
    return {
        "start_to_close_timeout": timeout,
        "retry_policy": RetryPolicy(maximum_attempts=3, non_retryable_error_types=["Denied"]),
        "summary": decl.name,
    }


def build_toolset(
    decls: list[ToolDecl],
    *,
    input_type: type[Contract],
    bindings: Bindings,
    reversible: frozenset[str],
) -> FunctionToolset[RunDeps]:
    toolset: FunctionToolset[RunDeps] = FunctionToolset(id="tools")
    for decl in decls:
        if decl.is_write and not decl.approval and decl.capability not in reversible:
            raise CompileError(
                "ZT-TOOL-005",
                f"{decl.name} writes {decl.capability} without @approval; list it under [reversible] in zetra.toml",
            )
        wrapper = _wrap(decl, input_type, bindings)
        metadata: dict[str, Any] = {
            "zetra": {
                "capability": decl.capability,
                "effect": decl.effect,
                "approval": decl.approval,
            }
        }
        config = activity_config(decl, bindings.tool_timeout)
        if config is not None:
            metadata["temporal"] = config
        toolset.add_function(
            wrapper,
            takes_ctx=True,
            name=decl.name,
            requires_approval=decl.approval,
            metadata=metadata,
        )
    return toolset


def _wrap(decl: ToolDecl, input_type: type[Contract], bindings: Bindings) -> Callable[..., Any]:
    function = decl.function
    signature = inspect.signature(function)
    hints = function.__annotations__

    def injected(ctx: RunContext[RunDeps]) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for pname, kind in decl.injects.items():
            values[pname] = (
                input_type.model_validate(ctx.deps.input) if kind == "input" else ctx.deps
            )
        return values

    if not decl.is_write:
        if inspect.iscoroutinefunction(function):

            async def read_async(ctx: RunContext[RunDeps], *args: Any, **kwargs: Any) -> Any:
                return await function(*args, **kwargs, **injected(ctx))

            wrapper: Callable[..., Any] = read_async
        else:

            def read_sync(ctx: RunContext[RunDeps], *args: Any, **kwargs: Any) -> Any:
                return function(*args, **kwargs, **injected(ctx))

            wrapper = read_sync
    else:

        def write(ctx: RunContext[RunDeps], *args: Any, **kwargs: Any) -> dict[str, Any]:
            payload = function(*args, **kwargs, **injected(ctx))
            if not isinstance(payload, Contract):
                raise Denied(f"{decl.name} must return a Contract payload")
            actual = payload_digest(payload)
            if decl.approval:
                if not ctx.tool_call_approved:
                    raise Denied(f"{decl.name} requires approval")
                raw = (ctx.tool_call_metadata or {}).get("grant")
                if raw is None:
                    raise Denied("approved call carries no grant")
                grant = Grant.model_validate(raw)
                if grant.digest != actual:
                    raise Denied(f"{decl.name} rebuilt a different payload than the one approved")
            else:
                grant = Grant(
                    operation_id=f"{ctx.deps.run}:{ctx.tool_call_id or decl.name}",
                    digest=actual,
                    actor="policy:reversible",
                    mode="policy",
                )
            receipt = bindings.receiver(decl.resource).commit(grant, payload)
            return {
                "status": receipt.status,
                "receipt_id": receipt.receipt_id,
                "digest": receipt.digest,
            }

        wrapper = write

    params = [
        inspect.Parameter("ctx", inspect.Parameter.POSITIONAL_ONLY, annotation=RunContext[RunDeps])
    ]
    params += [p for n, p in signature.parameters.items() if n not in decl.injects]
    shaped: Any = wrapper
    shaped.__signature__ = inspect.Signature(params, return_annotation=signature.return_annotation)
    shaped.__annotations__ = {n: t for n, t in hints.items() if n not in decl.injects} | {
        "ctx": RunContext[RunDeps]
    }
    wrapper.__name__ = decl.name
    wrapper.__doc__ = decl.docstring
    return wrapper


def preview_payload(
    decl: ToolDecl, args: dict[str, Any], deps: RunDeps, input_type: type[Contract]
) -> Contract:
    """Run the pure payload builder outside the run, to show an approver exactly what will be committed."""
    values = dict(args)
    for pname, kind in decl.injects.items():
        values[pname] = input_type.model_validate(deps.input) if kind == "input" else deps
    payload = decl.function(**values)
    if not isinstance(payload, Contract):
        raise Denied(f"{decl.name} must return a Contract payload")
    return payload
