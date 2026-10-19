"""Strict data-only metadata. This module never imports agent code."""

import hashlib
import json
import re
import tomllib
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


class AgentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,47}$")
    owner: str = Field(min_length=1, max_length=128)
    entrypoint: str = Field(pattern=r"^[a-zA-Z_]\w*(\.[a-zA-Z_]\w*)*:[a-zA-Z_]\w*$")
    model: Literal["fixture", "vllm"] = "fixture"
    profile: Literal["serial-v1"] = "serial-v1"
    policy: Literal["workspace", "vllm-chat"] = "workspace"
    instructions: str | None = None

    @field_validator("instructions")
    @classmethod
    def valid_instructions(cls, value):
        if value is not None:
            resource_parts(value)
        return value

    def validate_policy(self):
        if (self.model == "vllm") != (self.policy == "vllm-chat"):
            raise ValueError("model and reviewed policy do not match")
        return self


class Release(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[2] = 2
    agent: AgentSpec
    image: str
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    policy_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    sdk: Literal["0.1.0"] = "0.1.0"
    lock_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    sdk_revision: str = Field(pattern=r"^[a-f0-9]{40}$")

    @field_validator("image")
    @classmethod
    def immutable_image(cls, value):
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._:/-]*@sha256:[a-f0-9]{64}", value):
            raise ValueError("image must use repository@sha256 digest")
        return value

    @property
    def identity(self):
        return "zetra-" + digest(self.model_dump())


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def relative_path(value):
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("invalid project path")
    path = Path(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in value.split("/")):
        raise ValueError("invalid project path")
    return path


def resource_parts(value):
    package, sep, resource = value.partition(":")
    if not sep or not re.fullmatch(r"[a-zA-Z_]\w*(\.[a-zA-Z_]\w*)*", package):
        raise ValueError("invalid instruction resource")
    return package, relative_path(resource)


def project_file(root, relative):
    root = Path(root).resolve()
    relative = relative_path(relative)
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("project paths cannot contain symlinks")
    if not current.is_file():
        raise ValueError(f"missing project resource: {relative}")
    return current


def project_agents(root):
    root = Path(root)
    config = tomllib.loads(project_file(root, "pyproject.toml").read_text())
    tool = config.get("tool", {})
    zetra = tool.get("zetra", {}) if isinstance(tool, dict) else {}
    agents = zetra.get("agents", {}) if isinstance(zetra, dict) else {}
    if not isinstance(agents, dict) or not agents:
        raise ValueError("project requires [tool.zetra.agents]")
    for name, path in agents.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", name):
            raise ValueError("invalid agent name")
        relative = relative_path(path)
        if relative.parts[0] != "src" or relative.name != "agent.yaml":
            raise ValueError("manifest path must be src/.../agent.yaml")
    if len(set(agents.values())) != len(agents):
        raise ValueError("duplicate agent manifest path")
    return agents


def agent_manifest(root, agent=None):
    agents = project_agents(root)
    if agent is None:
        if len(agents) != 1:
            raise ValueError("select an agent with --agent")
        agent = next(iter(agents))
    if agent not in agents:
        raise ValueError(f"unknown agent: {agent}")
    return agent, project_file(root, agents[agent])


def load_agent(root, agent=None):
    name, path = agent_manifest(root, agent)
    if path.stat().st_size > 128_000:
        raise ValueError("manifest too large")

    class UniqueLoader(yaml.SafeLoader):
        pass

    def mapping(loader, node):
        result = {}
        for key, value in node.value:
            key = loader.construct_object(key)
            if key in result:
                raise ValueError("duplicate manifest key")
            result[key] = loader.construct_object(value)
        return result

    UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    spec = AgentSpec.model_validate(
        yaml.load(path.read_text(), Loader=UniqueLoader)
    ).validate_policy()
    if spec.name != name:
        raise ValueError("agent name does not match project index")
    if spec.instructions:
        package, resource = resource_parts(spec.instructions)
        project_file(root, (Path("src", *package.split(".")) / resource).as_posix())
    return spec


def source_files(root):
    """Only install inputs enter an image or its identity; evidence stays external."""
    root = Path(root).resolve()
    paths = [root / name for name in ("pyproject.toml", "uv.lock", "README.md", "LICENSE")]
    if (root / "src").is_symlink():
        raise ValueError("agent packages cannot contain symlinks")
    paths += list((root / "src").rglob("*"))
    for path in sorted(paths):
        rel = path.relative_to(root)
        if any(
            p == "__pycache__" or p.startswith(".") or p.endswith(".egg-info") for p in rel.parts
        ):
            continue
        if path.is_symlink():
            raise ValueError("agent packages cannot contain symlinks")
        if path.is_file():
            if path.stat().st_size > 20_000_000:
                raise ValueError("package file too large")
            yield path


def source_digest(root):
    root = Path(root).resolve()
    h = hashlib.sha256()
    for path in source_files(root):
        name = path.relative_to(root).as_posix().encode("utf-8")
        h.update(len(name).to_bytes(8, "big") + name + hashlib.sha256(path.read_bytes()).digest())
    return h.hexdigest()


def load_release(path):
    release = Release.model_validate_json(Path(path).read_text())
    release.agent.validate_policy()
    return release
