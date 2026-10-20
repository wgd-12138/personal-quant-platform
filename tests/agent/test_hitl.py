"""A1-05 HITL 合同测试：副作用前停下、审批绑定线程与参数、一次性消费、fail-closed。"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import unittest
import uuid

from pq_agent.approval import (
    ERROR_APPROVAL_ACTION_MISMATCH,
    ERROR_APPROVAL_ACTOR_MISMATCH,
    ERROR_APPROVAL_ALREADY_CONSUMED,
    ERROR_APPROVAL_ALREADY_DECIDED,
    ERROR_APPROVAL_EXPIRED,
    ERROR_APPROVAL_NOT_APPROVED,
    ERROR_APPROVAL_REQUEST_INVALID,
    ERROR_APPROVAL_THREAD_MISMATCH,
    ApprovalStore,
)
from pq_agent.checkpoint import guarded_sqlite_checkpointer
from pq_agent.contracts import ContractError
from pq_agent.graph import GraphDeps, build_graph, pending_interrupt, resume_graph, run_graph
from pq_agent.outbox import OutboxStore

from ._graph_fakes import FIXED_NOW, FakeProvider, FakeTool, FakeTools, make_state

ROOT = pathlib.Path(__file__).resolve().parents[2]
SNAPSHOT_ID = "replay:2026-07-09:deadbeefcafe"


def _plan():
    """一个必然需要人工确认的副作用调用（写控制状态）。"""
    return [
        {
            "call_id": "c0",
            "tool_name": "push_preview",
            "arguments": {"slot": "pre_open"},
            "capability": "control_write",
            "dry_run": False,
            "schema_version": 1,
        }
    ]


class _Clock:
    def __init__(self, moment=None):
        self.moment = moment or FIXED_NOW

    def __call__(self):
        return self.moment

    def advance(self, **kwargs):
        self.moment = self.moment + dt.timedelta(**kwargs)


def _tmp_dir(name):
    """刻意不用 tempfile：本机沙箱会把 0o700 目录变成自己进不去的目录。"""
    base = ROOT / ".tmp" / name
    base.mkdir(parents=True, exist_ok=True)
    path = base / uuid.uuid4().hex[:8]
    path.mkdir()
    return path


class _ErrorCodeMixin:
    def assertCode(self, code, callable_obj):
        with self.assertRaises(ContractError) as ctx:
            callable_obj()
        self.assertEqual(ctx.exception.code, code)
        return ctx.exception


class ApprovalStoreTests(_ErrorCodeMixin, unittest.TestCase):
    def setUp(self):
        self.dir = _tmp_dir("a1-05-hitl-store")
        self.path = str(self.dir / "approval.jsonl")
        self.clock = _Clock()
        self.store = ApprovalStore(self.path, clock=self.clock, ttl_s=900)

    def _request(self, **overrides):
        kwargs = dict(
            thread_id="t-1",
            call_id="c0",
            tool_name="push_preview",
            capability="control_write",
            arguments_hash="h1",
            preview={"slot": "pre_open"},
        )
        kwargs.update(overrides)
        return self.store.request(**kwargs)

    def _rows(self):
        path = pathlib.Path(self.path)
        if not path.exists():
            return []
        return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]

    def test_request_persists_pending_record(self):
        record = self._request(approval_id="a0001")
        self.assertEqual(record.status, "pending")
        self.assertEqual(record.expires_at, "2026-07-09T16:45:00+08:00")
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["approval_id"], "a0001")
        self.assertEqual(rows[0]["thread_id"], "t-1")
        self.assertTrue(rows[0]["action_hash"])

    def test_request_is_idempotent_for_same_approval_id(self):
        first = self._request(approval_id="a0001")
        second = self._request(approval_id="a0001")
        self.assertEqual(first, second)
        self.assertEqual(len(self.store.records()), 1)

    def test_request_cannot_rebind_action_hash(self):
        self._request(approval_id="a0001")
        self.assertCode(ERROR_APPROVAL_REQUEST_INVALID, lambda: self._request(approval_id="a0001", arguments_hash="h2"))

    def test_consume_before_decide_is_rejected(self):
        record = self._request(approval_id="a0001")
        self.assertCode(
            ERROR_APPROVAL_NOT_APPROVED,
            lambda: self.store.consume("a0001", thread_id="t-1", action_hash=record.action_hash, now=self.clock()),
        )

    def test_decide_binds_thread_action_and_actor(self):
        record = self._request(approval_id="a0001", actor="wgd")
        self.assertCode(
            ERROR_APPROVAL_THREAD_MISMATCH,
            lambda: self.store.decide("a0001", approved=True, thread_id="t-9", action_hash=record.action_hash, now=self.clock()),
        )
        self.assertCode(
            ERROR_APPROVAL_ACTION_MISMATCH,
            lambda: self.store.decide("a0001", approved=True, thread_id="t-1", action_hash="deadbeef", now=self.clock()),
        )
        self.assertCode(
            ERROR_APPROVAL_ACTOR_MISMATCH,
            lambda: self.store.decide("a0001", approved=True, actor="someone-else", thread_id="t-1", action_hash=record.action_hash, now=self.clock()),
        )
        decided = self.store.decide("a0001", approved=True, actor="wgd", thread_id="t-1", action_hash=record.action_hash, now=self.clock())
        self.assertEqual(decided.status, "approved")
        self.assertCode(
            ERROR_APPROVAL_ALREADY_DECIDED,
            lambda: self.store.decide("a0001", approved=False, actor="wgd", thread_id="t-1", action_hash=record.action_hash, now=self.clock()),
        )

    def test_consume_is_one_shot(self):
        record = self._request(approval_id="a0001")
        self.store.decide("a0001", approved=True, thread_id="t-1", action_hash=record.action_hash, now=self.clock())
        consumed = self.store.consume("a0001", thread_id="t-1", action_hash=record.action_hash, now=self.clock())
        self.assertTrue(consumed.consumed_at)
        self.assertCode(
            ERROR_APPROVAL_ALREADY_CONSUMED,
            lambda: self.store.consume("a0001", thread_id="t-1", action_hash=record.action_hash, now=self.clock()),
        )

    def test_consume_rejects_wrong_action_hash(self):
        record = self._request(approval_id="a0001")
        self.store.decide("a0001", approved=True, thread_id="t-1", action_hash=record.action_hash, now=self.clock())
        self.assertCode(
            ERROR_APPROVAL_ACTION_MISMATCH,
            lambda: self.store.consume("a0001", thread_id="t-1", action_hash="other", now=self.clock()),
        )

    def test_expired_approval_cannot_be_consumed(self):
        record = self._request(approval_id="a0001")
        self.store.decide("a0001", approved=True, thread_id="t-1", action_hash=record.action_hash, now=self.clock())
        self.clock.advance(minutes=16)
        self.assertCode(
            ERROR_APPROVAL_EXPIRED,
            lambda: self.store.consume("a0001", thread_id="t-1", action_hash=record.action_hash, now=self.clock()),
        )
        self.assertEqual(self.store.get("a0001").status, "expired")

    def test_preview_is_redacted_on_disk_and_fingerprint_survives_reload(self):
        secret = "https://open.feishu.cn/open-apis/bot/v2/hook/" + "0" * 32
        store = ApprovalStore(self.path, clock=self.clock, ttl_s=900, secrets=[secret])
        record = store.request(
            thread_id="t-1",
            call_id="c0",
            tool_name="push_preview",
            capability="notify_external",
            arguments_hash="h1",
            approval_id="a0001",
            preview={"webhook": secret, "note": "x" * 400},
            now=self.clock(),
        )
        self.assertNotIn(secret, json.dumps(record.preview, ensure_ascii=False))
        self.assertNotIn(secret, pathlib.Path(self.path).read_text(encoding="utf-8"))
        self.assertLessEqual(len(record.preview["note"]), 150)
        reloaded = ApprovalStore(self.path, clock=self.clock, ttl_s=900, secrets=[secret])
        self.assertEqual(reloaded.fingerprint(), store.fingerprint())


class GraphHitlTests(_ErrorCodeMixin, unittest.TestCase):
    def setUp(self):
        self.dir = _tmp_dir("a1-05-hitl-graph")
        self.db = str(self.dir / "cp.sqlite")
        self.approval_path = str(self.dir / "approval.jsonl")
        self.outbox_path = str(self.dir / "outbox.jsonl")
        self.clock = _Clock()

    def _deps(self, tool, *, with_stores=True):
        tools = FakeTools()
        tools.add(tool)
        kwargs = {}
        if with_stores:
            kwargs["approval_store"] = ApprovalStore(self.approval_path, clock=self.clock, ttl_s=900)
            kwargs["outbox"] = OutboxStore(self.outbox_path, clock=self.clock)
        return GraphDeps(
            provider=FakeProvider(),
            tools=tools,
            clock=self.clock,
            plan_builder=lambda state: _plan(),
            **kwargs,
        )

    def _records(self):
        path = pathlib.Path(self.approval_path)
        if not path.exists():
            return []
        return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]

    def _start(self, thread_id, *, tool=None, with_stores=True):
        tool = tool or FakeTool("push_preview")
        with guarded_sqlite_checkpointer(self.db) as saver:
            app = build_graph(self._deps(tool, with_stores=with_stores), checkpointer=saver)
            out, interrupted = run_graph(
                app,
                make_state(thread_id=thread_id, user_query="预览一下盘前推送"),
                thread_id=thread_id,
            )
            pending = pending_interrupt(app, thread_id=thread_id)
        return out, interrupted, pending, tool

    def _finish(self, decision, thread_id):
        tool = FakeTool("push_preview")
        with guarded_sqlite_checkpointer(self.db) as saver:
            app = build_graph(self._deps(tool), checkpointer=saver)
            out, interrupted = resume_graph(app, decision, thread_id=thread_id)
        return out, interrupted, tool

    def test_side_effect_stops_before_execution(self):
        out, interrupted, pending, tool = self._start("t-a")
        self.assertTrue(interrupted)
        self.assertEqual(tool.calls, [])
        self.assertIsNotNone(pending)
        rows = self._records()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "pending")
        self.assertEqual(rows[0]["action_hash"], pending["value"]["action_hash"])

    def test_pending_payload_carries_binding_fields(self):
        _, _, pending, _ = self._start("t-a")
        value = pending["value"]
        for field in ("approval_id", "thread_id", "action_hash", "arguments_hash", "risk_note", "expires_at", "arguments_preview", "capability"):
            self.assertIn(field, value)
        self.assertEqual(value["thread_id"], "t-a")
        self.assertEqual(value["risk_note"].split(":")[0], "push_preview")

    def test_resume_executes_exactly_once_and_consumes_approval(self):
        _, _, pending, _ = self._start("t-a")
        value = pending["value"]
        out, interrupted, tool = self._finish(
            {"approved": True, "arguments_hash": value["arguments_hash"], "action_hash": value["action_hash"], "actor": "human"},
            "t-a",
        )
        self.assertFalse(interrupted)
        self.assertEqual(len(tool.calls), 1)
        self.assertEqual([(a["tool_name"], a["status"]) for a in out["approvals"]], [("push_preview", "approved")])
        rows = self._records()
        self.assertTrue(rows[-1]["consumed_at"])

    def test_arguments_hash_mismatch_is_rejected(self):
        _, _, pending, _ = self._start("t-a")
        out, _, tool = self._finish(
            {"approved": True, "arguments_hash": "deadbeef", "action_hash": pending["value"]["action_hash"]},
            "t-a",
        )
        self.assertEqual(tool.calls, [])
        self.assertEqual([a["status"] for a in out["approvals"]], ["rejected"])

    def test_action_hash_mismatch_is_rejected(self):
        _, _, pending, _ = self._start("t-a")
        out, _, tool = self._finish(
            {"approved": True, "arguments_hash": pending["value"]["arguments_hash"], "action_hash": "deadbeef"},
            "t-a",
        )
        self.assertEqual(tool.calls, [])
        self.assertEqual([a["status"] for a in out["approvals"]], ["rejected"])

    def test_rejection_never_executes(self):
        _, _, pending, _ = self._start("t-a")
        out, _, tool = self._finish(
            {"approved": False, "arguments_hash": pending["value"]["arguments_hash"], "action_hash": pending["value"]["action_hash"]},
            "t-a",
        )
        self.assertEqual(tool.calls, [])
        self.assertEqual([a["status"] for a in out["approvals"]], ["rejected"])
        self.assertEqual(self._records()[-1]["status"], "rejected")

    def test_second_thread_needs_fresh_approval(self):
        _, _, first, _ = self._start("t-a")
        _, _, tool = self._finish(
            {"approved": True, "arguments_hash": first["value"]["arguments_hash"], "action_hash": first["value"]["action_hash"]},
            "t-a",
        )
        self.assertEqual(len(tool.calls), 1)
        _, interrupted, second, second_tool = self._start("t-b")
        self.assertTrue(interrupted)
        self.assertEqual(second_tool.calls, [])
        self.assertNotEqual(second["value"]["approval_id"], first["value"]["approval_id"])
        records = self._records()
        # 审批账本是 append-only 事件流：首次请求 + 决策 + 消费 + 新线程请求 = 4 条
        self.assertEqual(len(records), 4)
        self.assertEqual(len({r["approval_id"] for r in records}), 2)

    def test_without_store_the_graph_still_stops(self):
        _, interrupted, pending, tool = self._start("t-a", with_stores=False)
        self.assertTrue(interrupted)
        self.assertEqual(tool.calls, [])
        self.assertIn("action_hash", pending["value"])
        self.assertFalse(pathlib.Path(self.approval_path).exists())


if __name__ == "__main__":
    unittest.main()