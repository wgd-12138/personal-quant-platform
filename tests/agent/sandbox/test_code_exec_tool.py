"""A6-03：code_exec 从计划到审批到沙箱执行到最终回答。

为什么这里用真注册表而不是 FakeTools：
A1-07 的教训是假实现的签名与真实现不一致时会给出虚假的绿色
（graph.py 调用不存在的 ToolSpec.run 这个致命缺陷靠 FakeTools 躲过了三轮任务）。
这里一律走真 ToolSpec、真参数校验、真审批哈希，只把「运行时」换成可控的假实现。

本机没有 Docker，所以边界分两层：
- 容器命令、stdin 信封、输出上限、超时清理由 tests/agent/sandbox/test_docker_exec.py
  用注入的 spawn 验证；
- 这里验证「上层是否真的只走沙箱」以及「沙箱不可用或能力不够时必须明确拒绝」。
假运行时只证明接线，不冒充真实隔离。
"""

from __future__ import annotations

import pathlib
import unittest
import uuid

from pq_agent.contracts import CAPABILITIES, ToolCall
from pq_agent.graph import (
    APPROVAL_REQUIRED_CAPABILITIES,
    CAPABILITY_CODE_EXEC,
    ERROR_APPROVAL_REJECTED,
    ERROR_TOOL_UNKNOWN,
    GraphDeps,
    build_graph,
    run_graph,
    thread_config,
)
from pq_agent.sandbox.base import STATUS_OK, ExecResult
from pq_agent.sandbox.docker_runtime import DockerProbe, DockerRuntime
from pq_agent.sandbox.local_runtime import LocalRuntime
from pq_agent.tools import CODE_EXEC_TOOL_NAME, build_default_registry, make_code_exec_spec
from pq_agent.tools.contracts import ToolError

from .._graph_fakes import FakeProvider, fixed_clock, make_state

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _tmp_dir(name: str) -> pathlib.Path:
    """刻意不用 tempfile：本机沙箱把 0o700 目录映射成自己无权的 DACL。"""
    path = ROOT / ".tmp" / "a6-03-tests" / (name + "-" + uuid.uuid4().hex[:8])
    path.mkdir(parents=True, exist_ok=True)
    return path


def _absent_probe() -> DockerProbe:
    return DockerProbe(False, "docker", "", "", "命令不存在：docker", "2026-10-16T00:00:00+0800")


class RecordingSandbox:
    """记录调用的假容器运行时：证明接线，不冒充隔离。"""

    name = "fake-container"
    isolation_level = "container"

    def __init__(self, *, result=None, error=None):
        self.calls = []
        self._result = result
        self._error = error

    def capabilities(self):
        return {"supported": ["untrusted_code_safe"], "unsupported": []}

    def execute(self, request):
        self.calls.append(request)
        if self._error is not None:
            raise self._error
        if self._result is not None:
            return self._result
        return ExecResult(
            status=STATUS_OK,
            executed=True,
            stdout="hello-from-sandbox",
            exit_code=0,
            runtime=self.name,
            isolation_level="container",
        )


class CapabilityContractTests(unittest.TestCase):
    def test_code_exec_is_a_declared_capability(self):
        self.assertEqual(CAPABILITY_CODE_EXEC, "code_exec")
        self.assertIn(CAPABILITY_CODE_EXEC, CAPABILITIES)

    def test_tool_call_accepts_code_exec_and_round_trips(self):
        call = ToolCall(
            call_id="c0",
            tool_name=CODE_EXEC_TOOL_NAME,
            arguments={"source": "print(1)"},
            capability=CAPABILITY_CODE_EXEC,
            dry_run=False,
        )
        again = ToolCall.from_dict(call.to_dict())
        self.assertEqual(again.capability, CAPABILITY_CODE_EXEC)
        self.assertFalse(again.dry_run)

    def test_code_exec_always_needs_approval(self):
        spec = make_code_exec_spec(sandbox=RecordingSandbox())
        self.assertTrue(spec.requires_approval)
        self.assertEqual(tuple(spec.side_effects), ("code_execution",))
        self.assertIn(CAPABILITY_CODE_EXEC, APPROVAL_REQUIRED_CAPABILITIES)
        self.assertEqual(tuple(spec.arg_names), ("source", "stdin", "mode", "timeout_s"))
        self.assertEqual(tuple(spec.required_arg_names), ("source",))


class RegistryGateTests(unittest.TestCase):
    def test_without_sandbox_there_is_no_code_exec_tool(self):
        reg = build_default_registry()
        self.assertNotIn(CODE_EXEC_TOOL_NAME, reg.names())

    def test_with_sandbox_code_exec_is_registered(self):
        reg = build_default_registry(sandbox=RecordingSandbox())
        self.assertIn(CODE_EXEC_TOOL_NAME, reg.names())

    def test_missing_approval_means_zero_execution(self):
        sandbox = RecordingSandbox()
        reg = build_default_registry(sandbox=sandbox)
        with self.assertRaises(ToolError) as ctx:
            reg.invoke(
                CODE_EXEC_TOOL_NAME,
                {"source": "print(1)", "mode": "execute"},
                approval_token=None,
                dry_run=False,
            )
        self.assertIn("TOOL_APPROVAL_REQUIRED", str(ctx.exception))
        self.assertEqual(sandbox.calls, [])

    def test_wrong_approval_token_is_rejected_before_execution(self):
        sandbox = RecordingSandbox()
        reg = build_default_registry(sandbox=sandbox)
        with self.assertRaises(ToolError) as ctx:
            reg.invoke(
                CODE_EXEC_TOOL_NAME,
                {"source": "print(1)", "mode": "execute"},
                approval_token="deadbeef",
                dry_run=False,
            )
        self.assertIn("TOOL_APPROVAL_REQUIRED", str(ctx.exception))
        self.assertEqual(sandbox.calls, [])

    def test_preview_mode_never_touches_the_sandbox(self):
        sandbox = RecordingSandbox()
        reg = build_default_registry(sandbox=sandbox)
        args = {"source": "print(1)"}
        token = reg.preview(CODE_EXEC_TOOL_NAME, args)["arguments_hash"]
        out = reg.invoke(CODE_EXEC_TOOL_NAME, args, approval_token=token, dry_run=True)
        self.assertTrue(out["ok"])
        self.assertEqual(sandbox.calls, [])
        self.assertEqual(out["data"]["mode"], "preview")
        self.assertFalse(out["data"]["executed"])

    def test_execute_mode_runs_the_sandbox_exactly_once(self):
        sandbox = RecordingSandbox()
        reg = build_default_registry(sandbox=sandbox)
        args = {"source": "print(1)", "mode": "execute"}
        token = reg.preview(CODE_EXEC_TOOL_NAME, args)["arguments_hash"]
        out = reg.invoke(CODE_EXEC_TOOL_NAME, args, approval_token=token, dry_run=False)
        self.assertTrue(out["ok"])
        self.assertEqual(len(sandbox.calls), 1)
        request = sandbox.calls[0]
        self.assertEqual(request.code, "print(1)")
        self.assertIn("untrusted_code_safe", tuple(request.require))
        self.assertEqual(out["data"]["stdout"], "hello-from-sandbox")
        self.assertEqual(out["data"]["runtime"], "fake-container")
        self.assertEqual(out["data"]["isolation_level"], "container")
        self.assertFalse(out["dry_run"])


class RuntimeRefusalTests(unittest.TestCase):
    def _execute_args(self):
        return {"source": "print(1)", "mode": "execute"}

    def test_local_runtime_refuses_untrusted_code(self):
        """LocalRuntime 的 untrusted_code_safe 是 unsupported：必须明确拒绝，
        不允许拿「约束层」冒充隔离来把代码跑起来。"""
        runtime = LocalRuntime(root=_tmp_dir("local"))
        reg = build_default_registry(sandbox=runtime)
        args = self._execute_args()
        token = reg.preview(CODE_EXEC_TOOL_NAME, args)["arguments_hash"]
        with self.assertRaises(ToolError) as ctx:
            reg.invoke(CODE_EXEC_TOOL_NAME, args, approval_token=token, dry_run=False)
        self.assertIn("TOOL_SANDBOX_BLOCKED", str(ctx.exception))
        self.assertIn("SANDBOX_CAPABILITY_UNSUPPORTED", str(ctx.exception))

    def test_unavailable_docker_blocks_and_never_spawns(self):
        """Docker 不可用时结果是拒绝，绝不回落 LocalRuntime。"""
        spawned = []
        runtime = DockerRuntime(
            root=ROOT,
            probe=_absent_probe(),
            spawn=lambda argv: spawned.append(argv),
            allow_untrusted=True,
        )
        reg = build_default_registry(sandbox=runtime)
        args = self._execute_args()
        token = reg.preview(CODE_EXEC_TOOL_NAME, args)["arguments_hash"]
        with self.assertRaises(ToolError) as ctx:
            reg.invoke(CODE_EXEC_TOOL_NAME, args, approval_token=token, dry_run=False)
        self.assertIn("TOOL_SANDBOX_BLOCKED", str(ctx.exception))
        self.assertIn("SANDBOX_CAPABILITY_UNSUPPORTED", str(ctx.exception))
        self.assertEqual(spawned, [])
        self.assertEqual(runtime.isolation_level, "container")


class GraphCodeExecTests(unittest.TestCase):
    """主图链路：计划 → 人工审批 → 沙箱执行 → ToolResult → checkpoint → 最终回答。"""

    THREAD = "t-code-exec"

    def _deps(self, tools, *, approver=None):
        return GraphDeps(provider=FakeProvider(), tools=tools, clock=fixed_clock, approver=approver)

    def _state(self, **overrides):
        params = {"thread_id": self.THREAD, "user_query": "帮我写个脚本算一下相关性"}
        params.update(overrides)
        return make_state(**params)

    def _state_with_code(self):
        return self._state(slots={"source": "print(1)", "mode": "execute"})

    def test_graph_plans_approves_executes_and_answers(self):
        sandbox = RecordingSandbox()
        tools = build_default_registry(sandbox=sandbox)
        app = build_graph(self._deps(tools, approver=lambda state, call: True))
        out, interrupted = run_graph(app, self._state_with_code(), thread_id=self.THREAD)
        self.assertFalse(interrupted)
        self.assertEqual(out.get("intent"), "code_task")
        plan = list(out.get("plan") or [])
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["tool_name"], CODE_EXEC_TOOL_NAME)
        self.assertEqual(plan[0]["capability"], CAPABILITY_CODE_EXEC)
        self.assertFalse(plan[0]["dry_run"], "调用方要求 execute，计划就不该再标 dry_run")
        self.assertEqual([item["status"] for item in out.get("approvals") or []], ["approved"])
        self.assertEqual(len(sandbox.calls), 1)
        results = list(out.get("tool_results") or [])
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["ok"])
        self.assertEqual(out.get("cursor"), 1)
        self.assertIn(CODE_EXEC_TOOL_NAME, str(out.get("final_answer")))

    def test_rejection_means_the_sandbox_is_never_called(self):
        sandbox = RecordingSandbox()
        tools = build_default_registry(sandbox=sandbox)
        app = build_graph(self._deps(tools, approver=lambda state, call: False))
        out, _ = run_graph(app, self._state_with_code(), thread_id=self.THREAD)
        self.assertEqual(sandbox.calls, [])
        self.assertEqual(out.get("tool_results") or [], [])
        self.assertTrue(any(ERROR_APPROVAL_REJECTED in str(e) for e in out.get("errors") or []))
        self.assertEqual([item["status"] for item in out.get("approvals") or []], ["rejected"])

    def test_execution_lands_in_the_checkpoint(self):
        from langgraph.checkpoint.memory import InMemorySaver

        sandbox = RecordingSandbox()
        tools = build_default_registry(sandbox=sandbox)
        app = build_graph(
            self._deps(tools, approver=lambda state, call: True),
            checkpointer=InMemorySaver(),
        )
        run_graph(app, self._state_with_code(), thread_id=self.THREAD)
        values = dict(app.get_state(thread_config(self.THREAD)).values or {})
        self.assertEqual(values.get("intent"), "code_task")
        results = list(values.get("tool_results") or [])
        self.assertEqual(len(results), 1)
        self.assertIn("hello-from-sandbox", str(results))

    def test_no_sandbox_means_no_execution_and_an_honest_error(self):
        """没有注入沙箱时连工具都不该登记：错误要说清楚，而不是假装跑过。"""
        tools = build_default_registry()
        app = build_graph(self._deps(tools, approver=lambda state, call: True))
        out, _ = run_graph(app, self._state_with_code(), thread_id=self.THREAD)
        codes = [item.get("error_code") for item in (out.get("tool_results") or [])]
        self.assertIn(ERROR_TOOL_UNKNOWN, codes)


if __name__ == "__main__":
    unittest.main()