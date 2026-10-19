import pytest

from zetra._runtime.transport import Authority, ProtocolError


def test_scoped_authenticated_commands():
    authority = Authority(b"x" * 32, "session-a", generation=1)
    command = authority.sign("advance", {"cursor": 0}, now=100, ttl=10)
    assert authority.verify(command, now=101) == ("advance", {"cursor": 0})
    with pytest.raises(ProtocolError):
        authority.verify(command, now=111)
    with pytest.raises(ProtocolError):
        Authority(b"x" * 32, "session-b", generation=1).verify(command, now=101)
    with pytest.raises(ProtocolError):
        Authority(b"x" * 32, "session-a", generation=2).verify(command, now=101)
    command["body"]["payload"]["cursor"] = 1
    with pytest.raises(ProtocolError):
        authority.verify(command, now=101)


def test_protocol_size_and_command_allowlist():
    authority = Authority(b"x" * 32, "s", generation=1)
    with pytest.raises(ProtocolError):
        authority.sign("shell", {}, now=100)
    with pytest.raises(ProtocolError):
        authority.sign("advance", {"huge": "x" * 262144}, now=100)


def test_temporal_wire_size_checks_converted_unicode_not_only_canonical_json():
    from temporalio.converter import DataConverter

    from zetra._runtime.records import canonical_bytes
    from zetra._runtime.transport import enforce_payload_size

    value = {"text": "界" * 60000}
    assert len(canonical_bytes(value)) < 262144
    with pytest.raises(ProtocolError):
        enforce_payload_size(value, DataConverter.default.payload_converter)
