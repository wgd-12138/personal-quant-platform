from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from personal_quant.notifier.windows_tasks import EXPECTED_WINDOWS_TASK_COUNT


@dataclass(frozen=True)
class GoLiveCheck:
    name: str
    requirement: str
    current: str
    passed: bool
    next_action: str


def build_push_go_live_report(
    runs_dir: Path = Path("planning/runs"),
    expected_dates_path: Path = Path("planning/runs/expected_dates.txt"),
) -> str:
    status_text = _read_required(runs_dir / "push_status.md")
    checks = _build_checks(status_text, expected_dates_path)
    passed = all(item.passed for item in checks)
    rows = [
        "# 推送上线门禁",
        "",
        "## 总判断",
        f"- 结果：{'通过' if passed else '未通过'}",
        f"- 是否可以自动真实推送：{'是' if passed else '否'}",
        "",
        "## 检查项",
        "| 项目 | 要求 | 当前 | 状态 | 下一步 |",
        "|---|---|---|---|---|",
    ]
    rows.extend(
        (
            f"| {item.name} | {item.requirement} | {_table_text(item.current)} | "
            f"{'通过' if item.passed else '未通过'} | {_table_text(item.next_action)} |"
        )
        for item in checks
    )
    rows.extend(
        [
            "",
            "## 边界",
            "- 本门禁只读现有报告，不联网、不注册任务、不发送消息。",
            "- 交易日是否加入清单必须由用户确认，不自动猜节假日。",
        ]
    )
    return "\n".join(rows) + "\n"


def write_push_go_live_report(
    output_path: Path,
    runs_dir: Path = Path("planning/runs"),
    expected_dates_path: Path = Path("planning/runs/expected_dates.txt"),
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        build_push_go_live_report(runs_dir=runs_dir, expected_dates_path=expected_dates_path),
        encoding="utf-8",
    )
    return output_path


def _build_checks(status_text: str, expected_dates_path: Path) -> list[GoLiveCheck]:
    check_rows = _parse_check_rows(status_text)
    summary = _extract_summary(status_text)
    return [
        _check_from_row(
            check_rows,
            "飞书 webhook",
            "必须设置 FEISHU_WEBHOOK_URL",
            "设置用户环境变量 FEISHU_WEBHOOK_URL",
        ),
        _check_from_row(
            check_rows,
            "飞书通道测试",
            "当前 webhook 必须 live 飞书卡片测试通过",
            "运行 python -m personal_quant.cli push-test --output-dir planning/runs --live",
        ),
        _check_from_row(
            check_rows,
            "交易日清单",
            "expected_dates.txt 必须可解析",
            f"运行 python -m personal_quant.cli expected-dates-add --path {expected_dates_path} --date YYYY-MM-DD",
        ),
        _check_from_row(
            check_rows,
            "推送时间表",
            "每日推送时间表必须已生成",
            "运行 python -m personal_quant.cli push-schedule --output planning/runs/push_schedule.md --reports-dir reports --runs-dir planning/runs --positions data/positions.csv",
        ),
        _check_from_row(
            check_rows,
            "Windows 定时脚本",
            "定时任务注册脚本必须已生成",
            "运行 python -m personal_quant.cli push-windows-tasks --output planning/runs/register_push_tasks.ps1 --project-dir 当前项目目录 --reports-dir reports --runs-dir planning/runs --positions data/positions.csv --live",
        ),
        _check_from_row(
            check_rows,
            "Windows 任务注册",
            f"{EXPECTED_WINDOWS_TASK_COUNT} 个 PersonalQuant 计划任务必须已注册",
            "以用户确认后运行 planning/runs/register_push_tasks.ps1，再运行 push-windows-task-status",
        ),
        _check_from_row(
            check_rows,
            "持仓实时提醒输入",
            "真实持仓文件必须就绪",
            "补齐 data/positions.csv 后重新运行 push-status",
        ),
        GoLiveCheck(
            "自动定时",
            "push-status 总判断必须就绪",
            summary.get("自动定时", "未记录"),
            summary.get("自动定时") == "就绪",
            "无需处理" if summary.get("自动定时") == "就绪" else "先完成 Windows 定时脚本和系统任务注册",
        ),
        GoLiveCheck(
            "真实推送通道",
            "push-status 总判断必须就绪",
            summary.get("真实推送通道", "未记录"),
            summary.get("真实推送通道") == "就绪",
            "无需处理" if summary.get("真实推送通道") == "就绪" else "先设置 webhook 并完成 push-test --live",
        ),
    ]


def _check_from_row(rows: dict[str, tuple[str, str]], name: str, requirement: str, next_action: str) -> GoLiveCheck:
    current, status = rows.get(name, ("缺失", "未通过"))
    return GoLiveCheck(
        name=name,
        requirement=requirement,
        current=current,
        passed=status == "通过",
        next_action="无需处理" if status == "通过" else next_action,
    )


def _parse_check_rows(text: str) -> dict[str, tuple[str, str]]:
    rows: dict[str, tuple[str, str]] = {}
    for line in text.splitlines():
        parts = _split_markdown_row(line)
        if len(parts) != 4 or parts[0] in {"项目", "---"}:
            continue
        rows[parts[0]] = (parts[2], parts[3])
    return rows


def _extract_summary(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        if not line.startswith("- ") or "：" not in line:
            continue
        key, value = line.removeprefix("- ").split("：", 1)
        values[key.strip()] = value.strip()
    return values


def _split_markdown_row(line: str) -> list[str]:
    stripped = line.strip()
    if not stripped.startswith("|") or set(stripped.replace("|", "").strip()) <= {"-"}:
        return []
    return [part.strip() for part in stripped.strip("|").split("|")]


def _read_required(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"PUSH_GO_LIVE_FILE_MISSING:{path}")
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"PUSH_GO_LIVE_FILE_EMPTY:{path}")
    return text


def _table_text(value: str) -> str:
    return " ".join(value.replace("|", "/").split())
