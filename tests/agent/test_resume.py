"""A1-03 恢复：中断后重启、同一 thread_id 续跑，且副作用不重复执行。"""

from __future__ import annotations

import os
import unittest
import uuid

from pq_agent.checkpoint import (
    audit_checkpoint_database,
    checkpointer_driver_available,
    guarded_sqlite_checkpointer,
)
from pq_agent.contracts import ContractError
from pq_agent.graph import (
    GraphDeps,
    build_graph,
    pending_interrupt,
    resume_graph,
    run_graph,
)
from ._graph_fakes import (
    FakeProvider,
    FakeTool,
    FakeTools,
    fixed_clock,
    make_state,
    plain_plan,
)

DRIVER_OK, DRIVER_REASON = checkpointer_driver_available()


def _tmp_dir(name):
    # 刻意不用 tempfile：本机沙箱把 0o700 目录映射成自锁 DACL（见 A0-00 证据）
    root = os.path.join(os.getcwd(), ".tmp", "a1-03-tests")
    path = os.path.join(root, name + "-" + uuid.uuid4().hex[:8])
    os.makedirs(path)
    return path


class ResumeTests(unittest.TestCase):
    setUp_patcher = None

    def setUp(self):
        if not DRIVER_OK:
            self.skipTest("checkpoint 驱动不可用：" + DRIVER_REASON)
        self.dir = _tmp_dir("resume")
        self.db = os.path.join(self.dir, "cp.sqlite")

    def _deps(self, tool):
        tools = FakeTools()
        tools.add(tool)
        return GraphDeps(
            provider=FakeProvider(),
            tools=tools,
            clock=fixed_clock,
            plan_builder=lambda s: plain_plan(("push_preview", "business_write")),
        )

    def test_side_effect_waits_for_approval_then_resumes_once(self):
        tool = FakeTool("push_preview")
        state = make_state(user_query="预览一下晚间推送")
        with guarded_sqlite_checkpointer(self.db) as saver:
            app = build_graph(self._deps(tool), checkpointer=saver)
            _, interrupted = run_graph(app, state, thread_id="resume-1")
            self.assertTrue(interrupted)
            self.assertEqual(tool.calls, [])
            pend = pending_interrupt(app, thread_id="resume-1")
            self.assertIsNotNone(pend)
            self.assertEqual(pend["value"]["tool_name"], "push_preview")
            arguments_hash = pend["value"]["arguments_hash"]
        with guarded_sqlite_checkpointer(self.db) as saver2:
            app2 = build_graph(self._deps(tool), checkpointer=saver2)
            decision = {"approved": True, "arguments_hash": arguments_hash}
            out2, interrupted2 = resume_graph(app2, decision, thread_id="resume-1")
        self.assertFalse(interrupted2)
        self.assertEqual(len(tool.calls), 1)
        statuses = [a.get("status") for a in (out2.get("approvals") or [])]
        self.assertEqual(statuses, ["approved"])
        self.assertEqual(out2.get("cursor"), 1)

    def test_rejection_after_resume_never_executes(self):
        tool = FakeTool("push_preview")
        state = make_state(user_query="预览一下晚间推送")
        with guarded_sqlite_checkpointer(self.db) as saver:
            app = build_graph(self._deps(tool), checkpointer=saver)
            _, interrupted = run_graph(app, state, thread_id="resume-2")
            self.assertTrue(interrupted)
        with guarded_sqlite_checkpointer(self.db) as saver2:
            app2 = build_graph(self._deps(tool), checkpointer=saver2)
            out2, _ = resume_graph(app2, {"approved": False}, thread_id="resume-2")
        self.assertEqual(tool.calls, [])
        statuses = [a.get("status") for a in (out2.get("approvals") or [])]
        self.assertEqual(statuses, ["rejected"])

    def test_wrong_arguments_hash_after_resume_is_rejected(self):
        tool = FakeTool("push_preview")
        state = make_state(user_query="预览一下晚间推送")
        with guarded_sqlite_checkpointer(self.db) as saver:
            app = build_graph(self._deps(tool), checkpointer=saver)
            _, interrupted = run_graph(app, state, thread_id="resume-3")
            self.assertTrue(interrupted)
        with guarded_sqlite_checkpointer(self.db) as saver2:
            app2 = build_graph(self._deps(tool), checkpointer=saver2)
            decision = {"approved": True, "arguments_hash": "0" * 64}
            out2, _ = resume_graph(app2, decision, thread_id="resume-3")
        self.assertEqual(tool.calls, [])
        statuses = [a.get("status") for a in (out2.get("approvals") or [])]
        self.assertEqual(statuses, ["rejected"])

    def test_checkpoint_audit_lists_thread(self):
        tool = FakeTool("push_preview")
        with guarded_sqlite_checkpointer(self.db) as saver:
            app = build_graph(self._deps(tool), checkpointer=saver)
            run_graph(app, make_state(user_query="预览一下晚间推送"), thread_id="audit-1")
        report = audit_checkpoint_database(self.db)
        self.assertTrue(report["readable"])
        self.assertIn("audit-1", report["threads"])
        self.assertTrue(report["fingerprint"])

    def test_corrupted_checkpoint_is_reported_not_silently_empty(self):
        with open(self.db, "wb") as fh:
            fh.write(b"not a sqlite database at all")
        report = audit_checkpoint_database(self.db)
        self.assertFalse(report["readable"])
        self.assertTrue(report["error"])

    def test_thread_id_required(self):
        app = build_graph(self._deps(FakeTool("push_preview")))
        state = make_state()
        state["thread_id"] = ""
        with self.assertRaises(ContractError) as ctx:
            run_graph(app, state, thread_id="")
        self.assertIn("STATE_NOT_STARTABLE", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
