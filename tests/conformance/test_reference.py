from tests.fixtures.cases import FixtureCase
from tests.fixtures.reference import run_native_reference


async def test_native_reference_has_five_boundaries_and_final_callback_state():
    case = FixtureCase()
    result = await run_native_reference(case)
    assert case.effects == ["fixture", "inspect_repository", "fixture", "write_report", "fixture"]
    assert case.trace == [
        "before_model",
        "after_model",
        "before:inspect_repository",
        "after:inspect_repository",
        "before_model",
        "after_model",
        "before:write_report",
        "after:write_report",
        "before_model",
        "after_model",
        "after_agent",
    ]
    assert result["session"]["data"]["state"] == {
        "inspected": True,
        "report_path": "report.md",
        "finished": True,
    }
    assert [e["author"] for e in result["session"]["data"]["events"]].count("user") == 1
    assert result["final_text"] == "Report ready."
