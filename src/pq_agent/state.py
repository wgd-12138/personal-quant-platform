"""pq_agent AgentState 与版本化序列化合同。

state 是企业 agent 的**持久化 API**：它同时是 LangGraph 的通道定义、checkpoint 的
落盘格式、以及 API 层暴露给前端的契约。因此本模块负责：

1. 定义唯一的 state 字段集合与每个字段的类型约束。
2. 提供 JSON schema、序列化与反序列化，非法类型显式拒绝。
3. 版本兼容规则：恢复时版本不匹配必须失败，禁止静默重建。
4. 出口脱敏：state / checkpoint 落盘前必须过 pq_agent.security 的统一闸门。

禁止进入 state 的内容：完整 prompt、HTTP headers、webhook、API key、异常堆栈、
任意文件内容。messages 只保存脱敏摘要和 ID（见 contracts.Message）。
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypedDict

from pq_agent.contracts import (
    BUDGET_JSON_SCHEMA,
    CONTRACTS_SCHEMA_VERSION,
    INTENT_SOURCES,
    MESSAGE_SCHEMA_VERSION,
    Approval,
    Budget,
    Citation,
    ContractError,
    Message,
    ToolCall,
    ToolResult,
    optional_str,
    require_choice,
    require_int,
    require_json_value,
    require_mapping,
    require_sequence,
    require_str,
)
from pq_agent.data.provider import DEFAULT_PROFILE, Profile
from pq_agent.security.redaction import find_secrets, guard_payload, is_sensitive_key, scrub_text

STATE_SCHEMA_VERSION = 1
CHECKPOINT_SCHEMA_VERSION = STATE_SCHEMA_VERSION
GRAPH_VERSION = "pq_agent.graph/1"

MODES: tuple[str, ...] = tuple(profile.value for profile in Profile)
DEFAULT_MODE = DEFAULT_PROFILE.value

STATE_MESSAGE_SUMMARY_MAX_LEN = 2000
STATE_ERROR_MAX_LEN = 300


class AgentState(TypedDict):
    """LangGraph 通道定义。字段集合与 STATE_FIELDS 必须一致（有测试守护）。"""

    messages: list[dict[str, Any]]
    user_query: str
    thread_id: str
    request_id: str
    mode: str
    as_of: str | None
    timezone: str
    data_snapshot_id: str | None
    checkpoint_schema_version: int
    graph_version: str
    intent: str
    intent_source: str
    slots: dict[str, Any]
    plan: list[dict[str, Any]]
    cursor: int
    pending_approval: dict[str, Any] | None
    approvals: list[dict[str, Any]]
    tool_results: list[dict[str, Any]]
    citations: list[dict[str, Any]]
    budget: dict[str, Any]
    errors: list[str]
    final_answer: str | None
    trace_id: str


STATE_FIELDS: tuple[str, ...] = tuple(AgentState.__annotations__)

# 图启动必须齐备的字段：缺任何一个都不能开始跑图。
STATE_START_REQUIRED_FIELDS: tuple[str, ...] = (
    "thread_id",
    "request_id",
    "mode",
    "timezone",
    "checkpoint_schema_version",
    "graph_version",
)

# 任何层级的键名命中这些归一化名就拒绝进入 state/checkpoint。
FORBIDDEN_STATE_KEYS: tuple[str, ...] = (
    "headers",
    "authorization",
    "cookie",
    "raw_prompt",
    "prompt",
    "system_prompt",
    "traceback",
    "stack",
    "exception",
    "webhook",
    "webhook_url",
    "api_key",
    "apikey",
    "password",
    "secret",
    "file_content",
)


def _now_iso(now: dt.datetime | None) -> str:
    moment = now if now is not None else dt.datetime.now(dt.timezone.utc)
    if moment.tzinfo is None:
        raise ContractError("DATETIME_NAIVE", "state 的时间必须带时区")
    return moment.isoformat()


def new_state(
    *,
    thread_id: str,
    mode: str = DEFAULT_MODE,
    user_query: str = "",
    request_id: str | None = None,
    timezone: str = "Asia/Shanghai",
    as_of: str | None = None,
    data_snapshot_id: str | None = None,
    trace_id: str = "",
    budget: Budget | None = None,
    now: dt.datetime | None = None,
    slots: Mapping[str, Any] | None = None,
) -> AgentState:
    """构造一个通过校验的初始 state。request_id 默认由 thread_id 派生，保证可追溯。

    slots 允许调用方预置（例如 UI 上选定的推送时段）。意图层会在自己的结果里合并，
    不会把它覆盖掉；不传就是空对象，与旧行为一致。
    """
    stamp = _now_iso(now)
    plan_budget = budget or Budget(started_at=stamp)
    state: AgentState = {
        "messages": [],
        "user_query": user_query,
        "thread_id": thread_id,
        "request_id": request_id or f"{thread_id}:{stamp}",
        "mode": mode,
        "as_of": as_of,
        "timezone": timezone,
        "data_snapshot_id": data_snapshot_id,
        "checkpoint_schema_version": STATE_SCHEMA_VERSION,
        "graph_version": GRAPH_VERSION,
        "intent": "",
        "intent_source": "fallback",
        "slots": dict(slots or {}),
        "plan": [],
        "cursor": 0,
        "pending_approval": None,
        "approvals": [],
        "tool_results": [],
        "citations": [],
        "budget": plan_budget.to_dict(),
        "errors": [],
        "final_answer": None,
        "trace_id": trace_id,
    }
    validate_state(state, require_startable=True)
    return state


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------


def _check_forbidden_keys(value: Any, where: str, *, depth: int = 0) -> None:
    if depth > 12:
        raise ContractError("STATE_DEPTH_EXCEEDED", f"{where} 嵌套超过 12 层")
    if isinstance(value, Mapping):
        for key, item in value.items():
            name = str(key)
            if is_sensitive_key(name) or name.lower() in FORBIDDEN_STATE_KEYS:
                raise ContractError("STATE_FORBIDDEN_KEY", f"{where}.{name} 禁止进入 state/checkpoint")
            _check_forbidden_keys(item, f"{where}.{name}", depth=depth + 1)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, item in enumerate(value):
            _check_forbidden_keys(item, f"{where}[{index}]", depth=depth + 1)


def _validate_messages(value: Any, where: str) -> list[dict[str, Any]]:
    items = require_sequence(value, where)
    out: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        message = Message.from_dict(item)
        summary = scrub_text(message.summary, max_len=STATE_MESSAGE_SUMMARY_MAX_LEN)
        if len(summary) > STATE_MESSAGE_SUMMARY_MAX_LEN:
            raise ContractError("FIELD_TOO_LONG", f"{where}[{index}].summary 超过 {STATE_MESSAGE_SUMMARY_MAX_LEN}")
        out.append(Message(message.message_id, message.role, summary, message.schema_version).to_dict())
    return out


def _validate_date_or_none(value: Any, where: str) -> str | None:
    text = optional_str(value, where)
    if text is None:
        return None
    try:
        dt.date.fromisoformat(text)
    except ValueError as exc:
        raise ContractError("DATE_INVALID", f"{where}='{text}'：{exc}") from exc
    return text


def validate_state(
    state: Mapping[str, Any],
    *,
    require_startable: bool = False,
    expected_state_schema_version: int | None = None,
    expected_graph_version: str | None = None,
) -> None:
    """逐字段校验 state。任何非法类型都显式拒绝，不做隐式转换。"""
    source = require_mapping(state, "state")
    unknown = sorted(set(source) - set(STATE_FIELDS))
    if unknown:
        raise ContractError("STATE_UNKNOWN_FIELD", f"state 出现未声明字段 {unknown}")
    missing = [name for name in STATE_FIELDS if name not in source]
    if missing:
        raise ContractError("STATE_MISSING_FIELD", f"state 缺字段 {missing}")
    if require_startable:
        absent = [name for name in STATE_START_REQUIRED_FIELDS if not source.get(name)]
        if absent:
            raise ContractError("STATE_NOT_STARTABLE", f"图启动前必须齐备 {absent}")

    _check_forbidden_keys(source, "state")

    thread_id = require_str(source["thread_id"], "state.thread_id", max_len=128)
    request_id = require_str(source["request_id"], "state.request_id", max_len=256)
    mode = require_choice(source["mode"], "state.mode", MODES)
    require_str(source["timezone"], "state.timezone", max_len=64)
    require_str(source["user_query"], "state.user_query", allow_empty=True, max_len=2000)
    _validate_date_or_none(source["as_of"], "state.as_of")
    optional_str(source["data_snapshot_id"], "state.data_snapshot_id", max_len=200)
    schema_version = require_int(source["checkpoint_schema_version"], "state.checkpoint_schema_version", minimum=1)
    graph_version = require_str(source["graph_version"], "state.graph_version", max_len=64)
    require_str(source["intent"], "state.intent", allow_empty=True, max_len=64)
    require_choice(source["intent_source"], "state.intent_source", INTENT_SOURCES)
    slots = require_json_value(source["slots"], "state.slots")
    if not isinstance(slots, Mapping):
        raise ContractError("STATE_FIELD_TYPE", "state.slots 需要对象")
    require_str(source["trace_id"], "state.trace_id", allow_empty=True, max_len=128)
    optional_str(source["final_answer"], "state.final_answer", max_len=8000)
    require_int(source["cursor"], "state.cursor", minimum=0)

    if expected_state_schema_version is not None and schema_version != expected_state_schema_version:
        raise ContractError(
            "CHECKPOINT_VERSION_MISMATCH",
            f"state.checkpoint_schema_version={schema_version}，期望 {expected_state_schema_version}；禁止静默重建",
        )
    if expected_graph_version is not None and graph_version != expected_graph_version:
        raise ContractError(
            "GRAPH_VERSION_MISMATCH",
            f"state.graph_version='{graph_version}'，期望 '{expected_graph_version}'；禁止静默重建",
        )

    _validate_messages(source["messages"], "state.messages")

    plan = require_sequence(source["plan"], "state.plan")
    for index, item in enumerate(plan):
        ToolCall.from_dict(item)

    approvals = require_sequence(source["approvals"], "state.approvals")
    for index, item in enumerate(approvals):
        from pq_agent.contracts import Receipt

        Receipt.from_dict(item)

    tool_results = require_sequence(source["tool_results"], "state.tool_results")
    for index, item in enumerate(tool_results):
        ToolResult.from_dict(item)

    citations = require_sequence(source["citations"], "state.citations")
    for index, item in enumerate(citations):
        Citation.from_dict(item)

    if source["pending_approval"] is not None:
        Approval.from_dict(source["pending_approval"])

    errors = require_sequence(source["errors"], "state.errors")
    for index, item in enumerate(errors):
        text = require_str(item, f"state.errors[{index}]", allow_empty=False, max_len=STATE_ERROR_MAX_LEN)
        if "\n" in text:
            raise ContractError("STATE_ERROR_MULTILINE", f"state.errors[{index}] 只允许单行错误码摘要")

    Budget.from_dict(source["budget"])

    hits = find_secrets(source)
    if hits:
        raise ContractError("STATE_SECRET_DETECTED", f"state 命中敏感内容 {sorted(set(hits))}")

    if not thread_id or not request_id or not mode:
        raise ContractError("STATE_NOT_STARTABLE", "thread_id / request_id / mode 必须非空")


# --------------------------------------------------------------------------
# 序列化
# --------------------------------------------------------------------------


def state_to_dict(state: Mapping[str, Any]) -> dict[str, Any]:
    validate_state(state)
    return json.loads(json.dumps(dict(state), ensure_ascii=False, sort_keys=True))


def state_to_json(state: Mapping[str, Any]) -> str:
    return json.dumps(state_to_dict(state), ensure_ascii=False, sort_keys=True)


def state_from_json(text: str, **kwargs: Any) -> AgentState:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ContractError("STATE_JSON_INVALID", str(exc)) from exc
    return state_from_dict(payload, **kwargs)


# --------------------------------------------------------------------------
# 版本与迁移
# --------------------------------------------------------------------------

StateMigration = Callable[[Mapping[str, Any]], Mapping[str, Any]]

STATE_MIGRATIONS: dict[tuple[int, int], StateMigration] = {}


def register_state_migration(source_version: int, target_version: int) -> Callable[[StateMigration], StateMigration]:
    """注册显式迁移函数。没有注册迁移路径的旧 checkpoint 一律拒绝恢复。"""

    def decorator(func: StateMigration) -> StateMigration:
        key = (source_version, target_version)
        if key in STATE_MIGRATIONS:
            raise ContractError("STATE_MIGRATION_DUPLICATE", f"{source_version}->{target_version} 已注册")
        STATE_MIGRATIONS[key] = func
        return func

    return decorator


@register_state_migration(0, 1)
def _migrate_state_v0_to_v1(state: Mapping[str, Any]) -> dict[str, Any]:
    """v0 是规划阶段的草稿形状：缺 request_id / timezone / graph_version / 审批与结果字段。

    迁移只补结构、不猜业务：as_of、data_snapshot_id 一律置空，intent_source 记为 fallback。
    """
    work = dict(state)
    thread_id = require_str(work.get("thread_id"), "state.thread_id", max_len=128)
    work.setdefault("request_id", f"{thread_id}:legacy-v0")
    work.setdefault("timezone", "Asia/Shanghai")
    work.setdefault("as_of", None)
    work.setdefault("data_snapshot_id", None)
    work.setdefault("graph_version", GRAPH_VERSION)
    work.setdefault("intent", "")
    work.setdefault("intent_source", "fallback")
    work.setdefault("slots", {})
    work.setdefault("plan", [])
    work.setdefault("cursor", 0)
    work.setdefault("pending_approval", None)
    work.setdefault("approvals", [])
    work.setdefault("tool_results", [])
    work.setdefault("citations", [])
    work.setdefault("errors", [])
    work.setdefault("final_answer", None)
    work.setdefault("trace_id", "")
    work.setdefault("user_query", "")
    if "budget" not in work:
        work["budget"] = Budget().to_dict()
    messages = work.get("messages", [])
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        raise ContractError("TYPE_INVALID", "state.messages 需要数组")
    work["messages"] = [
        {
            "message_id": str(item.get("message_id") or item.get("id") or f"m{index}"),
            "role": str(item.get("role", "user")),
            "summary": str(item.get("summary", "")),
            "schema_version": int(item.get("schema_version", MESSAGE_SCHEMA_VERSION)),
        }
        for index, item in enumerate(messages)
        if isinstance(item, Mapping)
    ]
    work["checkpoint_schema_version"] = 1
    return work


def migrate_state(state: Mapping[str, Any], *, target: int = STATE_SCHEMA_VERSION) -> dict[str, Any]:
    """按注册的迁移路径升级 state；没有路径就失败，绝不静默重建。"""
    source = require_mapping(state, "state")
    current = source.get("checkpoint_schema_version")
    if isinstance(current, bool) or not isinstance(current, int):
        raise ContractError(
            "CHECKPOINT_VERSION_MISMATCH",
            f"checkpoint_schema_version 缺失或类型非法：{current!r}",
        )
    if current > target:
        raise ContractError(
            "CHECKPOINT_VERSION_MISMATCH",
            f"checkpoint 版本 {current} 高于当前支持 {target}，禁止降级重建",
        )
    work = dict(source)
    version = current
    guard = 0
    while version != target:
        guard += 1
        if guard > 32:
            raise ContractError("CHECKPOINT_VERSION_MISMATCH", "迁移路径过长，疑似迁移函数自环")
        candidates = sorted(t for (s, t) in STATE_MIGRATIONS if s == version and t > version)
        if not candidates:
            raise ContractError(
                "CHECKPOINT_VERSION_MISMATCH",
                f"没有从 v{version} 到 v{target} 的迁移路径",
            )
        nxt = candidates[0]
        work = dict(STATE_MIGRATIONS[(version, nxt)](work))
        work["checkpoint_schema_version"] = nxt
        version = nxt
    return work


def state_from_dict(
    data: Mapping[str, Any],
    *,
    migrate: bool = True,
    target: int = STATE_SCHEMA_VERSION,
    require_startable: bool = False,
    expected_graph_version: str | None = None,
) -> AgentState:
    """从 checkpoint 恢复。版本不符且无迁移路径时抛 CHECKPOINT_VERSION_MISMATCH。"""
    source = require_mapping(data, "state")
    if "checkpoint_schema_version" not in source:
        raise ContractError("STATE_MISSING_FIELD", "state 缺字段 ['checkpoint_schema_version']")
    version = source.get("checkpoint_schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ContractError("STATE_FIELD_TYPE", "state.checkpoint_schema_version 需要整数")
    if version != target:
        if not migrate:
            raise ContractError(
                "CHECKPOINT_VERSION_MISMATCH",
                f"checkpoint_schema_version={version}，当前={target}；禁止静默重建",
            )
        source = migrate_state(source, target=target)
    validate_state(
        source,
        require_startable=require_startable,
        expected_state_schema_version=target,
        expected_graph_version=expected_graph_version,
    )
    return json.loads(json.dumps(dict(source), ensure_ascii=False, sort_keys=True))


def require_startable_state(
    data: Mapping[str, Any],
    *,
    expected_graph_version: str | None = None,
) -> AgentState:
    """图启动入口：缺 thread_id / mode / schema version 直接失败。"""
    return state_from_dict(
        data,
        migrate=False,
        require_startable=True,
        expected_graph_version=expected_graph_version,
    )


# --------------------------------------------------------------------------
# 出口脱敏
# --------------------------------------------------------------------------


def guard_state_for_persistence(
    state: Mapping[str, Any],
    *,
    secrets: Sequence[str] | None = None,
    where: str = "checkpoint",
) -> dict[str, Any]:
    """state / checkpoint 落盘前的唯一出口。

    顺序固定为：先过脱敏闸门（fail-closed，残留即抛 SecretLeakError），再校验结构。
    入口写入 state 之前也应先过同一闸门，这样 state 里永远不会出现明文密钥。
    """
    scrubbed = guard_payload(dict(state), secrets=secrets, where=where)
    validate_state(scrubbed)
    return json.loads(json.dumps(scrubbed, ensure_ascii=False, sort_keys=True))


# --------------------------------------------------------------------------
# JSON Schema
# --------------------------------------------------------------------------

_STATE_PROPERTIES: dict[str, Any] = {
    "messages": {"type": "array", "items": {"type": "object"}},
    "user_query": {"type": "string"},
    "thread_id": {"type": "string", "minLength": 1},
    "request_id": {"type": "string", "minLength": 1},
    "mode": {"type": "string", "enum": list(MODES)},
    "as_of": {"type": ["string", "null"]},
    "timezone": {"type": "string", "minLength": 1},
    "data_snapshot_id": {"type": ["string", "null"]},
    "checkpoint_schema_version": {"type": "integer", "minimum": 1},
    "graph_version": {"type": "string", "minLength": 1},
    "intent": {"type": "string"},
    "intent_source": {"type": "string", "enum": list(INTENT_SOURCES)},
    "slots": {"type": "object"},
    "plan": {"type": "array", "items": {"type": "object"}},
    "cursor": {"type": "integer", "minimum": 0},
    "pending_approval": {"type": ["object", "null"]},
    "approvals": {"type": "array", "items": {"type": "object"}},
    "tool_results": {"type": "array", "items": {"type": "object"}},
    "citations": {"type": "array", "items": {"type": "object"}},
    "budget": {"type": "object"},
    "errors": {"type": "array", "items": {"type": "string"}},
    "final_answer": {"type": ["string", "null"]},
    "trace_id": {"type": "string"},
}

STATE_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "pq_agent.agent_state",
    "type": "object",
    "additionalProperties": False,
    "properties": _STATE_PROPERTIES,
    "required": list(STATE_FIELDS),
}

STATE_CONTRACTS_SCHEMA_VERSION = CONTRACTS_SCHEMA_VERSION
STATE_BUDGET_JSON_SCHEMA = BUDGET_JSON_SCHEMA

__all__ = [
    "AgentState",
    "CHECKPOINT_SCHEMA_VERSION",
    "DEFAULT_MODE",
    "FORBIDDEN_STATE_KEYS",
    "GRAPH_VERSION",
    "MODES",
    "STATE_BUDGET_JSON_SCHEMA",
    "STATE_CONTRACTS_SCHEMA_VERSION",
    "STATE_FIELDS",
    "STATE_JSON_SCHEMA",
    "STATE_MIGRATIONS",
    "STATE_SCHEMA_VERSION",
    "STATE_START_REQUIRED_FIELDS",
    "guard_state_for_persistence",
    "migrate_state",
    "new_state",
    "register_state_migration",
    "require_startable_state",
    "state_from_dict",
    "state_from_json",
    "state_to_dict",
    "state_to_json",
    "validate_state",
]
