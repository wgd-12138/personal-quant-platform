"""A1-05 出件箱测试：幂等准备、逐条结算、UNKNOWN 不自动重试、归档保留期。"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import unittest
import uuid

from pq_agent.approval import ApprovalStore
from pq_agent.checkpoint import guarded_sqlite_checkpointer
from pq_agent.contracts import ContractError, ToolCall
from pq_agent.graph import GraphDeps, _idempotency_key_for, build_graph, pending_interrupt, resume_graph, run_graph
from pq_agent.outbox import (
    ERROR_OUTBOX_ENTRY_INVALID,
    ERROR_OUTBOX_ITEM_ALREADY_SETTLED,
    ERROR_OUTBOX_ITEM_INDEX_INVALID,
    ERROR_OUTBOX_ITEM_STATUS_INVALID,
    ERROR_OUTBOX_PHASE_INVALID,
    ERROR_OUTBOX_RETRY_FORBIDDEN,
    ERROR_OUTBOX_UNKNOWN_REQUIRES_HUMAN,
    OutboxStore,
    compute_idempotency_key,
)

from ._graph_fakes import FIXED_NOW, FakeProvider, FakeTool, FakeTools, make_state

ROOT = pathlib.Path(__file__).resolve().parents[2]
SNAPSHOT_ID = "replay:2026-07-09:deadbeefcafe"
KEY_PARTS = dict(
    tool_name="push_preview",
    action_hash="ah1",
    thread_id="t-1",
    as_of="2026-07-09",
    slot="pre_open",
    payload_digest="pd1",
    data_snapshot_id=SNAPSHOT_ID,
    runtime="local",
)


def _plan(capability="control_write"):
    return [
        {
            "call_id": "c0",
            "tool_name": "push_preview",
            "arguments": {"slot": "pre_open"},
            "capability": capability,
            "dry_run": False,
            "schema_version": 1,
        }
    ]


class _Clock:
    def __init__(self, moment=None):
        self.moment = moment or FIXED_NOW

    def __call__(self):
        return self.moment


def _tmp_dir(name):
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


class OutboxStoreTests(_ErrorCodeMixin, unittest.TestCase):
    def setUp(self):
        self.dir = _tmp_dir("a1-05-outbox-store")
        self.path = self.dir / "outbox.jsonl"
        self.clock = _Clock()
        self.store = OutboxStore(self.path, clock=self.clock)

    def _prepare(self, key=None, items=("c0", "c1", "c2")):
        return self.store.prepare(
            thread_id="t-1",
            call_id="c0",
            tool_name="push_preview",
            phase="commit",
            idempotency_key=key or compute_idempotency_key(**KEY_PARTS),
            item_keys=list(items),
            now=self.clock(),
        )

    def test_idempotency_key_rejects_unknown_field(self):
        self.assertCode(ERROR_OUTBOX_ENTRY_INVALID, lambda: compute_idempotency_key(**{**KEY_PARTS, "bogus": 1}))

    def test_prepare_is_idempotent_per_key(self):
        first, created_first = self._prepare()
        second, created_second = self._prepare()
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first.entry_id, second.entry_id)
        self.assertEqual([i["status"] for i in first.items], ["PENDING", "PENDING", "PENDING"])

    def test_prepare_rejects_unknown_phase(self):
        self.assertCode(
            ERROR_OUTBOX_PHASE_INVALID,
            lambda: self.store.prepare(
                thread_id="t-1",
                call_id="c0",
                tool_name="push_preview",
                phase="bogus",
                idempotency_key=compute_idempotency_key(**KEY_PARTS),
                item_keys=["c0"],
                now=self.clock(),
            ),
        )

    def test_mark_item_validates_index_status_and_settlement(self):
        entry, _ = self._prepare()
        self.assertCode(ERROR_OUTBOX_ITEM_INDEX_INVALID, lambda: self.store.mark_item(entry.entry_id, 5, "SENT", now=self.clock()))
        self.assertCode(ERROR_OUTBOX_ITEM_STATUS_INVALID, lambda: self.store.mark_item(entry.entry_id, 0, "BOGUS", now=self.clock()))
        self.store.mark_item(entry.entry_id, 0, "SENT", external_id="m0", now=self.clock())
        self.assertCode(ERROR_OUTBOX_ITEM_ALREADY_SETTLED, lambda: self.store.mark_item(entry.entry_id, 0, "SENT", now=self.clock()))
        state = self.store.delivery_state(entry.entry_id)
        self.assertEqual(state["sent"], [0])
        self.assertEqual(state["pending"], [1, 2])

    def test_unknown_entry_blocks_automatic_retry(self):
        entry, _ = self._prepare()
        self.assertEqual(self.store.mark_unknown(entry.entry_id, reason="外发超时", now=self.clock()).status, "UNKNOWN")
        self.assertCode(ERROR_OUTBOX_UNKNOWN_REQUIRES_HUMAN, lambda: self._prepare())
        self.assertCode(ERROR_OUTBOX_ITEM_ALREADY_SETTLED, lambda: self.store.mark_item(entry.entry_id, 1, "FAILED", now=self.clock()))
        self.assertEqual([e.entry_id for e in self.store.unknown()], [entry.entry_id])

    def test_retry_requires_approved_record_and_keeps_sent_items(self):
        entry, _ = self._prepare()
        self.store.mark_item(entry.entry_id, 0, "SENT", now=self.clock())
        self.store.mark_unknown(entry.entry_id, reason="外发超时", now=self.clock())
        approvals = ApprovalStore(self.dir / "approval.jsonl", clock=self.clock, ttl_s=900)
        record = approvals.request(
            thread_id="t-1",
            call_id="c0",
            tool_name="push_preview",
            capability="control_write",
            arguments_hash="h1",
            now=self.clock(),
        )
        self.assertCode(ERROR_OUTBOX_RETRY_FORBIDDEN, lambda: self.store.retry(entry.entry_id, approval=record, now=self.clock()))
        approved = approvals.decide(record.approval_id, approved=True, actor="human", thread_id="t-1", now=self.clock())
        retried = self.store.retry(entry.entry_id, approval=approved, now=self.clock())
        self.assertEqual(retried.status, "PREPARED")
        self.assertEqual([i["status"] for i in retried.items], ["SENT", "PENDING", "PENDING"])
        self.assertEqual(self.store.unknown(), ())

    def test_archive_keeps_unsettled_entries(self):
        entry, _ = self._prepare()
        result = self.store.archive(now=self.clock())
        self.assertEqual(result["archived"], [])
        self.assertEqual(result["retained"], 1)
        self.assertIsNotNone(self.store.find_by_key(entry.idempotency_key))


class GraphOutboxTests(_ErrorCodeMixin, unittest.TestCase):
    def setUp(self):
        self.dir = _tmp_dir("a1-05-outbox-graph")
        self.db = str(self.dir / "cp.sqlite")
        self.approval_path = str(self.dir / "approval.jsonl")
        self.outbox_path = str(self.dir / "outbox.jsonl")
        self.clock = _Clock()

    def _deps(self, tool, capability="control_write"):
        tools = FakeTools()
        tools.add(tool)
        return GraphDeps(
            provider=FakeProvider(),
            tools=tools,
            clock=self.clock,
            approval_store=ApprovalStore(self.approval_path, clock=self.clock, ttl_s=900),
            outbox=OutboxStore(self.outbox_path, clock=self.clock),
            plan_builder=lambda state: _plan(capability),
        )

    def _run_to_approval(self, thread_id, *, capability="control_write", tool=None):
        tool = tool or FakeTool("push_preview")
        with guarded_sqlite_checkpointer(self.db) as saver:
            app = build_graph(self._deps(tool, capability), checkpointer=saver)
            run_graph(app, make_state(thread_id=thread_id, user_query="预览一下盘前推送"), thread_id=thread_id)
            pending = pending_interrupt(app, thread_id=thread_id)
        return pending, tool

    def _resume(self, thread_id, pending, *, capability="control_write", tool=None):
        tool = tool or FakeTool("push_preview")
        decision = {
            "approved": True,
            "arguments_hash": pending["value"]["arguments_hash"],
            "action_hash": pending["value"]["action_hash"],
        }
        with guarded_sqlite_checkpointer(self.db) as saver:
            app = build_graph(self._deps(tool, capability), checkpointer=saver)
            out, _ = resume_graph(app, decision, thread_id=thread_id)
        return out, tool

    def _entries(self):
        path = pathlib.Path(self.outbox_path)
        if not path.exists():
            return []
        return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]

    def _key_for(self, thread_id, capability="control_write"):
        state = make_state(thread_id=thread_id, user_query="预览一下盘前推送")
        state["data_snapshot_id"] = SNAPSHOT_ID
        state["as_of"] = "2026-07-09"
        call = ToolCall(call_id="c0", tool_name="push_preview", arguments={"slot": "pre_open"}, capability=capability, dry_run=False)
        return _idempotency_key_for(state, call)

    def test_successful_execution_marks_entry_sent(self):
        pending, _ = self._run_to_approval("t-a")
        out, tool = self._resume("t-a", pending)
        self.assertEqual(len(tool.calls), 1)
        entries = self._entries()
        self.assertEqual(entries[-1]["status"], "SENT")
        self.assertEqual([i["status"] for i in entries[-1]["items"]], ["SENT"])

    def test_already_sent_entry_blocks_re_execution(self):
        store = OutboxStore(self.outbox_path, clock=self.clock)
        entry, _ = store.prepare(
            thread_id="t-a",
            call_id="c0",
            tool_name="push_preview",
            phase="commit",
            idempotency_key=self._key_for("t-a"),
            item_keys=["c0"],
            now=self.clock(),
        )
        store.mark_item(entry.entry_id, 0, "SENT", external_id="m0", now=self.clock())
        pending, _ = self._run_to_approval("t-a")
        out, tool = self._resume("t-a", pending)
        self.assertEqual(tool.calls, [])
        self.assertTrue(any("ALREADY_COMMITTED" in e for e in out["errors"]), out["errors"])

    def test_unknown_entry_blocks_re_execution(self):
        store = OutboxStore(self.outbox_path, clock=self.clock)
        entry, _ = store.prepare(
            thread_id="t-a",
            call_id="c0",
            tool_name="push_preview",
            phase="commit",
            idempotency_key=self._key_for("t-a"),
            item_keys=["c0"],
            now=self.clock(),
        )
        store.mark_unknown(entry.entry_id, reason="外发超时", now=self.clock())
        pending, _ = self._run_to_approval("t-a")
        out, tool = self._resume("t-a", pending)
        self.assertEqual(tool.calls, [])
        self.assertTrue(any(ERROR_OUTBOX_UNKNOWN_REQUIRES_HUMAN in e for e in out["errors"]), out["errors"])

    def test_notify_failure_marks_entry_unknown(self):
        tool = FakeTool("push_preview", raises=RuntimeError("网络断了"))
        pending, _ = self._run_to_approval("t-a", capability="notify_external", tool=tool)
        broken = FakeTool("push_preview", raises=RuntimeError("网络断了"))
        out, resumed = self._resume("t-a", pending, capability="notify_external", tool=broken)
        self.assertEqual(len(resumed.calls), 1)
        self.assertEqual(self._entries()[-1]["status"], "UNKNOWN")
        self.assertTrue(any("TOOL_FAILED" in e for e in out["errors"]), out["errors"])


if __name__ == "__main__":
    unittest.main()