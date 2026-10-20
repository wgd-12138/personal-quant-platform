from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PushSlot:
    key: str
    name: str
    time: str
    report_action: str
    message_keys: tuple[str, ...]
    purpose: str


PUSH_SLOTS = [
    PushSlot(
        key="pre_open",
        name="开盘前计划",
        time="08:50",
        report_action="不重新拉行情，读取上一交易日完整报告",
        message_keys=("next_day", "risk"),
        purpose="开盘前只看今天怎么应对，不追新结论",
    ),
    PushSlot(
        key="morning_build",
        name="早盘建仓推荐",
        time="09:26",
        report_action="生成当天 daily-pack --split；需要集合竞价数据，否则输出暂无",
        message_keys=("morning_build",),
        purpose="集合竞价后只推 1-2 支适合今日建仓的股票；证据不足不推",
    ),
    PushSlot(
        key="morning",
        name="早盘盘面",
        time="10:35",
        report_action="生成当天 daily-pack --split",
        message_keys=("intraday", "news"),
        purpose="早盘固定看盘面和消息，持仓操作交给实时触发提醒",
    ),
    PushSlot(
        key="midday",
        name="午间休息",
        time="11:45",
        report_action="生成当天 daily-pack --split",
        message_keys=("intraday", "watchlist", "news", "risk"),
        purpose="午休时固定看盘面总结、消息总结、候选和风险",
    ),
    PushSlot(
        key="overnight_arbitrage",
        name="尾盘隔夜套利",
        time="14:30",
        report_action="重新生成当天 daily-pack --split",
        message_keys=("overnight_arbitrage",),
        purpose="尾盘只推 1-2 支适合隔夜套利的股票；次日冲高全部卖出",
    ),
    PushSlot(
        key="tail",
        name="尾盘盘面",
        time="14:35",
        report_action="重新生成当天 daily-pack --split",
        message_keys=("intraday", "news", "risk"),
        purpose="收盘前固定看盘面、消息和风险，持仓操作交给实时触发提醒",
    ),
    PushSlot(
        key="evening",
        name="收盘复盘",
        time="16:10",
        report_action="生成当天 daily-pack --split",
        message_keys=("intraday", "positions", "news", "evening_review", "next_day", "risk"),
        purpose="收盘后发送完整盘后决策包",
    ),
]


def get_push_slot(key: str) -> PushSlot:
    for slot in PUSH_SLOTS:
        if slot.key == key:
            return slot
    raise ValueError(f"PUSH_SLOT_UNKNOWN:{key}")


def build_push_schedule_report(
    reports_dir: Path = Path("reports"),
    runs_dir: Path = Path("planning/runs"),
    target_days: int = 10,
    expected_dates_path: Path = Path("planning/runs/expected_dates.txt"),
    positions_path: Path | None = None,
    focus_positions: bool = True,
) -> str:
    rows = [
        "# 每日生成和推送时间表",
        "",
        "## 总判断",
        "- 通道：飞书自定义机器人 webhook",
        "- 默认：先 dry-run；只有加 `--live` 才真实发送",
        "- 纪律：推送只发送本地报告内容，不生成新结论",
        "",
        "## 时间节点",
        "| 时间 | 节点 | 生成动作 | 推送内容 | 用途 |",
        "|---|---|---|---|---|",
    ]
    for slot in PUSH_SLOTS:
        rows.append(
            f"| {slot.time} | {slot.name} | {slot.report_action} | "
            f"{'、'.join(slot.message_keys)} | {slot.purpose} |"
        )
    rows.extend(
        [
            "",
            "## 推荐命令",
            "| 节点 | 生成命令 | dry-run 生成并推送 | 真实生成并推送 |",
            "|---|---|---|---|",
        ]
    )
    for slot in PUSH_SLOTS:
        rows.append(
            f"| {slot.name} | `{_report_command(slot, reports_dir, positions_path, focus_positions)}` | "
            f"`{_push_command(slot, reports_dir, runs_dir, positions_path, focus_positions, live=False)}` | "
            f"`{_push_command(slot, reports_dir, runs_dir, positions_path, focus_positions, live=True)}` |"
        )
    rows.extend(
        [
            "",
            "## 试运行门禁",
            (
                "- 推送模块可以先 dry-run；真实自动推送前仍要看 "
                f"`python -m personal_quant.cli trial-status --reports-dir {_path_text(reports_dir)} "
                f"--runs-dir {_path_text(runs_dir)} --target-days {target_days} "
                f"--expected-dates {_path_text(expected_dates_path)}{'' if focus_positions else ' --ignore-positions'}`"
            ),
        ]
    )
    return "\n".join(rows) + "\n"


def write_push_schedule_report(
    output_path: Path,
    reports_dir: Path = Path("reports"),
    runs_dir: Path = Path("planning/runs"),
    target_days: int = 10,
    expected_dates_path: Path = Path("planning/runs/expected_dates.txt"),
    positions_path: Path | None = None,
    focus_positions: bool = True,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        build_push_schedule_report(
            reports_dir=reports_dir,
            runs_dir=runs_dir,
            target_days=target_days,
            expected_dates_path=expected_dates_path,
            positions_path=positions_path,
            focus_positions=focus_positions,
        ),
        encoding="utf-8",
    )
    return output_path


def build_windows_task_script(
    project_dir: Path,
    reports_dir: Path = Path("reports"),
    runs_dir: Path = Path("planning/runs"),
    expected_dates_path: Path = Path("planning/runs/expected_dates.txt"),
    positions_path: Path | None = None,
    python_command: str = "python",
    focus_positions: bool = True,
    live: bool = False,
) -> str:
    rows = [
        "$ErrorActionPreference = 'Stop'",
        "",
        "# Set FEISHU_WEBHOOK_URL in the current user's environment before running live tasks.",
        "# [Environment]::SetEnvironmentVariable('FEISHU_WEBHOOK_URL', 'your Feishu webhook', 'User')",
        "",
    ]
    rows.extend(
        _market_monitor_task_rows(
            project_dir=project_dir,
            runs_dir=runs_dir,
            expected_dates_path=expected_dates_path,
            positions_path=positions_path if focus_positions else None,
            python_command=python_command,
            live=live,
        )
    )
    if focus_positions and positions_path is not None:
        rows.extend(
            _position_monitor_task_rows(
                project_dir=project_dir,
                runs_dir=runs_dir,
                expected_dates_path=expected_dates_path,
                positions_path=positions_path,
                python_command=python_command,
                live=live,
            )
        )
    for slot in PUSH_SLOTS:
        task_name = f"PersonalQuant_{slot.key}"
        command = _daily_push_command(
            slot,
            reports_dir=reports_dir,
            runs_dir=runs_dir,
            expected_dates_path=expected_dates_path,
            positions_path=positions_path,
            focus_positions=focus_positions,
            live=live,
        )
        argument = (
            '-NoProfile -ExecutionPolicy Bypass -Command "'
            f"Set-Location {_quote_ps_command(project_dir)}; "
            "$env:PYTHONPATH='src'; "
            f"& {_quote_ps_command(Path(python_command))} -m personal_quant.cli {command}"
            '"'
        )
        variable = slot.key.replace("-", "_")
        rows.extend(
            [
                f"${variable}Action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument {_quote_ps_literal(argument)}",
                f"${variable}Trigger = New-ScheduledTaskTrigger -Daily -At '{slot.time}'",
                (
                    f"Register-ScheduledTask -TaskName '{task_name}' "
                    f"-Action ${variable}Action -Trigger ${variable}Trigger "
                    f"-Description '{task_name}' -Force"
                ),
                "",
            ]
        )
    return "\n".join(rows).rstrip() + "\n"


def _position_monitor_task_rows(
    project_dir: Path,
    runs_dir: Path,
    expected_dates_path: Path,
    positions_path: Path,
    python_command: str,
    live: bool,
) -> list[str]:
    command = _position_monitor_command(
        runs_dir=runs_dir,
        expected_dates_path=expected_dates_path,
        positions_path=positions_path,
        live=live,
    )
    argument = (
        '-NoProfile -ExecutionPolicy Bypass -Command "'
        f"Set-Location {_quote_ps_command(project_dir)}; "
        "$env:PYTHONPATH='src'; "
        f"& {_quote_ps_command(Path(python_command))} -m personal_quant.cli {command}"
        '"'
    )
    return [
        "$position_monitorAction = New-ScheduledTaskAction -Execute 'powershell.exe' "
        f"-Argument {_quote_ps_literal(argument)}",
        "$position_monitorTrigger = New-ScheduledTaskTrigger -Daily -At '09:30'",
        (
            "Register-ScheduledTask -TaskName 'PersonalQuant_position_monitor' "
            "-Action $position_monitorAction -Trigger $position_monitorTrigger "
            "-Description 'PersonalQuant_position_monitor' -Force"
        ),
        "",
    ]


def _market_monitor_task_rows(
    project_dir: Path,
    runs_dir: Path,
    expected_dates_path: Path,
    positions_path: Path | None,
    python_command: str,
    live: bool,
) -> list[str]:
    command = _market_monitor_command(
        runs_dir=runs_dir,
        expected_dates_path=expected_dates_path,
        positions_path=positions_path,
        live=live,
    )
    argument = (
        '-NoProfile -ExecutionPolicy Bypass -Command "'
        f"Set-Location {_quote_ps_command(project_dir)}; "
        "$env:PYTHONPATH='src'; "
        f"& {_quote_ps_command(Path(python_command))} -m personal_quant.cli {command}"
        '"'
    )
    return [
        "$market_monitorAction = New-ScheduledTaskAction -Execute 'powershell.exe' "
        f"-Argument {_quote_ps_literal(argument)}",
        "$market_monitorTrigger = New-ScheduledTaskTrigger -Daily -At '09:30'",
        (
            "Register-ScheduledTask -TaskName 'PersonalQuant_market_monitor' "
            "-Action $market_monitorAction -Trigger $market_monitorTrigger "
            "-Description 'PersonalQuant_market_monitor' -Force"
        ),
        "",
    ]


def write_windows_task_script(
    output_path: Path,
    project_dir: Path,
    reports_dir: Path = Path("reports"),
    runs_dir: Path = Path("planning/runs"),
    expected_dates_path: Path = Path("planning/runs/expected_dates.txt"),
    positions_path: Path | None = None,
    python_command: str = "python",
    focus_positions: bool = True,
    live: bool = False,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        build_windows_task_script(
            project_dir=project_dir,
            reports_dir=reports_dir,
            runs_dir=runs_dir,
            expected_dates_path=expected_dates_path,
            positions_path=positions_path,
            python_command=python_command,
            focus_positions=focus_positions,
            live=live,
        ),
        encoding="utf-8",
    )
    return output_path


def _report_command(slot: PushSlot, reports_dir: Path, positions_path: Path | None, focus_positions: bool) -> str:
    if slot.key == "pre_open":
        return "不执行；使用上一交易日已生成报告"
    parts = [
        "python -m personal_quant.cli daily-pack",
        "--as-of YYYY-MM-DD",
        f"--output-dir {_path_text(reports_dir)}",
        "--split",
    ]
    if focus_positions and positions_path is not None:
        parts.append(f"--positions {_path_text(positions_path)}")
    if not focus_positions:
        parts.append("--ignore-positions")
    return " ".join(parts)


def _push_command(
    slot: PushSlot,
    reports_dir: Path,
    runs_dir: Path,
    positions_path: Path | None,
    focus_positions: bool,
    live: bool,
) -> str:
    return (
        "python -m personal_quant.cli "
        + _daily_push_command(
            slot,
            reports_dir=reports_dir,
            runs_dir=runs_dir,
            expected_dates_path=Path("planning/runs/expected_dates.txt"),
            positions_path=positions_path,
            focus_positions=focus_positions,
            live=live,
        )
    )


def _daily_push_command(
    slot: PushSlot,
    reports_dir: Path,
    runs_dir: Path,
    expected_dates_path: Path,
    positions_path: Path | None,
    focus_positions: bool,
    live: bool,
) -> str:
    parts = [
        "push-daily",
        f"--slot {slot.key}",
        f"--reports-dir {_path_text(reports_dir)}",
        f"--output-dir {_path_text(runs_dir)}",
        f"--expected-dates {_path_text(expected_dates_path)}",
    ]
    if focus_positions and positions_path is not None:
        parts.append(f"--positions {_path_text(positions_path)}")
    if not focus_positions:
        parts.append("--ignore-positions")
    if live:
        parts.append("--live")
    return " ".join(parts)


def _position_monitor_command(
    runs_dir: Path,
    expected_dates_path: Path,
    positions_path: Path,
    live: bool,
) -> str:
    parts = [
        "position-monitor",
        f"--positions {_path_text(positions_path)}",
        f"--output-dir {_path_text(runs_dir)}",
        f"--state {_path_text(runs_dir / 'position_monitor_state.json')}",
        f"--expected-dates {_path_text(expected_dates_path)}",
    ]
    if live:
        parts.append("--live")
    return " ".join(parts)


def _market_monitor_command(
    runs_dir: Path,
    expected_dates_path: Path,
    positions_path: Path | None,
    live: bool,
) -> str:
    parts = [
        "market-monitor",
    ]
    if positions_path is not None:
        parts.append(f"--positions {_path_text(positions_path)}")
    parts.extend(
        [
            f"--output-dir {_path_text(runs_dir)}",
            f"--state {_path_text(runs_dir / 'market_monitor_state.json')}",
            f"--expected-dates {_path_text(expected_dates_path)}",
        ]
    )
    if live:
        parts.append("--live")
    return " ".join(parts)


def _path_text(path: Path) -> str:
    return str(path).replace("\\", "/")


def _quote_ps_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _quote_ps_command(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"
