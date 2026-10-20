"""pq_agent 本地 API 服务：离线演示面板、SSE 事件流、人工确认入口。

## 为什么单独一层 server

- 图本身是纯逻辑（不依赖 HTTP 框架）。服务层只做四件事：会话归属、鉴权、事件转发、状态查询。
- SSE 需要 async，但锁定版 SqliteSaver **只实现同步接口**（aget_tuple 直接抛 NotImplementedError）。
  本项目的真实取舍是：工作线程跑同步 app.stream，用 asyncio 队列把事件桥回事件循环，
  而不是为了好看去换 AsyncSqliteSaver 并引入 aiosqlite 这个新依赖。
- 会话归属用 HttpOnly + SameSite=Strict 的 cookie 承载，thread_id 必须属于当前会话，
  否则一个浏览器可以读另一个浏览器的持仓问答。

## 安全边界（对应 A1-07 任务合同）

1. 监听地址只能是 127.0.0.1；填 0.0.0.0 直接构造失败。
2. 每个 endpoint 都要 X-PQ-Token，比较用 hmac.compare_digest（常量时间）。
3. token 只在内存里，进程退出即失效，不写盘、不进日志、不进 telemetry。
4. 写操作（POST）必须带自定义头，且 Origin/Host 必须是本机，否则拒绝——这是 CSRF 防线。
5. body 有硬上限，query 有长度上限，超限直接拒绝。
6. trace 接口只回摘要（节点序列、耗时、错误码、降级原因），绝不回原始属性。
7. demo 模式绑定 demo_offline + fixture + 无 sender，运行时目录固定在仓库 .tmp 下。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import datetime as dt
import hmac
import json
import os
import pathlib
import secrets as _secrets
import socket
import threading
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pq_agent.contracts import ContractError
from pq_agent.graph import (
    GraphDeps,
    build_graph,
    pending_interrupt,
    resume_graph,
    thread_config,
)
from pq_agent.roles import ROLE_SUPERVISOR, RoleError, RoleRegistry, role_definition
from pq_agent.roles.role_registry import role_registry as make_role_registry

from pq_agent.state import new_state, validate_state
from pq_agent.observability.apm_signals import SPAN_RUN

# 演示服务只在本机跑，且只有装了 agent extra 才会走到这里。
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

SERVER_SCHEMA_VERSION = 1

# A4-02 APM snapshot schema version.
APM_ENVIRONMENT_SCHEMA_VERSION = "pq_agent.apm_environment/1"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8111
DEFAULT_BODY_LIMIT_BYTES = 32768
DEFAULT_MAX_QUERY_CHARS = 500
SESSION_COOKIE = "pq_session"
TOKEN_HEADER = "X-PQ-Token"
# 运行模式三选一：演示用 fixture、回放用固定快照、live 必须接真实数据源。
# 为什么要显式区分：以前 --no-demo 仍会落到 demo_offline，等于把演示数据
# 当成真实链路的结果。
MODE_DEMO_OFFLINE = "demo_offline"
MODE_REPLAY = "replay"
MODE_LIVE = "live"
RUN_MODES = (MODE_DEMO_OFFLINE, MODE_REPLAY, MODE_LIVE)
ALLOWED_HOSTNAMES = ("127.0.0.1", "localhost", "::1")
DEMO_BANNER = "演示数据：全部行情来自本地 fixture（demo_offline），禁止用于真实决策或生产推送"

# 错误码：全部显式，前端与测试都按错误码断言，不靠 HTTP 状态猜原因
ERROR_TOKEN_MISSING = "SERVER_TOKEN_MISSING"
ERROR_TOKEN_INVALID = "SERVER_TOKEN_INVALID"
ERROR_CSRF_HEADER_REQUIRED = "SERVER_CSRF_HEADER_REQUIRED"
ERROR_ORIGIN_DENIED = "SERVER_ORIGIN_DENIED"
ERROR_HOST_DENIED = "SERVER_HOST_DENIED"
ERROR_BODY_TOO_LARGE = "SERVER_BODY_TOO_LARGE"
ERROR_QUERY_INVALID = "SERVER_QUERY_INVALID"
ERROR_QUERY_TOO_LONG = "SERVER_QUERY_TOO_LONG"
ERROR_SESSION_MISSING = "SERVER_SESSION_MISSING"
ERROR_THREAD_NOT_FOUND = "SERVER_THREAD_NOT_FOUND"
ERROR_THREAD_FORBIDDEN = "SERVER_THREAD_FORBIDDEN"
ERROR_THREAD_BUSY = "SERVER_THREAD_BUSY"
ERROR_APPROVAL_NOT_PENDING = "SERVER_APPROVAL_NOT_PENDING"
ERROR_APPROVAL_STALE = "SERVER_APPROVAL_STALE"
ERROR_APPROVAL_DUPLICATE = "SERVER_APPROVAL_DUPLICATE"
ERROR_APPROVAL_FORBIDDEN = "SERVER_APPROVAL_FORBIDDEN"
ERROR_TRACE_NOT_FOUND = "SERVER_TRACE_NOT_FOUND"
ERROR_RUN_FAILED = "SERVER_RUN_FAILED"
ERROR_PORT_IN_USE = "SERVER_PORT_IN_USE"
ERROR_DRIVER_MISSING = "SERVER_DRIVER_MISSING"
ERROR_HOST_NOT_LOCAL = "SERVER_HOST_NOT_LOCAL"
# 角色层拒绝。值和 roles.contracts 里的码完全一致，不另造一套：
# 前端拿到的 error_code 必须能和角色模块的文档对上。
ERROR_ROLE_UNKNOWN = "SERVER_ROLE_UNKNOWN"
ERROR_ROLE_CAPABILITY = "ROLE_CAPABILITY_DENIED"
# 「不挂角色视图」。只给演示脚本这种「要同时展示只读与写」的完整链路用；
# 空串也当作它处理。真实服务的默认值是 supervisor，不是它。
SERVICE_ROLE_UNSCOPED = "unscoped"

RUN_STATUSES = ("running", "awaiting_approval", "finished", "aborted", "failed")


class ApiError(Exception):
    """带 HTTP 状态与错误码的业务异常。前端只看 error_code。"""

    def __init__(self, status: int, code: str, message: str, *, hint: str = "") -> None:
        super().__init__(message)
        self.status = int(status)
        self.code = str(code)
        self.message = str(message)
        self.hint = str(hint)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": False, "error_code": self.code, "message": self.message}
        if self.hint:
            payload["hint"] = self.hint
        return payload


def is_unscoped_role(role: Any) -> bool:
    """这个 role 是不是「不挂角色视图」。空串也按它算（等价于历史上「没传角色」）。"""
    return str(role or "") in ("", SERVICE_ROLE_UNSCOPED)


def _repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[2]


def _now_iso(moment: dt.datetime | None = None) -> str:
    stamp = moment if moment is not None else dt.datetime.now(dt.timezone.utc)
    if stamp.tzinfo is None:
        raise ContractError("DATETIME_NAIVE", "服务层时间必须带时区")
    return stamp.isoformat()


@dataclass
class ServerSettings:
    """服务设置。token 为空时进程内生成，绝不落盘。"""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    demo: bool = True
    token: str = ""
    body_limit_bytes: int = DEFAULT_BODY_LIMIT_BYTES
    max_query_chars: int = DEFAULT_MAX_QUERY_CHARS
    runtime_dir: pathlib.Path | None = None
    repo_root: pathlib.Path | None = None
    allow_query_token: bool = True
    mode: str = ""
    # 默认角色。supervisor 的能力集是空的：默认配置下服务层一个工具都调不动，
    # 要真读数据必须显式把角色写在启动参数里（--role analyst / --role operator）。
    # 这不是故意添堵，是「权限默认最小」—— 以前 role 参数被读进 self._role 就没人用了。
    role: str = ROLE_SUPERVISOR

    def __post_init__(self) -> None:
        if str(self.host) not in ALLOWED_HOSTNAMES:
            raise ContractError(
                ERROR_HOST_NOT_LOCAL,
                "本地演示服务只允许监听 127.0.0.1，实际 " + str(self.host),
            )
        if not self.token:
            self.token = _secrets.token_urlsafe(32)
        if int(self.body_limit_bytes) <= 0:
            raise ContractError(ERROR_BODY_TOO_LARGE, "body_limit_bytes 必须为正数")
        if int(self.max_query_chars) <= 0:
            raise ContractError(ERROR_QUERY_INVALID, "max_query_chars 必须为正数")
        if self.mode and self.mode not in RUN_MODES:
            raise ContractError("RUN_MODE_INVALID", "未知运行模式：" + str(self.mode))
        if self.mode == MODE_DEMO_OFFLINE and not self.demo:
            raise ContractError("MODE_DEMO_OFFLINE_FORBIDDEN", "关闭演示标记后不能再声明 demo_offline 模式")
        # 角色必须在装配时就合法：拼错一个字母不许悄悄回落成 supervisor，
        # 也不许留到第一次调工具时才报错。
        if not is_unscoped_role(self.role):
            try:
                role_definition(self.role)
            except RoleError as exc:
                raise ContractError(ERROR_ROLE_UNKNOWN, "未知角色：" + repr(self.role) + "；" + exc.detail) from exc
        self.port = int(self.port)
        self.repo_root = pathlib.Path(self.repo_root) if self.repo_root else _repo_root()
        self.runtime_dir = (
            pathlib.Path(self.runtime_dir)
            if self.runtime_dir is not None
            else self.repo_root / ".tmp" / "pq_agent_server"
        )

    def public_dict(self) -> dict[str, Any]:
        """对外暴露的设置摘要。token 永远不出现。"""
        return {
            "host": self.host,
            "port": self.port,
            "demo": bool(self.demo),
            "mode": str(self.mode or (MODE_DEMO_OFFLINE if self.demo else MODE_REPLAY)),
            "body_limit_bytes": int(self.body_limit_bytes),
            "max_query_chars": int(self.max_query_chars),
            "runtime_dir": str(self.runtime_dir),
            "role": str(self.role),
        }


@dataclass
class Session:
    session_id: str
    actor: str
    created_at: str
    threads: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "actor": self.actor,
            "created_at": self.created_at,
            "threads": list(self.threads),
        }


class JsonStateFile:
    """极小的 JSON 状态文件：原子写、坏文件显式报错、写入幂等。

    为什么不用 sqlite：这里存的是会话归属和运行摘要，量级是「几十条」，
    用一个 JSON 文件就能看清全部内容，运维排障时直接打开就行。
    但必须原子写（先写 .tmp 再 os.replace），否则进程在写一半时被杀会留下坏文件，
    重启后整站 500。
    """

    def __init__(self, path: pathlib.Path | str | None = None) -> None:
        self.path = pathlib.Path(path) if path else None
        self._lock = threading.Lock()

    def load(self) -> dict[str, Any]:
        if self.path is None or not self.path.exists():
            return {}
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ContractError("SERVER_STATE_UNREADABLE", "状态文件读不出来：" + str(self.path) + "：" + str(exc)) from exc
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw)
        except ValueError as exc:
            # 坏文件不静默当成空：那等于悄悄丢掉所有会话归属与审批现场
            raise ContractError("SERVER_STATE_CORRUPT", "状态文件损坏：" + str(self.path) + "：" + str(exc)) from exc
        if not isinstance(data, Mapping):
            raise ContractError("SERVER_STATE_CORRUPT", "状态文件顶层必须是对象：" + str(self.path))
        return dict(data)

    def save(self, data: Mapping[str, Any]) -> None:
        if self.path is None:
            return
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(dict(data), ensure_ascii=False, sort_keys=True, indent=1)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(payload, encoding="utf-8")
            os.replace(str(tmp), str(self.path))


class SessionRegistry:
    """会话与线程归属。线程一旦绑定到某个会话，别的会话不能读。

    A6-04：状态写到磁盘。进程重启后，同一个 session_id / thread_id 的归属必须还在，
    否则重启前停在审批的线程，重启后连「这条线程属于谁」都查不到，HTTP HITL 直接断掉。
    """

    def __init__(self, *, clock=None, store_path: pathlib.Path | str | None = None) -> None:
        self._clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self._store = JsonStateFile(store_path)
        self._sessions: dict[str, Session] = {}
        self._thread_owner: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        data = self._store.load()
        for item in data.get("sessions") or []:
            if not isinstance(item, Mapping):
                continue
            session = Session(
                session_id=str(item.get("session_id") or ""),
                actor=str(item.get("actor") or "local"),
                created_at=str(item.get("created_at") or ""),
                threads=[str(x) for x in (item.get("threads") or [])],
            )
            if session.session_id:
                self._sessions[session.session_id] = session
        for thread_id, session_id in (data.get("thread_owner") or {}).items():
            self._thread_owner[str(thread_id)] = str(session_id)

    def _save(self) -> None:
        self._store.save({
            "schema_version": 1,
            "sessions": [session.to_dict() for session in self._sessions.values()],
            "thread_owner": dict(self._thread_owner),
        })

    def create(self, actor: str = "local") -> Session:
        session = Session(
            session_id="s" + uuid.uuid4().hex[:16],
            actor=str(actor or "local"),
            created_at=_now_iso(self._clock()),
        )
        self._sessions[session.session_id] = session
        self._save()
        return session

    def get(self, session_id: str | None) -> Session | None:
        if not session_id:
            return None
        return self._sessions.get(str(session_id))

    def owner_of(self, thread_id: str) -> str | None:
        return self._thread_owner.get(str(thread_id))

    def owns(self, session_id: str | None, thread_id: str) -> bool:
        owner = self.owner_of(thread_id)
        if owner is None:
            return False
        return bool(session_id) and owner == str(session_id)

    def bind(self, session_id: str, thread_id: str) -> None:
        """把线程登记到会话。已被别的会话占用时显式拒绝。"""
        current = self.owner_of(thread_id)
        if current is not None and current != str(session_id):
            raise ApiError(403, ERROR_THREAD_FORBIDDEN, "该会话无权访问线程 " + str(thread_id))
        self._thread_owner[str(thread_id)] = str(session_id)
        session = self._sessions.get(str(session_id))
        if session is not None and thread_id not in session.threads:
            session.threads.append(str(thread_id))
        self._save()

    def threads_of(self, session_id: str) -> list[str]:
        session = self.get(session_id)
        return list(session.threads) if session is not None else []


@dataclass
class RunRecord:
    """一次会话运行的公开摘要。不含 prompt、原始参数、密钥。"""

    run_id: str
    thread_id: str
    session_id: str
    actor: str
    query: str
    mode: str
    as_of: str
    demo: bool
    status: str = "running"
    trace_id: str = ""
    # OTel 自己生成的 trace_id。它不是本服务的运行编号：
    # 拿运行编号去 span 里过滤，结果永远是「一个 span 都没有」。
    otel_trace_id: str = ""
    intent: str = ""
    intent_source: str = ""
    # 调用方显式给出的槽位（时段、日期、股票代码、top_k）。
    # 意图层只从文本里抽它看得懂的，缺参数时计划层会停下来问；
    # 这里给的是「用户已经在界面上选好了」的那部分，不能靠文本猜。
    slots: dict[str, Any] = field(default_factory=dict)
    nodes: list[str] = field(default_factory=list)
    tool_calls: int = 0
    approval_requests: int = 0
    decided_approvals: list[str] = field(default_factory=list)
    error_codes: list[str] = field(default_factory=list)
    answer_preview: str = ""
    next_nodes: list[str] = field(default_factory=list)
    cursor: int = 0
    data_snapshot_id: str = ""
    client_disconnected: bool = False
    started_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SERVER_SCHEMA_VERSION,
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "actor": self.actor,
            "query": self.query,
            "mode": self.mode,
            "as_of": self.as_of,
            "demo": bool(self.demo),
            "status": self.status,
            "trace_id": self.trace_id,
            "otel_trace_id": self.otel_trace_id,
            "intent": self.intent,
            "intent_source": self.intent_source,
            "slots": dict(self.slots),
            "nodes": list(self.nodes),
            "tool_calls": int(self.tool_calls),
            "approval_requests": int(self.approval_requests),
            "error_codes": list(self.error_codes),
            "answer_preview": self.answer_preview,
            "next_nodes": list(self.next_nodes),
            "cursor": int(self.cursor),
            "data_snapshot_id": self.data_snapshot_id,
            "client_disconnected": bool(self.client_disconnected),
            "started_at": self.started_at,
            "updated_at": self.updated_at,
        }


class RunStore:
    """线程级运行登记。一个线程同时只允许一次运行。

    A6-04：记录写盘。重启后同一条线程必须还能查到它跑到哪个节点、
    有没有待审批动作；否则 HTTP 会给出「线程不存在」，而 SQLite 里 checkpoint 还在。
    """

    def __init__(self, store_path: pathlib.Path | str | None = None) -> None:
        self._lock = threading.Lock()
        self._store = JsonStateFile(store_path)
        self._by_thread: dict[str, RunRecord] = {}
        self._by_id: dict[str, RunRecord] = {}
        self._load()

    def _load(self) -> None:
        data = self._store.load()
        fields = {item.name for item in dataclasses.fields(RunRecord)}
        for item in data.get("runs") or []:
            if not isinstance(item, Mapping):
                continue
            payload = {key: value for key, value in item.items() if key in fields}
            try:
                record = RunRecord(**payload)
            except TypeError:
                continue
            self._by_id[record.run_id] = record
            self._by_thread[record.thread_id] = record

    def _save(self) -> None:
        # 存盘用的是持久化形状（mode 明确写成 replay/demo_offline/live），
        # 而 to_dict() 里同一个键是面向客户端展示的 "demo_offline"/"replay"，
        # 直接拿 to_dict() 回灌不回去。所以整条记录按字段名一一映射。
        self._store.save({
            "schema_version": 1,
            "runs": [self._persisted(record) for record in self._by_id.values()],
        })

    @staticmethod
    def _persisted(record: RunRecord) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for item in dataclasses.fields(RunRecord):
            out[item.name] = getattr(record, item.name)
        return out

    def start(self, record: RunRecord) -> RunRecord:
        with self._lock:
            existing = self._by_thread.get(record.thread_id)
            if existing is not None and existing.status == "running":
                raise ApiError(409, ERROR_THREAD_BUSY, "线程 " + record.thread_id + " 已有运行中的任务")
            self._by_thread[record.thread_id] = record
            self._by_id[record.run_id] = record
            self._save()
        return record

    def get_thread(self, thread_id: str) -> RunRecord | None:
        return self._by_thread.get(str(thread_id))

    def get_run(self, run_id: str) -> RunRecord | None:
        return self._by_id.get(str(run_id))


    def touch(self, record: RunRecord) -> None:
        record.updated_at = _now_iso()
        with self._lock:
            self._by_thread[record.thread_id] = record
            self._by_id[record.run_id] = record
            self._save()

    def save(self, record: RunRecord) -> None:
        """运行状态变了（进入审批 / 结束）就落盘。"""
        self.touch(record)


@dataclass
class TraceSummary:
    """trace 对外只回摘要：节点序列、耗时、错误码、降级原因。"""

    trace_id: str
    run_id: str
    thread_id: str
    mode: str
    as_of: str
    node_sequence: list[str]
    span_count: int
    duration_ms: int
    error_codes: list[str]
    degraded: bool
    degraded_reasons: list[str]
    created_at: str
    otel_trace_id: str = ""
    intent: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SERVER_SCHEMA_VERSION,
            "trace_id": self.trace_id,
            "otel_trace_id": self.otel_trace_id,
            "intent": self.intent,
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "mode": self.mode,
            "as_of": self.as_of,
            "node_sequence": list(self.node_sequence),
            "span_count": int(self.span_count),
            "duration_ms": int(self.duration_ms),
            "error_codes": list(self.error_codes),
            "degraded": bool(self.degraded),
            "degraded_reasons": list(self.degraded_reasons),
            "created_at": self.created_at,
            "note": "仅摘要：不含提示词、原始参数、checkpoint 内容与密钥",
        }


class TraceIndex:
    """trace 摘要索引。落 JSONL，重启后仍可查；永不落原始属性。"""

    def __init__(self, path: pathlib.Path | None = None) -> None:
        self.path = pathlib.Path(path) if path is not None else None
        self._items: dict[str, TraceSummary] = {}

    def add(self, summary: TraceSummary) -> TraceSummary:
        self._items[summary.trace_id] = summary
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline=chr(10)) as handle:
                handle.write(json.dumps(summary.to_dict(), ensure_ascii=False, sort_keys=True))
                handle.write(chr(10))
        return summary

    def get(self, trace_id: str) -> TraceSummary | None:
        found = self._items.get(str(trace_id))
        if found is not None:
            return found
        if self.path is None or not self.path.exists():
            return None
        for line in self.path.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except Exception:
                continue
            if str(row.get("trace_id")) == str(trace_id):
                return TraceSummary(
                    trace_id=str(row.get("trace_id") or ""),
                    otel_trace_id=str(row.get("otel_trace_id") or ""),
                    intent=str(row.get("intent") or ""),
                    run_id=str(row.get("run_id") or ""),
                    thread_id=str(row.get("thread_id") or ""),
                    mode=str(row.get("mode") or ""),
                    as_of=str(row.get("as_of") or ""),
                    node_sequence=list(row.get("node_sequence") or []),
                    span_count=int(row.get("span_count") or 0),
                    duration_ms=int(row.get("duration_ms") or 0),
                    error_codes=list(row.get("error_codes") or []),
                    degraded=bool(row.get("degraded")),
                    degraded_reasons=list(row.get("degraded_reasons") or []),
                    created_at=str(row.get("created_at") or ""),
                )
        return None

    @staticmethod
    def _from_row(row: dict[str, Any]) -> TraceSummary:
        """从 JSONL 行还原摘要。缺字段给空值，不猜。"""
        return TraceSummary(
            trace_id=str(row.get("trace_id") or ""),
            otel_trace_id=str(row.get("otel_trace_id") or ""),
            intent=str(row.get("intent") or ""),
            run_id=str(row.get("run_id") or ""),
            thread_id=str(row.get("thread_id") or ""),
            mode=str(row.get("mode") or ""),
            as_of=str(row.get("as_of") or ""),
            node_sequence=list(row.get("node_sequence") or []),
            span_count=int(row.get("span_count") or 0),
            duration_ms=int(row.get("duration_ms") or 0),
            error_codes=list(row.get("error_codes") or []),
            degraded=bool(row.get("degraded")),
            degraded_reasons=list(row.get("degraded_reasons") or []),
            created_at=str(row.get("created_at") or ""),
        )

    def latest_for_thread(self, thread_id: str) -> TraceSummary | None:
        """该线程最近一条 trace。中断—恢复的因果链接靠它。

        先看内存，再看落盘 JSONL：跨进程恢复时内存索引是空的，
        只看内存会得出「这条线程从没跑过」的错误结论。
        """
        rows = [item for item in self._items.values() if str(item.thread_id) == str(thread_id)]
        if not rows and self.path is not None and self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                text = line.strip()
                if not text:
                    continue
                try:
                    row = json.loads(text)
                except Exception:
                    continue
                if str(row.get("thread_id")) == str(thread_id):
                    rows.append(self._from_row(row))
        if not rows:
            return None
        return sorted(rows, key=lambda item: str(item.created_at or ""))[-1]


@dataclass
class CostLedger:
    """成本账本。拿不到用量就写 unknown，绝不估算（A1-06 的纪律）。"""

    requests: int = 0
    tool_calls: int = 0
    approval_decisions: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SERVER_SCHEMA_VERSION,
            "requests": int(self.requests),
            "tool_calls": int(self.tool_calls),
            "approval_decisions": int(self.approval_decisions),
            "tokens": "unknown",
            "cost": "unknown",
            "currency": "unknown",
            "note": "未接入模型用量回传；按纪律记为 unknown，不做估算",
        }

def check_port_free(host: str, port: int) -> tuple[bool, str]:
    """端口可用性探测。不做任何监听以外的副作用。"""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((str(host), int(port)))
    except OSError as exc:
        return False, "端口不可用：" + type(exc).__name__
    finally:
        probe.close()
    return True, "端口可用"


class AgentService:
    """把图、数据源、工具、审批存储装配成可被 HTTP 调用的服务。

    装配纪律：所有依赖显式注入；缺依赖就报错，绝不静默用假实现。
    demo 模式下运行时目录固定在仓库 .tmp 下，不碰用户的 data/ 与 reports/。
    """

    def __init__(
        self,
        settings: ServerSettings,
        *,
        provider: Any = None,
        tools: Any = None,
        tracer: Any = None,
        clock: Any = None,
        plan_builder: Any = None,
        approval_store: Any = None,
        outbox: Any = None,
        positions: Iterable[Any] = (),
        news_items: Iterable[Any] = (),
        sender: Any = None,
        allowed_dates: Iterable[dt.date] | None = None,
        checkpoint_path: Any = None,
        trace_index: TraceIndex | None = None,
        trace_recorder: Any = None,
        secrets: Iterable[str] | None = None,
        retriever: Any = None,
        intent_model: Any = None,
        role: str | None = None,
        sandbox: Any = None,
    ) -> None:
        self.settings = settings
        # A6-04：会话与运行状态必须能跨进程重启恢复。
        # 以前这里是 SessionRegistry() / RunStore() 两个纯内存对象：
        # 服务重启后同一个 thread 的归属消失，HTTP 就再也没法对同一条线程 resume 审批，
        # 而 SQLite 里的 checkpoint 还在——两边状态对不上，是典型的数据面/控制面分叉。
        self.sessions = SessionRegistry(store_path=settings.runtime_dir / "sessions.json")
        self.runs = RunStore(store_path=settings.runtime_dir / "runs.json")
        self.cost = CostLedger()
        self.demo_as_of = dt.date(2026, 7, 9)
        self.settings.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoint_path = pathlib.Path(checkpoint_path) if checkpoint_path else settings.runtime_dir / "checkpoints.sqlite"
        self.runs_dir = settings.runtime_dir / "runs"
        self.reports_dir = settings.runtime_dir / "reports"
        self.positions_path = settings.runtime_dir / "positions.csv"
        self.trace_index = trace_index or TraceIndex(settings.runtime_dir / "traces.jsonl")
        # A4-02：本机没有 Collector，span 收完就没了。没有 recorder 时行为与 A1-07 完全一致。
        self._trace_recorder = trace_recorder
        # 指标与告警是跨运行累积的全局状态，所以是「一份快照不断覆盖」，
        # 不是每条 trace 存一份——那样会得到互相矛盾的数字。
        self.apm_environment_path = settings.repo_root / "agent_eval" / "results" / "apm_environment.json"
        self._provider = provider
        # 传进来的注册表只当作「底层真注册表」，对外一律给角色视图（见 tools()）。
        # 以前这里直接存下来、tools() 原样返回，于是「角色隔离」只存在于演示脚本里，
        # HTTP 主图拿到的仍是全量注册表 —— 谁都能调 report_write 与 push_send。
        self._raw_tools = tools
        self._tools = None
        self._tracer = tracer
        self._clock = clock
        self._plan_builder = plan_builder
        self._approval_store = approval_store
        self._outbox = outbox
        self._positions = tuple(positions)
        self._news_items = tuple(news_items)
        self._sender = sender
        self._allowed_dates = tuple(allowed_dates) if allowed_dates is not None else (self.demo_as_of,)
        self._secrets = tuple(item for item in (secrets or ()) if isinstance(item, str) and item.strip())
        # A6-01：这四个依赖以前根本没接进图，导致主图必然出现 RAG_UNAVAILABLE、
        # 计划只能靠静态意图映射，角色与沙箱只存在于演示脚本里。
        self._retriever = retriever
        self._intent_model = intent_model
        # 角色来自显式参数，缺省取设置里的（设置里再缺省才是 supervisor）。
        # actor 不是权限凭证，所以不允许拿请求里的用户名当角色用。
        chosen = str(role if role is not None else (getattr(settings, "role", "") or ROLE_SUPERVISOR))
        if not is_unscoped_role(chosen):
            try:
                role_definition(chosen)
            except RoleError as exc:
                raise ContractError(ERROR_ROLE_UNKNOWN, "未知角色：" + repr(chosen)) from exc
            chosen = role_definition(chosen).role
        else:
            # 不挂角色视图时按 supervisor 记账：它是「能力集为空」的那个角色，
            # 与「什么都不给」语义一致，也不需要在角色表里新增一个'不挂角色'的假角色。
            chosen = ROLE_SUPERVISOR
        self._role = chosen
        self._sandbox = sandbox

    # ---- 依赖装配 ----------------------------------------------------
    def now(self) -> dt.datetime:
        if self._clock is None:
            return dt.datetime.now(dt.timezone.utc)
        moment = self._clock()
        if moment.tzinfo is None:
            raise ContractError("DATETIME_NAIVE", "服务层时钟必须返回带时区的 datetime")
        return moment

    def mode(self) -> str:
        """运行模式三选一。demo_offline 只允许在演示标记下使用。"""
        if self.settings.mode:
            return self.settings.mode
        return MODE_DEMO_OFFLINE if self.settings.demo else MODE_REPLAY

    def provider(self) -> Any:
        if self._provider is not None:
            return self._provider
        mode = self.mode()
        from pq_agent.contracts import ContractError

        if mode == MODE_DEMO_OFFLINE:
            if not self.settings.demo:
                raise ContractError("MODE_DEMO_OFFLINE_FORBIDDEN", "非演示模式禁止使用 demo_offline provider")
            from pq_agent.data.provider import create_provider

            self._provider = create_provider("demo_offline", clock=self._clock)
            return self._provider
        # replay / live 必须由调用方显式注入 provider，绝不静默回落 fixture。
        raise ContractError(
            "PROVIDER_REQUIRED",
            "%s 模式必须显式注入 provider；缺依赖时拒绝启动，不回落 demo fixture" % mode,
        )

    def retriever(self) -> Any:
        return self._retriever

    def intent_model(self) -> Any:
        return self._intent_model

    def role_view(self) -> str:
        return self._role

    def sandbox(self) -> Any:
        return self._sandbox

    def raw_registry(self) -> Any:
        """真注册表。只有装配层和体检用得到，**不进图**。

        名字刻意不叫 registry / tools：服务层的唯一对外入口是 tools()，
        它返回的是角色视图。想拿全量注册表就必须明确写 raw_registry()，
        这样评审一眼就能看出「哪里绕过了角色隔离」。
        """
        if self._raw_tools is None:
            from pq_agent.tools.quant_tools import build_default_registry

            self._raw_tools = build_default_registry(
                provider=self.provider(),
                reports_dir=self.reports_dir,
                runs_dir=self.runs_dir,
                positions=self._positions,
                news_items=self._news_items,
                sender=self._sender,
                positions_path=self.positions_path,
                allowed_dates=self._allowed_dates,
                # 沙箱只有显式注入时才会被登记成 code_exec 工具：
                # 服务层不许自己挑运行时，更不许在没有沙箱时装一个假的。
                sandbox=self._sandbox,
            )
        return self._raw_tools

    def tools(self) -> Any:
        """当前角色的工具视图。这是服务层对外唯一的工具入口。

        为什么必须包一层：RoleRegistry 直接建在真注册表上，
        schemas() 只给该角色能看的工具，invoke() 在执行前再查一次能力。
        graph 的 plan_tools 用 get() 取规格，越权工具在计划阶段就被拒掉了。

        任何服务都包，没有例外：角色隔离要进主图，就不能留一个「不用角色」的服务形状。
        """
        if self._tools is None:
            self._tools = make_role_registry(self.raw_registry(), self._role)
        return self._tools

    def approval_store(self) -> Any:
        if self._approval_store is None:
            from pq_agent.approval import ApprovalStore

            self._approval_store = ApprovalStore(
                str(self.settings.runtime_dir / "approvals.jsonl"),
                clock=self._clock,
                secrets=self._secrets,
            )
        return self._approval_store

    def outbox(self) -> Any:
        if self._outbox is None:
            from pq_agent.outbox import OutboxStore

            self._outbox = OutboxStore(
                str(self.settings.runtime_dir / "outbox.jsonl"),
                clock=self._clock,
                secrets=self._secrets,
            )
        return self._outbox

    def tracer(self) -> Any:
        return self._tracer

    def deps(self) -> GraphDeps:
        """图的依赖。tools 只能是角色视图 —— 这条是角色隔离「进入主图」的唯一证据。

        deps() 是 build_graph 的唯一入口，所以在这一行把裸注册表换成角色视图，
        就没有任何一条执行路径能带着全量权限跑起来。
        """
        return GraphDeps(
            provider=self.provider(),
            tools=self.tools(),
            retriever=self.retriever(),
            intent_model=self.intent_model(),
            clock=self._clock,
            tracer=self.tracer(),
            plan_builder=self._plan_builder,
            approval_store=self.approval_store(),
            outbox=self.outbox(),
        )

    def _app(self, saver: Any) -> Any:
        # 守卫本身交给 build_graph：传 saver.inner 会让 GuardedSqliteSaver.put 退回摆设（A6-04）
        return build_graph(self.deps(), checkpointer=saver)

    def model(self) -> str:
        return "demo_offline" if self.settings.demo else "replay"

    # ---- 会话 --------------------------------------------------------
    def create_session(self, actor: str = "local") -> Session:
        return self.sessions.create(actor)

    def require_session(self, session: Session | None) -> Session:
        if session is None:
            raise ApiError(401, ERROR_SESSION_MISSING, "缺少会话 cookie，请先打开演示页")
        return session

    def require_thread(self, session: Session, thread_id: str) -> RunRecord | None:
        owner = self.sessions.owner_of(thread_id)
        if owner is None:
            raise ApiError(404, ERROR_THREAD_NOT_FOUND, "线程不存在：" + str(thread_id))
        if owner != session.session_id:
            raise ApiError(403, ERROR_THREAD_FORBIDDEN, "该会话无权访问线程 " + str(thread_id))
        return self.runs.get_thread(thread_id)

    # ---- 运行 --------------------------------------------------------
    @staticmethod
    def _checked_slots(slots: Mapping[str, Any] | None) -> dict[str, Any]:
        """把 HTTP 送来的槽位过一遍意图层同一套白名单，非法值直接 400。

        复用 intent_schema 的校验器，而不是在服务层再写一份：
        两处规则一旦分叉，界面上能选的时段和工具真正接受的时段就会对不上。
        """
        if slots is None:
            return {}
        from pq_agent.intent_schema import IntentError, validate_slots

        try:
            return dict(validate_slots(dict(slots), where="slots"))
        except IntentError as exc:
            raise ApiError(400, ERROR_QUERY_INVALID, str(exc)) from None

    def start_run(
        self,
        session: Session,
        *,
        query: str,
        thread_id: str | None = None,
        as_of: str | None = None,
        slots: Mapping[str, Any] | None = None,
    ) -> RunRecord:
        text = str(query or "").strip()
        if not text:
            raise ApiError(400, ERROR_QUERY_INVALID, "query 不能为空")
        if len(text) > int(self.settings.max_query_chars):
            raise ApiError(413, ERROR_QUERY_TOO_LONG, "query 超过 " + str(self.settings.max_query_chars) + " 字")
        tid = str(thread_id or "").strip()
        if tid:
            if len(tid) > 64 or any(ch in tid for ch in ("/", chr(92), " ", "..")):
                raise ApiError(400, ERROR_THREAD_NOT_FOUND, "thread_id 非法：" + tid)
        else:
            tid = session.actor + "-" + uuid.uuid4().hex[:10]
        self.sessions.bind(session.session_id, tid)
        date_text = str(as_of or self.demo_as_of.isoformat())
        try:
            dt.date.fromisoformat(date_text)
        except ValueError:
            raise ApiError(400, ERROR_QUERY_INVALID, "as_of 必须是 YYYY-MM-DD") from None
        slots_value = self._checked_slots(slots)
        moment = self.now()
        record = RunRecord(
            run_id="r" + uuid.uuid4().hex[:12],
            thread_id=tid,
            session_id=session.session_id,
            actor=session.actor,
            query=text,
            mode=self.model(),
            as_of=date_text,
            slots=slots_value,
            demo=bool(self.settings.demo),
            trace_id="tr" + uuid.uuid4().hex[:16],
            started_at=_now_iso(moment),
            updated_at=_now_iso(moment),
        )
        self.runs.start(record)
        self.cost.requests += 1
        return record

    def _state_for(self, run: RunRecord) -> dict[str, Any]:
        state = new_state(
            thread_id=run.thread_id,
            mode=run.mode,
            user_query=run.query,
            as_of=run.as_of,
            slots=run.slots,
            trace_id=run.trace_id,
            request_id="req-" + run.run_id,
            now=self.now(),
        )
        validate_state(state, require_startable=True)
        return dict(state)

    @staticmethod
    def _interrupt_value(update: Any) -> dict[str, Any]:
        items = list(update) if isinstance(update, (list, tuple)) else [update]
        for item in items:
            value = getattr(item, "value", None)
            if isinstance(value, Mapping):
                return dict(value)
        return {}

    @staticmethod
    def _error_code_of(text: str) -> str:
        body = str(text).split("|")[-1].strip()
        head = body.split(":")[0].strip()
        return head if head else ""

    def _note_errors(self, run: RunRecord, body: Mapping[str, Any]) -> None:
        for item in (body.get("errors") or []):
            code = self._error_code_of(str(item))
            if code and code not in run.error_codes:
                run.error_codes.append(code)

    def _translate(self, run: RunRecord, chunk: Any) -> list[tuple[str, dict[str, Any]]]:
        """把 LangGraph 的 updates 分片翻译成前端能懂的事件。"""
        events: list[tuple[str, dict[str, Any]]] = []
        if not isinstance(chunk, Mapping):
            return events
        for node, update in chunk.items():
            if str(node) == "__interrupt__":
                value = self._interrupt_value(update)
                run.approval_requests += 1
                run.status = "awaiting_approval"
                events.append(("approval_required", {
                    "approval_id": value.get("approval_id"),
                    "thread_id": value.get("thread_id"),
                    "call_id": value.get("call_id"),
                    "kind": value.get("kind"),
                    "dry_run": value.get("dry_run"),
                    "tool_name": value.get("tool_name"),
                    "capability": value.get("capability"),
                    "action_hash": value.get("action_hash"),
                    "arguments_hash": value.get("arguments_hash"),
                    "arguments_preview": value.get("arguments_preview"),
                    "risk_note": value.get("risk_note"),
                    "expires_at": value.get("expires_at"),
                    "question": value.get("question"),
                }))
                continue
            body: dict[str, Any] = dict(update) if isinstance(update, Mapping) else {}
            name = str(node)
            run.nodes.append(name)
            self._note_errors(run, body)
            if name == "classify_intent":
                run.intent = str(body.get("intent") or run.intent)
                run.intent_source = str(body.get("intent_source") or run.intent_source)
                events.append(("intent", {"intent": run.intent, "source": run.intent_source}))
            if name == "check_inputs" and body.get("data_snapshot_id"):
                run.data_snapshot_id = str(body.get("data_snapshot_id"))
            if name == "execute_tool":
                results = list(body.get("tool_results") or [])
                if results:
                    item = dict(results[-1]) if isinstance(results[-1], Mapping) else {}
                    run.tool_calls += 1
                    events.append(("tool", {
                        "tool_name": item.get("tool_name"),
                        "ok": bool(item.get("ok")),
                        "error_code": item.get("error_code"),
                        "duration_ms": item.get("duration_ms"),
                        "summary": str(item.get("summary") or "")[:200],
                    }))
            if name == "request_approval":
                decisions = list(body.get("approvals") or [])
                last = dict(decisions[-1]) if decisions and isinstance(decisions[-1], Mapping) else {}
                events.append(("approval", {
                    "tool_name": last.get("tool_name"),
                    "status": last.get("status"),
                    "reason": str(last.get("reason") or "")[:200],
                }))
            events.append(("node", {"node": name, "fields": sorted(body.keys())}))
        return events

    def _snapshot_of(self, app: Any, run: RunRecord) -> dict[str, Any]:
        config = thread_config(run.thread_id)
        snapshot = app.get_state(config)
        values = dict(snapshot.values or {})
        interrupts: list[Any] = []
        for task in (snapshot.tasks or ()):
            interrupts.extend(list(getattr(task, "interrupts", ()) or ()))
        return {
            "values": values,
            "next_nodes": [str(item) for item in (snapshot.next or ())],
            "interrupts": interrupts,
        }

    def _apply_snapshot(self, run: RunRecord, snapshot: Mapping[str, Any]) -> None:
        values = dict(snapshot.get("values") or {})
        run.next_nodes = list(snapshot.get("next_nodes") or [])
        run.cursor = int(values.get("cursor") or 0)
        run.intent = str(values.get("intent") or run.intent)
        run.intent_source = str(values.get("intent_source") or run.intent_source)
        run.data_snapshot_id = str(values.get("data_snapshot_id") or run.data_snapshot_id)
        run.answer_preview = str(values.get("final_answer") or "")[:300]
        self._note_errors(run, values)
        if snapshot.get("interrupts"):
            run.status = "awaiting_approval"
        elif run.status == "running":
            run.status = "finished"
        self.runs.touch(run)

    @contextlib.contextmanager
    def _run_scope(self, run: RunRecord, *, node_label: str = "") -> Any:
        """一次运行的根 span。没有 tracer 时返回一个什么都不做的空 scope。

        为什么要它：A1-06 只埋了 graph.node 一层，整棵调用树没有根，
        「这次运行总共多久、跑在哪个 thread」只能靠外部计时器拼。
        graph.run 把一次运行的全部 span 挂在同一个父节点下。

        进入 span 时顺手记下 OTel 自己的 trace_id：事后要按 trace 取 span，
        只能用这个 id；本服务的运行编号和它毫无关系。
        """
        tracer = getattr(self, "_tracer", None)
        if tracer is None:
            yield None
            return
        attributes: dict[str, Any] = {
            "thread_id": str(run.thread_id),
            "agent.mode": str(run.mode),
            "as_of": str(run.as_of),
        }
        if node_label:
            attributes["agent.node"] = str(node_label)
        with tracer.start_as_current_span(SPAN_RUN, attributes=attributes) as span:
            context = span.get_span_context()
            raw = int(getattr(context, "trace_id", 0) or 0)
            run.otel_trace_id = format(raw, "032x") if raw else ""
            yield span

    def _flush_spans(self) -> None:
        """把批处理里的 span 逼出来，再统计。

        批处理导出器是异步的：不 flush 就直接读 recorder，会把
        「还没导出」误报成「这次运行一个 span 都没有」。
        flush 失败不抛：观测层不许把主流程带崩。
        """
        try:
            from pq_agent.observability import current_tracing

            state = current_tracing()
            provider = getattr(state, "provider", None)
            if provider is not None:
                provider.force_flush()
        except Exception:
            return

    def _telemetry_exporter(self) -> str:
        """当前导出去向的名字。取不到就写 none，不假装有 Collector。"""
        try:
            from pq_agent.observability import current_tracing

            state = current_tracing()
            status = getattr(state, "status", None)
            return str(getattr(status, "exporter", "") or "none")
        except Exception:
            return "none"

    def _telemetry_degraded(self) -> bool:
        """取不到观测状态时按「已降级」算：宁可说没接上，也不说一切正常。"""
        try:
            from pq_agent.observability import current_tracing

            state = current_tracing()
            status = getattr(state, "status", None)
            return bool(getattr(status, "degraded", True))
        except Exception:
            return True
    def _spans_for(self, *, thread_id: str, otel_trace_id: str) -> list[dict[str, Any]]:
        """按 OTel trace_id 取 span。没有 recorder 或没有 trace_id 就返回空列表。

        必须用 OTel 的 trace_id 过滤：本服务的 trace_id 是自己的运行编号，
        和 span 上那个 32 位十六进制 id 不是同一个东西，用它过滤永远是空。
        """
        recorder = self._trace_recorder
        if recorder is None or not str(otel_trace_id or ""):
            return []
        try:
            return list(recorder.for_thread(thread_id, trace_id=str(otel_trace_id)))
        except Exception:
            return []

    def _recorded_spans(self, run: RunRecord) -> list[dict[str, Any]]:
        """取出这条运行的 span。没有采录就返回空列表——不编造 span。"""
        return self._spans_for(thread_id=run.thread_id, otel_trace_id=run.otel_trace_id)

    def _resume_links(self, thread_id: str, trace_id: str) -> list[Any]:
        """恢复运行指向「上一次中断的那条 trace」。首次运行不建链接。

        没有这一步，SkyWalking 里两段 trace 长得一模一样，
        没人能回答「这次恢复到底接着哪次中断跑的」。
        """
        prior = self.trace_index.latest_for_thread(thread_id)
        if prior is None or str(prior.trace_id) == str(trace_id):
            return []
        from pq_agent.observability.apm_traces import resume_links

        return list(
            resume_links(
                thread_id=str(thread_id),
                new_trace_id=str(trace_id),
                prior_trace_ids=(prior.trace_id,),
            )
        )

    def _record_trace(self, run: RunRecord, *, duration_ms: int) -> TraceSummary:
        degraded = False
        reasons: list[str] = []
        try:
            from pq_agent.observability import current_tracing

            state = current_tracing()
            if state is not None:
                degraded = bool(state.status.degraded)
                reasons = list(state.status.reasons)
        except Exception:
            degraded = True
            reasons = ["tracing_unavailable"]
        self._flush_spans()
        summary = TraceSummary(
            trace_id=run.trace_id,
            otel_trace_id=run.otel_trace_id,
            intent=run.intent,
            run_id=run.run_id,
            thread_id=run.thread_id,
            mode=run.mode,
            as_of=run.as_of,
            node_sequence=list(run.nodes),
            span_count=len(self._recorded_spans(run)),
            duration_ms=int(duration_ms),
            error_codes=list(run.error_codes),
            degraded=degraded,
            degraded_reasons=reasons,
            created_at=_now_iso(self.now()),
        )
        self._record_apm(run)
        return self.trace_index.add(summary)

    def _record_apm(self, run: RunRecord) -> list[dict[str, Any]]:
        """把这次运行的 span 变成指标、判定告警、写成一份可回看的快照。

        失败一律吞掉：观测层不许把主流程带崩。这里失败只意味着
        「这次没有 APM 快照」，不意味着运行失败。
        """
        spans = self._recorded_spans(run)
        if not spans or self._trace_recorder is None:
            return []
        try:
            from pq_agent.observability.apm_alerts import evaluate_alerts
            from pq_agent.observability.apm_metrics import collect_run_metrics
            from pq_agent.observability.apm_traces import run_facts_from_spans

            # 指标的事实来自真 span（节点耗时、工具结果、沙箱结局），
            # 意图与来源来自运行登记。两处都不是在这里猜出来的。
            facts = run_facts_from_spans(
                spans,
                intent=run.intent,
                intent_source=run.intent_source,
                mode=run.mode,
                exporter=self._telemetry_exporter(),
                telemetry_degraded=self._telemetry_degraded(),
                telemetry_dropped=int(getattr(self._trace_recorder, "dropped", 0) or 0),
            )
            batch = collect_run_metrics(**facts)
            previous = self._load_apm_snapshot()
            run_index = int(previous.get("run_index") or 0) + 1
            evaluation = evaluate_alerts(
                batch,
                previous=self._load_alert_states(previous),
                run_index=run_index,
            )
            payload = {
                "schema_version": APM_ENVIRONMENT_SCHEMA_VERSION,
                "run_index": run_index,
                "run_id": run.run_id,
                "trace_id": run.trace_id,
                "otel_trace_id": run.otel_trace_id,
                "thread_id": run.thread_id,
                "metrics": batch.to_dict(),
                "alerts": evaluation.to_dict(),
                "recorded_spans": len(spans),
            }
            self.apm_environment_path.parent.mkdir(parents=True, exist_ok=True)
            self.apm_environment_path.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
                encoding="utf-8",
            )
            return list(evaluation.firing)
        except Exception:
            return []

    def _load_apm_snapshot(self) -> dict[str, Any]:
        """读回上一次的 APM 快照。读不到就返回空字典，不编一份出来。"""
        try:
            raw = json.loads(self.apm_environment_path.read_text(encoding="utf-8"))
            return dict(raw) if isinstance(raw, dict) else {}
        except Exception:
            return {}

    def _load_alert_states(self, snapshot: Mapping[str, Any]) -> list[Any]:
        """从上一份快照还原告警状态，用来判「连续几轮满足」与「是否恢复」。"""

        """告警状态是跨运行累积的：丢了它，每次运行都会把「已经响了三轮」重新算成第一轮，
        连续满足次数的规则就永远不会触发。读不回来就返回空列表——宁可从头计数，
        也不凭空说「上一轮也在响」。"""
        try:
            from pq_agent.observability.apm_alerts import AlertState

            rows = list(dict(snapshot.get("alerts") or {}).get("states") or [])
            states: list[Any] = []
            for row in rows:
                item = dict(row)
                states.append(
                    AlertState(
                        key=str(item.get("key") or ""),
                        status=str(item.get("status") or ""),
                        value=float(item.get("value") or 0.0),
                        threshold=float(item.get("threshold") or 0.0),
                        severity=str(item.get("severity") or ""),
                        summary=str(item.get("summary") or ""),
                        consecutive=int(item.get("consecutive") or 0),
                        evidence=tuple(str(x) for x in (item.get("evidence") or ())),
                    )
                )
            return states
        except Exception:
            return []

    async def stream_events(self, run: RunRecord, *, is_disconnected: Any = None):
        """把一次 graph 运行变成事件流。断线只标记，不假装任务被回滚。

        为什么用线程 + 队列：锁定版 SqliteSaver 不支持 async，app.astream 直接抛
        NotImplementedError。宁可多一层桥接，也不为了写法好看去引 aiosqlite。
        """
        from pq_agent.checkpoint import guarded_sqlite_checkpointer

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        cancel = threading.Event()
        started = self.now()
        yield ("meta", {
            "run_id": run.run_id,
            "thread_id": run.thread_id,
            "trace_id": run.trace_id,
            "mode": run.mode,
            "as_of": run.as_of,
            "demo": bool(run.demo),
            "banner": DEMO_BANNER if run.demo else "",
        })
        with guarded_sqlite_checkpointer(self.checkpoint_path) as saver:
            app = self._app(saver)
            state = self._state_for(run)
            config = thread_config(run.thread_id)

            def worker() -> None:
                # 路由函数（例如角色守卫）在条件边里拿不到 deps，靠这个 contextvar 拿到工具对象。
                # 必须在工作线程里设置：contextvar 不跨线程继承，裸 invoke 时又没人设它。
                from pq_agent.graph import _ROUTING_TOOLS

                _ROUTING_TOOLS.set(getattr(app, "_pq_tools", None))
                try:
                    with self._run_scope(run):
                        for chunk in app.stream(state, config, stream_mode="updates"):
                            if cancel.is_set():
                                break
                            loop.call_soon_threadsafe(queue.put_nowait, ("chunk", chunk))
                except BaseException as exc:  # noqa: BLE001 - 统一转成事件，绝不让线程静默死掉
                    loop.call_soon_threadsafe(queue.put_nowait, ("error", exc))
                finally:
                    loop.call_soon_threadsafe(queue.put_nowait, ("end", None))
            thread = threading.Thread(target=worker, name="pq-agent-stream", daemon=True)
            thread.start()
            try:
                while True:
                    kind, payload = await queue.get()
                    if kind == "end":
                        break
                    if kind == "error":
                        run.status = "failed"
                        code = type(payload).__name__
                        if code not in run.error_codes:
                            run.error_codes.append(code)
                        yield ("error", {"error_code": ERROR_RUN_FAILED, "exception": code})
                        break
                    for event in self._translate(run, payload):
                        yield event
                    if is_disconnected is not None:
                        try:
                            gone = bool(await is_disconnected())
                        except Exception:
                            gone = False
                        if gone:
                            cancel.set()
                            run.client_disconnected = True
                            if run.status == "running":
                                run.status = "aborted"
                            yield ("aborted", {"reason": "client_disconnected", "thread_id": run.thread_id})
                            break
            finally:
                cancel.set()
                thread.join(timeout=5.0)
            snapshot = self._snapshot_of(app, run)
            self._apply_snapshot(run, snapshot)
            duration_ms = max(0, int((self.now() - started).total_seconds() * 1000))
            summary = self._record_trace(run, duration_ms=duration_ms)
            yield ("state", {"thread_id": run.thread_id, "status": run.status, "next_nodes": run.next_nodes, "error_codes": run.error_codes})
            yield ("done", {"status": run.status, "trace_id": summary.trace_id, "duration_ms": duration_ms, "answer": run.answer_preview})

    # ---- 查询与审批 --------------------------------------------------
    def graph_state(self, session: Session, thread_id: str) -> dict[str, Any]:
        from pq_agent.checkpoint import guarded_sqlite_checkpointer

        run = self.require_thread(session, thread_id)
        payload: dict[str, Any] = {"ok": True, "run": run.to_dict() if run is not None else None}
        with guarded_sqlite_checkpointer(self.checkpoint_path) as saver:
            app = self._app(saver)
            snapshot = app.get_state(thread_config(thread_id))
            values = dict(snapshot.values or {})
            interrupts: list[Any] = []
            for task in (snapshot.tasks or ()):
                interrupts.extend(list(getattr(task, "interrupts", ()) or ()))
            pending = dict(interrupts[0].value) if interrupts else None
            payload["graph"] = {
                "next_nodes": [str(item) for item in (snapshot.next or ())],
                "intent": str(values.get("intent") or ""),
                "cursor": int(values.get("cursor") or 0),
                "data_snapshot_id": str(values.get("data_snapshot_id") or ""),
                "errors": [str(item)[:200] for item in (values.get("errors") or [])],
                "messages": [dict(item) for item in (values.get("messages") or [])][-8:],
                "citations": [dict(item) for item in (values.get("citations") or [])][-8:],
                "final_answer": str(values.get("final_answer") or ""),
                "pending_approval": pending,
                "awaiting_approval": bool(interrupts),
            }
        return self._guard_out(payload, where="api.state")

    def pending_for(self, thread_id: str) -> dict[str, Any] | None:
        from pq_agent.checkpoint import guarded_sqlite_checkpointer

        with guarded_sqlite_checkpointer(self.checkpoint_path) as saver:
            app = self._app(saver)
            return pending_interrupt(app, thread_id=thread_id)

    def decide(self, session: Session, *, thread_id: str, decision: Mapping[str, Any]) -> dict[str, Any]:
        from pq_agent.checkpoint import guarded_sqlite_checkpointer

        run = self.require_thread(session, thread_id)
        if run is None:
            raise ApiError(404, ERROR_THREAD_NOT_FOUND, "线程没有运行记录：" + str(thread_id))
        pending = self.pending_for(thread_id)
        if pending is None:
            raise ApiError(409, ERROR_APPROVAL_NOT_PENDING, "该线程当前没有待审批的动作")
        value = dict(pending.get("value") or {})
        approval_id = str(decision.get("approval_id") or "")
        if not approval_id or approval_id != str(value.get("approval_id") or ""):
            raise ApiError(409, ERROR_APPROVAL_STALE, "审批已失效：图中当前等待的是 " + str(value.get("approval_id")))
        for field in ("action_hash", "arguments_hash"):
            offered = decision.get(field)
            expected = value.get(field)
            if offered is not None and str(offered) != str(expected):
                raise ApiError(409, ERROR_APPROVAL_STALE, "审批绑定的 " + field + " 与当前动作不一致")
        if approval_id in run.decided_approvals:
            raise ApiError(409, ERROR_APPROVAL_DUPLICATE, "该审批已经处理过：" + approval_id)
        record = self.approval_store().get(approval_id)
        if record is not None and record.status != "pending":
            raise ApiError(409, ERROR_APPROVAL_DUPLICATE, "该审批已经处理过：" + approval_id)
        payload = {
            "approved": bool(decision.get("approved")),
            "reason": str(decision.get("reason") or "")[:200],
            "actor": session.actor,
            "approval_id": approval_id,
            "action_hash": str(value.get("action_hash") or ""),
            "arguments_hash": str(value.get("arguments_hash") or ""),
        }
        run.decided_approvals.append(approval_id)
        self.cost.approval_decisions += 1
        started = self.now()
        with guarded_sqlite_checkpointer(self.checkpoint_path) as saver:
            app = self._app(saver)
            with self._run_scope(run, node_label="approve"):
                out, interrupted = resume_graph(app, payload, thread_id=thread_id)
            snapshot = self._snapshot_of(app, run)
        run.status = "awaiting_approval" if interrupted else "finished"
        self._apply_snapshot(run, snapshot)
        if not interrupted:
            run.status = "finished"
        duration_ms = max(0, int((self.now() - started).total_seconds() * 1000))
        self._record_trace(run, duration_ms=duration_ms)
        result = {
            "ok": True,
            "thread_id": thread_id,
            "approval_id": approval_id,
            "approved": bool(payload["approved"]),
            "interrupted": bool(interrupted),
            "status": run.status,
            "next_nodes": list(run.next_nodes),
            "error_codes": list(run.error_codes),
            "answer": str(out.get("final_answer") or "") if isinstance(out, Mapping) else "",
            "state": run.to_dict(),
        }
        return self._guard_out(result, where="api.approve")

    def trace_payload(self, trace_id: str) -> dict[str, Any]:
        summary = self.trace_index.get(trace_id)
        if summary is None:
            raise ApiError(404, ERROR_TRACE_NOT_FOUND, "未知 trace：" + str(trace_id))
        return self._guard_out(
            {"ok": True, "trace": summary.to_dict()},
            where="api.trace",
        )

    def trace_detail_payload(self, trace_id: str) -> dict[str, Any]:
        """trace 明细：拓扑 / 瀑布 / 日志关联 / 指标 / 告警 / 恢复链接。

        没有 recorder 时返回 detail=None 并说明原因，而不是给一棵空树——
        空树会让人以为「这次运行确实一个 span 都没有」，那是撒谎。
        """
        summary = self.trace_index.get(trace_id)
        if summary is None:
            raise ApiError(404, ERROR_TRACE_NOT_FOUND, "未知 trace：" + str(trace_id))
        if self._trace_recorder is None:
            return self._guard_out(
                {
                    "ok": True,
                    "trace": summary.to_dict(),
                    "detail": None,
                    "detail_reason": "no_trace_recorder",
                },
                where="api.trace_detail",
            )
        from pq_agent.observability.apm_alerts import evaluate_alerts
        from pq_agent.observability.apm_metrics import collect_run_metrics
        from pq_agent.observability.apm_traces import build_trace_detail, run_facts_from_spans

        spans = self._spans_for(thread_id=summary.thread_id, otel_trace_id=summary.otel_trace_id)
        # 明细页必须和写快照时用同一套事实来源，否则「页面上看到的指标」与
        # 「落盘快照里的指标」会不一致，而这种不一致最难查。
        previous = self._load_apm_snapshot()
        batch = collect_run_metrics(
            **run_facts_from_spans(
                spans,
                intent=str(summary.intent or ""),
                intent_source="",
                mode=str(summary.mode or ""),
                exporter=self._telemetry_exporter(),
                telemetry_degraded=self._telemetry_degraded(),
                telemetry_dropped=int(getattr(self._trace_recorder, "dropped", 0) or 0),
            )
        )
        evaluation = evaluate_alerts(
            batch,
            previous=self._load_alert_states(previous),
            run_index=int(previous.get("run_index") or 0),
        )
        detail = build_trace_detail(
            trace_id=str(trace_id),
            otel_trace_id=str(summary.otel_trace_id or ""),
            thread_id=summary.thread_id,
            run_id=summary.run_id,
            spans=spans,
            metrics=batch,
            alerts=evaluation,
            links=tuple(self._resume_links(summary.thread_id, trace_id)),
            secrets=self._secrets,
            recorder_dropped=int(getattr(self._trace_recorder, "dropped", 0)),
        )
        return self._guard_out(
            {"ok": True, "trace": summary.to_dict(), "detail": detail.to_dict()},
            where="api.trace_detail",
        )

    def apm_payload(self) -> dict[str, Any]:
        """当前 APM 采录状态：采录开关、已记录 span 数、最近一次快照位置。

        本机没有 Collector，所以这里只会报「采录中 / 未采录」，
        不会报「APM 平台正常」——那需要真机上 Docker 才算数。
        """
        recorder = self._trace_recorder
        payload: dict[str, Any] = {
            "recording": recorder is not None,
            "recorded_spans": int(len(getattr(recorder, "spans", ()) or ())),
            "dropped_spans": int(getattr(recorder, "dropped", 0) or 0),
            "snapshot_path": str(self.apm_environment_path),
            "snapshot_exists": bool(self.apm_environment_path.exists()),
            "collector": "unavailable_on_this_host",
        }
        if payload["snapshot_exists"]:
            try:
                raw = json.loads(self.apm_environment_path.read_text(encoding="utf-8"))
                payload["last_run_id"] = str(raw.get("run_id") or "")
                payload["last_trace_id"] = str(raw.get("trace_id") or "")
                payload["last_otel_trace_id"] = str(raw.get("otel_trace_id") or "")
                payload["firing_alerts"] = list(
                    dict(raw.get("alerts") or {}).get("firing") or []
                )
            except Exception:
                payload["snapshot_readable"] = False
        return payload

    def cost_payload(self) -> dict[str, Any]:
        return {"ok": True, "cost": self.cost.to_dict()}

    def doctor(self) -> dict[str, Any]:
        from pq_agent.checkpoint import checkpointer_driver_available

        checks: list[dict[str, Any]] = []
        free, detail = check_port_free(self.settings.host, self.settings.port)
        checks.append({"name": "port", "ok": bool(free), "detail": detail})
        driver, driver_detail = checkpointer_driver_available()
        checks.append({"name": "checkpoint_driver", "ok": bool(driver), "detail": driver_detail or "ok"})
        manifest = self.settings.repo_root / "tests" / "fixtures" / "manifest.json"
        checks.append({
            "name": "demo_fixture",
            "ok": bool(manifest.exists()),
            "detail": str(manifest) if manifest.exists() else "缺少 " + str(manifest),
        })
        degraded = False
        reasons: list[str] = []
        try:
            from pq_agent.observability import current_tracing

            state = current_tracing()
            if state is not None:
                degraded = bool(state.status.degraded)
                reasons = list(state.status.reasons)
        except Exception:
            reasons = ["tracing_unavailable"]
            degraded = True
        checks.append({
            "name": "tracing",
            "ok": True,
            "detail": "降级：" + ",".join(reasons) if degraded else "已配置 exporter",
            "degraded": bool(degraded),
        })
        # 角色隔离的体检项：确认服务真正拿在手上的是一份角色视图，而不是全量注册表。
        # 这一项要能自检失败，否则「角色已经接入」只能靠读代码相信。
        view = self.tools()
        raw = self.raw_registry()
        total = len(raw.names()) if hasattr(raw, "names") else -1
        view_ok = isinstance(view, RoleRegistry) and getattr(view, "_registry", None) is raw
        detail = "角色 %s：可见 %d / 全量 %d 个工具" % (
            self._role,
            len(view.names()) if hasattr(view, "names") else -1,
            total,
        )
        checks.append({"name": "role_isolation", "ok": bool(view_ok), "detail": detail})
        return {
            "ok": all(item["ok"] for item in checks),
            "settings": self.settings.public_dict(),
            "checks": checks,
        }

    def _guard_out(self, payload: Any, *, where: str) -> Any:
        """API 出口也过一遍脱敏闸门：宁可 500，也不把未脱敏内容发给前端。"""
        from pq_agent.security.redaction import guard_payload

        try:
            return guard_payload(payload, secrets=self._secrets, where=where)
        except Exception as exc:
            raise ApiError(500, ERROR_RUN_FAILED, "响应未通过脱敏闸门：" + type(exc).__name__) from exc


def build_demo_service(settings: ServerSettings, **kwargs: Any) -> AgentService:
    """装配一个可直接跑的演示服务：demo_offline + fixture + 无外发通道。"""
    from pq_agent.observability import LocalEventLog, configure_tracing
    from pq_agent.security.redaction import collect_secrets_from_environ

    secrets = tuple(collect_secrets_from_environ())
    event_log = LocalEventLog(
        settings.runtime_dir / "telemetry.jsonl",
        secrets=secrets,
    )
    from pq_agent.observability.apm_traces import TraceRecordingExporter

    recorder = TraceRecordingExporter()
    configure_tracing(
        exporter=recorder,
        exporter_kind="recording",
        event_log=event_log,
        secrets=secrets,
    )
    tracer = None
    try:
        from pq_agent.observability import get_tracer

        tracer = get_tracer()
    except Exception:
        tracer = None
    kwargs.setdefault("sender", None)
    kwargs.setdefault("secrets", secrets)
    # recorder 必须传进 service：只把它交给 configure_tracing 的话，
    # /api/trace 的 span_count 与 APM 快照都会是空的，而界面看起来一切正常。
    kwargs.setdefault("trace_recorder", recorder)
    service = AgentService(settings, tracer=tracer, **kwargs)
    service.event_log = event_log
    return service

def role_scoped_service(
    settings: ServerSettings,
    role: str = ROLE_SUPERVISOR,
    *,
    build: Any = None,
    **kwargs: Any,
) -> AgentService:
    """按指定角色装配服务。角色写进设置里，装配与对外摘要用同一个值。

    build 默认是 build_demo_service，测试可以换成别的装配函数；
    显式传入而不是在内部 if/else，是为了让「装配方式」和「角色」两件事互不牵连。
    """
    scoped = dataclasses.replace(settings, role=str(role))
    factory = build if build is not None else build_demo_service
    return factory(scoped, **kwargs)


def _build_injected_service(settings: ServerSettings, provider_name: str) -> AgentService:
    """非演示模式的装配入口：provider 由调用方指定，缺真实依赖就报错。"""
    from pq_agent.data.provider import create_provider

    try:
        provider = create_provider(provider_name, clock=None)
    except Exception as exc:  # noqa: BLE001 - 缺真实数据源就是启动失败，不兜底
        raise ContractError("PROVIDER_REQUIRED", "无法构造 provider %s：%s: %s" % (provider_name, type(exc).__name__, exc)) from exc
    return AgentService(settings, provider=provider, sender=None, secrets=())


def format_sse(event: str, payload: Any) -> str:
    """SSE 帧：一行 event，一行 data，空行结束。"""
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return "event: " + str(event) + chr(10) + "data: " + body + chr(10) + chr(10)


def _web_dir() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent / "web"


def build_index_html(*, demo: bool) -> str:
    """把 index.html / style.css / app.js 合成单页返回。

    为什么内联：静态文件用的是普通 <script src>，浏览器不会带自定义头，
    只能靠 cookie 保护；内联后整页仍由 token 保护，少一个鉴权面。
    """
    base = _web_dir()
    html = (base / "index.html").read_text(encoding="utf-8")
    style = (base / "style.css").read_text(encoding="utf-8")
    script = (base / "app.js").read_text(encoding="utf-8")
    html = html.replace("/*__STYLE__*/", style)
    html = html.replace("/*__APP_JS__*/", script)
    html = html.replace("__BANNER_TEXT__", DEMO_BANNER if demo else "本地 replay 模式：数据来自固定日期的本地快照")
    html = html.replace("__DEMO_FLAG__", "true" if demo else "false")
    return html


def create_app(service: AgentService, settings: ServerSettings | None = None) -> Any:
    """构造 FastAPI 应用。所有 endpoint 走同一套鉴权。"""
    # FastAPI 的类型在模块级导入：本文件启用了 from __future__ import annotations，
    # 注解是字符串，FastAPI 只在模块全局命名空间里解析，写在函数内会让 Request 被当成 query 参数。

    resolved = settings or service.settings
    app = FastAPI(title="pq_agent local demo", docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(ApiError)
    async def _api_error_handler(request: Request, exc: ApiError):  # noqa: ARG001
        return JSONResponse(status_code=exc.status, content=exc.to_dict())

    @app.exception_handler(ContractError)
    async def _contract_error_handler(request: Request, exc: ContractError):  # noqa: ARG001
        code = str(getattr(exc, "code", "CONTRACT_ERROR"))
        return JSONResponse(status_code=400, content={"ok": False, "error_code": code, "message": str(exc)[:300]})

    def _hostname_of(value: str) -> str:
        text = str(value or "").strip()
        if text.startswith("["):
            return text.split("]")[0].lstrip("[")
        return text.split(":")[0]

    def _check_host(request: Request) -> None:
        host = request.headers.get("host") or ""
        if _hostname_of(host) not in ALLOWED_HOSTNAMES:
            raise ApiError(403, ERROR_HOST_DENIED, "Host 头不是本机地址：" + host)

    def _check_origin(request: Request) -> None:
        origin = request.headers.get("origin")
        if not origin:
            return
        target = str(origin).split("//")[-1]
        if _hostname_of(target) not in ALLOWED_HOSTNAMES:
            raise ApiError(403, ERROR_ORIGIN_DENIED, "Origin 不是本机地址：" + str(origin))

    def _check_body_size(request: Request) -> None:
        declared = request.headers.get("content-length")
        if declared is None:
            return
        try:
            size = int(declared)
        except ValueError:
            raise ApiError(400, ERROR_BODY_TOO_LARGE, "Content-Length 非法") from None
        if size > int(resolved.body_limit_bytes):
            raise ApiError(413, ERROR_BODY_TOO_LARGE, "请求体超过 " + str(resolved.body_limit_bytes) + " 字节")

    def _presented_token(request: Request) -> str:
        header = request.headers.get(TOKEN_HEADER)
        if header:
            return str(header)
        if resolved.allow_query_token:
            return str(request.query_params.get("token") or "")
        return ""

    def _guard(request: Request, *, mutating: bool) -> None:
        _check_host(request)
        if mutating and not request.headers.get(TOKEN_HEADER):
            raise ApiError(
                403,
                ERROR_CSRF_HEADER_REQUIRED,
                "写操作必须在 " + TOKEN_HEADER + " 请求头里携带 token；跨站表单无法带上自定义头",
            )
        token = _presented_token(request)
        if not token:
            raise ApiError(401, ERROR_TOKEN_MISSING, "缺少 " + TOKEN_HEADER)
        if not hmac.compare_digest(token, str(resolved.token)):
            raise ApiError(401, ERROR_TOKEN_INVALID, "token 不正确")
        _check_origin(request)
        _check_body_size(request)

    async def _read_json(request: Request) -> dict[str, Any]:
        raw = await request.body()
        if len(raw) > int(resolved.body_limit_bytes):
            raise ApiError(413, ERROR_BODY_TOO_LARGE, "请求体超过 " + str(resolved.body_limit_bytes) + " 字节")
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            raise ApiError(400, ERROR_QUERY_INVALID, "请求体不是合法 JSON") from None
        if not isinstance(data, dict):
            raise ApiError(400, ERROR_QUERY_INVALID, "请求体必须是 JSON 对象")
        return data

    def _session_of(request: Request) -> Session:
        return service.sessions.get(request.cookies.get(SESSION_COOKIE)) or service.create_session("local")

    def _attach_cookie(response: Any, session: Session) -> Any:
        response.set_cookie(
            SESSION_COOKIE,
            session.session_id,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return response

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        _guard(request, mutating=False)
        session = _session_of(request)
        html = build_index_html(demo=bool(resolved.demo))
        return _attach_cookie(HTMLResponse(content=html), session)

    @app.get("/api/health")
    async def health(request: Request):
        _guard(request, mutating=False)
        report = service.doctor()
        report["tracing"] = getattr(service, "event_log", None) is not None
        return JSONResponse(content=report)

    @app.post("/api/chat")
    async def chat(request: Request):
        _guard(request, mutating=True)
        body = await _read_json(request)
        session = _session_of(request)
        run = service.start_run(
            session,
            query=str(body.get("query") or ""),
            thread_id=str(body.get("thread_id") or "") or None,
            as_of=str(body.get("as_of") or "") or None,
            slots=body.get("slots") if isinstance(body.get("slots"), Mapping) else None,
        )

        async def event_source():
            async for event, payload in service.stream_events(run, is_disconnected=request.is_disconnected):
                yield format_sse(event, payload)

        response = StreamingResponse(
            event_source(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )
        return _attach_cookie(response, session)

    @app.get("/api/state/{thread_id}")
    async def state(request: Request, thread_id: str):
        _guard(request, mutating=False)
        session = _session_of(request)
        payload = service.graph_state(session, thread_id)
        return _attach_cookie(JSONResponse(content=payload), session)

    @app.post("/api/approve")
    async def approve(request: Request):
        _guard(request, mutating=True)
        body = await _read_json(request)
        session = _session_of(request)
        thread_id = str(body.get("thread_id") or "")
        if not thread_id:
            raise ApiError(400, ERROR_QUERY_INVALID, "缺少 thread_id")
        if "approved" not in body or not isinstance(body.get("approved"), bool):
            raise ApiError(400, ERROR_QUERY_INVALID, "approved 必须是布尔值（缺失时按 fail-closed 拒绝）")
        result = await asyncio.to_thread(service.decide, session, thread_id=thread_id, decision=body)
        return _attach_cookie(JSONResponse(content=result), session)

    @app.get("/api/trace/{trace_id}")
    async def trace(request: Request, trace_id: str):
        _guard(request, mutating=False)
        session = _session_of(request)
        summary = service.trace_index.get(trace_id)
        if summary is None:
            raise ApiError(404, ERROR_TRACE_NOT_FOUND, "未知 trace：" + str(trace_id))
        if service.sessions.owner_of(summary.thread_id) not in (None, session.session_id):
            raise ApiError(403, ERROR_THREAD_FORBIDDEN, "该会话无权访问这条 trace")
        return _attach_cookie(JSONResponse(content=service.trace_payload(trace_id)), session)

    @app.get("/api/trace/{trace_id}/detail")
    async def trace_detail(request: Request, trace_id: str):
        _guard(request, mutating=False)
        session = _session_of(request)
        summary = service.trace_index.get(trace_id)
        if summary is None:
            raise ApiError(404, ERROR_TRACE_NOT_FOUND, "未知 trace：" + str(trace_id))
        if service.sessions.owner_of(summary.thread_id) not in (None, session.session_id):
            raise ApiError(403, ERROR_THREAD_FORBIDDEN, "该会话无权访问这条 trace")
        payload = await asyncio.to_thread(service.trace_detail_payload, trace_id)
        return _attach_cookie(JSONResponse(content=payload), session)

    @app.get("/api/apm")
    async def apm(request: Request):
        _guard(request, mutating=False)
        session = _session_of(request)
        payload = {"ok": True, "apm": service.apm_payload()}
        return _attach_cookie(JSONResponse(content=payload), session)

    @app.get("/api/cost")
    async def cost(request: Request):
        _guard(request, mutating=False)
        session = _session_of(request)
        return _attach_cookie(JSONResponse(content=service.cost_payload()), session)

    return app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pq_agent.server",
        description="pq_agent 本地离线演示服务（只监听 127.0.0.1）",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="只允许 127.0.0.1 / localhost / ::1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--token", default="", help="固定 token；默认进程内随机生成，不落盘")
    parser.add_argument("--no-demo", action="store_true", help="关闭演示标记；必须同时给 --provider，否则拒绝启动")
    parser.add_argument("--provider", default="", help="非演示模式的真实数据源（replay 快照或 live 源）")
    parser.add_argument("--runtime-dir", default="", help="运行时目录，默认 .tmp/pq_agent_server")
    parser.add_argument(
        "--role",
        default=ROLE_SUPERVISOR,
        choices=("supervisor", "analyst", "operator"),
        help="服务以哪个角色的工具视图跑：supervisor 只路由（一个工具都调不动），analyst 只读，operator 只写",
    )
    parser.add_argument("--doctor", action="store_true", help="只做自检并输出 JSON，不启动服务")
    parser.add_argument("--print-url", action="store_true", help="打印带 token 的访问地址")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = ServerSettings(
        host=args.host,
        port=args.port,
        demo=not args.no_demo,
        token=args.token,
        runtime_dir=(args.runtime_dir or None),
        mode=(MODE_REPLAY if args.no_demo else MODE_DEMO_OFFLINE),
        role=str(args.role),
    )
    # 非演示模式必须显式注入真实 provider：缺依赖时直接失败，
    # 不允许悄悄用 build_demo_service 的 fixture 顶上去。
    if args.no_demo:
        if not args.provider:
            print("启动失败：--no-demo 需要 --provider 指定真实数据源；缺依赖时不回落 demo fixture")
            return 2
        service = _build_injected_service(settings, args.provider)
    else:
        service = build_demo_service(settings)
    if args.doctor:
        report = service.doctor()
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if report["ok"] else 1
    free, detail = check_port_free(settings.host, settings.port)
    if not free:
        print("启动失败：" + ERROR_PORT_IN_USE + " " + detail + "；请换 --port 或用 --doctor 排查")
        return 1
    url = "http://" + settings.host + ":" + str(settings.port) + "/?token=" + settings.token
    print("pq_agent 本地演示服务已启动（仅监听 " + settings.host + "）")
    print("访问地址（token 只在内存里，进程退出即失效）：")
    print(url)
    print(DEMO_BANNER)
    import uvicorn

    uvicorn.run(create_app(service, settings), host=settings.host, port=settings.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
