import datetime as dt
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from personal_quant.report_builder.manifest import EXPECTED_REPORTS, build_manifest_report
from personal_quant.report_builder.run_audit import (
    add_expected_dates,
    build_run_audit_report,
    build_run_summary_report,
    build_stage_gate_report,
    build_trial_status_report,
    load_expected_dates,
    select_next_expected_date,
    write_run_audit_report,
    write_run_summary_report,
    write_stage_gate_report,
    write_trial_status_report,
)


class RunAuditTests(unittest.TestCase):
    def test_run_audit_marks_complete_run_as_passed(self):
        with TemporaryDirectory() as tmp:
            reports_dir = Path(tmp) / "reports"
            archive_dir = reports_dir / "2026-07-09"
            archive_dir.mkdir(parents=True)
            for expected in EXPECTED_REPORTS:
                (archive_dir / f"{expected.prefix}_2026-07-09.md").write_text("# ok\n", encoding="utf-8")
            (archive_dir / "manifest_2026-07-09.md").write_text(
                build_manifest_report(dt.date(2026, 7, 9), archive_dir, True, []),
                encoding="utf-8",
            )
            (archive_dir / "push_contract_2026-07-09.md").write_text(
                _push_contract(strict_ready=True),
                encoding="utf-8",
            )

            report = build_run_audit_report(
                dt.date(2026, 7, 9),
                reports_dir,
                command="daily-pack --split --ignore-positions",
            )

            self.assertIn("结果：通过", report)
            self.assertIn("严格完整：是", report)
            self.assertIn("缺口：暂无", report)
            self.assertIn("命令：`daily-pack --split --ignore-positions`", report)
            self.assertIn("daily_pack_2026-07-09.md", report)
            self.assertIn("| 盘中实时盘面 | 可发送 | 暂无 |", report)
            self.assertIn("| 持仓个股操作 | 本阶段跳过 | 用户已选择暂不关注持仓个股 |", report)
            self.assertIn("## 结构指纹", report)
            self.assertIn("整体结构指纹：", report)
            self.assertIn("| daily_pack_2026-07-09.md | 1 |", report)
            self.assertIn("这一天可纳入连续试运行统计", report)

    def test_run_audit_normalizes_dates_in_structure_fingerprint(self):
        with TemporaryDirectory() as tmp:
            reports_dir = Path(tmp) / "reports"
            archive_dir = reports_dir / "2026-07-09"
            archive_dir.mkdir(parents=True)
            for expected in EXPECTED_REPORTS:
                (archive_dir / f"{expected.prefix}_2026-07-09.md").write_text(
                    f"# {expected.name}（2026-07-09）\n## 总判断\n",
                    encoding="utf-8",
                )
            (archive_dir / "manifest_2026-07-09.md").write_text(
                build_manifest_report(dt.date(2026, 7, 9), archive_dir, True, []),
                encoding="utf-8",
            )
            (archive_dir / "push_contract_2026-07-09.md").write_text(
                _push_contract(strict_ready=True),
                encoding="utf-8",
            )

            today_report = build_run_audit_report(dt.date(2026, 7, 9), reports_dir)

            next_archive_dir = reports_dir / "2026-07-10"
            next_archive_dir.mkdir()
            for expected in EXPECTED_REPORTS:
                (next_archive_dir / f"{expected.prefix}_2026-07-10.md").write_text(
                    f"# {expected.name}（2026-07-10）\n## 总判断\n",
                    encoding="utf-8",
                )
            (next_archive_dir / "manifest_2026-07-10.md").write_text(
                build_manifest_report(dt.date(2026, 7, 10), next_archive_dir, True, []),
                encoding="utf-8",
            )
            (next_archive_dir / "push_contract_2026-07-10.md").write_text(
                _push_contract(strict_ready=True, day="2026-07-10"),
                encoding="utf-8",
            )

            next_report = build_run_audit_report(dt.date(2026, 7, 10), reports_dir)

            self.assertEqual(_extract_overall_fingerprint(today_report), _extract_overall_fingerprint(next_report))

    def test_run_audit_marks_missing_daily_report_as_failed(self):
        with TemporaryDirectory() as tmp:
            reports_dir = Path(tmp) / "reports"
            archive_dir = reports_dir / "2026-07-09"
            archive_dir.mkdir(parents=True)
            (archive_dir / "daily_pack_2026-07-09.md").write_text("# ok\n", encoding="utf-8")
            (archive_dir / "push_contract_2026-07-09.md").write_text(
                _push_contract(strict_ready=True),
                encoding="utf-8",
            )
            (archive_dir / "manifest_2026-07-09.md").write_text(
                build_manifest_report(dt.date(2026, 7, 9), archive_dir, True, []),
                encoding="utf-8",
            )

            report = build_run_audit_report(dt.date(2026, 7, 9), reports_dir)

            self.assertIn("结果：失败", report)
            self.assertIn("缺失", report)
            self.assertIn("这一天不能纳入连续试运行统计", report)

    def test_write_run_audit_report_creates_run_file(self):
        with TemporaryDirectory() as tmp:
            reports_dir = Path(tmp) / "reports"
            output_dir = Path(tmp) / "planning" / "runs"
            archive_dir = reports_dir / "2026-07-09"
            archive_dir.mkdir(parents=True)
            for expected in EXPECTED_REPORTS:
                (archive_dir / f"{expected.prefix}_2026-07-09.md").write_text("# ok\n", encoding="utf-8")
            (archive_dir / "manifest_2026-07-09.md").write_text(
                build_manifest_report(dt.date(2026, 7, 9), archive_dir, True, []),
                encoding="utf-8",
            )
            (archive_dir / "push_contract_2026-07-09.md").write_text(
                _push_contract(strict_ready=True),
                encoding="utf-8",
            )

            path = write_run_audit_report(dt.date(2026, 7, 9), reports_dir, output_dir)

            self.assertEqual(path.name, "run_audit_2026-07-09.md")
            self.assertTrue(path.exists())
            self.assertIn("结果：通过", path.read_text(encoding="utf-8"))

    def test_run_summary_tracks_latest_consecutive_passes_and_failures(self):
        with TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            _write_run_record(runs_dir, "2026-07-07", "通过", "暂无", "9/9")
            _write_run_record(runs_dir, "2026-07-08", "失败", "FIELD_MISSING", "8/9")
            _write_run_record(runs_dir, "2026-07-09", "通过", "暂无", "9/9")

            report = build_run_summary_report(runs_dir, target_days=10)

            self.assertIn("# 本地试运行汇总", report)
            self.assertIn("目标：10 个交易日", report)
            self.assertIn("已记录：3 天", report)
            self.assertIn("最近连续通过：1 天", report)
            self.assertIn("还差：9 天", report)
            self.assertIn("最新日期：2026-07-09", report)
            self.assertIn("状态：未达标", report)
            self.assertIn("结构一致：是", report)
            self.assertIn("| 2026-07-08 | 失败 | FIELD_MISSING | 8/9 |", report)
            self.assertIn("失败日期：2026-07-08", report)

    def test_run_summary_marks_structure_drift(self):
        with TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            _write_run_record(runs_dir, "2026-07-08", "通过", "暂无", "9/9", "aaa111")
            _write_run_record(runs_dir, "2026-07-09", "通过", "暂无", "9/9", "bbb222")

            report = build_run_summary_report(runs_dir, target_days=2)

            self.assertIn("结构一致：否", report)
            self.assertIn("最近连续通过：2 天", report)
            self.assertIn("最近连续稳定通过：1 天", report)
            self.assertIn("还差：1 天", report)
            self.assertIn("状态：未达标", report)
            self.assertIn("结构变动日期：2026-07-09", report)

    def test_run_summary_marks_missing_expected_trading_day(self):
        with TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            _write_run_record(runs_dir, "2026-07-07", "通过", "暂无", "9/9")
            _write_run_record(runs_dir, "2026-07-09", "通过", "暂无", "9/9")

            report = build_run_summary_report(
                runs_dir,
                target_days=3,
                expected_dates=[
                    dt.date(2026, 7, 7),
                    dt.date(2026, 7, 8),
                    dt.date(2026, 7, 9),
                ],
            )

            self.assertIn("期望交易日：3 天", report)
            self.assertIn("交易日覆盖：否", report)
            self.assertIn("漏跑交易日：2026-07-08", report)
            self.assertIn("最近连续通过：2 天", report)
            self.assertIn("最近连续稳定通过：1 天", report)
            self.assertIn("还差：2 天", report)
            self.assertIn("状态：未达标", report)

    def test_run_summary_marks_target_as_reached(self):
        with TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            _write_run_record(runs_dir, "2026-07-08", "通过", "暂无", "9/9")
            _write_run_record(runs_dir, "2026-07-09", "通过", "暂无", "9/9")

            report = build_run_summary_report(runs_dir, target_days=2)

            self.assertIn("最近连续通过：2 天", report)
            self.assertIn("还差：0 天", report)
            self.assertIn("状态：已达标", report)

    def test_run_summary_fails_when_no_run_records_exist(self):
        with TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError) as exc:
                build_run_summary_report(Path(tmp))

            self.assertIn("RUN_SUMMARY_NO_RUN_AUDITS", str(exc.exception))

    def test_write_run_summary_report_creates_summary_file(self):
        with TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            _write_run_record(runs_dir, "2026-07-09", "通过", "暂无", "9/9")

            path = write_run_summary_report(runs_dir)

            self.assertEqual(path.name, "run_summary.md")
            self.assertTrue(path.exists())
            self.assertIn("已记录：1 天", path.read_text(encoding="utf-8"))

    def test_stage_gate_blocks_push_when_trial_run_not_reached(self):
        with TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            (runs_dir / "run_summary.md").write_text(
                _run_summary(status="未达标", stable_days="1 天", remaining="9 天"),
                encoding="utf-8",
            )

            report = build_stage_gate_report(runs_dir)

            self.assertIn("# 本地试运行阶段门禁", report)
            self.assertIn("结果：未通过", report)
            self.assertIn("是否允许进入推送阶段：否", report)
            self.assertIn("| 连续稳定通过 | 状态必须已达标 | 未达标，1 天，还差 9 天 | 未通过 |", report)
            self.assertIn("继续本地试运行，不进入推送模块", report)

    def test_stage_gate_allows_push_only_when_all_conditions_pass(self):
        with TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            (runs_dir / "run_summary.md").write_text(
                _run_summary(status="已达标", stable_days="10 天", remaining="0 天"),
                encoding="utf-8",
            )

            report = build_stage_gate_report(runs_dir)

            self.assertIn("结果：通过", report)
            self.assertIn("是否允许进入推送阶段：是", report)
            self.assertIn("| 连续稳定通过 | 状态必须已达标 | 已达标，10 天，还差 0 天 | 通过 |", report)
            self.assertIn("| 交易日覆盖 | 必须为是 | 是 | 通过 |", report)

    def test_stage_gate_blocks_push_when_expected_dates_are_not_enabled(self):
        with TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            (runs_dir / "run_summary.md").write_text(
                _run_summary(
                    status="已达标",
                    stable_days="10 天",
                    remaining="0 天",
                    expected_dates="未启用",
                ),
                encoding="utf-8",
            )

            report = build_stage_gate_report(runs_dir)

            self.assertIn("结果：未通过", report)
            self.assertIn("是否允许进入推送阶段：否", report)
            self.assertIn("| 期望交易日清单 | 必须启用 | 未启用 | 未通过 |", report)

    def test_write_stage_gate_report_creates_gate_file(self):
        with TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            (runs_dir / "run_summary.md").write_text(
                _run_summary(status="未达标", stable_days="1 天", remaining="9 天"),
                encoding="utf-8",
            )

            path = write_stage_gate_report(runs_dir)

            self.assertEqual(path.name, "stage_gate_09.md")
            self.assertTrue(path.exists())
            self.assertIn("结果：未通过", path.read_text(encoding="utf-8"))

    def test_add_expected_dates_appends_sorted_unique_dates(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "expected_dates.txt"
            path.write_text("2026-07-09\n", encoding="utf-8")

            add_expected_dates(path, [dt.date(2026, 7, 11), dt.date(2026, 7, 10)])

            self.assertEqual(
                path.read_text(encoding="utf-8"),
                "2026-07-09\n2026-07-10\n2026-07-11\n",
            )
            self.assertEqual(
                load_expected_dates(path),
                [dt.date(2026, 7, 9), dt.date(2026, 7, 10), dt.date(2026, 7, 11)],
            )

    def test_add_expected_dates_rejects_existing_date(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "expected_dates.txt"
            path.write_text("2026-07-09\n", encoding="utf-8")

            with self.assertRaises(ValueError) as exc:
                add_expected_dates(path, [dt.date(2026, 7, 9)])

            self.assertIn("EXPECTED_DATE_ALREADY_EXISTS:2026-07-09", str(exc.exception))
            self.assertEqual(path.read_text(encoding="utf-8"), "2026-07-09\n")

    def test_select_next_expected_date_returns_first_unrecorded_day(self):
        with TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            _write_run_record(runs_dir, "2026-07-09", "通过", "暂无", "9/9")

            next_date = select_next_expected_date(
                runs_dir,
                [dt.date(2026, 7, 9), dt.date(2026, 7, 10), dt.date(2026, 7, 11)],
            )

            self.assertEqual(next_date, dt.date(2026, 7, 10))

    def test_select_next_expected_date_fails_when_all_days_recorded(self):
        with TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            _write_run_record(runs_dir, "2026-07-09", "通过", "暂无", "9/9")

            with self.assertRaises(ValueError) as exc:
                select_next_expected_date(runs_dir, [dt.date(2026, 7, 9)])

            self.assertIn("TRIAL_RUN_NO_PENDING_EXPECTED_DATE", str(exc.exception))

    def test_trial_status_report_shows_next_pending_date_and_command(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            runs_dir = base / "runs"
            expected_dates = base / "expected_dates.txt"
            _write_run_record(runs_dir, "2026-07-09", "通过", "暂无", "9/9")
            expected_dates.write_text("2026-07-09\n2026-07-10\n", encoding="utf-8")
            _write_run_summary(runs_dir, status="未达标", stable_days="1 天", remaining="9 天")
            write_stage_gate_report(runs_dir)

            report = build_trial_status_report(
                runs_dir,
                expected_dates,
                reports_dir=Path("reports"),
                target_days=10,
                focus_positions=False,
                today=dt.date(2026, 7, 10),
            )

            self.assertIn("# 本地试运行状态", report)
            self.assertIn("下一个待运行日期：2026-07-10", report)
            self.assertIn("门禁：否", report)
            self.assertIn("阶段：未达标，1 天，还差 9 天", report)
            self.assertIn("trial-run-next --reports-dir reports --runs-dir", report)
            self.assertIn("--ignore-positions", report)

    def test_trial_status_report_waits_when_next_pending_date_is_future(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            runs_dir = base / "runs"
            expected_dates = base / "expected_dates.txt"
            _write_run_record(runs_dir, "2026-07-09", "通过", "暂无", "9/9")
            expected_dates.write_text("2026-07-09\n2026-07-10\n", encoding="utf-8")
            _write_run_summary(runs_dir, status="未达标", stable_days="1 天", remaining="9 天")
            write_stage_gate_report(runs_dir)

            report = build_trial_status_report(
                runs_dir,
                expected_dates,
                reports_dir=Path("reports"),
                target_days=10,
                focus_positions=False,
                today=dt.date(2026, 7, 9),
            )

            self.assertIn("下一个待运行日期：2026-07-10", report)
            self.assertIn("下一步：等待 2026-07-10，当天再执行 trial-run-next", report)
            self.assertIn("推荐命令：暂不执行", report)
            self.assertNotIn("trial-run-next --reports-dir", report)

    def test_trial_status_report_shows_no_pending_date_action(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            runs_dir = base / "runs"
            expected_dates = base / "expected_dates.txt"
            _write_run_record(runs_dir, "2026-07-09", "通过", "暂无", "9/9")
            expected_dates.write_text("2026-07-09\n", encoding="utf-8")
            _write_run_summary(runs_dir, status="未达标", stable_days="1 天", remaining="9 天")
            write_stage_gate_report(runs_dir)

            report = build_trial_status_report(
                runs_dir,
                expected_dates,
                reports_dir=Path("reports"),
                target_days=10,
                focus_positions=False,
            )

            self.assertIn("下一个待运行日期：暂无", report)
            self.assertIn("下一步：先用 expected-dates-add 添加已确认交易日", report)

    def test_trial_status_report_shows_push_step_when_gate_is_passed(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            runs_dir = base / "runs"
            expected_dates = base / "expected_dates.txt"
            _write_run_record(runs_dir, "2026-07-09", "通过", "暂无", "9/9")
            expected_dates.write_text("2026-07-09\n", encoding="utf-8")
            _write_run_summary(runs_dir, status="已达标", stable_days="10 天", remaining="0 天")
            write_stage_gate_report(runs_dir)

            report = build_trial_status_report(
                runs_dir,
                expected_dates,
                reports_dir=Path("reports"),
                target_days=10,
                focus_positions=False,
            )

            self.assertIn("门禁：是", report)
            self.assertIn("下一个待运行日期：暂无", report)
            self.assertIn("下一步：进入推送模块开发", report)
            self.assertIn("推荐命令：暂不执行", report)
            self.assertNotIn("expected-dates-add", report)

    def test_write_trial_status_report_creates_status_file(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            runs_dir = base / "runs"
            expected_dates = base / "expected_dates.txt"
            _write_run_record(runs_dir, "2026-07-09", "通过", "暂无", "9/9")
            expected_dates.write_text("2026-07-09\n", encoding="utf-8")
            _write_run_summary(runs_dir, status="未达标", stable_days="1 天", remaining="9 天")
            write_stage_gate_report(runs_dir)

            path = write_trial_status_report(runs_dir, expected_dates)

            self.assertEqual(path.name, "trial_status.md")
            self.assertTrue(path.exists())
            self.assertIn("下一个待运行日期：暂无", path.read_text(encoding="utf-8"))


def _push_contract(strict_ready: bool, day: str = "2026-07-09") -> str:
    strict_text = "是" if strict_ready else "否"
    return f"""# 推送内容验收清单（{day}）

## 完整性状态
- 严格完整：{strict_text}
- 缺口：暂无

## 推送内容验收清单
| 推送内容 | 必含内容 | 证据文件 | 状态 | 缺口/处理 |
|---|---|---|---|---|
| 盘中实时盘面 | 热门板块 | intraday_{day}.md | 可发送 | 暂无 |
| 持仓个股操作 | 暂不关注 | positions_{day}.md | 本阶段跳过 | 用户已选择暂不关注持仓个股 |
| 建仓候选推荐 | 候选股 | watchlist_{day}.md | 可发送 | 当前无主升候选，按规则输出暂无 |
| 利好利空捕捉 | 来源 | news_{day}.md | 可发送 | 当前无匹配新闻，不作为交易依据 |
| 晚间复盘 | 复盘 | evening_{day}.md | 可发送 | 暂无 |
| 次日操作计划 | 计划 | evening_{day}.md | 可发送 | 暂无 |
| 风险提示 | 风险 | evening_{day}.md | 可发送 | 暂无 |
"""


def _write_run_record(
    runs_dir: Path,
    day: str,
    result: str,
    gap: str,
    report_files: str,
    fingerprint: str = "same123",
) -> None:
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / f"run_audit_{day}.md").write_text(
        f"""# 本地试运行记录（{day}）

## 总判断
- 结果：{result}
- 严格完整：{'是' if result == '通过' else '否'}
- 缺口：{gap}
- 报告文件：{report_files}
- 整体结构指纹：{fingerprint}
- 命令：`daily-pack --split --ignore-positions`
""",
        encoding="utf-8",
    )


def _extract_overall_fingerprint(report: str) -> str:
    for line in report.splitlines():
        if line.startswith("- 整体结构指纹："):
            return line.removeprefix("- 整体结构指纹：")
    raise AssertionError("missing fingerprint")


def _run_summary(
    status: str,
    stable_days: str,
    remaining: str,
    expected_dates: str = "1 天",
    coverage: str = "是",
    structure: str = "是",
    failed_dates: str = "暂无",
    missing_dates: str = "暂无",
    drift_dates: str = "暂无",
) -> str:
    return f"""# 本地试运行汇总

## 总判断
- 目标：10 个交易日
- 已记录：1 天
- 期望交易日：{expected_dates}
- 交易日覆盖：{coverage}
- 最近连续通过：1 天
- 最近连续稳定通过：{stable_days}
- 还差：{remaining}
- 最新日期：2026-07-09
- 最新结果：通过
- 状态：{status}
- 结构一致：{structure}

## 失败追踪
- 失败日期：{failed_dates}
- 漏跑交易日：{missing_dates}
- 结构变动日期：{drift_dates}
"""


def _write_run_summary(
    runs_dir: Path,
    status: str,
    stable_days: str,
    remaining: str,
) -> None:
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / "run_summary.md").write_text(
        _run_summary(status=status, stable_days=stable_days, remaining=remaining),
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
