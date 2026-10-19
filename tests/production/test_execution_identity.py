import pytest
from temporalio.api.common.v1 import WorkflowType
from temporalio.api.history.v1 import HistoryEvent, WorkflowExecutionStartedEventAttributes
from temporalio.converter import DataConverter

from zetra.session import SessionHandle, execution_session, load_execution


@pytest.fixture
def handle():
    return SessionHandle(
        namespace="default", workflow_id="session", run_id="run-1", runtime_id="lab"
    )


def test_exact_execution_separates_workspaces(handle):
    assert execution_session(handle) != execution_session(
        handle.model_copy(update={"run_id": "run-2"})
    )
    assert execution_session(handle) != execution_session(
        handle.model_copy(update={"namespace": "other"})
    )


@pytest.mark.parametrize(
    "lineage",
    [{"original_execution_run_id": "previous"}, {"continued_execution_run_id": "previous"}],
)
async def test_rejects_reset_and_continued_execution(handle, lineage):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    attrs = WorkflowExecutionStartedEventAttributes(
        workflow_type=WorkflowType(name="SessionWorkflow"), **lineage
    )
    history = SimpleNamespace(
        events=[HistoryEvent(workflow_execution_started_event_attributes=attrs)]
    )
    temporal = Mock(namespace="default", data_converter=DataConverter.default)
    temporal.get_workflow_handle.return_value.fetch_history = AsyncMock(return_value=history)
    with pytest.raises(ValueError, match="lineage"):
        await load_execution(temporal, handle)


async def test_rejects_wrong_workflow_type_before_decoding(handle):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    attrs = WorkflowExecutionStartedEventAttributes(workflow_type=WorkflowType(name="Other"))
    temporal = Mock(namespace="default", data_converter=DataConverter.default)
    temporal.get_workflow_handle.return_value.fetch_history = AsyncMock(
        return_value=SimpleNamespace(
            events=[HistoryEvent(workflow_execution_started_event_attributes=attrs)]
        )
    )
    with pytest.raises(ValueError, match="workflow type"):
        await load_execution(temporal, handle)
