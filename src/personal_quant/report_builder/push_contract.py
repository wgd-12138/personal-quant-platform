from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True)
class PushContentContract:
    key: str
    title: str
    required_content: str
    evidence_file: str
    section_keys: tuple[str, ...]
    required_keywords: tuple[str, ...] = ()
    requires_positions: bool = False


PUSH_CONTENT_CONTRACTS = [
    PushContentContract(
        key="intraday",
        title="盘中实时盘面",
        required_content="热门板块、资金强弱、外围表现、外围共振、今日操作建议、主线判断、20日涨幅",
        evidence_file="intraday",
        section_keys=("intraday",),
        required_keywords=("20日涨幅", "博通"),
    ),
    PushContentContract(
        key="positions",
        title="持仓个股操作",
        required_content="逐股止盈、止损、减仓、补仓、清仓、利好利空；缺真实持仓时直接报缺",
        evidence_file="positions",
        section_keys=("positions",),
        requires_positions=True,
    ),
    PushContentContract(
        key="watchlist",
        title="建仓候选推荐",
        required_content="候选股、选择原因、买入触发、仓位上限、失败条件；暂无主升时输出暂无，不硬推",
        evidence_file="watchlist",
        section_keys=("watchlist",),
    ),
    PushContentContract(
        key="news",
        title="利好利空捕捉",
        required_content="来源、时间、影响对象、影响方向、匹配依据；无匹配时说明原因",
        evidence_file="news",
        section_keys=("news",),
    ),
    PushContentContract(
        key="evening_review",
        title="晚间复盘",
        required_content="当天结论、最强方向、失败方向、资金状态",
        evidence_file="evening",
        section_keys=("evening",),
    ),
    PushContentContract(
        key="next_day",
        title="次日操作计划",
        required_content="高开、低开、震荡、破位四种情景，以及明日关注",
        evidence_file="evening",
        section_keys=("evening",),
    ),
    PushContentContract(
        key="risk",
        title="风险提示",
        required_content="持仓、主线、退潮、建仓、事件、外围、交易风险",
        evidence_file="evening",
        section_keys=("evening",),
    ),
]


def build_push_contract_report(
    as_of: dt.date,
    sections: dict[str, str],
    strict_ready: bool,
    readiness_errors: list[str],
    focus_positions: bool = True,
) -> str:
    contract_rows: list[str] = []
    content_errors: list[str] = []
    for contract in PUSH_CONTENT_CONTRACTS:
        status, action = _contract_status(contract, sections, strict_ready, focus_positions)
        if status == "缺字段，不能完整发送":
            missing = action.removeprefix("缺少字段：")
            content_errors.append(f"FIELD_MISSING:{contract.title}:{missing}")
        required_content = contract.required_content
        if contract.requires_positions and not focus_positions:
            required_content = "本阶段暂不关注持仓个股，不验收逐股操作"
        contract_rows.append(
            f"| {contract.title} | {required_content} | "
            f"{contract.evidence_file}_{as_of.isoformat()}.md | {status} | {action} |"
        )
    all_errors = readiness_errors + content_errors
    rows = [
        f"# 推送内容验收清单（{as_of.isoformat()}）",
        "",
        "## 完整性状态",
        f"- 严格完整：{'是' if strict_ready else '否'}",
        f"- 缺口：{'、'.join(all_errors) if all_errors else '暂无'}",
        "",
        "## 推送内容验收清单",
        "| 推送内容 | 必含内容 | 证据文件 | 状态 | 缺口/处理 |",
        "|---|---|---|---|---|",
    ]
    rows.extend(contract_rows)
    return "\n".join(rows) + "\n"


def _contract_status(
    contract: PushContentContract,
    sections: dict[str, str],
    strict_ready: bool,
    focus_positions: bool,
) -> tuple[str, str]:
    missing_sections = [key for key in contract.section_keys if key not in sections or not sections[key].strip()]
    if missing_sections:
        return "缺失", f"缺少报告章节：{'、'.join(missing_sections)}"
    evidence_text = "\n".join(sections[key] for key in contract.section_keys)
    missing_keywords = [keyword for keyword in contract.required_keywords if keyword not in evidence_text]
    if missing_keywords:
        return "缺字段，不能完整发送", f"缺少字段：{'、'.join(missing_keywords)}"
    if contract.requires_positions and not focus_positions:
        return "本阶段跳过", "用户已选择暂不关注持仓个股"
    if contract.requires_positions and not strict_ready:
        return "缺真实持仓，不能完整发送", "补齐 data/positions.csv 后重新生成逐股持仓建议"
    if contract.key == "watchlist" and "暂无满足主升条件的建仓候选" in sections["watchlist"]:
        return "可发送", "当前无主升候选，按规则输出暂无"
    if contract.key == "news" and "暂无已匹配" in sections["news"]:
        return "可发送", "当前无匹配新闻，不作为交易依据"
    return "可发送", "暂无"
