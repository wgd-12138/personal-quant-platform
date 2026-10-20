import unittest
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from personal_quant.data_center.models import BoardSnapshot, ExternalSnapshot, IndexSnapshot, MarketDataset, StockSnapshot
from personal_quant.cli import (
    add_expected_trading_dates,
    generate_daily_pack,
    generate_run_audit,
    generate_run_summary,
    generate_trial_gate,
    generate_trial_run,
    generate_trial_run_next,
    generate_trial_status,
)
from personal_quant.report_builder.manifest import EXPECTED_REPORTS, build_manifest_report


class ProjectBootstrapTests(unittest.TestCase):
    def test_bootstrap_placeholder(self):
        self.assertTrue(True)

    def test_require_positions_fails_before_fetching_market_data(self):
        with TemporaryDirectory() as tmp:
            with patch("personal_quant.cli.fetch_market_dataset") as fetch_market_dataset:
                fetch_market_dataset.side_effect = AssertionError("不应该先拉行情")

                with self.assertRaises(ValueError) as exc:
                    generate_daily_pack(
                        date(2026, 7, 9),
                        Path(tmp),
                        positions_path=None,
                        require_positions=True,
                    )

                self.assertIn("STRICT_REQUIRE_POSITIONS", str(exc.exception))
                fetch_market_dataset.assert_not_called()

    def test_split_daily_pack_writes_readiness_report(self):
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            with patch("personal_quant.cli.fetch_market_dataset", return_value=_dataset()):
                generate_daily_pack(
                    date(2026, 7, 9),
                    output_dir,
                    positions_path=None,
                    split=True,
                    require_positions=False,
                )

            archive_dir = output_dir / "2026-07-09"
            readiness = archive_dir / "readiness_2026-07-09.md"
            manifest = archive_dir / "manifest_2026-07-09.md"

            self.assertTrue(readiness.exists())
            self.assertIn("POSITIONS_FILE_MISSING", readiness.read_text(encoding="utf-8"))
            self.assertIn("readiness_2026-07-09.md", manifest.read_text(encoding="utf-8"))

    def test_ignore_positions_removes_positions_gap(self):
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            with patch("personal_quant.cli.fetch_market_dataset", return_value=_dataset()):
                generate_daily_pack(
                    date(2026, 7, 9),
                    output_dir,
                    positions_path=None,
                    split=True,
                    require_positions=False,
                    focus_positions=False,
                )

            archive_dir = output_dir / "2026-07-09"
            manifest = (archive_dir / "manifest_2026-07-09.md").read_text(encoding="utf-8")
            contract = (archive_dir / "push_contract_2026-07-09.md").read_text(encoding="utf-8")
            readiness = (archive_dir / "readiness_2026-07-09.md").read_text(encoding="utf-8")

            self.assertIn("严格完整：是", manifest)
            self.assertIn("严格完整：是", contract)
            self.assertIn("本阶段跳过", contract)
            self.assertIn("暂不关注持仓个股", readiness)
            self.assertNotIn("POSITIONS_FILE_MISSING", manifest)
            self.assertNotIn("POSITIONS_FILE_MISSING", contract)

    def test_generate_run_audit_writes_planning_run_record(self):
        with TemporaryDirectory() as tmp:
            reports_dir = Path(tmp) / "reports"
            runs_dir = Path(tmp) / "planning" / "runs"
            archive_dir = reports_dir / "2026-07-09"
            archive_dir.mkdir(parents=True)
            for expected in EXPECTED_REPORTS:
                (archive_dir / f"{expected.prefix}_2026-07-09.md").write_text("# ok\n", encoding="utf-8")
            (archive_dir / "manifest_2026-07-09.md").write_text(
                build_manifest_report(date(2026, 7, 9), archive_dir, True, []),
                encoding="utf-8",
            )
            (archive_dir / "push_contract_2026-07-09.md").write_text(
                """# 推送内容验收清单（2026-07-09）

## 完整性状态
- 严格完整：是
- 缺口：暂无

## 推送内容验收清单
| 推送内容 | 必含内容 | 证据文件 | 状态 | 缺口/处理 |
|---|---|---|---|---|
| 盘中实时盘面 | 热门板块 | intraday_2026-07-09.md | 可发送 | 暂无 |
| 持仓个股操作 | 暂不关注 | positions_2026-07-09.md | 本阶段跳过 | 用户已选择暂不关注持仓个股 |
| 建仓候选推荐 | 候选股 | watchlist_2026-07-09.md | 可发送 | 暂无 |
| 利好利空捕捉 | 来源 | news_2026-07-09.md | 可发送 | 暂无 |
| 晚间复盘 | 复盘 | evening_2026-07-09.md | 可发送 | 暂无 |
| 次日操作计划 | 计划 | evening_2026-07-09.md | 可发送 | 暂无 |
| 风险提示 | 风险 | evening_2026-07-09.md | 可发送 | 暂无 |
""",
                encoding="utf-8",
            )

            output_path = generate_run_audit(
                date(2026, 7, 9),
                reports_dir,
                runs_dir,
                command="daily-pack --split --ignore-positions",
            )

            self.assertEqual(output_path.name, "run_audit_2026-07-09.md")
            self.assertIn("结果：通过", output_path.read_text(encoding="utf-8"))

    def test_generate_run_summary_writes_trial_summary(self):
        with TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            (runs_dir / "run_audit_2026-07-09.md").write_text(
                """# 本地试运行记录（2026-07-09）

## 总判断
- 结果：通过
- 严格完整：是
- 缺口：暂无
- 报告文件：9/9
- 命令：`daily-pack --split --ignore-positions`
""",
                encoding="utf-8",
            )

            output_path = generate_run_summary(runs_dir, target_days=10)

            self.assertEqual(output_path.name, "run_summary.md")
            self.assertIn("已记录：1 天", output_path.read_text(encoding="utf-8"))

    def test_generate_run_summary_uses_expected_dates_file(self):
        with TemporaryDirectory() as tmp:
            runs_dir = Path(tmp) / "runs"
            runs_dir.mkdir()
            expected_dates = Path(tmp) / "expected_dates.txt"
            expected_dates.write_text("2026-07-07\n2026-07-08\n2026-07-09\n", encoding="utf-8")
            (runs_dir / "run_audit_2026-07-07.md").write_text(
                _run_record("2026-07-07"),
                encoding="utf-8",
            )
            (runs_dir / "run_audit_2026-07-09.md").write_text(
                _run_record("2026-07-09"),
                encoding="utf-8",
            )

            output_path = generate_run_summary(runs_dir, target_days=3, expected_dates_path=expected_dates)

            summary = output_path.read_text(encoding="utf-8")
            self.assertIn("交易日覆盖：否", summary)
            self.assertIn("漏跑交易日：2026-07-08", summary)

    def test_generate_trial_run_writes_daily_audit_and_summary(self):
        with TemporaryDirectory() as tmp:
            reports_dir = Path(tmp) / "reports"
            runs_dir = Path(tmp) / "planning" / "runs"

            with patch("personal_quant.cli.fetch_market_dataset", return_value=_dataset()):
                output_path = generate_trial_run(
                    date(2026, 7, 9),
                    reports_dir,
                    runs_dir,
                    focus_positions=False,
                    target_days=10,
                )

            audit = runs_dir / "run_audit_2026-07-09.md"
            summary = runs_dir / "run_summary.md"
            gate = runs_dir / "stage_gate_09.md"

            self.assertEqual(output_path, summary)
            self.assertTrue((reports_dir / "2026-07-09" / "daily_pack_2026-07-09.md").exists())
            self.assertTrue(audit.exists())
            self.assertTrue(summary.exists())
            self.assertTrue(gate.exists())
            self.assertIn("结果：通过", audit.read_text(encoding="utf-8"))
            self.assertIn("--ignore-positions", audit.read_text(encoding="utf-8"))
            self.assertIn("已记录：1 天", summary.read_text(encoding="utf-8"))
            self.assertIn("还差：9 天", summary.read_text(encoding="utf-8"))
            self.assertIn("是否允许进入推送阶段：否", gate.read_text(encoding="utf-8"))

    def test_generate_trial_run_records_failure_before_reraising(self):
        with TemporaryDirectory() as tmp:
            reports_dir = Path(tmp) / "reports"
            runs_dir = Path(tmp) / "planning" / "runs"

            with patch("personal_quant.cli.fetch_market_dataset", side_effect=RuntimeError("DATA_SOURCE_DOWN")):
                with self.assertRaises(RuntimeError):
                    generate_trial_run(
                        date(2026, 7, 9),
                        reports_dir,
                        runs_dir,
                        focus_positions=False,
                        target_days=10,
                    )

            audit = (runs_dir / "run_audit_2026-07-09.md").read_text(encoding="utf-8")
            summary = (runs_dir / "run_summary.md").read_text(encoding="utf-8")
            gate = (runs_dir / "stage_gate_09.md").read_text(encoding="utf-8")

            self.assertIn("结果：失败", audit)
            self.assertIn("TRIAL_RUN_FAILED:RuntimeError:DATA_SOURCE_DOWN", audit)
            self.assertIn("这一天不能纳入连续试运行统计", audit)
            self.assertIn("| 2026-07-09 | 失败 | TRIAL_RUN_FAILED:RuntimeError:DATA_SOURCE_DOWN | 0/9 |", summary)
            self.assertIn("失败日期：2026-07-09", summary)
            self.assertIn("是否允许进入推送阶段：否", gate)

    def test_generate_trial_run_requires_as_of_in_expected_dates(self):
        with TemporaryDirectory() as tmp:
            reports_dir = Path(tmp) / "reports"
            runs_dir = Path(tmp) / "planning" / "runs"
            expected_dates = Path(tmp) / "expected_dates.txt"
            expected_dates.write_text("2026-07-08\n", encoding="utf-8")

            with patch("personal_quant.cli.fetch_market_dataset") as fetch_market_dataset:
                fetch_market_dataset.side_effect = AssertionError("不应该拉行情")

                with self.assertRaises(ValueError) as exc:
                    generate_trial_run(
                        date(2026, 7, 9),
                        reports_dir,
                        runs_dir,
                        focus_positions=False,
                        target_days=10,
                        expected_dates_path=expected_dates,
                    )

                fetch_market_dataset.assert_not_called()

            audit = (runs_dir / "run_audit_2026-07-09.md").read_text(encoding="utf-8")
            summary = (runs_dir / "run_summary.md").read_text(encoding="utf-8")

            self.assertIn("TRIAL_RUN_DATE_NOT_EXPECTED:2026-07-09", str(exc.exception))
            self.assertIn("TRIAL_RUN_FAILED:ValueError:TRIAL_RUN_DATE_NOT_EXPECTED:2026-07-09", audit)
            self.assertIn("结果：失败", audit)
            self.assertIn("漏跑交易日：2026-07-08", summary)

    def test_generate_trial_run_refuses_existing_record_before_fetching(self):
        with TemporaryDirectory() as tmp:
            reports_dir = Path(tmp) / "reports"
            runs_dir = Path(tmp) / "planning" / "runs"
            expected_dates = Path(tmp) / "expected_dates.txt"
            runs_dir.mkdir(parents=True)
            expected_dates.write_text("2026-07-09\n", encoding="utf-8")
            existing_record = runs_dir / "run_audit_2026-07-09.md"
            existing_record.write_text(_run_record("2026-07-09"), encoding="utf-8")

            with patch("personal_quant.cli.fetch_market_dataset") as fetch_market_dataset:
                fetch_market_dataset.side_effect = AssertionError("不应该拉行情")

                with self.assertRaises(ValueError) as exc:
                    generate_trial_run(
                        date(2026, 7, 9),
                        reports_dir,
                        runs_dir,
                        focus_positions=False,
                        target_days=10,
                        expected_dates_path=expected_dates,
                    )

                fetch_market_dataset.assert_not_called()

            self.assertIn("TRIAL_RUN_RECORD_EXISTS:2026-07-09", str(exc.exception))
            self.assertEqual(existing_record.read_text(encoding="utf-8"), _run_record("2026-07-09"))
            self.assertFalse((runs_dir / "run_summary.md").exists())
            self.assertFalse((runs_dir / "stage_gate_09.md").exists())

    def test_generate_trial_run_allows_existing_record_when_explicitly_enabled(self):
        with TemporaryDirectory() as tmp:
            reports_dir = Path(tmp) / "reports"
            runs_dir = Path(tmp) / "planning" / "runs"
            expected_dates = Path(tmp) / "expected_dates.txt"
            runs_dir.mkdir(parents=True)
            expected_dates.write_text("2026-07-09\n", encoding="utf-8")
            (runs_dir / "run_audit_2026-07-09.md").write_text(_run_record("2026-07-09"), encoding="utf-8")

            with patch("personal_quant.cli.fetch_market_dataset", return_value=_dataset()):
                output_path = generate_trial_run(
                    date(2026, 7, 9),
                    reports_dir,
                    runs_dir,
                    focus_positions=False,
                    target_days=10,
                    expected_dates_path=expected_dates,
                    allow_rerun=True,
                )

            self.assertEqual(output_path, runs_dir / "run_summary.md")
            self.assertIn("结果：通过", (runs_dir / "run_audit_2026-07-09.md").read_text(encoding="utf-8"))

    def test_generate_trial_run_rejects_future_date_before_fetching(self):
        with TemporaryDirectory() as tmp:
            reports_dir = Path(tmp) / "reports"
            runs_dir = Path(tmp) / "planning" / "runs"
            expected_dates = Path(tmp) / "expected_dates.txt"
            expected_dates.write_text("2026-07-10\n", encoding="utf-8")

            with patch("personal_quant.cli.fetch_market_dataset") as fetch_market_dataset:
                fetch_market_dataset.side_effect = AssertionError("不应该拉行情")

                with self.assertRaises(ValueError) as exc:
                    generate_trial_run(
                        date(2026, 7, 10),
                        reports_dir,
                        runs_dir,
                        focus_positions=False,
                        target_days=10,
                        expected_dates_path=expected_dates,
                        today=date(2026, 7, 9),
                    )

                fetch_market_dataset.assert_not_called()

            self.assertIn("TRIAL_RUN_FUTURE_DATE:2026-07-10:TODAY:2026-07-09", str(exc.exception))
            self.assertFalse((runs_dir / "run_audit_2026-07-10.md").exists())
            self.assertFalse((runs_dir / "run_summary.md").exists())
            self.assertFalse((runs_dir / "stage_gate_09.md").exists())

    def test_generate_trial_run_next_uses_first_unrecorded_expected_date(self):
        with TemporaryDirectory() as tmp:
            reports_dir = Path(tmp) / "reports"
            runs_dir = Path(tmp) / "planning" / "runs"
            expected_dates = Path(tmp) / "expected_dates.txt"
            runs_dir.mkdir(parents=True)
            expected_dates.write_text("2026-07-09\n2026-07-10\n", encoding="utf-8")
            (runs_dir / "run_audit_2026-07-09.md").write_text(
                _run_record("2026-07-09"),
                encoding="utf-8",
            )

            with patch("personal_quant.cli.fetch_market_dataset", return_value=_dataset(as_of=date(2026, 7, 10))):
                output_path = generate_trial_run_next(
                    reports_dir,
                    runs_dir,
                    expected_dates,
                    focus_positions=False,
                    target_days=10,
                    today=date(2026, 7, 10),
                )

            self.assertEqual(output_path, runs_dir / "run_summary.md")
            self.assertTrue((runs_dir / "run_audit_2026-07-10.md").exists())
            summary = output_path.read_text(encoding="utf-8")
            self.assertIn("已记录：2 天", summary)
            self.assertIn("最新日期：2026-07-10", summary)

    def test_generate_trial_run_next_fails_before_fetch_when_no_pending_date(self):
        with TemporaryDirectory() as tmp:
            reports_dir = Path(tmp) / "reports"
            runs_dir = Path(tmp) / "planning" / "runs"
            expected_dates = Path(tmp) / "expected_dates.txt"
            runs_dir.mkdir(parents=True)
            expected_dates.write_text("2026-07-09\n", encoding="utf-8")
            (runs_dir / "run_audit_2026-07-09.md").write_text(
                _run_record("2026-07-09"),
                encoding="utf-8",
            )

            with patch("personal_quant.cli.fetch_market_dataset") as fetch_market_dataset:
                fetch_market_dataset.side_effect = AssertionError("不应该拉行情")
                with self.assertRaises(ValueError) as exc:
                    generate_trial_run_next(
                        reports_dir,
                        runs_dir,
                        expected_dates,
                        focus_positions=False,
                        target_days=10,
                    )

                fetch_market_dataset.assert_not_called()

            self.assertIn("TRIAL_RUN_NO_PENDING_EXPECTED_DATE", str(exc.exception))

    def test_generate_trial_gate_writes_stage_gate_file(self):
        with TemporaryDirectory() as tmp:
            runs_dir = Path(tmp) / "planning" / "runs"
            runs_dir.mkdir(parents=True)
            (runs_dir / "run_summary.md").write_text(
                """# 本地试运行汇总

## 总判断
- 目标：10 个交易日
- 已记录：1 天
- 期望交易日：1 天
- 交易日覆盖：是
- 最近连续通过：1 天
- 最近连续稳定通过：1 天
- 还差：9 天
- 最新日期：2026-07-09
- 最新结果：通过
- 状态：未达标
- 结构一致：是

## 失败追踪
- 失败日期：暂无
- 漏跑交易日：暂无
- 结构变动日期：暂无
""",
                encoding="utf-8",
            )

            output_path = generate_trial_gate(runs_dir)

            self.assertEqual(output_path.name, "stage_gate_09.md")
            self.assertIn("是否允许进入推送阶段：否", output_path.read_text(encoding="utf-8"))

    def test_add_expected_trading_dates_writes_sorted_file(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "expected_dates.txt"
            path.write_text("2026-07-09\n", encoding="utf-8")

            output_path = add_expected_trading_dates(path, ["2026-07-11", "2026-07-10"])

            self.assertEqual(output_path, path)
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                "2026-07-09\n2026-07-10\n2026-07-11\n",
            )

    def test_add_expected_trading_dates_rejects_invalid_date(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "expected_dates.txt"

            with self.assertRaises(ValueError) as exc:
                add_expected_trading_dates(path, ["2026-99-99"])

            self.assertIn("EXPECTED_DATE_INVALID:2026-99-99", str(exc.exception))
            self.assertFalse(path.exists())

    def test_generate_trial_status_writes_status_file(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            runs_dir = base / "planning" / "runs"
            expected_dates = runs_dir / "expected_dates.txt"
            runs_dir.mkdir(parents=True)
            expected_dates.write_text("2026-07-09\n", encoding="utf-8")
            (runs_dir / "run_audit_2026-07-09.md").write_text(
                _run_record("2026-07-09"),
                encoding="utf-8",
            )
            (runs_dir / "run_summary.md").write_text(
                """# 本地试运行汇总

## 总判断
- 目标：10 个交易日
- 已记录：1 天
- 期望交易日：1 天
- 交易日覆盖：是
- 最近连续通过：1 天
- 最近连续稳定通过：1 天
- 还差：9 天
- 最新日期：2026-07-09
- 最新结果：通过
- 状态：未达标
- 结构一致：是

## 失败追踪
- 失败日期：暂无
- 漏跑交易日：暂无
- 结构变动日期：暂无
""",
                encoding="utf-8",
            )
            generate_trial_gate(runs_dir)

            output_path = generate_trial_status(
                runs_dir,
                expected_dates,
                reports_dir=Path("reports"),
                target_days=10,
                focus_positions=False,
            )

            self.assertEqual(output_path.name, "trial_status.md")
            self.assertIn("下一个待运行日期：暂无", output_path.read_text(encoding="utf-8"))


def _dataset(as_of: date = date(2026, 7, 9)) -> MarketDataset:
    return MarketDataset(
        as_of=as_of,
        boards=[
            BoardSnapshot(
                code="BK0001",
                name="低位反弹",
                board_type="行业",
                change_pct=3.0,
                amount=1000000000,
                main_net_inflow=10000000,
                five_day_return=3.5,
                sixty_day_return=-10.0,
                up_count=20,
                down_count=3,
            )
        ],
        externals=[
            ExternalSnapshot(
                name="纳斯达克",
                symbol="^IXIC",
                may_june_return=4.0,
                july_return=-2.0,
            ),
            ExternalSnapshot(
                name="博通",
                symbol="AVGO",
                may_june_return=8.0,
                july_return=3.0,
            ),
        ],
        indexes=[
            IndexSnapshot("000001", "上证指数", 3952.49, -0.46, -18.39, 790773118087.2),
        ],
        stocks=[
            StockSnapshot("600000", "浦发银行", 10.0, 1.0, 100000000, 1.0, "银行", 10.0, 9.8, 9.9),
        ],
        data_sources=("东方财富板块", "东方财富个股", "Yahoo Finance", "东方财富快讯"),
        fetched_at=datetime(2026, 7, 9, 11, 30),
    )


def _run_record(day: str) -> str:
    return f"""# 本地试运行记录（{day}）

## 总判断
- 结果：通过
- 严格完整：是
- 缺口：暂无
- 报告文件：9/9
- 整体结构指纹：same123
- 命令：`daily-pack --split --ignore-positions`
"""


if __name__ == "__main__":
    unittest.main()
