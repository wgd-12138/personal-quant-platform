from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from personal_quant.notifier.schedule import PUSH_SLOTS

POSITION_MONITOR_TASK_NAME = "PersonalQuant_position_monitor"
POSITION_MONITOR_SLOT_NAME = "持仓实时监控"
POSITION_MONITOR_TIME = "09:30"
MARKET_MONITOR_TASK_NAME = "PersonalQuant_market_monitor"
MARKET_MONITOR_SLOT_NAME = "市场突发监控"
MARKET_MONITOR_TIME = "09:30"
EXPECTED_WINDOWS_TASK_COUNT = len(PUSH_SLOTS) + 2


@dataclass(frozen=True)
class WindowsTaskStatus:
    task_name: str
    slot_name: str
    time: str
    exists: bool
    state: str
    trigger: str
    action: str


Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def query_windows_task_status(runner: Runner | None = None) -> tuple[WindowsTaskStatus, ...]:
    run_command = runner if runner is not None else _run_powershell
    task_names = [_task_name(slot.key) for slot in PUSH_SLOTS] + [POSITION_MONITOR_TASK_NAME, MARKET_MONITOR_TASK_NAME]
    command = _powershell_status_command(task_names)
    result = run_command(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ]
    )
    if result.returncode != 0:
        raise RuntimeError(f"WINDOWS_TASK_STATUS_FAILED:{_clean(result.stderr)}")
    raw_items = json.loads(result.stdout)
    if isinstance(raw_items, dict):
        items = [raw_items]
    elif isinstance(raw_items, list):
        items = raw_items
    else:
        raise RuntimeError("WINDOWS_TASK_STATUS_INVALID_JSON")
    by_name = {str(item.get("TaskName", "")): item for item in items}
    statuses: list[WindowsTaskStatus] = []
    for slot in PUSH_SLOTS:
        name = _task_name(slot.key)
        item = by_name.get(name, {})
        statuses.append(
            WindowsTaskStatus(
                task_name=name,
                slot_name=slot.name,
                time=slot.time,
                exists=bool(item.get("Exists", False)),
                state=str(item.get("State", "缺失")),
                trigger=str(item.get("Trigger", "")),
                action=str(item.get("Action", "")),
            )
        )
    monitor_item = by_name.get(POSITION_MONITOR_TASK_NAME, {})
    statuses.append(
        WindowsTaskStatus(
            task_name=POSITION_MONITOR_TASK_NAME,
            slot_name=POSITION_MONITOR_SLOT_NAME,
            time=POSITION_MONITOR_TIME,
            exists=bool(monitor_item.get("Exists", False)),
            state=str(monitor_item.get("State", "缺失")),
            trigger=str(monitor_item.get("Trigger", "")),
            action=str(monitor_item.get("Action", "")),
        )
    )
    market_item = by_name.get(MARKET_MONITOR_TASK_NAME, {})
    statuses.append(
        WindowsTaskStatus(
            task_name=MARKET_MONITOR_TASK_NAME,
            slot_name=MARKET_MONITOR_SLOT_NAME,
            time=MARKET_MONITOR_TIME,
            exists=bool(market_item.get("Exists", False)),
            state=str(market_item.get("State", "缺失")),
            trigger=str(market_item.get("Trigger", "")),
            action=str(market_item.get("Action", "")),
        )
    )
    return tuple(statuses)


def build_windows_task_status_report(statuses: tuple[WindowsTaskStatus, ...]) -> str:
    passed = all(item.exists for item in statuses) and len(statuses) == EXPECTED_WINDOWS_TASK_COUNT
    rows = [
        "# Windows 定时任务状态",
        "",
        "## 总判断",
        f"- 结果：{'通过' if passed else '未通过'}",
        f"- 任务数：{sum(1 for item in statuses if item.exists)}/{EXPECTED_WINDOWS_TASK_COUNT}",
        "",
        "## 任务清单",
        "| 任务 | 节点 | 时间 | 注册 | 状态 | 触发器 | 动作 |",
        "|---|---|---|---|---|---|---|",
    ]
    rows.extend(
        (
            f"| {item.task_name} | {item.slot_name} | {item.time} | "
            f"{'已注册' if item.exists else '缺失'} | {_table_text(item.state)} | "
            f"{_table_text(item.trigger) or '未记录'} | {_table_text(item.action) or '未记录'} |"
        )
        for item in statuses
    )
    rows.extend(
        [
            "",
            "## 边界",
            "- 本报告只读取 Windows 计划任务，不注册、不修改任务。",
        ]
    )
    return "\n".join(rows) + "\n"


def write_windows_task_status_report(
    output_path: Path,
    statuses: tuple[WindowsTaskStatus, ...] | None = None,
    runner: Runner | None = None,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    actual_statuses = statuses if statuses is not None else query_windows_task_status(runner=runner)
    output_path.write_text(build_windows_task_status_report(actual_statuses), encoding="utf-8")
    return output_path


def _task_name(slot_key: str) -> str:
    return f"PersonalQuant_{slot_key}"


def _powershell_status_command(task_names: list[str]) -> str:
    quoted_names = ", ".join("'" + name.replace("'", "''") + "'" for name in task_names)
    return (
        "$ErrorActionPreference = 'Stop'; "
        f"$names = @({quoted_names}); "
        "$items = foreach ($name in $names) { "
        "$task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue; "
        "if ($null -eq $task) { "
        "[pscustomobject]@{TaskName=$name; Exists=$false; State='缺失'; Trigger=''; Action=''} "
        "} else { "
        "$triggers = ($task.Triggers | ForEach-Object { $_.StartBoundary }) -join ','; "
        "$actions = ($task.Actions | ForEach-Object { $_.Execute + ' ' + $_.Arguments }) -join ' '; "
        "[pscustomobject]@{TaskName=$name; Exists=$true; State=$task.State.ToString(); Trigger=$triggers; Action=$actions} "
        "} "
        "}; "
        "$items | ConvertTo-Json -Compress"
    )


def _run_powershell(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, encoding="utf-8")


def _clean(value: str) -> str:
    return " ".join(value.replace("|", "/").split())


def _table_text(value: str) -> str:
    return " ".join(value.replace("|", "/").split())
