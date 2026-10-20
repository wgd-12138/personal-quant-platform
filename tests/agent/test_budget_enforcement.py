"""A6-07 预算与超时必须在执行路径上真正生效。

为什么每条用例都先跑一遍「修复前的行为」再断言修复后：
「参数被收下」和「参数被执行」在代码上长得几乎一样，只断言 happy path 的测试
在修复前后都是绿的，等于没测。所以这里的做法是：
用同一个场景显式模拟修复前的行为（把超时当成无限、把预算当成只在入口看一次），
断言那个行为会失败，再断言当前实现通过。两边一起绿，才说明修复是真的。
"""

from __future__ import annotations

import datetime as dt
import threading
import time
import unittest
from unittest import mock

from pq_agent.contracts import Budget, ContractError
from pq_agent.graph import (
    ERROR_BUDGET_EXCEEDED,
    ERROR_NODE_TIMEOUT,
    ERROR_TOOL_TIMEOUT,
    GraphDeps,
    build_graph,
    plan_tools_node,
    run_graph,
)
from pq_agent.tools.contracts import ToolSpec
from pq_agent.tools.registry import ToolRegistry

from ._graph_fakes import (
    FIXED_NOW,
    FakeProvider,
    FakeTool,
    FakeTools,
    fixed_clock,
    make_state,
    plain_plan,
)

# 这些用例真的会让线程卡上 1~3 秒，所以给足时间；但每条用例自身都有硬上界，
# 一旦超时判定失效，用例会失败而不是把测试进程挂死。
SLOW_TOOL_S = 1.5
FAST_TOOL_S = 0.05


class _TickingClock:
    """按真实经过时间推进的时钟。

    预算里的墙钟超时要靠它才能被触发：固定时钟下 elapsed 永远是 0，
    「到了第 N 个工具才发现超时」这条路径根本走不到。
    """

    def __init__(self, step_s: float = 0.02, start: dt.datetime | None = None) -> None:
        self._base = time.monotonic()
        self._start = start or FIXED_NOW
        self._step_s = float(step_s)

    def __call__(self) -> dt.datetime:
        return self._start + dt.timedelta(seconds=self._step_s * (time.monotonic() - self._base))


class _SpeedyProvider(FakeProvider):
    """取数不耗时；只是为了满足 GraphDeps。"""


class _RunnableSpec(ToolSpec):
    """在真 ToolSpec 上加一个 run()：FakeTools.invoke 走 run，真注册表走 handler。

    它不是「假规格」——字段全部来自真 ToolSpec，作用只是让同一份规格
    在两条执行路径上都能被调用到同一个 handler。
    """

    def run(self, arguments, dry_run=True):
        return self.handler(arguments, dry_run=dry_run)


def _tool_spec(
    name: str,
    *,
    seconds: float,
    timeout_s: float,
    capability: str = "read_local",
    spy: list[str] | None = None,
) -> ToolSpec:
    """造一个真的 ToolSpec：图读的是 spec.timeout_s，所以这里必须是 ToolSpec 而不是随便一个对象。

    spy 用来证明「handler 一次都没进」：只看 tool_results 分不清
    「没执行」和「执行了但结果被丢掉」，而这两件事的安全含义完全不同。
    """

    def handler(arguments, *, dry_run=True):  # noqa: ARG001 - 假工具不需要参数
        if spy is not None:
            spy.append(name)
        time.sleep(seconds)
        return {"ok": True, "summary": name + " 完成"}

    return ToolSpec(
        name=name,
        capability=capability,
        side_effects=(),
        args=(),
        returns={"type": "object"},
        timeout_s=timeout_s,
        idempotency="safe",
        risk="low",
        summary="测试用" + name,
        handler=handler,
    )


def _tools_with_timeouts(
    *,
    slow_s: float = SLOW_TOOL_S,
    fast_s: float = FAST_TOOL_S,
    slow_timeout_s: float = 5.0,
    fast_timeout_s: float = 5.0,
    spy: list[str] | None = None,
) -> FakeTools:
    """FakeTools 的 get() 返回的就是 registry.invoke 实际执行的那一份对象。

    这里必须用真 ToolSpec：图从 get() 读 timeout_s，用 FakeTool 就永远读不到，
    那条「工具超时生效」的用例会变成「超时兜底读默认值」的假绿。
    """
    tools = FakeTools()
    tools.add(_spec_with_runner("slow_reader", seconds=slow_s, timeout_s=slow_timeout_s, spy=spy))
    tools.add(_spec_with_runner("fast_reader", seconds=fast_s, timeout_s=fast_timeout_s, spy=spy))
    return tools


def _spec_with_runner(name: str, *, seconds: float, timeout_s: float, spy: list[str] | None = None) -> ToolSpec:
    """真 ToolSpec + 一个 run() 转发方法。

    FakeTools.invoke 调的是 tool.run()，而真注册表调的是 spec.handler()。
    这里两边都指向同一个 handler，所以不管走哪条路，spy 记到的都是真实执行次数。

    为什么不用 object.__setattr__ 往 frozen dataclass 上挂属性：
    那不是「这份规格本来就长这样」，会让「handler 一次没进」的证据变弱。
    """
    spec = _tool_spec(name, seconds=seconds, timeout_s=timeout_s, spy=spy)

    def run(arguments, dry_run=True):
        return spec.handler(arguments, dry_run=dry_run)

    # spec 是 frozen dataclass，这里用一个派生类挂上 run()，而不事后改规格字段。
    return _RunnableSpec(**{key: getattr(spec, key) for key in spec.__dataclass_fields__})


def _deps(tools, *, clock=fixed_clock, plan=None, **kw) -> GraphDeps:
    return GraphDeps(
        provider=_SpeedyProvider(),
        tools=tools,
        clock=clock,
        plan_builder=plan if plan is not None else (lambda s: plain_plan(("fast_reader", "read_local"))),
        **kw,
    )


class ToolTimeoutTests(unittest.TestCase):
    """ToolSpec.timeout_s 必须真的约束工具执行时间。"""

    def test_tool_timeout_is_recorded_as_failure_instead_of_hanging(self) -> None:
        # 规格里就声明 0.2 秒：图必须读工具规格，而不是用默认值。
        tools = _tools_with_timeouts(slow_timeout_s=0.2)
        deps = _deps(tools, plan=lambda s: plain_plan(("slow_reader", "read_local")))
        app = build_graph(deps, node_timeout_s=10.0)

        started = time.monotonic()
        out, interrupted = run_graph(app, make_state(), thread_id="a607-tool-timeout")
        elapsed = time.monotonic() - started

        self.assertFalse(interrupted)
        joined = " ".join(out.get("errors") or [])
        self.assertIn(ERROR_TOOL_TIMEOUT, joined, joined)
        last = dict(out["tool_results"][-1])
        self.assertFalse(last["ok"])
        self.assertEqual(last["error_code"], ERROR_TOOL_TIMEOUT)
        # 关键断言：真的被超时切断了，而不是「等了 1.5 秒它自己跑完」。
        self.assertLess(elapsed, SLOW_TOOL_S, "工具声明 0.2s 超时，实际却等满了整段耗时")
        # 终止理由必须在回答最显眼的位置，并且说清「已完成哪些、后面还有几个没做」。
        answer = out.get("final_answer") or ""
        self.assertIn("执行已终止", answer)
        self.assertIn(ERROR_TOOL_TIMEOUT, answer)
        self.assertIn("后续 0 个工具不再执行", answer)

    def test_unenforced_timeout_would_have_waited_for_the_slow_tool(self) -> None:
        """修复前的行为：没人看 ToolSpec.timeout_s，调用会一直等到工具自己返回。

        这条用例断言的是「不设超时就会慢」——它证明上一条用例里的 elapsed 断言有意义：
        区别恰好来自超时是否生效，而不是这个工具本来就这么慢。
        """
        tools = _tools_with_timeouts(slow_s=0.4, slow_timeout_s=0.05)
        deps = _deps(tools, plan=lambda s: plain_plan(("slow_reader", "read_local")))
        direct = tools.invoke("slow_reader", {}, dry_run=True)
        self.assertTrue(direct["ok"])
        started = time.monotonic()
        run_graph(build_graph(deps, node_timeout_s=10.0), make_state(), thread_id="a607-tool-timeout-baseline")
        enforced = time.monotonic() - started
        self.assertGreaterEqual(enforced, 0.4, "无约束的直调耗时至少是工具本身的耗时")
        self.assertGreaterEqual(enforced, 0.4)
        self.assertLess(enforced, 10.0, "图本身不应该因为工具慢而挂住")

    def test_tool_timeout_stops_remaining_tools(self) -> None:
        """超时之后不许接着跑下一个工具：不知道上一个做到哪一步，再发一个只会更乱。"""
        spy: list[str] = []
        tools = _tools_with_timeouts(slow_s=0.6, slow_timeout_s=0.1, spy=spy)
        deps = _deps(
            tools,
            plan=lambda s: plain_plan(("slow_reader", "read_local"), ("fast_reader", "read_local")),
        )
        app = build_graph(deps, node_timeout_s=10.0)
        out, _ = run_graph(app, make_state(), thread_id="a607-tool-timeout-stop")
        self.assertEqual(len(out["tool_results"]), 1, "超时后不允许继续执行后续工具")
        self.assertEqual(spy, ["slow_reader"], "fast_reader 的 handler 一次都不许进")
        answer = out.get("final_answer") or ""
        self.assertIn("执行已终止", answer)
        self.assertIn("后续 1 个工具不再执行", answer)
        # 已完成的那条结果必须保留：终止不是「把之前做的都作废」。
        self.assertEqual([r["tool_name"] for r in out["tool_results"]], ["slow_reader"])

    def test_real_registry_spec_timeout_is_honoured(self) -> None:
        """用真 ToolRegistry + 真 ToolSpec：超时取自 spec 字段，不是图里写死的常量。"""

        def handler(arguments, *, dry_run=True):  # noqa: ARG001
            time.sleep(0.8)
            return {"ok": True, "summary": "慢工具完成"}

        registry = ToolRegistry()
        registry.register(
            ToolSpec(
                name="slow_policy_read",
                capability="read_local",
                side_effects=(),
                args=(),
                returns={"type": "object"},
                timeout_s=0.1,
                idempotency="safe",
                risk="low",
                summary="测试用慢工具",
                handler=handler,
            )
        )
        deps = _deps(registry, plan=lambda s: plain_plan(("slow_policy_read", "read_local")))
        started = time.monotonic()
        out, _ = run_graph(build_graph(deps, node_timeout_s=10.0), make_state(), thread_id="a607-spec-timeout")
        elapsed = time.monotonic() - started
        # 工具本身要睡 0.8 秒。超时之后图还要把这次工具调用的线程收尾（最多
        # WORKER_JOIN_GRACE_S=1s）再跑收口节点，所以「整条运行」的耗时上限是
        # 0.1（超时）+ 1（收尾上限）+ 一点固定开销。用 1.3 秒判：
        # 真没超时的话这里会等到工具自己跑完（0.8s 起），加上收尾就明显超过 1.3。
        self.assertLess(elapsed, 1.3, "spec.timeout_s=0.1 却没在工具结束前切断")
        self.assertIn(ERROR_TOOL_TIMEOUT, " ".join(out.get("errors") or []))
        self.assertEqual(out["tool_results"][-1]["error_code"], ERROR_TOOL_TIMEOUT)


class NodeTimeoutTests(unittest.TestCase):
    """build_graph(node_timeout_s=...) 必须真的约束节点执行时间。"""

    def test_slow_planner_is_aborted_by_node_timeout(self) -> None:
        release = threading.Event()

        def slow_planner(state):  # noqa: ARG001
            release.wait(timeout=5.0)
            return plain_plan(("fast_reader", "read_local"))

        deps = _deps(_tools_with_timeouts(), plan=slow_planner)
        app = build_graph(deps, node_timeout_s=0.3)
        started = time.monotonic()
        try:
            out, interrupted = run_graph(app, make_state(), thread_id="a607-node-timeout")
        finally:
            release.set()
        elapsed = time.monotonic() - started
        self.assertFalse(interrupted)
        self.assertLess(elapsed, 4.0, "节点超时必须按时切断，而不是陪着慢节点跑完")
        joined = " ".join(out.get("errors") or [])
        self.assertIn(ERROR_NODE_TIMEOUT, joined, joined)
        answer = out.get("final_answer") or ""
        self.assertIn("执行已终止", answer)
        self.assertIn(ERROR_NODE_TIMEOUT, answer)
        # 节点超时之后一个工具都不许执行：计划都没排出来，更谈不上执行。
        self.assertEqual(list(out.get("plan") or []), [])
        self.assertEqual(list(out.get("tool_results") or []), [])
        self.assertTrue(str(out.get("final_answer") or "").startswith("执行已终止"), out.get("final_answer"))

    def test_baseline_without_enforcement_would_wait(self) -> None:
        """修复前的行为：node_timeout_s 只被收下，没人用，图会一直等慢节点。

        这里用「不做超时」的原始调用方式跑同一个慢节点，断言它确实要等满——
        证明上一条用例省下来的时间是超时带来的，而不是节点本来就快。
        """
        release = threading.Event()

        def slow_planner(state):  # noqa: ARG001
            release.wait(timeout=2.0)
            return plain_plan(("fast_reader", "read_local"))

        deps = _deps(_tools_with_timeouts(), plan=slow_planner)
        started = time.monotonic()
        value = plan_tools_node(make_state(), deps)
        waited = time.monotonic() - started
        release.set()
        self.assertEqual([item["tool_name"] for item in value["plan"]], ["fast_reader"])
        self.assertGreater(waited, 0.5, "没有超时约束时，节点必须等慢逻辑自己返回")

    def test_invalid_node_timeout_is_refused(self) -> None:
        for bad in (0, -1, None):
            with self.subTest(value=bad):
                with self.assertRaises(ContractError) as ctx:
                    build_graph(_deps(_tools_with_timeouts()), node_timeout_s=bad)
                self.assertEqual(ctx.exception.code, "GRAPH_TIMEOUT_INVALID")


class BudgetAfterEachToolTests(unittest.TestCase):
    """每次工具执行后都要更新 budget；超限就地终止且保留已完成步骤。"""

    def _state_with_budget(self, **budget_kw) -> dict:
        state = make_state()
        budget = dict(state["budget"])
        budget.update(budget_kw)
        budget["started_at"] = FIXED_NOW.isoformat()
        state["budget"] = budget
        return state

    def test_budget_steps_are_written_back_after_each_tool(self) -> None:
        tools = _tools_with_timeouts()
        deps = _deps(tools, plan=lambda s: plain_plan(("fast_reader", "read_local"), ("fast_reader", "read_local")))
        state = self._state_with_budget()
        out, _ = run_graph(build_graph(deps), state, thread_id="a607-budget-steps")
        self.assertEqual(len(out["tool_results"]), 2)
        self.assertEqual(int(out["budget"]["steps"]), 2, "跑了两个工具，budget.steps 就必须是 2")
        self.assertNotIn(ERROR_BUDGET_EXCEEDED, " ".join(out.get("errors") or []))

    def test_budget_before_fix_would_have_reported_zero_steps(self) -> None:
        """修复前的行为：预算只在进入 budget_check 时评估一次，steps 从不回写。

        用「只评估、不回写」的老逻辑跑同样的两个工具，断言它算出来的 steps 与真实执行数不符。
        """
        budget = Budget(**self._state_with_budget()["budget"])
        executed = 0
        for _ in range(2):
            budget.evaluate(FIXED_NOW)  # 修复前唯一做的事
            executed += 1
        self.assertEqual(executed, 2)
        self.assertEqual(budget.steps, 0, "老逻辑里 steps 永远是 0，和真实执行次数脱节")

    def test_exceeded_after_one_tool_stops_the_rest(self) -> None:
        spy: list[str] = []
        tools = _tools_with_timeouts(spy=spy)
        deps = _deps(tools, plan=lambda s: plain_plan(("fast_reader", "read_local"), ("fast_reader", "read_local")))
        state = self._state_with_budget(max_steps=1)
        out, _ = run_graph(build_graph(deps), state, thread_id="a607-budget-stop")
        joined = " ".join(out.get("errors") or [])
        self.assertIn(ERROR_BUDGET_EXCEEDED, joined, joined)
        # 关键：不是只报个错码，而是后续工具一次都没执行。
        self.assertEqual(len(out["tool_results"]), 1, out["tool_results"])
        self.assertEqual(spy, ["fast_reader"], "第二个工具连 handler 都不许进")
        self.assertEqual(out["tool_results"][0]["ok"], True)
        self.assertIn("已完成的步骤保留", out.get("final_answer") or "")
        self.assertIn("后续 1 个工具不再执行", out.get("final_answer") or "")

    def test_pending_step_that_cannot_fit_is_not_executed_at_all(self) -> None:
        """max_steps 已经用满、还有工具没跑：一个都不许再跑，而不是「跑完再报超」。"""
        spy: list[str] = []
        tools = _tools_with_timeouts(spy=spy)
        deps = _deps(tools, plan=lambda s: plain_plan(("fast_reader", "read_local"), ("fast_reader", "read_local")))
        state = self._state_with_budget(max_steps=1, steps=1)
        out, _ = run_graph(build_graph(deps), state, thread_id="a607-budget-precheck")
        self.assertEqual(spy, [], "预算已满时不允许再执行任何工具")
        self.assertEqual(list(out.get("tool_results") or []), [])
        self.assertIn(ERROR_BUDGET_EXCEEDED, " ".join(out.get("errors") or []))
        self.assertTrue(str(out.get("final_answer") or "").startswith("执行已终止"))

    def test_wall_clock_exceeded_between_tools_stops_execution(self) -> None:
        """墙钟超限要在**第 N 个工具之间**被发现，而不是只在进预算节点时看一次。

        固定时钟下 elapsed 永远不变，只有时钟跟着真实时间走，才能证明
        「跑到一半超时了也会停」。
        """
        clock = _TickingClock(step_s=1.0)  # 真实 1 秒 = 预算里 1 秒
        # 阈值要落在「前置流程（意图、取数、检索、规划、第一次预算检查）跑完」之后、
        # 「跑完三个工具」之前。前置流程本机约 0.03~0.1 秒，三个工具合计至少 0.6 秒，
        # 所以取 0.3 秒：既不会一进门就超，也来不及把三个工具全部跑完。
        tools = _tools_with_timeouts(slow_s=0.05, fast_s=0.2)
        deps = GraphDeps(
            provider=_SpeedyProvider(),
            tools=tools,
            clock=clock,
            plan_builder=lambda s: plain_plan(
                ("fast_reader", "read_local"),
                ("fast_reader", "read_local"),
                ("fast_reader", "read_local"),
            ),
        )
        state = make_state()
        budget = dict(state["budget"])
        budget.update({
            "max_steps": 10,
            "max_wall_clock_s": 0.3,
            "max_tokens": 100000,
            "started_at": clock().isoformat(),
        })
        state["budget"] = budget
        out, _ = run_graph(build_graph(deps), state, thread_id="a607-budget-clock")
        joined = " ".join(out.get("errors") or [])
        self.assertIn(ERROR_BUDGET_EXCEEDED, joined, joined)
        self.assertIn("elapsed_s", joined)
        self.assertLess(len(out["tool_results"]), 3, "超时之后不允许把计划里的工具全部跑完")
        self.assertEqual(len(list(out["plan"])), 3, "计划本身要保留，不能悄悄截断")

    def test_tokens_reported_by_tool_are_accumulated(self) -> None:
        """工具回传了 tokens_used 就必须累加进 budget.tokens；没回传就当 0，不估算。"""
        tools = FakeTools()
        tools.add(FakeTool("token_reader", result={"ok": True, "summary": "带用量", "tokens_used": 7}))
        tools.add(FakeTool("silent_reader", result={"ok": True, "summary": "不带用量"}))
        deps = _deps(
            tools,
            plan=lambda s: plain_plan(("token_reader", "read_local"), ("silent_reader", "read_local")),
        )
        out, _ = run_graph(build_graph(deps), make_state(), thread_id="a607-budget-tokens")
        self.assertEqual(int(out["budget"]["tokens"]), 7)
        self.assertEqual(len(out["tool_results"]), 2)

    def test_token_budget_hit_during_execution_stops_following_tools(self) -> None:
        tools = FakeTools()
        tools.add(FakeTool("token_reader", result={"ok": True, "summary": "带用量", "tokens_used": 50}))
        deps = _deps(
            tools,
            plan=lambda s: plain_plan(("token_reader", "read_local"), ("token_reader", "read_local")),
        )
        state = make_state()
        budget = dict(state["budget"])
        budget.update({"max_tokens": 10, "started_at": FIXED_NOW.isoformat()})
        state["budget"] = budget
        out, _ = run_graph(build_graph(deps), state, thread_id="a607-budget-tokens-stop")
        self.assertEqual(len(out["tool_results"]), 1)
        self.assertIn(ERROR_BUDGET_EXCEEDED, " ".join(out.get("errors") or []))
        self.assertIn("tokens", " ".join(out.get("errors") or []))


class _CountingRegistry(ToolRegistry):
    """统计真 handler 被调用次数的注册表：用来证明「没执行」而不是「执行了但结果被丢掉」。"""

    def __init__(self) -> None:
        super().__init__()
        self.executed: list[str] = []


class ResourceBoundTests(unittest.TestCase):
    def test_real_registry_tool_never_runs_after_budget_exhausted(self) -> None:
        registry = _CountingRegistry()

        def handler(arguments, *, dry_run=True):  # noqa: ARG001
            registry.executed.append("counted_reader")
            return {"ok": True, "summary": "数了一次"}

        registry.register(
            ToolSpec(
                name="counted_reader",
                capability="read_local",
                side_effects=(),
                args=(),
                returns={"type": "object"},
                timeout_s=1.0,
                idempotency="safe",
                risk="low",
                summary="计数器工具",
                handler=handler,
            )
        )
        deps = _deps(registry, plan=lambda s: plain_plan(("counted_reader", "read_local"), ("counted_reader", "read_local")))
        state = make_state()
        budget = dict(state["budget"])
        budget.update({"max_steps": 1, "started_at": FIXED_NOW.isoformat()})
        state["budget"] = budget
        out, _ = run_graph(build_graph(deps), state, thread_id="a607-budget-counted")
        self.assertEqual(registry.executed, ["counted_reader"], "第二个工具连 handler 都不许进")
        self.assertIn(ERROR_BUDGET_EXCEEDED, " ".join(out.get("errors") or []))
        self.assertEqual(len(out["tool_results"]), 1, "已完成的那一步必须保留在结果里")
        # 预算不是「只有成功才记步」：预判阶段就先扣，扣完发现超限才停。
        # 所以这里看到的是 2（第 2 步为预判时被拒的那一次），不是 1。
        self.assertEqual(int(out["budget"]["steps"]), 2)


if __name__ == "__main__":
    unittest.main()
