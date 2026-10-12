"""Deterministic tests for the agent SDK (layer 0): compile checks, receivers, approval loop.

These run without a model provider or a Temporal server. The durable path is exercised by
``zetra dev`` and recorded in docs/research; it needs the Temporal CLI, which this suite does
not download.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

try:
    import pydantic_ai  # noqa: F401

    HAVE_SDK = True
except ImportError:  # pragma: no cover - the sdk extra is optional
    HAVE_SDK = False

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = REPO / "examples" / "agents"

if HAVE_SDK:
    from pydantic_ai.messages import RetryPromptPart

    from zetra.cli import main
    from zetra.sdk import CompileError, Contract, Denied
    from zetra.sdk.bundle import build, lock_status
    from zetra.sdk.receiver import Grant, MemoryReceiver, SqliteReceiver
    from zetra.sdk.spec import load_spec
    from zetra.sdk.testing import AgentUnderTest, call, final
    from zetra.sdk.tools import check_tool_file

    class Payload(Contract):
        text: str


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    return path


def _cli(*argv: str) -> tuple[int, dict]:
    code, report, _ = _cli_full(*argv)
    return code, report


def _cli_full(*argv: str) -> tuple[int, dict, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    text = out.getvalue()
    return code, json.loads(text) if text.strip().startswith("{") else {}, err.getvalue()


@unittest.skipUnless(HAVE_SDK, "requires the sdk extra")
class BundleTests(unittest.TestCase):
    def test_example_locks_are_current_and_tiers_follow_effects(self):
        expected = {"handbook_qa": "T1", "alert_responder": "T2", "scheduled_report": "T2"}
        for package, tier in expected.items():
            bundle = build(EXAMPLES / package)
            self.assertEqual(lock_status(bundle), "current", package)
            self.assertEqual(bundle.tier, tier, package)
            lock = bundle.lock()
            self.assertEqual(lock["durable_identifiers"]["workflow"], "zetra.agent")
            self.assertTrue(
                set(lock["capabilities"]) >= {t["capability"] for t in lock["tools"].values()}
            )

    def test_frozen_check_passes_on_committed_examples(self):
        code, report = _cli("check", str(EXAMPLES / "alert_responder"), "--frozen")
        self.assertEqual((code, report["lock"], report["tier"]), (0, "current", "T2"))
        self.assertEqual(
            report["tools"]["page_oncall"]["approval"], {"mode": "human", "ttl": "PT10M"}
        )

    def test_new_scaffold_checks_and_writes_a_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sdk_scaffold"
            code, report = _cli("new", str(root), "--write")
            self.assertEqual((code, report["status"]), (0, "created"))
            _write(root.parent / "zetra.toml", "[project]\nmodel_policy = 'approved:balanced-v1'\n")
            code, report = _cli("check", str(root))
            self.assertEqual((code, report["tier"], report["lock"]), (0, "T2", "written"))
            self.assertTrue((root / "zetra.lock").is_file())
            self.assertEqual(report["tools"]["record_note"]["approval"]["mode"], "human")
            code, report = _cli("check", str(root), "--frozen")
            self.assertEqual((code, report["lock"]), (0, "current"))
            pytest_run = subprocess.run(
                [sys.executable, "-m", "pytest", str(root), "-q", "-p", "no:cacheprovider"],
                capture_output=True,
                text=True,
                env={**os.environ, "PYDANTIC_AI_NO_BANNER": "1"},
            )
            self.assertEqual(pytest_run.returncode, 0, pytest_run.stdout + pytest_run.stderr)
            self.assertIn("1 passed", pytest_run.stdout)

    def test_positional_input_parameter_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sdk_positional"
            _cli("new", str(root), "--write")
            tool = root / "tools" / "record_note.py"
            tool.write_text(tool.read_text().replace("text: str, *, request", "text: str, request"))
            with self.assertRaises(CompileError) as ctx:
                build(root)
            self.assertEqual(ctx.exception.code, "ZT-TOOL-019")

    def test_prompt_approval_refuses_to_run_without_a_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sdk_prompt"
            _cli("new", str(root), "--write")
            with patch("sys.stdin", io.StringIO("")):
                code, report, err = _cli_full(
                    "run", str(root), "--input", '{"text": "hi"}', "--model", "test"
                )
            self.assertEqual((code, report), (1, {}))
            self.assertIn("needs a terminal", err)

    def test_unapproved_irreversible_write_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sdk_irreversible"
            _cli("new", str(root), "--write")
            tool = root / "tools" / "record_note.py"
            tool.write_text(tool.read_text().replace("@approval\n", ""), encoding="utf-8")
            with self.assertRaises(CompileError) as ctx:
                build(root)
            self.assertEqual(ctx.exception.code, "ZT-TOOL-005")


@unittest.skipUnless(HAVE_SDK, "requires the sdk extra")
class StaticCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def check(self, name: str, source: str) -> dict:
        return check_tool_file(_write(self.root / f"{name}.py", source))

    def assert_refused(self, code: str, name: str, source: str) -> None:
        with self.assertRaises(CompileError) as ctx:
            self.check(name, source)
        self.assertEqual(ctx.exception.code, code)

    def test_write_body_must_be_a_pure_payload_builder(self):
        pure = self.check(
            "note",
            '''
            @approval(ttl="PT1H")
            @tool("notes.write")
            def note(text: str, *, request: Request) -> Note:
                """Pure."""
                return Note(text=text.strip())
            ''',
        )
        self.assertEqual(
            (pure["capability"], pure["effect"], pure["approval"], pure["ttl"]),
            ("notes.write", "write", True, "PT1H"),
        )
        impure = (
            "    import os\n    return Note(text=os.getcwd())",
            "    return Note(text=str(datetime.now()))",
            "    return Note(text=open('x').read())",
            "    return Note(text=str(random.random()))",
        )
        header = '@tool("notes.write")\ndef note(text: str, *, request: Request) -> Note:\n'
        for body in impure:
            self.assert_refused("ZT-TOOL-004", "note", header + body + "\n")
        self.assert_refused(
            "ZT-TOOL-004",
            "note",
            """
            @tool("notes.write")
            async def note(text: str) -> Note:
                return Note(text=text)
            """,
        )

    def test_decorators_are_literal_and_closed(self):
        self.assert_refused("ZT-TOOL-012", "x", '@tool(CAP)\ndef x() -> str:\n    return ""\n')
        self.assert_refused(
            "ZT-TOOL-012", "x", '@tool("notes.delete")\ndef x() -> str:\n    return ""\n'
        )
        self.assert_refused(
            "ZT-TOOL-013",
            "x",
            '@approval(ttl=TTL)\n@tool("n.write")\ndef x() -> str:\n    return ""\n',
        )
        self.assert_refused(
            "ZT-TOOL-014", "x", '@lru_cache\n@tool("n.read")\ndef x() -> str:\n    return ""\n'
        )
        self.assert_refused("ZT-TOOL-011", "x", '@tool("n.read")\ndef y() -> str:\n    return ""\n')
        self.assert_refused(
            "ZT-TOOL-010",
            "x",
            '@tool("n.read")\ndef x() -> str:\n    return ""\n\n@tool("m.read")\ndef z() -> str:\n    return ""\n',
        )

    def test_spec_refuses_gateway_bypass_and_unapproved_models(self):
        cases = {
            "ZT-SPEC-007": "name: a\nmodel: approved:x\ninstructions: i\ncapabilities: [WebSearch]\n",
            "ZT-SPEC-008": "name: a\nmodel: approved:x\ninstructions: i\ncapabilities: [Unknown]\n",
            "ZT-SPEC-003": "name: a\nmodel: openai:gpt-5\ninstructions: i\n",
            "ZT-SPEC-005": "name: a\nmodel: approved:x\ninstructions: i\nmetadata: {owner: me}\n",
            "ZT-SPEC-006": (
                "name: a\nmodel: approved:x\ninstructions: i\n"
                "model_settings: {extra_headers: {x: y}}\n"
            ),
        }
        for code, text in cases.items():
            with self.assertRaises(CompileError, msg=code) as ctx:
                load_spec(_write(self.root / code / "agent.yaml", text))
            self.assertEqual(ctx.exception.code, code)
        spec, raw = load_spec(
            _write(
                self.root / "ok" / "agent.yaml",
                "name: a\nmodel: approved:x\ninstructions: i\n"
                "capabilities:\n  - GatewayMCP: {server: s, tools: [t]}\n",
            )
        )
        self.assertEqual((spec.name, spec.model, raw["model"]), ("a", "approved:x", "approved:x"))


@unittest.skipUnless(HAVE_SDK, "requires the sdk extra")
class ReceiverTests(unittest.TestCase):
    def grant(self, payload: Contract, **overrides) -> Grant:
        from zetra.sdk.receiver import payload_digest

        fields = {
            "operation_id": "run:c000001",
            "digest": payload_digest(payload),
            "actor": "oncall:bob",
            "mode": "human",
            "expires_at": datetime.now(UTC) + timedelta(hours=1),
        }
        fields.update(overrides)
        return Grant(**fields)

    def test_receivers_bind_digest_actor_and_operation(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory, sqlite = (
                MemoryReceiver("notes"),
                SqliteReceiver("notes", Path(tmp) / "notes.db"),
            )
            receivers: list[Any] = [memory, sqlite]
            for receiver in receivers:
                payload, other = Payload(text="a"), Payload(text="b")
                grant = self.grant(payload)
                first = receiver.commit(grant, payload)
                self.assertEqual(receiver.commit(grant, payload).receipt_id, first.receipt_id)
                with self.assertRaises(Denied):
                    receiver.commit(grant, other)
                with self.assertRaises(Denied):
                    receiver.commit(self.grant(other, operation_id=grant.operation_id), other)
                with self.assertRaises(Denied):
                    receiver.commit(self.grant(payload, actor="someone:else"), payload)
                expired = self.grant(payload, operation_id="run:c2", expires_at=datetime.now(UTC))
                with self.assertRaises(Denied):
                    receiver.commit(expired, payload)
            self.assertEqual(sqlite.count(), 1)
            self.assertEqual(memory.commits, [Payload(text="a")])


@unittest.skipUnless(HAVE_SDK, "requires the sdk extra")
class ApprovalLoopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = build(EXAMPLES / "alert_responder")
        cls.tmp = tempfile.mkdtemp()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def agent(self) -> AgentUnderTest:
        under_test = AgentUnderTest(self.bundle)
        gateway = under_test.gateway("status")
        gateway.stub("read_status", "degraded").stub("recent_deploys", "none")
        return under_test

    def alert(self):
        from alert_responder.contracts import Alert

        return Alert(
            alert_id="ALR-20261012", service="checkout-api", severity="critical", message="5xx"
        )

    def test_approved_payload_is_committed_exactly_as_shown(self):
        from alert_responder.contracts import Page, Paged

        agent = self.agent()
        agent.model.script(
            call("page_oncall", urgency="high", summary="5xx storm"), final(Paged(reason="ok"))
        )
        run = agent.run(self.alert())
        shown = run.approvals.pending
        self.assertEqual(shown, [Page(service="checkout-api", urgency="high", summary="5xx storm")])
        self.assertEqual(agent.receiver("pager").commits, [])
        run.approve_all(actor="oncall:bob")
        self.assertEqual(run.output, Paged(reason="ok"))
        self.assertEqual(agent.receiver("pager").commits, shown)
        [proposal] = run.proposals
        self.assertEqual(proposal.operation_id, "test-run:c000001")
        self.assertEqual(proposal.ttl, "PT10M")

    def test_model_cannot_choose_the_injected_service(self):
        from alert_responder.contracts import NoAction

        agent = self.agent()
        agent.model.script(
            call("page_oncall", urgency="high", summary="x", service="payments"),
            final(NoAction(reason="gave up")),
        )
        run = agent.run(self.alert())
        self.assertEqual(run.approvals.pending, [])
        self.assertEqual(run.output, NoAction(reason="gave up"))
        self.assertEqual(agent.receiver("pager").commits, [])

    def test_invalid_payload_becomes_a_retry_prompt_not_a_proposal(self):
        from alert_responder.contracts import NoAction

        agent = self.agent()
        agent.model.script(
            call("page_oncall", urgency="high", summary="x" * 600),  # Page.summary max 500
            final(NoAction(reason="could not page")),
        )
        run = agent.run(self.alert())
        self.assertEqual(run.approvals.pending, [])
        self.assertEqual(run.output, NoAction(reason="could not page"))
        retries = [p for m in run.messages for p in m.parts if isinstance(p, RetryPromptPart)]
        self.assertEqual(len(retries), 1)
        self.assertIn("summary", str(retries[0].content))
        self.assertEqual(agent.receiver("pager").commits, [])

    def test_budget_exhaustion_and_prose_output_are_typed_failures(self):
        from alert_responder.contracts import NoAction

        agent = self.agent()
        agent.model.script(*([call("read_status")] * 20), final(NoAction(reason="done")))
        run = agent.run(self.alert())
        self.assertEqual((run.status, run.output), ("failed", None))
        assert run.error is not None
        self.assertEqual(run.error.code, "budget_exhausted")
        self.assertIn("request_limit", run.error.message)
        self.assertGreater(len(run.messages), 8, "messages are kept for evidence")

        agent = self.agent()
        agent.model.script(final("I think we should page someone."))
        run = agent.run(self.alert())
        assert run.error is not None
        self.assertEqual((run.status, run.error.code), ("failed", "output_invalid"))
        self.assertEqual(run.approvals.pending, [])

    def test_rejection_and_reversible_writes(self):
        from alert_responder.contracts import Annotation, NoAction

        agent = self.agent()
        agent.model.script(
            call("page_oncall", urgency="low", summary="x"),
            call("annotate_alert", note="not paged"),
            final(NoAction(reason="rejected")),
        )
        run = agent.run(self.alert()).reject_all(actor="oncall:bob")
        self.assertEqual(run.output.kind, "no_action")
        self.assertEqual(agent.receiver("pager").commits, [])
        self.assertEqual(
            agent.receiver("alerts").commits,
            [Annotation(alert_id="ALR-20261012", note="not paged")],
        )
