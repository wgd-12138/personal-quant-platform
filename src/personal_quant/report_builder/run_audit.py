from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from personal_quant.report_builder.archive import report_archive_dir
from personal_quant.report_builder.manifest import EXPECTED_REPORTS
from personal_quant.report_builder.push_contract import PUSH_CONTENT_CONTRACTS


@dataclass(frozen=True)
class ReportFileAudit:
    file_name: str
    status: str
    size: int


@dataclass(frozen=True)
class PushContentAudit:
    title: str
    status: str
    action: str


@dataclass(frozen=True)
class RunRecord:
    as_of: dt.date
    result: str
    gap: str
    report_files: str
    structure_fingerprint: str


@dataclass(frozen=True)
class StructureAudit:
    file_name: str
    heading_count: int
    fingerprint: str


@dataclass(frozen=True)
class StageGateCheck:
    name: str
    requirement: str
    current: str
    passed: bool


PASS_PUSH_STATUSES = {"可发送", "本阶段跳过"}


def build_run_audit_report(as_of: dt.date, reports_dir: Path, command: str | None = None) -> str:
    archive_dir = report_archive_dir(reports_dir, as_of)
    manifest_text = _read_required(archive_dir / f"manifest_{as_of.isoformat()}.md")
    contract_text = _read_required(archive_dir / f"push_contract_{as_of.isoformat()}.md")
    manifest_strict = _extract_required_value(manifest_text, "严格完整")
    contract_strict = _extract_required_value(contract_text, "严格完整")
    gaps = _extract_required_value(contract_text, "缺口")
    file_audits = _audit_report_files(as_of, archive_dir, manifest_text)
    structure_audits = _audit_structure(as_of, archive_dir)
    overall_structure_fingerprint = _overall_structure_fingerprint(structure_audits)
    push_audits = _audit_push_contract(contract_text)
    file_ready = all(item.status == "已生成" and item.size > 0 for item in file_audits)
    push_ready = all(item.status in PASS_PUSH_STATUSES for item in push_audits)
    strict_ready = manifest_strict == "是" and contract_strict == "是" and gaps == "暂无"
    passed = file_ready and push_ready and strict_ready
    rows = [
        f"# 本地试运行记录（{as_of.isoformat()}）",
        "",
        "## 总判断",
        f"- 结果：{'通过' if passed else '失败'}",
        f"- 严格完整：{contract_strict}",
        f"- 缺口：{gaps}",
        f"- 报告文件：{sum(1 for item in file_audits if item.status == '已生成')}/{len(file_audits)}",
        f"- 整体结构指纹：{overall_structure_fingerprint}",
        f"- 命令：{_format_command(command)}",
        "",
        "## 报告文件",
        "| 文件 | 状态 | 大小 |",
        "|---|---|---:|",
    ]
    rows.extend(f"| {item.file_name} | {item.status} | {item.size} |" for item in file_audits)
    rows.extend(
        [
            "",
            "## 结构指纹",
            "| 文件 | 标题数 | 指纹 |",
            "|---|---:|---|",
        ]
    )
    rows.extend(f"| {item.file_name} | {item.heading_count} | {item.fingerprint} |" for item in structure_audits)
    rows.extend(
        [
            "",
            "## 推送验收",
            "| 推送内容 | 状态 | 处理 |",
            "|---|---|---|",
        ]
    )
    rows.extend(f"| {item.title} | {item.status} | {item.action} |" for item in push_audits)
    rows.extend(
        [
            "",
            "## 连续试运行统计",
            f"- 结论：{'这一天可纳入连续试运行统计' if passed else '这一天不能纳入连续试运行统计'}",
        ]
    )
    return "\n".join(rows) + "\n"


def write_run_audit_report(
    as_of: dt.date,
    reports_dir: Path,
    output_dir: Path,
    command: str | None = None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"run_audit_{as_of.isoformat()}.md"
    output_path.write_text(
        build_run_audit_report(as_of, reports_dir, command=command),
        encoding="utf-8",
    )
    return output_path


def build_failed_run_audit_report(as_of: dt.date, error: Exception, command: str | None = None) -> str:
    error_type = type(error).__name__
    error_message = _format_error_message(str(error))
    gap = f"TRIAL_RUN_FAILED:{error_type}:{error_message}"
    rows = [
        f"# 本地试运行记录（{as_of.isoformat()}）",
        "",
        "## 总判断",
        "- 结果：失败",
        "- 严格完整：否",
        f"- 缺口：{gap}",
        f"- 报告文件：0/{len(EXPECTED_REPORTS)}",
        "- 整体结构指纹：失败无结构",
        f"- 命令：{_format_command(command)}",
        "",
        "## 失败原因",
        f"- 类型：{error_type}",
        f"- 信息：{error_message}",
        "",
        "## 连续试运行统计",
        "- 结论：这一天不能纳入连续试运行统计",
    ]
    return "\n".join(rows) + "\n"


def write_failed_run_audit_report(
    as_of: dt.date,
    output_dir: Path,
    error: Exception,
    command: str | None = None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"run_audit_{as_of.isoformat()}.md"
    output_path.write_text(
        build_failed_run_audit_report(as_of, error, command=command),
        encoding="utf-8",
    )
    return output_path


def build_run_summary_report(
    runs_dir: Path,
    target_days: int = 10,
    expected_dates: list[dt.date] | None = None,
) -> str:
    if target_days <= 0:
        raise ValueError("RUN_SUMMARY_INVALID_TARGET_DAYS")
    records = _load_run_records(runs_dir)
    if not records:
        raise ValueError(f"RUN_SUMMARY_NO_RUN_AUDITS: {runs_dir}")
    latest = records[-1]
    expected_active_dates = _active_expected_dates(expected_dates, latest.as_of)
    missing_expected_dates = _missing_expected_dates(records, expected_active_dates)
    consecutive_passes = _latest_consecutive_passes(records)
    consecutive_stable_passes = _latest_consecutive_stable_passes(records, expected_active_dates)
    remaining = max(target_days - consecutive_stable_passes, 0)
    failed_dates = [record.as_of.isoformat() for record in records if record.result != "通过"]
    structure_consistent, structure_drift_dates = _structure_consistency(records)
    reached = consecutive_stable_passes >= target_days
    rows = [
        "# 本地试运行汇总",
        "",
        "## 总判断",
        f"- 目标：{target_days} 个交易日",
        f"- 已记录：{len(records)} 天",
        f"- 期望交易日：{len(expected_active_dates) if expected_dates is not None else '未启用'} 天",
        f"- 交易日覆盖：{'否' if missing_expected_dates else '是'}",
        f"- 最近连续通过：{consecutive_passes} 天",
        f"- 最近连续稳定通过：{consecutive_stable_passes} 天",
        f"- 还差：{remaining} 天",
        f"- 最新日期：{latest.as_of.isoformat()}",
        f"- 最新结果：{latest.result}",
        f"- 状态：{'已达标' if reached else '未达标'}",
        f"- 结构一致：{'是' if structure_consistent else '否'}",
        "",
        "## 每日记录",
        "| 日期 | 结果 | 缺口 | 报告文件 |",
        "|---|---|---|---|",
    ]
    rows.extend(
        f"| {record.as_of.isoformat()} | {record.result} | {record.gap} | {record.report_files} |"
        for record in records
    )
    rows.extend(
        [
            "",
            "## 失败追踪",
            f"- 失败日期：{', '.join(failed_dates) if failed_dates else '暂无'}",
            f"- 漏跑交易日：{', '.join(date.isoformat() for date in missing_expected_dates) if missing_expected_dates else '暂无'}",
            f"- 结构变动日期：{', '.join(structure_drift_dates) if structure_drift_dates else '暂无'}",
            "- 统计口径：按已生成的运行记录统计；交易日是否纳入，以当天是否生成运行记录为准",
        ]
    )
    return "\n".join(rows) + "\n"


def write_run_summary_report(
    runs_dir: Path,
    target_days: int = 10,
    expected_dates: list[dt.date] | None = None,
) -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    output_path = runs_dir / "run_summary.md"
    output_path.write_text(
        build_run_summary_report(runs_dir, target_days=target_days, expected_dates=expected_dates),
        encoding="utf-8",
    )
    return output_path


def build_stage_gate_report(runs_dir: Path) -> str:
    summary_text = _read_required(runs_dir / "run_summary.md")
    checks = _stage_gate_checks(summary_text)
    passed = all(item.passed for item in checks)
    rows = [
        "# 本地试运行阶段门禁",
        "",
        "## 总判断",
        f"- 结果：{'通过' if passed else '未通过'}",
        f"- 是否允许进入推送阶段：{'是' if passed else '否'}",
        "- 汇总文件：run_summary.md",
        "",
        "## 检查项",
        "| 检查项 | 要求 | 当前 | 状态 |",
        "|---|---|---|---|",
    ]
    rows.extend(
        f"| {item.name} | {item.requirement} | {item.current} | {'通过' if item.passed else '未通过'} |"
        for item in checks
    )
    rows.extend(
        [
            "",
            "## 处理",
            f"- {'可以进入推送模块开发' if passed else '继续本地试运行，不进入推送模块'}",
        ]
    )
    return "\n".join(rows) + "\n"


def write_stage_gate_report(runs_dir: Path) -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    output_path = runs_dir / "stage_gate_09.md"
    output_path.write_text(build_stage_gate_report(runs_dir), encoding="utf-8")
    return output_path


def build_trial_status_report(
    runs_dir: Path,
    expected_dates_path: Path,
    reports_dir: Path = Path("reports"),
    target_days: int = 10,
    focus_positions: bool = True,
    today: dt.date | None = None,
) -> str:
    summary_text = _read_required(runs_dir / "run_summary.md")
    gate_text = _read_required(runs_dir / "stage_gate_09.md")
    expected_dates = load_expected_dates(expected_dates_path)
    next_date = _pending_expected_date_text(runs_dir, expected_dates)
    current_date = today if today is not None else dt.date.today()
    stage_status = _extract_required_value(summary_text, "状态")
    stable_days = _extract_required_value(summary_text, "最近连续稳定通过")
    remaining = _extract_required_value(summary_text, "还差")
    latest_date = _extract_required_value(summary_text, "最新日期")
    gate_allowed = _extract_required_value(gate_text, "是否允许进入推送阶段")
    rows = [
        "# 本地试运行状态",
        "",
        "## 总判断",
        f"- 最新运行日期：{latest_date}",
        f"- 下一个待运行日期：{next_date}",
        f"- 门禁：{gate_allowed}",
        f"- 阶段：{stage_status}，{stable_days}，还差 {remaining}",
        "",
        "## 下一步",
        f"- 下一步：{_trial_status_next_action(next_date, current_date, gate_allowed)}",
        f"- 推荐命令：{_trial_status_command(next_date, reports_dir, runs_dir, expected_dates_path, target_days, focus_positions, current_date, gate_allowed)}",
    ]
    return "\n".join(rows) + "\n"


def write_trial_status_report(
    runs_dir: Path,
    expected_dates_path: Path,
    reports_dir: Path = Path("reports"),
    target_days: int = 10,
    focus_positions: bool = True,
    today: dt.date | None = None,
) -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    output_path = runs_dir / "trial_status.md"
    output_path.write_text(
        build_trial_status_report(
            runs_dir,
            expected_dates_path,
            reports_dir=reports_dir,
            target_days=target_days,
            focus_positions=focus_positions,
            today=today,
        ),
        encoding="utf-8",
    )
    return output_path


def load_expected_dates(path: Path) -> list[dt.date]:
    text = _read_required(path)
    dates: list[dt.date] = []
    seen: set[dt.date] = set()
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            date_value = dt.date.fromisoformat(line)
        except ValueError as exc:
            raise ValueError(f"RUN_SUMMARY_INVALID_EXPECTED_DATE:{line_no}:{line}") from exc
        if date_value in seen:
            raise ValueError(f"RUN_SUMMARY_DUPLICATE_EXPECTED_DATE:{line_no}:{line}")
        seen.add(date_value)
        dates.append(date_value)
    if not dates:
        raise ValueError(f"RUN_SUMMARY_NO_EXPECTED_DATES: {path}")
    return sorted(dates)


def add_expected_dates(path: Path, dates: list[dt.date]) -> Path:
    if not dates:
        raise ValueError("EXPECTED_DATE_EMPTY_INPUT")
    existing = load_expected_dates(path) if path.exists() else []
    existing_set = set(existing)
    new_set = set(dates)
    duplicate_input = _duplicate_dates(dates)
    if duplicate_input:
        raise ValueError(f"EXPECTED_DATE_DUPLICATE_INPUT:{duplicate_input.isoformat()}")
    duplicate_existing = sorted(existing_set & new_set)
    if duplicate_existing:
        raise ValueError(f"EXPECTED_DATE_ALREADY_EXISTS:{duplicate_existing[0].isoformat()}")
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = sorted(existing_set | new_set)
    path.write_text("".join(f"{date.isoformat()}\n" for date in merged), encoding="utf-8")
    return path


def select_next_expected_date(runs_dir: Path, expected_dates: list[dt.date]) -> dt.date:
    if not expected_dates:
        raise ValueError("TRIAL_RUN_NO_EXPECTED_DATES")
    recorded_dates = {record.as_of for record in _load_run_records(runs_dir)}
    for expected_date in sorted(expected_dates):
        if expected_date not in recorded_dates:
            return expected_date
    raise ValueError("TRIAL_RUN_NO_PENDING_EXPECTED_DATE")


def _pending_expected_date_text(runs_dir: Path, expected_dates: list[dt.date]) -> str:
    try:
        return select_next_expected_date(runs_dir, expected_dates).isoformat()
    except ValueError as exc:
        if str(exc) == "TRIAL_RUN_NO_PENDING_EXPECTED_DATE":
            return "暂无"
        raise


def _trial_status_next_action(next_date: str, today: dt.date, gate_allowed: str) -> str:
    if gate_allowed == "是":
        return "进入推送模块开发"
    if next_date == "暂无":
        return "先用 expected-dates-add 添加已确认交易日"
    if dt.date.fromisoformat(next_date) > today:
        return f"等待 {next_date}，当天再执行 trial-run-next"
    return "执行 trial-run-next"


def _trial_status_command(
    next_date: str,
    reports_dir: Path,
    runs_dir: Path,
    expected_dates_path: Path,
    target_days: int,
    focus_positions: bool,
    today: dt.date,
    gate_allowed: str,
) -> str:
    if gate_allowed == "是":
        return "暂不执行"
    if next_date == "暂无":
        return (
            "python -m personal_quant.cli expected-dates-add "
            f"--path {expected_dates_path} --date YYYY-MM-DD"
        )
    if dt.date.fromisoformat(next_date) > today:
        return "暂不执行"
    parts = [
        "python -m personal_quant.cli trial-run-next",
        f"--reports-dir {reports_dir}",
        f"--runs-dir {runs_dir}",
        f"--target-days {target_days}",
        f"--expected-dates {expected_dates_path}",
    ]
    if not focus_positions:
        parts.append("--ignore-positions")
    return " ".join(parts)


def _duplicate_dates(dates: list[dt.date]) -> dt.date | None:
    seen: set[dt.date] = set()
    for date in dates:
        if date in seen:
            return date
        seen.add(date)
    return None


def _stage_gate_checks(summary_text: str) -> list[StageGateCheck]:
    status = _extract_required_value(summary_text, "状态")
    stable_days = _extract_required_value(summary_text, "最近连续稳定通过")
    remaining = _extract_required_value(summary_text, "还差")
    expected_dates = _extract_required_value(summary_text, "期望交易日")
    coverage = _extract_required_value(summary_text, "交易日覆盖")
    structure = _extract_required_value(summary_text, "结构一致")
    failed_dates = _extract_required_value(summary_text, "失败日期")
    missing_dates = _extract_required_value(summary_text, "漏跑交易日")
    drift_dates = _extract_required_value(summary_text, "结构变动日期")
    return [
        StageGateCheck(
            "连续稳定通过",
            "状态必须已达标",
            f"{status}，{stable_days}，还差 {remaining}",
            status == "已达标",
        ),
        StageGateCheck(
            "期望交易日清单",
            "必须启用",
            expected_dates,
            expected_dates not in {"未启用", "0 天"},
        ),
        StageGateCheck("交易日覆盖", "必须为是", coverage, coverage == "是"),
        StageGateCheck("结构一致", "必须为是", structure, structure == "是"),
        StageGateCheck("失败日期", "必须为暂无", failed_dates, failed_dates == "暂无"),
        StageGateCheck("漏跑交易日", "必须为暂无", missing_dates, missing_dates == "暂无"),
        StageGateCheck("结构变动日期", "必须为暂无", drift_dates, drift_dates == "暂无"),
    ]


def _audit_report_files(as_of: dt.date, reports_dir: Path, manifest_text: str) -> list[ReportFileAudit]:
    manifest_rows = _parse_manifest_rows(manifest_text)
    audits: list[ReportFileAudit] = []
    for expected in EXPECTED_REPORTS:
        file_name = f"{expected.prefix}_{as_of.isoformat()}.md"
        path = reports_dir / file_name
        if not path.exists():
            audits.append(ReportFileAudit(file_name, "缺失", 0))
            continue
        size = path.stat().st_size
        if file_name not in manifest_rows:
            audits.append(ReportFileAudit(file_name, "清单缺失", size))
            continue
        status = manifest_rows[file_name]
        audits.append(ReportFileAudit(file_name, status, size))
    return audits


def _load_run_records(runs_dir: Path) -> list[RunRecord]:
    records: list[RunRecord] = []
    for path in sorted(runs_dir.glob("run_audit_*.md")):
        as_of = _date_from_run_audit_name(path)
        text = _read_required(path)
        records.append(
            RunRecord(
                as_of=as_of,
                result=_extract_required_value(text, "结果"),
                gap=_extract_required_value(text, "缺口"),
                report_files=_extract_required_value(text, "报告文件"),
                structure_fingerprint=_extract_optional_value(text, "整体结构指纹", "未记录"),
            )
        )
    return sorted(records, key=lambda item: item.as_of)


def _date_from_run_audit_name(path: Path) -> dt.date:
    stem = path.stem
    prefix = "run_audit_"
    if not stem.startswith(prefix):
        raise ValueError(f"RUN_SUMMARY_INVALID_RUN_AUDIT_NAME: {path.name}")
    return dt.date.fromisoformat(stem.removeprefix(prefix))


def _latest_consecutive_passes(records: list[RunRecord]) -> int:
    count = 0
    for record in reversed(records):
        if record.result != "通过":
            break
        count += 1
    return count


def _latest_consecutive_stable_passes(
    records: list[RunRecord],
    expected_dates: list[dt.date] | None = None,
) -> int:
    if not records or records[-1].result != "通过":
        return 0
    latest_fingerprint = records[-1].structure_fingerprint
    if expected_dates:
        by_date = {record.as_of: record for record in records}
        count = 0
        for expected_date in reversed(expected_dates):
            record = by_date.get(expected_date)
            if record is None or record.result != "通过" or record.structure_fingerprint != latest_fingerprint:
                break
            count += 1
        return count
    count = 0
    for record in reversed(records):
        if record.result != "通过" or record.structure_fingerprint != latest_fingerprint:
            break
        count += 1
    return count


def _active_expected_dates(expected_dates: list[dt.date] | None, latest_date: dt.date) -> list[dt.date]:
    if expected_dates is None:
        return []
    return sorted(date for date in expected_dates if date <= latest_date)


def _missing_expected_dates(records: list[RunRecord], expected_dates: list[dt.date]) -> list[dt.date]:
    recorded_dates = {record.as_of for record in records}
    return [date for date in expected_dates if date not in recorded_dates]


def _structure_consistency(records: list[RunRecord]) -> tuple[bool, list[str]]:
    passed = [record for record in records if record.result == "通过"]
    if not passed:
        return True, []
    baseline = passed[0].structure_fingerprint
    drift_dates = [
        record.as_of.isoformat()
        for record in passed[1:]
        if record.structure_fingerprint != baseline
    ]
    return not drift_dates, drift_dates


def _audit_push_contract(contract_text: str) -> list[PushContentAudit]:
    found = _parse_push_rows(contract_text)
    audits: list[PushContentAudit] = []
    for contract in PUSH_CONTENT_CONTRACTS:
        if contract.title in found:
            audits.append(found[contract.title])
        else:
            audits.append(PushContentAudit(contract.title, "缺失", "推送合同缺少此项"))
    return audits


def _audit_structure(as_of: dt.date, reports_dir: Path) -> list[StructureAudit]:
    audits: list[StructureAudit] = []
    for expected in EXPECTED_REPORTS:
        file_name = f"{expected.prefix}_{as_of.isoformat()}.md"
        path = reports_dir / file_name
        if not path.exists():
            audits.append(StructureAudit(file_name, 0, "缺失"))
            continue
        headings = _extract_normalized_headings(path.read_text(encoding="utf-8"))
        audits.append(StructureAudit(file_name, len(headings), _fingerprint("\n".join(headings))))
    return audits


def _extract_normalized_headings(text: str) -> list[str]:
    headings: list[str] = []
    for line in text.splitlines():
        if not line.startswith("#"):
            continue
        stripped = line.strip()
        if not stripped or not stripped.lstrip("#").startswith(" "):
            continue
        headings.append(_normalize_heading(stripped))
    return headings


def _normalize_heading(heading: str) -> str:
    without_iso_date = re.sub(r"\d{4}-\d{2}-\d{2}", "YYYY-MM-DD", heading)
    return re.sub(r"\s+", " ", without_iso_date).strip()


def _overall_structure_fingerprint(items: list[StructureAudit]) -> str:
    return _fingerprint(
        "\n".join(f"{_normalize_heading(item.file_name)}:{item.fingerprint}" for item in items)
    )


def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _parse_manifest_rows(text: str) -> dict[str, str]:
    rows: dict[str, str] = {}
    for line in text.splitlines():
        parts = _split_markdown_row(line)
        if len(parts) != 4 or parts[0] == "文件" or not parts[0].endswith(".md"):
            continue
        rows[parts[0]] = parts[2]
    return rows


def _parse_push_rows(text: str) -> dict[str, PushContentAudit]:
    titles = {contract.title for contract in PUSH_CONTENT_CONTRACTS}
    rows: dict[str, PushContentAudit] = {}
    for line in text.splitlines():
        parts = _split_markdown_row(line)
        if len(parts) < 5 or parts[0] not in titles:
            continue
        rows[parts[0]] = PushContentAudit(parts[0], parts[3], parts[4])
    return rows


def _split_markdown_row(line: str) -> list[str]:
    stripped = line.strip()
    if not stripped.startswith("|") or set(stripped.replace("|", "").strip()) <= {"-"}:
        return []
    return [part.strip() for part in stripped.strip("|").split("|")]


def _read_required(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"RUN_AUDIT_FILE_MISSING: {path}")
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"RUN_AUDIT_FILE_EMPTY: {path}")
    return text


def _extract_required_value(text: str, key: str) -> str:
    prefix = f"- {key}："
    for line in text.splitlines():
        if line.startswith(prefix):
            value = line.removeprefix(prefix).strip()
            if not value:
                raise ValueError(f"RUN_AUDIT_FIELD_EMPTY: {key}")
            return value
    raise ValueError(f"RUN_AUDIT_FIELD_MISSING: {key}")


def _extract_optional_value(text: str, key: str, default: str) -> str:
    prefix = f"- {key}："
    for line in text.splitlines():
        if line.startswith(prefix):
            value = line.removeprefix(prefix).strip()
            return value if value else default
    return default


def _format_command(command: str | None) -> str:
    if command is None or not command.strip():
        return "未记录"
    return f"`{command.strip()}`"


def _format_error_message(message: str) -> str:
    cleaned = " ".join(message.split()).replace("|", "/")
    return cleaned if cleaned else "EMPTY_ERROR_MESSAGE"
