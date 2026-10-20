"""A6-07 角色隔离必须进入 API 主图，越权不能留 call_log 痕迹。

三条不变量，每条都对应一个「修复前会失败」的场景：
1. 服务层交给图的工具只能是角色视图，拿不到能绕过守卫的全量注册表；
2. 低权限角色经 API 调写工具会被拒，且拒绝之后真注册表的 call_log 长度不变；
3. 角色不是请求参数：调用方不能靠 HTTP 请求把自己换成 operator。
"""

from __future__ import annotations

import pathlib
import sys
import unittest
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from pq_agent.contracts import ContractError, ToolCall  # noqa: E402
from pq_agent.graph import GraphDeps  # noqa: E402
from pq_agent.observability import shutdown_tracing  # noqa: E402
from pq_agent.roles import (  # noqa: E402
    ROLE_ANALYST,
    ROLE_CAPABILITY_DENIED_CODE,
    ROLE_OPERATOR,
    ROLE_SUPERVISOR,
    RoleError,
    RoleRegistry,
)
from pq_agent.roles.role_registry import role_registry  # noqa: E402
from pq_agent.server import (  # noqa: E402
    SESSION_COOKIE,
    TOKEN_HEADER,
    AgentService,
    ServerSettings,
    build_demo_service,
    create_app,
)

TOKEN = "a6-07-role-token-0123456789"


def _tmp_dir(prefix: str) -> pathlib.Path:
    """临时目录放在仓库 .tmp 下：本机沙箱会把 tempfile 的 0o700 目录变成自己进不去的目录。"""
    path = ROOT / ".tmp" / prefix / uuid.uuid4().hex[:8]
    path.mkdir(parents=True, exist_ok=True)
    return path


def _settings(**overrides) -> ServerSettings:
    preset = dict(host="127.0.0.1", port=8123, demo=True, token=TOKEN)
    preset.update(overrides)
    preset.setdefault("runtime_dir", _tmp_dir("a6-07-role"))
    return ServerSettings(**preset)


def _write_plan(state: dict) -> list[dict]:  # noqa: ARG001 - 计划固定，不看 state
    return [
        ToolCall(
            call_id="c-role-write",
            tool_name="report_write",
            arguments={"as_of": "2026-07-09"},
            capability="business_write",
            dry_run=True,
        ).to_dict()
    ]


def _login(client: TestClient) -> dict[str, str]:
    resp = client.get("/", headers={TOKEN_HEADER: TOKEN})
    assert resp.status_code == 200, resp.text[:200]
    session = resp.headers.get("set-cookie", "").split(";")[0].split("=", 1)[1]
    return {TOKEN_HEADER: TOKEN, "Cookie": SESSION_COOKIE + "=" + session}


class GraphGetsRoleViewTests(unittest.TestCase):
    """图拿到的 tools 必须是角色视图，而不是全量注册表。"""

    def tearDown(self) -> None:
        shutdown_tracing()

    def test_deps_carry_a_role_view_not_the_raw_registry(self) -> None:
        service = build_demo_service(_settings(role=ROLE_ANALYST))
        deps = service.deps()
        self.assertIsInstance(deps.tools, RoleRegistry)
        self.assertEqual(deps.tools.role, ROLE_ANALYST)
        self.assertIsNot(deps.tools, service.raw_registry())
        # 真注册表里有写工具，角色视图里没有 —— 这就是「包了一层」的直接证据。
        self.assertIn("report_write", service.raw_registry().names())
        self.assertNotIn("report_write", deps.tools.names())

    def test_before_fix_the_graph_received_the_raw_registry(self) -> None:
        """修复前的样子：service.tools() 直接把 build_default_registry 的结果交出去。

        这里显式重现那个形状，断言它**没有**角色视图的特性：
        names() 里能看到写工具、invoke 也不经过能力判定。
        它证明上一条用例的断言不是永远成立的。
        """
        service = build_demo_service(_settings(role=ROLE_ANALYST))
        raw = service.raw_registry()
        self.assertNotIsInstance(raw, RoleRegistry)
        self.assertIn("report_write", raw.names())
        self.assertFalse(hasattr(raw, "role"))

    def test_role_view_sees_only_its_own_capabilities(self) -> None:
        """写工具在真注册表里存在，但角色视图看不见 —— 这两件事要同时断言。"""
        service = build_demo_service(_settings(role=ROLE_OPERATOR))
        self.assertIn("report_write", service.raw_registry().names())
        self.assertIn("report_write", service.tools().names())
        self.assertNotIn("market_snapshot", service.tools().names())

    def test_supervisor_default_can_see_no_tool(self) -> None:
        service = build_demo_service(_settings())
        self.assertEqual(service.role_view(), ROLE_SUPERVISOR)
        self.assertEqual(service.tools().names(), [])
        self.assertEqual(service.tools().schemas(), [])

    def test_role_is_stable_per_service_even_if_argument_passed(self) -> None:
        """role 是装配参数，不是每个调用都能改的开关：deps() 反复取都还是同一个视图。"""
        service = build_demo_service(_settings(role=ROLE_OPERATOR))
        first = service.deps().tools
        second = service.deps().tools
        self.assertIs(first, second)
        self.assertEqual(first.role, ROLE_OPERATOR)

    def test_unknown_role_is_refused_at_assembly(self) -> None:
        with self.assertRaises(ContractError) as ctx:
            _settings(role="admin")
        self.assertEqual(ctx.exception.code, "SERVER_ROLE_UNKNOWN")
        with self.assertRaises(ContractError):
            AgentService(_settings(), role="root")

    def test_session_actor_is_not_a_role(self) -> None:
        """会话的 actor 是「谁在用」，不是「能用什么」：actor 叫 operator 也不给 operator 权限。"""
        service = build_demo_service(_settings())
        session = service.create_session("operator")
        self.assertEqual(session.actor, "operator")
        self.assertEqual(service.role_view(), ROLE_SUPERVISOR)
        self.assertEqual(service.tools().names(), [])

    def test_role_view_cannot_be_wrapped_again(self) -> None:
        """拿角色视图再包一个角色视图会叠两层判定，必须显式拒绝。"""
        service = build_demo_service(_settings(role=ROLE_ANALYST))
        with self.assertRaises(RoleError):
            role_registry(service.tools(), ROLE_OPERATOR)


class DeniedCallLeavesNoTraceTests(unittest.TestCase):
    """执行面：越权调用被拒，且真注册表的 call_log 一条都不许多。"""

    def tearDown(self) -> None:
        # 这一组用 build_demo_service 装配，它会装进程级 trace 状态；
        # 不还原会污染后面「未配置 tracing 时 current_tracing() 必须为 None」的用例。
        # 用 demo harness 时由 DemoHarness 自己管，这里不再额外绕一层。
        shutdown_tracing()

    def test_analyst_write_is_denied_without_touching_call_log(self) -> None:
        service = build_demo_service(_settings(role=ROLE_ANALYST))
        view = service.tools()
        raw = service.raw_registry()
        before = len(raw.call_log)
        with self.assertRaises(RoleError) as ctx:
            view.invoke("report_write", {"as_of": "2026-07-09"}, approval_token=None, dry_run=True)
        self.assertEqual(ctx.exception.code, ROLE_CAPABILITY_DENIED_CODE)
        self.assertEqual(len(raw.call_log), before, "被拒的调用不许在真注册表里留下记录")

    def test_before_fix_direct_registry_invoke_would_log_the_call(self) -> None:
        """修复前能拿到裸注册表，于是这次调用真的进了 call_log（哪怕后来失败了）。

        这里直接对 raw_registry 调同一个写工具，证明「有守卫」和「没守卫」在 call_log 上
        确实看得出差别：裸表那条一定会多出一行。
        """
        service = build_demo_service(_settings(role=ROLE_ANALYST))
        raw = service.raw_registry()
        before = len(raw.call_log)
        # 带正确令牌的调用会真的进入 call_log（这是「被允许」的记录）；
        # 角色视图会在到达这一步之前就拒绝，所以带令牌也进不来。
        token = raw.preview("report_write", {"as_of": "2026-07-09"})["arguments_hash"]
        raw.invoke("report_write", {"as_of": "2026-07-09"}, approval_token=token, dry_run=True)
        self.assertGreater(len(raw.call_log), before, "绕过守卫的调用会在 call_log 留下痕迹")
        guarded_before = len(raw.call_log)
        with self.assertRaises(RoleError):
            service.tools().invoke("report_write", {"as_of": "2026-07-09"}, approval_token=token, dry_run=True)
        self.assertEqual(len(raw.call_log), guarded_before, "守卫拦住的调用不许留下痕迹")

    def test_supervisor_cannot_invoke_anything(self) -> None:
        service = build_demo_service(_settings())
        raw = service.raw_registry()
        before = len(raw.call_log)
        for name, args in (("policy_rules_read", {}), ("report_write", {"as_of": "2026-07-09"})):
            with self.subTest(tool=name):
                with self.assertRaises(RoleError):
                    service.tools().invoke(name, args, dry_run=True)
        self.assertEqual(len(raw.call_log), before)


class ApiRoleIsolationTests(unittest.TestCase):
    """经 HTTP 主图验证：低权限角色调写工具被拒，且调用方改不了角色。"""

    def setUp(self) -> None:
        self.runtime = _tmp_dir("a6-07-role-api")
        self.settings = _settings(runtime_dir=self.runtime, role=ROLE_ANALYST)
        self.service = build_demo_service(self.settings, plan_builder=_write_plan)
        self.app = create_app(self.service, self.settings)
        self.client = TestClient(self.app, base_url="http://127.0.0.1")

    def tearDown(self) -> None:
        self.client.close()
        shutdown_tracing()

    def _run(self, thread_id: str, *, extra: dict | None = None) -> dict:
        head = _login(self.client)
        body = {"query": "生成今天的日报", "thread_id": thread_id, "as_of": "2026-07-09"}
        body.update(extra or {})
        resp = self.client.post("/api/chat", headers=head, json=body)
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        return {"text": resp.text, "head": head}

    def test_analyst_api_denial_leaves_no_call_log_entry(self) -> None:
        raw = self.service.raw_registry()
        before = len(raw.call_log)
        head = _login(self.client)
        self.client.post(
            "/api/chat",
            headers=head,
            json={"query": "生成今天的日报", "thread_id": "t-role-log", "as_of": "2026-07-09"},
        )
        self.assertEqual(len(raw.call_log), before, "被角色拒绝的调用不许留下 call_log 痕迹")

    def test_analyst_read_guard_denies_without_touching_call_log(self) -> None:
        """读类越权真的会被守卫拦：角色视图里没有的只读工具，在路由阶段就被分流。

        造一个正常登记的只读工具，让 supervisor（能力集为空）的计划去调它：
        守卫必须把它送去 role_denied，而不是执行它，更不能在 call_log 留下记录。
        这一条证明「可见面」与「执行面」是同一份判断，而不是两套各自演算。
        """
        from pq_agent.tools.contracts import ToolSpec

        calls: list[str] = []

        def handler(arguments, *, dry_run=True):  # noqa: ARG001
            calls.append("ran")
            return {"ok": True, "summary": "不该被调用"}

        # supervisor 的能力集是空的：任何只读工具它都看不见，正好用来验证「读类越权被拦」。
        sup_settings = _settings(runtime_dir=_tmp_dir("a6-07-role-guard"), role=ROLE_SUPERVISOR)
        sup = build_demo_service(sup_settings, plan_builder=lambda s: [
            ToolCall(call_id="c-guard", tool_name="zzz_extra_reader", arguments={},
                     capability="read_local", dry_run=True).to_dict()
        ])
        sup.raw_registry().register(
            ToolSpec(
                name="zzz_extra_reader",
                capability="read_local",
                side_effects=(),
                args=(),
                returns={"type": "object"},
                timeout_s=2.0,
                idempotency="safe",
                risk="low",
                summary="测试用只读工具",
                handler=handler,
            )
        )
        client = TestClient(create_app(sup, sup_settings), base_url="http://127.0.0.1")
        try:
            head = _login(client)
            client.post(
                "/api/chat",
                headers=head,
                json={"query": "看看持仓", "thread_id": "t-role-guard", "as_of": "2026-07-09"},
            )
            session = sup.sessions.get(head["Cookie"].split("=", 1)[1])
            graph = sup.graph_state(session, "t-role-guard")["graph"]
        finally:
            client.close()
        errors = " ".join(graph.get("errors") or [])
        self.assertIn(ROLE_CAPABILITY_DENIED_CODE, errors, errors)
        self.assertEqual(calls, [], "被角色守卫拒绝的只读工具一次都不许真的执行")
        self.assertEqual(len(sup.raw_registry().call_log), 0, "被拒的调用不许在 call_log 留痕")
        self.assertEqual(list(graph.get("tool_results") or []), [])

    def test_request_body_cannot_change_role(self) -> None:
        """请求里塞 role=operator 也不该改变服务角色：角色是装配参数，不是请求参数。"""
        self._run("t-role-body", extra={"role": ROLE_OPERATOR})
        self.assertEqual(self.service.role_view(), ROLE_ANALYST)
        self.assertEqual(self.service.tools().role, ROLE_ANALYST)

    def test_operator_service_still_needs_approval_for_write(self) -> None:
        """换到 operator 也不等于「能直接写」：写工具照样停在人工确认。"""
        shutdown_tracing()
        settings = _settings(runtime_dir=_tmp_dir("a6-07-role-op"), role=ROLE_OPERATOR)
        service = build_demo_service(settings, plan_builder=_write_plan)
        client = TestClient(create_app(service, settings), base_url="http://127.0.0.1")
        try:
            head = _login(client)
            text = client.post(
                "/api/chat",
                headers=head,
                json={"query": "生成今天的日报", "thread_id": "t-role-op", "as_of": "2026-07-09"},
            ).text
        finally:
            client.close()
        self.assertIn("approval_required", text, "operator 也必须先过人工确认")
        self.assertNotIn("ROLE_CAPABILITY_DENIED", text)


class SupervisorGuardTests(unittest.TestCase):
    """supervisor 装配下，只读工具会被角色守卫拦下 —— 这是守卫生效的直接证据。"""

    def tearDown(self) -> None:
        shutdown_tracing()

    def test_supervisor_cannot_execute_read_tool_and_leaves_no_execute_trace(self) -> None:
        from pq_agent.tools import quant_tools

        ran: list[str] = []
        original = quant_tools.make_market_snapshot_spec

        def spy(*args, **kwargs):
            spec = original(*args, **kwargs)
            handler = spec.handler

            def counted(arguments, *, dry_run=True):
                ran.append("market_snapshot")
                return handler(arguments, dry_run=dry_run)

            object.__setattr__(spec, "handler", counted)
            return spec

        runtime = _tmp_dir("a6-07-role-sup")
        settings = _settings(runtime_dir=runtime)  # 默认角色就是 supervisor
        from unittest import mock

        with mock.patch.object(quant_tools, "make_market_snapshot_spec", spy):
            service = build_demo_service(settings)
            client = TestClient(create_app(service, settings), base_url="http://127.0.0.1")
            try:
                head = _login(client)
                client.post(
                    "/api/chat",
                    headers=head,
                    json={"query": "今天大盘怎么样", "thread_id": "t-role-sup", "as_of": "2026-07-09"},
                )
                session = service.sessions.get(head["Cookie"].split("=", 1)[1])
                graph = service.graph_state(session, "t-role-sup")["graph"]
            finally:
                client.close()

        errors = " ".join(graph.get("errors") or [])
        self.assertIn(ROLE_CAPABILITY_DENIED_CODE, errors, errors)
        # 守卫在执行之前就分流了：工具 handler 一次都没进，tool_results 里也没有它。
        self.assertEqual(ran, [], "supervisor 视图里看不到的只读工具不许被执行")
        self.assertEqual(list(graph.get("tool_results") or []), [])
        self.assertEqual(len(service.raw_registry().call_log), 0, "被拒的调用不许留下 call_log 痕迹")
        self.assertNotIn("TOOL_FAILED", errors, "这是权限拒绝，不是执行失败")


class NoRawRegistryBypassTests(unittest.TestCase):
    """静态自检：服务层对外的工具入口只有一个，而且那个入口返回角色视图。"""

    def tearDown(self) -> None:
        # 这组会用 build_demo_service 装配服务，它会装进程级 trace 状态。
        # 不还原会污染后面「未配置 tracing 时 current_tracing() 必须为 None」的用例。
        shutdown_tracing()

    def test_only_tools_is_used_as_the_graph_dependency(self) -> None:
        source = (ROOT / "src" / "pq_agent" / "server.py").read_text(encoding="utf-8")
        # deps() 里必须是 tools=self.tools()，不能出现 tools=self.raw_registry()。
        self.assertIn("tools=self.tools()", source)
        self.assertNotIn("tools=self.raw_registry()", source)
        self.assertNotIn("tools=self._raw_tools", source)

    def test_raw_registry_has_no_public_alias(self) -> None:
        service = build_demo_service(_settings(role=ROLE_ANALYST))
        for alias in ("registry", "tool_registry", "get_registry"):
            self.assertFalse(hasattr(service, alias), "服务层不该再暴露 " + alias + " 这种裸入口")

    def test_graph_deps_type_is_role_view(self) -> None:
        service = build_demo_service(_settings(role=ROLE_ANALYST))
        deps = service.deps()
        self.assertIsInstance(deps, GraphDeps)
        self.assertIsInstance(deps.tools, RoleRegistry)


if __name__ == "__main__":
    unittest.main()
