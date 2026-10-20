"""A1-03 图的边与路由：正常问答、缺数据、非法意图、工具失败、审批拒绝、预算超限、取消。"""

from __future__ import annotations

import unittest

from pq_agent.graph import (
    ERROR_APPROVAL_REJECTED,
    ERROR_BUDGET_EXCEEDED,
    ERROR_CANCELLED,
    ERROR_TOOL_FAILED,
    ERROR_TOOL_UNKNOWN,
    NODE_HANDLE_ERROR,
    GraphDeps,
    build_graph,
    graph_structure,
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


class RouteTests(unittest.TestCase):
    def _deps(self, provider=None, tools=None, **kw):
        return GraphDeps(
            provider=provider or FakeProvider(),
            tools=tools or FakeTools("market_snapshot"),
            clock=fixed_clock,
            **kw,
        )

    def test_normal_replay_question_runs_read_tool(self):
        tools = FakeTools("market_snapshot")
        app = build_graph(self._deps(tools=tools))
        out, interrupted = run_graph(app, make_state(), thread_id="t-graph")
        self.assertFalse(interrupted)
        self.assertEqual(out.get("cursor"), 1)
        self.assertEqual(len(tools.calls("market_snapshot")), 1)
        self.assertIn("数据快照", out.get("final_answer"))
        self.assertTrue(out.get("data_snapshot_id"))

    def test_missing_data_goes_straight_to_answer(self):
        provider = FakeProvider(fail=True)
        tools = FakeTools("market_snapshot")
        app = build_graph(self._deps(provider=provider, tools=tools))
        out, _ = run_graph(app, make_state(), thread_id="t-graph")
        self.assertIsNone(out.get("data_snapshot_id"))
        self.assertEqual(tools.calls("market_snapshot"), [])
        self.assertIn("不输出任何行情结论", out.get("final_answer"))
        self.assertTrue(any("INPUTS_NOT_READY" in e for e in out.get("errors")))

    def test_illegal_intent_has_no_plan_and_no_tool_call(self):
        tools = FakeTools("market_snapshot")
        app = build_graph(self._deps(tools=tools))
        state = make_state(user_query="嗯")
        out, _ = run_graph(app, state, thread_id="t-graph")
        self.assertEqual(out.get("intent"), "fallback")
        self.assertEqual(out.get("plan"), [])
        self.assertEqual(tools.calls("market_snapshot"), [])

    def test_tool_failure_is_recorded_and_not_retried(self):
        boom = FakeTool("market_snapshot", raises=RuntimeError("boom"))
        tools = FakeTools()
        tools.add(boom)
        app = build_graph(self._deps(tools=tools))
        out, _ = run_graph(app, make_state(), thread_id="t-graph")
        self.assertEqual(len(boom.calls), 1)
        codes = [r.get("error_code") for r in (out.get("tool_results") or [])]
        self.assertIn(ERROR_TOOL_FAILED, codes)

    def test_unknown_tool_is_recorded(self):
        tools = FakeTools()
        app = build_graph(self._deps(tools=tools))
        out, _ = run_graph(app, make_state(), thread_id="t-graph")
        codes = [r.get("error_code") for r in (out.get("tool_results") or [])]
        self.assertIn(ERROR_TOOL_UNKNOWN, codes)

    def test_approval_rejection_skips_tool_but_advances(self):
        tool = FakeTool("push_preview")
        tools = FakeTools()
        tools.add(tool)
        deps = self._deps(
            tools=tools,
            plan_builder=lambda s: plain_plan(("push_preview", "business_write")),
            approver=lambda s, c: {"approved": False, "reason": "不批"},
        )
        app = build_graph(deps)
        out, _ = run_graph(app, make_state(), thread_id="t-graph")
        self.assertEqual(tool.calls, [])
        self.assertEqual(out.get("cursor"), 1)
        self.assertTrue(any(ERROR_APPROVAL_REJECTED in e for e in out.get("errors")))
        statuses = [a.get("status") for a in (out.get("approvals") or [])]
        self.assertEqual(statuses, ["rejected"])

    def test_budget_exceeded_terminates_with_reason(self):
        app = build_graph(self._deps())
        state = make_state()
        budget = dict(state["budget"])
        budget["max_steps"] = 1
        budget["steps"] = 5
        state["budget"] = budget
        out, _ = run_graph(app, state, thread_id="t-graph")
        self.assertTrue(any(ERROR_BUDGET_EXCEEDED in e for e in out.get("errors")))
        self.assertIn("执行已终止", out.get("final_answer"))

    def test_cancelled_run_short_circuits(self):
        tools = FakeTools("market_snapshot")
        app = build_graph(self._deps(tools=tools))
        state = make_state()
        # 取消通过 errors 通道表达（AgentState 的 23 字段由 A1-01 冻结，不能新增 cancelled）
        state["errors"] = ["external | RUN_CANCELLED: 用户请求取消"]
        out, _ = run_graph(app, state, thread_id="t-graph")
        self.assertTrue(any(ERROR_CANCELLED in e for e in out.get("errors")))
        self.assertEqual(tools.calls("market_snapshot"), [])

    def test_graph_has_no_forbidden_nodes(self):
        app = build_graph(self._deps())
        structure = graph_structure(app)
        nodes = structure["nodes"]
        for node in nodes:
            low = node.lower()
            self.assertNotIn("broker", low)
            self.assertNotIn("execution", low)
            self.assertNotIn("loop", low)
        self.assertIn(NODE_HANDLE_ERROR, nodes)
        self.assertIn("execute_tool->budget_check", structure["edges"])


if __name__ == "__main__":
    unittest.main()
