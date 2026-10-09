import copy
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from zetra.analysis import risk, scan
from zetra.cli import init, main
from zetra.deployment import render
from zetra.evidence import evaluate, fingerprint, validate_evidence
from zetra.manifest import ContractError, Manifest, load
from zetra.profiling import profile
from zetra.runtime import ActionStore, Denied, RuntimeDependencies

REPO = Path(__file__).resolve().parents[1]
IMAGE = "registry.invalid/zetra@sha256:" + "a" * 64


class ToolkitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "agent"
        init(self.root, "test-agent")
        self.manifest = load(self.root)

    def test_manifest_rejects_missing_unknown_duplicates_types(self):
        for mutate in (
            lambda x: x["spec"].update({"hiddenApprovalBypass": True}),
            lambda x: x["metadata"].pop("owner"),
            lambda x: x["spec"]["budgets"].update({"maxSteps": True}),
            lambda x: x["spec"]["budgets"].update({"maxCostUsd": float("nan")}),
            lambda x: x["spec"].update({"capabilities": ["knowledge.read", "knowledge.read"]}),
            lambda x: x["spec"].update({"capabilities": [123]}),
        ):
            raw = copy.deepcopy(self.manifest.raw)
            mutate(raw)
            with self.assertRaises(ContractError):
                Manifest.parse(raw)
        with (self.root / "agent.yaml").open("a") as f:
            f.write("kind: Agent\n")
        with self.assertRaises(ContractError):
            load(self.root)

    def test_static_check_never_executes_and_detects_alias(self):
        source = self.root / "src" / "knowledge" / "factory.py"
        marker = self.root / "marker"
        source.write_text(
            f"from subprocess import run as launch\nlaunch(['touch', {str(marker)!r}])\n"
        )
        findings = scan(self.root, self.manifest)
        self.assertIn("process.exec", findings[0]["message"])
        self.assertFalse(marker.exists())

    def test_unknown_is_high_risk_and_writes_require_nonaverage_gates(self):
        raw = copy.deepcopy(self.manifest.raw)
        raw["spec"]["dataClassification"] = "unknown"
        self.assertEqual(risk(Manifest.parse(raw))["tier"], "T3")
        raw["spec"]["dataClassification"] = "public"
        raw["spec"]["capabilities"] = ["ticket.write"]
        report = risk(Manifest.parse(raw))
        self.assertEqual(report["tier"], "T2")
        self.assertIn("approval.bound", report["requiredScenarios"])
        self.assertIn("idempotency.retry", report["requiredScenarios"])
        raw["spec"]["autonomy"] = "autonomous"
        self.assertEqual(risk(Manifest.parse(raw))["tier"], "T3")

    def test_actual_scenarios_execute_and_hash_binds(self):
        evidence = evaluate(self.root, self.manifest)
        self.assertEqual(evidence["status"], "passed")
        self.assertEqual(len(evidence["scenarios"]), 5)
        validate_evidence(self.root, self.manifest, evidence)
        (self.root / "src" / "knowledge" / "factory.py").write_text("# changed\n")
        with self.assertRaisesRegex(ContractError, "stale"):
            validate_evidence(self.root, self.manifest, evidence)

    def test_gate_rejects_missing_duplicate_stale_and_failed_evidence(self):
        good = evaluate(self.root, self.manifest)
        for mutate in (
            lambda x: x["scenarios"].pop(),
            lambda x: x["scenarios"].append(x["scenarios"][0]),
            lambda x: x["scenarios"][0].update({"status": "failed"}),
            lambda x: x.update({"createdAt": (datetime.now(UTC) - timedelta(days=2)).isoformat()}),
            lambda x: x.update({"createdAt": (datetime.now(UTC) + timedelta(days=1)).isoformat()}),
            lambda x: x.update({"sourceHash": "forged"}),
        ):
            bad = copy.deepcopy(good)
            mutate(bad)
            with self.assertRaises(ContractError):
                validate_evidence(self.root, self.manifest, bad)

    def test_dependency_and_binary_prompt_edits_invalidate_evidence(self):
        before = fingerprint(self.root)
        (self.root / "uv.lock").write_text("dependency = 'changed'\n")
        self.assertNotEqual(before, fingerprint(self.root))
        before = fingerprint(self.root)
        (self.root / "src" / "prompt.asset").write_bytes(b"\x01changed instructions")
        self.assertNotEqual(before, fingerprint(self.root))
        (self.root / "src" / "escape").symlink_to(self.root / "agent.yaml")
        with self.assertRaises(ContractError):
            fingerprint(self.root)

    def test_skipped_scenarios_are_failures(self):
        path = self.root / "tests" / "test_cases.py"
        path.write_text(
            path.read_text().replace(
                "def test_stop(self):",
                '@unittest.skip("not implemented")\n    def test_stop(self):',
            )
        )
        evidence = evaluate(self.root, self.manifest)
        self.assertEqual(evidence["status"], "failed")
        with self.assertRaises(ContractError):
            validate_evidence(self.root, self.manifest, evidence)

    def test_filenames_do_not_count_as_evaluation_coverage(self):
        (self.root / "evals" / "scenarios.json").write_text('{"scenarios": {}}')
        (self.root / "tests" / "approval.bound.py").write_text("# not a test")
        with self.assertRaisesRegex(ContractError, "Uncovered"):
            evaluate(self.root, self.manifest)

    def test_profile_exact_workload_scope_candidate_only(self):
        fixture = Path(self.tmp.name) / "events.jsonl"
        events = [
            {
                "agent": "test-agent",
                "image": IMAGE,
                "container": "agent",
                "kind": "exec",
                "binary": "/usr/local/bin/python",
            },
            {
                "agent": "test-agent",
                "image": IMAGE,
                "container": "sidecar",
                "kind": "exec",
                "binary": "/bin/sh",
            },
            {
                "agent": "other-agent",
                "image": IMAGE,
                "container": "agent",
                "kind": "exec",
                "binary": "/bin/sh",
            },
        ]
        fixture.write_text("\n".join(json.dumps(e) for e in events))
        candidate = profile(fixture, "test-agent", IMAGE)
        self.assertFalse(candidate["authorizes"])
        self.assertEqual(candidate["processes"], ["/usr/local/bin/python"])
        self.assertEqual(candidate["excludedEvents"], 2)
        with self.assertRaises(ContractError):
            profile(fixture, "nonexistent", IMAGE)
        fixture.write_text(
            '{"agent":"other","agent":"test-agent","image":'
            + json.dumps(IMAGE)
            + ',"container":"agent","kind":"exec","binary":"/bin/sh"}'
        )
        with self.assertRaisesRegex(ContractError, "Duplicate"):
            profile(fixture, "test-agent", IMAGE)

    def test_profile_denied_events_never_become_access_candidates(self):
        fixture = Path(self.tmp.name) / "denied-events.jsonl"
        scope = {"agent": "test-agent", "image": IMAGE, "container": "agent"}
        events = [
            {
                **scope,
                "kind": "file",
                "path": "/tmp/approved",
                "mode": "read",
                "outcome": "allowed",
            },
            {
                **scope,
                "kind": "file",
                "path": "/tmp/sensitive",
                "mode": "read",
                "outcome": "denied",
            },
            {
                **scope,
                "kind": "connect",
                "address": "attacker.invalid",
                "port": 443,
                "outcome": "denied",
            },
            {**scope, "kind": "exec", "binary": "/bin/sh", "outcome": "denied"},
        ]
        fixture.write_text("\n".join(json.dumps(e) for e in events))
        candidate = profile(fixture, "test-agent", IMAGE)
        self.assertEqual(candidate["files"], [{"path": "/tmp/approved", "mode": "read"}])
        self.assertEqual(candidate["processes"], [])
        self.assertEqual(candidate["network"], [])
        self.assertEqual(candidate["deniedEvents"], 3)
        self.assertEqual(len(candidate["deniedObservations"]), 3)
        self.assertFalse(candidate["authorizes"])
        events[0]["outcome"] = "assumed-safe"
        fixture.write_text("\n".join(json.dumps(e) for e in events))
        with self.assertRaisesRegex(ContractError, "Unsupported normalized outcome"):
            profile(fixture, "test-agent", IMAGE)

    def test_deployment_constraints_and_withheld_start(self):
        evidence = evaluate(self.root, self.manifest)
        docs = render(
            self.root,
            self.manifest,
            evidence,
            image=IMAGE,
            namespace="agents",
            gateway_namespace="gateway",
            gateway_label="app=agentgateway",
        )
        self.assertEqual(
            [d["kind"] for d in docs],
            ["ServiceAccount", "NetworkPolicy", "NetworkPolicy", "Deployment"],
        )
        dep = docs[-1]
        self.assertEqual(dep["spec"]["replicas"], 0)
        pod = dep["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        context = pod["containers"][0]["securityContext"]
        self.assertTrue(context["readOnlyRootFilesystem"])
        self.assertFalse(context["allowPrivilegeEscalation"])
        self.assertEqual(context["capabilities"]["drop"], ["ALL"])
        self.assertEqual(docs[1]["spec"]["egress"], [])
        peer = docs[2]["spec"]["egress"][0]["to"][0]
        self.assertIn("namespaceSelector", peer)
        self.assertIn("podSelector", peer)
        with self.assertRaises(ContractError):
            render(
                self.root,
                self.manifest,
                evidence,
                image="registry.invalid/image:latest",
                namespace="agents",
                gateway_namespace="gateway",
                gateway_label="app=gateway",
            )

    def test_cli_fail_closed_on_malformed_manifest(self):
        (self.root / "agent.yaml").write_text("spec: null")
        with patch("sys.stderr"):
            self.assertEqual(main(["check", str(self.root)]), 1)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.store = ActionStore(self.path / "receiver.db")
        self.payload = {"title": "Approved task", "body": "Perform one local action"}

    def test_approval_exact_binding_expiry_single_use_and_retry(self):
        token = self.store.issue(self.payload, actor="alice")
        for actor, payload, approval in (
            ("bob", self.payload, token),
            ("alice", {"title": "changed", "body": "x"}, token),
            ("alice", self.payload, "forged"),
        ):
            with self.assertRaises(Denied):
                self.store.commit_ticket(
                    payload, actor=actor, approval=approval, idempotency_key="key"
                )
        first = self.store.commit_ticket(
            self.payload, actor="alice", approval=token, idempotency_key="key"
        )
        restarted = ActionStore(self.path / "receiver.db")
        self.assertEqual(
            first,
            restarted.commit_ticket(
                self.payload, actor="alice", approval=token, idempotency_key="key"
            ),
        )
        with self.assertRaises(Denied):
            restarted.commit_ticket(
                self.payload, actor="alice", approval=token, idempotency_key="other-key"
            )
        other = self.store.issue(self.payload, actor="alice")
        with self.store.connect() as conn:
            conn.execute("UPDATE grants SET expires=0 WHERE token=?", (other,))
        with self.assertRaises(Denied):
            self.store.commit_ticket(
                self.payload, actor="alice", approval=other, idempotency_key="expired"
            )
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM tickets").fetchone()[0], 1)

    def test_deny_cost_step_deadline_and_stop(self):
        deps = RuntimeDependencies(frozenset({"knowledge.read"}), 2, 30, 0.05, self.path / "STOP")
        for cost in (float("nan"), float("inf"), -1, True):
            with self.assertRaises(Denied):
                deps.require("knowledge.read", cost)
        with self.assertRaises(Denied):
            deps.require("process.exec")
        deps.require("knowledge.read", 0.04)
        with self.assertRaises(Denied):
            deps.require("knowledge.read", 0.02)
        deps._started -= 31
        with self.assertRaises(Denied):
            deps.require("knowledge.read")
        deps._started += 31
        deps.stop_file.touch()
        with self.assertRaises(Denied):
            deps.require("knowledge.read")


class TemporalContractTests(unittest.TestCase):
    def test_recorded_live_history_replays(self):
        try:
            from temporalio.client import WorkflowHistory
            from temporalio.worker import Replayer

            from zetra.temporal_workflow import AgentWorkflow
        except ImportError:
            self.skipTest("Install optional temporal extra to replay live-test history")
        import asyncio

        history = WorkflowHistory.from_json(
            "zetra-live-local-retry",
            (REPO / "tests" / "fixtures" / "temporal-readonly-history.json").read_text(),
        )

        async def replay():
            await Replayer(workflows=[AgentWorkflow]).replay_workflow(history)

        asyncio.run(replay())

    def test_sdk_contract_imports_and_write_worker_denied(self):
        try:
            from temporalio import workflow

            from zetra.temporal_worker import AgentActivities
            from zetra.temporal_workflow import AgentWorkflow
        except ImportError:
            self.skipTest("Install optional temporal extra to validate SDK contract")
        self.assertIsNotNone(workflow._Definition.must_from_class(AgentWorkflow))
        with self.assertRaisesRegex(ContractError, "write-capable"):
            AgentActivities(REPO / "examples" / "action", {})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "agent"
            init(root, "worker-test")
            import yaml

            raw = load(root).raw
            for cap in ("network.http", "process.exec", "custom.read"):
                raw["spec"]["capabilities"] = [cap]
                (root / "agent.yaml").write_text(yaml.safe_dump(raw))
                with self.assertRaisesRegex(ContractError, "Only knowledge.read"):
                    AgentActivities(root, {})


if __name__ == "__main__":
    unittest.main()
