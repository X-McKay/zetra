"""Bounded classic-pcap inspection; payload and credentials never leave this module.

The operator captures Ethernet on one exact sandbox veth. Missing/ambiguous TCP
bytes, unexpected endpoints, truncation and non-TLS flows are failures. A positive
result is scoped to the retained capture interval, not all possible traffic.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import struct

MAX_BYTES = 32 * 1024 * 1024
MAX_PACKETS = 4096
MAX_STREAM = 8 * 1024 * 1024
CREDENTIAL = re.compile(
    rb"authorization\s*:|bearer\s+|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
    re.IGNORECASE,
)


def packets(raw):
    if not 24 <= len(raw) <= MAX_BYTES:
        raise ValueError("capture size is outside bounds")
    formats = {
        b"\xd4\xc3\xb2\xa1": ("<", 1_000_000),
        b"\xa1\xb2\xc3\xd4": (">", 1_000_000),
        b"\x4d\x3c\xb2\xa1": ("<", 1_000_000_000),
        b"\xa1\xb2\x3c\x4d": (">", 1_000_000_000),
    }
    if raw[:4] not in formats:
        raise ValueError("capture must be classic pcap")
    order, units = formats[raw[:4]]
    major, minor, _, _, snaplen, linktype = struct.unpack(order + "HHIIII", raw[4:24])
    if (major, minor, linktype) != (2, 4, 1) or snaplen < 65535:
        raise ValueError("capture must retain full Ethernet packets")
    offset, count = 24, 0
    while offset < len(raw):
        if len(raw) - offset < 16:
            raise ValueError("truncated capture record")
        seconds, fraction, included, original = struct.unpack(
            order + "IIII", raw[offset : offset + 16]
        )
        offset += 16
        count += 1
        if (
            count > MAX_PACKETS
            or fraction >= units
            or included != original
            or included > snaplen
            or offset + included > len(raw)
        ):
            raise ValueError("capture packet limit or truncation")
        frame = raw[offset : offset + included]
        offset += included
        yield seconds + fraction / units, frame


def tcp(frame):
    if len(frame) < 54 or frame[12:14] != b"\x08\x00":
        raise ValueError("capture contains non-IPv4 Ethernet traffic")
    packet = frame[14:]
    ihl = (packet[0] & 15) * 4
    length = int.from_bytes(packet[2:4], "big")
    fragment = int.from_bytes(packet[6:8], "big")
    if (
        packet[0] >> 4 != 4
        or ihl < 20
        or length < ihl + 20
        or length > len(packet)
        or fragment & 0x3FFF
        or packet[9] != 6
    ):
        raise ValueError("capture has malformed, fragmented or non-TCP IPv4")
    src, dst = str(ipaddress.IPv4Address(packet[12:16])), str(ipaddress.IPv4Address(packet[16:20]))
    segment = packet[ihl:length]
    sp, dp, sequence = struct.unpack("!HHI", segment[:8])
    thl = (segment[12] >> 4) * 4
    if thl < 20 or thl > len(segment):
        raise ValueError("malformed TCP header")
    return src, dst, sp, dp, sequence, segment[13], segment[thl:]


def reassemble(direction):
    syn = direction["syn"]
    if syn is None:
        return None
    segments = sorted(
        (((sequence - syn - 1) % (1 << 32), value) for sequence, value in direction["segments"]),
        key=lambda pair: pair[0],
    )
    result = bytearray()
    for offset, value in segments:
        if offset > len(result) or offset + len(value) > MAX_STREAM:
            return None
        overlap = min(len(result) - offset, len(value))
        if result[offset : offset + overlap] != value[:overlap]:
            return None
        result.extend(value[overlap:])
    return bytes(result)


def tls_stream(raw, hello):
    if not raw:
        return False
    offset, application = 0, False
    handshake = bytearray()
    while offset < len(raw):
        if len(raw) - offset < 5:
            return False
        kind, major, minor = raw[offset : offset + 3]
        size = int.from_bytes(raw[offset + 3 : offset + 5], "big")
        if (
            kind not in (20, 21, 22, 23)
            or major != 3
            or minor not in (1, 2, 3)
            or not 0 < size <= 18432
        ):
            return False
        offset += 5
        if offset + size > len(raw):
            return False
        value = raw[offset : offset + size]
        if kind == 22 and not application:
            handshake.extend(value)
        if kind == 23:
            application = True
        offset += size
    # First handshake must be a complete ClientHello/ServerHello. TLS1.3's
    # encrypted handshake shares type23; T4's actual HTTP success independently
    # establishes that application traffic occurred during this capture.
    return bool(
        application
        and len(handshake) >= 4
        and handshake[0] == hello
        and 0 < int.from_bytes(handshake[1:4], "big") <= len(handshake) - 4
    )


def analyze_pcap(raw, pod_ip, gateway_ip="10.43.250.10"):
    pod_ip, gateway_ip = str(ipaddress.IPv4Address(pod_ip)), str(ipaddress.IPv4Address(gateway_ip))
    if gateway_ip != "10.43.250.10" or pod_ip == gateway_ip:
        raise ValueError("capture endpoint binding is invalid")
    flows, times = {}, []
    ambiguous = False
    for at, frame in packets(raw):
        times.append(at)
        src, dst, sp, dp, sequence, flags, payload = tcp(frame)
        if (src, dst, dp) == (pod_ip, gateway_ip, 443):
            key, direction = sp, "client"
        elif (src, dst, sp) == (gateway_ip, pod_ip, 443):
            key, direction = dp, "server"
        else:
            raise ValueError("capture contains traffic outside exact endpoint binding")
        if key not in flows:
            if len(flows) >= 64:
                raise ValueError("capture connection count exceeds bound")
            flows[key] = {name: {"syn": None, "segments": []} for name in ("client", "server")}
        row = flows[key][direction]
        if flags & 2:
            if row["syn"] not in (None, sequence):
                ambiguous = True
            row["syn"] = sequence
            if payload:  # TCP Fast Open is outside this complete-SYN capture profile.
                ambiguous = True
        if payload:
            row["segments"].append((sequence, payload))
    complete = bool(flows) and not ambiguous
    clear, tls_connections = False, 0
    for flow in flows.values():
        directions = [reassemble(flow[name]) for name in ("client", "server")]
        clear |= any(
            CREDENTIAL.search(stream) is not None for stream in directions if stream is not None
        )
        valid = all(
            stream is not None and tls_stream(stream, hello)
            for stream, hello in zip(directions, (1, 2), strict=True)
        )
        complete &= valid
        tls_connections += int(valid)
    return {
        "complete": complete,
        "clear_credentials": clear,
        "tls_connections": tls_connections,
        "connections": len(flows),
        "packets": len(times),
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "first_packet_at": min(times) if times else None,
        "last_packet_at": max(times) if times else None,
    }
