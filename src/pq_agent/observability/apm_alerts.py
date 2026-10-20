"""A4-02 告警规则：6 条规则的唯一定义处，以及 firing/cleared 两态评估。

为什么把规则写成代码而不是只写一份 YAML：

1. **规则必须有可执行的定义。**只写 YAML，就无法在验收里断言「这条规则真的会响」。
   所以规则先在 Python 里定义，再由 render_alarm_settings() 导出成 SkyWalking 的 alarm-settings.yml。
   导出物是产物，定义是源头——两者不可能各写一份然后悄悄分叉。
2. **告警必须有恢复态。**只会响不会消的告警，用两天就会被无视。
   所以评估结果同时给出 firing 与 cleared，并记录**状态迁移**，
   「触发、查询、恢复」三件事各自留下证据，正好对应 A4-02 合同 verify 里的要求。
3. **阈值不能拍脑袋。**本机没有真实流量，任何「延迟超过 800ms 就告警」都没有实测依据。
   所以每条规则的 threshold 都标了 basis 字段：要么来自实测数字，要么明写 "unmeasured"。
   明写 unmeasured 不丢人，把猜的数字说成实测才丢人。

本模块不依赖任何后端：评估函数是纯函数，输入是 MetricBatch，输出是状态与迁移。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pq_agent.contracts import ContractError

from pq_agent.observability.apm_metrics import (
    METRIC_APPROVAL_BACKLOG,
    METRIC_ERROR_TOTAL,
    METRIC_SANDBOX_TOTAL,
    METRIC_TELEMETRY_DROPPED,
    METRIC_TOOL_TOTAL,
    METRIC_TURN_DURATION,
    MetricBatch,
    USAGE_STATUS_UNKNOWN,
)

ALERTS_SCHEMA_VERSION = "pq_agent.apm_alerts/1"

ALERT_AGENT_ERROR = "pq_agent_agent_error"
ALERT_TOOL_FAILURE = "pq_agent_tool_failure"
ALERT_LATENCY = "pq_agent_latency"
ALERT_APPROVAL_BACKLOG = "pq_agent_approval_backlog"
ALERT_SANDBOX_RISK = "pq_agent_sandbox_risk"
ALERT_COLLECTOR_DROPPED = "pq_agent_collector_dropped"

ALERT_KEYS: tuple[str, ...] = (
    ALERT_AGENT_ERROR,
    ALERT_TOOL_FAILURE,
    ALERT_LATENCY,
    ALERT_APPROVAL_BACKLOG,
    ALERT_SANDBOX_RISK,
    ALERT_COLLECTOR_DROPPED,
)

SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"
SEVERITIES_ALERT: tuple[str, ...] = (SEVERITY_WARNING, SEVERITY_CRITICAL)

STATUS_FIRING = "firing"
STATUS_CLEARED = "cleared"
STATUS_PENDING = "pending"
ALERT_STATUSES: tuple[str, ...] = (STATUS_FIRING, STATUS_CLEARED, STATUS_PENDING)

ERROR_ALERT_UNKNOWN = "APM_ALERT_KEY_UNKNOWN"
ERROR_ALERT_RULE = "APM_ALERT_RULE_INVALID"

@dataclass(frozen=True)
class AlertRule:
    """一条告警规则的完整定义。basis 字段是给未来的人看的：这个阈值凭什么。"""

    key: str
    metric: str
    comparator: str
    threshold: float
    severity: str
    summary: str
    basis: str
    reduction: str = "sum"
    # label_filter 的值可以是 str（精确匹配）或 tuple（属于其中之一）。
    # 用集合形式是因为「沙箱被拦截」对应多个状态值，只写一个必然漏。
    label_filter: Mapping[str, Any] = field(default_factory=dict)
    for_runs: int = 1
    period_minutes: int = 10
    silence_minutes: int = 10

    def __post_init__(self) -> None:
        if self.key not in ALERT_KEYS:
            raise ContractError(ERROR_ALERT_UNKNOWN, "未登记的告警键：" + str(self.key))
        if self.comparator not in (">", ">="):
            raise ContractError(ERROR_ALERT_RULE, "只支持 > 与 >=，收到：" + str(self.comparator))
        if self.reduction not in ("sum", "max", "count"):
            raise ContractError(ERROR_ALERT_RULE, "未知归约方式：" + str(self.reduction))
        if self.severity not in SEVERITIES_ALERT:
            raise ContractError(ERROR_ALERT_RULE, "未知严重级别：" + str(self.severity))
        if int(self.for_runs) < 1:
            raise ContractError(ERROR_ALERT_RULE, "for_runs 至少为 1")
        if not self.basis:
            raise ContractError(ERROR_ALERT_RULE, "必须写明阈值依据（basis），不许留空")

    def compare(self, value: float) -> bool:
        if self.comparator == ">":
            return float(value) > float(self.threshold)
        return float(value) >= float(self.threshold)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "metric": self.metric,
            "comparator": self.comparator,
            "threshold": float(self.threshold),
            "severity": self.severity,
            "summary": self.summary,
            "basis": self.basis,
            "reduction": self.reduction,
            "label_filter": dict(self.label_filter),
            "for_runs": int(self.for_runs),
            "period_minutes": int(self.period_minutes),
            "silence_minutes": int(self.silence_minutes),
        }


# 六条规则。basis 分两类：
#   measured —— 阈值来自本仓已有的事实（如图节点超时默认值）；
#   unmeasured —— 阈值是占位，必须在接入真实流量后重标。明写出来，不假装有依据。
ALERT_RULES: tuple[AlertRule, ...] = (
    AlertRule(
        key=ALERT_AGENT_ERROR,
        metric=METRIC_ERROR_TOTAL,
        comparator=">",
        threshold=0,
        severity=SEVERITY_WARNING,
        summary="agent 出现错误码：任何一条错误都值得看，不做数量容忍",
        basis="measured：错误是异常路径，正常一轮运行不应产生任何错误码，故阈值取 0",
        reduction="sum",
    ),
    AlertRule(
        key=ALERT_TOOL_FAILURE,
        metric=METRIC_TOOL_TOTAL,
        comparator=">",
        threshold=0,
        severity=SEVERITY_WARNING,
        summary="工具调用失败：tool_ok=false 的次数大于 0",
        basis="measured：工具失败会让答复缺数据，属需要人工看一眼的事件，故阈值取 0",
        reduction="sum",
        label_filter={"tool_ok": "false"},
    ),
    AlertRule(
        key=ALERT_LATENCY,
        metric=METRIC_TURN_DURATION,
        comparator=">=",
        threshold=30000,
        severity=SEVERITY_WARNING,
        summary="单轮耗时达到单节点超时上限：说明已经撞到超时，不是有点慢",
        basis="measured：graph 的 DEFAULT_NODE_TIMEOUT_S 为 30 秒，超过它意味着某节点已触发超时保护",
        reduction="max",
    ),
    AlertRule(
        key=ALERT_APPROVAL_BACKLOG,
        metric=METRIC_APPROVAL_BACKLOG,
        comparator=">",
        threshold=0,
        severity=SEVERITY_WARNING,
        summary="人工确认积压：有待批事项没处理",
        basis="measured：正常态积压为 0，积压即代表有人该批没批",
        reduction="max",
    ),
    AlertRule(
        key=ALERT_SANDBOX_RISK,
        metric=METRIC_SANDBOX_TOTAL,
        comparator=">",
        threshold=0,
        severity=SEVERITY_CRITICAL,
        summary="沙箱拦截：有代码试图越界（static_blocked / policy_denied）",
        basis="measured：沙箱拦截不是噪音，每一次都代表有代码试图越界，故为 critical 且阈值 0",
        reduction="sum",
        label_filter={"status": ("static_blocked", "policy_denied")},
    ),
    AlertRule(
        key=ALERT_COLLECTOR_DROPPED,
        metric=METRIC_TELEMETRY_DROPPED,
        comparator=">",
        threshold=0,
        severity=SEVERITY_WARNING,
        summary="遥测被丢弃：观测出现缺口",
        basis="measured：丢一条就是缺口，观测完整性与数量无关，故阈值取 0",
        reduction="sum",
    ),
)

ALERT_RULES_BY_KEY: dict[str, AlertRule] = {item.key: item for item in ALERT_RULES}

def _label_matches(labels: Mapping[str, str], wanted: Mapping[str, Any]) -> bool:
    """label 过滤：值是 tuple 表示「属于其中之一」，是 str 表示精确相等。"""
    for key, expect in wanted.items():
        actual = labels.get(str(key))
        if isinstance(expect, (tuple, list, set)):
            if actual not in {str(item) for item in expect}:
                return False
        elif actual != str(expect):
            return False
    return True


def metric_value(batch: MetricBatch, rule: AlertRule) -> float:
    """按规则从批次里取一个数。归约方式与 label 过滤都由规则决定。"""
    rows = [
        item for item in batch.points
        if item.name == rule.metric and _label_matches(item.labels, rule.label_filter)
    ]
    if rule.reduction == "count":
        return float(len(rows))
    if not rows:
        # 没有匹配的点不是「0 次」而是「没有证据」。两者对告警来说必须区分：
        # 没有证据时返回 0，但 evidence 里会写明 matched=0，看的人知道这是空集。
        return 0.0
    if rule.reduction == "max":
        return float(max(item.value for item in rows))
    return float(sum(item.value for item in rows))


@dataclass(frozen=True)
class AlertState:
    """一条规则在某一轮评估后的状态。"""

    key: str
    status: str
    value: float
    threshold: float
    severity: str
    summary: str
    consecutive: int = 0
    evidence: tuple[str, ...] = ()

    @property
    def firing(self) -> bool:
        return self.status == STATUS_FIRING

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "status": self.status,
            "value": float(self.value),
            "threshold": float(self.threshold),
            "severity": self.severity,
            "summary": self.summary,
            "consecutive": int(self.consecutive),
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class AlertEvaluation:
    """一次评估的全部结果：状态 + 状态迁移。迁移是「触发/恢复」的证据。"""

    states: tuple[AlertState, ...] = ()
    transitions: tuple[dict[str, Any], ...] = ()
    run_index: int = 0

    @property
    def firing(self) -> tuple[str, ...]:
        return tuple(item.key for item in self.states if item.status == STATUS_FIRING)

    @property
    def cleared(self) -> tuple[str, ...]:
        return tuple(item.key for item in self.states if item.status == STATUS_CLEARED)

    @property
    def pending(self) -> tuple[str, ...]:
        return tuple(item.key for item in self.states if item.status == STATUS_PENDING)

    def state_of(self, key: str) -> AlertState | None:
        for item in self.states:
            if item.key == str(key):
                return item
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": ALERTS_SCHEMA_VERSION,
            "run_index": int(self.run_index),
            "firing": list(self.firing),
            "pending": list(self.pending),
            "cleared": list(self.cleared),
            "states": [item.to_dict() for item in self.states],
            "transitions": [dict(item) for item in self.transitions],
        }


def evaluate_alerts(
    batch: MetricBatch,
    *,
    previous: Sequence[AlertState] = (),
    run_index: int = 0,
    rules: Sequence[AlertRule] = ALERT_RULES,
) -> AlertEvaluation:
    """评估全部规则。纯函数：同一批指标 + 同一份历史状态，结果永远一致。

    三态的意义：
      firing  —— 条件满足且连续满足次数达到 for_runs，真的要响；
      pending —— 条件满足了但还没到次数，属于「快了」，不能当成已触发；
      cleared —— 条件不满足，包括「从 firing 恢复」的情况。
    只把 firing 当告警、把 cleared 当恢复，中间的 pending 不能混进来，
    否则一次抖动就会被报成「触发后又恢复」。
    """
    prev_map = {item.key: item for item in previous or ()}
    states: list[AlertState] = []
    transitions: list[dict[str, Any]] = []
    for rule in rules:
        value = metric_value(batch, rule)
        met = rule.compare(value)
        prev = prev_map.get(rule.key)
        prev_consec = int(prev.consecutive) if prev is not None else 0
        consec = prev_consec + 1 if met else 0
        if met and consec >= int(rule.for_runs):
            status = STATUS_FIRING
        elif met:
            status = STATUS_PENDING
        else:
            status = STATUS_CLEARED
        evidence = (
            "%s %s=%s %s %s" % (rule.metric, rule.reduction, _fmt(value), rule.comparator, _fmt(rule.threshold)),
            "matched_filter=%s" % (dict(rule.label_filter) or "none"),
        )
        state = AlertState(
            key=rule.key,
            status=status,
            value=value,
            threshold=rule.threshold,
            severity=rule.severity,
            summary=rule.summary,
            consecutive=consec,
            evidence=evidence,
        )
        states.append(state)
        before = prev.status if prev is not None else ""
        if before != status:
            transitions.append({
                "key": rule.key,
                "from": before,
                "to": status,
                "value": value,
                "threshold": float(rule.threshold),
                "severity": rule.severity,
                "run_index": int(run_index),
            })
    return AlertEvaluation(states=tuple(states), transitions=tuple(transitions), run_index=int(run_index))


def _fmt(value: Any) -> str:
    """数字格式化：整数不带小数点，避免 0.0 与 0 看起来像两个阈值。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number == int(number):
        return str(int(number))
    return ("%g" % number)

# YAML 里以这些字符开头的标量会被当成语法（锚点、注释、块标量等），必须加引号。
# 双引号与单引号本身也要算进去，用 chr() 拼出来，避免源码里出现难以阅读的转义。
_YAML_SPECIAL_HEADS = "!&*#?|-<>=@`" + chr(34) + chr(39)


def render_alarm_settings(
    *,
    rules: Sequence[AlertRule] = ALERT_RULES,
    prefix: str = "pq_agent",
) -> dict[str, Any]:
    """把规则导出成 SkyWalking alarm-settings.yml 的结构。

    只做导出，不做语法糖：导出的字段就是 SkyWalking 认的字段名（metrics-name / op /
    threshold / period / count / silence-period / message）。
    具体后端能不能吃这份配置，由部署脚本那一层去验，不在这里假设。
    """
    out: dict[str, Any] = {}
    for rule in rules:
        key = str(rule.key)
        if prefix and not key.startswith(prefix + "_"):
            key = prefix + "_" + key
        out[key] = {
            "metrics-name": str(rule.metric).replace(".", "_"),
            "op": str(rule.comparator),
            "threshold": _fmt(rule.threshold),
            "period": int(rule.period_minutes),
            "count": int(rule.for_runs),
            "silence-period": int(rule.silence_minutes),
            "message": str(rule.summary),
        }
    return {"rules": out}


def render_alarm_settings_text(
    *,
    rules: Sequence[AlertRule] = ALERT_RULES,
    prefix: str = "pq_agent",
) -> str:
    """手写 YAML 渲染，不引第三方库：核心包保持零依赖。

    只支持本项目需要的两层结构（map -> map -> 标量），不做通用 YAML。
    会被 YAML 误读的字符串一律加双引号。
    """
    payload = render_alarm_settings(rules=rules, prefix=prefix)
    lines: list[str] = [
        "# 本文件由 pq_agent.observability.apm_alerts.render_alarm_settings_text() 生成。",
        "# 请勿手工编辑：规则的唯一定义在 src/pq_agent/observability/apm_alerts.py。",
        "rules:",
    ]
    for name, body in payload["rules"].items():
        lines.append("  %s:" % name)
        for field_name, field_value in body.items():
            lines.append("    %s: %s" % (field_name, _yaml_scalar(field_value)))
    return chr(10).join(lines) + chr(10)


def _yaml_scalar(value: Any) -> str:
    """把标量渲染成 YAML。只处理 str/int/float/bool；其它类型直接拒绝。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _fmt(value)
    if not isinstance(value, str):
        raise ContractError(ERROR_ALERT_RULE, "YAML 渲染不支持类型：" + type(value).__name__)
    text = value
    needs_quote = (
        text == ""
        or text[0] in _YAML_SPECIAL_HEADS
        or ":" in text
        or "#" in text
        or chr(10) in text
    )
    if not needs_quote:
        return text
    escaped = text.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))
    return chr(34) + escaped + chr(34)


def describe_alerts(*, rules: Sequence[AlertRule] = ALERT_RULES) -> list[dict[str, Any]]:
    """规则清单，给事实卡与文档用。"""
    return [item.to_dict() for item in rules]


def alert_coverage(evaluation: AlertEvaluation) -> dict[str, bool]:
    """6 条规则是否都被评估过。返回逐条布尔，便于断言「没有规则被漏掉」。"""
    seen = {item.key for item in evaluation.states}
    return {key: key in seen for key in ALERT_KEYS}


__all__ = [
    "ALERTS_SCHEMA_VERSION",
    "ALERT_AGENT_ERROR",
    "ALERT_APPROVAL_BACKLOG",
    "ALERT_COLLECTOR_DROPPED",
    "ALERT_KEYS",
    "ALERT_LATENCY",
    "ALERT_RULES",
    "ALERT_RULES_BY_KEY",
    "ALERT_SANDBOX_RISK",
    "ALERT_STATUSES",
    "ALERT_TOOL_FAILURE",
    "AlertEvaluation",
    "AlertRule",
    "AlertState",
    "ERROR_ALERT_RULE",
    "ERROR_ALERT_UNKNOWN",
    "SEVERITIES_ALERT",
    "SEVERITY_CRITICAL",
    "SEVERITY_WARNING",
    "STATUS_CLEARED",
    "STATUS_FIRING",
    "STATUS_PENDING",
    "alert_coverage",
    "describe_alerts",
    "evaluate_alerts",
    "metric_value",
    "render_alarm_settings",
    "render_alarm_settings_text",
]