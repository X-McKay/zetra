"""Root-only T21 finalizer guard; never imported by harness/worker startup.

Use in the root coordinator after its exact active-harness ready-delete signal.
The coordinator owns kubeconfig authentication, signal freshness, and waiting for
the scheduled-removal signal. The guard independently expires the hold even when
that signal never arrives, and always removes only its exact test finalizer.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

from kubernetes.client.exceptions import ApiException

from runner.activities import pod_name
from runner.conformance_generation_control import FINALIZER, NAMESPACE
from runner.conformance_lifecycle_control import canonical_uuid


class FinalizerGuard:
    def __init__(
        self,
        core,
        control,
        pod_uid,
        worker_uid,
        *,
        hold_seconds=720,
        timer_factory=threading.Timer,
        now_ns=time.monotonic_ns,
        boot_id=None,
    ):
        if type(hold_seconds) is not int or not 30 <= hold_seconds <= 720:
            raise ValueError("T21 finalizer hold must be bounded to 30..720 seconds")
        if (
            getattr(
                getattr(getattr(core, "api_client", None), "configuration", None), "retries", None
            )
            != 0
        ):
            raise ValueError("root T21 guard requires its own API client with retries=0")
        if not canonical_uuid(pod_uid) or not canonical_uuid(worker_uid):
            raise ValueError("root T21 requires exact Pod and worker UUIDs")
        self.core, self.control = core, control
        self.pod_uid, self.worker_uid = pod_uid, worker_uid
        self.session = control.config["sessions"]["blocked_deletion"]
        self.name = pod_name(self.session, 1)
        self.now_ns = now_ns
        self.boot_id = boot_id or Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        self.receipt = {
            "run_id": control.config["run_id"],
            "session_id": self.session,
            "target_uid": pod_uid,
            "finalizer_added": False,
            "finalizer_removed": False,
            "hold_expired": False,
            "operator_errors": [],
        }
        self.lock = threading.Lock()
        self.timer = timer_factory(hold_seconds, self._expire)
        # Keep the owning process alive if __enter__ raises before a context
        # manager can invoke __exit__. Only confirmed cleanup cancels this owner.
        self.timer.daemon = False
        self.error_type = None
        self.add_resource_version = None
        self.cleanup_confirmed = False
        self.persistent_cleanup_failure = False
        self._retry_sleep = time.sleep

    def pod(self):
        pod = self.core.read_namespaced_pod(self.name, "tenant-test", _request_timeout=5)
        labels = pod.metadata.labels or {}
        if pod.metadata.uid != self.pod_uid or any(
            labels.get("agentplatform.io/" + k) != v
            for k, v in {
                "session": self.session,
                "generation": "1",
                "agent": "conformance-checkpoint",
                "release": self.control.config["recovery_release"],
                "tenant": "tenant-test",
                "sandbox": "true",
            }.items()
        ):
            raise ValueError("root T21 finalizer target identity changed")
        return pod

    def patch_finalizers(self, pod, finalizers):
        patch = [
            {"op": "test", "path": "/metadata/uid", "value": self.pod_uid},
            {
                "op": "test",
                "path": "/metadata/resourceVersion",
                "value": pod.metadata.resource_version,
            },
            {"op": "add", "path": "/metadata/finalizers", "value": finalizers},
        ]
        self.core.patch_namespaced_pod(
            self.name,
            "tenant-test",
            patch,
            _content_type="application/json-patch+json",
            _request_timeout=5,
        )

    def write(self, key, value, expected=None):
        if key not in {"receipt-blocked_deletion", "command-blocked_deletion"}:
            raise ValueError("root T21 guard cannot write another control field")
        current = self.control.read()
        raw = current.get("data", {}).get(key)
        if (json.loads(raw) if raw is not None else None) != expected:
            raise ValueError("root T21 control field changed concurrently")
        patch = [
            {"op": "test", "path": "/metadata/uid", "value": self.control.uid},
            {
                "op": "test",
                "path": "/metadata/resourceVersion",
                "value": current["metadata"]["resourceVersion"],
            },
            {"op": "add", "path": "/data/" + key, "value": json.dumps(value, sort_keys=True)},
        ]
        self.core.patch_namespaced_config_map(
            "conformance-generation-control",
            NAMESPACE,
            patch,
            _content_type="application/json-patch+json",
            _request_timeout=5,
        )

    def __enter__(self):
        self.timer.start()  # arm before the first possibly ambiguous mutation
        try:
            pod = self.pod()
            if (
                pod.metadata.deletion_timestamp
                or pod.status.phase != "Running"
                or pod.metadata.finalizers
            ):
                raise ValueError("root T21 needs original Running Pod with no finalizers")
            if (
                self.control.get("command-blocked_deletion") is not None
                or self.control.get("receipt-blocked_deletion") is not None
            ):
                raise ValueError("root T21 requires fresh exact control fields")
            self.add_resource_version = pod.metadata.resource_version
            self.patch_finalizers(pod, [FINALIZER])
            if self.pod().metadata.finalizers != [FINALIZER]:
                raise ValueError("root T21 exact finalizer addition not verified")
            self.receipt["finalizer_added"] = True
            self.write("receipt-blocked_deletion", self.receipt)
            self.write(
                "command-blocked_deletion",
                {
                    "run_id": self.control.config["run_id"],
                    "session_id": self.session,
                    "pod_uid": self.pod_uid,
                    "worker_uid": self.worker_uid,
                    "action": "delete_for_recovery",
                },
            )
            return self
        except BaseException as exc:
            self.receipt["operator_errors"].append(type(exc).__name__)
            self.release()
            raise

    def release(self):
        with self.lock:
            if self.cleanup_confirmed:
                self.timer.cancel()
                return
            if self.add_resource_version is None and not self.receipt["finalizer_added"]:
                self.cleanup_confirmed = True  # no Pod mutation was ever attempted
                self.timer.cancel()
                return
            patch_error = None
            try:
                pod = self.pod()
                finalizers = list(pod.metadata.finalizers or [])
                if FINALIZER not in finalizers:
                    if self.receipt["finalizer_added"]:
                        raise ValueError("T21 finalizer disappeared before guarded removal")
                    if self.add_resource_version is None:
                        self.cleanup_confirmed = True
                        return
                    if pod.metadata.resource_version != self.add_resource_version:
                        # Original JSON Patch tested its old RV, so it cannot
                        # commit after this newer version was observed.
                        self.cleanup_confirmed = True
                        return
                    # An add response may have been lost before application.
                    # Commit our own exact finalizer CAS first, advancing RV so
                    # the earlier unknown request cannot apply after cleanup.
                    self.patch_finalizers(pod, finalizers + [FINALIZER])
                    pod = self.pod()
                    finalizers = list(pod.metadata.finalizers or [])
                    if FINALIZER not in finalizers:
                        raise ValueError("unknown T21 finalizer add did not settle")
                # A lost add response can still have applied this run's finalizer.
                self.receipt["finalizer_added"] = True
                started = self.now_ns()
                try:
                    self.patch_finalizers(pod, [f for f in finalizers if f != FINALIZER])
                except Exception as exc:
                    patch_error = exc
                    self.receipt["operator_errors"].append(type(exc).__name__)
                    # A timeout is not proof of failure; verify the exact old UID.
                    try:
                        if FINALIZER in (self.pod().metadata.finalizers or []):
                            raise
                    except ApiException as exc:
                        if exc.status != 404:
                            raise
                try:
                    if FINALIZER in (self.pod().metadata.finalizers or []):
                        raise ValueError("root T21 finalizer removal not confirmed")
                except ApiException as exc:
                    if exc.status != 404:
                        raise
                self.cleanup_confirmed = True
                old = self.control.finalizer_receipt(self.pod_uid)
                self.receipt.update(
                    finalizer_removed=True,
                    clock_boot_id=self.boot_id,
                    removal_started_ns=started,
                    removal_acknowledged_ns=self.now_ns(),
                )
                self.write("receipt-blocked_deletion", self.receipt, expected=old)
                self.error_type = None
            except Exception as exc:
                if isinstance(exc, ApiException) and exc.status == 404:
                    self.cleanup_confirmed = True  # exact old name is absent
                self.error_type = type(exc).__name__
                if exc is not patch_error:
                    self.receipt["operator_errors"].append(type(exc).__name__)
                raise
            finally:
                if self.cleanup_confirmed:
                    self.timer.cancel()

    def _expire(self):
        with self.lock:
            if self.cleanup_confirmed:
                return
            self.receipt["hold_expired"] = True
        # Every API request is bounded, retries=0. Reconcile fresh exact-UID
        # state on each attempt; a transient failure cannot disarm the owner.
        for attempt in range(3):
            try:
                self.release()
            except Exception:
                if self.cleanup_confirmed:
                    return
            if self.cleanup_confirmed:
                return
            if attempt < 2:
                self._retry_sleep(1)
        self.persistent_cleanup_failure = True
        print(
            json.dumps(
                {
                    "event": "t21_root_persistent_cleanup_failure",
                    "run_id": self.control.config["run_id"],
                    "session_id": self.session,
                    "pod_uid": self.pod_uid,
                    "attempts": 3,
                    "error_type": self.error_type,
                    "action": "root must retain cleanup ownership; qualification failed",
                }
            ),
            file=sys.stderr,
            flush=True,
        )

    def __exit__(self, *_):
        self.release()
        self.timer.cancel()
        if self.timer is not threading.current_thread():
            self.timer.join()
        if self.error_type is not None:
            raise RuntimeError("root T21 guard failed: " + self.error_type)
        if self.receipt["hold_expired"]:
            raise TimeoutError("root T21 finalizer hold expired")
        if self.receipt["operator_errors"]:
            raise RuntimeError("root T21 guard recorded operator errors")
