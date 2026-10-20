"""A6-04：进程重启之后，HTTP 能把同一条线程继续批完。

为什么必须是「两个服务实例」而不是「同一个实例调两次」：
A1-07 留下的 SessionRegistry / RunStore 是纯内存对象，同一个实例里当然查得到；
只有真的重建 AgentService + 重建 FastAPI app + 重建 sqlite 连接，
才能证明「重启后线程归属、运行状态、待审批动作都还在」。
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

from pq_agent.contracts import ToolCall  # noqa: E402
from pq_agent.observability import shutdown_tracing  # noqa: E402
from pq_agent.server import (  # noqa: E402
    SESSION_COOKIE,
    TOKEN_HEADER,
    ServerSettings,
    build_demo_service,
    create_app,
)

TOKEN = "a6-04-restart-token-0123456789"


def _runtime_dir() -> pathlib.Path:
    path = ROOT / ".tmp" / "a6-04-restart" / uuid.uuid4().hex[:8]
    path.mkdir(parents=True, exist_ok=True)
    return path


def _plan_builder(state: dict) -> list[dict]:
    return [
        ToolCall(
            call_id="c-write-restart",
            tool_name="report_write",
            arguments={"as_of": "2026-07-09"},
            capability="business_write",
            dry_run=False,
        ).to_dict()
    ]


def parse_sse(text: str) -> dict[str, dict]:
    events: dict[str, dict] = {}
    for block in text.split(chr(10) + chr(10)):
        if not block.strip():
            continue
        name = ""
        data = ""
        for line in block.split(chr(10)):
            if line.startswith("event: "):
                name = line[len("event: "):].strip()
            elif line.startswith("data: "):
                data = line[len("data: "):]
        if name:
            try:
                import json

                events[name] = json.loads(data) if data else {}
            except ValueError:
                events[name] = {}
    return events


class RestartResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = _runtime_dir()
        self.thread_id = "t-restart-" + uuid.uuid4().hex[:6]
        self.clients: list[TestClient] = []

    def tearDown(self) -> None:
        for client in self.clients:
            client.close()
        shutdown_tracing()

    def _boot(self):
        """起一个全新的服务实例（新 AgentService、新 checkpoint 连接、新内存态）。"""
        # 这条用例走的是「写日报」这条链路：默认 supervisor 一个工具都调不动，
        # 要证明的是重启后写操作照样只执行一次，所以服务必须以 operator 身份装配。
        settings = ServerSettings(
            host="127.0.0.1", port=8123, demo=True, token=TOKEN, runtime_dir=self.runtime, role="operator"
        )
        service = build_demo_service(settings, plan_builder=_plan_builder)
        client = TestClient(create_app(service, settings), base_url="http://127.0.0.1")
        self.clients.append(client)
        return service, client

    def _login(self, client: TestClient) -> dict[str, str]:
        resp = client.get("/", headers={TOKEN_HEADER: TOKEN})
        self.assertEqual(resp.status_code, 200, resp.text[:200])
        session = resp.headers.get("set-cookie", "").split(";")[0].split("=", 1)[1]
        self.assertIn(SESSION_COOKIE + "=", resp.headers.get("set-cookie", ""))
        return {TOKEN_HEADER: TOKEN, "Cookie": SESSION_COOKIE + "=" + session}

    def test_restart_keeps_thread_owner_and_pending_approval(self) -> None:
        _, client = self._boot()
        head = self._login(client)
        text = client.post(
            "/api/chat",
            headers=head,
            json={"query": "生成今天的日报", "thread_id": self.thread_id, "as_of": "2026-07-09"},
        ).text
        pending = parse_sse(text).get("approval_required")
        self.assertIsNotNone(pending, "写操作必须先停下来等人工确认")

        # ---- 重启：旧实例的连接直接丢弃，全部按新进程重建 ----
        for old in self.clients:
            old.close()
        self.clients.clear()

        service2, client2 = self._boot()
        # 同一个 Cookie（同一个 session_id）在新实例里必须仍然有效
        head2 = dict(head)
        resp = client2.get("/api/state/" + self.thread_id, headers=head2)
        self.assertEqual(resp.status_code, 200, resp.text[:300])
        graph = resp.json().get("graph") or {}
        self.assertEqual(resp.json().get("run", {}).get("status"), "awaiting_approval")
        self.assertEqual(graph.get("next_nodes") or ["request_approval"], ["request_approval"])

        signed = dict(pending)
        decided = client2.post(
            "/api/approve",
            headers=head2,
            json={
                "thread_id": self.thread_id,
                "approval_id": signed["approval_id"],
                "action_hash": signed.get("action_hash"),
                "arguments_hash": signed.get("arguments_hash"),
                "approved": True,
                "reason": "重启后批准",
            },
        )
        self.assertEqual(decided.status_code, 200, decided.text[:300])
        self.assertTrue(decided.json()["ok"])
        target = self.runtime / "reports" / "2026-07-09" / "daily_pack_2026-07-09.md"
        self.assertTrue(target.exists(), "重启后批准必须真的执行写操作")
        self.assertEqual(service2.runs.get_thread(self.thread_id).status, "finished")

    def test_restart_keeps_ownership_isolation(self) -> None:
        """别人的线程在重启后依然不能碰：持久化不能把归属校验变成摆设。"""
        _, client = self._boot()
        head = self._login(client)
        client.post(
            "/api/chat",
            headers=head,
            json={"query": "生成今天的日报", "thread_id": self.thread_id, "as_of": "2026-07-09"},
        )
        for old in self.clients:
            old.close()
        self.clients.clear()
        _, client2 = self._boot()
        other = self._login(client2)
        resp = client2.get("/api/state/" + self.thread_id, headers=other)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json().get("error_code"), "SERVER_THREAD_FORBIDDEN")

    def test_unknown_thread_after_restart_is_404(self) -> None:
        _, client = self._boot()
        head = self._login(client)
        for old in self.clients:
            old.close()
        self.clients.clear()
        _, client2 = self._boot()
        resp = client2.get("/api/state/t-never-existed", headers=head)
        self.assertEqual(resp.status_code, 404)

    def test_corrupt_state_file_is_refused_not_silently_reset(self) -> None:
        """状态文件坏了必须显式报错：静默当成空等于把所有会话和审批现场扔掉。"""
        _, client = self._boot()
        head = self._login(client)
        client.post("/api/chat", headers=head, json={"query": "今天大盘怎么样", "thread_id": self.thread_id})
        for old in self.clients:
            old.close()
        self.clients.clear()
        (self.runtime / "sessions.json").write_text("{ this is not json", encoding="utf-8")
        with self.assertRaises(Exception) as ctx:
            self._boot()
        self.assertIn("SERVER_STATE_CORRUPT", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
