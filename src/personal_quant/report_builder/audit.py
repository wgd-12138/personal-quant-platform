from __future__ import annotations

from dataclasses import dataclass

from personal_quant.report_builder.daily_pack import REQUIRED_SECTIONS


MISSING_POSITIONS_TEXT = "未提供真实持仓文件"


@dataclass(frozen=True)
class ReportAudit:
    passed: bool
    failures: list[str]


class ReportAuditError(ValueError):
    pass


def audit_daily_report(report: str, require_positions: bool) -> ReportAudit:
    failures: list[str] = []
    for section in REQUIRED_SECTIONS:
        if f"## {section}" not in report:
            failures.append(f"缺少章节: {section}")
    if require_positions and MISSING_POSITIONS_TEXT in report:
        failures.append("严格模式要求真实持仓，但报告显示未提供真实持仓文件")
    if failures:
        raise ReportAuditError("；".join(failures))
    return ReportAudit(passed=True, failures=[])
