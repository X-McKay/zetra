"""Explicit trusted-host development using the same agent core as durable sessions."""

import asyncio
import ipaddress
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, field_validator, model_serializer

from .bindings import MCPFixtures, mcp_provenance
from .manifest import digest, load_agent, source_digest


class ModelCallLimit(Exception):
    """Local request-count budget exhausted before another provider call."""


class LocalModelConfig(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, hide_input_in_errors=True, validate_default=True
    )
    base_url: str
    model: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
    api_key_env: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    max_tokens: int = Field(default=512, ge=1, le=32768)
    timeout: FiniteFloat = Field(default=20, gt=0, le=120)
    temperature: FiniteFloat = Field(default=0, ge=0, le=2)
    enable_thinking: bool | None = None

    @model_serializer(mode="wrap")
    def serialize_configuration(self, handler):
        data = handler(self)
        # Omission preserves the identity of reports written before this option.
        if self.enable_thinking is None:
            data.pop("enable_thinking", None)
        return data

    @field_validator("base_url")
    @classmethod
    def explicit_endpoint(cls, value):
        url = urlsplit(value)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.port == 0
            or any(c.isspace() for c in value)
        ):
            raise ValueError("model endpoint must be an explicit HTTP(S) URL without credentials")
        try:
            loopback = ipaddress.ip_address(url.hostname).is_loopback
        except ValueError:
            loopback = url.hostname == "localhost"
        if url.scheme == "http" and not loopback:
            raise ValueError("plain HTTP is allowed only for loopback model endpoints")
        return value.rstrip("/")

    def executor(self):
        from ._runtime.chat_model import ChatExecutor

        key = os.environ.get(self.api_key_env) if self.api_key_env else "local-no-auth"
        if not key:
            raise ValueError(f"missing model credential environment variable: {self.api_key_env}")
        return ChatExecutor(
            base_url=self.base_url,
            model=f"openai/{self.model}",
            api_key=key,
            max_tokens=self.max_tokens,
            timeout=self.timeout,
            temperature=self.temperature,
            extra_body={"chat_template_kwargs": {"enable_thinking": self.enable_thinking}}
            if self.enable_thinking is not None
            else None,
        )


def _redact(value):
    if isinstance(value, dict):
        return {
            k: "[redacted]"
            if any(
                word in k.lower()
                for word in ("password", "secret", "token", "api_key", "authorization")
            )
            else _redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, str) and len(value) > 16384:
        return value[:16384] + "[truncated]"
    return value


def _output_path(root, workspace, output):
    if output is None:
        return None
    output = Path(output).absolute()
    if any(p.is_symlink() for p in [output, *output.parents]):
        raise ValueError("output path cannot contain symlinks")
    output = output.resolve()
    for source in (root, workspace):
        if source is not None:
            source = Path(source).resolve()
            if output == source or output in source.parents or source in output.parents:
                raise ValueError("output cannot overlap source or input workspace")
    if output.exists():
        raise ValueError("output already exists")
    return output


async def local_run(
    root,
    message,
    *,
    agent=None,
    model_config=None,
    mcp_fixtures=None,
    timeout=60,
    workspace=None,
    output=None,
    model_call_limit=None,
):
    from ._runtime.process import run_child

    root = Path(root).resolve()
    spec = load_agent(root, agent)
    if not isinstance(message, str) or not message or len(message.encode()) > 65536:
        raise ValueError("input must be nonempty text of at most 65536 bytes")
    if type(timeout) not in (int, float) or not 0 < timeout <= 600:
        raise ValueError("timeout must be between zero and 600 seconds")
    if model_call_limit is not None and (
        type(model_call_limit) is not int or not 1 <= model_call_limit <= 32
    ):
        raise ValueError("model call limit must be an integer between 1 and 32")
    config = LocalModelConfig.model_validate(model_config) if model_config is not None else None
    fixtures = MCPFixtures(mcp_fixtures) if mcp_fixtures is not None else None
    if config is None and spec.model != "fixture":
        raise ValueError("local inference requires an explicit model configuration")
    if workspace is not None:
        workspace = Path(workspace).absolute()
        if any(p.is_symlink() for p in [workspace, *workspace.parents]):
            raise ValueError("input workspace path cannot contain symlinks")
        if not workspace.is_dir():
            raise ValueError("input workspace must be an existing directory")
    output = _output_path(root, workspace, output)
    source = source_digest(root)
    if output:
        output.mkdir(parents=True, mode=0o700)
    request = {
        "root": str(root),
        "message": message,
        "agent": spec.name,
        "model_config": config.model_dump() if config else None,
        "mcp_fixtures": fixtures.config() if fixtures else None,
        "timeout": timeout,
        "workspace": str(Path(workspace).resolve()) if workspace else None,
        "output": str(output) if output else None,
        "model_call_limit": model_call_limit,
    }
    try:
        result = await run_child(
            "zetra.testing.local", request, timeout=timeout + 10, max_output=8_388_608
        )
    except (TimeoutError, RuntimeError) as exc:
        result = {
            "status": "OUTCOME_UNKNOWN",
            "error_type": "ProcessTimeout" if isinstance(exc, TimeoutError) else "ChildFailed",
            "trajectory": [],
            "tools": {},
            "files": [],
            "file_contents": {},
            "metrics": {
                "steps": None,
                "duration_ms": None,
                "input_tokens": None,
                "output_tokens": None,
            },
        }
    try:
        changed = source != source_digest(root)
    except (OSError, ValueError):
        changed = True
    if changed:
        result.update(error_type="SourceChanged", integrity_errors=["source_changed_or_unreadable"])
        if result["status"] not in {"OUTCOME_UNKNOWN", "CANCELED", "REMEDIATION_REQUIRED"}:
            result["status"] = "FAILED"
    result.update(
        schema_version=1,
        mode="local" if config else "fixture",
        source_sha256=source,
        agent=spec.name,
        agent_sha256=digest(spec.model_dump()),
        trust="local-unsigned",
        model_config=config.model_dump() if config else {"model": "fixture"},
        model_config_sha256=digest(config.model_dump() if config else {"model": "fixture"}),
        mcp_binding=mcp_provenance(fixtures),
        scope="trusted host development; not isolation or production authorization",
    )
    if output:
        path = output / "report.json"
        with path.open("x", encoding="utf-8") as stream:
            os.chmod(path, 0o600)
            json.dump(result, stream, indent=2, allow_nan=False)
            stream.write("\n")
    return result


async def _run_in_process(
    root,
    message,
    *,
    agent=None,
    model_config=None,
    mcp_fixtures=None,
    timeout=60,
    workspace=None,
    output=None,
    model_call_limit=None,
):
    from google.adk.models.llm_response import LlmResponse

    from ._runtime.artifacts import Receipt
    from ._runtime.definition import Definition
    from ._runtime.dispatch import OutcomeUnknown
    from ._runtime.records import decode_model
    from ._runtime.runtime import Runtime
    from ._runtime.workspace import Workspace

    spec = load_agent(root, agent)
    config = LocalModelConfig.model_validate(model_config) if model_config is not None else None
    fixtures = MCPFixtures(mcp_fixtures) if mcp_fixtures is not None else None
    if config is None and spec.model != "fixture":
        raise ValueError("local inference requires an explicit model configuration")
    binding = (f"openai/{config.model}", config.executor()) if config else None
    sys.path.insert(0, str(Path(root).resolve() / "src"))
    started = time.perf_counter()
    events = []
    report = {"status": "FAILED", "trajectory": events, "files": [], "file_contents": {}}
    input_tokens, output_tokens = [], []
    visible_text = ""
    model_calls = 0
    try:
        with tempfile.TemporaryDirectory(prefix="zetra-local-") as temp:
            definition = Definition(
                spec.model_dump(),
                Path(temp) / "workspace",
                model_binding=binding,
                mcp_binding=fixtures.call if fixtures else None,
            )
            report["tools"] = definition.tools
            runtime = Runtime(
                temp,
                session="local",
                manifest=digest(
                    {
                        "source": source_digest(root),
                        "model": config.model_dump() if config else "fixture",
                        "mcp_binding": mcp_provenance(fixtures),
                    }
                ),
                key=os.urandom(32),
                message=message,
                definition=definition,
            )
            try:
                if workspace:
                    snapshot = Workspace(Path(workspace), runtime.store).capture()
                    runtime.workspace.restore(snapshot)
                position = runtime.tape.empty(runtime.workspace.capture())
                async with asyncio.timeout(timeout):
                    for _ in range(65):
                        command = {
                            "binding": {"session": "local"},
                            "position": position.model_dump(),
                            "pending": None,
                        }
                        advanced = await runtime.handle(runtime.authority.sign("advance", command))
                        if advanced["status"] == "done":
                            report.update(status="SUCCEEDED", result={"final_text": visible_text})
                            break
                        op = advanced["operation"]
                        if op["kind"] == "model":
                            if model_call_limit is not None and model_calls >= model_call_limit:
                                raise ModelCallLimit
                            model_calls += 1
                            # A dispatched request can consume tokens even if its response is
                            # lost. Keep incomplete usage unknown, not a partial known total.
                            input_tokens.append(None)
                            output_tokens.append(None)
                        event = {
                            "index": op["index"],
                            "kind": op["kind"],
                            "executor": op["executor"],
                            "status": "outcome_unknown",
                        }
                        if op["kind"] == "tool":
                            event["arguments"] = _redact(op["request"]["arguments"])
                        events.append(event)
                        executed = await runtime.handle(
                            runtime.authority.sign(
                                "execute",
                                {**command, "operation": op, "attempt_id": str(position.cursor)},
                            )
                        )
                        position = Receipt.model_validate(executed["receipt"]).position
                        outcome = runtime.tape.records(position)[-1]["outcome"]
                        event["status"] = outcome["status"]
                        if op["kind"] == "tool":
                            event["result"] = _redact(outcome["value"])
                        else:
                            response = decode_model(LlmResponse, outcome["value"])
                            visible_text = (
                                "".join(
                                    p.text or ""
                                    for p in (response.content.parts or [])
                                    if not p.thought
                                )
                                if response.content
                                else ""
                            )
                            usage = response.usage_metadata
                            input_tokens[-1] = usage.prompt_token_count if usage else None
                            output_tokens[-1] = usage.candidates_token_count if usage else None
                    else:
                        raise ValueError("local boundary limit")
                total = 0
                for path in sorted(runtime.workdir.rglob("*")):
                    if path.is_file():
                        name = path.relative_to(runtime.workdir).as_posix()
                        report["files"].append(name)
                        if path.stat().st_size <= 65536 and total + path.stat().st_size <= 262144:
                            try:
                                content = path.read_text(encoding="utf-8")
                            except UnicodeError:
                                continue
                            report["file_contents"][name] = content
                            total += path.stat().st_size
                if output:
                    destination = Path(output) / "workspace"
                    destination.mkdir(mode=0o700)
                    Workspace(destination, runtime.store).restore(runtime.workspace.capture())
            except (OutcomeUnknown, TimeoutError) as exc:
                report.update(status="OUTCOME_UNKNOWN", error_type=type(exc).__name__)
                if events and events[-1]["kind"] == "model":
                    from ._runtime.model_diagnostics import model_failure_evidence

                    report["diagnostic"] = model_failure_evidence(exc, events[-1]["index"])
            except Exception as exc:  # noqa: BLE001 -- execution boundary, never serialize secrets
                report.update(status="FAILED", error_type=type(exc).__name__)
            finally:
                await runtime.close()
    finally:
        sys.path.pop(0)
    report["metrics"] = {
        "duration_ms": (time.perf_counter() - started) * 1000,
        "steps": sum(e["status"] != "outcome_unknown" for e in events),
        "input_tokens": sum(input_tokens) if input_tokens and None not in input_tokens else None,
        "output_tokens": sum(output_tokens)
        if output_tokens and None not in output_tokens
        else None,
    }
    return report
