import struct

import pytest

from runner.conformance_transport_capture import analyze_pcap

POD = "10.42.1.17"
GW = "10.43.250.10"


def packet(src, dst, sp, dp, seq, flags, payload=b""):
    tcp = struct.pack("!HHIIBBHHH", sp, dp, seq, 0, 5 << 4, flags, 65535, 0, 0) + payload
    ip = struct.pack(
        "!BBHHHBBH4s4s",
        0x45,
        0,
        20 + len(tcp),
        0,
        0,
        64,
        6,
        0,
        bytes(map(int, src.split("."))),
        bytes(map(int, dst.split("."))),
    )
    return b"\0" * 12 + b"\x08\x00" + ip + tcp


def record(kind, data):
    return bytes([kind, 3, 3]) + len(data).to_bytes(2, "big") + data


def handshake(kind):
    return record(22, bytes([kind]) + b"\0\0\1x")


def pcap(rows):
    out = struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)
    for i, row in enumerate(rows):
        out += struct.pack("<IIII", 100 + i, 0, len(row), len(row)) + row
    return out


def wire(extra=b""):
    client = handshake(1) + record(23, b"\x8a\xee" * 12 + extra)
    server = handshake(2) + record(23, b"\xc4\x81" * 12)
    return [
        packet(POD, GW, 49152, 443, 10, 2),
        packet(GW, POD, 443, 49152, 100, 0x12),
        packet(POD, GW, 49152, 443, 11, 0x18, client[:9]),
        packet(POD, GW, 49152, 443, 20, 0x18, client[9:]),
        packet(GW, POD, 443, 49152, 101, 0x18, server),
    ]


def inspect(rows):
    return analyze_pcap(pcap(rows), POD, GW)


def test_complete_tls_streams_with_split_records_have_no_clear_bearer():
    result = inspect(wire())
    assert result["complete"] is True
    assert result["clear_credentials"] is False
    assert result["tls_connections"] == 1
    assert result["packets"] == 5
    assert result["first_packet_at"] == 100 and result["last_packet_at"] == 104
    assert "payload" not in result


@pytest.mark.parametrize(
    "secret",
    [
        b"Authorization: Bearer marker",
        b"Bearer abc.def.ghi",
        b"eyJ" + b"a" * 20 + b"." + b"b" * 20 + b"." + b"c" * 20,
    ],
)
def test_credentials_split_across_tcp_segments_are_detected_without_output(secret):
    rows = wire(secret)
    payload = rows[3][54:]
    cut = max(1, len(payload) - len(secret) // 2)
    rows[3:4] = [
        packet(POD, GW, 49152, 443, 20, 0x18, payload[:cut]),
        packet(POD, GW, 49152, 443, 20 + cut, 0x18, payload[cut:]),
    ]
    result = inspect(rows)
    assert result["clear_credentials"] is True
    assert secret.decode() not in str(result)


def test_missing_syn_or_gap_never_qualifies():
    for rows in [wire()[1:], wire()[:2] + wire()[3:]]:
        assert inspect(rows)["complete"] is False


def test_identical_retransmission_is_valid_but_conflicting_overlap_fails():
    rows = wire()
    rows.insert(4, rows[3])
    assert inspect(rows)["complete"] is True
    rows.insert(4, packet(POD, GW, 49152, 443, 20, 0x18, b"wrong"))
    assert inspect(rows)["complete"] is False


def test_non_tls_plaintext_flow_fails_even_without_credentials():
    rows = wire()
    rows[2:4] = [packet(POD, GW, 49152, 443, 11, 0x18, b"GET / HTTP/1.1\r\n\r\n")]
    assert inspect(rows)["complete"] is False


def test_unexpected_endpoint_and_truncated_packet_are_failures():
    with pytest.raises(ValueError):
        analyze_pcap(pcap(wire() + [packet("10.42.1.18", GW, 12, 443, 0, 2)]), POD, GW)
    with pytest.raises(ValueError):
        analyze_pcap(pcap(wire())[:-2], POD, GW)


def test_packet_and_file_bounds_are_enforced():
    with pytest.raises(ValueError):
        analyze_pcap(b"x" * (32 * 1024 * 1024 + 1), POD, GW)
    with pytest.raises(ValueError):
        analyze_pcap(pcap(wire() * 820), POD, GW)
