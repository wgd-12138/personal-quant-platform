"""A6-01：主图真实集成测试，证明一次真实请求会走完意图、检索、规划与执行节点。

为什么必须走服务层而不是直接调节点：评审指出的问题正是「服务层没有把 retriever /
intent_model 接进图」，所以只有从 AgentService 出发的断言才能证明接线真实生效。
"""
from __future__ import annotations

import json
import pathlib
import unittest
from unittest import mock

from pq_agent.graph import NODE_CLASSIFY_INTENT, NODE_COMPOSE_ANSWER, NODE_EXECUTE_TOOL, NODE_PLAN_TOOLS, NODE_RAG_RETRIEVE
from pq_agent.observability import shutdown_tracing
from pq_agent.rag.retriever import build_retriever
from pq_agent.server import AgentService, ServerSettings, build_demo_service

ROOT = pathlib.Path(__file__).resolve().parents[2]


class LowConfidenceModel:
    """合法的 L2 输出，但置信度低于阈值 —— 图必须停下澄清。"""

    name = "fake-low-confidence"

    def complete(self, prompt: str) -> str:
        return json.dumps({"intent": "market_check", "confidence": 0.1, "slots": {}})


class HighConfidenceModel:
    name = "fake-high-confidence"

    def complete(self, prompt: str) -> str:
        return json.dumps({"intent": "market_check", "confidence": 0.98, "slots": {}})


class MainGraphIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.index_path = ROOT / "agent_eval" / "results" / "rag_index.json"
        if not cls.index_path.is_file():
            raise unittest.SkipTest("缺少 RAG 索引，先跑 A3 的索引构建")

    def tearDown(self) -> None:
        shutdown_tracing()

    def _service(self, **kwargs) -> AgentService:
        runtime = ROOT / ".tmp" / "a6-01-tests" / (self._testMethodName + "-" + str(id(self)))
        # 这些用例要证明「主图真的走完了意图→取数→执行」，所以服务必须以 analyst 装配：
        # 默认角色 supervisor 的能力集为空，market_snapshot 会在执行前被角色守卫拦下，
        # 那样测出来的「零执行」分不清是低置信度造成的还是角色造成的。
        settings = ServerSettings(
            demo=True, port=0 or 8199, token="a6-01-token-0123456789", runtime_dir=runtime, role="analyst"
        )
        retriever = build_retriever(index_path=self.index_path, root=ROOT)
        return build_demo_service(settings, retriever=retriever, **kwargs)

    def test_service_injects_retriever_into_graph(self) -> None:
        service = self._service()
        deps = service.deps()
        self.assertIsNotNone(deps.retriever, "AgentService 必须把 retriever 接进 GraphDeps")
        self.assertIs(deps.retriever, service.retriever())

    def test_run_walks_full_node_sequence(self) -> None:
        service = self._service()
        session = service.create_session("local")
        run = service.start_run(session, query="今天大盘怎么样", thread_id="t-a6-01-full")
        import asyncio

        async def drain() -> None:
            async for _ in service.stream_events(run):
                pass

        asyncio.run(drain())
        trace = service.trace_payload(run.trace_id)
        sequence = list((trace.get("trace") or {}).get("node_sequence") or [])
        for node in (NODE_CLASSIFY_INTENT, NODE_RAG_RETRIEVE, NODE_PLAN_TOOLS, NODE_EXECUTE_TOOL, NODE_COMPOSE_ANSWER):
            self.assertIn(node, sequence, "主图必须经过 " + node)
        state = service.graph_state(session, run.thread_id)
        errors = " ".join(state["graph"].get("errors") or [])
        self.assertNotIn("RAG_UNAVAILABLE", errors, "已注入 retriever 时不允许再报 RAG_UNAVAILABLE")
        self.assertTrue(state["graph"].get("citations"), "主图检索引用必须可由状态接口核对")

    def test_low_confidence_reaches_zero_real_tool_calls(self) -> None:
        """低置信度必须一个工具都不执行——用间谍统计「真实数据层取数」的次数。

        为什么不能只看 tool_results 为空：审批事件、计划事件都不代表执行。
        这里把 provider.snapshot 包起来计数，0 次才是真的没调。
        """
        from pq_agent.tools import quant_tools

        calls: list[object] = []
        original = quant_tools.make_market_snapshot_spec

        def spy(*args, **kwargs):
            spec = original(*args, **kwargs)
            handler = spec.handler

            def counted(arguments, *, dry_run=True):
                calls.append(dict(arguments))
                return handler(arguments, dry_run=dry_run)

            object.__setattr__(spec, "handler", counted)
            return spec

        with mock.patch.object(quant_tools, "make_market_snapshot_spec", spy):
            service = self._service(intent_model=LowConfidenceModel())
            session = service.create_session("local")
            run = service.start_run(session, query="随便看看那边的情况怎么样啊", thread_id="t-a6-01-spy")
            import asyncio

            async def drain() -> None:
                async for _ in service.stream_events(run):
                    pass

            asyncio.run(drain())
            trace = service.trace_payload(run.trace_id)
        self.assertEqual(calls, [], "低置信度时不允许执行任何工具")
        errors = " ".join(run.error_codes)
        self.assertIn("INTENT_LOW_CONFIDENCE", errors)
        self.assertIn("澄清", str(run.answer_preview))
        self.assertIn("INTENT_LOW_CONFIDENCE", json.dumps(trace, ensure_ascii=False))

    def test_low_confidence_stops_every_tool(self) -> None:
        service = self._service(intent_model=LowConfidenceModel())
        session = service.create_session("local")
        run = service.start_run(session, query="随便看看那边的情况怎么样啊", thread_id="t-a6-01-low")
        import asyncio

        async def drain() -> list:
            out = []
            async for item in service.stream_events(run):
                out.append(item)
            return out

        events = asyncio.run(drain())
        kinds = [name for name, _ in events]
        self.assertNotIn("approval_required", kinds, "低置信度不允许产生审批")
        state = service.graph_state(session, run.thread_id)["graph"]
        self.assertEqual(list(state.get("plan") or []), [], "低置信度不允许排任何计划")
        self.assertEqual(list(state.get("tool_results") or []), [], "低置信度不允许执行任何工具")
        errors = " ".join(state.get("errors") or [])
        self.assertIn("INTENT_LOW_CONFIDENCE", errors)
        self.assertIn("澄清", state.get("final_answer") or "")


class RunModeTests(unittest.TestCase):
    def tearDown(self) -> None:
        shutdown_tracing()

    def _settings(self, *, demo: bool = True, **kwargs) -> ServerSettings:
        runtime = ROOT / ".tmp" / "a6-01-tests" / ("mode-" + self._testMethodName)
        return ServerSettings(
            demo=demo, port=8199, token="a6-01-token-0123456789", runtime_dir=runtime, **kwargs
        )

    def test_demo_mode_allows_offline_provider(self) -> None:
        service = AgentService(self._settings())
        self.assertEqual(service.mode(), "demo_offline")
        self.assertIsNotNone(service.provider())

    def test_replay_mode_without_provider_fails_loudly(self) -> None:
        from pq_agent.contracts import ContractError

        settings = self._settings(demo=False, mode="replay")
        service = AgentService(settings)
        with self.assertRaises(ContractError) as ctx:
            service.provider()
        self.assertEqual(ctx.exception.code, "PROVIDER_REQUIRED")
        self.assertIn("replay", str(ctx.exception))

    def test_live_mode_without_provider_fails_loudly(self) -> None:
        from pq_agent.contracts import ContractError

        service = AgentService(self._settings(demo=False, mode="live"))
        with self.assertRaises(ContractError) as ctx:
            service.provider()
        self.assertEqual(ctx.exception.code, "PROVIDER_REQUIRED")

    def test_demo_offline_forbidden_without_demo_flag(self) -> None:
        from pq_agent.contracts import ContractError

        with self.assertRaises(ContractError) as ctx:
            self._settings(demo=False, mode="demo_offline")
        self.assertEqual(ctx.exception.code, "MODE_DEMO_OFFLINE_FORBIDDEN")

    def test_unknown_mode_rejected(self) -> None:
        from pq_agent.contracts import ContractError

        with self.assertRaises(ContractError) as ctx:
            self._settings(mode="whatever")
        self.assertEqual(ctx.exception.code, "RUN_MODE_INVALID")

    def test_no_demo_without_provider_exits_two(self) -> None:
        import io
        from contextlib import redirect_stdout

        from pq_agent.server import main

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["--no-demo", "--doctor", "--runtime-dir", str(ROOT / ".tmp" / "a6-01-tests" / "nodemo")])
        self.assertEqual(code, 2)
        self.assertIn("--provider", buffer.getvalue())
        self.assertIn("demo", buffer.getvalue())

    def test_settings_public_dict_has_mode(self) -> None:
        payload = self._settings().public_dict()
        self.assertEqual(payload["mode"], "demo_offline")
        self.assertNotIn("token", payload)


if __name__ == "__main__":
    unittest.main()
