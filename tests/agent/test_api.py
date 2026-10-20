"""A1-07 本地 API 与演示面板的测试。

全部离线：demo_offline fixture + 不注入 sender，不联网、不碰用户真实 data/ 与 reports/。
临时运行时目录放在仓库 .tmp 下（本机沙箱会把 tempfile 的 0o700 目录变成自己进不去的目录）。
"""

import datetime as dt
import json
import pathlib
import socket
import sys
import unittest
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from pq_agent.contracts import ContractError, ToolCall  # noqa: E402
from pq_agent.data.provider import NetworkGuard, Profile  # noqa: E402
from pq_agent.observability import shutdown_tracing  # noqa: E402
from pq_agent.roles import ROLE_ANALYST  # noqa: E402
from pq_agent.roles.role_registry import role_registry  # noqa: E402
from pq_agent.server import (  # noqa: E402
    SESSION_COOKIE,
    TOKEN_HEADER,
    ApiError,
    ServerSettings,
    build_demo_service,
    check_port_free,
    create_app,
    format_sse,
)

TOKEN = "test-token-abcdef-0123456789"


def _tmp_dir(prefix: str) -> pathlib.Path:
    path = ROOT / ".tmp" / prefix / uuid.uuid4().hex[:8]
    path.mkdir(parents=True, exist_ok=True)
    return path


class ApiTestCase(unittest.TestCase):
    """统一装配：每个用例一个独立运行时目录，互不干扰。"""

    def setUp(self) -> None:
        self.runtime_dir = _tmp_dir("a1-07-tests")
        self.settings = ServerSettings(
            host="127.0.0.1",
            port=8123,
            demo=True,
            token=TOKEN,
            runtime_dir=self.runtime_dir,
        )
        self.service = build_demo_service(self.settings)
        self.app = create_app(self.service, self.settings)
        self.client = TestClient(self.app, base_url="http://127.0.0.1")

    def tearDown(self) -> None:
        self.client.close()
        # build_demo_service 会装进程级 trace 状态（真实服务必须这么做），测试之间要还原，
        # 否则会污染后面的用例（例如 A1-06 断言未配置 tracing 时 current_tracing() 为 None）。
        shutdown_tracing()

    def headers(self, *, session: str | None = None, token: str | None = TOKEN) -> dict[str, str]:
        head: dict[str, str] = {}
        if token is not None:
            head[TOKEN_HEADER] = token
        if session:
            head["Cookie"] = SESSION_COOKIE + "=" + session
        return head

    def open_session(self) -> str:
        resp = self.client.get("/", headers=self.headers())
        self.assertEqual(resp.status_code, 200, resp.text[:200])
        raw = resp.headers.get("set-cookie", "")
        self.assertIn(SESSION_COOKIE + "=", raw)
        return raw.split(";")[0].split("=", 1)[1]

    def other_session_client(self):
        """另开一个不带 cookie 的客户端并登录，用来验证跨会话隔离。

        TestClient 自带 cookie jar，同一个 client 连续两次 open_session() 会一直复用
        第一个会话的 cookie，等于自己访问自己，测不出越权。
        """
        client = TestClient(self.app, base_url="http://127.0.0.1")
        resp = client.get("/", headers=self.headers())
        self.assertEqual(resp.status_code, 200, resp.text[:200])
        raw = resp.headers.get("set-cookie", "")
        session = raw.split(";")[0].split("=", 1)[1]
        return client, session
    def chat(
        self,
        *,
        session: str,
        query: str = "把晨间推送预览准备一下",
        thread_id: str | None = None,
        as_of: str = "2026-07-09",
        slots: dict | None = None,
    ) -> str:
        body = {"query": query, "as_of": as_of}
        # 默认查询是 push_request，计划层会去要推送时段。
        # 这些用例验证的是审批与线程归属，必须真的走到人工确认那一步，
        # 所以显式给一个合法时段（等价于用户在界面上选好时段）。
        body["slots"] = dict(slots) if slots is not None else {"slot": "morning_build"}
        if thread_id:
            body["thread_id"] = thread_id
        resp = self.client.post("/api/chat", headers=self.headers(session=session), json=body)
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        return resp.text


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for block in text.split(chr(10) + chr(10)):
        if not block.strip():
            continue
        name = "message"
        data = ""
        for line in block.split(chr(10)):
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
        try:
            payload = json.loads(data) if data else {}
        except Exception:
            payload = {"raw": data}
        events.append((name, payload))
    return events


class AuthGuardTests(ApiTestCase):
    def test_index_requires_token(self) -> None:
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error_code"], "SERVER_TOKEN_MISSING")

    def test_wrong_token_is_rejected(self) -> None:
        resp = self.client.get("/", headers=self.headers(token="wrong-token"))
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.json()["error_code"], "SERVER_TOKEN_INVALID")

    def test_index_serves_demo_banner(self) -> None:
        resp = self.client.get("/", headers=self.headers())
        self.assertEqual(resp.status_code, 200)
        self.assertIn("演示数据", resp.text)
        self.assertNotIn("__BANNER_TEXT__", resp.text)
        self.assertNotIn("__APP_JS__", resp.text)

    def test_session_cookie_is_httponly_and_strict(self) -> None:
        resp = self.client.get("/", headers=self.headers())
        raw = resp.headers.get("set-cookie", "").lower()
        self.assertIn("httponly", raw)
        self.assertIn("samesite=strict", raw)

    def test_host_header_must_be_local(self) -> None:
        head = self.headers()
        head["host"] = "evil.example.com"
        resp = self.client.get("/", headers=head)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error_code"], "SERVER_HOST_DENIED")

    def test_cross_site_origin_is_rejected(self) -> None:
        head = self.headers()
        head["origin"] = "http://evil.example.com"
        resp = self.client.get("/", headers=head)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error_code"], "SERVER_ORIGIN_DENIED")

    def test_post_without_custom_header_is_rejected(self) -> None:
        session = self.open_session()
        head = {"Cookie": SESSION_COOKIE + "=" + session}
        resp = self.client.post("/api/chat", headers=head, json={"query": "hi"})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error_code"], "SERVER_CSRF_HEADER_REQUIRED")

    def test_body_limit_is_enforced(self) -> None:
        session = self.open_session()
        resp = self.client.post(
            "/api/chat",
            headers=self.headers(session=session),
            json={"query": "hi", "pad": "x" * 40000},
        )
        self.assertEqual(resp.status_code, 413)
        self.assertEqual(resp.json()["error_code"], "SERVER_BODY_TOO_LARGE")

    def test_token_never_touches_disk(self) -> None:
        session = self.open_session()
        self.chat(session=session)
        hits = 0
        for path in sorted(self.runtime_dir.rglob("*")):
            if path.is_file():
                if TOKEN in path.read_text(encoding="utf-8", errors="ignore"):
                    hits += 1
        self.assertEqual(hits, 0)

    def test_token_comparison_is_constant_time(self) -> None:
        source = (ROOT / "src" / "pq_agent" / "server.py").read_text(encoding="utf-8")
        self.assertIn("hmac.compare_digest", source)


class SessionOwnershipTests(ApiTestCase):
    def test_other_session_cannot_read_thread(self) -> None:
        owner = self.open_session()
        text = self.chat(session=owner, thread_id="t-owner-1")
        self.assertIn("approval_required", text)
        other_client, other = self.other_session_client()
        try:
            resp = other_client.get("/api/state/t-owner-1", headers=self.headers(session=other))
        finally:
            other_client.close()
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["error_code"], "SERVER_THREAD_FORBIDDEN")

    def test_unknown_thread_is_404(self) -> None:
        session = self.open_session()
        resp = self.client.get("/api/state/t-nope", headers=self.headers(session=session))
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error_code"], "SERVER_THREAD_NOT_FOUND")

    def test_owner_can_read_thread_state(self) -> None:
        session = self.open_session()
        self.chat(session=session, thread_id="t-owner-2")
        resp = self.client.get("/api/state/t-owner-2", headers=self.headers(session=session))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["graph"]["awaiting_approval"])
        self.assertEqual(body["graph"]["next_nodes"], ["request_approval"])


class QueryValidationTests(ApiTestCase):
    def test_empty_query_is_rejected(self) -> None:
        session = self.open_session()
        resp = self.client.post("/api/chat", headers=self.headers(session=session), json={"query": "   "})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error_code"], "SERVER_QUERY_INVALID")

    def test_too_long_query_is_rejected(self) -> None:
        session = self.open_session()
        resp = self.client.post("/api/chat", headers=self.headers(session=session), json={"query": "x" * 600})
        self.assertEqual(resp.status_code, 413)
        self.assertEqual(resp.json()["error_code"], "SERVER_QUERY_TOO_LONG")

    def test_bad_as_of_is_rejected(self) -> None:
        session = self.open_session()
        resp = self.client.post(
            "/api/chat",
            headers=self.headers(session=session),
            json={"query": "把晨间推送预览准备一下", "as_of": "2026/07/09"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error_code"], "SERVER_QUERY_INVALID")

    def test_path_traversal_thread_id_is_rejected(self) -> None:
        session = self.open_session()
        resp = self.client.post(
            "/api/chat",
            headers=self.headers(session=session),
            json={"query": "把晨间推送预览准备一下", "thread_id": ".." + chr(92) + "escape"},
        )
        self.assertEqual(resp.status_code, 400)

class SSETests(ApiTestCase):
    def test_stream_carries_full_flow(self) -> None:
        session = self.open_session()
        events = parse_sse(self.chat(session=session, thread_id="t-sse-1"))
        kinds = [name for name, _ in events]
        self.assertEqual(kinds[0], "meta")
        self.assertIn("intent", kinds)
        self.assertIn("node", kinds)
        self.assertIn("approval_required", kinds)
        self.assertEqual(kinds[-1], "done")
        self.assertIn("state", kinds)
        self.assertLess(kinds.index("intent"), kinds.index("approval_required"))

    def test_meta_event_describes_demo_mode(self) -> None:
        session = self.open_session()
        events = dict(parse_sse(self.chat(session=session, thread_id="t-sse-2")))
        meta = events["meta"]
        self.assertTrue(meta["demo"])
        self.assertEqual(meta["mode"], "demo_offline")
        self.assertEqual(meta["as_of"], "2026-07-09")
        self.assertTrue(meta["banner"])
        self.assertTrue(str(meta["trace_id"]).startswith("tr"))

    def test_approval_event_carries_binding_fields(self) -> None:
        session = self.open_session()
        events = dict(parse_sse(self.chat(session=session, thread_id="t-sse-3")))
        payload = events["approval_required"]
        for field in ("approval_id", "thread_id", "call_id", "capability", "action_hash", "arguments_hash", "risk_note", "expires_at"):
            self.assertTrue(payload.get(field), field)
        self.assertEqual(payload["thread_id"], "t-sse-3")
        self.assertEqual(payload["capability"], "control_write")
        self.assertEqual(payload["dry_run"], True)  # 默认计划里的调用就是 dry-run

    def test_stream_does_not_leak_query_or_token(self) -> None:
        session = self.open_session()
        secret_query = "把晨间推送预览准备一下 secret-marker-9527"
        text = self.chat(session=session, query=secret_query, thread_id="t-sse-4")
        self.assertNotIn("secret-marker-9527", text)
        self.assertNotIn(TOKEN, text)

    def test_disconnect_marks_run_aborted(self) -> None:
        import asyncio

        session = self.service.create_session("local")
        run = self.service.start_run(session, query="把晨间推送预览准备一下", thread_id="t-abort-1")

        async def gone() -> bool:
            return True

        async def collect() -> list[tuple[str, dict]]:
            out: list[tuple[str, dict]] = []
            async for item in self.service.stream_events(run, is_disconnected=gone):
                out.append(item)
                if len(out) > 60:
                    break
            return out

        events = asyncio.run(collect())
        kinds = [name for name, _ in events]
        self.assertIn("aborted", kinds)
        self.assertTrue(run.client_disconnected)
        self.assertEqual(run.status, "aborted")

    def test_sse_frame_format(self) -> None:
        frame = format_sse("meta", {"a": 1})
        self.assertTrue(frame.startswith("event: meta" + chr(10) + "data: "))
        self.assertTrue(frame.endswith(chr(10) + chr(10)))


class ApprovalFlowTests(ApiTestCase):
    def _pending(self, session: str, thread_id: str) -> dict:
        events = dict(parse_sse(self.chat(session=session, thread_id=thread_id)))
        return events["approval_required"]

    def test_approve_requires_boolean(self) -> None:
        session = self.open_session()
        pending = self._pending(session, "t-app-1")
        resp = self.client.post(
            "/api/approve",
            headers=self.headers(session=session),
            json={"thread_id": "t-app-1", "approval_id": pending["approval_id"]},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error_code"], "SERVER_QUERY_INVALID")

    def test_approve_with_wrong_id_is_stale(self) -> None:
        session = self.open_session()
        self._pending(session, "t-app-2")
        resp = self.client.post(
            "/api/approve",
            headers=self.headers(session=session),
            json={"thread_id": "t-app-2", "approval_id": "a-deadbeef", "approved": True},
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error_code"], "SERVER_APPROVAL_STALE")

    def test_approve_with_mismatched_action_hash_is_stale(self) -> None:
        session = self.open_session()
        pending = self._pending(session, "t-app-3")
        resp = self.client.post(
            "/api/approve",
            headers=self.headers(session=session),
            json={
                "thread_id": "t-app-3",
                "approval_id": pending["approval_id"],
                "action_hash": "tampered-hash",
                "approved": True,
            },
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["error_code"], "SERVER_APPROVAL_STALE")

    def test_approve_without_pending_is_rejected(self) -> None:
        session = self.open_session()
        self.chat(session=session, thread_id="t-app-4")
        resp = self.client.post(
            "/api/approve",
            headers=self.headers(session=session),
            json={"thread_id": "t-app-4", "approval_id": "a-anything", "approved": True},
        )
        self.assertEqual(resp.status_code, 409)
        self.assertIn(resp.json()["error_code"], ("SERVER_APPROVAL_NOT_PENDING", "SERVER_APPROVAL_STALE"))

    def test_approve_unknown_thread_is_404(self) -> None:
        session = self.open_session()
        resp = self.client.post(
            "/api/approve",
            headers=self.headers(session=session),
            json={"thread_id": "t-missing", "approval_id": "a-1", "approved": True},
        )
        self.assertEqual(resp.status_code, 404)

    def test_write_tool_only_runs_after_approval(self) -> None:
        def plan_builder(state: dict) -> list[dict]:
            return [
                ToolCall(
                    call_id="c-write-1",
                    tool_name="report_write",
                    arguments={"as_of": "2026-07-09"},
                    capability="business_write",
                    dry_run=False,
                ).to_dict()
            ]

        runtime = _tmp_dir("a1-07-write")
        # 写日报要求服务以 operator 身份装配：默认 supervisor 看不到任何工具，
        # 那样这条用例会停在 TOOL_UNKNOWN 上，测不出「批准后才写」这件事。
        settings = ServerSettings(host="127.0.0.1", port=8123, demo=True, token=TOKEN, runtime_dir=runtime, role="operator")
        service = build_demo_service(settings, plan_builder=plan_builder)
        client = TestClient(create_app(service, settings), base_url="http://127.0.0.1")
        try:
            resp = client.get("/", headers={TOKEN_HEADER: TOKEN})
            session = resp.headers.get("set-cookie", "").split(";")[0].split("=", 1)[1]
            head = {TOKEN_HEADER: TOKEN, "Cookie": SESSION_COOKIE + "=" + session}
            text = client.post(
                "/api/chat",
                headers=head,
                json={"query": "生成今天的日报", "thread_id": "t-write-1", "as_of": "2026-07-09"},
            ).text
            pending = dict(parse_sse(text))["approval_required"]
            target = runtime / "reports" / "2026-07-09" / "daily_pack_2026-07-09.md"
            self.assertFalse(target.exists(), "审批前不得产生业务文件")
            decided = client.post(
                "/api/approve",
                headers=head,
                json={
                    "thread_id": "t-write-1",
                    "approval_id": pending["approval_id"],
                    "action_hash": pending["action_hash"],
                    "arguments_hash": pending["arguments_hash"],
                    "approved": True,
                    "reason": "测试批准",
                },
            )
            self.assertEqual(decided.status_code, 200, decided.text[:300])
            self.assertTrue(decided.json()["ok"])
            self.assertTrue(target.exists(), "批准后必须真的写出业务文件")
            self.assertIn("每日", target.read_text(encoding="utf-8"))
        finally:
            client.close()

    def test_reject_stops_before_write(self) -> None:
        def plan_builder(state: dict) -> list[dict]:
            return [
                ToolCall(
                    call_id="c-write-2",
                    tool_name="report_write",
                    arguments={"as_of": "2026-07-09"},
                    capability="business_write",
                    dry_run=False,
                ).to_dict()
            ]

        runtime = _tmp_dir("a1-07-reject")
        # 同上：拒绝路径也必须真的走到「已经批过、只差没批」的位置，才有拒绝可言。
        settings = ServerSettings(host="127.0.0.1", port=8123, demo=True, token=TOKEN, runtime_dir=runtime, role="operator")
        service = build_demo_service(settings, plan_builder=plan_builder)
        client = TestClient(create_app(service, settings), base_url="http://127.0.0.1")
        try:
            resp = client.get("/", headers={TOKEN_HEADER: TOKEN})
            session = resp.headers.get("set-cookie", "").split(";")[0].split("=", 1)[1]
            head = {TOKEN_HEADER: TOKEN, "Cookie": SESSION_COOKIE + "=" + session}
            text = client.post(
                "/api/chat",
                headers=head,
                json={"query": "生成今天的日报", "thread_id": "t-write-2", "as_of": "2026-07-09"},
            ).text
            pending = dict(parse_sse(text))["approval_required"]
            decided = client.post(
                "/api/approve",
                headers=head,
                json={
                    "thread_id": "t-write-2",
                    "approval_id": pending["approval_id"],
                    "action_hash": pending["action_hash"],
                    "arguments_hash": pending["arguments_hash"],
                    "approved": False,
                    "reason": "测试拒绝",
                },
            )
            self.assertEqual(decided.status_code, 200, decided.text[:300])
            self.assertFalse((runtime / "reports").exists(), "拒绝后不得写出业务文件")
        finally:
            client.close()


class TraceAndCostTests(ApiTestCase):
    def test_trace_returns_summary_only(self) -> None:
        session = self.open_session()
        events = dict(parse_sse(self.chat(session=session, thread_id="t-trace-1")))
        trace_id = events["meta"]["trace_id"]
        resp = self.client.get("/api/trace/" + trace_id, headers=self.headers(session=session))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()["trace"]
        self.assertEqual(body["trace_id"], trace_id)
        self.assertGreater(body["span_count"], 0)
        self.assertIn("node_sequence", body)
        self.assertIn("仅摘要", body["note"])
        self.assertNotIn("prompt", json.dumps(body, ensure_ascii=False))
        self.assertNotIn(TOKEN, json.dumps(body, ensure_ascii=False))

    def test_unknown_trace_is_404(self) -> None:
        session = self.open_session()
        resp = self.client.get("/api/trace/tr-nope", headers=self.headers(session=session))
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["error_code"], "SERVER_TRACE_NOT_FOUND")

    def test_other_session_cannot_read_trace(self) -> None:
        owner = self.open_session()
        events = dict(parse_sse(self.chat(session=owner, thread_id="t-trace-2")))
        other_client, other = self.other_session_client()
        try:
            resp = other_client.get(
                "/api/trace/" + events["meta"]["trace_id"],
                headers=self.headers(session=other),
            )
        finally:
            other_client.close()
        self.assertEqual(resp.status_code, 403)

    def test_cost_is_unknown_not_estimated(self) -> None:
        session = self.open_session()
        self.chat(session=session, thread_id="t-cost-1")
        resp = self.client.get("/api/cost", headers=self.headers(session=session))
        self.assertEqual(resp.status_code, 200)
        cost = resp.json()["cost"]
        self.assertEqual(cost["tokens"], "unknown")
        self.assertEqual(cost["cost"], "unknown")
        self.assertGreaterEqual(cost["requests"], 1)


class DemoIsolationTests(ApiTestCase):
    def test_runtime_dir_is_under_repo_tmp(self) -> None:
        self.assertTrue(str(self.settings.runtime_dir).startswith(str(ROOT / ".tmp")))

    def test_provider_is_demo_offline(self) -> None:
        self.assertEqual(self.service.provider().profile, Profile.DEMO_OFFLINE)

    def test_no_external_sender_tool_without_sender(self) -> None:
        # service.tools() 现在是角色视图（默认 supervisor 一个工具都没有），
        # 看全量工具要显式走 raw_registry()，否则这条断言会因为「看不见」而假绿。
        names = list(self.service.raw_registry().names())
        self.assertNotIn("push_send", names)
        self.assertIn("push_preview", names)

    def test_full_run_makes_zero_network_attempts(self) -> None:
        """demo_offline 的数据层与只读工具零外呼。

        注意：NetworkGuard 替换的是 socket.getaddrinfo / socket.connect，而 Windows 上
        asyncio 建事件循环要用 socketpair 造自管道，所以不能把 TestClient 的 HTTP 调用
        一起包进来——那会把 127.0.0.1 的自管道误判成外呼，
        实测报 OUTBOUND_NETWORK_DENIED: demo_offline 禁止网络连接 (127.0.0.1, 4927)。
        这里只罩住同步的数据层与只读工具调用。
        """
        # 用 analyst 视图调用只读工具：它就是这个角色应该看到的那部分，
        # 顺便证明角色视图本身不会偷偷外呼。
        tools_registry = role_registry(self.service.raw_registry(), ROLE_ANALYST)
        with NetworkGuard() as guard:
            snapshot = self.service.provider().snapshot(dt.date(2026, 7, 9))
            self.assertTrue(str(snapshot.provenance.source).startswith("fixture:"))
            for name in ("policy_rules_read", "positions_snapshot"):
                tools_registry.invoke(name, {}, dry_run=True)
        self.assertEqual(guard.attempt_count, 0)

    def test_run_does_not_write_user_reports_dir(self) -> None:
        session = self.open_session()
        self.chat(session=session, thread_id="t-iso-1")
        self.assertFalse((ROOT / "reports").exists())
        self.assertFalse((ROOT / "logs").exists())

    def test_demo_service_records_trace_index(self) -> None:
        session = self.open_session()
        self.chat(session=session, thread_id="t-iso-2")
        self.assertTrue((self.runtime_dir / "traces.jsonl").exists())


class SettingsAndDoctorTests(ApiTestCase):
    def test_non_local_host_is_refused(self) -> None:
        with self.assertRaises(ContractError) as ctx:
            ServerSettings(host="0.0.0.0", port=8123, demo=True, token=TOKEN)
        self.assertEqual(ctx.exception.code, "SERVER_HOST_NOT_LOCAL")

    def test_token_is_generated_when_missing(self) -> None:
        settings = ServerSettings(host="127.0.0.1", port=8123, demo=True, token="")
        self.assertGreater(len(settings.token), 20)
        # 允许用查询串带 token 是为了让控制台打印的 ?token= 链接能直接打开页面；
        # 写操作仍然强制要求 X-PQ-Token 自定义头，CSRF 防线不受影响。
        self.assertTrue(settings.allow_query_token)

    def test_port_conflict_is_detected(self) -> None:
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            # 用系统分配的临时端口：本沙箱禁止绑定固定端口 8199（WinError 10013）。
            holder.bind(("127.0.0.1", 0))
            holder.listen(1)
            port = holder.getsockname()[1]
            free, _detail = check_port_free("127.0.0.1", port)
            self.assertFalse(free)
        finally:
            holder.close()
        free, _detail = check_port_free("127.0.0.1", port)
        self.assertTrue(free)

    def test_doctor_reports_four_checks(self) -> None:
        report = self.service.doctor()
        names = [item["name"] for item in report["checks"]]
        # role_isolation 是 A6-07 新增的体检项：确认服务手上的工具是角色视图而不是全量注册表。
        self.assertEqual(names, ["port", "checkpoint_driver", "demo_fixture", "tracing", "role_isolation"])
        isolation = next(item for item in report["checks"] if item["name"] == "role_isolation")
        self.assertTrue(isolation["ok"], isolation)
        self.assertTrue(all("detail" in item for item in report["checks"]))

    def test_guard_out_redacts_webhook(self) -> None:
        payload = {"webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/abc12345-6789"}
        safe = self.service._guard_out(payload, where="test")
        self.assertNotIn("open.feishu.cn", json.dumps(safe, ensure_ascii=False))

    def test_guard_out_failure_is_an_api_error(self) -> None:
        class Boom:
            def __repr__(self) -> str:
                return "boom"

        with self.assertRaises(ApiError) as ctx:
            self.service._guard_out({"ok": True, "weird": Boom()}, where="test")
        self.assertEqual(ctx.exception.status, 500)


if __name__ == "__main__":
    unittest.main()