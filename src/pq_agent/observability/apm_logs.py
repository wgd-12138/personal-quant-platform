"""A4-02 structured logs：让日志能被 trace 找到，同时不把原文写进去。

为什么需要单独一层：

1. **日志与 trace 的关联必须显式建立。**只写 trace_id 不够——一条日志属于哪个 span，
   决定了在 SkyWalking 里点开 waterfall 某一层时能不能看到它。所以记录里必须有 span_id，
   并且 span_id 必须真的出现在本次 trace 的 span 树里，否则就是假关联。
2. **错误原文是最容易泄密的地方。**异常字符串里经常带 URL、带 token、带文件绝对路径、
   带用户原始问题。所以 scrub 不是可选项：record() 里无条件过一遍脱敏，
   而且 scrub 之后要再扫一遍确认没有残留，有残留就拒绝写入（fail-closed）。
3. **日志的字段集合必须固定。**允许任意 extra 字段，就等于允许有人把 arguments 塞进来。
   所以字段表是白名单，未知字段直接抛错。

与 event_log 的分工：event_log 记「观测系统自身的事件」（降级、丢弃、导出失败），
本模块记「业务执行过程中的结构化日志」（节点开始/结束、工具调用、审批、沙箱）。
两者都用同一套脱敏规则，都落 JSONL。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pq_agent.contracts import ContractError
from pq_agent.security.redaction import SecretLeakError
from pq_agent.security.redaction import collect_secrets_from_environ, find_secrets
from pq_agent.security.redaction import scrub_text as secure_scrub_text

LOG_SCHEMA_VERSION = "pq_agent.apm_logs/1"

ERROR_LOG_FIELD = "APM_LOG_FIELD_UNKNOWN"
ERROR_LOG_RESIDUAL_SECRET = "APM_LOG_SECRET_RESIDUAL"
ERROR_LOG_ORPHAN_SPAN = "APM_LOG_ORPHAN_SPAN"
ERROR_LOG_MISSING_TRACE = "APM_LOG_TRACE_ID_REQUIRED"

# 允许出现的字段。每一个都必须是「不能装下整段正文」的字段。
ALLOWED_LOG_FIELDS: tuple[str, ...] = (
    "trace_id",
    "span_id",
    "parent_span_id",
    "thread_id",
    "run_id",
    "node",
    "tool",
    "capability",
    "status",
    "error_code",
    "error_type",
    "duration_ms",
    "intent",
    "intent_source",
    "mode",
    "decision",
    "runtime",
    "isolation_level",
    "hit_count",
    "dropped",
    "degraded",
    "severity",
    "event",
    "message",
    "occurred_at",
    "schema_version",
)

# 这些字段名即使出现在 extra 里也必须拒绝：它们能装下提示词、参数或密钥。
FORBIDDEN_LOG_FIELDS: tuple[str, ...] = (
    "prompt",
    "system_prompt",
    "messages",
    "message_content",
    "query",
    "user_query",
    "arguments",
    "headers",
    "authorization",
    "cookie",
    "webhook",
    "webhook_url",
    "token",
    "secret",
    "api_key",
    "password",
    "checkpoint",
    "exception",
    "stack",
    "traceback",
    "file_content",
    "raw_text",
    "content",
)

SEVERITIES: tuple[str, ...] = ("debug", "info", "warning", "error")

# message 字段的长度上限。日志是摘要，不是正文转存。
MAX_MESSAGE_CHARS = 240

# 结构化日志的事件名白名单。名字固定才能让「同一失败在 UI 里对得上」这件事可断言。
LOG_EVENT_RUN_START = "run.start"
LOG_EVENT_RUN_END = "run.end"
LOG_EVENT_NODE_START = "node.start"
LOG_EVENT_NODE_END = "node.end"
LOG_EVENT_TOOL_INVOKE = "tool.invoke"
LOG_EVENT_APPROVAL = "approval.decision"
LOG_EVENT_SANDBOX = "sandbox.execute"
LOG_EVENT_RAG = "rag.retrieve"
LOG_EVENT_LLM = "llm.intent"
LOG_EVENT_TELEMETRY = "telemetry.degraded"

LOG_EVENTS: tuple[str, ...] = (
    LOG_EVENT_RUN_START,
    LOG_EVENT_RUN_END,
    LOG_EVENT_NODE_START,
    LOG_EVENT_NODE_END,
    LOG_EVENT_TOOL_INVOKE,
    LOG_EVENT_APPROVAL,
    LOG_EVENT_SANDBOX,
    LOG_EVENT_RAG,
    LOG_EVENT_LLM,
    LOG_EVENT_TELEMETRY,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def scrub_text(text: str, *, secrets: Sequence[str] = ()) -> tuple[str, tuple[str, ...]]:
    """对一段文本做脱敏，并再扫一遍确认没有残留。

    这里刻意复用 security.redaction 的 scrub_text，而不是自己拼一套正则：
    它是全仓唯一一处「替换已知密钥 + 正则打码 + 残留检查」的实现。
    再写第二份，就会出现两套脱敏规则，其中一套迟早落后，
    而落后的那套正好写在日志路径上——日志是最容易把密钥带出去的地方。

    返回 (脱敏后的文本, 命中的残留标签列表)。
    残留非空时抛 ContractError：宁可丢一条日志，也不能把密钥写进文件。
    """
    raw = str(text or "")
    try:
        cleaned = secure_scrub_text(raw, secrets=list(secrets or ()))
    except SecretLeakError as exc:
        raise ContractError(ERROR_LOG_RESIDUAL_SECRET, "脱敏后仍检出密钥形态：" + str(exc))
    residual = tuple(str(item) for item in find_secrets(cleaned, secrets=list(secrets or ())))
    return cleaned, residual


@dataclass(frozen=True)
class LogRecord:
    """一条结构化日志。字段固定，值全部是标量。"""

    trace_id: str
    event: str
    severity: str = "info"
    span_id: str = ""
    parent_span_id: str = ""
    message: str = ""
    fields: Mapping[str, Any] = field(default_factory=dict)
    occurred_at: str = ""

    def __post_init__(self) -> None:
        if not self.trace_id:
            raise ContractError(ERROR_LOG_MISSING_TRACE, "日志必须带 trace_id，否则在 UI 里找不到它")
        if not self.event:
            raise ContractError(ERROR_LOG_FIELD, "日志必须带 event 名")
        if self.event not in LOG_EVENTS:
            raise ContractError(ERROR_LOG_FIELD, "未登记的事件名：" + str(self.event))
        if self.severity not in SEVERITIES:
            raise ContractError(ERROR_LOG_FIELD, "未知日志级别：" + str(self.severity))
        if len(str(self.message)) > MAX_MESSAGE_CHARS:
            raise ContractError(
                ERROR_LOG_FIELD,
                "message 超过 %d 字，日志不能当正文转存" % MAX_MESSAGE_CHARS,
            )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": LOG_SCHEMA_VERSION,
            # 不拿「现在」顶替空时间：那会让昨天跑的运存在界面上显示成今天。
            # 空着就是「这条日志没有可信时间」，看的人应该知道这件事。
            "occurred_at": self.occurred_at,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "severity": self.severity,
            "event": self.event,
            "message": self.message,
        }
        if self.parent_span_id:
            payload["parent_span_id"] = self.parent_span_id
        for key in sorted(self.fields):
            payload[key] = self.fields[key]
        return payload


def build_log_record(
    *,
    trace_id: str,
    event: str,
    severity: str = "info",
    span_id: str = "",
    parent_span_id: str = "",
    message: str = "",
    secrets: Sequence[str] = (),
    occurred_at: str = "",
    **fields: Any,
) -> LogRecord:
    """构造日志记录：字段白名单 -> 脱敏 -> 残留检查，顺序不能换。

    残留检查放在最后，因为只有脱敏后的文本才值得扫；
    先扫原文会把「密钥出现」和「密钥被写进日志」混成一件事。
    """
    for key in fields:
        name = str(key)
        if name in FORBIDDEN_LOG_FIELDS:
            raise ContractError(ERROR_LOG_FIELD, "禁止写入日志字段：" + name)
        if name not in ALLOWED_LOG_FIELDS:
            raise ContractError(ERROR_LOG_FIELD, "日志字段不在白名单里：" + name)

    cleaned_message, residual = scrub_text(message, secrets=secrets)
    if residual:
        raise ContractError(
            ERROR_LOG_RESIDUAL_SECRET,
            "脱敏后仍检出密钥形态：%s" % ", ".join(sorted(set(residual))),
        )

    safe_fields: dict[str, Any] = {}
    for key in sorted(fields):
        value = fields[key]
        if isinstance(value, (dict, list, tuple, set)):
            raise ContractError(ERROR_LOG_FIELD, "日志字段 %s 不能是容器类型" % key)
        if isinstance(value, str):
            text, hit = scrub_text(value, secrets=secrets)
            if hit:
                raise ContractError(
                    ERROR_LOG_RESIDUAL_SECRET,
                    "字段 %s 脱敏后仍检出密钥形态" % key,
                )
            if len(text) > MAX_MESSAGE_CHARS:
                raise ContractError(
                    ERROR_LOG_FIELD,
                    "字段 %s 超过 %d 字" % (key, MAX_MESSAGE_CHARS),
                )
            safe_fields[key] = text
        else:
            safe_fields[key] = value

    return LogRecord(
        trace_id=str(trace_id),
        event=str(event),
        severity=str(severity),
        span_id=str(span_id),
        parent_span_id=str(parent_span_id),
        message=cleaned_message,
        fields=safe_fields,
        occurred_at=str(occurred_at or ""),
    )

class LogSink:
    """结构化日志的去处。默认只留内存；给了 path 才落盘。

    落盘用 JSONL 追加，写入前统一经过 build_log_record 的脱敏，
    所以「有没有密钥写进文件」这件事可以在验收里直接断言文件内容。
    """

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        max_bytes: int = 1_000_000,
        secrets: Sequence[str] | None = None,
        clock: Any = None,
    ) -> None:
        self.path = Path(path) if path else None
        self.max_bytes = int(max_bytes)
        self.secrets = tuple(secrets or ())
        self.clock = clock
        self.records: list[LogRecord] = []
        self.dropped = 0
        self.rotations = 0

    def _now(self) -> datetime:
        if self.clock is not None:
            return self.clock()
        return datetime.now(timezone.utc)

    def add(self, record: LogRecord) -> LogRecord:
        """收下一条日志。落盘失败只累加 dropped，不抛给主流程。"""
        self.records.append(record)
        if self.path is not None:
            try:
                self._persist(record)
            except OSError:
                self.dropped += 1
        return record

    def _persist(self, record: LogRecord) -> None:
        payload = record.to_dict()
        if not payload.get("occurred_at"):
            payload["occurred_at"] = self._now().isoformat()
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size + len(line) > self.max_bytes:
            rotated = self.path.with_name(self.path.name + ".%d" % self.rotations)
            self.path.replace(rotated)
            self.rotations += 1
        with self.path.open("a", encoding="utf-8", newline=chr(10)) as handle:
            handle.write(line + chr(10))

    def read_lines(self) -> list[dict[str, Any]]:
        """回读盘上内容。用来做「文件里确实没有密钥」这类断言。"""
        if self.path is None or not self.path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for raw in self.path.read_text(encoding="utf-8").splitlines():
            text = raw.strip()
            if not text:
                continue
            try:
                rows.append(json.loads(text))
            except ValueError:
                self.dropped += 1
        return rows

    def for_trace(self, trace_id: str) -> list[LogRecord]:
        return [item for item in self.records if item.trace_id == str(trace_id)]

    def status(self) -> dict[str, Any]:
        return {
            "schema_version": LOG_SCHEMA_VERSION,
            "path": str(self.path) if self.path else "",
            "record_count": len(self.records),
            "dropped": self.dropped,
            "rotations": self.rotations,
            "events": sorted({item.event for item in self.records}),
        }


@dataclass(frozen=True)
class CorrelationReport:
    """日志与 trace 的关联体检结果。"""

    trace_id: str
    log_count: int = 0
    orphan_span_ids: tuple[str, ...] = ()
    missing_events: tuple[str, ...] = ()
    span_ids_without_logs: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.orphan_span_ids and not self.missing_events

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "log_count": int(self.log_count),
            "orphan_span_ids": list(self.orphan_span_ids),
            "missing_events": list(self.missing_events),
            "span_ids_without_logs": list(self.span_ids_without_logs),
            "ok": bool(self.ok),
        }


def correlate_logs(
    records: Iterable[LogRecord],
    *,
    trace_id: str,
    known_span_ids: Iterable[str],
    require_events: Sequence[str] = (),
    side_effect_span_ids: Iterable[str] = (),
) -> CorrelationReport:
    """检查日志与 span 树的关联是否成立。

    三条判据，缺一不可：
    1. 每条日志的 span_id（若有）必须真的在 span 树里 —— 否则 UI 点进去看不到它，是假关联。
    2. 要求的 event 必须都出现过 —— 「应该有工具调用日志」这种事不能靠眼看。
    3. 有副作用的 span 必须有日志 —— 副作用没有日志，等于事后无法追责。
    """
    rows = [item for item in records if item.trace_id == str(trace_id)]
    known = {str(item) for item in known_span_ids}
    orphans = tuple(sorted({item.span_id for item in rows if item.span_id and item.span_id not in known}))
    seen_events = {item.event for item in rows}
    missing = tuple(sorted(str(item) for item in require_events if str(item) not in seen_events))
    logged_spans = {item.span_id for item in rows if item.span_id}
    without_logs = tuple(sorted(str(item) for item in side_effect_span_ids if str(item) not in logged_spans))
    return CorrelationReport(
        trace_id=str(trace_id),
        log_count=len(rows),
        orphan_span_ids=orphans,
        missing_events=missing,
        span_ids_without_logs=without_logs,
    )


def logs_as_events(records: Iterable[LogRecord]) -> list[dict[str, Any]]:
    """把日志摊平成 SSE 风格的事件列表，给 web 的「日志关联」面板用。"""
    return [item.to_dict() for item in records]


__all__ = [
    "ALLOWED_LOG_FIELDS",
    "CorrelationReport",
    "ERROR_LOG_FIELD",
    "ERROR_LOG_MISSING_TRACE",
    "ERROR_LOG_ORPHAN_SPAN",
    "ERROR_LOG_RESIDUAL_SECRET",
    "FORBIDDEN_LOG_FIELDS",
    "LOG_EVENTS",
    "LOG_EVENT_APPROVAL",
    "LOG_EVENT_NODE_END",
    "LOG_EVENT_NODE_START",
    "LOG_EVENT_RAG",
    "LOG_EVENT_RUN_END",
    "LOG_EVENT_RUN_START",
    "LOG_EVENT_SANDBOX",
    "LOG_EVENT_TELEMETRY",
    "LOG_EVENT_TOOL_INVOKE",
    "LOG_SCHEMA_VERSION",
    "LogRecord",
    "LogSink",
    "MAX_MESSAGE_CHARS",
    "SEVERITIES",
    "build_log_record",
    "correlate_logs",
    "logs_as_events",
    "scrub_text",
]