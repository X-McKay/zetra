import os
import signal
import threading

from registry_provisioner.main import main, run_loop
from tests.fakes import FakeRegistry, Stopper


def test_loop_runs_two_passes_then_stops(kube, registry, env):
    stop = Stopper()
    registry.on_desired = lambda n: stop.set() if n == 2 else None
    assert main([], kube=kube, registry=registry, environ=env, stop=stop) == 0
    assert registry.desired_calls == [0, 3]
    assert len(registry.reports) == 2


def test_loop_survives_failed_passes_and_beats(kube, registry, env, tmp_path):
    from registry_provisioner.registry import RegistryError

    stop = Stopper()
    registry.desired_error = RegistryError("down")
    registry.on_desired = lambda n: stop.set() if n == 3 else None
    heartbeat = tmp_path / "heartbeat"
    assert main([], kube=kube, registry=registry, environ=env, stop=stop) == 0
    assert len(registry.desired_calls) == 3 and heartbeat.exists()


def test_heartbeat_touched_by_a_failed_single_pass(kube, registry, env, tmp_path):
    from registry_provisioner.registry import RegistryError

    registry.desired_error = RegistryError("down")
    assert main(["--once"], kube=kube, registry=registry, environ=env) == 1
    assert (tmp_path / "heartbeat").exists()


def test_heartbeat_touched_when_a_pass_raises_unexpectedly(kube, registry, env, tmp_path):
    registry.answer.pop("fieldManager")  # a malformed answer raises inside the pass
    assert main(["--once"], kube=kube, registry=registry, environ=env) == 1
    assert (tmp_path / "heartbeat").exists()


def test_interval_comes_from_settings(kube, registry, env):
    waits = []

    class Recording(Stopper):
        def wait(self, timeout=None):
            waits.append(timeout)
            self.set()
            return True

    stop = Recording()
    main(
        [],
        kube=kube,
        registry=registry,
        environ={**env, "PROVISIONER_INTERVAL_SECONDS": "7"},
        stop=stop,
    )
    assert waits == [7.0]


def test_sigterm_stops_the_loop(kube, env):
    registry = FakeRegistry()
    registry.answer = {
        "cluster": "k", "mode": "normal", "generation": 1, "fieldManager": "m",
        "namespaces": [], "fluxInventories": [], "deployments": [], "observe": [],
    }  # fmt: skip
    registry.on_desired = lambda n: (os.kill(os.getpid(), signal.SIGTERM) if n == 2 else None)
    previous = signal.getsignal(signal.SIGTERM)
    t = threading.Timer(10, lambda: os.kill(os.getpid(), signal.SIGKILL))  # never reached
    t.daemon = True
    t.start()
    code = main(
        [], kube=kube, registry=registry, environ={**env, "PROVISIONER_INTERVAL_SECONDS": "0.01"}
    )
    t.cancel()
    assert code == 0 and len(registry.desired_calls) == 2
    assert signal.getsignal(signal.SIGTERM) is previous


def test_run_loop_checks_the_flag_first():
    stop = Stopper()
    stop.set()
    calls = []
    run_loop(lambda: calls.append(1), 1, stop)
    assert calls == []
