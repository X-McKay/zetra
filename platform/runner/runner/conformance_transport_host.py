"""T27 host-only capture helper. Never run without explicit operator approval.

prepare reads exact Pod/CRI/CNI identities. capture additionally opens one bounded
raw-packet socket through tcpdump. Neither command writes Kubernetes objects;
the operator separately publishes the two private sanitized receipts. No raw
packet bytes, bearer tokens, key material or arbitrary command errors are printed.
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import resource
import selectors
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from runner.conformance_transport_capture import MAX_BYTES, MAX_PACKETS, analyze_pcap
from runner.conformance_transport_control import (
    GATE_FIELDS,
    capture_filter,
    capture_started,
    validate_gate,
)

ENDPOINT = "unix:///run/k3s/containerd/containerd.sock"


def netns_path(cri):
    rows = cri.get("info", {}).get("runtimeSpec", {}).get("linux", {}).get("namespaces", [])
    paths = [r.get("path") for r in rows if r.get("type") == "network"]
    if (
        len(paths) != 1
        or not isinstance(paths[0], str)
        or not re.fullmatch(r"/var/run/netns/cni-[a-zA-Z0-9-]{1,64}", paths[0])
    ):
        raise ValueError("CRI has no exact CNI network namespace")
    return paths[0]


def bind_interfaces(gate, pod, cri, guest_links, host_links, *, netns_inode, host_name):
    validate_gate(gate)
    meta, spec, status = pod.get("metadata", {}), pod.get("spec", {}), pod.get("status", {})
    labels = meta.get("labels", {})
    if (
        (
            meta.get("name"),
            meta.get("uid"),
            meta.get("namespace"),
            spec.get("nodeName"),
            status.get("podIP"),
        )
        != (gate["pod"], gate["pod_uid"], "tenant-test", gate["node"], gate["pod_ip"])
        or meta.get("deletionTimestamp")
        or status.get("phase") != "Running"
        or spec.get("runtimeClassName") != "kata-qemu-runtime-rs"
        or host_name.casefold() != gate["node"]
    ):
        raise ValueError("host capture Pod identity/node/state changed")
    if any(
        labels.get("agentplatform.io/" + key) != value
        for key, value in {
            "session": gate["session_id"],
            "generation": "1",
            "agent": "conformance",
        }.items()
    ):
        raise ValueError("host capture Pod labels differ from bound T4 session")
    c = cri.get("status", {})
    if (
        c.get("metadata") is None
        or any(c["metadata"].get(k) != meta[k] for k in ("uid", "name", "namespace"))
        or c.get("network", {}).get("ip") != gate["pod_ip"]
        or c.get("state") != "SANDBOX_READY"
        or not re.fullmatch(r"[a-f0-9]{64}", str(c.get("id", "")))
    ):
        raise ValueError("CRI sandbox identity differs from exact Pod")
    path = netns_path(cri)
    guest = [r for r in guest_links if r.get("ifname") == "eth0"]
    if len(guest) != 1:
        raise ValueError("CNI namespace does not have one eth0")
    host = [r for r in host_links if r.get("ifindex") == guest[0].get("link_index")]
    if (
        len(host) != 1
        or host[0].get("link_index") != guest[0].get("ifindex")
        or host[0].get("linkinfo", {}).get("info_kind") != "veth"
        or not re.fullmatch(r"veth[a-zA-Z0-9]{1,11}", str(host[0].get("ifname", "")))
    ):
        raise ValueError("host veth and namespace peer indices are ambiguous")
    if type(netns_inode) is not int or netns_inode <= 0:
        raise ValueError("network namespace inode missing")
    return {
        **gate,
        "sandbox_id": c["id"],
        "netns": path,
        "netns_inode": netns_inode,
        "interface": host[0]["ifname"],
        "ifindex": host[0]["ifindex"],
        "peer_ifindex": guest[0]["ifindex"],
    }


def tcpdump_command(binding, path, *, user):
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user):
        raise ValueError("invalid local capture owner")
    return [
        "tcpdump",
        "-i",
        binding["interface"],
        "-p",
        "-nn",
        "-s",
        "0",
        "-U",
        "-c",
        str(MAX_PACKETS),
        "-Z",
        user,
        "-w",
        str(path),
        capture_filter(binding["pod_ip"]),
    ]


def bounded_command(command, *, soft_seconds=115, kill_after=5):
    # Independent process owns the timer even if this Python helper is SIGKILLed.
    # Defaults guarantee INT at115s and hard kill at120s; CLI cannot widen them.
    return [
        "/usr/bin/timeout",
        "--signal=INT",
        f"--kill-after={kill_after}s",
        f"{soft_seconds}s",
        *command,
    ]


def private_json(path, value, owner=None):
    fd, temp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        if owner is not None:
            os.fchown(fd, owner.pw_uid, owner.pw_gid)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


class Host:
    def __init__(self, kubeconfig):
        self.kubeconfig = str(kubeconfig)

    def command(self, args):
        result = subprocess.run(args, capture_output=True, timeout=5, check=False)
        if result.returncode or len(result.stdout) > 2 * 1024 * 1024:
            raise RuntimeError("host metadata command failed")
        return json.loads(result.stdout)

    def pod(self, gate):
        return self.command(
            [
                "kubectl",
                "--kubeconfig",
                self.kubeconfig,
                "get",
                "pod",
                gate["pod"],
                "-n",
                "tenant-test",
                "-o",
                "json",
            ]
        )

    def binding(self, gate):
        pod = self.pod(gate)
        sandboxes = self.command(
            [
                "crictl",
                "--runtime-endpoint",
                ENDPOINT,
                "pods",
                "--label",
                "io.kubernetes.pod.uid=" + gate["pod_uid"],
                "-o",
                "json",
            ]
        ).get("items", [])
        if len(sandboxes) != 1 or not re.fullmatch(
            r"[a-f0-9]{64}", str(sandboxes[0].get("id", ""))
        ):
            raise ValueError("exact Pod has no unique CRI sandbox")
        cri = self.command(
            ["crictl", "--runtime-endpoint", ENDPOINT, "inspectp", sandboxes[0]["id"]]
        )
        namespace = netns_path(cri)
        inode = os.stat(namespace, follow_symlinks=False)
        if stat.S_ISLNK(inode.st_mode):
            raise ValueError("CNI namespace path is a symlink")
        guest = self.command(["nsenter", "--net=" + namespace, "--", "ip", "-j", "link", "show"])
        host = self.command(["ip", "-d", "-j", "link", "show"])
        return bind_interfaces(
            gate, pod, cri, guest, host, netns_inode=inode.st_ino, host_name=socket.gethostname()
        )


def capture(host, binding, directory, user):
    if os.geteuid() != 0:
        raise PermissionError("capture requires explicitly approved host privilege")
    gate = {k: binding[k] for k in GATE_FIELDS}
    if host.binding(gate) != binding:
        raise ValueError("host binding changed after preparation")
    owner = pwd.getpwnam(user)
    if owner.pw_uid == 0:
        raise ValueError("tcpdump must drop to an unprivileged capture owner")
    directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    os.chown(directory, owner.pw_uid, owner.pw_gid)
    pcap = directory / "packets.pcap"
    fd = os.open(pcap, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.fchown(fd, owner.pw_uid, owner.pw_gid)
    os.close(fd)

    def limits():
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_BYTES, MAX_BYTES))

    process = subprocess.Popen(
        bounded_command(tcpdump_command(binding, pcap, user=user)),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        preexec_fn=limits,
        start_new_session=True,
    )
    log = bytearray()
    ready = None
    reason = "error"
    identity_verified = False
    started = time.monotonic()
    selector = selectors.DefaultSelector()
    selector.register(process.stderr, selectors.EVENT_READ)
    try:
        while time.monotonic() - started < 10:
            if selector.select(timeout=0.1):
                log.extend(os.read(process.stderr.fileno(), 4096))
            if len(log) > 16384 or process.poll() is not None:
                raise RuntimeError("tcpdump failed before ready")
            if ("listening on " + binding["interface"] + ",").encode() in log:
                if host.binding(gate) != binding:
                    raise ValueError("host binding changed before capture ready")
                ready = {
                    **gate,
                    **{
                        k: binding[k]
                        for k in ("interface", "ifindex", "peer_ifindex", "netns_inode")
                    },
                    "started_at": time.time(),
                    "pid": process.pid,
                    "filter": capture_filter(gate["pod_ip"]),
                    "max_seconds": 120,
                    "max_packets": MAX_PACKETS,
                    "max_bytes": MAX_BYTES,
                }
                capture_started(ready, gate)
                private_json(directory / "capture-start.json", ready, owner)
                print(
                    json.dumps(
                        {
                            "phase": "capture_started",
                            "receipt": str(directory / "capture-start.json"),
                            "session_id": gate["session_id"],
                        }
                    ),
                    flush=True,
                )
                break
        if ready is None:
            raise TimeoutError("tcpdump readiness deadline")
        # Root publishes capture-start and creates this private local stop marker
        # at the after-exec gate, while the same Pod is still held Running. No
        # Kubernetes write occurs here; replacement/cleanup cannot be mistaken
        # for a valid endpoint while capture is running.
        next_identity_check = time.monotonic()
        while time.monotonic() - started < 120:
            if process.poll() is not None:
                reason = "unexpected_exit"
                break
            if time.monotonic() >= next_identity_check:
                if host.binding(gate) != binding:
                    raise ValueError("host binding changed during capture")
                next_identity_check = time.monotonic() + 1
            if (directory / "stop").exists():
                reason = "operator_stop"
                break
            time.sleep(0.1)
        else:
            reason = "time_limit"
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGINT)
        try:
            tail = process.communicate(timeout=5)[1]
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            tail = process.communicate(timeout=5)[1]
            reason = "forced_stop"
        selector.close()
        log.extend(tail or b"")
        ended = time.time()
        if reason == "operator_stop":
            try:
                identity_verified = host.binding(gate) == binding
            except Exception:
                identity_verified = False
        stats = {
            name: int(match.group(1)) if (match := re.search(pattern, log)) else None
            for name, pattern in {
                "captured": rb"(?m)^(\d+) packets captured$",
                "received": rb"(?m)^(\d+) packets received by filter$",
                "dropped": rb"(?m)^(\d+) packets dropped by kernel$",
            }.items()
        }
        result = {
            "capture_start": ready,
            "ended_at": ended,
            "stop_reason": reason,
            "returncode": process.returncode,
            "stats": stats,
            "file_mode": stat.S_IMODE(pcap.stat().st_mode),
            "identity_verified_through_stop": identity_verified,
        }
        try:
            result["analysis"] = analyze_pcap(pcap.read_bytes(), gate["pod_ip"])
        except Exception as exc:
            result["analysis_error"] = type(exc).__name__
        result["limits_not_hit"] = (
            reason == "operator_stop"
            and time.monotonic() - started < 120
            and pcap.stat().st_size < MAX_BYTES
            and stats["captured"] is not None
            and stats["captured"] < MAX_PACKETS
        )
        private_json(directory / "capture-complete.json", result, owner)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "capture"))
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--gate", type=Path, required=True)
    parser.add_argument("--binding", type=Path, required=True)
    parser.add_argument("--allow-host-metadata", action="store_true")
    parser.add_argument("--allow-host-capture", action="store_true")
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--owner", default="al")
    args = parser.parse_args()
    if not args.allow_host_metadata or not args.kubeconfig.is_file():
        parser.error("explicit host-metadata approval and kubeconfig required")

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    try:
        gate = validate_gate(json.loads(args.gate.read_text()))
        host = Host(args.kubeconfig)
        if args.action == "prepare":
            if args.binding.exists():
                raise ValueError("use a fresh private binding path")
            private_json(args.binding, host.binding(gate))
            print(json.dumps({"binding": str(args.binding), "capture_authorized": False}))
        else:
            if not args.allow_host_capture or args.directory is None:
                parser.error("explicit host-capture approval and fresh private directory required")
            binding = json.loads(args.binding.read_text())
            if any(binding.get(k) != v for k, v in gate.items()):
                raise ValueError("prepared gate changed")
            capture(host, binding, args.directory, args.owner)
            print(
                json.dumps(
                    {
                        "phase": "capture_complete",
                        "receipt": str(args.directory / "capture-complete.json"),
                    }
                )
            )
    except (Exception, KeyboardInterrupt) as exc:
        print(json.dumps({"error_type": type(exc).__name__}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
