"""pq_agent LangGraph 主图：路由、工具执行循环、预算闸门与恢复。

## 为什么用 graph 而不是 chain

这个 agent 的真实流程不是一条直线：

- 意图识别之后要**看一眼数据齐不齐**，不齐就直接给结论，齐了才往下走；
- 工具执行是 **cursor 循环**：一次计划里排 N 个工具，执行一个、看结果、再判断下一个；
- 有副作用的工具（写盘 / 外发 / 执行代码）必须**停下来等人批**，人批完之后**从断点继续**；
- 每一步都可能**超预算**（步数 / token / 墙钟），超了要终止并说清原因。

chain 表达不了"根据中间结果决定下一步"和"停在中间等人"，所以用 StateGraph。

## 图的形状

    START -> classify_intent -> check_inputs
                                 |-- 缺数据 -> compose_answer -> END
                                 |-- 数据齐 -> rag_retrieve -> plan_tools -> budget_check
                                                                                    |
                                                       route_by_capability <----------+
                                                         |-- read            -> execute_tool -> budget_check（cursor 循环）
                                                         |-- 有副作用         -> request_approval -> execute_tool
                                                                                    |
                                                                    全部执行完 -> compose_answer -> END

任何节点抛错都走显式失败边 handle_error，最后仍会 compose_answer 输出可解释的失败原因。

## 显式边（不允许"静默失败"）

- 失败：节点异常 -> handle_error -> compose_answer -> END
- 取消：外部置 state.cancelled -> 在 budget_check 处短路 -> compose_answer
- 预算超限：budget_check 返回 exceeded -> compose_answer（带 BUDGET_EXCEEDED 原因）
- checkpoint 损坏 / 版本不匹配：在恢复入口抛错，不进入图（见 pq_agent.checkpoint）
- 外发未知结果：不自动重试（见 NOT_RETRYABLE_CAPABILITIES），走人工确认
"""

from __future__ import annotations

import datetime as dt
import json
import contextvars
import threading
import time as _time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.errors import GraphInterrupt
from langgraph.types import Command, interrupt

from pq_agent.contracts import (
    Approval,
    Citation,
    Budget,
    ContractError,
    Message,
    ToolCall,
    ToolResult,
    require_mapping,
)
from pq_agent.intent import classify_intent
from pq_agent.intent_schema import INTENT_CLASSES
from pq_agent.data.models import sha256_text
from pq_agent.approval import APPROVAL_TTL_S, compute_action_hash
from pq_agent.outbox import compute_idempotency_key, compute_payload_digest
from pq_agent.state import GRAPH_VERSION, AgentState, validate_state
def verify_citation_against(records: Mapping[str, Any], payload: Mapping[str, Any]) -> tuple[bool, str]:
    """转调检索层的引用校验。放在这里是为了让图能独立于检索模块被导入。"""
    from pq_agent.rag.retriever import verify_citation_against as _verify

    return _verify(records, payload)

# 不可路由意图：证据不足时交回用户澄清，禁止猜测（见 intent_schema.INTENT_LABELS）
INTENT_FALLBACK = "fallback"


# --------------------------------------------------------------------------
# 节点名（外部按名字定位 trace / 中断点，改名等于破坏对外契约）


# --------------------------------------------------------------------------


NODE_CLASSIFY_INTENT = "classify_intent"


NODE_CHECK_INPUTS = "check_inputs"


NODE_RAG_RETRIEVE = "rag_retrieve"


NODE_PLAN_TOOLS = "plan_tools"


NODE_BUDGET_CHECK = "budget_check"


NODE_REQUEST_APPROVAL = "request_approval"


NODE_EXECUTE_TOOL = "execute_tool"


NODE_COMPOSE_ANSWER = "compose_answer"


NODE_HANDLE_ERROR = "handle_error"


GRAPH_NODES: tuple[str, ...] = (
    NODE_CLASSIFY_INTENT,
    NODE_CHECK_INPUTS,
    NODE_RAG_RETRIEVE,
    NODE_PLAN_TOOLS,
    NODE_BUDGET_CHECK,
    NODE_REQUEST_APPROVAL,
    NODE_EXECUTE_TOOL,
    NODE_COMPOSE_ANSWER,
    NODE_HANDLE_ERROR,
)


ROUTE_READ = "read"


ROUTE_APPROVAL = "approval"


ROUTE_DONE = "done"


ROUTE_EXCEEDED = "exceeded"


ROUTE_MISSING_DATA = "missing_data"
# 角色守卫拒绝：计划里的工具当前角色没权限碰。它是一条独立分支，
# 不是「工具执行失败」——两者的错误码、留痕与处理方式都不同。
ROUTE_ROLE_DENIED = "role_denied"


ROUTE_CANCELLED = "cancelled"
ROUTE_CLARIFY = "clarify"

# 图的一个「阶段」。它不进 state（state 的 23 个字段是冻结的），
# 而是跟在 GraphDeps 上，由节点读写：
#   idle      —— 还没产出计划，或计划已全部处理完
#   planned   —— 计划已经排好、还有调用没执行，下一次回到预算节点就是「执行下一个工具」
#   executing —— 正处于一次工具执行之中（只在一次节点调用内部有效）
# 为什么需要它：工具执行完之后需要知道「下一个还要不要执行」，
# 但 agent 节点拿不到 LangGraph 的 snapshot.next，只能靠这个标记判断。
PHASE_IDLE = "idle"
PHASE_PLANNED = "planned"
PHASE_EXECUTING = "executing"
# 低置信度必须澄清，先把它定义成终止性错误码，收口节点才不会被普通回答覆盖。
ERROR_INTENT_LOW_CONFIDENCE = "INTENT_LOW_CONFIDENCE"
# 意图层在「有效意图但置信度不足」时给的是这个码，两个都要认，否则只拦一半。
INTENT_CONFIDENCE_LOW = "INTENT_CONFIDENCE_LOW"


READ_CAPABILITIES: tuple[str, ...] = ("read_local", "read_external")


SIDE_EFFECT_CAPABILITIES: tuple[str, ...] = ("control_write", "business_write", "notify_external")

# **绝不允许自动重试**的能力。外发一旦发出但结果未知，自动重试会造成重复推送。


NOT_RETRYABLE_CAPABILITIES: tuple[str, ...] = ("notify_external",)

# code_exec 不属于 Contract 的五个 capability（A0-02 单列为工具类别），
# 这里用显式常量表达"执行代码 = 有副作用 = 必须确认"。


CAPABILITY_CODE_EXEC = "code_exec"


ERROR_BUDGET_EXCEEDED = "BUDGET_EXCEEDED"


ERROR_CANCELLED = "RUN_CANCELLED"


ERROR_TOOL_FAILED = "TOOL_FAILED"


ERROR_APPROVAL_REJECTED = "APPROVAL_REJECTED"


ERROR_GRAPH_INTERNAL = "GRAPH_INTERNAL_ERROR"


ERROR_INPUTS_NOT_READY = "INPUTS_NOT_READY"


ERROR_TOOL_UNKNOWN = "TOOL_UNKNOWN"
# 角色守卫拒绝。码与 roles.contracts 的 ROLE_CAPABILITY_DENIED 完全一致：
# 前端拿到的码必须能和角色模块的文档对上，不允许两处各起一个名字。
ERROR_ROLE_CAPABILITY = "ROLE_CAPABILITY_DENIED"
NODE_ROLE_DENIED = "role_denied"

# A6-02 新增：引用合同相关错误码。引用不合法与引用没用上，是两种不同的错。
ERROR_CITATION_INVALID = "CITATION_INVALID"
ERROR_CITATION_UNUSED = "CITATION_UNUSED"
# A1-05 新增：幂等与审批落库相关错误码
ERROR_ALREADY_COMMITTED = "ALREADY_COMMITTED"
ERROR_OUTBOX_UNKNOWN = "OUTBOX_UNKNOWN_REQUIRES_HUMAN"
ERROR_OUTBOX_SETTLE_FAILED = "OUTBOX_SETTLE_FAILED"
ERROR_APPROVAL_STORE_FAILED = "APPROVAL_STORE_FAILED"


DEFAULT_NODE_TIMEOUT_S = 30.0
# 节点与工具的墙钟约束。以前 build_graph(node_timeout_s=...) 只是把参数收下，
# 节点照着跑到底；ToolSpec.timeout_s 也只是写进 schema 给人看。两者都没有执行路径，
# 于是「超时」这个合同条款等于不存在（A6-07 修的就是这件事）。
#
# 做法：真正干活的那部分代码放进一个守护线程里跑，主线程最多等 timeout_s。
# 为什么用线程而不是 SIGALRM / asyncio：节点是同步函数，Windows 上没有 signal.alarm，
# 把整张图改成 async 又会碰上锁定版 sqlite saver 不支持异步。
# 代价要写清楚：线程杀不掉，超时后它会继续跑到自然结束，
# 所以超时的工具调用我们等它自己收尾再往下走（宁可多等，也不让两个写动作并发）。
WORKER_JOIN_GRACE_S = 1.0
# 节点超时留出的余量：守护线程到点返回、主线程再补记错误需要一点时间，
# 不留余量的话「节点超时」会先被包在外面的外层护栏判成「节点整体超时」。
NODE_DEADLINE_SLACK_S = 1.0

# 节点超时的错误码。刻意与 GRAPH_INTERNAL_ERROR 分开：
# 前者是「这个节点太慢」，后者是「这个节点坏了」，排查方向完全不同。
ERROR_NODE_TIMEOUT = "NODE_TIMEOUT"
# 工具超时的错误码。沿用工具层合同里已登记的 TOOL_TIMEOUT，
# 与 TOOL_FAILED（工具自己抛错）区分：超时是「不知道它做完没有」，失败是「明确没做成」。
ERROR_TOOL_TIMEOUT = "TOOL_TIMEOUT"
# 审批与幂等键里的运行时标识。上云换实现时这里必须跟着变，
# 否则云端和本机会算出同一个动作哈希，审批会跨环境被复用。
RUNTIME_LOCAL = "local"


# --------------------------------------------------------------------------
# 依赖注入：图不自己 new 工具、不自己连数据源，全部由外部传入


# --------------------------------------------------------------------------


@dataclass


class GraphDeps:
    """图的外部依赖。默认全部为 None，缺哪个就在用到时显式报错。

    为什么不做成"有默认实现"：企业 agent 里最危险的不是报错，而是**静默用了假依赖**
    （比如把 fake provider 当成真数据源，或把内存 saver 当成持久化）。缺依赖就失败。
    """

    provider: Any = None
    tools: Any = None
    retriever: Any = None
    intent_model: Any = None
    clock: Callable[[], dt.datetime] | None = None
    tracer: Any = None
    approver: Any = None
    approval_store: Any = None
    outbox: Any = None
    plan_builder: Any = None
    # 当前所处阶段。节点在进入/退出时读写它；见 PHASE_* 的说明。
    phase: str = PHASE_IDLE
    # 运行期事实（不进 state、不进 checkpoint）：最近一次执行开始于什么时刻、
    # 外层是否已经判过超时。用来识别「被丢掉的那段代码后来才写回的迟到结果」。
    runtime_calls: dict[str, Any] = field(default_factory=dict)
    # 最近一次进入节点的代际编号。迟到的那一份比较旧，写回时会被丢掉。
    current_generation: int = 0

    def now(self) -> dt.datetime:
        if self.clock is None:
            return dt.datetime.now(dt.timezone.utc)
        moment = self.clock()
        if moment.tzinfo is None:
            raise ContractError("DATETIME_NAIVE", "GraphDeps.clock 必须返回带时区的 datetime")
        return moment


def _require_dep(value: Any, name: str) -> Any:
    if value is None:
        raise ContractError(
            "GRAPH_DEPENDENCY_MISSING",
            f"图需要依赖 {name}，但未注入；请显式提供，禁止静默使用降级实现",
        )
    return value


def _strip_code_prefix(exc):
    """ContractError 的 str() 形如 'CODE: detail'，而 _error_entry 会再拼一次 code。"""
    """这里剥掉前缀，避免日志里出现 CODE: CODE: detail。"""
    detail = str(exc)
    prefix = exc.code + ": "
    return detail[len(prefix):] if detail.startswith(prefix) else detail


def _error_entry(code: str, detail: str, *, node: str = "") -> str:
    text = f"{code}: {detail}" if detail else code
    if node:
        text = f"{node} | {text}"
    return text[:300]


def _append_message(state: Mapping[str, Any], role: str, summary: str) -> list[dict[str, Any]]:
    """给 messages 追加一条脱敏摘要。message_id 用内容哈希，天然幂等。"""
    messages = list(state.get("messages") or [])
    text = str(summary)[:2000]
    message_id = "m" + sha256_text(f"{role}:{len(messages)}:{text}")[:12]
    messages.append(Message(message_id, role, text).to_dict())
    return messages


def tool_call_arguments_hash(call: ToolCall) -> str:
    """审批绑定用的参数哈希。批准 A 却执行 B 是 HITL 最典型的漏洞。"""
    payload = {"tool_name": call.tool_name, "arguments": dict(call.arguments), "capability": call.capability}
    return sha256_text(json.dumps(payload, sort_keys=True, ensure_ascii=False))


# 节点与常量现已合并到本模块
_UNUSED_IMPORT_MARKER = (
    CAPABILITY_CODE_EXEC,
    ERROR_APPROVAL_REJECTED,
    ERROR_BUDGET_EXCEEDED,
    ERROR_CANCELLED,
    ERROR_GRAPH_INTERNAL,
    ERROR_INPUTS_NOT_READY,
    ERROR_NODE_TIMEOUT,
    ERROR_TOOL_FAILED,
    ERROR_TOOL_TIMEOUT,
    ERROR_TOOL_UNKNOWN,
    GraphDeps,
    READ_CAPABILITIES,
    ROUTE_APPROVAL,
    ROUTE_CANCELLED,
    ROUTE_DONE,
    ROUTE_EXCEEDED,
    ROUTE_MISSING_DATA,
    ROUTE_READ,
    SIDE_EFFECT_CAPABILITIES,
    _append_message,
    _error_entry,
    _require_dep,
    tool_call_arguments_hash,
)

# 需要人工确认的能力集合（含 code_exec 这个 Contract 之外的类别）


APPROVAL_REQUIRED_CAPABILITIES: tuple[str, ...] = SIDE_EFFECT_CAPABILITIES + (CAPABILITY_CODE_EXEC,)


def _state_of(state: Mapping[str, Any]) -> dict[str, Any]:
    return dict(state)


def classify_intent_node(state: Mapping[str, Any], deps: GraphDeps) -> dict[str, Any]:
    """节点 1：意图识别。L1 规则先拦，拦不住才调 L2 模型。

    注意：这里**不重新读时钟**——图的可复现性依赖"参考日期来自 state"。
    相对日期（今天/明天）由意图层在 classify_intent 内部按 today 解析，
    today 必须来自 state.as_of 或 deps.clock 的固定时刻，否则同一次重放会产生不同槽位。
    """
    query = str(state.get("user_query") or "")
    today: dt.date | None = None
    raw_as_of = state.get("as_of")
    if isinstance(raw_as_of, str) and raw_as_of:
        try:
            today = dt.date.fromisoformat(raw_as_of)
        except ValueError:
            today = None
    if today is None:
        today = deps.now().date()

    result = classify_intent(query, today=today, model=deps.intent_model)
    # 调用方给的槽位（例如 UI 上选定的推送时段）必须保留：
    # 意图层只补自己认出来的键，不能用一个空字典把调用方的选择抹掉。
    slots = dict(state.get("slots") or {})
    slots.update(dict(result.slots or {}))
    errors = list(state.get("errors") or [])
    if result.errors:
        errors.extend(_error_entry(code, "", node="classify_intent") for code in result.errors)
    update: dict[str, Any] = {
        "intent": result.intent,
        "intent_source": result.source,
        "slots": slots,
        "messages": _append_message(state, "assistant", f"意图={result.intent} 来源={result.source} 置信度={result.confidence}"),
    }
    if errors != list(state.get("errors") or []):
        update["errors"] = errors
    if getattr(result, "low_confidence", False):
        # 低置信度直接收口：不查数据、不检索、不规划、不执行任何工具。
        # 用一个必然失败的调用去「试」一次，是拿副作用换答案，必须禁止。
        update["errors"] = list(update.get("errors", errors)) + [
            _error_entry(ERROR_INTENT_LOW_CONFIDENCE, "置信度 %s 低于阈值，需要澄清" % result.confidence, node="classify_intent")
        ]
        update["next"] = ROUTE_CLARIFY
        update["final_answer"] = (
            "没听准你的意思，先不动任何工具（需要澄清）。"
            + "识别到的意图：%s（来源 %s，置信度 %s）。" % (result.intent, result.source, result.confidence)
            + "请补充一句更明确的说法，例如要看的日期、股票代码或要生成哪份报告。"
        )
    return update


def check_inputs_node(state: Mapping[str, Any], deps: GraphDeps) -> dict[str, Any]:
    """节点 2：检查数据是否齐备。

    设计要点：**不在这里联网取数**。数据由 provider 以 replay/live 两种 profile 提供，
    replay 下取的是 fixture，live 才触网。这样单测和演示不会意外打到真实行情接口。
    """
    provider = _require_dep(deps.provider, "provider")
    as_of_raw = state.get("as_of")
    if not isinstance(as_of_raw, str) or not as_of_raw:
        return {"errors": list(state.get("errors") or []) + [_error_entry(ERROR_INPUTS_NOT_READY, "缺少 as_of，无法确定数据日期", node="check_inputs")],
                "data_snapshot_id": None}
    as_of = dt.date.fromisoformat(as_of_raw)
    try:
        snapshot = provider.snapshot(as_of)
    except Exception as exc:
        return {
            "errors": list(state.get("errors") or []) + [_error_entry(ERROR_INPUTS_NOT_READY, f"{type(exc).__name__}: {exc}", node="check_inputs")],
            "data_snapshot_id": None,
        }
    prov = snapshot.provenance
    return {
        "data_snapshot_id": snapshot.data_snapshot_id,
        "messages": _append_message(state, "tool", f"数据就绪 snapshot={snapshot.data_snapshot_id} 来源={prov.source} demo={prov.demo}"),
    }


def route_after_intent(state: Mapping[str, Any]) -> str:
    """意图后的分支：低置信度直接去澄清，绝不进入检索与工具链。

    注意：条件边必须用函数，传字符串会被 _error_first 当成固定分支，
    那样无论状态里写什么都一路往下走（这正是低置信度曾被忽略的原因）。
    """
    errors = " ".join(str(item) for item in (state.get("errors") or ()))
    if ERROR_INTENT_LOW_CONFIDENCE in errors or INTENT_CONFIDENCE_LOW in errors:
        return ROUTE_CLARIFY
    return "next"


def route_after_inputs(state: Mapping[str, Any]) -> str:
    """数据检查后的条件路由：缺数据直接收口，不往下走工具。"""
    if state.get("data_snapshot_id"):
        return "ready"
    return ROUTE_MISSING_DATA


def rag_retrieve_node(state: Mapping[str, Any], deps: GraphDeps) -> dict[str, Any]:
    """节点 3：检索历史报告 / 文档。检索器缺失时不硬撑，直接记一条可解释的错误。

    A6-02：检索结果必须逐条通过引用合同校验（chunk_id / 路径 / 行区间 / 内容 hash 四项全对），
    不合法的引用不进 state，而是记 CITATION_INVALID —— 宁可少一条引用，也不给假引用。
    校验的对照物是检索器自己的块表，所以「引用对了」这件事是可回放的，不是自我声明。
    """
    if deps.retriever is None:
        return {"errors": list(state.get("errors") or []) + [_error_entry("RAG_UNAVAILABLE", "未注入 retriever，本阶段跳过检索", node="rag_retrieve")]}
    query = str(state.get("user_query") or "")
    try:
        hits = deps.retriever.retrieve(query, top_k=int((state.get("slots") or {}).get("top_k") or 5))
    except Exception as exc:
        return {"errors": list(state.get("errors") or []) + [_error_entry("RAG_FAILED", f"{type(exc).__name__}: {exc}", node="rag_retrieve")]}
    records = {str(record.chunk_id): record for record in getattr(deps.retriever, "records", ()) or ()}
    citations: list[dict[str, Any]] = []
    errors = list(state.get("errors") or [])
    rejected = 0
    for item in hits or []:
        payload = dict(item)
        ok, reason = verify_citation_against(records, payload)
        if ok:
            citations.append(payload)
        else:
            rejected += 1
            errors.append(_error_entry(ERROR_CITATION_INVALID, reason, node="rag_retrieve"))
    update: dict[str, Any] = {
        "citations": citations,
        "messages": _append_message(state, "tool", f"检索命中 {len(citations)} 条（引用校验通过）"),
    }
    if errors != list(state.get("errors") or []):
        update["errors"] = errors
    if rejected and not citations:
        update["messages"] = _append_message(state, "tool", f"检索命中 0 条可用引用（{rejected} 条未通过引用校验）")
    return update


def plan_tools_node(state: Mapping[str, Any], deps: GraphDeps) -> dict[str, Any]:
    """节点 4：产出工具调用计划。

    计划来源有两处：① 注入的 plan_builder（真实 LLM 规划）；② 意图到工具的直接映射（确定性）。
    默认走②，因为确定性映射可以被单测和评测稳定断言；LLM 规划只作为可插拔增强。

    确定性计划必须以注册表的工具规格为准：能力取自 spec，参数只填工具声明过的字段，
    必填参数凑不齐就不排这个调用并把原因写进 errors。
    """
    if deps.plan_builder is not None:
        plan = deps.plan_builder(dict(state))
        entries = [dict(item) for item in plan or []]
        return {"plan": entries, "cursor": 0}
    plan, notes = plan_with_notes(dict(state), registry=getattr(deps, "tools", None))
    update: dict[str, Any] = {"plan": plan, "cursor": 0}
    if notes:
        update["errors"] = list(state.get("errors") or []) + notes
    return update


def _after_plan_tools(state: Mapping[str, Any], update: Mapping[str, Any], deps: GraphDeps) -> None:
    """计划排好之后记下阶段：下一次回到预算节点就是「执行下一个工具」。

    空计划直接回到 idle：没东西可执行时，预算节点只做一次常规检查，
    不该为一个不存在的工具再扣一步。
    """
    entries = list(update.get("plan") or [])
    deps.phase = PHASE_PLANNED if entries else PHASE_IDLE


INTENT_TOOLS: Mapping[str, tuple[tuple[str, str], ...]] = {
    # 意图 -> ((工具名, 能力), ...)。第二个元素只是「没有注册表时」的兜底声明：
    # 真正生效的能力取自注册表里的 ToolSpec（见 plan_with_notes）。
    # 早先这里写死了能力，于是 daily_pack_preview 被标成 control_write，
    # 一个纯内存预览也要人批；而工具自己声明的是 read_external。
    # 工具名必须与注册表里的名字完全一致：曾经写成 risk_rules_read（真名 policy_rules_read）
    # 和 report_search（当时没注册），对应意图只会得到 TOOL_UNKNOWN。
    "pos_question": (("positions_snapshot", "read_local"),),
    "market_check": (("market_snapshot", "read_external"),),
    "history_question": (("report_search", "read_local"),),
    "report_generate": (("daily_pack_preview", "control_write"),),
    "risk_question": (("policy_rules_read", "read_local"),),
    "push_request": (("push_preview", "control_write"),),
    # code_task 现在有真实工具：code_exec 只接受声明了 untrusted_code_safe 的运行时，
    # 不够条件的部署会在这里拿到明确的拒绝，而不是一句「没有相关工具」。
    "code_task": (("code_exec", "code_exec"),),
    "fallback": (),
}

PLAN_NOTE_MISSING_ARGUMENTS = "PLAN_MISSING_ARGUMENTS"


def default_plan_for_intent(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """按意图给出确定性的只读工具计划。没有计划就返回空（收口时说明原因）。

    参数从 state 里补：market_snapshot 与 daily_pack_preview 的 as_of 是必填项，
    而 state 里本来就有 as_of（会话开始时定死，不由模型自由发挥）。
    早先这里一律传空参数，导致「今天大盘怎么样」这条主只读路径每次都失败在
    TOOL_SCHEMA_INVALID: market_snapshot 缺少必填字段：as_of —— 计划能排出来却永远执行不了，
    这不是保守，是把可用路径弄坏了。
    """
    plan, _notes = plan_with_notes(state)
    return plan


def plan_with_notes(
    state: Mapping[str, Any],
    *,
    registry: Any = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """确定性计划 + 跳过原因。registry 为空时退回到「不问规格」的老行为。

    为什么参数要按规格过滤：plan 早先一律塞 as_of，于是 positions_snapshot（零参数工具）
    收到 as_of 直接报 TOOL_FIELD_FORBIDDEN；push_preview 缺 slot 又报 TOOL_SCHEMA_INVALID。
    「排得出来、一执行就报参数错」的计划不是保守，是把可用路径弄坏了。
    """
    intent = str(state.get("intent") or INTENT_FALLBACK)
    entries = INTENT_TOOLS.get(intent, ())
    available = _plan_arguments(state)
    plan: list[dict[str, Any]] = []
    notes: list[str] = []
    for index, (name, declared_capability) in enumerate(entries):
        spec = _spec_for(registry, name)
        arguments = available if spec is None else _arguments_for_spec(spec, available)
        missing = () if spec is None else _missing_required(spec, arguments)
        if missing:
            notes.append(
                _error_entry(
                    PLAN_NOTE_MISSING_ARGUMENTS,
                    f"{name} 缺少必填参数：{','.join(missing)}",
                    node=NODE_PLAN_TOOLS,
                )
            )
            continue
        capability = declared_capability
        if spec is not None:
            capability = str(getattr(spec, "capability", "") or declared_capability)
        call_id = "c" + sha256_text(f"{state.get('thread_id')}:{intent}:{index}:{name}")[:12]
        plan.append(
            ToolCall(
                call_id=call_id,
                tool_name=name,
                arguments=dict(arguments),
                capability=capability,
                dry_run=_plan_dry_run(capability, arguments),
            ).to_dict()
        )
    return plan, notes


def _plan_dry_run(capability: str, arguments: Mapping[str, Any]) -> bool:
    """计划里的 dry_run：只有调用方显式要求执行时，才允许计划产生副作用。

    code_exec 是唯一一个「计划本身就想真的跑代码」的能力：必须在 slots 里把
    mode 设成 execute 才排成执行，否则一律按预览排。默认永远是 True，
    这样「模型在参数里塞一句 execute」不会变成「没人批准就真的跑起来」。
    """
    if str(capability) == CAPABILITY_CODE_EXEC and str(arguments.get("mode") or "") == "execute":
        return False
    return True


def _spec_for(registry: Any, name: str) -> Any:
    """取工具规格。取不到（未注册、没有注册表、假注册表）就返回 None，不猜规格。"""
    if registry is None:
        return None
    getter = getattr(registry, "get", None)
    if not callable(getter):
        return None
    try:
        return getter(name)
    except Exception:
        return None


def _arguments_for_spec(spec: Any, available: Mapping[str, Any]) -> dict[str, Any]:
    """只保留工具声明过的参数。

    没有 arg_names 属性（假注册表、自定义实现）才原样透传。
    注意不能写成 if not names：空元组是「这个工具一个参数都不收」，
    把它当成「不知道参数表」会让零参数只读工具收到 as_of 而报 TOOL_FIELD_FORBIDDEN ——
    这是本任务实测踩到的坑。
    """
    names = getattr(spec, "arg_names", None)
    if names is None:
        return dict(available)
    declared = {str(item) for item in names}
    return {key: value for key, value in available.items() if key in declared}


def _missing_required(spec: Any, arguments: Mapping[str, Any]) -> tuple[str, ...]:
    """必填但没值的参数。拿不到必填清单就当没有必填项。"""
    required = getattr(spec, "required_arg_names", None)
    if not required:
        return ()
    return tuple(str(item) for item in required if item not in arguments)


def _plan_arguments(state: Mapping[str, Any]) -> dict[str, Any]:
    """从 state 取确定性参数：as_of 加意图槽位。只取状态里已有的值，不造值、不猜日期。"""
    arguments: dict[str, Any] = {}
    as_of = str(state.get("as_of") or "")
    if as_of:
        arguments["as_of"] = as_of
    slots = state.get("slots")
    if isinstance(slots, Mapping):
        for key, value in slots.items():
            if value is None or value == "":
                continue
            arguments.setdefault(str(key), value)
    return arguments


# 终止性错误码：一旦出现，后续工具一律不再执行。
# 为什么是这三个：预算超了、节点超时了、工具超时了——三种情况的共同点是
# 「继续做下去已经不可信」，而不是「这一步失败了但下一步还能做」。
TERMINAL_ERROR_CODES: tuple[str, ...] = (ERROR_BUDGET_EXCEEDED, ERROR_NODE_TIMEOUT, ERROR_TOOL_TIMEOUT)


def _is_terminal(state: Mapping[str, Any]) -> bool:
    joined = " ".join(str(item) for item in (state.get("errors") or []))
    return any(code in joined for code in TERMINAL_ERROR_CODES)


def _force_terminal(update: Any) -> dict[str, Any]:
    """把终止之后的 update 削成纯记录：去掉 plan / cursor / phase / final_answer。

    终止之后还允许迁移的只有 cursor 以外的记录型字段。cursor 必须停住，
    否则路由会把「cursor 前进了」当成「还有下一个工具」。"""
    if not isinstance(update, Mapping):
        return {}
    settled = dict(update)
    for key in ("plan", "phase", "final_answer"):
        settled.pop(key, None)
    return settled


def _terminal_update(state: Mapping[str, Any], update: dict[str, Any], *, steps: int) -> dict[str, Any]:
    """终止之后只写「这一步发生了什么」，绝不写 final_answer（会被覆盖），也不推进 phase。"""
    settled = dict(update)
    budget = Budget(**dict(settled.get("budget") or _budget_of(state).to_dict()))
    if steps > int(budget.steps):
        settled["budget"] = Budget(
            max_steps=budget.max_steps,
            max_tokens=budget.max_tokens,
            max_wall_clock_s=budget.max_wall_clock_s,
            steps=int(steps),
            tokens=int(budget.tokens),
            started_at=budget.started_at,
            schema_version=budget.schema_version,
        ).to_dict()
    return settled


def _budget_of(state: Mapping[str, Any]) -> Budget:
    """从 state 还原预算。budget 是 dict，加字段会破坏 checkpoint，所以只改值不加键。"""
    return Budget(**dict(state.get("budget") or {}))


def _step_of(state: Mapping[str, Any], budget: Budget) -> int:
    """本次要消耗的步数：每执行一次工具算一步。

    消耗值取 max(budget.steps + 1, cursor + 1)。为什么要和 cursor 取大：
    计划里每个调用对应一个 cursor 位置，而预算里的 steps 还要覆盖「工具全按预算下界跑完」的提前拒绝，
    两者取大才能保证「累计步数」既不被工具执行次数少算，也不被重复计数虚增。
    """
    return max(int(budget.steps) + 1, int(state.get("cursor") or 0) + 1)


def _budget_exceeded_update(state: Mapping[str, Any], *, steps: int, reason: str) -> dict[str, Any]:
    """超预算的统一收口：写回预算、记错误码、保留已完成步骤、给出终止性结论。

    「保留已完成步骤」是这里的重点：
    - tool_results 原样保留（已经拿到的结果是可以继续用的证据，不是废纸）；
    - plan 与 cursor 也原样不动，事后能看清「排了 3 个、跑了 2 个、第 3 个为什么没跑」。
    只把「剩下的不跑了」写进 final_answer 与 errors，不悄悄截断计划。
    """
    current = _budget_of(state)
    updated = Budget(
        max_steps=current.max_steps,
        max_tokens=current.max_tokens,
        max_wall_clock_s=current.max_wall_clock_s,
        steps=max(int(steps), int(current.steps)),
        tokens=int(current.tokens),
        started_at=current.started_at,
        schema_version=current.schema_version,
    )
    plan = list(state.get("plan") or [])
    done = [str(item.get("tool_name") or "") for item in (state.get("tool_results") or []) if isinstance(item, Mapping)]
    remaining = max(0, len(plan) - int(state.get("cursor") or 0))
    return {
        "budget": updated.to_dict(),
        "errors": list(state.get("errors") or []) + [_error_entry(ERROR_BUDGET_EXCEEDED, reason, node=NODE_EXECUTE_TOOL)],
        "final_answer": (
            f"执行已终止：{reason}。"
            f"已完成的步骤保留：{len(done)} 个工具结果（{', '.join(done) or '无'}）；"
            f"后续 {remaining} 个工具不再执行。"
        ),
    }


def _budget_gate_update(state: Mapping[str, Any], deps: GraphDeps) -> dict[str, Any] | None:
    """进入节点时的预算闸门。未超限返回 None。

    这里的「下一大步」预估：正准进工具执行的调用按已消耗步数 +1 计，
    其它节点（意图、检索、规划、收口）按 +1 算，是为了让 max_steps=1 这类极紧预算
    真的只会放行一件事，而不是「先跑完整个前置流程再说」。
    """
    phase = str(state.get("phase") or PHASE_IDLE)
    budget = _budget_of(state)
    if phase == PHASE_PLANNED:
        next_step = _step_of(state, budget)
    else:
        next_step = int(budget.steps) + 1
    verdict = budget.evaluate(deps.now(), extra_steps=next_step - int(budget.steps))
    if verdict["ok"]:
        return None
    return _budget_exceeded_update(state, steps=next_step, reason=verdict["reason"])


def budget_check_node(state: Mapping[str, Any], deps: GraphDeps) -> dict[str, Any]:
    """节点 5：预算闸门。超限要给出明确原因，不静默截断。

    现状修正（A6-07）：预算以前只在**进入这个节点时**评估一次，
    于是计划里第 1 个工具跑完、第 2 个工具直接把预算撞爆也不会被拦。
    现在预算在三个位置都生效：节点入口（_wrap）、工具执行后（execute_tool）、
    以及再次回到这里的检查点。评估出来的 steps 会**写回** budget，不再只算不回写。
    """
    if state.get("_cancel_requested") or any(ERROR_CANCELLED in str(e) for e in (state.get("errors") or [])):
        return {"errors": list(state.get("errors") or []) + [_error_entry(ERROR_CANCELLED, "外部请求取消", node="budget_check")],
                "final_answer": "已按请求取消本次执行。"}
    budget = _budget_of(state)
    if str(state.get("phase") or PHASE_IDLE) == PHASE_PLANNED:
        # 这次进来是为了执行计划里的下一个调用：把它要消耗的步数先计入预算。
        steps = _step_of(state, budget)
        verdict = budget.evaluate(deps.now(), extra_steps=steps - int(budget.steps))
    else:
        steps = int(budget.steps)
        verdict = budget.evaluate(deps.now())
    if not verdict["ok"]:
        return _budget_exceeded_update(state, steps=steps, reason=verdict["reason"])
    if steps != int(budget.steps):
        updated = Budget(
            max_steps=budget.max_steps,
            max_tokens=budget.max_tokens,
            max_wall_clock_s=budget.max_wall_clock_s,
            steps=steps,
            tokens=int(budget.tokens),
            started_at=budget.started_at,
            schema_version=budget.schema_version,
        )
        return {"budget": updated.to_dict()}
    return {}


def route_after_budget(state: Mapping[str, Any]) -> str:
    """预算后的路由：取消/超限/已终止/无计划 -> 收口；否则按能力分流。

    「已终止」要单列：工具超时之后 cursor 仍然是那条调用的下标（不推进），
    plan 也没变，只看 cursor 会算出「还有工具要跑」，于是又发一次。
    终止性错误码是唯一可靠的停止信号。
    """
    errors = " ".join(state.get("errors") or [])
    if ERROR_CANCELLED in errors or _is_terminal(state):
        return ROUTE_DONE
    plan = list(state.get("plan") or [])
    cursor = int(state.get("cursor") or 0)
    if cursor >= len(plan):
        return ROUTE_DONE
    call = ToolCall.from_dict(plan[cursor])
    # 角色守卫（执行面）：计划里可以排出一个工具，但当前角色未必有权碰它。
    # 只拦「只读类」调用：读数据这件事角色表说了算，越权读必须在执行前拦下，
    # 也不该给它发审批单（看不见的工具不该走到人工确认）。
    # 有副作用的调用（写盘、外发、代码执行）不在这里分流，照常走 request_approval：
    # 它们本来就靠审批账本、幂等键与出件箱留痕，提前分流反而会让动作「什么都没留下」。
    # 被拒的读调用由 role_denied_node 记账，不进 execute_tool，也就不会碰真注册表的 call_log。
    # 守卫只认「角色视图」：只有 RoleRegistry 这种对象才代表一个角色。
    # 没挂角色视图的工具对象（假注册表、自定义实现）不在这里猜权限，
    # 交给执行面：真注册表对未登记工具自然给 TOOL_NOT_ALLOWED / TOOL_UNKNOWN。
    from pq_agent.roles.role_registry import RoleRegistry as _RoleRegistry

    tools = _ROUTING_TOOLS.get()
    if isinstance(tools, _RoleRegistry) and str(call.capability) in READ_CAPABILITIES and _role_denies(tools, call):
        return ROUTE_ROLE_DENIED
    if call.capability in READ_CAPABILITIES:
        return ROUTE_READ
    return ROUTE_APPROVAL


# 路由阶段能看到的工具对象。LangGraph 的条件边函数只会收到 state，拿不到 GraphDeps，
# build_graph 在装配时把 deps.tools 放进这里。用 contextvar 而不是模块级可变变量，
# 是为了让同一个进程里两张图（例如演示脚本同时开 analyst 与 operator）互不串味。
_ROUTING_TOOLS: contextvars.ContextVar[Any] = contextvars.ContextVar("pq_agent_routing_tools", default=None)


def _requires_approval_only(tools: Any, call: ToolCall) -> bool:
    """这个工具是不是「角色表不管、只靠人工确认把关」的那一类。

    判定只看两件事，都从工具规格读，不另抄一份表：
    - 能力是 control_write（写的是控制面：推送预览、回执文件，不是业务文件、不对外发送）；
    - 规格声明了 requires_approval（没有人工确认就执行不了）。

    为什么需要这条旁路：control_write 刻意不属于任何角色（A5-01 的裁定），
    如果角色守卫把它整类拦掉，supervisor 连「生成预览 → 等人批 → 执行」这条主流程都走不通，
    审批与断点恢复也就无从演示。放行它不会放宽隔离：
    它仍然必须走 request_approval，仍然受 arguments_hash / 幂等 / 出件箱约束。
    业务写（business_write）与外发（notify_external）不在这条旁路上，越权照拦。
    """
    if str(getattr(call, "capability", "")) != "control_write":
        return False
    getter = getattr(tools, "get", None)
    if not callable(getter):
        return False
    try:
        spec = getter(call.tool_name)
    except Exception:  # noqa: BLE001 - 取不到规格就不放行，fail-closed
        return False
    return bool(getattr(spec, "requires_approval", False))


def _role_denies(tools: Any, call: ToolCall) -> bool:
    """当前角色能不能碰这个工具。判断依据是注入的工具对象自己算出来的可见面。

    为什么用「名字在不在可见面里」而不是另写一份能力表：
    能力表已经有两份（roles.contracts 的 ROLE_CAPABILITIES 与各 ToolSpec 的 capability），
    再抄第三份，迟早会三处不一致。这里只问工具对象「你给当前角色的名单里有没有它」。

    没有 names() 的工具对象（自定义实现）一律返回 False（不在这里拦）：
    真正的拦截在调用时由 RoleRegistry 做，这里只是提前分流。
    """
    names_getter = getattr(tools, "names", None)
    if not callable(names_getter):
        return False
    try:
        names = set(str(item) for item in names_getter())
    except Exception:  # noqa: BLE001 - 取不到名单就不拦，交给执行面
        return False
    return str(call.tool_name) not in names


def role_denied_node(state: Mapping[str, Any], deps: GraphDeps) -> dict[str, Any]:
    """节点：计划里的工具被角色守卫拒绝。

    这个节点的存在本身就是隔离生效的证据：被拒的调用**不进 execute_tool**，
    所以真注册表的 call_log 不会多一行，审批账本也不会多一张待批单。
    唯一的变化是 errors 多一条 ROLE_CAPABILITY_DENIED、cursor 前进一格，
    这样计划里剩下的工具还能继续被逐个检查（有的能碰、有的不能碰）。
    """
    call = _pending_call(state)
    cursor = int(state.get("cursor") or 0)
    view = getattr(deps, "tools", None)
    role = str(getattr(view, "role", "") or "")
    capability = str(getattr(call, "capability", "") or "")

    detail = "角色 %s 不持有能力 %s，拒绝执行 %s" % (role or "（未分角色）", capability or "未知能力", call.tool_name)
    errors = list(state.get("errors") or []) + [_error_entry(ERROR_ROLE_CAPABILITY, detail, node=NODE_ROLE_DENIED)]
    return {
        "cursor": cursor + 1,
        "errors": errors,
        "messages": _append_message(state, "system", "角色拒绝：" + detail),
        # 不写 final_answer：被拒只是这一个动作不行，后面可能还有能做的；收口节点会把它讲出来。
    }


def _iso(moment: dt.datetime | None) -> str | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        raise ContractError("DATETIME_NAIVE", "审批时间必须带时区")
    return moment.isoformat()


def _pending_call(state: Mapping[str, Any]) -> ToolCall:
    plan = list(state.get("plan") or [])
    cursor = int(state.get("cursor") or 0)
    if cursor >= len(plan):
        raise ContractError("GRAPH_PLAN_EXHAUSTED", f"cursor={cursor} 已越过计划长度 {len(plan)}")
    return ToolCall.from_dict(plan[cursor])


RISK_NOTES = {
    "control_write": "会写入控制状态文件（推送预览与回执），不对外发送。",
    "business_write": "会写入业务文件（日报归档），落盘后内容即对外可见。",
    "notify_external": "会向外部通道（飞书）真实发送消息，发送后无法撤回。",
    "code_exec": "会执行模型生成的代码，风险最高，必须在沙箱内且逐次人工确认。",
}


def _action_hash_for(state: Mapping[str, Any], call: ToolCall, arguments_hash: str) -> str:
    """把一次动作压成一个哈希：工具名 + 参数哈希 + 数据快照 + 运行时。

    三个作用：人看到的审批单和真正执行的调用必须是同一个东西（防偷换）；
    进程重启后能重算出同一个值（防重放）；换了数据快照就不算同一次动作。
    """
    return compute_action_hash(
        tool_name=call.tool_name,
        arguments_hash=arguments_hash,
        data_snapshot_id=str(state.get("data_snapshot_id") or ""),
        runtime=RUNTIME_LOCAL,
        code_hash=str(state.get("code_hash") or ""),
        prompt_hash=str(state.get("prompt_hash") or ""),
    )


def _risk_note_for(call: ToolCall) -> str:
    """给审批人写清楚这次动作会动什么；能力未知时按最高风险提示，不隐瞒。"""
    base = RISK_NOTES.get(str(call.capability), "未知能力，按最高风险处理，请人工判断。")
    return call.tool_name + ": " + base


def _decision_actor(decision: Any) -> str:
    """从审批决定里取决策人；没有就记 human，不编造具体账号。"""
    if isinstance(decision, Mapping):
        return str(decision.get("actor") or "human")
    return "human"


def _idempotency_key_for(state: Mapping[str, Any], call: ToolCall) -> str:
    """算出这次提交的幂等键：同一动作重复执行必须算出同一个键。"""
    return compute_idempotency_key(
        tool_name=call.tool_name,
        action_hash=_action_hash_for(state, call, tool_call_arguments_hash(call)),
        thread_id=str(state.get("thread_id") or ""),
        as_of=str(state.get("as_of") or ""),
        slot=str(dict(call.arguments).get("slot") or ""),
        payload_digest=compute_payload_digest(dict(call.arguments)),
        data_snapshot_id=str(state.get("data_snapshot_id") or ""),
        runtime=RUNTIME_LOCAL,
    )


def _prepare_outbox_entry(state: Mapping[str, Any], deps: GraphDeps, call: ToolCall):
    """真副作用执行前先登记出件箱：只有非 dry-run 且有副作用的调用才登记。

    返回既有条目（可能是上一轮留下的 PREPARED / SENT / UNKNOWN），
    调用方据此判断要不要跳过执行。dry-run 与只读调用一律返回 None。
    """
    outbox = deps.outbox
    if outbox is None or bool(call.dry_run):
        return None
    if str(call.capability) not in APPROVAL_REQUIRED_CAPABILITIES:
        return None
    entry, _created = outbox.prepare(
        thread_id=str(state.get("thread_id") or ""),
        call_id=call.call_id,
        tool_name=call.tool_name,
        phase="commit",
        idempotency_key=_idempotency_key_for(state, call),
        item_keys=[call.call_id],
        now=deps.now(),
    )
    return entry


def _settle_tool_step(
    state: Mapping[str, Any],
    deps: GraphDeps,
    update: dict[str, Any],
    *,
    steps: int,
    tokens_added: int,
    phase_after: str,
) -> dict[str, Any]:
    """一次工具执行的统一收尾：把预算写回 state，超限就地终止。

    顺序固定：
    1. 先算出执行后的预算（steps 已 +1、tokens 已累加、墙钟按当前时刻重算）；
    2. 写回 budget —— 即使这次没超限也要写，否则「跑了几个工具」永远是 0；
    3. 超限时就地把 final_answer 改成终止性结论，并记 BUDGET_EXCEEDED；
       已完成的结果、计划与 cursor 一律保留，只停止后续工具。
    """
    deps.phase = phase_after
    current = _budget_of(state)
    steps_after = max(int(steps), int(current.steps))
    tokens_after = int(current.tokens) + max(0, int(tokens_added))
    updated = Budget(
        max_steps=current.max_steps,
        max_tokens=current.max_tokens,
        max_wall_clock_s=current.max_wall_clock_s,
        steps=steps_after,
        tokens=tokens_after,
        started_at=current.started_at,
        schema_version=current.schema_version,
    )
    settled = dict(update)
    settled["budget"] = updated.to_dict()
    verdict = updated.evaluate(deps.now())
    if not verdict["ok"]:
        terminal = _budget_exceeded_update(state, steps=steps_after, reason=verdict["reason"])
        settled["budget"] = terminal["budget"]
        settled["errors"] = terminal["errors"]
        _annotate_terminal(state, settled, steps=steps_after)
        return settled
    # 终止性错误出现时立刻收手：工具超时（不知道它做到哪一步）不许接着跑下一个工具。
    if any(code in " ".join(str(item) for item in (settled.get("errors") or [])) for code in TERMINAL_ERROR_CODES):
        deps.phase = PHASE_IDLE
        _annotate_terminal(state, settled, steps=steps_after)
    return settled


def _terminal_summary(state: Mapping[str, Any]) -> str:
    """终止时给人看的一句话：完成了几个、还剩几个没做。数字全部来自 state，不估算。"""
    plan = list(state.get("plan") or [])
    results = list(state.get("tool_results") or [])
    done = [str(item.get("tool_name") or "") for item in results if isinstance(item, Mapping)]
    left = max(0, len(plan) - int(state.get("cursor") or 0))
    failed = [str(item.get("tool_name") or "") for item in results if isinstance(item, Mapping) and not item.get("ok")]
    return "已完成步骤保留 %d 个（%s）；失败 %d 个（%s）；后续 %d 个工具不再执行。" % (
        len(done),
        ", ".join(done) or "无",
        len(failed),
        ", ".join(failed) or "无",
        left,
    )


def _annotate_terminal(state: Mapping[str, Any], settled: dict[str, Any], *, steps: int) -> None:
    """把「为什么停、停在哪」写进 errors 通道，**不写 final_answer**。

    为什么不写 final_answer：LangGraph 的通道是后写覆盖，节点的最终 update 会覆盖同一节点
    更早的部分；而收口节点 compose_answer 本来就应该把「失败的工具 + 错误码」讲出来。
    终止性理由放在 errors 里，收口节点会把它原样带进最终回答，也不会被覆盖。
    """
    errors = list(settled.get("errors") or [])
    plan_len = len(list(state.get("plan") or []))
    cursor = int(settled.get("cursor") or state.get("cursor") or 0)
    done = len(list(settled.get("tool_results") or []))
    left = max(0, plan_len - cursor)
    note = "终态：已保留 %d 个已完成步骤，%d 个工具不再执行" % (done, left)
    if not any(note in str(item) for item in errors):
        errors.append(_error_entry("RUN_TERMINATED", note, node=NODE_EXECUTE_TOOL))
    settled["errors"] = errors
    settled.pop("final_answer", None)
    settled["budget"] = _terminal_update(state, {"budget": settled.get("budget")}, steps=steps)["budget"]


def _settle_outbox_entry(deps: GraphDeps, entry, result: ToolResult, *, raised: bool, capability: str, errors: list) -> None:
    """把执行结果写回出件箱。

    成功记 SENT；明确失败记 FAILED；外发类调用中途抛异常时结果不可知，
    必须记 UNKNOWN，绝不允许自动重发（重发就等于给用户重复推送）。
    """
    if entry is None or deps.outbox is None:
        return
    if result.ok:
        status = "SENT"
    elif raised and str(capability) in NOT_RETRYABLE_CAPABILITIES:
        status = "UNKNOWN"
    else:
        status = "FAILED"
    try:
        deps.outbox.mark_item(entry.entry_id, 0, status, reason="" if result.ok else str(result.error_code or ""))
    except ContractError as exc:
        # 回执写不进去本身就是不一致状态，必须冒泡成错误，不能悄悄咽下
        errors.append(_error_entry(ERROR_OUTBOX_SETTLE_FAILED, _strip_code_prefix(exc), node="execute_tool"))


def request_approval_node(state: Mapping[str, Any], deps: GraphDeps) -> dict[str, Any]:
    """节点 6：有副作用的工具执行前停下来等人批。

    这里用 LangGraph 的 interrupt：它会**把整个图的状态存进 checkpoint 并挂起**，
    进程退出后仍可用同一个 thread_id + Command(resume=...) 从这一行继续。

    审批记录必须绑定 arguments_hash，否则会退化成"批了 A 却执行了 B"。
    """
    call = _pending_call(state)
    # A4-02：人工确认 span。这里**刻意不用 with**——interrupt() 会抛 GraphInterrupt 穿出函数，
    # with 会把它标成 error 再抛，于是「正在等人批」在链路上看起来像一次故障。语义反了。
    approval_span_cm = None
    approval_span_obj = None
    approval_tracer = getattr(deps, "tracer", None)
    if approval_tracer is not None:
        try:
            from pq_agent.observability.apm_spans import approval_span

            approval_span_cm = approval_span(
                approval_tracer,
                tool_name=call.tool_name,
                capability=call.capability,
                decision="",
            )
            approval_span_obj = approval_span_cm.__enter__()
        except Exception:
            approval_span_cm = None
            approval_span_obj = None
    arguments_hash = tool_call_arguments_hash(call)
    thread_id = str(state.get("thread_id") or "")
    # approval_id 里带 thread_id：同一线程 resume 复用旧记录（见 ApprovalStore.request 的幂等分支），
    # 不同线程发起同一动作必须重新人工确认，而不是撞上旧审批被永久拒绝。
    approval_id = "a" + sha256_text(f"{thread_id}:{call.call_id}:{arguments_hash}")[:12]
    action_hash = _action_hash_for(state, call, arguments_hash)
    preview = _safe_arguments_preview(call.arguments)
    risk_note = _risk_note_for(call)
    store = deps.approval_store
    expires_at = ""
    if store is not None:
        # 先把待批动作写进持久审批账本，再停图。
        # 同一 approval_id 在 resume 时复用旧记录（见 ApprovalStore.request），
        # 这样进程崩了重启，人批过的审批不会凭空消失，也不会被重置成 pending。
        record = store.request(
            thread_id=thread_id,
            call_id=call.call_id,
            tool_name=call.tool_name,
            capability=call.capability,
            arguments_hash=arguments_hash,
            action_hash=action_hash,
            preview=preview,
            risk_note=risk_note,
            data_snapshot_id=str(state.get("data_snapshot_id") or ""),
            runtime=RUNTIME_LOCAL,
            approval_id=approval_id,
            now=deps.now(),
        )
        approval_id = record.approval_id
        expires_at = record.expires_at
    else:
        expires_at = _iso(deps.now() + dt.timedelta(seconds=APPROVAL_TTL_S))

    decision: Any = None
    if deps.approver is not None:
        # 有注入的审批器（测试/演示用），直接问它，不走 interrupt
        decision = deps.approver(dict(state), call)
    else:
        payload = {
            "kind": "approval_request",
            "approval_id": approval_id,
            "thread_id": thread_id,
            "call_id": call.call_id,
            "tool_name": call.tool_name,
            "capability": call.capability,
            "action_hash": action_hash,
            "arguments_hash": arguments_hash,
            "arguments_preview": preview,
            "risk_note": risk_note,
            "expires_at": expires_at,
            "dry_run": bool(call.dry_run),
            "question": f"是否允许执行 {call.tool_name}（能力 {call.capability}）？",
        }
        try:
            decision = interrupt(payload)
        except GraphInterrupt:
            # 挂起等人批：span 以 ok 收尾，decision 留空表示「还没批」，
            # 再把挂起信号原样抛回给 LangGraph——它要靠这个信号存 checkpoint 并停图。
            _close_approval_span(approval_span_cm, approval_span_obj, decision="")
            raise

    approvals = [dict(item) for item in (state.get("approvals") or [])]
    now = deps.now()
    approved, reason = _interpret_decision(decision, arguments_hash)
    decided_actor = _decision_actor(decision)
    if approved and isinstance(decision, Mapping):
        seen_action = decision.get("action_hash")
        if seen_action is not None and str(seen_action) != action_hash:
            approved = False
            reason = "审批绑定的动作哈希与当前重算不一致，拒绝执行"
    errors = list(state.get("errors") or [])

    if store is not None:
        try:
            store.decide(
                approval_id,
                approved=approved,
                actor=decided_actor,
                thread_id=thread_id,
                action_hash=action_hash,
                reason=reason,
                now=now,
            )
            if approved:
                # CAS 一次性消费：同一条审批被第二次执行时必然失败，
                # 这是重放防护的最后一道闸门。
                store.consume(approval_id, thread_id=thread_id, action_hash=action_hash, actor=decided_actor, now=now)
        except ContractError as exc:
            approved = False
            reason = "审批落库失败，按 fail-closed 拒绝：" + _strip_code_prefix(exc)
            errors.append(_error_entry(ERROR_APPROVAL_STORE_FAILED, _strip_code_prefix(exc), node="request_approval"))

    record = Approval(
        approval_id=approval_id,
        call_id=call.call_id,
        tool_name=call.tool_name,
        arguments_hash=arguments_hash,
        capability=call.capability,
        status="approved" if approved else "rejected",
        requested_at=_iso(now),
        decided_at=_iso(now),
        actor=decided_actor,
        reason=reason,
    )
    approvals.append(record.to_dict())
    update: dict[str, Any] = {
        "approvals": approvals,
        "pending_approval": None,
        "messages": _append_message(state, "system", f"审批 {call.tool_name}={record.status}"),
        "errors": errors,
    }
    if not approved:
        # 被拒绝：跳过这个工具，cursor 前进，并记下原因（不静默丢弃）
        update["cursor"] = int(state.get("cursor") or 0) + 1
        update["errors"] = errors + [
            _error_entry(ERROR_APPROVAL_REJECTED, f"{call.tool_name} 被拒绝：{reason}", node="request_approval")
        ]
    _close_approval_span(approval_span_cm, approval_span_obj, decision=record.status)
    return update


def _safe_arguments_preview(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """给审批人看的参数预览：只保留短标量，避免把整段文件内容或密钥抛到界面上。"""
    preview: dict[str, Any] = {}
    for key, value in (arguments or {}).items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            text = str(value)
            preview[str(key)] = text[:120]
        else:
            preview[str(key)] = f"<{type(value).__name__}>"
    return preview


def _interpret_decision(decision: Any, arguments_hash: str) -> tuple[bool, str]:
    """解析人类决定。

    三条 fail-closed 规则：
    1. 没给决定（None / 空）＝拒绝，不是默认通过；
    2. 明确 approved=False ＝拒绝；
    3. 带回 arguments_hash 且与当前不一致 ＝拒绝（防止批 A 执行 B）。
    """
    if decision is None:
        return False, "未收到审批决定（超时或缺失），按 fail-closed 处理"
    if isinstance(decision, bool):
        return decision, "直接布尔决定" if decision else "人工拒绝"
    if not isinstance(decision, Mapping):
        return False, f"审批决定格式非法：{type(decision).__name__}"
    seen_hash = decision.get("arguments_hash")
    if seen_hash is not None and str(seen_hash) != arguments_hash:
        return False, "审批绑定的参数哈希与当前调用不一致，拒绝执行"
    approved = decision.get("approved")
    if approved is None:
        return False, "审批决定缺少 approved 字段，按 fail-closed 处理"
    if not isinstance(approved, bool):
        return False, f"approved 需要布尔值，实际 {type(approved).__name__}"
    return approved, str(decision.get("reason") or ("人工同意" if approved else "人工拒绝"))


def execute_tool_node(state: Mapping[str, Any], deps: GraphDeps) -> dict[str, Any]:
    """节点 7：执行当前 cursor 指向的工具，然后 cursor+1。

    失败处理原则：
    - 工具不在注册表里 = 计划本身有问题，记 TOOL_UNKNOWN 并前进（不重试）；
    - 工具抛错 = 记 TOOL_FAILED，**外发类能力绝不自动重试**（会重复推送）；
    - 只读类失败可以记录后继续，让后面的步骤仍有信息可用。
    """
    tools = _require_dep(deps.tools, "tools")
    call = _pending_call(state)
    cursor = int(state.get("cursor") or 0)
    errors = list(state.get("errors") or [])
    results = [dict(item) for item in (state.get("tool_results") or [])]
    start = deps.now()

    # 「再执行一个工具就超预算」要在真执行之前拦住：
    # 先在内存里预判一次，超了就直接收口，绝不多跑一个工具再报错。
    # 做完预判之后所有分支（不在注册表、幂等命中、真执行）都必须走这里收尾，
    # 否则「cursor 前进了、steps 没动」，下一轮的估算会越来越松。
    projected = int(state.get("cursor") or 0) + 1
    steps_used = _step_of(state, _budget_of(state))
    deps.phase = PHASE_EXECUTING
    step_started = _time.monotonic()

    def _finish(update: dict[str, Any], *, tokens_added: int = 0) -> dict[str, Any]:
        if _is_terminal(state):
            # 上游已经判过终止（例如节点超时、预算超限）：不要再补一条 TOOL_TIMEOUT，
            # 也不能再返回一个会让图继续往下走的 update。把这一轮的失败记录原样收进来就停。
            return _terminal_update(state, update, steps=steps_used)
        remaining = projected < len(list(state.get("plan") or []))
        return _settle_tool_step(
            state,
            deps,
            update,
            steps=steps_used,
            tokens_added=tokens_added,
            phase_after=PHASE_PLANNED if remaining else PHASE_IDLE,
        )

    names = set(tools.names()) if hasattr(tools, "names") else set()
    if call.tool_name not in names and not _requires_approval_only(tools, call):
        # 区分两种「不在名单里」：
        # - 工具压根没登记：计划本身有问题，记 TOOL_UNKNOWN；
        # - 工具登记了，但当前角色视图里没有：记 ROLE_CAPABILITY_DENIED。
        # 混成一个码，事后分不清「计划排错了」还是「权限不够」。
        role = str(getattr(tools, "role", "") or "")
        raw_getter = getattr(getattr(tools, "_registry", None), "get", None)
        exists_in_raw = False
        # 只有真注册表（ToolRegistry）才谈得上「登记过、但角色看不见」。
        # 别的自定义注册表若碰巧也带 role 属性，不能拿它的名单去猜「存在与否」。
        from pq_agent.tools.registry import ToolRegistry as _ToolRegistry

        underlying = getattr(tools, "_registry", None)
        if callable(raw_getter) and isinstance(underlying, _ToolRegistry):
            spec_in_raw = underlying._specs.get(str(call.tool_name))
            # 只靠人工确认把关的工具（control_write）是「角色看不见、但合法」的那一类，
            # 它必须真的走到执行面去过审批，不能在这里被当成越权拒掉。
            if spec_in_raw is not None and _requires_approval_only(tools, call):
                exists_in_raw = False
            else:
                exists_in_raw = spec_in_raw is not None
        if exists_in_raw:
            code = ERROR_ROLE_CAPABILITY
            detail = "角色 %s 不持有能力 %s，拒绝执行 %s" % (role, call.capability, call.tool_name)
        else:
            code = ERROR_TOOL_UNKNOWN
            detail = "工具 %s 不在注册表中" % call.tool_name
        result = ToolResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            ok=False,
            error_code=code,
            summary=detail,
        )
        results.append(result.to_dict())
        errors.append(_error_entry(code, detail, node="execute_tool"))
        return _finish({"cursor": cursor + 1, "tool_results": results, "errors": errors})

    entry = None
    try:
        entry = _prepare_outbox_entry(state, deps, call)
    except ContractError as exc:
        if exc.code == ERROR_OUTBOX_UNKNOWN:
            # 上一次外发结果不可知：绝不自动重发，交回给人处理
            result = ToolResult(
                call_id=call.call_id,
                tool_name=call.tool_name,
                ok=False,
                error_code=ERROR_OUTBOX_UNKNOWN,
                summary=_strip_code_prefix(exc)[:300],
            )
            results.append(result.to_dict())
            errors.append(_error_entry(ERROR_OUTBOX_UNKNOWN, _strip_code_prefix(exc), node="execute_tool"))
            return _finish({"cursor": cursor + 1, "tool_results": results, "errors": errors})
        raise
    if entry is not None and str(entry.status) == "SENT":
        # 幂等命中：这个动作已经成功提交过，说清原因，不再执行一次
        result = ToolResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            ok=False,
            error_code=ERROR_ALREADY_COMMITTED,
            summary="同一动作已经有成功回执，按幂等拒绝重复执行",
        )
        results.append(result.to_dict())
        errors.append(_error_entry(ERROR_ALREADY_COMMITTED, call.tool_name, node="execute_tool"))
        return _finish({"cursor": cursor + 1, "tool_results": results, "errors": errors})

    # A4-02：工具调用 span。dry_run 必须进属性——
    # 否则事后翻 trace 分不清「真的发出去了」和「只是预览了一遍」。
    tool_span_cm = None
    tool_span_obj = None
    tool_tracer = getattr(deps, "tracer", None)
    if tool_tracer is not None:
        try:
            from pq_agent.observability.apm_spans import tool_span

            tool_span_cm = tool_span(
                tool_tracer,
                tool_name=call.tool_name,
                capability=call.capability,
                dry_run=bool(call.dry_run),
            )
            tool_span_obj = tool_span_cm.__enter__()
        except Exception:
            tool_span_cm = None
            tool_span_obj = None
    raised = False
    # 「这一次执行是在什么时候开始的」要挂在 deps 上，节点外层才能判断结果是否已经过时。
    # 为什么需要它：工具超时之后，被丢弃的那个线程还会继续跑到自然结束，并写回它自己的 update。
    # 那段 update 也会走一遍 _wrap，如果外层不知道「这是上一轮的迟到结果」，就会把
    # 已经判死的运行重新推着往下跑（实测：超时后第二个工具照样执行了）。
    runtime_calls = getattr(deps, "runtime_calls", None)
    if isinstance(runtime_calls, dict):
        runtime_calls["last_started_at"] = _time.monotonic()
    try:
        # 工具层的 approval_token 是「参数绑定令牌」，不是权限凭证：
        # 权限凭证是审批存储里的人类决策，图只有在 request_approval 之后才会走到这里。
        # 令牌必须和 registry 自己清洗后的参数一致，所以用 preview 向 registry 取，
        # 绝不在图里自己拼哈希（默认值填充与类型转换会让两边算出的值不一致）。
        token = None
        if call.capability in APPROVAL_REQUIRED_CAPABILITIES:
            token = str(tools.preview(call.tool_name, dict(call.arguments)).get("arguments_hash") or "")
        tool_timeout = tool_timeout_of(tools, call.tool_name)
        # 为什么拆成两段（取得规格 / 真正调用）：
        # 工具超时只可能发生在「调用」这一段的 waiter 上。如果调用方已经先等过了
        # （比如节点自己的超时守卫），waiter 已经标记过超时，这里就不该再被误判成工具超时。
        invoke_result = {"state": RUNNING, "value": None}

        def _invoke() -> Any:
            value = tools.invoke(
                call.tool_name,
                dict(call.arguments),
                approval_token=token,
                dry_run=bool(call.dry_run),
            )
            # 真跑完了就把状态翻成 RETURNED：即便外层已经判定超时，这个事实也要留下来，
            # 否则「工具其实做完了」会被永远写成「超时失败」。
            invoke_result["state"] = RETURNED
            return value

        outcome = _call_with_timeout(_invoke, timeout_s=tool_timeout, waiter=invoke_result)
        if outcome["state"] == TIMED_OUT and outcome.get("timed_out_now"):
            # 这一次是「调用方自己等超了」：记工具超时并停手。
            # 外发类走 UNKNOWN（结果不可知），绝不重试；其余按 TOOL_TIMEOUT 记失败。
            raised = True
            result = ToolResult(
                call_id=call.call_id,
                tool_name=call.tool_name,
                ok=False,
                error_code=ERROR_TOOL_TIMEOUT,
                summary=f"{call.tool_name} 超过 {tool_timeout:.3f}s 未返回，按超时失败处理",
                duration_ms=max(0, int((deps.now() - start).total_seconds() * 1000)),
            )
            errors.append(_error_entry(ERROR_TOOL_TIMEOUT, f"{call.tool_name} 超过 {tool_timeout:.3f}s", node="execute_tool"))
        elif outcome["state"] == TIMED_OUT:
            # 调用方之前已经等过这一段（外层超时守卫），真正跑完的耗时早已超过工具时限。
            # 时限照样算违背：按超时失败记，但要写清楚是「跑完之后才发现」。
            raised = True
            result = ToolResult(
                call_id=call.call_id,
                tool_name=call.tool_name,
                ok=False,
                error_code=ERROR_TOOL_TIMEOUT,
                summary=f"{call.tool_name} 耗时超过声明时限 {tool_timeout:.3f}s，按超时失败处理",
                duration_ms=max(0, int((deps.now() - start).total_seconds() * 1000)),
            )
            errors.append(_error_entry(ERROR_TOOL_TIMEOUT, f"{call.tool_name} 超过 {tool_timeout:.3f}s", node="execute_tool"))
        elif outcome["state"] == RAISED:
            exc = outcome["error"]
            raised = True
            result = ToolResult(
                call_id=call.call_id,
                tool_name=call.tool_name,
                ok=False,
                error_code=ERROR_TOOL_FAILED,
                summary=f"{type(exc).__name__}: {str(exc)[:200]}",
                duration_ms=max(0, int((deps.now() - start).total_seconds() * 1000)),
            )
            errors.append(_error_entry(ERROR_TOOL_FAILED, f"{call.tool_name}: {type(exc).__name__}", node="execute_tool"))
        else:
            result = _normalize_result(call, outcome["value"], start, deps)
    except Exception as exc:  # noqa: BLE001 - 工具抛错要转成失败记录，不能穿透
        raised = True
        result = ToolResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            ok=False,
            error_code=ERROR_TOOL_FAILED,
            summary=f"{type(exc).__name__}: {str(exc)[:200]}",
            duration_ms=max(0, int((deps.now() - start).total_seconds() * 1000)),
        )
        errors.append(_error_entry(ERROR_TOOL_FAILED, f"{call.tool_name}: {type(exc).__name__}", node="execute_tool"))

    if tool_span_cm is not None:
        # 工具失败在 span 上标 error，但**不重新抛**：失败已经被记进 errors 并走失败边，
        # 观测层只负责让它可见。
        try:
            if tool_span_obj is not None:
                tool_span_obj.set_attribute("tool.ok", bool(result.ok))
                tool_span_obj.set_attribute("status", "ok" if result.ok else "error")
                if not result.ok:
                    tool_span_obj.set_attribute("error_code", str(result.error_code or ERROR_TOOL_FAILED))
                tool_span_obj.set_attribute("tool.result_count", int(len(result.data or {})))
        except Exception:
            pass
        try:
            tool_span_cm.__exit__(None, None, None)
        except Exception:
            pass

    _settle_outbox_entry(deps, entry, result, raised=raised, capability=call.capability, errors=errors)

    results.append(result.to_dict())
    update: dict[str, Any] = {
        "cursor": cursor + 1,
        "tool_results": results,
        "errors": errors,
        "messages": _append_message(state, "tool", f"{call.tool_name} ok={result.ok} {result.summary[:200]}"),
    }
    tokens_added = 0 if outcome["state"] != RETURNED else tool_tokens_of(outcome["value"])
    # 每次工具执行后都要更新 budget：步数 +1、token 按工具回传累加、墙钟按当前时刻重算。
    # 超限就地终止（保留已完成步骤），不再把「已经超了」的消息带到下一个工具去。
    return _finish(update, tokens_added=tokens_added)


TIMED_OUT = "timeout"
RAISED = "raised"
RETURNED = "returned"
# 「调用方还没等过」——只在 _call_with_timeout 内部使用。
RUNNING = "running"


def _call_with_timeout(func: Callable[[], Any], *, timeout_s: float, waiter: dict[str, Any] | None = None) -> dict[str, Any]:
    """在守护线程里跑一次调用，最多等 timeout_s 秒。返回 {state, value, error, timed_out_now}。

    timed_out_now 表示「是这一次等待等超了」，用来区分两种完全不同的情况：
    - 外层已经先等过（例如节点超时守卫先顶住了），这一次等待只是顺手收尾；
    - 这一次等待才等超。
    把两者混成一个结论，会把「没超时的工具」记成超时，证据就失真了。

    三件事必须说清楚：
    1. 到点没返回就是 TIMED_OUT —— 调用方据此记为失败，绝不放行下一步。
    2. 先到点、随后线程才抛异常，仍然按 TIMED_OUT 报：调用方那一刻已经按超时处理了，
       事后再改口说「其实它是抛错了」会让状态和决定对不上。
    3. 超时之后仍然等线程自然结束再返回。线程杀不掉（Python 没有强杀线程的能力），
       但可以让它跑完再往下走，这样「超时的写动作」不会和「下一个写动作」并发执行。
       等多久有上限，超过就不等了——不能让一个卡死的工具把整张图挂住，那正是本次要修的问题。
    """
    box: dict[str, Any] = {}
    lock = threading.Lock()

    def runner() -> None:
        try:
            value = func()
        except BaseException as exc:  # noqa: BLE001 - 原样交给调用方判断
            with lock:
                box.setdefault("state", RAISED)
                box["error"] = exc
            return
        with lock:
            box.setdefault("state", RETURNED)
            box["value"] = value

    already_timed_out = bool(waiter) and waiter.get("state") == TIMED_OUT
    worker = threading.Thread(target=runner, name="pq-agent-tool-call", daemon=True)
    worker.start()
    worker.join(timeout=float(timeout_s))
    # 「这一次等待就等超了」才算调用方亲自判定的超时。之前已经被外层判过一次超时的，
    # 这里只是在收尾：结论仍然是超时，但不是这次等出来的。
    timed_out_now = not already_timed_out
    if worker.is_alive():
        # 再给它一点时间把已产生的副作用收尾，但绝不无限等。
        worker.join(timeout=WORKER_JOIN_GRACE_S)
        return {
            "state": TIMED_OUT,
            "value": None,
            "error": None,
            "abandoned": bool(worker.is_alive()),
            "timed_out_now": bool(timed_out_now),
        }
    with lock:
        result = dict(box)
    result.setdefault("state", TIMED_OUT)
    result.setdefault("value", None)
    result.setdefault("error", None)
    result["timed_out_now"] = bool(timed_out_now)
    return result


def tool_timeout_of(tools: Any, tool_name: str) -> float:
    """取工具声明的超时。取不到规格时用图级默认值，不猜、不放大。"""
    try:
        spec = tools.get(tool_name)
    except Exception:  # noqa: BLE001 - 规格取不到不是这里该处理的错误
        return float(DEFAULT_NODE_TIMEOUT_S)
    try:
        value = float(getattr(spec, "timeout_s", 0) or 0)
    except (TypeError, ValueError):
        return float(DEFAULT_NODE_TIMEOUT_S)
    return value if value > 0 else float(DEFAULT_NODE_TIMEOUT_S)


def tool_tokens_of(value: Any) -> int:
    """取工具回传的 token 用量。没有就当 0 计入，绝不估算。

    顺序：ToolResult / Mapping 的 tokens_used → data.tokens_used。
    registry.invoke 返回的是工具原始结果 dict，图会再包一层 ToolResult，
    所以一层 ToolResult 包一层结果 dict 的形状也要认。
    """
    total = 0
    for node in (value, getattr(value, "data", None)):
        if isinstance(node, Mapping):
            total = max(total, _as_int(node.get("tokens_used")))
            inner = node.get("data")
            if isinstance(inner, Mapping):
                total = max(total, _as_int(inner.get("tokens_used")))
    return total


def _as_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def _normalize_result(call: ToolCall, raw: Any, start: dt.datetime, deps: GraphDeps) -> ToolResult:
    """把工具返回值统一成 ToolResult。已经是 ToolResult 的直接用，否则包装。"""
    duration = max(0, int((deps.now() - start).total_seconds() * 1000))
    if isinstance(raw, ToolResult):
        return raw
    if isinstance(raw, Mapping):
        ok = bool(raw.get("ok", True))
        return ToolResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            ok=ok,
            summary=str(raw.get("summary") or "")[:2000],
            error_code=(None if ok else str(raw.get("error_code") or ERROR_TOOL_FAILED)),
            data=dict(raw.get("data") or {}) if ok else {},
            degraded=bool(raw.get("degraded", False)),
            duration_ms=duration,
        )
    return ToolResult(
        call_id=call.call_id,
        tool_name=call.tool_name,
        ok=True,
        summary=str(raw)[:2000],
        duration_ms=duration,
    )


def _citation_lines(citations: Sequence[Mapping[str, Any]]) -> tuple[list[str], list[str]]:
    """把引用渲染成给人看的清单。

    返回 (行清单, 失败原因清单)。不合合同的引用不静默跳过 —— 它会变成 CITATION_INVALID，
    因为「引用格式坏了」和「这次没有引用」是两件事，混在一起就查不出问题。
    """
    lines: list[str] = []
    problems: list[str] = []
    for index, item in enumerate(citations, start=1):
        try:
            citation = Citation.from_dict(dict(item))
        except Exception as exc:  # noqa: BLE001 - 合同校验失败就是引用不合法
            problems.append("%s: %s" % (type(exc).__name__, exc))
            continue
        label = "%s:%d-%d" % (citation.source_path, citation.line_start, citation.line_end)
        title = " (%s)" % citation.title if citation.title else ""
        lines.append("[%d] %s%s" % (index, label, title))
    return lines, problems

def compose_answer_node(state: Mapping[str, Any], deps: GraphDeps) -> dict[str, Any]:
    """节点 8：收口。把"意图 + 数据 + 工具结果 + 错误"讲成一段人话。

    关键约束：**只复述工具返回的事实，不添加工具没给过的数字**。
    没有主升就输出"暂无"（项目红线）。
    """
    errors = list(state.get("errors") or [])
    intent = str(state.get("intent") or INTENT_FALLBACK)
    results = list(state.get("tool_results") or [])
    lines: list[str] = []
    lines.append(f"意图：{intent}（来源 {state.get('intent_source')}）")
    snapshot_id = state.get("data_snapshot_id")
    lines.append(f"数据快照：{snapshot_id or '未取到（数据不齐）'}")
    ok_results = [item for item in results if item.get("ok")]
    bad_results = [item for item in results if not item.get("ok")]
    if ok_results:
        lines.append("工具结果：")
        for item in ok_results:
            lines.append(f"- {item.get('tool_name')}：{item.get('summary') or '（无摘要）'}")
    else:
        lines.append("工具结果：暂无")
    if bad_results:
        lines.append("失败的工具：")
        for item in bad_results:
            lines.append(f"- {item.get('tool_name')}：{item.get('error_code')}")
    if not state.get("data_snapshot_id"):
        lines.append("说明：本次没有拿到可用数据，因此不输出任何行情结论。")
    if errors:
        lines.append("过程记录：" + "；".join(errors[:5]))

    # A6-02：检索到的引用必须真的出现在回答里。被检索、被校验通过、却一句话都没引用，
    # 说明这次检索没有产生价值，必须说出来，而不是安静地丢掉。
    citations = [dict(item) for item in (state.get("citations") or [])]
    citation_lines, citation_problems = _citation_lines(citations)
    for problem in citation_problems:
        errors.append(_error_entry(ERROR_CITATION_INVALID, problem, node="compose_answer"))
    terminal = state.get("final_answer") or ""
    if terminal and any(code in " ".join(errors) for code in (ERROR_BUDGET_EXCEEDED, ERROR_CANCELLED, ERROR_INTENT_LOW_CONFIDENCE)):
        # 预算超限 / 取消是终止性结论，必须原样保留，不能被普通收口覆盖
        if citations:
            errors.append(_error_entry(ERROR_CITATION_UNUSED, f"终止性结论保留了原回答，{len(citations)} 条检索引用未被使用", node="compose_answer"))
        return {
            "final_answer": terminal,
            "errors": errors,
            "messages": _append_message(state, "assistant", terminal[:1000]),
        }
    if _is_terminal(state):
        # 预算超限 / 节点超时 / 工具超时：终止性理由必须排在回答最前面，
        # 不能埋在「过程记录」的最末尾（那里只显示前 5 条）。
        reasons = [str(item) for item in errors if any(code in str(item) for code in TERMINAL_ERROR_CODES)]
        lines.insert(0, "执行已终止：" + "；".join(reasons[:3]))
        lines.insert(1, _terminal_summary(state))
    if citation_lines:
        lines.append("引用：")
        lines.extend(citation_lines)
    elif citations:
        errors.append(_error_entry(ERROR_CITATION_UNUSED, f"检索到 {len(citations)} 条引用，但没有任何一条能作为引用展示", node="compose_answer"))
    answer = "\n".join(lines)
    return {
        "final_answer": answer,
        "errors": errors,
        "messages": _append_message(state, "assistant", answer[:1000]),
    }


def handle_error_node(state: Mapping[str, Any], deps: GraphDeps) -> dict[str, Any]:
    """节点 9：失败边。异常不会让整张图崩掉，而是转成可解释的收口。"""
    raw = state.get("_node_error")
    detail = "未知节点异常"
    if isinstance(raw, Mapping):
        detail = f"{raw.get('node')} 节点失败：{raw.get('type')}: {raw.get('message')}"
    errors = list(state.get("errors") or []) + [_error_entry(ERROR_GRAPH_INTERNAL, detail, node="handle_error")]
    return {
        "errors": errors,
        "final_answer": f"执行中断：{detail}",
    }


# --------------------------------------------------------------------------
# 图装配：build_graph / run_graph / resume_graph
# --------------------------------------------------------------------------


def _graph_version(state) -> str:
    """图版本既可能来自 state（正常路径），也可能来自模块常量（非常路径）。"""
    text = str(state.get("graph_version") or "")
    if text:
        return text
    from pq_agent.state import GRAPH_VERSION

    return str(GRAPH_VERSION)


def _span_context(state, node_name: str) -> dict:
    """span 上下文：只放**允许进 telemetry 的键**，白名单与脱敏在 observability 里兜底。

    注意这里没有 prompt、没有 arguments、没有 checkpoints——不是懒，是纪律。
    """
    return {
        "agent.node": str(node_name),
        "thread_id": str(state.get("thread_id") or ""),
        "agent.mode": str(state.get("mode") or ""),
        "as_of": str(state.get("as_of") or ""),
        "graph_version": _graph_version(state),
    }


def _error_code_of(exc: BaseException | None) -> str:
    """从异常里取出错误码。ContractError 自带 .code，其余用类型名代替。"""
    if exc is None:
        return ""
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code
    return type(exc).__name__


def _finish_span(span_cm, span, span_ctx, started, deps, *, ok: bool, error_code: str, detail: str) -> None:
    """收尾 span。无论什么情况都吞掉异常：观测层不许把主流程带崩。"""
    if span_cm is None:
        return
    try:
        if span is not None:
            attributes = dict(span_ctx)
            attributes["status"] = "ok" if ok else "error"
            if error_code:
                attributes["error_code"] = str(error_code)
            if detail:
                attributes["error_type"] = str(detail)
            attributes["duration_ms"] = max(0, int((deps.now() - started).total_seconds() * 1000))
            span.set_attributes(attributes)
    except Exception:
        pass
    try:
        span_cm.__exit__(None, None, None)
    except Exception:
        pass

def _close_approval_span(span_cm, span, *, decision: str) -> None:
    """收尾人工确认 span。

    挂起等人批和「人批了」都不是错误，所以固定以 ok 收尾，只把决定记下来。
    decision 为空表示「还在等」——这本身是有用信息，不是缺失。
    """
    if span_cm is None:
        return
    try:
        if span is not None:
            span.set_attribute("approval.decision", str(decision))
            span.set_attribute("status", "ok")
    except Exception:
        pass
    try:
        span_cm.__exit__(None, None, None)
    except Exception:
        pass


def _wrap(
    node_name,
    func,
    deps,
    *,
    timeout_s: float | None = None,
    after: Callable[..., Any] | None = None,
    gate: bool = True,
):
    """把 (state, deps) 形式的节点包成 LangGraph 要的 (state) -> update。

    四件事，顺序不能变：
    0. 预算闸门：进节点先看一眼预算，超了就不进节点，直接收口（见 _budget_gate_update）。
    1. 节点超时：真正干活的部分放进守护线程，最多等 timeout_s 秒；超时记 NODE_TIMEOUT。
       request_approval 不套超时——它是 interrupt 挂起点，外面套线程会把人等审批当成故障。
    2. GraphInterrupt 必须原样抛出。它是 LangGraph 用来挂起等人批的信号，不是错误；
       被 except Exception 吃掉，人工确认就会静默失效。
    3. 其余异常转成显式失败边所需的 errors 条目，绝不让异常穿透导致图无输出。
    4. 通知 tracer（A1-06 接 OTel；这里先留最小接口）。
    """
    # 节点超时要比外层护栏早触发：外层护栏是 timeout_s + NODE_DEADLINE_SLACK_S，
    # 内层按 timeout_s 判。两层都留，是因为内层线程万一自己卡死（比如被 GIL 外的 C 调用占住），
    # 外层护栏还能保证图有输出，而不是永远等下去。
    inner_timeout = float(timeout_s) if timeout_s else None

    def runner(state):
        # 代际：每次进入 _wrap 都 +1。被超时丢弃的那段代码稍后写回时时，带回来的是旧代际。
        # 迟到结果必须被识别并原样丢回去，否则它会以「正常结果」的身份把已经终止的运行重新推起来。
        deps.current_generation = int(getattr(deps, "current_generation", 0)) + 1
        generation = deps.current_generation
        # 进入时的终止闸门：运行已经判死，就只允许写记录，绝不允许改 cursor / plan / phase。
        # 两个时点都要看：进入时（挡迟到调用）和返回时（挡自己把运行推起来）。
        # 进入时只在「已经判死」的情况下做限制，收口节点不受限：它要负责把终止理由写出来。
        entering_terminal = _is_terminal(state) and node_name != NODE_COMPOSE_ANSWER

        # 第 3 件事：通知 tracer。可观测层是**旁路**——span 出任何问题都不许影响主流程，
        # 所以这里只取一个"能 start_span 的对象"，其余（属性白名单、脱敏、降级）全在
        # pq_agent.observability 里，图本身不认识 OTel 的类型。
        span_cm = None
        span = None
        started = deps.now()
        span_ctx = _span_context(state, node_name)
        tracer = getattr(deps, "tracer", None)
        if tracer is not None:
            try:
                span_cm = tracer.start_as_current_span("graph.node")
                span = span_cm.__enter__()
            except Exception:
                span_cm = None
                span = None
        update: Any = None
        failure: BaseException | None = None
        try:
            # 收口节点不设闸门：预算超限时它正是负责把「为什么停」说出来的节点，
            # 在这里再拦一次会让整条运行没有任何输出。
            gated = _budget_gate_update(state, deps) if gate else None
            if gated is not None:
                update = gated
            else:
                outcome = _call_with_timeout(lambda: func(state, deps), timeout_s=inner_timeout) if inner_timeout else {
                    "state": RETURNED, "value": func(state, deps), "error": None
                }
                if outcome["state"] == TIMED_OUT:
                    # 节点超时 = 节点失败。绝不返回半截 update：
                    # 超时那一刻节点的局部变量已经不可信，写回一半状态比什么都不写更危险。
                    deps.phase = PHASE_IDLE
                    update = {
                        "errors": list(state.get("errors") or []) + [
                            _error_entry(ERROR_NODE_TIMEOUT, f"节点 {node_name} 超过 {float(inner_timeout):.3f}s", node=node_name)
                        ],
                        "_node_error": {"node": node_name, "type": "TimeoutError", "message": "node timeout"},
                        # 这里刻意不写 final_answer：
                        # LangGraph 的通道是「后写覆盖」，超时线程稍后返回的 update 会把这里的值冲掉，
                        # 而收口节点还要正常产出说明（失败的工具、错误码）。终止性理由走 errors，
                        # 由 compose_answer 原样带进最终回答，不依赖一个会被覆盖的字段。
                        "messages": _append_message(
                            state, "system",
                            f"节点 {node_name} 超过 {float(inner_timeout):.3f}s 未返回，按失败处理",
                        ),
                    }
                    failure = TimeoutError(f"{node_name} timeout")
                elif outcome["state"] == RAISED:
                    raise outcome["error"]
                else:
                    update = outcome["value"]
                    if after is not None:
                        after(state, update, deps)
        except GraphInterrupt:
            _finish_span(span_cm, span, span_ctx, started, deps, ok=True, error_code="", detail="graph_interrupt")
            raise
        except ContractError as exc:
            update = {
                "errors": list(state.get("errors") or []) + [_error_entry(exc.code, _strip_code_prefix(exc), node=node_name)],
                "_node_error": {"node": node_name, "type": "ContractError", "message": str(exc)[:200]},
            }
            failure = exc
        except Exception as exc:
            update = {
                "errors": list(state.get("errors") or []) + [_error_entry(ERROR_GRAPH_INTERNAL, type(exc).__name__, node=node_name)],
                "_node_error": {"node": node_name, "type": type(exc).__name__, "message": str(exc)[:200]},
            }
            failure = exc
        _finish_span(
            span_cm,
            span,
            span_ctx,
            started,
            deps,
            ok=failure is None,
            error_code=_error_code_of(failure),
            detail=type(failure).__name__ if failure is not None else "",
        )
        # 我还在跑的时候又有人进来过（= 我这份结果是迟到结果）：一个字都不能写回去。
        if generation != int(getattr(deps, "current_generation", generation)):
            return {}
        # 终止闸门：运行已经判死（节点超时 / 预算超限 / 工具超时）之后，
        # 任何节点的结果都不许再把它推起来。这里是最外一层，挡的是「判死之后才写回的东西」。
        if entering_terminal or (_is_terminal(state) and node_name != NODE_COMPOSE_ANSWER):
            forced = _force_terminal(update)
            if entering_terminal:
                # cursor 在终止时冻结：让它前进，路由就会以为「还有下一个工具」。
                forced.pop("cursor", None)
            return forced
        return update
    runner.__name__ = node_name
    return runner

def _error_first(normal):
    """条件边工厂：只要 errors 里出现内部错误码，一律转失败边。

    normal 可以是字符串（固定分支）或函数（动态分支）。
    为什么用 errors 通道而不是自定义键：AgentState 的 23 个字段由 A1-01 冻结，
    私自加通道会破坏 checkpoint 兼容；errors 本来就是声明过的通道。
    """
    def router(state):
        for entry in state.get("errors") or []:
            if isinstance(entry, str) and ERROR_GRAPH_INTERNAL in entry:
                return "error"
        return normal(state) if callable(normal) else normal
    return router


def build_graph(deps, *, checkpointer=None, node_timeout_s=DEFAULT_NODE_TIMEOUT_S):
    """装配并编译主图。checkpointer=None 时是内存态（只用于单测），生产必须传持久化 saver。

    node_timeout_s 现在是真的：每个节点最多跑这么久，超时记为 NODE_TIMEOUT 并走失败边。
    唯一的例外是 request_approval —— 它挂起等人审批的时间不该算成节点超时。
    """
    if node_timeout_s is None or float(node_timeout_s) <= 0:
        raise ContractError("GRAPH_TIMEOUT_INVALID", "node_timeout_s 必须为正数，实际 %r" % (node_timeout_s,))
    timeout_s = float(node_timeout_s)
    graph = StateGraph(AgentState)
    graph.add_node(NODE_CLASSIFY_INTENT, _wrap(NODE_CLASSIFY_INTENT, classify_intent_node, deps, timeout_s=timeout_s))
    graph.add_node(NODE_CHECK_INPUTS, _wrap(NODE_CHECK_INPUTS, check_inputs_node, deps, timeout_s=timeout_s))
    graph.add_node(NODE_RAG_RETRIEVE, _wrap(NODE_RAG_RETRIEVE, rag_retrieve_node, deps, timeout_s=timeout_s))
    graph.add_node(NODE_PLAN_TOOLS, _wrap(NODE_PLAN_TOOLS, plan_tools_node, deps, timeout_s=timeout_s, after=_after_plan_tools))
    graph.add_node(NODE_BUDGET_CHECK, _wrap(NODE_BUDGET_CHECK, budget_check_node, deps, timeout_s=timeout_s))
    # 等人审批不设超时：这里超时会把「人还没点」误判成节点故障。
    graph.add_node(NODE_REQUEST_APPROVAL, _wrap(NODE_REQUEST_APPROVAL, request_approval_node, deps))
    graph.add_node(NODE_EXECUTE_TOOL, _wrap(NODE_EXECUTE_TOOL, execute_tool_node, deps, timeout_s=timeout_s))
    graph.add_node(NODE_COMPOSE_ANSWER, _wrap(NODE_COMPOSE_ANSWER, compose_answer_node, deps, timeout_s=timeout_s, gate=False))
    graph.add_node(NODE_HANDLE_ERROR, _wrap(NODE_HANDLE_ERROR, handle_error_node, deps, timeout_s=timeout_s, gate=False))

    graph.add_edge(START, NODE_CLASSIFY_INTENT)
    graph.add_conditional_edges(NODE_CLASSIFY_INTENT, _error_first(route_after_intent), {
        "error": NODE_HANDLE_ERROR,
        "next": NODE_CHECK_INPUTS,
        ROUTE_CLARIFY: NODE_COMPOSE_ANSWER,
    })
    graph.add_conditional_edges(NODE_CHECK_INPUTS, _error_first(route_after_inputs), {
        "error": NODE_HANDLE_ERROR,
        "ready": NODE_RAG_RETRIEVE,
        ROUTE_MISSING_DATA: NODE_COMPOSE_ANSWER,
    })
    graph.add_conditional_edges(NODE_RAG_RETRIEVE, _error_first("next"), {
        "error": NODE_HANDLE_ERROR,
        "next": NODE_PLAN_TOOLS,
    })
    graph.add_conditional_edges(NODE_PLAN_TOOLS, _error_first("next"), {
        "error": NODE_HANDLE_ERROR,
        "next": NODE_BUDGET_CHECK,
    })
    graph.add_conditional_edges(NODE_BUDGET_CHECK, _error_first(route_after_budget), {
        "error": NODE_HANDLE_ERROR,
        ROUTE_DONE: NODE_COMPOSE_ANSWER,
        ROUTE_READ: NODE_EXECUTE_TOOL,
        ROUTE_APPROVAL: NODE_REQUEST_APPROVAL,
        ROUTE_ROLE_DENIED: NODE_ROLE_DENIED,
    })
    graph.add_conditional_edges(NODE_REQUEST_APPROVAL, _error_first(_route_after_approval), {
        "error": NODE_HANDLE_ERROR,
        "execute": NODE_EXECUTE_TOOL,
        "next": NODE_BUDGET_CHECK,
    })
    graph.add_conditional_edges(NODE_EXECUTE_TOOL, _error_first("next"), {
        "error": NODE_HANDLE_ERROR,
        "next": NODE_BUDGET_CHECK,
    })
    graph.add_node(NODE_ROLE_DENIED, _wrap(NODE_ROLE_DENIED, role_denied_node, deps, timeout_s=timeout_s))
    graph.add_edge(NODE_COMPOSE_ANSWER, END)
    graph.add_edge(NODE_HANDLE_ERROR, NODE_COMPOSE_ANSWER)
    # 角色守卫产生的新分支：被拒的工具直接回预算节点取下一条，绝不进 execute_tool。
    graph.add_conditional_edges(NODE_ROLE_DENIED, _error_first("next"), {
        "error": NODE_HANDLE_ERROR,
        "next": NODE_BUDGET_CHECK,
    })
    compiled = graph.compile(checkpointer=checkpointer)
    # 路由函数拿不到 deps，把工具对象挂在编译产物上，run_graph / resume_graph 会把它交给条件边用。
    compiled._pq_tools = getattr(deps, "tools", None)
    return compiled


def _route_after_approval(state):
    """审批后被拒绝的工具不再执行，直接回到预算节点取下一条。"""
    errors = " ".join(state.get("errors") or [])
    if ERROR_APPROVAL_REJECTED in errors:
        return "next"
    return "execute"


def thread_config(thread_id):
    """LangGraph 要求配置里必须有 thread_id，否则 checkpoint 无法定位会话。"""
    if not thread_id:
        raise ContractError("THREAD_ID_REQUIRED", "调用图必须提供非空 thread_id")
    return {"configurable": {"thread_id": str(thread_id)}}


def run_graph(app, state, *, thread_id=None, tracer=None):
    """首次执行。返回 (最终 state, 是否被中断)。

    tracer 是可选的（A4-02）：传了就在最外层开一个 graph.run 根 span。
    没有根 span，节点 span 在 SkyWalking 里就是一盘散沙，连不成拓扑。
    这里用惰性 import：图本身仍然不认识 OTel 类型，只在真有人传 tracer 时才碰观测层。
    """
    tid = thread_id or state.get("thread_id")
    validate_state(state, require_startable=True)
    _ROUTING_TOOLS.set(getattr(app, "_pq_tools", None))
    if tracer is None:
        out = app.invoke(dict(state), thread_config(tid))
        return out, bool(out.get("__interrupt__"))
    from pq_agent.observability.apm_spans import run_span

    with run_span(
        tracer,
        thread_id=str(tid or ""),
        mode=str(state.get("mode") or ""),
        as_of=str(state.get("as_of") or ""),
        graph_version=_graph_version(state),
    ):
        out = app.invoke(dict(state), thread_config(tid))
    return out, bool(out.get("__interrupt__"))


def resume_graph(app, decision, *, thread_id, tracer=None):
    """从断点恢复。decision 会作为 interrupt() 的返回值交给 request_approval 节点。

    A4-02：恢复是**另一次运行**，所以它有自己的一根 graph.run 根 span。
    两次运行之间靠 Link 关联（谁恢复了谁），不是父子关系——
    父子会让人以为「这是一次调用」，而实际中间隔了一次进程重启和一次人工点击。
    """
    _ROUTING_TOOLS.set(getattr(app, "_pq_tools", None))
    if tracer is None:
        out = app.invoke(Command(resume=decision), thread_config(thread_id))
        return out, bool(out.get("__interrupt__"))
    from pq_agent.observability.apm_spans import run_span

    with run_span(tracer, thread_id=str(thread_id or ""), mode="resume"):
        out = app.invoke(Command(resume=decision), thread_config(thread_id))
    return out, bool(out.get("__interrupt__"))


def pending_interrupt(app, *, thread_id):
    """读取当前挂起的中断信息（审批界面用它渲染"等谁批什么"）。"""
    snapshot = app.get_state(thread_config(thread_id))
    for task in getattr(snapshot, "tasks", ()) or ():
        for item in getattr(task, "interrupts", ()) or ():
            return {"value": item.value, "id": getattr(item, "id", "")}
    return None


def graph_structure(app):
    """导出图的结构（节点/边），供文档与验收脚本断言"图没有乱连"。"""
    try:
        drawn = app.get_graph()
    except Exception as exc:
        raise ContractError("GRAPH_STRUCTURE_UNAVAILABLE", f"{type(exc).__name__}: {exc}")
    nodes = sorted(node for node in drawn.nodes if not node.startswith("__"))
    edges = sorted(f"{edge.source}->{edge.target}" for edge in drawn.edges)
    return {"nodes": nodes, "edges": edges}


def assert_no_forbidden_tools(app, *, forbidden_prefixes=("broker_", "execution_", "risk_")):
    """静态自检：图里不许出现券商/执行/风控类工具，也不许出现 loop 命令。"""
    structure = graph_structure(app)
    bad = []
    for node in structure["nodes"]:
        low = node.lower()
        for prefix in forbidden_prefixes:
            if prefix in low:
                bad.append(node)
        if low.endswith("_loop") or "-loop" in low:
            bad.append(node)
    if bad:
        raise ContractError("GRAPH_FORBIDDEN_NODE", f"图中出现禁止节点 {sorted(set(bad))}")
    return structure
