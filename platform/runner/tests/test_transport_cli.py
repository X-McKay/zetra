import pytest

from runner.conformance import parse_args


def test_transport_requires_exclusive_explicit_exact_fixture_and_t4_identity():
    base = [
        "--release",
        "conformance-signed",
        "--tests",
        "T27",
        "--transport-fixture-release",
        "conformance-transport-signed",
        "--observe-host-capture",
        "--session-timeout",
        "900",
    ]
    args = parse_args(base)
    assert args.tests == ["T27"] and args.observe_host_capture
    for changed in [
        base[:6] + base[7:],
        base + ["--tests", "T27,T4"],
        base + ["--tenant", "tenant-prod"],
        base + ["--agent", "investigator"],
        base + ["--session-timeout", "899"],
    ]:
        with pytest.raises(SystemExit):
            parse_args(changed)
