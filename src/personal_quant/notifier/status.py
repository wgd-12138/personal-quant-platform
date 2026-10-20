from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from personal_quant.data_center.readiness import check_inputs
from personal_quant.notifier.channel_test import webhook_url_fingerprint
from personal_quant.notifier.schedule import PUSH_SLOTS
from personal_quant.notifier.webhook_env import resolve_feishu_webhook
from personal_quant.report_builder.archive import report_archive_dir
from personal_quant.report_builder.run_audit import load_expected_dates


@dataclass(frozen=True)
class PushStatusCheck:
    name: str
    requirement: str
    current: str
    passed: bool


def build_push_status_report(
    reports_dir: Path = Path("reports"),
    runs_dir: Path = Path("planning/runs"),
    expected_dates_path: Path = Path("planning/runs/expected_dates.txt"),
    positions_path: Path = Path("data/positions.csv"),
    today: dt.date | None = None,
    webhook_url: str | None = None,
) -> str:
    current_date = today if today is not None else dt.date.today()
    expected_dates, expected_error = _read_expected_dates(expected_dates_path)
    resolved_webhook = resolve_feishu_webhook(webhook_url)
    webhook_present = resolved_webhook.url is not None
    current_webhook_fingerprint = webhook_url_fingerprint(resolved_webhook.url) if resolved_webhook.url else None
    channel_test_path = runs_dir / "push_channel_test.md"
    channel_test_passed = _channel_test_passed(channel_test_path, current_webhook_fingerprint)
    task_status_path = runs_dir / "push_windows_task_status.md"
    task_status_passed = _status_report_passed(task_status_path)
    position_readiness = check_inputs(positions_path, focus_positions=True)
    previous_expected = _previous_expected_date(expected_dates, current_date) if expected_dates else None
    pre_open_preview = _push_preview_path(reports_dir, previous_expected) if previous_expected else None
    today_expected = current_date in expected_dates if expected_dates else False

    checks = [
        PushStatusCheck(
            "飞书 webhook",
            "真实发送必须设置 FEISHU_WEBHOOK_URL",
            _webhook_state_text(resolved_webhook.source, webhook_present),
            webhook_present,
        ),
        PushStatusCheck(
            "交易日清单",
            "expected_dates.txt 必须存在且可解析",
            _expected_dates_text(expected_dates, expected_error),
            bool(expected_dates) and expected_error is None,
        ),
        PushStatusCheck(
            "飞书通道测试",
            "必须有一次当前 webhook 的 live 飞书卡片测试通过",
            _channel_test_text(channel_test_path, current_webhook_fingerprint),
            channel_test_passed,
        ),
        PushStatusCheck(
            "开盘前报告",
            "08:50 必须能读到上一确认交易日的 push_preview",
            _pre_open_text(previous_expected, pre_open_preview),
            pre_open_preview is not None and _non_empty_file(pre_open_preview),
        ),
        PushStatusCheck(
            "今日盘中日期",
            "10:35、11:45、14:35、16:10 只在当天写入 expected_dates.txt 时执行",
            f"{current_date.isoformat()} {'已在清单' if today_expected else '不在清单'}",
            today_expected,
        ),
        PushStatusCheck(
            "推送时间表",
            "push_schedule.md 必须已生成",
            _file_state(runs_dir / "push_schedule.md"),
            _non_empty_file(runs_dir / "push_schedule.md"),
        ),
        PushStatusCheck(
            "Windows 定时脚本",
            "register_push_tasks.ps1 必须已生成；是否注册由系统任务决定",
            _file_state(runs_dir / "register_push_tasks.ps1"),
            _non_empty_file(runs_dir / "register_push_tasks.ps1"),
        ),
        PushStatusCheck(
            "Windows 任务注册",
            "全部 PersonalQuant 计划任务必须已注册",
            _status_report_text(task_status_path),
            task_status_passed,
        ),
        PushStatusCheck(
            "持仓实时提醒输入",
            "data/positions.csv 必须是真实持仓文件",
            _position_input_text(position_readiness.position_count, position_readiness.errors),
            position_readiness.ready,
        ),
    ]

    channel_ready = webhook_present and channel_test_passed
    pre_open_ready = channel_ready and checks[1].passed and checks[3].passed
    intraday_ready = channel_ready and checks[1].passed and checks[4].passed
    automation_ready = checks[5].passed and checks[6].passed and checks[7].passed
    monitor_ready = channel_ready and automation_ready and checks[8].passed
    market_monitor_ready = channel_ready and automation_ready
    slot_rows = _slot_rows(pre_open_ready, intraday_ready)

    rows = [
        f"# 推送上线自检（{current_date.isoformat()}）",
        "",
        "## 总判断",
        f"- 真实推送通道：{'就绪' if channel_ready else '未就绪'}",
        f"- 今日 08:50：{'就绪' if pre_open_ready else '未就绪'}",
        f"- 今日盘中/午间/收盘：{'就绪' if intraday_ready else '未就绪'}",
        f"- 持仓实时提醒：{'就绪' if monitor_ready else '未就绪'}",
        f"- 市场突发提醒：{'就绪' if market_monitor_ready else '未就绪'}",
        f"- 自动定时：{'就绪' if automation_ready else '未就绪'}",
        "",
        "## 检查项",
        "| 项目 | 要求 | 当前 | 状态 |",
        "|---|---|---|---|",
    ]
    rows.extend(
        f"| {item.name} | {item.requirement} | {item.current} | {'通过' if item.passed else '未通过'} |"
        for item in checks
    )
    rows.extend(
        [
            "",
            "## 今日节点",
            "| 时间 | 节点 | 状态 |",
            "|---|---|---|",
            *slot_rows,
            "",
            "## 最近回执",
            f"- {_latest_receipt_text(runs_dir)}",
            "",
            "## 边界",
            "- 本命令不联网、不发送消息，只检查真实推送前的本地条件。",
            "- Windows 定时任务是否已经注册，需要运行脚本后由系统任务负责。",
        ]
    )
    return "\n".join(rows) + "\n"


def write_push_status_report(
    output_path: Path,
    reports_dir: Path = Path("reports"),
    runs_dir: Path = Path("planning/runs"),
    expected_dates_path: Path = Path("planning/runs/expected_dates.txt"),
    positions_path: Path = Path("data/positions.csv"),
    today: dt.date | None = None,
    webhook_url: str | None = None,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        build_push_status_report(
            reports_dir=reports_dir,
            runs_dir=runs_dir,
            expected_dates_path=expected_dates_path,
            positions_path=positions_path,
            today=today,
            webhook_url=webhook_url,
        ),
        encoding="utf-8",
    )
    return output_path


def _read_expected_dates(path: Path) -> tuple[list[dt.date], str | None]:
    try:
        return load_expected_dates(path), None
    except Exception as exc:
        return [], str(exc)


def _webhook_state_text(source: str, present: bool) -> str:
    if not present:
        return "未设置"
    return f"已设置（来源：{source}，不显示密钥）"


def _previous_expected_date(expected_dates: list[dt.date], today: dt.date) -> dt.date | None:
    previous_dates = [date for date in expected_dates if date < today]
    return previous_dates[-1] if previous_dates else None


def _push_preview_path(reports_dir: Path, as_of: dt.date) -> Path:
    return report_archive_dir(reports_dir, as_of) / f"push_preview_{as_of.isoformat()}.md"


def _expected_dates_text(expected_dates: list[dt.date], error: str | None) -> str:
    if error is not None:
        return f"读取失败：{_table_text(error)}"
    return f"{len(expected_dates)} 天，最新 {expected_dates[-1].isoformat()}"


def _pre_open_text(previous_expected: dt.date | None, preview_path: Path | None) -> str:
    if previous_expected is None or preview_path is None:
        return "没有早于今天的确认交易日"
    return f"{previous_expected.isoformat()}，{_file_state(preview_path)}"


def _channel_test_text(path: Path, current_webhook_fingerprint: str | None) -> str:
    if not _non_empty_file(path):
        return _file_state(path)
    text = path.read_text(encoding="utf-8")
    mode = _extract_optional_value(text, "模式", "未记录")
    message_format = _extract_optional_value(text, "格式", "未记录")
    status = _extract_optional_value(text, "发送状态", "未记录")
    conclusion = _extract_optional_value(text, "结论", "未记录")
    recorded_fingerprint = _extract_optional_value(text, "Webhook 指纹", "未记录")
    fingerprint_state = _fingerprint_state(recorded_fingerprint, current_webhook_fingerprint)
    return (
        f"{path.name}，模式 {mode}，格式 {message_format}，发送状态 {status}，"
        f"结论 {conclusion}，Webhook 指纹 {fingerprint_state}"
    )


def _channel_test_passed(path: Path, current_webhook_fingerprint: str | None) -> bool:
    if not _non_empty_file(path):
        return False
    text = path.read_text(encoding="utf-8")
    recorded_fingerprint = _extract_optional_value(text, "Webhook 指纹", "")
    return (
        _extract_optional_value(text, "模式", "") == "live"
        and _extract_optional_value(text, "格式", "") == "飞书卡片"
        and _extract_optional_value(text, "发送状态", "") == "已发送"
        and _extract_optional_value(text, "结论", "") == "通过"
        and current_webhook_fingerprint is not None
        and recorded_fingerprint == current_webhook_fingerprint
    )


def _fingerprint_state(recorded_fingerprint: str, current_webhook_fingerprint: str | None) -> str:
    if recorded_fingerprint in {"未记录", ""}:
        return "未记录"
    if current_webhook_fingerprint is None:
        return "未检查"
    return "匹配" if recorded_fingerprint == current_webhook_fingerprint else "不匹配"


def _status_report_text(path: Path) -> str:
    if not _non_empty_file(path):
        return _file_state(path)
    text = path.read_text(encoding="utf-8")
    result = _extract_optional_value(text, "结果", "未记录")
    count = _extract_optional_value(text, "任务数", "未记录")
    return f"{path.name}，结果 {result}，任务数 {count}"


def _status_report_passed(path: Path) -> bool:
    if not _non_empty_file(path):
        return False
    text = path.read_text(encoding="utf-8")
    return _extract_optional_value(text, "结果", "") == "通过"


def _position_input_text(position_count: int, errors: list[str]) -> str:
    if not errors:
        return f"已就绪，持仓 {position_count} 个"
    return "缺口：" + "、".join(_table_text(error) for error in errors)


def _file_state(path: Path) -> str:
    if not path.exists():
        return f"缺失：{_path_text(path)}"
    if not path.is_file():
        return f"不是文件：{_path_text(path)}"
    if path.stat().st_size <= 0:
        return f"空文件：{_path_text(path)}"
    return f"已生成：{_path_text(path)}"


def _non_empty_file(path: Path) -> bool:
    return path.exists() and path.is_file() and path.stat().st_size > 0


def _slot_rows(pre_open_ready: bool, intraday_ready: bool) -> list[str]:
    rows: list[str] = []
    for slot in PUSH_SLOTS:
        ready = pre_open_ready if slot.key == "pre_open" else intraday_ready
        rows.append(f"| {slot.time} | {slot.name} | {'就绪' if ready else '未就绪'} |")
    return rows


def _latest_receipt_text(runs_dir: Path) -> str:
    receipts = sorted(
        runs_dir.glob("push_receipt_*.md"),
        key=lambda path: (path.stat().st_mtime, path.name),
    )
    if not receipts:
        return "暂无推送回执"
    latest = receipts[-1]
    text = latest.read_text(encoding="utf-8")
    mode = _extract_optional_value(text, "模式", "未记录")
    status = _extract_optional_value(text, "发送状态", "未记录")
    return f"{latest.name}，模式 {mode}，发送状态 {status}"


def _extract_optional_value(text: str, key: str, default: str) -> str:
    prefix = f"- {key}："
    for line in text.splitlines():
        if line.startswith(prefix):
            value = line.removeprefix(prefix).strip()
            return _table_text(value) if value else default
    return default


def _table_text(value: str) -> str:
    return " ".join(value.replace("|", "/").split())


def _path_text(path: Path) -> str:
    return str(path).replace("\\", "/")
