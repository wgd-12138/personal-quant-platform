"""A4-02 span 层级与恢复链：把「一次运行」画成一棵能被机器断言的树。

为什么必须有这个模块（而不是在 graph.py 里顺手多打几个 span）：

1. **层级是可验收的合同，不是观感。**SkyWalking 的 trace waterfall 会把 span 按 parent 排成树，
   如果层级只在装配时用现成的 context manager 隐式形成，那么「tool 挂在哪个节点下」这件事
   就没有任何地方能断言。评测脚本需要一个不依赖 OTel SDK 的纯数据结构来描述这棵树。
2. **恢复链（resume link）不能靠父子关系表达。**一次被人工确认打断的运行分成两次真实执行：
   第一次跑到 request_approval 挂起，第二次是新进程 resume。它们之间**没有父子关系**
   （第二次不是第一次的子 span），只能是 OTel 的 Link。用父子关系硬凑，拓扑图上会出现
   一个永远不结束的 span，或者两次执行的耗时被算成一次。
3. **A4-01 已经证明了「没测」和「没过」必须分开。**这里沿用同一条纪律：
   `trace_is_complete()` 对缺失层级返回 missing 列表，而不是返回 False 了事——
   「少了一层 span」和「层级结构错了」是两件不同的事。

本模块不认识 OTel 的类型，只做三件事：定义层级、校验层级、描述恢复链。
真正的 span 由 graph.py / server.py 生成，属性白名单与脱敏在 observability.redaction 里兜底。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pq_agent.contracts import ContractError

SPAN_SCHEMA_VERSION = "pq_agent.apm_spans/1"

# ---------------------------- span 名字 ----------------------------

SPAN_RUN = "graph.run"
SPAN_NODE = "graph.node"
SPAN_TOOL = "tool.invoke"
SPAN_SANDBOX = "sandbox.execute"
SPAN_APPROVAL = "approval.decision"
SPAN_RAG = "rag.retrieve"
SPAN_LLM = "llm.intent"

# 一次完整运行必须出现的 span 名字。缺任何一项都算层级不完整。
REQUIRED_RUN_SPANS: tuple[str, ...] = (SPAN_RUN, SPAN_NODE)

# 层级合同：子 span 只能挂在允许的父 span 下面。
# 键是父 span 名，值是该父下允许出现的子 span 集合。
ALLOWED_EDGES: dict[str, tuple[str, ...]] = {
    SPAN_RUN: (SPAN_NODE, SPAN_TOOL, SPAN_SANDBOX, SPAN_APPROVAL, SPAN_RAG, SPAN_LLM),
    SPAN_NODE: (SPAN_TOOL, SPAN_SANDBOX, SPAN_APPROVAL, SPAN_RAG, SPAN_LLM),
    SPAN_RAG: (SPAN_TOOL,),
    SPAN_TOOL: (),
    SPAN_SANDBOX: (),
    SPAN_APPROVAL: (),
    SPAN_LLM: (),
}

# 产生副作用的 span：审计与告警都只关心这几个。
SIDE_EFFECT_SPANS: tuple[str, ...] = (SPAN_TOOL, SPAN_SANDBOX, SPAN_APPROVAL)

ERROR_SPAN_INVALID = "APM_SPAN_NAME_UNKNOWN"
ERROR_SPAN_HIERARCHY = "APM_SPAN_HIERARCHY_INVALID"
ERROR_SPAN_INCOMPLETE = "APM_SPAN_INCOMPLETE"
ERROR_LINK_INVALID = "APM_LINK_INVALID"


def is_known_span(name: str) -> bool:
    """未知 span 名一律拒绝：宁可报错，也不要让随便一个名字混进拓扑图。"""
    return str(name) in ALLOWED_EDGES


def require_known_span(name: str) -> str:
    text = str(name)
    if text not in ALLOWED_EDGES:
        raise ContractError(ERROR_SPAN_INVALID, "未知 span 名：" + text)
    return text


def edge_allowed(parent: str, child: str) -> bool:
    """父子边是否合法。未知名字一律不合法。"""
    p = str(parent)
    c = str(child)
    if p not in ALLOWED_EDGES or c not in ALLOWED_EDGES:
        return False
    return c in ALLOWED_EDGES[p]


@dataclass(frozen=True)
class SpanRecord:
    """一个 span 的最小可断言描述。不含属性值，只含结构与结论。"""

    span_id: str
    name: str
    parent_id: str = ""
    status: str = "ok"
    error_code: str = ""
    duration_ms: int = 0
    attributes: Mapping[str, Any] = field(default_factory=dict)
    links: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_known_span(self.name)
        if self.status not in ("ok", "error", "unset"):
            raise ContractError(ERROR_SPAN_HIERARCHY, "未知 span 状态：" + str(self.status))
        if int(self.duration_ms) < 0:
            raise ContractError(ERROR_SPAN_HIERARCHY, "duration_ms 不能为负")

    @property
    def has_error(self) -> bool:
        return self.status == "error"

    def to_dict(self) -> dict[str, Any]:
        return {
            "span_id": self.span_id,
            "name": self.name,
            "parent_id": self.parent_id,
            "status": self.status,
            "error_code": self.error_code,
            "duration_ms": int(self.duration_ms),
            "link_count": len(self.links),
        }


@dataclass(frozen=True)
class LinkRecord:
    """恢复链的一条边：resume 的那次执行回指到被打断的那次执行。

    为什么是 Link 而不是 parent：两次执行是**分开的两个根**，只是有因果关系。
    把 resume 的执行挂成 interrupt 那次的子 span，会让「被中断的耗时」把人工思考时间也算进去。
    """

    from_trace_id: str
    to_trace_id: str
    reason: str
    thread_id: str = ""
    approval_id: str = ""

    def __post_init__(self) -> None:
        if not self.from_trace_id or not self.to_trace_id:
            raise ContractError(ERROR_LINK_INVALID, "恢复链两端的 trace_id 都不能为空")
        if self.from_trace_id == self.to_trace_id:
            raise ContractError(ERROR_LINK_INVALID, "恢复链不能自指")
        if not self.reason:
            raise ContractError(ERROR_LINK_INVALID, "恢复链必须写明原因")

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_trace_id": self.from_trace_id,
            "to_trace_id": self.to_trace_id,
            "reason": self.reason,
            "thread_id": self.thread_id,
            "approval_id": self.approval_id,
        }

@dataclass(frozen=True)
class TraceShape:
    """一次运行的 span 树形状。用来做「层级对不对」的独立校验。"""

    trace_id: str
    spans: tuple[SpanRecord, ...] = ()
    links: tuple[LinkRecord, ...] = ()
    interrupted: bool = False
    resumed_from: str = ""

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.spans)

    def by_name(self, name: str) -> tuple[SpanRecord, ...]:
        return tuple(item for item in self.spans if item.name == str(name))

    def missing_spans(self) -> tuple[str, ...]:
        """缺失的必需层级。返回名字列表而不是布尔值——「少了什么」比「不对」有用。"""
        present = set(self.names)
        return tuple(item for item in REQUIRED_RUN_SPANS if item not in present)

    def hierarchy_errors(self) -> tuple[str, ...]:
        """层级错误列表。空元组表示层级合法。"""
        problems: list[str] = []
        by_id = {item.span_id: item for item in self.spans}
        for item in self.spans:
            if not item.parent_id:
                if item.name != SPAN_RUN:
                    problems.append("没有父 span 的 " + item.name + "（只有 " + SPAN_RUN + " 允许是根）")
                continue
            parent = by_id.get(item.parent_id)
            if parent is None:
                problems.append("span " + item.span_id + " 的父 " + item.parent_id + " 不在本次 trace 里")
                continue
            if not edge_allowed(parent.name, item.name):
                problems.append("非法层级：" + parent.name + " -> " + item.name)
        return tuple(problems)

    @property
    def complete(self) -> bool:
        return not self.missing_spans() and not self.hierarchy_errors()

    @property
    def side_effect_spans(self) -> tuple[SpanRecord, ...]:
        return tuple(item for item in self.spans if item.name in SIDE_EFFECT_SPANS)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SPAN_SCHEMA_VERSION,
            "trace_id": self.trace_id,
            "span_count": len(self.spans),
            "names": list(self.names),
            "missing_spans": list(self.missing_spans()),
            "hierarchy_errors": list(self.hierarchy_errors()),
            "complete": bool(self.complete),
            "interrupted": bool(self.interrupted),
            "resumed_from": self.resumed_from,
            "links": [item.to_dict() for item in self.links],
            "spans": [item.to_dict() for item in self.spans],
        }


def trace_shape(
    *,
    trace_id: str,
    spans: Iterable[SpanRecord],
    links: Iterable[LinkRecord] = (),
    interrupted: bool = False,
    resumed_from: str = "",
) -> TraceShape:
    """从 span 列表装配形状。重复 span_id 当场拒绝——它会静默破坏父子解析。"""
    rows = tuple(spans)
    seen: set[str] = set()
    for item in rows:
        if not item.span_id:
            raise ContractError(ERROR_SPAN_HIERARCHY, "span_id 不能为空")
        if item.span_id in seen:
            raise ContractError(ERROR_SPAN_HIERARCHY, "重复 span_id：" + item.span_id)
        seen.add(item.span_id)
    return TraceShape(
        trace_id=str(trace_id),
        spans=rows,
        links=tuple(links),
        interrupted=bool(interrupted),
        resumed_from=str(resumed_from),
    )


def shape_from_summary(summary: Mapping[str, Any]) -> TraceShape:
    """从 server 的 TraceSummary 摘要（只有节点名序列）恢复层级。

    这是给「没有真实 OTel exporter 的机器」用的路径：
    A1-07 的 trace 摘要里已经有 node_sequence，能据此生成 span 树并做层级断言，
    不必因为本机没有 Collector 就跳过层级验收。
    但必须诚实：这样造出来的 span **没有真实耗时**，duration_ms 一律为 0。
    """
    tid = str(summary.get("trace_id") or "")
    run_id = "run-root-" + tid
    spans: list[SpanRecord] = [
        SpanRecord(span_id=run_id, name=SPAN_RUN, status="ok", duration_ms=int(summary.get("duration_ms") or 0)),
    ]
    for index, node in enumerate(summary.get("node_sequence") or []):
        spans.append(SpanRecord(
            span_id="node-%d-%s" % (index, node),
            name=SPAN_NODE,
            parent_id=run_id,
            attributes={"agent.node": str(node)},
        ))
    return trace_shape(
        trace_id=tid,
        spans=spans,
        interrupted=bool(summary.get("interrupted")),
    )


__all__ = [
    "ALLOWED_EDGES",
    "ERROR_LINK_INVALID",
    "ERROR_SPAN_HIERARCHY",
    "ERROR_SPAN_INCOMPLETE",
    "ERROR_SPAN_INVALID",
    "LinkRecord",
    "REQUIRED_RUN_SPANS",
    "SIDE_EFFECT_SPANS",
    "SPAN_APPROVAL",

    "SPAN_LLM",
    "SPAN_NODE",
    "SPAN_RAG",

    "SPAN_RUN",
    "SPAN_SANDBOX",
    "SPAN_SCHEMA_VERSION",
    "SPAN_TOOL",
    "SpanRecord",
    "TraceShape",
    "edge_allowed",
    "is_known_span",
    "require_known_span",
    "shape_from_summary",
    "trace_shape",
]