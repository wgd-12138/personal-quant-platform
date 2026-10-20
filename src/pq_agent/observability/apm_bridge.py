"""A4-01 把 APM 体检接进 A1-06 的本地事件日志。

为什么单独一个模块而不是塞进 apm_health.py：
apm_health 是**纯探测**（只读、无副作用、可在任意环境跑），
bridge 是**写事件**（会落盘、需要凭据登记）。把两者分开，
测试可以在任何环境下验证探测逻辑，而不用为了跑一次体检去准备一个事件日志。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pq_agent.contracts import ContractError
from pq_agent.observability.apm_health import (
    APM_SCHEMA_VERSION,
    ApmHealthReport,
    apm_health,
)
from pq_agent.observability.event_log import LocalEventLog

EVENT_APM_DEGRADED = "apm_degraded"
EVENT_COLLECTOR_DROPPED = "collector_dropped"


def record_apm_health(
    report: ApmHealthReport,
    *,
    event_log: LocalEventLog,
) -> dict[str, Any]:
    """把体检结论写进事件日志。只写结论与原因，不写端点细节以外的任何内容。"""
    if not isinstance(event_log, LocalEventLog):
        raise ContractError("APM_EVENT_LOG_TYPE", "event_log 必须是 LocalEventLog")
    if not report.apm_degraded:
        return {}
    payload = event_log.record(
        EVENT_APM_DEGRADED,
        detail=report.verdict,
        verdict=report.verdict,
        docker_available=bool(report.docker_available),
        reason_count=len(report.reasons),
        reasons=";".join(report.reasons)[:300],
        unverified_signals=",".join(
            item.signal for item in report.signals if item.supported is None
        ),
    )
    return dict(payload)


def record_dropped_telemetry(
    *,
    event_log: LocalEventLog,
    dropped: int,
    where: str = "",
) -> dict[str, Any]:
    """记录被丢弃的 telemetry 数量。dropped=0 也记，因为「这一轮没丢」本身是证据。"""
    if int(dropped) < 0:
        raise ContractError("APM_DROPPED_INVALID", "dropped 不能为负数")
    payload = event_log.record(
        EVENT_COLLECTOR_DROPPED,
        detail=str(where),
        dropped=int(dropped),
        where=str(where),
    )
    return dict(payload)


def health_with_event_log(
    *,
    event_log: LocalEventLog,
    root: Any = None,
    docker_available: bool | None = None,
    evidence_path: Any = None,
    clock: Any = None,
) -> tuple[ApmHealthReport, Mapping[str, Any]]:
    """体检 + 落事件，一步完成，避免调用方忘记录事件。"""
    report = apm_health(
        root=root,
        docker_available=docker_available,
        evidence_path=evidence_path,
        clock=clock,
    )
    written = record_apm_health(report, event_log=event_log)
    return report, written


__all__ = [
    "APM_SCHEMA_VERSION",
    "EVENT_APM_DEGRADED",
    "EVENT_COLLECTOR_DROPPED",
    "health_with_event_log",
    "record_apm_health",
    "record_dropped_telemetry",
]