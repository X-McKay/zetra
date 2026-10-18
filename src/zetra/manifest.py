"""Strict data-only metadata. This module never imports agent code."""

import hashlib
import json
import re
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

    def validate_policy(self):
        if (self.model == "vllm") != (self.policy == "vllm-chat"):
            raise ValueError("model and reviewed policy do not match")
        return self


class Release(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    agent: AgentSpec
    image: str
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    policy_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    sdk: Literal["0.1.0"] = "0.1.0"

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


def load_agent(root):
    root = Path(root)
    path = root / "agent.yaml"
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
    return AgentSpec.model_validate(
        yaml.load(path.read_text(), Loader=UniqueLoader)
    ).validate_policy()


def source_digest(root):
    root = Path(root).resolve()
    h = hashlib.sha256()
    excluded = {".git", ".venv", ".zetra", "__pycache__", ".pytest_cache", "dist", "build"}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(p in excluded or p.endswith(".egg-info") for p in rel.parts):
            continue
        if path.is_symlink():
            raise ValueError("agent packages cannot contain symlinks")
        if path.is_file():
            if path.stat().st_size > 20_000_000:
                raise ValueError("package file too large")
            name = rel.as_posix().encode("utf-8")
            h.update(
                len(name).to_bytes(8, "big") + name + hashlib.sha256(path.read_bytes()).digest()
            )
    return h.hexdigest()


def load_release(path):
    release = Release.model_validate_json(Path(path).read_text())
    release.agent.validate_policy()
    return release
