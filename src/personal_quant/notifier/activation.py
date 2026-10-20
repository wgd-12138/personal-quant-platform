from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from personal_quant.notifier.windows_tasks import EXPECTED_WINDOWS_TASK_COUNT


@dataclass(frozen=True)
class ActivationStep:
    order: int
    purpose: str
    command: str
    done_when: str


def build_push_activation_report(
    runs_dir: Path = Path("planning/runs"),
    reports_dir: Path = Path("reports"),
    expected_dates_path: Path = Path("planning/runs/expected_dates.txt"),
    project_dir: Path = Path("."),
) -> str:
    go_live_text = _read_required(runs_dir / "push_go_live.md")
    blockers = _go_live_blockers(go_live_text)
    steps = _activation_steps(runs_dir, reports_dir, expected_dates_path, project_dir)
    rows = [
        "# 推送激活清单",
        "",
        "## 当前结论",
        f"- 门禁：{_extract_summary_value(go_live_text, '结果')}",
        f"- 是否可以自动真实推送：{_extract_summary_value(go_live_text, '是否可以自动真实推送')}",
        "",
        "## 当前缺口",
        "| 缺口 | 当前 | 下一步 |",
        "|---|---|---|",
    ]
    if blockers:
        rows.extend(f"| {name} | {_table_text(current)} | {_table_text(next_action)} |" for name, current, next_action in blockers)
    else:
        rows.append("| 暂无 | 全部通过 | 无需处理 |")
    rows.extend(
        [
            "",
            "## 激活步骤",
            "| 顺序 | 目的 | 命令 | 完成标志 |",
            "|---:|---|---|---|",
        ]
    )
    rows.extend(
        f"| {step.order} | {step.purpose} | `{step.command}` | {step.done_when} |"
        for step in steps
    )
    rows.extend(
        [
            "",
            "## 边界",
            "- 本清单只生成步骤，不设置 webhook、不注册系统任务、不发送消息。",
            "- webhook 密钥只应放在本机用户环境变量里，不写入仓库文件。",
        ]
    )
    return "\n".join(rows) + "\n"


def write_push_activation_report(
    output_path: Path,
    runs_dir: Path = Path("planning/runs"),
    reports_dir: Path = Path("reports"),
    expected_dates_path: Path = Path("planning/runs/expected_dates.txt"),
    project_dir: Path = Path("."),
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        build_push_activation_report(
            runs_dir=runs_dir,
            reports_dir=reports_dir,
            expected_dates_path=expected_dates_path,
            project_dir=project_dir,
        ),
        encoding="utf-8",
    )
    return output_path


def _activation_steps(
    runs_dir: Path,
    reports_dir: Path,
    expected_dates_path: Path,
    project_dir: Path,
) -> tuple[ActivationStep, ...]:
    runs = _path_text(runs_dir)
    reports = _path_text(reports_dir)
    expected = _path_text(expected_dates_path)
    project = _path_text(project_dir)
    return (
        ActivationStep(
            1,
            "进入项目目录",
            f"Set-Location {project}; $env:PYTHONPATH='src'",
            "后续命令能找到 personal_quant 包",
        ),
        ActivationStep(
            2,
            "设置飞书 webhook",
            "[Environment]::SetEnvironmentVariable('FEISHU_WEBHOOK_URL', '你的飞书机器人 webhook', 'User')",
            "后续推送命令能读取到 Windows 用户环境变量",
        ),
        ActivationStep(
            3,
            "验证飞书通道",
            f"Set-Location {project}; $env:PYTHONPATH='src'; python -m personal_quant.cli push-test --output-dir {runs} --live",
            "push_channel_test.md 显示 live 且结论通过",
        ),
        ActivationStep(
            4,
            "确认交易日清单",
            f"Set-Location {project}; $env:PYTHONPATH='src'; python -m personal_quant.cli expected-dates-add --path {expected} --date YYYY-MM-DD",
            "需要推送的真实交易日已在 expected_dates.txt",
        ),
        ActivationStep(
            5,
            "注册 Windows 定时任务",
            f"Set-Location {project}; powershell -NoProfile -ExecutionPolicy Bypass -File {runs}/register_push_tasks.ps1",
            f"{EXPECTED_WINDOWS_TASK_COUNT} 个 PersonalQuant 计划任务已注册",
        ),
        ActivationStep(
            6,
            "复查系统任务",
            f"Set-Location {project}; $env:PYTHONPATH='src'; python -m personal_quant.cli push-windows-task-status --output {runs}/push_windows_task_status.md",
            f"任务状态报告显示 {EXPECTED_WINDOWS_TASK_COUNT}/{EXPECTED_WINDOWS_TASK_COUNT} 通过",
        ),
        ActivationStep(
            7,
            "刷新上线自检",
            f"Set-Location {project}; $env:PYTHONPATH='src'; python -m personal_quant.cli push-status --output {runs}/push_status.md --reports-dir {reports} --runs-dir {runs} --expected-dates {expected} --positions data/positions.csv",
            "push_status.md 显示通道和自动定时就绪",
        ),
        ActivationStep(
            8,
            "确认最终门禁",
            f"Set-Location {project}; $env:PYTHONPATH='src'; python -m personal_quant.cli push-go-live --output {runs}/push_go_live.md --runs-dir {runs} --expected-dates {expected}",
            "push_go_live.md 显示可以自动真实推送",
        ),
    )


def _go_live_blockers(text: str) -> list[tuple[str, str, str]]:
    blockers: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        parts = _split_markdown_row(line)
        if len(parts) != 5 or parts[0] == "项目":
            continue
        if parts[3] != "通过":
            blockers.append((parts[0], parts[2], parts[4]))
    return blockers


def _extract_summary_value(text: str, key: str) -> str:
    prefix = f"- {key}："
    for line in text.splitlines():
        if line.startswith(prefix):
            value = line.removeprefix(prefix).strip()
            return value if value else "未记录"
    return "未记录"


def _split_markdown_row(line: str) -> list[str]:
    stripped = line.strip()
    if not stripped.startswith("|") or set(stripped.replace("|", "").strip()) <= {"-"}:
        return []
    return [part.strip() for part in stripped.strip("|").split("|")]


def _read_required(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"PUSH_ACTIVATION_FILE_MISSING:{path}")
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"PUSH_ACTIVATION_FILE_EMPTY:{path}")
    return text


def _path_text(path: Path) -> str:
    return str(path).replace("\\", "/")


def _table_text(value: str) -> str:
    return " ".join(value.replace("|", "/").split())
