"""A4-02 metrics 合同：8 条 pipeline 的指标定义，以及高基数守卫。

这个模块存在的理由：

1. **指标的名字和 label 是接口，不是实现细节。**告警规则、看板、验收脚本都按名字查。
   名字散落在各处改一处漏一处，所以集中在这里定义，别处只允许引用常量。
2. **高基数字段做 label 会直接把后端打爆。**thread_id、trace_id、run_id、query 每个请求都不同，
   一旦成为 label，会产生「每次请求一条时间序列」。这类字段不是「不推荐」，是**必须拒绝**——
   所以 validate_labels() 直接抛错，而不是发警告。
3. **没有 usage 时必须记 unknown。**cost / tokens 拿不到就写 unknown，绝不估算，
   这是 A1-06 定下的纪律，这里用 label 值 unknown 把它落到指标层面。

本模块不引入任何 OTel metrics SDK 依赖：只产出「指标名 + label 字典 + 数值」的普通数据结构，
交给上层决定怎么发。这样本机没有 Collector 也能把 8 条 pipeline 全部测完。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pq_agent.contracts import ContractError

METRICS_SCHEMA_VERSION = "pq_agent.apm_metrics/1"

# ---------------------------- 8 条 pipeline ----------------------------
# 名字与 A4-02 合同 action 第 2 条一一对应，顺序也保持一致。
PIPELINE_TURN = "turn"
PIPELINE_ERROR = "error"
PIPELINE_LATENCY = "latency"
PIPELINE_TOOL = "tool"
PIPELINE_APPROVAL = "approval"
PIPELINE_SANDBOX = "sandbox"
PIPELINE_RAG = "rag"
PIPELINE_TELEMETRY = "telemetry"

PIPELINES: tuple[str, ...] = (
    PIPELINE_TURN,
    PIPELINE_ERROR,
    PIPELINE_LATENCY,
    PIPELINE_TOOL,
    PIPELINE_APPROVAL,
    PIPELINE_SANDBOX,
    PIPELINE_RAG,
    PIPELINE_TELEMETRY,
)

# ---------------------------- 指标定义 ----------------------------

METRIC_TURN_TOTAL = "pq_agent.turn.total"
METRIC_TURN_DURATION = "pq_agent.turn.duration_ms"
METRIC_ERROR_TOTAL = "pq_agent.error.total"
METRIC_NODE_DURATION = "pq_agent.node.duration_ms"
METRIC_TOOL_TOTAL = "pq_agent.tool.total"
METRIC_TOOL_DURATION = "pq_agent.tool.duration_ms"
METRIC_APPROVAL_TOTAL = "pq_agent.approval.total"
METRIC_APPROVAL_BACKLOG = "pq_agent.approval.backlog"
METRIC_SANDBOX_TOTAL = "pq_agent.sandbox.total"
METRIC_SANDBOX_DURATION = "pq_agent.sandbox.duration_ms"
METRIC_RAG_TOTAL = "pq_agent.rag.retrieve.total"
METRIC_RAG_HIT_COUNT = "pq_agent.rag.hit_count"
METRIC_TELEMETRY_PIPELINE = "pq_agent.telemetry.pipeline.total"
METRIC_TELEMETRY_DROPPED = "pq_agent.telemetry.dropped.total"
METRIC_USAGE_TOTAL = "pq_agent.usage.total"

# ---------------------------- label 白名单 ----------------------------

# 允许做 label 的键。每一个都必须是有限枚举或低基数标签。
ALLOWED_LABEL_KEYS: tuple[str, ...] = (
    "intent",
    "intent_source",
    "mode",
    "node",
    "status",
    "error_code",
    "capability",
    "tool",
    "tool_ok",
    "dry_run",
    "decision",
    "runtime",
    "isolation_level",
    "degraded",
    "signal",
    "exporter",
    "usage_status",
)
# 明确禁止做 label 的键：每个请求都不同，做了就是高基数。
# 这是「禁止」而不是「不建议」，所以 validate_labels 直接抛 ContractError。
FORBIDDEN_LABEL_KEYS: tuple[str, ...] = (
    "thread_id",
    "session_id",
    "run_id",
    "trace_id",
    "span_id",
    "query",
    "user_query",
    "approval_id",
    "as_of",
    "arguments_hash",
    "arguments",
    "checkpoint",
    "file_path",
    "source_path",
    "chunk_id",
    "citation",
    "webhook",
    "webhook_url",
    "token",
    "secret",
    "api_key",
    "authorization",
    "cookie",
    "headers",
    "prompt",
    "message",
    "message_content",
    "raw_text",
    "duration_ms",
    "timestamp",
    "created_at",
)

ERROR_LABEL_FORBIDDEN = "APM_LABEL_HIGH_CARDINALITY"
ERROR_LABEL_UNKNOWN = "APM_LABEL_UNKNOWN"
ERROR_METRIC_UNKNOWN = "APM_METRIC_NAME_UNKNOWN"
ERROR_METRIC_VALUE = "APM_METRIC_VALUE_INVALID"

# value 也必须是低基数：字符串型 label 值的长度上限。
MAX_LABEL_VALUE_CHARS = 64
# 标签个数上限：超过这个数说明有人把一整份 state 当标签塞进来了。
MAX_LABEL_COUNT = 12

USAGE_STATUS_UNKNOWN = "unknown"
USAGE_STATUS_REPORTED = "reported"


def validate_labels(labels: Mapping[str, Any] | None, *, where: str = "") -> dict[str, str]:
    """校验并归一化 label。三条规则，任一违反直接抛错（fail-closed）。

    1. 键必须同时在白名单里 —— 不在白名单的键不是「未知」而是「没定义」，拒绝。
    2. 键不得在禁止列表里 —— 高基数字段即使被误加进白名单也要拦住。
    3. 值必须是标量且不超过 MAX_LABEL_VALUE_CHARS；None 一律写成 "unknown"。

    返回的字典值全部是 str：Prometheus 与 OTLP 的 label 都是字符串，
    在这里统一转换，避免上层有的传 int 有的传 str 导致同一序列被拆成两条。
    """
    src = dict(labels or {})
    if len(src) > MAX_LABEL_COUNT:
        raise ContractError(
            ERROR_LABEL_FORBIDDEN,
            "标签数量 %d 超过上限 %d（%s）" % (len(src), MAX_LABEL_COUNT, where or "未指明位置"),
        )
    out: dict[str, str] = {}
    for key, value in src.items():
        name = str(key)
        if name in FORBIDDEN_LABEL_KEYS:
            raise ContractError(
                ERROR_LABEL_FORBIDDEN,
                "禁止把 %s 当 label：它是高基数字段（%s）" % (name, where or "未指明位置"),
            )
        if name not in ALLOWED_LABEL_KEYS:
            raise ContractError(
                ERROR_LABEL_UNKNOWN,
                "label %s 不在白名单里，不能直接使用（%s）" % (name, where or "未指明位置"),
            )
        if isinstance(value, (dict, list, tuple, set)):
            raise ContractError(
                ERROR_LABEL_FORBIDDEN,
                "label %s 的值是容器类型，会把内容摊成基数（%s）" % (name, where or "未指明位置"),
            )
        if value is None:
            text = USAGE_STATUS_UNKNOWN
        elif isinstance(value, bool):
            text = "true" if value else "false"
        else:
            text = str(value)
        if len(text) > MAX_LABEL_VALUE_CHARS:
            raise ContractError(
                ERROR_LABEL_FORBIDDEN,
                "label %s 的值太长（%d>%d），疑似把正文当标签（%s）"
                % (name, len(text), MAX_LABEL_VALUE_CHARS, where or "未指明位置"),
            )
        out[name] = text
    return out


@dataclass(frozen=True)
class MetricPoint:
    """一个指标数据点。不是 SDK 对象，只是可序列化的普通值。"""

    name: str
    value: float
    labels: Mapping[str, str] = field(default_factory=dict)
    kind: str = "counter"
    pipeline: str = ""

    def __post_init__(self) -> None:
        if self.name not in METRIC_NAMES:
            raise ContractError(ERROR_METRIC_UNKNOWN, "未登记的指标名：" + str(self.name))
        if self.kind not in ("counter", "gauge", "histogram"):
            raise ContractError(ERROR_METRIC_VALUE, "未知指标类型：" + str(self.kind))
        if self.pipeline and self.pipeline not in PIPELINES:
            raise ContractError(ERROR_METRIC_VALUE, "未知 pipeline：" + str(self.pipeline))
        try:
            number = float(self.value)
        except (TypeError, ValueError) as exc:
            raise ContractError(ERROR_METRIC_VALUE, "指标值不是数字：" + str(exc))
        if number != number or number in (float("inf"), float("-inf")):
            raise ContractError(ERROR_METRIC_VALUE, "指标值必须是有限数")
        if self.kind == "counter" and number < 0:
            raise ContractError(ERROR_METRIC_VALUE, "计数器不能为负：" + str(number))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": float(self.value),
            "labels": dict(self.labels),
            "kind": self.kind,
            "pipeline": self.pipeline,
        }
METRIC_NAMES: tuple[str, ...] = (
    METRIC_TURN_TOTAL,
    METRIC_TURN_DURATION,
    METRIC_ERROR_TOTAL,
    METRIC_NODE_DURATION,
    METRIC_TOOL_TOTAL,
    METRIC_TOOL_DURATION,
    METRIC_APPROVAL_TOTAL,
    METRIC_APPROVAL_BACKLOG,
    METRIC_SANDBOX_TOTAL,
    METRIC_SANDBOX_DURATION,
    METRIC_RAG_TOTAL,
    METRIC_RAG_HIT_COUNT,
    METRIC_TELEMETRY_PIPELINE,
    METRIC_TELEMETRY_DROPPED,
    METRIC_USAGE_TOTAL,
)

# 指标 -> pipeline 归属。用来断言「8 条 pipeline 每条都有指标」。
PIPELINE_METRICS: dict[str, tuple[str, ...]] = {
    PIPELINE_TURN: (METRIC_TURN_TOTAL, METRIC_TURN_DURATION),
    PIPELINE_ERROR: (METRIC_ERROR_TOTAL,),
    PIPELINE_LATENCY: (METRIC_TURN_DURATION, METRIC_NODE_DURATION, METRIC_TOOL_DURATION, METRIC_SANDBOX_DURATION),
    PIPELINE_TOOL: (METRIC_TOOL_TOTAL, METRIC_TOOL_DURATION),
    PIPELINE_APPROVAL: (METRIC_APPROVAL_TOTAL, METRIC_APPROVAL_BACKLOG),
    PIPELINE_SANDBOX: (METRIC_SANDBOX_TOTAL, METRIC_SANDBOX_DURATION),
    PIPELINE_RAG: (METRIC_RAG_TOTAL, METRIC_RAG_HIT_COUNT),
    PIPELINE_TELEMETRY: (METRIC_TELEMETRY_PIPELINE, METRIC_TELEMETRY_DROPPED, METRIC_USAGE_TOTAL),
}


@dataclass(frozen=True)
class MetricBatch:
    """一次运行产出的全部指标点。也顺手保存被拒绝的尝试，供告警与验收脚本核对。"""

    points: tuple[MetricPoint, ...] = ()
    rejected: tuple[dict[str, Any], ...] = ()

    def names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.points)

    def pipelines(self) -> tuple[str, ...]:
        return tuple(sorted({item.pipeline for item in self.points if item.pipeline}))

    def add(self, point: MetricPoint) -> "MetricBatch":
        """返回新批次而不是原地改：冻结结构改了会让人以为没改动。"""
        return MetricBatch(points=self.points + (point,), rejected=self.rejected)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": METRICS_SCHEMA_VERSION,
            "point_count": len(self.points),
            "pipelines": list(self.pipelines()),
            "points": [item.to_dict() for item in self.points],
            "rejected": [dict(item) for item in self.rejected],
        }


def metric(
    name: str,
    value: float,
    *,
    labels: Mapping[str, Any] | None = None,
    kind: str = "counter",
    pipeline: str = "",
    where: str = "",
) -> MetricPoint:
    """构造一个指标点，label 一定先过校验。这是给调用方用的唯一入口。"""
    return MetricPoint(
        name=str(name),
        value=float(value),
        labels=validate_labels(labels, where=where or str(name)),
        kind=str(kind),
        pipeline=str(pipeline),
    )


def collect_run_metrics(
    *,
    intent: str = "",
    intent_source: str = "",
    mode: str = "",
    duration_ms: int = 0,
    node_durations: Mapping[str, int] | None = None,
    error_codes: Iterable[str] = (),
    tool_calls: Iterable[Mapping[str, Any]] = (),
    approvals: Iterable[Mapping[str, Any]] = (),
    approval_backlog: int = 0,
    sandbox_calls: Iterable[Mapping[str, Any]] = (),
    rag_hits: int = 0,
    telemetry_dropped: int = 0,
    telemetry_degraded: bool = False,
    exporter: str = "",
    usage_status: str = USAGE_STATUS_UNKNOWN,
) -> MetricBatch:
    """把一次运行的结构化结果转成 8 条 pipeline 的指标。

    这个函数是纯函数：同样的入参永远产出同样的点，没有时钟、没有随机数。
    验收脚本靠这一点断言「同一份证据跑两次结果一致」。
    """
    points: list[MetricPoint] = []
    base = {"intent": intent, "intent_source": intent_source, "mode": mode}

    points.append(metric(METRIC_TURN_TOTAL, 1, labels=base, pipeline=PIPELINE_TURN, where="turn"))
    points.append(metric(
        METRIC_TURN_DURATION,
        max(0, int(duration_ms)),
        labels={"intent": intent, "mode": mode},
        kind="histogram",
        pipeline=PIPELINE_LATENCY,
        where="turn_duration",
    ))

    for node, cost in sorted(dict(node_durations or {}).items()):
        points.append(metric(
            METRIC_NODE_DURATION,
            max(0, int(cost)),
            labels={"node": node},
            kind="histogram",
            pipeline=PIPELINE_LATENCY,
            where="node_duration",
        ))

    for code in sorted({str(item) for item in error_codes}):
        points.append(metric(
            METRIC_ERROR_TOTAL,
            1,
            labels={"error_code": code, "intent": intent},
            pipeline=PIPELINE_ERROR,
            where="error",
        ))

    for call in tool_calls or ():
        row = dict(call)
        ok = bool(row.get("ok", True))
        labels = {
            "tool": str(row.get("tool_name") or row.get("tool") or "unknown"),
            "capability": str(row.get("capability") or "none"),
            "tool_ok": "true" if ok else "false",
            "dry_run": "true" if row.get("dry_run") else "false",
        }
        points.append(metric(METRIC_TOOL_TOTAL, 1, labels=labels, pipeline=PIPELINE_TOOL, where="tool"))
        points.append(metric(
            METRIC_TOOL_DURATION,
            max(0, int(row.get("duration_ms") or 0)),
            labels=labels,
            kind="histogram",
            pipeline=PIPELINE_LATENCY,
            where="tool_duration",
        ))

    for row in approvals or ():
        item = dict(row)
        points.append(metric(
            METRIC_APPROVAL_TOTAL,
            1,
            labels={
                "decision": str(item.get("status") or "unknown"),
                "tool": str(item.get("tool_name") or "unknown"),
                "capability": str(item.get("capability") or "none"),
            },
            pipeline=PIPELINE_APPROVAL,
            where="approval",
        ))
    points.append(metric(
        METRIC_APPROVAL_BACKLOG,
        max(0, int(approval_backlog)),
        labels={"mode": mode},
        kind="gauge",
        pipeline=PIPELINE_APPROVAL,
        where="approval_backlog",
    ))

    for row in sandbox_calls or ():
        item = dict(row)
        points.append(metric(
            METRIC_SANDBOX_TOTAL,
            1,
            labels={
                "runtime": str(item.get("runtime") or "local"),
                "isolation_level": str(item.get("isolation_level") or "constraint_only"),
                "status": str(item.get("status") or "unknown"),
            },
            pipeline=PIPELINE_SANDBOX,
            where="sandbox",
        ))
        points.append(metric(
            METRIC_SANDBOX_DURATION,
            max(0, int(item.get("duration_ms") or 0)),
            labels={"runtime": str(item.get("runtime") or "local")},
            kind="histogram",
            pipeline=PIPELINE_LATENCY,
            where="sandbox_duration",
        ))

    points.append(metric(
        METRIC_RAG_TOTAL,
        1 if rag_hits else 0,
        labels={"mode": mode or "bm25", "status": "ok" if rag_hits else "empty"},
        pipeline=PIPELINE_RAG,
        where="rag",
    ))
    points.append(metric(
        METRIC_RAG_HIT_COUNT,
        max(0, int(rag_hits)),
        labels={"mode": mode or "bm25"},
        kind="histogram",
        pipeline=PIPELINE_RAG,
        where="rag_hits",
    ))

    points.append(metric(
        METRIC_TELEMETRY_PIPELINE,
        1,
        labels={
            "exporter": exporter or "none",
            "degraded": "true" if telemetry_degraded else "false",
        },
        pipeline=PIPELINE_TELEMETRY,
        where="telemetry",
    ))
    points.append(metric(
        METRIC_TELEMETRY_DROPPED,
        max(0, int(telemetry_dropped)),
        labels={"signal": "spans"},
        pipeline=PIPELINE_TELEMETRY,
        where="telemetry_dropped",
    ))
    points.append(metric(
        METRIC_USAGE_TOTAL,
        1,
        labels={"usage_status": usage_status or USAGE_STATUS_UNKNOWN},
        pipeline=PIPELINE_TELEMETRY,
        where="usage",
    ))

    return MetricBatch(points=tuple(points))


def pipeline_coverage(batch: MetricBatch) -> dict[str, bool]:
    """8 条 pipeline 是否都有指标点。返回逐条布尔，而不是一个总数。"""
    covered = set(batch.pipelines())
    return {name: name in covered for name in PIPELINES}


def label_cardinality_guard(
    pairs: Iterable[Mapping[str, Any]],
    *,
    limit: int = 100,
    where: str = "",
) -> dict[str, int]:
    """对一批「未来的 label 组合」做基数体检。

    这不是运行时护栏（运行时护栏是 validate_labels 的抛错），
    而是给评测用的：把整轮评测里出现过的 label 组合收集起来，
    断言唯一组合数不超过阈值。阈值默认 100——真跑到几千条，说明有人把高基数字段混进来了。
    """
    seen: dict[str, int] = {}
    for row in pairs or ():
        key = "|".join("%s=%s" % (k, v) for k, v in sorted(dict(row).items()))
        seen[key] = seen.get(key, 0) + 1
    if len(seen) > int(limit):
        raise ContractError(
            ERROR_LABEL_FORBIDDEN,
            "label 组合数 %d 超过阈值 %d（%s）" % (len(seen), int(limit), where or "未指明位置"),
        )
    return seen


__all__ = [
    "ALLOWED_LABEL_KEYS",
    "ERROR_LABEL_FORBIDDEN",
    "ERROR_LABEL_UNKNOWN",
    "ERROR_METRIC_UNKNOWN",
    "ERROR_METRIC_VALUE",
    "FORBIDDEN_LABEL_KEYS",
    "MAX_LABEL_COUNT",
    "MAX_LABEL_VALUE_CHARS",
    "METRICS_SCHEMA_VERSION",
    "METRIC_NAMES",
    "METRIC_APPROVAL_BACKLOG",
    "METRIC_APPROVAL_TOTAL",
    "METRIC_ERROR_TOTAL",
    "METRIC_NODE_DURATION",
    "METRIC_RAG_HIT_COUNT",
    "METRIC_RAG_TOTAL",
    "METRIC_SANDBOX_DURATION",
    "METRIC_SANDBOX_TOTAL",
    "METRIC_TELEMETRY_DROPPED",
    "METRIC_TELEMETRY_PIPELINE",
    "METRIC_TOOL_DURATION",
    "METRIC_TOOL_TOTAL",
    "METRIC_TURN_DURATION",
    "METRIC_TURN_TOTAL",
    "METRIC_USAGE_TOTAL",
    "MetricBatch",
    "MetricPoint",
    "PIPELINES",
    "PIPELINE_APPROVAL",
    "PIPELINE_ERROR",
    "PIPELINE_LATENCY",
    "PIPELINE_METRICS",
    "PIPELINE_RAG",
    "PIPELINE_SANDBOX",
    "PIPELINE_TELEMETRY",
    "PIPELINE_TOOL",
    "PIPELINE_TURN",
    "USAGE_STATUS_REPORTED",
    "USAGE_STATUS_UNKNOWN",
    "collect_run_metrics",
    "label_cardinality_guard",
    "metric",
    "pipeline_coverage",
    "validate_labels",
]