"""Zetra agent SDK, viability slice.

An agent is a PydanticAI Agent Spec plus ``contracts.py``, ``tools/`` and ``skills/``.
This package compiles that package into a PydanticAI agent, runs it in-process or on
Temporal, and gives tests one fixture. Everything here is cooperative; see SECURITY.md.
"""

from .contracts import Contract, RunDeps
from .errors import CompileError, Denied
from .gateway import GatewayMCP
from .skills import Skills
from .tools import approval, tool

__all__ = [
    "CompileError",
    "Contract",
    "Denied",
    "GatewayMCP",
    "RunDeps",
    "Skills",
    "approval",
    "tool",
]
