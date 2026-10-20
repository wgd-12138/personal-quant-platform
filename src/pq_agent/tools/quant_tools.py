"""量化工具的受控适配层。

设计原则（对应 A0-02 的学习产出）：
1. 直接 import 核心包纯函数，不走 subprocess，不把 CLI 命令原样暴露给模型。
2. 命令名不等于工具粒度：push-run 是"取数+生成日报+外发"三合一，这里必须拆成独立工具。
3. 只读工具不写业务文件；写盘与外发工具声明 side_effects，因此强制 require 人工确认。
4. 模型只能传白名单参数；路径、目录、解析器、命令行都由调用方注入，模型看不到也改不了。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from personal_quant.market_analysis.classifier import summarize_market
from personal_quant.notifier.schedule import PUSH_SLOTS
from personal_quant.report_builder.daily_pack import build_daily_pack_report
from pq_agent.tools.contracts import ArgSpec, ToolError, ToolSpec, build_result

SLOT_KEYS = tuple(slot.key for slot in PUSH_SLOTS)


def _require_as_of(arguments: Mapping[str, Any], *, allowed: Sequence[dt.date] | None = None) -> dt.date:
    raw = arguments.get("as_of")
    if raw is None:
        raise ToolError("INVALID_AS_OF", "缺少 as_of 参数")
    try:
        as_of = dt.date.fromisoformat(str(raw))
    except ValueError as exc:
        raise ToolError("INVALID_AS_OF", f"as_of 不是合法日期：{raw!r}") from exc
    if allowed is not None and as_of not in tuple(allowed):
        raise ToolError(
            "INVALID_AS_OF",
            f"as_of {as_of.isoformat()} 不在允许的交易日列表内",
            hint="固定日期评测只允许使用 replay manifest 中登记的日期。",
        )
    return as_of


def _slot_arg() -> ArgSpec:
    return ArgSpec(
        name="slot",
        type="string",
        required=True,
        choices=SLOT_KEYS,
        description="推送时段，取值必须是 PUSH_SLOTS 里登记的 key",
    )


def make_market_snapshot_spec(
    *,
    provider: Any,
    allowed_dates: Sequence[dt.date] | None = None,
) -> ToolSpec:
    """只读：从注入的 provider 取一个固定日期的数据快照。"""

    def handler(arguments: dict[str, Any], *, dry_run: bool = True) -> dict[str, Any]:
        as_of = _require_as_of(arguments, allowed=allowed_dates)
        snapshot = provider.snapshot(as_of)
        provenance = getattr(snapshot, "provenance", None)
        summary = summarize_market(snapshot.dataset)
        return build_result(
            spec,
            data={
                "data_snapshot_id": str(getattr(snapshot, "data_snapshot_id", "")),
                "snapshot_date": as_of.isoformat(),
                "profile": str(getattr(provenance, "profile", "")),
                "demo": bool(getattr(provenance, "demo", False)),
                "mainline": list(getattr(summary, "mainline", []) or []),
                "rebound_candidates": list(getattr(summary, "rebound_candidates", []) or []),
            },
            summary="已取得市场快照",
            data_source=str(getattr(snapshot, "data_snapshot_id", "")),
            as_of=as_of.isoformat(),
        )

    spec: ToolSpec | None = None
    spec = ToolSpec(
        name="market_snapshot",
        capability="read_external",
        side_effects=(),
        args=(ArgSpec(name="as_of", type="string", required=True, description="交易日 YYYY-MM-DD"),),
        returns={"type": "object"},
        timeout_s=30.0,
        idempotency="safe",
        risk="low",
        summary="读取指定交易日的市场快照（只读）",
        cli_command="daily-pack",
        reads_network=True,
        handler=handler,
    )
    return spec


def make_daily_pack_preview_spec(
    *,
    provider: Any,
    positions: Sequence[Any] = (),
    news_items: Sequence[Any] = (),
    allowed_dates: Sequence[dt.date] | None = None,
) -> ToolSpec:
    """生成日报文本，但只在内存里，不落盘（落盘是另一个需要确认的工具）。"""

    def handler(arguments: dict[str, Any], *, dry_run: bool = True) -> dict[str, Any]:
        as_of = _require_as_of(arguments, allowed=allowed_dates)
        snapshot = provider.snapshot(as_of)
        summary = summarize_market(snapshot.dataset)
        focus = bool(arguments.get("focus_positions", True))
        report = build_daily_pack_report(summary, list(positions), list(news_items), focus)
        return build_result(
            spec,
            data={"report": report, "length": len(report)},
            summary="已生成日报预览（未写入磁盘）",
            data_source=str(getattr(snapshot, "data_snapshot_id", "")),
            as_of=as_of.isoformat(),
            dry_run=True,
        )

    spec = ToolSpec(
        name="daily_pack_preview",
        capability="read_external",
        side_effects=(),
        args=(
            ArgSpec(name="as_of", type="string", required=True, description="交易日 YYYY-MM-DD"),
            ArgSpec(name="focus_positions", type="boolean", required=False, description="是否包含逐股内容"),
        ),
        returns={"type": "object"},
        timeout_s=30.0,
        idempotency="safe",
        risk="low",
        summary="生成日报内容预览（不落盘、不外发）",
        cli_command="daily-pack",
        reads_network=True,
        handler=handler,
    )
    return spec


def make_report_write_spec(
    *,
    provider: Any,
    reports_dir: Path,
    positions: Sequence[Any] = (),
    news_items: Sequence[Any] = (),
    allowed_dates: Sequence[dt.date] | None = None,
) -> ToolSpec:
    """写盘工具：生成日报并写入 reports/<日期>/。因为会落盘，必须人工确认。"""

    def handler(arguments: dict[str, Any], *, dry_run: bool = True) -> dict[str, Any]:
        as_of = _require_as_of(arguments, allowed=allowed_dates)
        target_dir = Path(reports_dir) / as_of.isoformat()
        if dry_run:
            return build_result(
                spec,
                data={"target": str(target_dir), "planned_files": ["daily_pack_%s.md" % as_of.isoformat()]},
                summary="dry-run：只返回将写入的位置，未落盘",
                data_source="dry_run",
                as_of=as_of.isoformat(),
                dry_run=True,
            )
        snapshot = provider.snapshot(as_of)
        summary = summarize_market(snapshot.dataset)
        report = build_daily_pack_report(summary, list(positions), list(news_items), True)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / ("daily_pack_%s.md" % as_of.isoformat())
        target.write_text(report, encoding="utf-8", newline=chr(10))
        return build_result(
            spec,
            data={"report_path": str(target), "length": len(report)},
            summary="日报已写入 reports 目录",
            artifacts=[{"path": str(target), "bytes": len(report.encode("utf-8"))}],
            data_source=str(getattr(snapshot, "data_snapshot_id", "")),
            as_of=as_of.isoformat(),
            dry_run=False,
        )

    spec = ToolSpec(
        name="report_write",
        capability="business_write",
        side_effects=("write_business_files",),
        args=(ArgSpec(name="as_of", type="string", required=True, description="交易日 YYYY-MM-DD"),),
        returns={"type": "object"},
        timeout_s=30.0,
        idempotency="requires_approval",
        risk="medium",
        summary="把日报写入 reports/<日期>/（写盘，需要人工确认）",
        cli_command="daily-pack",
        requires_approval=True,
        writes_business_files=True,
        reads_network=True,
        handler=handler,
    )
    return spec


def make_push_preview_spec(*, runs_dir: Path, allowed_dates: Sequence[dt.date] | None = None) -> ToolSpec:
    """控制面写：生成推送预览与合同文件。不对外发送，因此确认后即可执行。"""

    def handler(arguments: dict[str, Any], *, dry_run: bool = True) -> dict[str, Any]:
        as_of = _require_as_of(arguments, allowed=allowed_dates)
        slot = str(arguments.get("slot", ""))
        if slot not in SLOT_KEYS:
            raise ToolError("INVALID_SLOT", f"slot 不在 PUSH_SLOTS 内：{slot!r}")
        target = Path(runs_dir) / ("push_preview_%s_%s.md" % (as_of.isoformat(), slot))
        return build_result(
            spec,
            data={"slot": slot, "target": str(target), "prepared": True},
            summary="推送预览已准备（未发送任何外部消息）",
            data_source="control_plane",
            as_of=as_of.isoformat(),
            slot=slot,
            dry_run=True,
        )

    spec = ToolSpec(
        name="push_preview",
        capability="control_write",
        side_effects=("write_control_state",),
        args=(
            ArgSpec(name="as_of", type="string", required=True, description="交易日 YYYY-MM-DD"),
            _slot_arg(),
        ),
        returns={"type": "object"},
        timeout_s=30.0,
        idempotency="requires_approval",
        risk="medium",
        summary="准备推送预览（不发送，需要人工确认）",
        cli_command="push-run",
        requires_approval=True,
        handler=handler,
    )
    return spec


def make_push_send_spec(
    *,
    runs_dir: Path,
    sender: Any,
    allowed_dates: Sequence[dt.date] | None = None,
) -> ToolSpec:
    """外发工具：真正调用注入的 sender。non_retryable —— 外发结果未知时绝不能自动重试。"""

    def handler(arguments: dict[str, Any], *, dry_run: bool = True) -> dict[str, Any]:
        as_of = _require_as_of(arguments, allowed=allowed_dates)
        slot = str(arguments.get("slot", ""))
        if slot not in SLOT_KEYS:
            raise ToolError("INVALID_SLOT", f"slot 不在 PUSH_SLOTS 内：{slot!r}")
        if dry_run:
            return build_result(
                spec,
                data={"slot": slot, "sent": False},
                summary="dry-run：未发送任何外部消息",
                data_source="dry_run",
                as_of=as_of.isoformat(),
                slot=slot,
                dry_run=True,
            )
        if sender is None:
            raise ToolError("DATA_SOURCE_UNAVAILABLE", "未注入发送通道，拒绝执行外发")
        outcome = sender(as_of, slot)
        return build_result(
            spec,
            data={"slot": slot, "sent": True, "outcome": str(outcome)},
            summary="推送已发送",
            data_source="external_channel",
            as_of=as_of.isoformat(),
            slot=slot,
            dry_run=False,
        )

    spec = ToolSpec(
        name="push_send",
        capability="notify_external",
        side_effects=("notify_external",),
        args=(
            ArgSpec(name="as_of", type="string", required=True, description="交易日 YYYY-MM-DD"),
            _slot_arg(),
        ),
        returns={"type": "object"},
        timeout_s=30.0,
        idempotency="non_retryable",
        risk="high",
        summary="发送推送（外发，需要人工确认且失败不自动重试）",
        cli_command="push-send",
        requires_approval=True,
        notifies_external=True,
        handler=handler,
    )
    return spec


# positions_snapshot 真正能返回的字段，就这四个。
# 为什么写死在这里而不是用 {"type": "object"} 糊过去：
# 一个不声明字段的返回合同等于什么都没承诺，模型会以为「成本 / 盈亏 / 市值」也能拿到，
# 拿不到就开始编——这正是「禁止伪造成本/盈亏」的红线要被代码挡住的地方。
# 想加字段，先让这个工具真的能从注入数据里取到它，再改这里。
POSITIONS_ROW_FIELDS: tuple[str, ...] = ("code", "name")
POSITIONS_SNAPSHOT_RETURNS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "count": {"type": "integer", "description": "持仓条数"},
        "positions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "股票代码"},
                    "name": {"type": "string", "description": "股票名称"},
                },
                "required": list(POSITIONS_ROW_FIELDS),
                "additionalProperties": False,
            },
        },
    },
    "required": ["count", "positions"],
    "additionalProperties": False,
}
# 本工具明确不提供的字段。写出来是为了让「没有」是显式的，而不是靠读者猜。
POSITIONS_UNAVAILABLE_FIELDS: tuple[str, ...] = ("cost", "pnl", "market_value", "shares", "quantity")


def make_positions_snapshot_spec(*, positions: Sequence[Any] = ()) -> ToolSpec:
    """只读：返回调用方注入的持仓快照。不读 data/ 下任何文件，路径由调用方决定。

    只回代码和名称。成本、盈亏、市值这些字段注入的持仓数据里没有，
    这里就一个都不给 —— 拿不到的数据必须缺席，不能补一个 0 或一个估算值。
    条数为 0 时同样如实返回 count=0，不把「没有持仓数据」伪装成「没有持仓」以外的任何东西。
    """

    def handler(arguments: dict[str, Any], *, dry_run: bool = True) -> dict[str, Any]:
        rows = []
        for item in positions:
            code = str(getattr(item, "code", "") or getattr(item, "stock_code", ""))
            name = str(getattr(item, "name", ""))
            rows.append({"code": code, "name": name})
        return build_result(
            spec,
            data={"count": len(rows), "positions": rows},
            summary="已读取持仓数量 %d（只含代码与名称，不含成本与盈亏）" % len(rows),
            data_source="injected_positions",
        )

    spec = ToolSpec(
        name="positions_snapshot",
        capability="read_local",
        side_effects=(),
        args=(),
        returns=dict(POSITIONS_SNAPSHOT_RETURNS),
        timeout_s=10.0,
        idempotency="safe",
        risk="low",
        summary="读取当前持仓快照（只读；只有代码与名称，不提供成本与盈亏）",
        cli_command="positions-import",
        handler=handler,
    )
    return spec


def make_risk_rules_spec() -> ToolSpec:
    """只读：返回风控约束原文，供 agent 解释为什么不能下单。不暴露交易接口。"""

    def handler(arguments: dict[str, Any], *, dry_run: bool = True) -> dict[str, Any]:
        return build_result(
            spec,
            data={
                "constraints": [
                    "本项目只做分析与推送，不接券商、不自动下单。",
                    "没有主升就输出暂无，不硬推标的。",
                    "接口失败、字段为空、数据过期都要暴露，不吞数据错误。",
                    "触底反弹只能标反弹候选，不得与主升混淆。",
                    "AI、新闻、人工关注池都不能绕过风控。",
                ],
                "trading_layer_available": False,
            },
            summary="已返回风控约束",
            data_source="policy_document",
        )

    spec = ToolSpec(
        name="policy_rules_read",
        capability="read_local",
        side_effects=(),
        args=(),
        returns={"type": "object"},
        timeout_s=10.0,
        idempotency="safe",
        risk="none",
        summary="读取风控约束（只读）",
        cli_command="check-inputs",
        handler=handler,
    )
    return spec


def make_report_search_spec(*, reports_dir: Path, max_files: int = 20, max_headings: int = 40) -> ToolSpec:
    """只读：在日报归档目录里找某个交易日的报告，返回有哪些文件、各有哪些章节、在第几行。

    为什么要有这个工具：history_question（「昨天那份日报讲了什么」）在 INTENT_TOOLS 里映射的就是
    report_search，但注册表里从来没有这个工具，于是这条意图只会得到 TOOL_UNKNOWN。
    这里只列文件与章节位置，不回吐正文：把两万字的日报塞进模型上下文既贵又容易被当成指令。
    location 是文件位置（reports/<日期>/<文件名>:1-<行数>），不是 RAG 引用串——
    reports/ 不在 RAG 语料白名单里，拿不到索引比对，所以不叫 citation。
    """

    def handler(arguments: dict[str, Any], *, dry_run: bool = True) -> dict[str, Any]:
        as_of = _require_as_of(arguments)
        day_dir = Path(reports_dir) / as_of.isoformat()
        if not day_dir.is_dir():
            return build_result(
                spec,
                data={"date": as_of.isoformat(), "found": False, "files": []},
                summary="%s 没有归档报告（目录不存在，不是「没有内容」）" % as_of.isoformat(),
                data_source="reports_archive",
                degraded=("no_reports_for_date",),
                as_of=as_of.isoformat(),
            )
        files: list[dict[str, Any]] = []
        for path in sorted(day_dir.glob("*.md"))[:max_files]:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                raise ToolError("TOOL_EXECUTION_FAILED", "报告读不出来：%s" % path.name) from exc
            lines = text.splitlines()
            headings = [
                {"line": number, "title": line.lstrip("#").strip()}
                for number, line in enumerate(lines, start=1)
                if line.startswith("#")
            ][:max_headings]
            files.append(
                {
                    "name": path.name,
                    "lines": len(lines),
                    "chars": len(text),
                    "location": "%s:1-%d" % (path.name, len(lines)),
                    "headings": headings,
                }
            )
        return build_result(
            spec,
            data={"date": as_of.isoformat(), "found": bool(files), "files": files},
            summary="已列出 %s 的 %d 份报告（只给章节位置，正文不回吐）" % (as_of.isoformat(), len(files)),
            data_source="reports_archive",
            as_of=as_of.isoformat(),
        )

    spec = ToolSpec(
        name="report_search",
        capability="read_local",
        side_effects=(),
        args=(ArgSpec(name="as_of", type="string", required=True, description="交易日 YYYY-MM-DD"),),
        returns={"type": "object"},
        timeout_s=10.0,
        idempotency="safe",
        risk="low",
        summary="按交易日列出已归档日报的章节位置（只读，不返回正文）",
        cli_command="summary",
        handler=handler,
    )
    return spec


def build_default_registry(
    *,
    provider: Any = None,
    reports_dir: Path | None = None,
    runs_dir: Path | None = None,
    positions: Sequence[Any] = (),
    news_items: Sequence[Any] = (),
    sender: Any = None,
    positions_path: Path | None = None,
    allowed_dates: Sequence[dt.date] | None = None,
    registry: Any = None,
    sandbox: Any = None,
) -> Any:
    """按依赖装配默认工具集。缺少依赖时只跳过相关工具，不静默降级成假实现。"""

    from pq_agent.tools.readiness import make_readiness_check_spec
    from pq_agent.tools.registry import ToolRegistry

    reg = registry if registry is not None else ToolRegistry()
    reg.register(make_risk_rules_spec())
    reg.register(make_positions_snapshot_spec(positions=positions))
    reg.register(make_readiness_check_spec(positions_path))
    if provider is not None:
        reg.register(make_market_snapshot_spec(provider=provider, allowed_dates=allowed_dates))
        reg.register(
            make_daily_pack_preview_spec(
                provider=provider,
                positions=positions,
                news_items=news_items,
                allowed_dates=allowed_dates,
            )
        )
        # report_search 与 report_write 都依赖归档目录：前者读、后者写，能力不同因此是两个工具。
        if reports_dir is not None:
            reg.register(make_report_search_spec(reports_dir=reports_dir))
            reg.register(
                make_report_write_spec(
                    provider=provider,
                    reports_dir=reports_dir,
                    positions=positions,
                    news_items=news_items,
                    allowed_dates=allowed_dates,
                )
            )
    if runs_dir is not None:
        reg.register(make_push_preview_spec(runs_dir=runs_dir, allowed_dates=allowed_dates))
        if sender is not None:
            reg.register(
                make_push_send_spec(runs_dir=runs_dir, sender=sender, allowed_dates=allowed_dates)
            )
    # code_exec 只在部署方显式注入了沙箱运行时之后才登记：
    # 没有沙箱就不该存在「执行代码」这个工具，而不是登记一个跑不起来的空壳。
    if sandbox is not None:
        from pq_agent.tools.code_exec import make_code_exec_spec

        reg.register(make_code_exec_spec(sandbox=sandbox))
    return reg


TOOL_REGISTRY = build_default_registry()
