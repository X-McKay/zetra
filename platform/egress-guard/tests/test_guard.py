import json
import socket
import threading
import time

import pytest

from egress_guard import main as eg

KEY = "agentplatform.io/netpol-attested"
ATTESTED = f'{KEY}="2026-10-30T12:00:00Z;KUBE-POD-FW-ABCDEF0123456789"'


@pytest.fixture
def listener():
    """A local TCP listener that accepts and holds connections."""
    srv = socket.create_server(("127.0.0.1", 0))
    held = []

    def accept():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            held.append(conn)

    threading.Thread(target=accept, daemon=True).start()
    yield f"127.0.0.1:{srv.getsockname()[1]}"
    srv.close()
    for c in held:
        c.close()


@pytest.fixture
def closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens: connections are refused at once
    return f"127.0.0.1:{port}"


def annotations(tmp_path, *lines):
    path = tmp_path / "annotations"
    path.write_text("\n".join(lines) + "\n")
    return str(path)


def run(gateway, forbidden, attestation, timeout=3):
    return eg.main(
        [
            "--gateway",
            gateway,
            "--forbidden",
            forbidden,
            "--attestation",
            attestation,
            "--attestation-key",
            KEY,
            "--timeout",
            str(timeout),
            "--interval",
            "0.1",
            "--connect-timeout",
            "0.5",
        ]
    )


def test_t2a_exit_0_when_attested_and_gateway_reachable(tmp_path, listener, closed_port):
    path = annotations(tmp_path, 'agentplatform.io/policy-digest="sha256:9c1e"', ATTESTED)
    assert run(listener, closed_port, path) == 0


def test_t2a_exit_3_immediately_on_a_forbidden_connection(tmp_path, listener):
    path = annotations(tmp_path, ATTESTED)
    t = time.monotonic()
    assert run(listener, f"{listener},127.0.0.1:1", path, timeout=30) == 3
    assert time.monotonic() - t < 2  # no retry, no waiting for the timeout


def test_t2a_leak_wins_even_before_attestation(tmp_path, listener):
    assert run(listener, listener, annotations(tmp_path, ""), timeout=30) == 3


def test_t2a_exit_4_without_attestation(tmp_path, listener, closed_port):
    path = annotations(tmp_path, 'agentplatform.io/policy-digest="sha256:9c1e"')
    t = time.monotonic()
    assert run(listener, closed_port, path, timeout=1) == 4
    assert time.monotonic() - t >= 1


def test_exit_4_when_gateway_unreachable(tmp_path, closed_port):
    assert run(closed_port, "127.0.0.1:1", annotations(tmp_path, ATTESTED), timeout=1) == 4


def test_exit_4_when_attestation_file_missing(tmp_path, listener, closed_port):
    assert run(listener, closed_port, str(tmp_path / "absent"), timeout=1) == 4


def test_attestation_appearing_later_is_picked_up(tmp_path, listener, closed_port):
    path = annotations(tmp_path, 'other="x"')

    def attest():
        time.sleep(0.5)
        (tmp_path / "annotations").write_text(ATTESTED + "\n")  # the kubelet rewrites the file

    threading.Thread(target=attest).start()
    assert run(listener, closed_port, path, timeout=5) == 0


def test_downward_api_format_is_parsed(tmp_path):
    path = tmp_path / "a"
    path.write_text(
        'agentplatform.io/policy-digest="sha256:9c1e"\n'
        'kubectl.kubernetes.io/last-applied-configuration="{\\"a\\":\\"b=c\\"}\\n"\n'
        f'{KEY}-old="2026-10-30T12:00:00Z;KUBE-POD-FW-X"\n'
        "garbage line\n"
        'unicode="caf\\u00e9"\n'
    )
    got = eg.read_annotations(path)
    assert got["agentplatform.io/policy-digest"] == "sha256:9c1e"
    assert got["kubectl.kubernetes.io/last-applied-configuration"] == '{"a":"b=c"}\n'
    assert got["unicode"] == "café"
    assert not eg.attested(path, KEY)  # a key that merely starts with the name does not count


@pytest.mark.parametrize(
    "value,ok",
    [
        ("2026-10-30T12:00:00Z;KUBE-POD-FW-ABC", True),
        ("2026-10-30T12:00:00.123456+00:00;KUBE-POD-FW-ABC", True),
        ("", False),
        ("2026-10-30T12:00:00Z", False),
        ("2026-10-30T12:00:00Z;", False),
        ("yesterday;KUBE-POD-FW-ABC", False),
    ],
)
def test_attestation_value_must_be_timestamp_and_chain(tmp_path, value, ok):
    path = tmp_path / "a"
    path.write_text(f"{KEY}={json.dumps(value)}\n")
    assert eg.attested(path, KEY) is ok


@pytest.mark.parametrize("bad", ["10.43.0.1", "10.43.0.1:", ":443", "h:99999", ""])
def test_bad_targets_are_usage_errors(tmp_path, bad, capsys):
    assert (
        eg.main(
            [
                "--gateway",
                "127.0.0.1:1",
                "--forbidden",
                bad,
                "--attestation",
                "x",
                "--attestation-key",
                KEY,
                "--timeout",
                "1",
            ]
        )
        == 2
    )


def test_ipv6_target():
    assert eg.parse_target("[::1]:443") == ("::1", 443)


def test_t2b_attack_mode_reports_successes_and_usable_window(listener, closed_port, capsys):
    rc = eg.main(
        [
            "--attack",
            "--forbidden",
            f"{listener},{closed_port}",
            "--duration",
            "1",
            "--interval",
            "0.1",
            "--connect-timeout",
            "0.2",
            "--max-held",
            "3",
        ]
    )
    report = json.loads(capsys.readouterr().out)
    assert rc == 0 and report["mode"] == "attack"
    by = {t["target"]: t for t in report["targets"]}
    assert by[closed_port]["successes"] == 0 and by[closed_port]["attempts"] > 0
    open_t = by[listener]
    assert open_t["successes"] == 3 and len(open_t["connections"]) == 3  # held, capped
    assert open_t["first_success_s"] < 0.5
    assert all(c["closed"] == "still_open" for c in open_t["connections"])
    assert report["success_count"] == 3 and report["longest_usable_s"] >= 0.7


def test_t2b_attack_mode_notices_peer_close(closed_port, capsys):
    srv = socket.create_server(("127.0.0.1", 0))
    port = srv.getsockname()[1]

    def accept_then_close():
        conn, _ = srv.accept()
        time.sleep(0.3)
        conn.close()
        srv.close()

    threading.Thread(target=accept_then_close, daemon=True).start()
    eg.main(
        [
            "--attack",
            "--forbidden",
            f"127.0.0.1:{port}",
            "--duration",
            "1",
            "--interval",
            "0.05",
            "--connect-timeout",
            "0.2",
            "--max-held",
            "1",
        ]
    )
    report = json.loads(capsys.readouterr().out)
    (conn, *_) = report["targets"][0]["connections"]
    assert conn["closed"] in ("peer_closed", "error:ECONNRESET")
    assert 0.1 < conn["usable_for_s"] < 0.9
