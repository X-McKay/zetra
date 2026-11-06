import pytest

from runner.conformance import Harness, parse_args


def test_t24_requires_exclusive_explicit_operator_fixture():
    args = parse_args(
        [
            "--release",
            "normal",
            "--tests",
            "T24",
            "--lifecycle-fixture-release",
            "conformance-lifecycle-ready",
            "--observe-lifecycle-faults",
        ]
    )
    assert args.tests == ["T24"]
    for flags in (
        [],
        ["--observe-lifecycle-faults"],
        ["--lifecycle-fixture-release", "fixture"],
        [
            "--lifecycle-fixture-release",
            "fixture",
            "--observe-lifecycle-faults",
            "--tenant",
            "tenant-other",
        ],
        [
            "--lifecycle-fixture-release",
            "fixture",
            "--observe-lifecycle-faults",
            "--tests",
            "T24,T4",
        ],
    ):
        with pytest.raises(SystemExit):
            parse_args(["--release", "normal", "--tests", "T24", *flags])


async def test_t24_dispatches_only_the_explicit_lifecycle_adapter():
    class Adapter:
        async def t24(self):
            return {"id": "T24", "result": "pass"}

    async def logs(_):
        return ""

    harness = Harness(None, "tenant-test", "conformance", "release", logs, lifecycle=Adapter())
    result = await harness.run(["T24"])
    assert result["tests"] == [{"id": "T24", "result": "pass"}]
