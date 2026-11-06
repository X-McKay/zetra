import copy

import pytest

from runner.conformance_transport_host import bind_interfaces, tcpdump_command
from tests.test_transport_control import gate


def evidence():
    g = gate()
    pod = {
        "metadata": {
            "uid": g["pod_uid"],
            "name": g["pod"],
            "namespace": "tenant-test",
            "labels": {
                "agentplatform.io/session": g["session_id"],
                "agentplatform.io/generation": "1",
                "agentplatform.io/agent": "conformance",
            },
        },
        "spec": {"nodeName": "rig0", "runtimeClassName": "kata-qemu-runtime-rs"},
        "status": {"phase": "Running", "podIP": g["pod_ip"]},
    }
    cri = {
        "status": {
            "id": "a" * 64,
            "metadata": {"uid": g["pod_uid"], "name": g["pod"], "namespace": "tenant-test"},
            "state": "SANDBOX_READY",
            "network": {"ip": g["pod_ip"]},
        },
        "info": {
            "runtimeSpec": {
                "linux": {"namespaces": [{"type": "network", "path": "/var/run/netns/cni-abc"}]}
            }
        },
    }
    guest = [{"ifname": "eth0", "ifindex": 2, "link_index": 55}]
    host = [
        {"ifname": "vethabcdef", "ifindex": 55, "link_index": 2, "linkinfo": {"info_kind": "veth"}}
    ]
    return g, pod, cri, guest, host


def test_cri_and_both_veth_peer_indices_must_match_exact_pod():
    args = evidence()
    binding = bind_interfaces(*args, netns_inode=4002, host_name="RIG0")
    assert binding["interface"] == "vethabcdef" and binding["ifindex"] == 55
    assert binding["sandbox_id"] == "a" * 64
    assert binding["pod_uid"] == args[0]["pod_uid"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda a: a[1]["metadata"].update(uid="replacement"),
        lambda a: a[1]["spec"].update(nodeName="strix"),
        lambda a: a[2]["status"]["network"].update(ip="10.42.1.18"),
        lambda a: a[2]["info"]["runtimeSpec"]["linux"]["namespaces"][0].update(
            path="/proc/1/ns/net"
        ),
        lambda a: a[3][0].update(link_index=56),
        lambda a: a[4][0].update(link_index=3),
        lambda a: a[4][0].update(ifname="any"),
        lambda a: a[4].append(copy.deepcopy(a[4][0])),
    ],
)
def test_mismatched_or_ambiguous_identity_is_never_captured(mutate):
    args = list(evidence())
    mutate(args)
    with pytest.raises(ValueError):
        bind_interfaces(*args, netns_inode=4002, host_name="rig0")


def test_capture_command_never_uses_any_interface_or_unbounded_capture(tmp_path):
    binding = bind_interfaces(*evidence(), netns_inode=4002, host_name="rig0")
    command = tcpdump_command(binding, tmp_path / "packets.pcap", user="al")
    assert command[:2] == ["tcpdump", "-i"]
    assert command[2] == "vethabcdef"
    assert "-p" in command and "-nn" in command and "-U" in command
    assert command[command.index("-c") + 1] == "4096"
    assert command[-1] == "host 10.42.1.17 and host 10.43.250.10 and tcp port 443"


def test_capture_supervisor_has_independent_120_second_hard_deadline():
    from runner.conformance_transport_host import bounded_command

    assert bounded_command(["tcpdump", "-i", "vethabc"]) == [
        "/usr/bin/timeout",
        "--signal=INT",
        "--kill-after=5s",
        "115s",
        "tcpdump",
        "-i",
        "vethabc",
    ]


def test_killed_helper_cannot_leave_capture_child_running(tmp_path):
    import os
    import subprocess
    import sys
    import time
    from pathlib import Path

    pidfile = tmp_path / "child-pid"
    child = (
        "import os,signal,time; from pathlib import Path; Path("
        + repr(str(pidfile))
        + ").write_text(str(os.getpid())); signal.signal(signal.SIGINT, signal.SIG_IGN); time.sleep(30)"
    )
    helper = (
        "import subprocess,sys,time; from runner.conformance_transport_host import bounded_command; subprocess.Popen(bounded_command([sys.executable,'-c',"
        + repr(child)
        + "],soft_seconds=0.4,kill_after=0.2),start_new_session=True); time.sleep(30)"
    )
    parent = subprocess.Popen([sys.executable, "-c", helper])
    try:
        until = time.monotonic() + 3
        while not pidfile.exists() and time.monotonic() < until:
            time.sleep(0.01)
        assert pidfile.exists()
        pid = int(pidfile.read_text())
        parent.kill()
        parent.wait(timeout=2)
        until = time.monotonic() + 3
        while time.monotonic() < until:
            statfile = Path(f"/proc/{pid}/stat")
            if not statfile.exists() or statfile.read_text().split()[2] == "Z":
                break
            time.sleep(0.02)
        else:
            os.kill(pid, 9)
            pytest.fail("orphan capture survived its independent hard deadline")
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=2)
