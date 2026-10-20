from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ExpectedReport:
    prefix: str
    name: str


EXPECTED_REPORTS = [
    ExpectedReport("daily_pack", "汇总完整报告"),
    ExpectedReport("push_preview", "每日推送预览"),
    ExpectedReport("push_contract", "推送内容验收清单"),
    ExpectedReport("readiness", "输入就绪检查"),
    ExpectedReport("intraday", "盘中快照"),
    ExpectedReport("positions", "持仓建议"),
    ExpectedReport("watchlist", "建仓候选"),
    ExpectedReport("news", "利好利空"),
    ExpectedReport("evening", "晚间复盘和次日计划"),
]


def build_manifest_report(
    as_of: dt.date,
    output_dir: Path,
    strict_ready: bool,
    readiness_errors: list[str],
) -> str:
    rows = [
        f"# 每日输出清单（{as_of.isoformat()}）",
        "",
        "## 完整性状态",
        "- 普通输出：是",
        f"- 严格完整：{'是' if strict_ready else '否'}",
        "",
        "## 每日输出清单",
        "| 文件 | 内容 | 状态 | 大小 |",
        "|---|---|---|---:|",
    ]
    for expected in EXPECTED_REPORTS:
        path = output_dir / f"{expected.prefix}_{as_of.isoformat()}.md"
        if path.exists():
            rows.append(f"| {path.name} | {expected.name} | 已生成 | {path.stat().st_size} |")
        else:
            rows.append(f"| {path.name} | {expected.name} | 缺失 | 0 |")
    rows.extend(["", "## 严格完整缺口"])
    if readiness_errors:
        rows.extend([f"- {error}" for error in readiness_errors])
    else:
        rows.append("- 暂无")
    return "\n".join(rows) + "\n"


def write_manifest_report(
    as_of: dt.date,
    output_dir: Path,
    strict_ready: bool,
    readiness_errors: list[str],
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"manifest_{as_of.isoformat()}.md"
    path.write_text(
        build_manifest_report(as_of, output_dir, strict_ready, readiness_errors),
        encoding="utf-8",
    )
    return path
