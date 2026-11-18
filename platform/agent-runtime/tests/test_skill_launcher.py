"""The launcher and its supervisor (skills.md section 9), on real processes.

Every script here ends by itself within a few seconds, starts a handful of processes at
most and allocates tens of MiB at most, so a launcher that fails to end it leaves nothing
running for long. Scripts write the pid and start time of each process they start under
`pids/` in the workspace, and every test checks that none of them is still running.
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agent_runtime.skills import format as fmt
from agent_runtime.skills import launcher

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs /proc")

SKILL_MD = "---\nname: demo\ndescription: Runs a test script. Use in tests.\n---\n\nBody.\n"

# Prepended to every script: writes `pids/<pid>` holding the start time of the process.
RECORD = """\
import os, sys, time
def record(pid):
    with open(f"/proc/{pid}/stat") as handle:
        text = handle.read()
    os.makedirs("pids", exist_ok=True)
    with open(f"pids/{pid}.tmp", "w") as handle:
        handle.write(text[text.rfind(")") + 2 :].split()[19])
    os.rename(f"pids/{pid}.tmp", f"pids/{pid}")
record(os.getpid())
record(os.getppid())
open("started", "w").close()
"""

# The script, its supervisor, and a grandchild that detaches with setsid and outlives the
# child that started it. With the argument `wait` the script then waits; else it exits.
TREE = """\
child = os.fork()
if child == 0:
    if os.fork() == 0:
        os.setsid()
        record(os.getpid())
        time.sleep(8)
        os._exit(0)
    os._exit(0)
os.waitpid(child, 0)
deadline = time.monotonic() + 3
while len(os.listdir("pids")) < 3 and time.monotonic() < deadline:
    time.sleep(0.01)
open("ready", "w").close()
if sys.argv[1:] == ["wait"]:
    time.sleep(8)
"""


def make(tmp_path, source, interpreter="python3", sidecar=True):
    """An app directory with the one listed skill `demo` and its script `scripts/run.py`,
    pinned in the lock, and an empty workspace."""
    app = tmp_path / "app"
    directory = app / "skills" / "demo"
    (directory / "scripts").mkdir(parents=True)
    (directory / "SKILL.md").write_text(SKILL_MD)
    (directory / "scripts" / "run.py").write_text(RECORD + source)
    if sidecar:
        (directory / "zetra.yaml").write_text(
            "schema_version: 1\nscripts:\n"
            f"  - path: scripts/run.py\n    interpreter: {interpreter}\n    timeout_seconds: 10\n"
        )
    (app / fmt.LOCK).write_text(fmt.lock_text([fmt.read(directory)]))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return app, workspace


def run(app, workspace, arguments=(), *, script="scripts/run.py", **limits):
    bounds = launcher.Limits(**{"timeout_seconds": 10, **limits})
    return launcher.run(app, workspace, ["demo"], "demo", script, list(arguments), limits=bounds)


def _start_time(pid):
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    fields = text[text.rfind(")") + 2 :].split()
    return None if fields[0] in ("Z", "X") else fields[19]


def alive(workspace):
    """The recorded processes that are still running (same pid, same start time)."""
    pids = workspace / "pids"
    if not pids.is_dir():
        return []
    return [
        int(p.name)
        for p in pids.iterdir()
        if p.name.isdigit() and _start_time(int(p.name)) == p.read_text()
    ]


def children_of_this_process():
    found = []
    for name in os.listdir("/proc"):
        if name.isdigit():
            try:
                text = Path(f"/proc/{name}/stat").read_text()
            except OSError:
                continue
            fields = text[text.rfind(")") + 2 :].split()
            if int(fields[1]) == os.getpid() and fields[0] not in ("Z", "X"):
                found.append(int(name))
    return found


def wait_for(predicate, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


@pytest.fixture(autouse=True)
def nothing_left_behind(tmp_path):
    yield
    workspace = tmp_path / "workspace"
    assert alive(workspace) == []
    assert children_of_this_process() == []


def refused(app, workspace, code, **kwargs):
    result = run(app, workspace, **kwargs)
    assert result.refused == code
    assert result.exit_status is None and result.limits_hit == ()
    assert not (workspace / "started").exists()  # no process was started
    return result


# --- what is run --------------------------------------------------------------------------


def test_arguments_arrive_literally_and_no_shell_reads_them(tmp_path):
    app, workspace = make(tmp_path, "import json\nprint(json.dumps(sys.argv[1:]))\n")
    arguments = ["; touch X", "$(touch Y)", "`touch Z`", "a b", "*", "| touch W", "&& touch V"]
    result = run(app, workspace, arguments)
    assert result.refused is None and result.exit_status == 0, result
    assert json.loads(result.stdout) == arguments
    assert result.arguments == tuple(arguments)
    assert not any((workspace / name).exists() for name in "XYZWV")
    assert not any((tmp_path / name).exists() for name in "XYZWV")


def test_the_script_sees_exactly_the_contract_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("CALLER_SECRET_FOR_TEST", "must-not-leak")
    app, workspace = make(
        tmp_path,
        "import json\n"
        "print(json.dumps({'env': dict(os.environ), 'cwd': os.getcwd(),"
        " 'stdin': sys.stdin.read()}))\n",
    )
    result = run(app, workspace)
    assert result.exit_status == 0, result
    seen = json.loads(result.stdout)
    assert seen["env"] == {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(workspace),
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    assert seen["cwd"] == str(workspace)
    assert seen["stdin"] == ""
    assert result.digest == fmt.digest(app / "skills" / "demo")


def test_output_beyond_a_pipe_is_read_in_full_under_the_bound(tmp_path):
    app, workspace = make(tmp_path, "sys.stdout.write('y' * 12000)\nsys.stderr.write('e')\n")
    result = run(app, workspace)
    assert result.exit_status == 0 and result.limits_hit == ()
    assert result.stdout == "y" * 12000 and result.stderr == "e"


def test_an_exit_status_and_a_signal_are_reported(tmp_path):
    app, workspace = make(tmp_path, "sys.exit(int(sys.argv[1]))\n")
    assert run(app, workspace, ["3"]).exit_status == 3
    app2, workspace2 = make(tmp_path / "b", "import signal\nos.kill(os.getpid(), signal.SIGUSR1)\n")
    assert run(app2, workspace2).exit_status == -signal.SIGUSR1
    assert alive(workspace2) == []


# --- path confinement ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "script", ["../../../etc/passwd", "/etc/passwd", "scripts/../SKILL.md", "scripts//run.py"]
)
def test_a_path_out_of_the_skill_directory_is_refused(tmp_path, script):
    app, workspace = make(tmp_path, "")
    result = refused(app, workspace, "unknown_script", script=script)
    assert result.skill == "demo" and result.script is None


def test_a_scripts_directory_that_links_out_is_refused(tmp_path):
    app, workspace = make(tmp_path, "")
    outside = tmp_path / "outside"
    scripts = app / "skills" / "demo" / "scripts"
    scripts.rename(outside)
    scripts.symlink_to(outside)
    refused(app, workspace, "path")


def test_a_script_that_is_a_symlink_out_is_refused(tmp_path):
    app, workspace = make(tmp_path, "")
    script = app / "skills" / "demo" / "scripts" / "run.py"
    (tmp_path / "outside.py").write_text(RECORD)
    script.unlink()
    script.symlink_to(tmp_path / "outside.py")
    refused(app, workspace, "path")


def test_a_workspace_that_is_not_a_real_directory_is_refused(tmp_path):
    app, workspace = make(tmp_path, "")
    assert run(app, tmp_path / "missing").refused == "workspace"
    (tmp_path / "linked").symlink_to(workspace)
    assert run(app, tmp_path / "linked").refused == "workspace"
    assert not (workspace / "started").exists()


# --- refusals -----------------------------------------------------------------------------


def test_a_skill_changed_after_it_was_pinned_is_refused(tmp_path):
    app, workspace = make(tmp_path, "")
    directory = app / "skills" / "demo"
    pinned = fmt.digest(directory)
    (directory / "scripts" / "run.py").write_text(RECORD + "print('changed')\n")
    result = refused(app, workspace, "digest")
    assert result.digest == fmt.digest(directory) != pinned


def test_a_skill_with_no_sidecar_is_refused(tmp_path):
    app, workspace = make(tmp_path, "", sidecar=False)
    assert refused(app, workspace, "undeclared").script is None


def test_an_interpreter_not_on_the_path_is_refused(tmp_path):
    app, workspace = make(tmp_path, "", interpreter="no-such-interpreter")
    refused(app, workspace, "interpreter")


def test_a_skill_or_script_that_is_not_listed_is_refused(tmp_path):
    app, workspace = make(tmp_path, "")
    result = launcher.run(app, workspace, ["other"], "demo", "scripts/run.py", [])
    assert result.refused == "unknown_skill" and result.skill is None
    assert refused(app, workspace, "unknown_script", script="scripts/other.py").script is None


def test_another_platform_is_refused(tmp_path, monkeypatch):
    app, workspace = make(tmp_path, "")
    monkeypatch.setattr(launcher.sys, "platform", "darwin")
    refused(app, workspace, "unsupported")


@pytest.mark.parametrize("hidden", ["/proc/self/stat", os.devnull])
def test_a_step_that_cannot_read_what_the_supervisor_needs_is_refused(
    tmp_path, monkeypatch, hidden
):
    """Rules on the step (Landlock) that hide /proc or the null device: a refusal, and no
    supervisor is started to fail."""
    app, workspace = make(tmp_path, "")
    real = open

    def denied(path, *args, **kwargs):
        if path == hidden:
            raise PermissionError(13, "Permission denied", path)
        return real(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", denied)
    monkeypatch.setattr(launcher, "_supervise", lambda *a, **k: pytest.fail("supervisor started"))
    refused(app, workspace, "unsupported")


@pytest.mark.parametrize(
    "arguments",
    [
        ["a"] * 33,
        ["x" * 4097],
        ["é" * 2049],  # 4098 bytes
        ["x" * 4000] * 5,
        ["a\x00b"],
        ["\ud800"],
        [1],
        [None],
        [["nested"]],
        "not a list",
        None,
        {"a": "b"},
    ],
)
def test_arguments_out_of_bounds_are_refused(tmp_path, arguments):
    app, workspace = make(tmp_path, "")
    result = launcher.run(app, workspace, ["demo"], "demo", "scripts/run.py", arguments)
    assert result.refused == "arguments" and result.arguments == ()
    assert not (workspace / "started").exists()


def test_arguments_at_the_bounds_are_run(tmp_path):
    app, workspace = make(tmp_path, "print(sum(len(a) for a in sys.argv[1:]))\n")
    result = run(app, workspace, ["x" * 4096] * 4)
    assert result.exit_status == 0 and result.stdout.strip() == "16384"
    assert run(app, workspace, [""] * 32).exit_status == 0


@pytest.mark.parametrize(
    "skill, script, arguments",
    [
        (None, None, None),
        (5, "scripts/run.py", []),
        (["demo"], "scripts/run.py", []),
        ({"demo": 1}, "scripts/run.py", []),
        ("demo", 7, []),
        ("demo", ["scripts/run.py"], []),
        ("demo", {"path": "x"}, []),
        ("x" * 100000, "y" * 100000, ["z"] * 100000),
        ("DEMO", "scripts/run.py", []),
        ("demo\x00", "scripts/run.py\x00", []),
    ],
)
def test_odd_model_input_is_refused_and_never_raises(tmp_path, skill, script, arguments):
    app, workspace = make(tmp_path, "")
    result = launcher.run(app, workspace, ["demo"], skill, script, arguments)
    assert isinstance(result, launcher.Run) and result.refused is not None
    assert not (workspace / "started").exists()


# --- limits -------------------------------------------------------------------------------


def test_time_stops_the_run(tmp_path):
    app, workspace = make(tmp_path, "time.sleep(5)\n")
    result = run(app, workspace, timeout_seconds=1)
    assert result.limits_hit == ("time",) and result.exit_status is None
    assert result.refused is None and 900 <= result.duration_ms < 4000


def test_output_stops_the_run_at_the_first_byte_over(tmp_path):
    app, workspace = make(
        tmp_path, "sys.stdout.write('x' * 5000)\nsys.stdout.flush()\ntime.sleep(5)\n"
    )
    result = run(app, workspace, output_bytes=1000)
    assert result.limits_hit == ("output",) and result.exit_status is None
    assert result.stdout == "x" * 1000 and result.duration_ms < 4000


def test_memory_summed_over_the_tree_stops_the_run(tmp_path):
    app, workspace = make(tmp_path, "data = b'\\x01' * (48 << 20)\ntime.sleep(5)\n")
    result = run(app, workspace, memory_bytes=24 << 20)
    assert result.limits_hit == ("memory",) and result.exit_status is None
    assert result.duration_ms < 4000


def test_tasks_in_the_tree_stop_the_run(tmp_path):
    app, workspace = make(
        tmp_path,
        "import subprocess\n"
        "for _ in range(6):\n"
        "    record(subprocess.Popen(['sleep', '5']).pid)\n"
        "time.sleep(5)\n",
    )
    result = run(app, workspace, processes=4)
    assert result.limits_hit == ("processes",) and result.exit_status is None
    assert result.duration_ms < 4000


# --- the end of a run ---------------------------------------------------------------------


def test_a_process_left_in_the_background_is_ended_when_the_script_exits(tmp_path):
    app, workspace = make(tmp_path, TREE)
    result = run(app, workspace)
    assert result.exit_status == 0 and result.limits_hit == (), result
    assert len(list((workspace / "pids").iterdir())) == 3
    assert alive(workspace) == []


def test_sigterm_to_the_supervisor_ends_the_whole_tree(tmp_path):
    app, workspace = make(tmp_path, TREE)
    results = []
    thread = threading.Thread(target=lambda: results.append(run(app, workspace, ["wait"])))
    thread.start()
    try:
        assert wait_for(lambda: (workspace / "ready").exists(), 10)
        supervisor = _supervisor_of(workspace, os.getpid())
        os.kill(supervisor, signal.SIGTERM)
    finally:
        thread.join(30)
    (result,) = results
    assert result.refused is None and result.exit_status is None and result.limits_hit == ()
    assert result.duration_ms < 6000
    assert alive(workspace) == []


HELPER = """\
import sys
from pathlib import Path
from agent_runtime.skills import launcher
launcher.run(Path(sys.argv[1]), Path(sys.argv[2]), ["demo"], "demo", "scripts/run.py",
             ["wait"], limits=launcher.Limits(timeout_seconds=10))
"""


def test_the_step_dying_ends_the_whole_tree(tmp_path):
    app, workspace = make(tmp_path, TREE)
    step = subprocess.Popen([sys.executable, "-c", HELPER, str(app), str(workspace)], cwd=tmp_path)
    try:
        assert wait_for(lambda: (workspace / "ready").exists(), 10)
        _supervisor_of(workspace, step.pid)
    finally:
        step.kill()
        step.wait()
    assert wait_for(lambda: alive(workspace) == [], 8)


def _supervisor_of(workspace, parent):
    """The supervisor's pid, read from what the script recorded and checked: a child of
    `parent` that runs the supervisor module, with the start time recorded."""
    candidates = [
        int(p.name)
        for p in (workspace / "pids").iterdir()
        if p.name.isdigit()
        and Path(f"/proc/{p.name}/stat").exists()
        and b"agent_runtime.skills.supervisor" in Path(f"/proc/{p.name}/cmdline").read_bytes()
    ]
    (pid,) = candidates
    text = Path(f"/proc/{pid}/stat").read_text()
    assert int(text[text.rfind(")") + 2 :].split()[1]) == parent
    assert _start_time(pid) == (workspace / "pids" / str(pid)).read_text()
    return pid
