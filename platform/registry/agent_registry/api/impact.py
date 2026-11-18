"""The check a pull request on the cluster repository asks for when a tool server's contract
changes (`contract/dependencies.md` section 3.3). Caller: `registry-reader` or
`registry-operator`. It writes nothing."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from agent_registry.api.auth import reader
from agent_registry.api.deps import get_store
from agent_registry.models import Record
from agent_registry.rules import impact
from agent_registry.rules.impact import Impact
from agent_registry.store import Store

router = APIRouter(prefix="/v1", tags=["checks"], dependencies=[Depends(reader)])

StoreDep = Annotated[Store, Depends(get_store)]


class ToolContractCheckIn(Record):
    cluster: str
    server: str
    contract: dict[str, Any]


@router.post("/checks/tool-contract", response_model=Impact)
def tool_contract(store: StoreDep, body: ToolContractCheckIn) -> Impact:
    """Which agents a proposed contract for a tool server breaks. `not-found` for an unknown
    cluster or server; `invalid-arguments` when the contract is not one for that server."""
    return impact.tool_contract(store, body.cluster, body.server, body.contract)
