"""A1-03 预算闸门：步数 / token / 墙钟超限都必须明确终止并给出原因。"""

from __future__ import annotations

import datetime as dt
import unittest

from pq_agent.contracts import Budget, ContractError
from pq_agent.graph import (
    ERROR_BUDGET_EXCEEDED,
    GraphDeps,
    build_graph,
    route_after_budget,
    run_graph,
)
from ._graph_fakes import (
    FIXED_NOW,
    FakeProvider,
    FakeTool,
    FakeTools,
    fixed_clock,
    make_state,
    plain_plan,
)


class BudgetEvaluateTests(unittest.TestCase):
    """Budget.evaluate 是纯函数，先把三条阈值单独钉住。"""

    def test_steps_limit(self):
        budget = Budget(max_steps=3, steps=3, started_at=FIXED_NOW.isoformat())
        self.assertTrue(budget.evaluate(FIXED_NOW, extra_steps=0)["ok"])
        verdict = budget.evaluate(FIXED_NOW, extra_steps=1)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["code"], "BUDGET_EXCEEDED")
        self.assertIn("steps", verdict["reason"])

    def test_token_limit(self):
        budget = Budget(max_tokens=100, tokens=100, started_at=FIXED_NOW.isoformat())
        verdict = budget.evaluate(FIXED_NOW, extra_tokens=1)
        self.assertFalse(verdict["ok"])
        self.assertIn("tokens", verdict["reason"])

    def test_wall_clock_limit(self):
        start = FIXED_NOW - dt.timedelta(seconds=10)
        budget = Budget(max_wall_clock_s=5.0, started_at=start.isoformat())
        verdict = budget.evaluate(FIXED_NOW)
        self.assertFalse(verdict["ok"])
        self.assertIn("elapsed_s", verdict["reason"])

    def test_naive_now_is_rejected(self):
        with self.assertRaises(ContractError) as ctx:
            Budget().evaluate(dt.datetime(2026, 7, 9, 16, 30))
        self.assertIn("DATETIME_NAIVE", str(ctx.exception))


class BudgetNodeTests(unittest.TestCase):
    def _deps(self, **kw):
        tools = FakeTools("market_snapshot")
        return GraphDeps(provider=FakeProvider(), tools=tools, clock=fixed_clock, **kw)

    def test_budget_exceeded_stops_before_any_tool(self):
        deps = self._deps()
        app = build_graph(deps)
        state = make_state()
        budget = dict(state["budget"])
        budget["max_steps"] = 2
        budget["steps"] = 99
        state["budget"] = budget
        out, _ = run_graph(app, state, thread_id="budget-1")
        self.assertTrue(any(ERROR_BUDGET_EXCEEDED in e for e in out.get("errors")))
        self.assertEqual(deps.tools.calls("market_snapshot"), [])
        self.assertEqual(out.get("final_answer").startswith("执行已终止"), True)

    def test_wall_clock_exceeded_also_stops(self):
        late = FIXED_NOW + dt.timedelta(seconds=1000)
        deps = GraphDeps(provider=FakeProvider(), tools=FakeTools("market_snapshot"),
                         clock=lambda: late)
        app = build_graph(deps)
        state = make_state()
        budget = dict(state["budget"])
        budget["max_wall_clock_s"] = 1.0
        budget["started_at"] = FIXED_NOW.isoformat()
        state["budget"] = budget
        out, _ = run_graph(app, state, thread_id="budget-2")
        self.assertTrue(any(ERROR_BUDGET_EXCEEDED in e for e in out.get("errors")))

    def test_plan_exhausted_routes_to_done(self):
        deps = GraphDeps(provider=FakeProvider(), tools=FakeTools(), clock=fixed_clock,
                         plan_builder=lambda s: plain_plan(("a", "read_local")))
        app = build_graph(deps)
        out, _ = run_graph(app, make_state(), thread_id="budget-3")
        self.assertEqual(out.get("cursor"), 1)
        self.assertIn("意图", out.get("final_answer"))

    def test_route_after_budget_prefers_done_when_exhausted(self):
        state = {"errors": [], "plan": [], "cursor": 0}
        self.assertEqual(route_after_budget(state), "done")


if __name__ == "__main__":
    unittest.main()
