"""Egress guard: the sandbox's platform-owned init container (spec 5.5).

Guard mode exits 0 only when the node attestor's annotation is present in the Downward API
file AND the gateway is reachable AND no forbidden connection succeeded; 3 immediately on the
first forbidden connection that succeeds (never retried); 4 when the attestation or the
gateway did not appear within the timeout. Probe failures are diagnostics, not proof: the
attestation is the authority.

Attack mode (T2b) is a measurement, never a gate: it connects to every forbidden address
continuously from the first instruction, holds every connection that succeeds, and reports
as JSON how many succeeded and how long each stayed usable.
"""

import argparse
import ast
import datetime
import errno
import json
import pathlib
import select
import socket
import sys
import time

EXIT_OK, EXIT_LEAK, EXIT_TIMEOUT, EXIT_USAGE = 0, 3, 4, 2


def parse_target(value: str) -> tuple[str, int]:
    host, sep, port = value.strip().rpartition(":")
    if not sep or not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(f"not host:port: {value!r}")
    return host.strip("[]"), int(port)


def parse_targets(value: str) -> list[tuple[str, int]]:
    targets = [parse_target(v) for v in value.split(",") if v.strip()]
    if not targets:
        raise ValueError("no targets")
    return targets


def can_connect(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def read_annotations(path: pathlib.Path) -> dict[str, str]:
    """Downward API annotations file: one key="value" per line, value quoted as Go's %q does.
    Lines that do not parse are ignored."""
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, quoted = line.partition("=")
        if not sep or not quoted.startswith('"'):
            continue
        try:
            value = ast.literal_eval(quoted)
        except (ValueError, SyntaxError):
            continue
        if isinstance(value, str):
            out[key] = value
    return out


def valid_attestation(value: str) -> bool:
    """The attestor writes "<RFC3339>;<chain>"; anything else is not an attestation."""
    stamp, sep, chain = value.partition(";")
    if not sep or not chain.strip():
        return False
    try:
        datetime.datetime.fromisoformat(stamp)
    except ValueError:
        return False
    return True


def attested(path: pathlib.Path, key: str) -> bool:
    try:
        value = read_annotations(path).get(key)
    except (OSError, UnicodeDecodeError):
        return False
    return value is not None and valid_attestation(value)


def guard(a: argparse.Namespace) -> int:
    gateway = parse_target(a.gateway)
    forbidden = parse_targets(a.forbidden)
    attestation = pathlib.Path(a.attestation)
    deadline = time.monotonic() + a.timeout
    while time.monotonic() < deadline:
        for host, port in forbidden:
            if can_connect(host, port, a.connect_timeout):
                print(f"LEAK {host}:{port}", file=sys.stderr, flush=True)
                return EXIT_LEAK  # immediate failure; never retried
        if attested(attestation, a.attestation_key) and can_connect(*gateway, a.connect_timeout):
            print("egress-guard ok: attested, gateway reachable", flush=True)
            return EXIT_OK
        time.sleep(a.interval)
    print("no attestation or gateway unreachable within timeout", file=sys.stderr, flush=True)
    return EXIT_TIMEOUT


def _keepalive(s: socket.socket) -> None:
    """Aggressive keepalive, so a connection the engine starts dropping is noticed in seconds."""
    s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for opt, val in (("TCP_KEEPIDLE", 1), ("TCP_KEEPINTVL", 1), ("TCP_KEEPCNT", 3)):
        if hasattr(socket, opt):
            s.setsockopt(socket.IPPROTO_TCP, getattr(socket, opt), val)


class Held:
    def __init__(self, target: str, s: socket.socket, start: float) -> None:
        self.target, self.sock = target, s
        self.opened = self.last_ok = time.monotonic()
        self.opened_at = round(self.opened - start, 3)
        self.closed: str | None = None

    def check(self) -> None:
        """Usable means: no socket error and not closed by the peer."""
        now = time.monotonic()
        err = self.sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        if err:
            return self.close(f"error:{errno.errorcode.get(err, err)}")
        try:
            readable, _, _ = select.select([self.sock], [], [], 0)
            if readable and self.sock.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b"":
                return self.close("peer_closed")
        except BlockingIOError:
            pass
        except OSError as e:
            return self.close(f"error:{errno.errorcode.get(e.errno, e.errno)}")
        self.last_ok = now

    def close(self, reason: str) -> None:
        self.closed = reason
        self.sock.close()

    def report(self) -> dict:
        return {
            "opened_at_s": self.opened_at,
            "usable_for_s": round(self.last_ok - self.opened, 3),
            "closed": self.closed or "still_open",
        }


def attack(a: argparse.Namespace) -> int:
    forbidden = parse_targets(a.forbidden)
    start = time.monotonic()
    stats = {
        f"{h}:{p}": {"attempts": 0, "successes": 0, "first_success_s": None, "held": []}
        for h, p in forbidden
    }
    held: list[Held] = []
    while time.monotonic() - start < a.duration:
        for host, port in forbidden:
            name = f"{host}:{port}"
            st = stats[name]
            if sum(1 for h in held if h.target == name and h.closed is None) >= a.max_held:
                continue
            st["attempts"] += 1
            try:
                s = socket.create_connection((host, port), timeout=a.connect_timeout)
            except OSError:
                continue
            _keepalive(s)
            st["successes"] += 1
            if st["first_success_s"] is None:
                st["first_success_s"] = round(time.monotonic() - start, 3)
            h = Held(name, s, start)
            held.append(h)
            st["held"].append(h)
        for h in held:
            if h.closed is None:
                h.check()
        time.sleep(a.interval)
    for h in held:
        if h.closed is None:
            h.check()
    targets = []
    for name, st in stats.items():
        conns = [h.report() for h in st.pop("held")]
        targets.append({"target": name, **st, "connections": conns})
    usable = [c["usable_for_s"] for t in targets for c in t["connections"]]
    report = {
        "mode": "attack",
        "duration_s": a.duration,
        "success_count": sum(t["successes"] for t in targets),
        "longest_usable_s": max(usable, default=0.0),
        "targets": targets,
    }
    print(json.dumps(report), flush=True)
    for h in held:
        if h.closed is None:
            h.sock.close()
    return EXIT_OK


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="egress-guard")
    p.add_argument("--attack", action="store_true", help="T2b measurement mode; never a gate")
    p.add_argument("--gateway")  # host:port
    p.add_argument("--forbidden", required=True)  # comma-separated host:port list
    p.add_argument("--attestation")  # Downward API annotations file
    p.add_argument("--attestation-key")
    p.add_argument("--timeout", type=float, default=90)
    p.add_argument("--interval", type=float)  # default 1 s in guard mode, 50 ms in attack mode
    p.add_argument("--connect-timeout", type=float, default=2.0)
    p.add_argument("--duration", type=float, default=120)  # attack mode
    p.add_argument("--max-held", type=int, default=64)  # attack mode, per target
    return p


def main(argv: list[str] | None = None) -> int:
    p = parser()
    a = p.parse_args(argv)
    if a.interval is None:
        a.interval = 0.05 if a.attack else 1.0
    try:
        if a.attack:
            return attack(a)
        if not (a.gateway and a.attestation and a.attestation_key):
            p.error("--gateway, --attestation and --attestation-key are required")
        return guard(a)
    except ValueError as e:
        print(f"egress-guard: {e}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
