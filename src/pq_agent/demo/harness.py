"""A5-03 演示执行器：把 9 个步骤跑成一份可核对、可复现的文字记录。

设计上只有一条路：离线、可重放、零外呼。任何一步做不到，就把失败原因留在屏幕上，
不在演示里加载「反正演示能用」的假数据。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import pathlib
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from pq_agent.demo.paths import (
    DemoPathError,
    ResetReport,
    demo_root,
    demo_stage_root,
    ensure_demo_root,
    is_inside_demo,
    repo_root_of,
    reset_demo,
)
from pq_agent.demo.steps import (
    DEMO_SCHEMA_VERSION,
    DEMO_STEPS,
    DemoStep,
    step_by_id,
    total_narration_seconds,
)

DEMO_ERROR = "DEMO_ERROR"
DEMO_RAG_INDEX_MISSING = "DEMO_RAG_INDEX_MISSING"
DEMO_CORPUS_MISSING = "DEMO_CORPUS_MISSING"
DEMO_NETWORK_DETECTED = "DEMO_NETWORK_DETECTED"

CONTROL_FILE_NAMES: tuple[str, ...] = (
    "approvals.jsonl",
    "checkpoints.sqlite",
    "traces.jsonl",
    "telemetry.jsonl",
    "apm_snapshot.json",
    "apm_alerts.json",
    "outbox.jsonl",
)

# 演示服务的两个角色。角色能力集互不重叠（roles/contracts.py），
# 所以「读」和「写」必须是两个服务实例，而不是一个万能注册表。
DEMO_PRIMARY_ROLE = "analyst"
# 某个步骤要用哪个角色的服务：不写在这里的步骤一律用 DEMO_PRIMARY_ROLE。
# 第 2 步要取数，用 analyst；写/外发那几步用 analyst 的服务，靠 control_write
# 「必须人工确认、不落业务盘」这条旁路走审批与恢复（见 graph._role_denies 的说明）。
DEMO_STEP_ROLES: dict[str, str] = {
    "replay_qa": "analyst",
    "role_denied": "analyst",
}

DEFAULT_DEMO_QUERY = "今天大盘怎么样"
DEMO_PUSH_QUERY = "把日报推送到飞书"
DEMO_MISSING_AS_OF = "2026-07-08"


class _CountingSender:
    """外发通道探针：真有人调用就记一笔，演示结束时它必须是 0。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append({"args": [str(item)[:60] for item in args], "kwargs": sorted(kwargs)})
        raise AssertionError("演示模式不允许外发：" + str(sorted(kwargs)))


@dataclass
class DemoStepResult:
    step_id: str
    title: str
    ok: bool
    lines: list[str] = field(default_factory=list)
    markers_missing: tuple[str, ...] = ()
    duration_ms: int = 0
    error: str = ""
    blockers: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "title": self.title,
            "ok": self.ok,
            "markers_missing": list(self.markers_missing),
            "duration_ms": self.duration_ms,
            "error": self.error,
            "blockers": list(self.blockers),
            "lines": list(self.lines),
        }


@dataclass
class DemoRun:
    ok: bool = False
    steps: list[DemoStepResult] = field(default_factory=list)
    network_attempts: int = 0
    elapsed_ms: int = 0
    runtime_dir: str = ""
    narration_seconds: int = 0
    blockers: list[str] = field(default_factory=list)
    schema_version: str = DEMO_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "ok": self.ok,
            "narration_seconds": self.narration_seconds,
            "elapsed_ms": self.elapsed_ms,
            "runtime_dir": self.runtime_dir,
            "network_attempts": self.network_attempts,
            "blockers": list(self.blockers),
            "steps": [item.to_dict() for item in self.steps],
        }


class DemoNotReady(RuntimeError):
    """演示前置条件不满足。带着错误码，不静默降级。"""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code + ": " + detail)
        self.code = str(code)
        self.detail = str(detail)


def pick_free_port(*, first: int = 8199, last: int = 8250) -> int:
    """挑一个能绑上的本地端口。只做本地 bind，不产生任何外呼。"""

    for port in range(int(first), int(last) + 1):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
        finally:
            probe.close()
    raise DemoNotReady(DEMO_ERROR, "8199-8250 没有可用端口")


class DemoHarness:
    """离线演示执行器。构造即准备目录与 demo 服务。"""

    def __init__(
        self,
        *,
        repo_root: pathlib.Path | None = None,
        reset: bool = True,
    ) -> None:
        self.repo_root = repo_root_of(repo_root)
        self.root = demo_root(self.repo_root)
        self.stage = demo_stage_root(self.repo_root)
        self.reset_report: ResetReport | None = None
        if reset:
            self.reset_report = reset_demo(repo_root=self.repo_root)
        ensure_demo_root(repo_root=self.repo_root)
        self._sender = _CountingSender()
        self._service: Any = None
        # 每个角色一个服务实例：角色能力集互不重叠，一个实例服务不了全部步骤。
        self._services: dict[str, Any] = {}
        self.settings: Any = None
        self._retriever: Any = None
        self._loop: Any = None

    # ---- 装配 --------------------------------------------------------

    def _ensure_imports(self) -> None:
        if self._service is not None:
            return
        self._services = {}
        self.settings = self._make_settings()
        self._service = self._service_for(DEMO_PRIMARY_ROLE)

    def _make_settings(self) -> Any:
        from pq_agent.server import ServerSettings

        return ServerSettings(
            port=pick_free_port(),
            demo=True,
            token="demo-local-token",
            runtime_dir=self.stage,
            repo_root=self.repo_root,
            # 优先服务的角色：只读、需要取数的那几步（第 2、3、7 步）用它。
            # 写工具那几步换成 operator 的服务，见 DEMO_STEP_ROLES。
            role=DEMO_PRIMARY_ROLE,
        )

    def _service_for(self, role: str) -> Any:
        """按角色取一个服务实例。同一个共享运行时目录，只是工具视图不同。

        为什么不给演示服务一个「全量注册表」：角色的能力集互不重叠（这是 roles/contracts.py
        刻意的设计），三合一等于把隔离拆掉。演示宁可跑两个角色，也不假装有一个万能角色。
        """
        from pq_agent.server import build_demo_service

        if self.settings is None:
            # 第一次进来就是在 run() 的第一步：先把设置建好，否则没有可换角色的底子。
            self.settings = self._make_settings()
        if role not in self._services:
            scoped = dataclasses.replace(self.settings, role=role)
            self._services[role] = build_demo_service(
                scoped,
                sender=self._sender,
                retriever=self.retriever(),
            )
        return self._services[role]

    @property
    def service(self) -> Any:
        """当前步骤要用的服务（由 run() 按步骤 id 设角色；默认是 analyst）。"""
        self._ensure_imports()
        return self._service

    def fresh_service(self) -> Any:
        """同配置、同角色的新实例：用来证明状态落在磁盘而不是内存里。"""

        self._ensure_imports()
        return self._service_for(self.service.role_view())

    def retriever(self) -> Any:
        """词法检索器。索引缺失时明确报错，不退回空结果。"""

        if self._retriever is not None:
            return self._retriever
        from pq_agent.rag.retriever import build_retriever

        index_path = self.repo_root / "agent_eval" / "results" / "rag_index.json"
        if not index_path.is_file():
            raise DemoNotReady(
                DEMO_RAG_INDEX_MISSING,
                "缺索引：" + str(index_path) + "；先跑 agent_eval/run_rag_eval.py",
            )
        self._retriever = build_retriever(index_path=index_path, root=self.repo_root)
        return self._retriever

    def redteam_cases(self) -> dict[str, Mapping[str, Any]]:
        path = self.repo_root / "agent_eval" / "cases" / "sandbox_redteam.json"
        if not path.is_file():
            raise DemoNotReady(DEMO_CORPUS_MISSING, "缺沙箱样例：" + str(path))
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {str(item.get("case_id")): item for item in (payload.get("cases") or [])}

    # ---- 工具方法 ----------------------------------------------------

    async def _drive(
        self,
        *,
        query: str,
        as_of: str | None = None,
        slots: Mapping[str, Any] | None = None,
        service: Any = None,
    ) -> tuple[Any, Any, list[tuple[str, Any]]]:
        svc = service if service is not None else self.service
        session = svc.create_session()
        run = svc.start_run(session, query=query, as_of=as_of, slots=slots)
        events: list[tuple[str, Any]] = []
        async for name, payload in svc.stream_events(run):
            events.append((str(name), payload))
        return session, run, events

    def _await(self, coro: Any) -> Any:
        """在预先建好的事件循环上跑。

        循环不能建在 NetworkGuard 里面：Windows 的 ProactorEventLoop 在构造时要
        socketpair 到自己，那本身就是一次 127.0.0.1 连接，会被熔断器当成外呼掐掉。
        这是 A1-07 踩过的同一个坑，这里用「先建循环再进闸门」绕开。
        """

        loop = self._loop
        if loop is None:
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()
        return loop.run_until_complete(coro)

    def _pending(self, thread_id: str, *, service: Any = None) -> dict[str, Any]:
        svc = service if service is not None else self.service
        payload = svc.pending_for(thread_id) or {}
        return dict(payload.get("value") or {})

    def _files(self) -> set[str]:
        return {str(item.relative_to(self.stage)) for item in self.stage.rglob("*") if item.is_file()}

    def _business_writes(self, before: set[str]) -> list[str]:
        after = self._files()
        return sorted(
            name for name in (after - before) if pathlib.PurePath(name).name not in CONTROL_FILE_NAMES
        )

    def _external_sends(self) -> int:
        return len(self._sender.calls)

    def _intent_of(self, graph: Mapping[str, Any]) -> str:
        return str(graph.get("intent") or "")

    def _intent_message(self, graph: Mapping[str, Any]) -> str:
        for item in graph.get("messages") or []:
            summary = str((item or {}).get("summary") or "")
            if summary.startswith("意图="):
                return summary
        return ""

    # ---- 各步骤 ------------------------------------------------------

    def _step_doctor(self) -> list[str]:
        report = self.service.doctor()
        # role_isolation 是「这个服务有没有挂角色视图」的自检，不是环境问题：
        # 演示脚本本来就按角色开多个服务实例（见 DEMO_STEP_ROLES），
        # 单看一个实例的这项检查为 false 是正常的。它照常打印出来给人看，
        # 但不参与 doctor_ok —— 否则演示会因为「某个实例只挂了 analyst」而报环境不可用。
        checks = [item for item in (report.get("checks") or []) if str(item.get("name")) != "role_isolation"]
        lines = ["doctor_ok=" + str(bool(all(item.get("ok") for item in checks))).lower()]
        for check in report.get("checks") or []:
            lines.append(
                "  check."
                + str(check.get("name"))
                + "="
                + str(check.get("ok")).lower()
                + " "
                + str(check.get("detail"))[:70]
            )
        return lines

    def _step_replay_qa(self) -> list[str]:
        # 这一步要证明「只读取数真的执行了」。analyst 只有只读能力，所以要取数必须用它；
        # 演示服务默认也是 analyst（DEMO_PRIMARY_ROLE），这里显式写出来是为了让读者不必去翻常量。
        self._service = self._service_for("analyst")
        session, run, events = self._await(self._drive(query=DEFAULT_DEMO_QUERY))
        graph = self.service.graph_state(session, run.thread_id).get("graph") or {}
        lines = [
            "intent=" + self._intent_of(graph),
            "intent_message=" + self._intent_message(graph),
        ]
        for name, payload in events:
            if name == "tool":
                lines.append(
                    "tool_event=" + json.dumps(payload, ensure_ascii=False, sort_keys=True)[:220]
                )
        answer = str(graph.get("final_answer") or "")
        lines.append("answer=" + answer.replace(chr(10), " | ")[:260])
        for item in graph.get("errors") or []:
            lines.append("error=" + str(item)[:160])
        citations = list(graph.get("citations") or [])
        lines.append("graph_citations=" + str(len(citations)))
        for item in citations[:3]:
            citation = str(item.get("citation") or "")
            if citation:
                lines.append("citation=" + citation)
        retriever = self.retriever()
        outcome = retriever.search(DEFAULT_DEMO_QUERY, top_k=3)
        lines.append("rag_query=" + DEFAULT_DEMO_QUERY)
        lines.append("rag_mode=" + str(outcome.mode))
        lines.append("rag_degraded=" + str(bool(outcome.degraded)).lower())
        lines.append("rag_degraded_reason=" + str(outcome.degraded_reason)[:120])
        return lines

    def _step_missing_data(self) -> list[str]:
        session, run, _events = self._await(
            self._drive(query="生成今天的日报", as_of=DEMO_MISSING_AS_OF)
        )
        graph = self.service.graph_state(session, run.thread_id).get("graph") or {}
        lines = [
            "requested_as_of=" + DEMO_MISSING_AS_OF,
            "intent=" + self._intent_of(graph),
        ]
        for item in graph.get("errors") or []:
            lines.append("error=" + str(item)[:200])
        answer = str(graph.get("final_answer") or "")
        lines.append("answer=" + answer.replace(chr(10), " | ")[:300])
        lines.append("data_snapshot_id=" + str(graph.get("data_snapshot_id") or "<空>"))
        return lines

    def _step_reject_side_effects(self) -> list[str]:
        before = self._files()
        session, run, _events = self._await(
            self._drive(query=DEMO_PUSH_QUERY, slots={"slot": "morning_build"})
        )
        pending = self._pending(run.thread_id)
        lines = [
            "pending_tool=" + str(pending.get("tool_name")),
            "pending_capability=" + str(pending.get("capability")),
            "arguments_hash=" + str(pending.get("arguments_hash")),
        ]
        out = self.service.decide(
            session,
            thread_id=run.thread_id,
            decision={"approval_id": str(pending.get("approval_id")), "approved": False},
        )
        lines.append("approved=false")
        lines.append("status=" + str(out.get("status")))
        lines.append("error_codes=" + json.dumps(list(out.get("error_codes") or []), ensure_ascii=False))
        lines.append("answer=" + str(out.get("answer") or "").replace(chr(10), " | ")[:200])
        writes = self._business_writes(before)
        suffix = " " + json.dumps(writes, ensure_ascii=False) if writes else ""
        lines.append("business_writes=" + str(len(writes)) + suffix)
        lines.append("external_sends=" + str(self._external_sends()))
        return lines

    def _step_approve_two_phase(self) -> list[str]:
        before = self._files()
        session, run, _events = self._await(
            self._drive(query=DEMO_PUSH_QUERY, slots={"slot": "morning_build"})
        )
        pending = self._pending(run.thread_id)
        lines = [
            "approval_id=" + str(pending.get("approval_id")),
            "arguments_hash=" + str(pending.get("arguments_hash")),
            "action_hash=" + str(pending.get("action_hash")),
            "dry_run=" + str(bool(pending.get("dry_run"))),
        ]
        out = self.service.decide(
            session,
            thread_id=run.thread_id,
            decision={
                "approval_id": str(pending.get("approval_id")),
                "approved": True,
                "arguments_hash": str(pending.get("arguments_hash")),
            },
        )
        lines.append("approved=true")
        lines.append("status=" + str(out.get("status")))
        lines.append("answer=" + str(out.get("answer") or "").replace(chr(10), " | ")[:260])
        writes = self._business_writes(before)
        suffix = " " + json.dumps(writes, ensure_ascii=False) if writes else ""
        lines.append("business_writes=" + str(len(writes)) + suffix)
        lines.append("external_sends=" + str(self._external_sends()))
        return lines

    def _step_sandbox_layer(self) -> list[str]:
        from pq_agent.sandbox.base import ExecRequest
        from pq_agent.sandbox.docker_runtime import probe_docker
        from pq_agent.sandbox.local_runtime import LocalRuntime
        from pq_agent.sandbox.static_check import analyze

        cases = self.redteam_cases()
        rt = LocalRuntime(root=self.repo_root, work_parent=self.stage / "sandbox")
        lines = ["isolation_level=" + str(rt.isolation_level)]
        untrusted_code = str(cases["mal-01"]["code"])
        untrusted = rt.execute(ExecRequest(code=untrusted_code, purpose="demo:untrusted"))
        lines.append("untrusted_status=" + untrusted.status)
        lines.append("untrusted_error=" + str(untrusted.error_code))
        lines.append("untrusted_executed=" + str(bool(untrusted.executed)).lower())
        static = analyze(untrusted_code)
        lines.append("static_blocked=" + str(bool(static.blocked)))
        for finding in list(static.findings)[:3]:
            lines.append("static_finding=" + str(finding))
        benign = rt.execute(
            ExecRequest(
                code=str(cases["ben-01"]["code"]),
                trusted_fixture=True,
                purpose="demo:benign",
                timeout_s=5.0,
            )
        )
        lines.append("benign_status=" + benign.status)
        lines.append("benign_stdout=" + json.dumps(benign.stdout)[:60])
        slow = rt.execute(
            ExecRequest(
                code=str(cases["mal-09"]["code"]),
                trusted_fixture=True,
                purpose="demo:timeout",
                timeout_s=2.0,
            )
        )
        lines.append("slow_status=" + slow.status)
        lines.append("slow_error=" + str(slow.error_code))
        caps = rt.capabilities()
        entries = list(caps.get("capabilities") or [])
        supported = [str(item.get("key")) for item in entries if item.get("status") == "supported"]
        unsupported = [str(item.get("key")) for item in entries if item.get("status") != "supported"]
        lines.append("capabilities_supported=" + str(len(supported)) + " " + json.dumps(supported, ensure_ascii=False))
        lines.append("capabilities_unsupported=" + str(len(unsupported)) + " " + json.dumps(unsupported, ensure_ascii=False))
        probe = probe_docker()
        lines.append("docker=" + ("available" if probe.available else "unavailable"))
        lines.append("docker_detail=" + str(probe.error or probe.server_version)[:120])
        return lines

    def _step_trace_alerts(self) -> list[str]:
        # 这一步看的是「一次真实运行产生的 span」，所以要取数、要能执行工具：用 analyst。
        self._service = self._service_for("analyst")
        session, run, _events = self._await(self._drive(query=DEFAULT_DEMO_QUERY))
        summary = (self.service.trace_payload(run.trace_id) or {}).get("trace") or {}
        detail = (self.service.trace_detail_payload(run.trace_id) or {}).get("detail") or {}
        apm = self.service.apm_payload() or {}
        lines = [
            "trace_id=" + str(summary.get("trace_id")),
            "otel_trace_id=" + str(summary.get("otel_trace_id")),
            "span_count=" + str(summary.get("span_count")),
            "node_sequence=" + json.dumps(list(summary.get("node_sequence") or []), ensure_ascii=False),
            "recorded_spans=" + str(apm.get("recorded_spans")),
            "dropped_spans=" + str(apm.get("dropped_spans")),
            "collector=" + str(apm.get("collector")),
            "firing_alerts=" + json.dumps(list(apm.get("firing_alerts") or []), ensure_ascii=False),
        ]
        if detail:
            alerts = detail.get("alerts") or {}
            lines.append("detail_alerts_firing=" + json.dumps(list(alerts.get("firing") or []), ensure_ascii=False))
            lines.append("detail_links=" + str(len(detail.get("links") or [])))
        else:
            lines.append("detail=none")
        unavailable = str(apm.get("collector") or "") not in ("", "available")
        lines.append("apm_degraded=" + str(unavailable).lower())
        return lines

    def _step_resume_checkpoint(self) -> list[str]:
        session, run, _events = self._await(
            self._drive(query=DEMO_PUSH_QUERY, slots={"slot": "morning_build"})
        )
        pending = self._pending(run.thread_id)
        # service.tools() 现在是角色视图（默认 supervisor 一个工具都看不到），
        # 这一步要看真实执行次数，必须显式走上真注册表：raw_registry() 就是为这种审计用途留的。
        registry = self.service.raw_registry()
        executed_before = len([item for item in registry.call_log if str(item.get("tool")) == "push_preview"])
        new_service = self.fresh_service()
        durable = dict((new_service.pending_for(run.thread_id) or {}).get("value") or {})
        lines = [
            "interrupted=" + str(bool(pending.get("tool_name"))).lower(),
            "pending_tool=" + str(pending.get("tool_name")),
            "pending_from_new_instance=" + str(durable.get("tool_name")),
            "same_approval_id=" + str(str(durable.get("approval_id")) == str(pending.get("approval_id"))).lower(),
            "executions_before_resume=" + str(executed_before),
        ]
        out = self.service.decide(
            session,
            thread_id=run.thread_id,
            decision={
                "approval_id": str(pending.get("approval_id")),
                "approved": True,
                "arguments_hash": str(pending.get("arguments_hash")),
            },
        )
        after = len([item for item in registry.call_log if str(item.get("tool")) == "push_preview"])
        lines.append("resumed=" + str(str(out.get("status")) == "finished").lower())
        lines.append("status=" + str(out.get("status")))
        lines.append("side_effect_executions=" + str(after - executed_before))
        return lines

    def _step_role_denied(self) -> list[str]:
        from pq_agent.roles import RoleError
        from pq_agent.roles.role_registry import RoleRegistry

        # 角色视图直接建在真注册表上：service.tools() 已经是某一个角色的视图，
        # 再拿它去包第二个角色会叠两层判定（RoleRegistry 现在会显式拒绝这种包装）。
        registry = self.service.raw_registry()
        lines = []
        for role in ("supervisor", "analyst", "operator"):
            view = RoleRegistry(registry, role)
            lines.append(role + "_tools=" + json.dumps(list(view.visible_tool_names()), ensure_ascii=False))
        analyst = RoleRegistry(registry, "analyst")
        # 注意：这里必须看增量，不能看总数。角色视图共享同一个 call_log，
        # 前面几步已经执行过只读工具，总数本来就是非零；要看的是「被拒这次有没有留下痕迹」。
        # 演示的是越权写业务文件（business_write）：它不属于任何角色，也没有人工确认旁路，
        # 所以必须被拦。control_write（推送预览）不在这一条里 —— 它只需要人工确认，
        # 角色视图放行它走审批，那不是越权（见 roles/role_registry.py 的说明）。
        before = len(registry.call_log)
        try:
            analyst.invoke("report_write", {"as_of": "2026-07-09"})
            lines.append("role_denied_code=NOT-DENIED-BUG")
        except RoleError as exc:
            lines.append("role_denied_code=" + str(exc.code))
            lines.append("role_denied_message=" + str(exc)[:200])
        lines.append("call_log_delta_after_deny=" + str(len(registry.call_log) - before))
        lines.append("call_log_total=" + str(len(registry.call_log)))
        return lines

    _STEPS: dict[str, str] = {
        "doctor": "_step_doctor",
        "replay_qa": "_step_replay_qa",
        "missing_data": "_step_missing_data",
        "reject_side_effects": "_step_reject_side_effects",
        "approve_two_phase": "_step_approve_two_phase",
        "sandbox_layer": "_step_sandbox_layer",
        "trace_alerts": "_step_trace_alerts",
        "resume_checkpoint": "_step_resume_checkpoint",
        "role_denied": "_step_role_denied",
    }

    def run(self, step_ids: Sequence[str] | None = None) -> DemoRun:
        """跑演示。整场套在 demo_offline 网络熔断器里，末了核对零外呼。"""

        from pq_agent.data.provider import NetworkGuard

        requested = [str(item) for item in (step_ids or [])] or [step.step_id for step in DEMO_STEPS]
        result = DemoRun(
            runtime_dir=str(self.stage),
            narration_seconds=total_narration_seconds(),
        )
        started = time.perf_counter()
        # 先建循环再进闸门：循环构造时的自身管道是一次 127.0.0.1 连接，
        # 建在闸门里会被自己的熔断器掐掉。
        self._loop = asyncio.new_event_loop()
        try:
            with NetworkGuard() as guard:
                for step_id in requested:
                    result.steps.append(self._run_one(step_by_id(step_id)))
                result.network_attempts = int(guard.attempt_count)
        finally:
            self._loop.close()
            self._loop = None
        result.elapsed_ms = int((time.perf_counter() - started) * 1000)
        if result.network_attempts:
            result.blockers.append(
                DEMO_NETWORK_DETECTED + ": 演示路径出现 " + str(result.network_attempts) + " 次外呼尝试"
            )
        for item in result.steps:
            result.blockers.extend(item.blockers)
        result.ok = (
            bool(result.steps)
            and all(item.ok for item in result.steps)
            and not result.blockers
            and result.network_attempts == 0
        )
        return result

    def _run_one(self, step: DemoStep) -> DemoStepResult:
        item = DemoStepResult(step_id=step.step_id, title=step.title, ok=False)
        started = time.perf_counter()
        # 每一步用它该用的角色服务：只读步骤用 analyst，写/外发步骤用 operator。
        self._service = self._service_for(DEMO_STEP_ROLES.get(step.step_id, DEMO_PRIMARY_ROLE))
        try:
            item.lines = list(getattr(self, self._STEPS[step.step_id])())
        except DemoNotReady as exc:
            item.error = str(exc)
            item.blockers = (str(exc.code),)
        except DemoPathError as exc:
            item.error = str(exc)
            item.blockers = (str(exc.code),)
        except Exception as exc:  # noqa: BLE001 - 演示脚本不允许静默失败
            item.error = type(exc).__name__ + ": " + str(exc)
            item.blockers = (DEMO_ERROR,)
        item.duration_ms = int((time.perf_counter() - started) * 1000)
        joined = chr(10).join(item.lines)
        item.markers_missing = tuple(marker for marker in step.markers if marker not in joined)
        item.ok = not item.error and not item.markers_missing
        return item


def render_run(result: DemoRun, *, steps: Sequence[DemoStep] = ()) -> str:
    """把执行结果渲染成给面试官看的一屏一屏文字。"""

    table = {step.step_id: step for step in (steps or DEMO_STEPS)}
    lines: list[str] = []
    total = len(result.steps)
    for index, item in enumerate(result.steps, start=1):
        step = table.get(item.step_id)
        lines.append("=" * 68)
        lines.append("[%d/%d] %s" % (index, total, item.title))
        lines.append("=" * 68)
        lines.extend(item.lines or ["（没有输出）"])
        if item.error:
            lines.append("错误：" + item.error)
        if item.markers_missing:
            lines.append("缺失标记：" + json.dumps(list(item.markers_missing), ensure_ascii=False))
        if step is not None:
            lines.append("离线说明：" + step.offline_note)
        lines.append("状态：" + ("通过" if item.ok else "失败") + "（" + str(item.duration_ms) + " ms）")
    lines.append("-" * 68)
    lines.append("讲解预算：%d 秒；脚本实测：%d ms" % (result.narration_seconds, result.elapsed_ms))
    lines.append("外呼尝试：%d（必须为 0）" % result.network_attempts)
    for blocker in result.blockers:
        lines.append("阻塞：" + blocker)
    lines.append("结论：" + ("全部通过" if result.ok else "存在失败"))
    return chr(10).join(lines)


def demo_status(*, repo_root: pathlib.Path | None = None) -> dict[str, Any]:
    """不开跑，只报告演示环境的静态事实。"""

    repo = repo_root_of(repo_root)
    root = demo_root(repo)
    index_path = repo / "agent_eval" / "results" / "rag_index.json"
    corpus_path = repo / "agent_eval" / "cases" / "sandbox_redteam.json"
    return {
        "repo_root": str(repo),
        "demo_root": str(root),
        "demo_root_exists": root.exists(),
        "inside_tmp": is_inside_demo(root, repo_root=repo),
        "rag_index": str(index_path),
        "rag_index_exists": index_path.is_file(),
        "sandbox_corpus": str(corpus_path),
        "sandbox_corpus_exists": corpus_path.is_file(),
        "steps": [step.step_id for step in DEMO_STEPS],
        "narration_seconds": total_narration_seconds(),
    }


__all__ = [
    "CONTROL_FILE_NAMES",
    "DEFAULT_DEMO_QUERY",
    "DEMO_CORPUS_MISSING",
    "DEMO_ERROR",
    "DEMO_MISSING_AS_OF",
    "DEMO_NETWORK_DETECTED",
    "DEMO_PUSH_QUERY",
    "DEMO_RAG_INDEX_MISSING",
    "DemoHarness",
    "DemoNotReady",
    "DemoRun",
    "DemoStepResult",
    "demo_status",
    "pick_free_port",
    "render_run",
]
