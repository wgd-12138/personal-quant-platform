import datetime as dt
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from personal_quant.cli import (
    generate_push_activation,
    generate_push_go_live,
    generate_push_daily,
    generate_push_run,
    generate_push_schedule,
    generate_push_send,
    generate_push_status,
    generate_push_test,
    generate_windows_task_script,
)
from personal_quant.data_center.models import BoardSnapshot, ExternalSnapshot, IndexSnapshot, MarketDataset, StockSnapshot
from personal_quant.notifier.activation import build_push_activation_report
from personal_quant.notifier.channel_test import send_push_channel_test, webhook_url_fingerprint
from personal_quant.notifier.feishu import FeishuWebhookResult, build_feishu_card_payload, build_feishu_text_payload
from personal_quant.notifier.go_live import build_push_go_live_report
from personal_quant.notifier.push import load_slot_messages, send_push_slot
from personal_quant.notifier.schedule import PUSH_SLOTS, build_push_schedule_report, build_windows_task_script
from personal_quant.notifier.status import build_push_status_report
from personal_quant.notifier.webhook_env import resolve_feishu_webhook
from personal_quant.notifier.windows_tasks import (
    MARKET_MONITOR_SLOT_NAME,
    MARKET_MONITOR_TASK_NAME,
    MARKET_MONITOR_TIME,
    POSITION_MONITOR_SLOT_NAME,
    POSITION_MONITOR_TASK_NAME,
    POSITION_MONITOR_TIME,
    WindowsTaskStatus,
    build_windows_task_status_report,
)


class NotifierTests(unittest.TestCase):
    def test_schedule_fixes_daily_generation_times(self):
        report = build_push_schedule_report(focus_positions=False)

        self.assertIn("| 08:50 | 开盘前计划 | 不重新拉行情，读取上一交易日完整报告 | next_day、risk |", report)
        self.assertIn("| 10:35 | 早盘盘面 | 生成当天 daily-pack --split | intraday、news |", report)
        self.assertIn("| 11:45 | 午间休息 | 生成当天 daily-pack --split | intraday、watchlist、news、risk |", report)
        self.assertIn("| 14:35 | 尾盘盘面 | 重新生成当天 daily-pack --split | intraday、news、risk |", report)
        self.assertIn("| 16:10 | 收盘复盘 | 生成当天 daily-pack --split | intraday、positions、news、evening_review、next_day、risk |", report)
        self.assertIn("push-daily", report)
        self.assertIn("--ignore-positions", report)

    def test_generate_push_schedule_writes_file(self):
        with TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "push_schedule.md"

            path = generate_push_schedule(output_path, focus_positions=False)

            self.assertEqual(path, output_path)
            self.assertIn("每日生成和推送时间表", path.read_text(encoding="utf-8"))

    def test_schedule_can_show_positions_file(self):
        report = build_push_schedule_report(positions_path=Path("data/positions.csv"))

        self.assertIn("--positions data/positions.csv", report)
        self.assertNotIn("--ignore-positions", report)

    def test_load_slot_messages_reads_selected_preview_sections(self):
        with TemporaryDirectory() as tmp:
            reports_dir = Path(tmp) / "reports"
            _write_preview(reports_dir, "2026-07-09")

            messages = load_slot_messages(dt.date(2026, 7, 9), reports_dir, "evening")

            self.assertEqual(
                [message.key for message in messages],
                ["intraday", "positions", "news", "evening_review", "next_day", "risk"],
            )
            self.assertIn("盘中内容", messages[0].text)
            self.assertIn("持仓内容", messages[1].text)
            self.assertIn("仓位调整建议", messages[1].text)
            self.assertIn("新闻内容", messages[2].text)
            self.assertIn("复盘内容", messages[3].text)
            self.assertIn("次日计划", messages[4].text)
            self.assertIn("风险内容", messages[5].text)

    def test_dry_run_writes_receipt_without_webhook(self):
        with TemporaryDirectory() as tmp:
            reports_dir = Path(tmp) / "reports"
            output_dir = Path(tmp) / "runs"
            _write_preview(reports_dir, "2026-07-09")

            path = send_push_slot(dt.date(2026, 7, 9), reports_dir, "pre_open", output_dir)

            text = path.read_text(encoding="utf-8")
            self.assertIn("模式：dry-run", text)
            self.assertIn("消息数：2", text)
            self.assertIn("未发送，仅检查内容", text)

    def test_live_requires_webhook_url(self):
        with TemporaryDirectory() as tmp:
            reports_dir = Path(tmp) / "reports"
            output_dir = Path(tmp) / "runs"
            _write_preview(reports_dir, "2026-07-09")

            with patch.dict(os.environ, {"FEISHU_WEBHOOK_URL": ""}, clear=False):
                with patch("personal_quant.notifier.webhook_env._read_windows_user_env", return_value=None):
                    with self.assertRaises(ValueError) as exc:
                        send_push_slot(dt.date(2026, 7, 9), reports_dir, "pre_open", output_dir, live=True)

            self.assertIn("FEISHU_WEBHOOK_URL_REQUIRED", str(exc.exception))

    def test_webhook_resolution_prefers_explicit_url(self):
        result = resolve_feishu_webhook(
            " https://example.test/explicit ",
            environ={"FEISHU_WEBHOOK_URL": "https://example.test/process"},
            user_env_reader=lambda _name: "https://example.test/user",
        )

        self.assertEqual(result.url, "https://example.test/explicit")
        self.assertEqual(result.source, "命令参数")

    def test_webhook_resolution_reads_current_process_before_windows_user(self):
        result = resolve_feishu_webhook(
            environ={"FEISHU_WEBHOOK_URL": " https://example.test/process "},
            user_env_reader=lambda _name: "https://example.test/user",
        )

        self.assertEqual(result.url, "https://example.test/process")
        self.assertEqual(result.source, "当前进程环境变量")

    def test_webhook_resolution_reads_windows_user_env(self):
        result = resolve_feishu_webhook(
            environ={},
            user_env_reader=lambda _name: " https://example.test/user ",
        )

        self.assertEqual(result.url, "https://example.test/user")
        self.assertEqual(result.source, "Windows 用户环境变量")

    def test_live_sends_each_message_and_records_response(self):
        with TemporaryDirectory() as tmp:
            reports_dir = Path(tmp) / "reports"
            output_dir = Path(tmp) / "runs"
            sent: list[tuple[str, dict[str, object]]] = []
            _write_preview(reports_dir, "2026-07-09")

            def fake_sender(webhook_url: str, payload: dict[str, object]) -> FeishuWebhookResult:
                sent.append((webhook_url, payload))
                return FeishuWebhookResult(status_code=200, response_text='{"code":0,"msg":"success"}')

            path = send_push_slot(
                dt.date(2026, 7, 9),
                reports_dir,
                "pre_open",
                output_dir,
                live=True,
                webhook_url="https://example.test/webhook",
                sender=fake_sender,
            )

            self.assertEqual(len(sent), 2)
            self.assertEqual(sent[0][1]["msg_type"], "interactive")
            header_title = sent[0][1]["card"]["header"]["title"]["content"]
            element_texts = [element["text"]["content"] for element in sent[0][1]["card"]["elements"]]
            self.assertTrue(header_title.endswith("2026-07-09 08:50"))
            self.assertNotIn("**时间**", "\n".join(element_texts))
            serialized = json.dumps(sent[0][1], ensure_ascii=False)
            self.assertIn("个人量化｜开盘前计划", serialized)
            self.assertIn("先看结论", serialized)
            self.assertIn("只提示，不自动下单", serialized)
            self.assertIn("飞书响应", path.read_text(encoding="utf-8"))

    def test_live_card_is_human_readable_report_not_raw_markdown(self):
        with TemporaryDirectory() as tmp:
            reports_dir = Path(tmp) / "reports"
            output_dir = Path(tmp) / "runs"
            sent: list[tuple[str, dict[str, object]]] = []
            _write_human_preview(reports_dir, "2026-07-09")

            def fake_sender(webhook_url: str, payload: dict[str, object]) -> FeishuWebhookResult:
                sent.append((webhook_url, payload))
                return FeishuWebhookResult(status_code=200, response_text='{"code":0,"msg":"success"}')

            send_push_slot(
                dt.date(2026, 7, 9),
                reports_dir,
                "evening",
                output_dir,
                live=True,
                webhook_url="https://example.test/webhook",
                sender=fake_sender,
            )

            intraday_text = _card_text(sent[0][1])
            positions_text = _card_text(sent[1][1])
            next_day_text = _card_text(sent[4][1])
            self.assertIn("**市场状态**", intraday_text)
            self.assertIn("**主线判断**", intraday_text)
            self.assertIn("**资金与热点**", intraday_text)
            self.assertIn("**原因分析**", intraday_text)
            self.assertIn("**消息影响**", intraday_text)
            self.assertIn("**今日走势推演**", intraday_text)
            self.assertIn("商业航天", intraday_text)
            self.assertIn("关注股票", intraday_text)
            self.assertNotIn("消息影响：利好 政策新闻", intraday_text)
            self.assertIn("高开", intraday_text)
            self.assertIn("低开", intraday_text)
            self.assertIn("震荡", intraday_text)
            self.assertIn("**今日走势推演**", next_day_text)
            self.assertIn("**仓位结论**", positions_text)
            self.assertIn("京东方Ａ：观察", positions_text)
            self.assertNotIn("##", intraday_text)
            self.assertNotIn("完整内容", intraday_text)
            self.assertNotIn("数据质量", intraday_text)
            self.assertNotIn("| 股票 |", positions_text)

    def test_generate_push_send_uses_dry_run_by_default(self):
        with TemporaryDirectory() as tmp:
            reports_dir = Path(tmp) / "reports"
            output_dir = Path(tmp) / "runs"
            _write_preview(reports_dir, "2026-07-09")

            path = generate_push_send(dt.date(2026, 7, 9), reports_dir, "evening", output_dir)

            self.assertEqual(path.name, "push_receipt_2026-07-09_evening.md")
            text = path.read_text(encoding="utf-8")
            self.assertIn("模式：dry-run", text)
            self.assertIn("今日盘面总结", text)
            self.assertIn("个股操作与仓位调整", text)
            self.assertIn("利好利空消息分析", text)

    def test_push_run_pre_open_only_reads_existing_preview(self):
        with TemporaryDirectory() as tmp:
            reports_dir = Path(tmp) / "reports"
            output_dir = Path(tmp) / "runs"
            _write_preview(reports_dir, "2026-07-09")

            with patch("personal_quant.cli.fetch_market_dataset") as fetch_market_dataset:
                fetch_market_dataset.side_effect = AssertionError("开盘前不应重新拉行情")

                path = generate_push_run(dt.date(2026, 7, 9), "pre_open", reports_dir, output_dir)

            self.assertIn("消息数：2", path.read_text(encoding="utf-8"))

    def test_push_run_generates_daily_pack_before_sending(self):
        with TemporaryDirectory() as tmp:
            reports_dir = Path(tmp) / "reports"
            output_dir = Path(tmp) / "runs"

            with patch("personal_quant.cli.fetch_market_dataset", return_value=_dataset()):
                path = generate_push_run(
                    dt.date(2026, 7, 9),
                    "evening",
                    reports_dir,
                    output_dir,
                    focus_positions=False,
                )

            self.assertTrue((reports_dir / "2026-07-09" / "push_preview_2026-07-09.md").exists())
            self.assertIn("消息数：6", path.read_text(encoding="utf-8"))

    def test_push_daily_pre_open_uses_previous_expected_date(self):
        with TemporaryDirectory() as tmp:
            reports_dir = Path(tmp) / "reports"
            output_dir = Path(tmp) / "runs"
            expected_dates = Path(tmp) / "expected_dates.txt"
            expected_dates.write_text("2026-07-09\n2026-07-10\n", encoding="utf-8")
            _write_preview(reports_dir, "2026-07-09")

            with patch("personal_quant.cli.fetch_market_dataset") as fetch_market_dataset:
                fetch_market_dataset.side_effect = AssertionError("开盘前不应重新拉行情")

                path = generate_push_daily(
                    "pre_open",
                    reports_dir,
                    output_dir,
                    expected_dates,
                    focus_positions=False,
                    today=dt.date(2026, 7, 10),
                )

            self.assertEqual(path.name, "push_receipt_2026-07-09_pre_open.md")

    def test_push_daily_intraday_requires_today_in_expected_dates(self):
        with TemporaryDirectory() as tmp:
            reports_dir = Path(tmp) / "reports"
            output_dir = Path(tmp) / "runs"
            expected_dates = Path(tmp) / "expected_dates.txt"
            expected_dates.write_text("2026-07-09\n", encoding="utf-8")

            with patch("personal_quant.cli.fetch_market_dataset") as fetch_market_dataset:
                fetch_market_dataset.side_effect = AssertionError("非交易日不应拉行情")

                with self.assertRaises(ValueError) as exc:
                    generate_push_daily(
                        "morning",
                        reports_dir,
                        output_dir,
                        expected_dates,
                        focus_positions=False,
                        today=dt.date(2026, 7, 10),
                    )

                fetch_market_dataset.assert_not_called()

            self.assertIn("PUSH_DAILY_DATE_NOT_EXPECTED:2026-07-10", str(exc.exception))

    def test_push_daily_intraday_generates_today_report(self):
        with TemporaryDirectory() as tmp:
            reports_dir = Path(tmp) / "reports"
            output_dir = Path(tmp) / "runs"
            expected_dates = Path(tmp) / "expected_dates.txt"
            expected_dates.write_text("2026-07-10\n", encoding="utf-8")

            with patch("personal_quant.cli.fetch_market_dataset", return_value=_dataset(as_of=dt.date(2026, 7, 10))):
                path = generate_push_daily(
                    "morning",
                    reports_dir,
                    output_dir,
                    expected_dates,
                    focus_positions=False,
                    today=dt.date(2026, 7, 10),
                )

            self.assertEqual(path.name, "push_receipt_2026-07-10_morning.md")
            self.assertTrue((reports_dir / "2026-07-10" / "push_preview_2026-07-10.md").exists())

    def test_windows_task_script_uses_push_daily(self):
        script = build_windows_task_script(
            Path("D:/project"),
            focus_positions=False,
            live=True,
        )

        self.assertIn("Register-ScheduledTask", script)
        self.assertIn("FEISHU_WEBHOOK_URL", script)
        self.assertIn("push-daily --slot pre_open", script)
        self.assertIn("push-daily --slot midday", script)
        self.assertIn("push-daily --slot evening", script)
        self.assertIn("market-monitor", script)
        self.assertIn("--ignore-positions --live", script)
        self.assertNotIn("position-monitor", script)
        script.encode("ascii")

    def test_windows_task_script_can_use_positions_file(self):
        script = build_windows_task_script(
            Path("D:/project"),
            positions_path=Path("data/positions.csv"),
            live=True,
        )

        self.assertIn("push-daily --slot morning", script)
        self.assertIn("position-monitor --positions data/positions.csv", script)
        self.assertIn("market-monitor --positions data/positions.csv", script)
        self.assertIn("PersonalQuant_position_monitor", script)
        self.assertIn("PersonalQuant_market_monitor", script)
        self.assertIn("--positions data/positions.csv --live", script)
        self.assertNotIn("--ignore-positions", script)

    def test_generate_windows_task_script_writes_file(self):
        with TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "register_push_tasks.ps1"

            path = generate_windows_task_script(output_path, project_dir=Path("D:/project"), focus_positions=False)

            self.assertEqual(path, output_path)
            self.assertIn("push-daily", path.read_text(encoding="utf-8"))

    def test_windows_task_status_report_requires_all_tasks(self):
        # 回归：按 PUSH_SLOTS 动态生成，以后再新增推送时段本用例也不会过期
        statuses = [
            WindowsTaskStatus(
                f"PersonalQuant_{slot.key}", slot.name, slot.time, True, "Ready", slot.time, "push-daily"
            )
            for slot in PUSH_SLOTS
        ]
        statuses.append(
            WindowsTaskStatus(
                POSITION_MONITOR_TASK_NAME,
                POSITION_MONITOR_SLOT_NAME,
                POSITION_MONITOR_TIME,
                True,
                "Ready",
                POSITION_MONITOR_TIME,
                "position-monitor",
            )
        )
        statuses.append(
            WindowsTaskStatus(
                MARKET_MONITOR_TASK_NAME,
                MARKET_MONITOR_SLOT_NAME,
                MARKET_MONITOR_TIME,
                True,
                "Ready",
                MARKET_MONITOR_TIME,
                "market-monitor",
            )
        )
        missing_index = len(PUSH_SLOTS) - 1
        missing = statuses[missing_index]
        statuses[missing_index] = WindowsTaskStatus(
            missing.task_name, missing.slot_name, missing.time, False, "缺失", "", ""
        )

        report = build_windows_task_status_report(tuple(statuses))

        total = len(PUSH_SLOTS) + 2
        self.assertEqual(total, len(statuses))
        self.assertIn("结果：未通过", report)
        self.assertIn(f"任务数：{total - 1}/{total}", report)
        self.assertIn(f"| {missing.task_name} | {missing.slot_name} | {missing.time} | 缺失 |", report)

    def test_push_test_dry_run_writes_unverified_receipt(self):
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "runs"

            path = generate_push_test(output_dir, now=dt.datetime(2026, 7, 10, 9, 0))

            text = path.read_text(encoding="utf-8")
            self.assertIn("模式：dry-run", text)
            self.assertIn("结论：未验证", text)

    def test_push_test_live_requires_webhook_url(self):
        with TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"FEISHU_WEBHOOK_URL": ""}, clear=False):
                with patch("personal_quant.notifier.webhook_env._read_windows_user_env", return_value=None):
                    with self.assertRaises(ValueError) as exc:
                        generate_push_test(Path(tmp), live=True)

            self.assertIn("FEISHU_WEBHOOK_URL_REQUIRED", str(exc.exception))

    def test_push_test_live_sends_message_and_records_response(self):
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "runs"
            sent: list[tuple[str, dict[str, object]]] = []

            def fake_sender(webhook_url: str, payload: dict[str, object]) -> FeishuWebhookResult:
                sent.append((webhook_url, payload))
                return FeishuWebhookResult(status_code=200, response_text='{"code":0,"msg":"success"}')

            path = send_push_channel_test(
                output_dir,
                webhook_url="https://example.test/webhook",
                live=True,
                sender=fake_sender,
                now=dt.datetime(2026, 7, 10, 9, 0),
            )

            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0][1]["msg_type"], "interactive")
            header_title = sent[0][1]["card"]["header"]["title"]["content"]
            element_texts = [element["text"]["content"] for element in sent[0][1]["card"]["elements"]]
            self.assertTrue(header_title.endswith("2026-07-10 09:00:00"))
            self.assertNotIn("**时间**", "\n".join(element_texts))
            self.assertIn("推送通道测试", json.dumps(sent[0][1], ensure_ascii=False))
            text = path.read_text(encoding="utf-8")
            self.assertIn("模式：live", text)
            self.assertIn("格式：飞书卡片", text)
            self.assertIn("结论：通过", text)
            self.assertIn("Webhook 指纹：", text)

    def test_push_status_exposes_missing_webhook(self):
        with TemporaryDirectory() as tmp:
            reports_dir = Path(tmp) / "reports"
            runs_dir = Path(tmp) / "runs"
            expected_dates = Path(tmp) / "expected_dates.txt"
            expected_dates.write_text("2026-07-09\n", encoding="utf-8")
            _write_preview(reports_dir, "2026-07-09")
            (runs_dir / "push_schedule.md").parent.mkdir(parents=True)
            (runs_dir / "push_schedule.md").write_text("schedule", encoding="utf-8")
            (runs_dir / "register_push_tasks.ps1").write_text("tasks", encoding="utf-8")

            with patch.dict(os.environ, {"FEISHU_WEBHOOK_URL": ""}, clear=False):
                with patch("personal_quant.notifier.webhook_env._read_windows_user_env", return_value=None):
                    report = build_push_status_report(
                        reports_dir=reports_dir,
                        runs_dir=runs_dir,
                        expected_dates_path=expected_dates,
                        today=dt.date(2026, 7, 10),
                    )

            self.assertIn("真实推送通道：未就绪", report)
            self.assertIn("| 飞书 webhook | 真实发送必须设置 FEISHU_WEBHOOK_URL | 未设置 | 未通过 |", report)
            self.assertIn("今日 08:50：未就绪", report)

    def test_push_status_exposes_windows_user_webhook_source(self):
        with TemporaryDirectory() as tmp:
            reports_dir = Path(tmp) / "reports"
            runs_dir = Path(tmp) / "runs"
            expected_dates = Path(tmp) / "expected_dates.txt"
            expected_dates.write_text("2026-07-09\n", encoding="utf-8")
            _write_preview(reports_dir, "2026-07-09")
            runs_dir.mkdir()
            (runs_dir / "push_schedule.md").write_text("schedule", encoding="utf-8")
            (runs_dir / "register_push_tasks.ps1").write_text("tasks", encoding="utf-8")

            with patch.dict(os.environ, {"FEISHU_WEBHOOK_URL": ""}, clear=False):
                with patch(
                    "personal_quant.notifier.webhook_env._read_windows_user_env",
                    return_value="https://example.test/user",
                ):
                    report = build_push_status_report(
                        reports_dir=reports_dir,
                        runs_dir=runs_dir,
                        expected_dates_path=expected_dates,
                        today=dt.date(2026, 7, 10),
                    )

            self.assertIn("已设置（来源：Windows 用户环境变量，不显示密钥）", report)

    def test_push_status_requires_live_channel_test(self):
        with TemporaryDirectory() as tmp:
            reports_dir = Path(tmp) / "reports"
            runs_dir = Path(tmp) / "runs"
            expected_dates = Path(tmp) / "expected_dates.txt"
            expected_dates.write_text("2026-07-09\n2026-07-10\n", encoding="utf-8")
            _write_preview(reports_dir, "2026-07-09")
            runs_dir.mkdir()
            (runs_dir / "push_schedule.md").write_text("schedule", encoding="utf-8")
            (runs_dir / "register_push_tasks.ps1").write_text("tasks", encoding="utf-8")
            generate_push_test(runs_dir, now=dt.datetime(2026, 7, 10, 9, 0))

            report = build_push_status_report(
                reports_dir=reports_dir,
                runs_dir=runs_dir,
                expected_dates_path=expected_dates,
                today=dt.date(2026, 7, 10),
                webhook_url="https://example.test/webhook",
            )

            self.assertIn("真实推送通道：未就绪", report)
            self.assertIn("模式 dry-run", report)
            self.assertIn("| 飞书通道测试 | 必须有一次当前 webhook 的 live 飞书卡片测试通过 |", report)

    def test_push_status_rejects_channel_test_from_other_webhook(self):
        with TemporaryDirectory() as tmp:
            reports_dir = Path(tmp) / "reports"
            runs_dir = Path(tmp) / "runs"
            expected_dates = Path(tmp) / "expected_dates.txt"
            expected_dates.write_text("2026-07-09\n2026-07-10\n", encoding="utf-8")
            _write_preview(reports_dir, "2026-07-09")
            runs_dir.mkdir()
            (runs_dir / "push_schedule.md").write_text("schedule", encoding="utf-8")
            (runs_dir / "register_push_tasks.ps1").write_text("tasks", encoding="utf-8")
            send_push_channel_test(
                runs_dir,
                webhook_url="https://example.test/old",
                live=True,
                sender=lambda _url, _text: FeishuWebhookResult(200, '{"code":0,"msg":"success"}'),
                now=dt.datetime(2026, 7, 10, 9, 0),
            )

            report = build_push_status_report(
                reports_dir=reports_dir,
                runs_dir=runs_dir,
                expected_dates_path=expected_dates,
                today=dt.date(2026, 7, 10),
                webhook_url="https://example.test/new",
            )

            self.assertIn("真实推送通道：未就绪", report)
            self.assertIn("Webhook 指纹 不匹配", report)

    def test_push_status_rejects_old_text_channel_test(self):
        with TemporaryDirectory() as tmp:
            reports_dir = Path(tmp) / "reports"
            runs_dir = Path(tmp) / "runs"
            expected_dates = Path(tmp) / "expected_dates.txt"
            webhook_url = "https://example.test/webhook"
            expected_dates.write_text("2026-07-09\n2026-07-10\n", encoding="utf-8")
            _write_preview(reports_dir, "2026-07-09")
            runs_dir.mkdir()
            (runs_dir / "push_schedule.md").write_text("schedule", encoding="utf-8")
            (runs_dir / "register_push_tasks.ps1").write_text("tasks", encoding="utf-8")
            _write_task_status(runs_dir, passed=True)
            (runs_dir / "push_channel_test.md").write_text(
                f"""# 推送通道测试（2026-07-10 09:00:00）

## 总判断
- 模式：live
- 通道：飞书 webhook
- 发送状态：已发送
- 结论：通过
- Webhook 指纹：{webhook_url_fingerprint(webhook_url)}
""",
                encoding="utf-8",
            )

            report = build_push_status_report(
                reports_dir=reports_dir,
                runs_dir=runs_dir,
                expected_dates_path=expected_dates,
                today=dt.date(2026, 7, 10),
                webhook_url=webhook_url,
            )

            self.assertIn("真实推送通道：未就绪", report)
            self.assertIn("格式 未记录", report)

    def test_push_status_ready_when_webhook_dates_and_channel_test_are_ready(self):
        with TemporaryDirectory() as tmp:
            reports_dir = Path(tmp) / "reports"
            runs_dir = Path(tmp) / "runs"
            expected_dates = Path(tmp) / "expected_dates.txt"
            expected_dates.write_text("2026-07-09\n2026-07-10\n", encoding="utf-8")
            _write_preview(reports_dir, "2026-07-09")
            runs_dir.mkdir()
            (runs_dir / "push_schedule.md").write_text("schedule", encoding="utf-8")
            (runs_dir / "register_push_tasks.ps1").write_text("tasks", encoding="utf-8")
            _write_task_status(runs_dir, passed=True)
            send_push_channel_test(
                runs_dir,
                webhook_url="https://example.test/webhook",
                live=True,
                sender=lambda _url, _text: FeishuWebhookResult(200, '{"code":0,"msg":"success"}'),
                now=dt.datetime(2026, 7, 10, 9, 0),
            )

            report = build_push_status_report(
                reports_dir=reports_dir,
                runs_dir=runs_dir,
                expected_dates_path=expected_dates,
                today=dt.date(2026, 7, 10),
                webhook_url="https://example.test/webhook",
            )

            self.assertIn("真实推送通道：就绪", report)
            self.assertIn("今日 08:50：就绪", report)
            self.assertIn("今日盘中/午间/收盘：就绪", report)
            self.assertIn("自动定时：就绪", report)

    def test_generate_push_status_writes_file(self):
        with TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "push_status.md"
            expected_dates = Path(tmp) / "expected_dates.txt"
            expected_dates.write_text("2026-07-09\n", encoding="utf-8")

            path = generate_push_status(
                output_path,
                reports_dir=Path(tmp) / "reports",
                runs_dir=Path(tmp) / "runs",
                expected_dates_path=expected_dates,
                today=dt.date(2026, 7, 10),
            )

            self.assertEqual(path, output_path)
            self.assertIn("推送上线自检", path.read_text(encoding="utf-8"))

    def test_push_go_live_report_blocks_unready_status(self):
        with TemporaryDirectory() as tmp:
            runs_dir = Path(tmp) / "runs"
            runs_dir.mkdir()
            (runs_dir / "push_status.md").write_text(
                """# 推送上线自检（2026-07-10）

## 总判断
- 真实推送通道：未就绪
- 自动定时：未就绪

## 检查项
| 项目 | 要求 | 当前 | 状态 |
|---|---|---|---|
| 飞书 webhook | 真实发送必须设置 FEISHU_WEBHOOK_URL | 未设置 | 未通过 |
| 交易日清单 | expected_dates.txt 必须存在且可解析 | 1 天，最新 2026-07-09 | 通过 |
| 飞书通道测试 | 必须有一次当前 webhook 的 live 测试通过 | dry-run | 未通过 |
| 推送时间表 | push_schedule.md 必须已生成 | 已生成 | 通过 |
| Windows 定时脚本 | register_push_tasks.ps1 必须已生成；是否注册由系统任务决定 | 已生成 | 通过 |
| Windows 任务注册 | 全部 PersonalQuant 计划任务必须已注册 | 0/6 | 未通过 |
| 持仓实时提醒输入 | data/positions.csv 必须是真实持仓文件 | 缺口：POSITIONS_FILE_MISSING | 未通过 |
""",
                encoding="utf-8",
            )

            report = build_push_go_live_report(runs_dir=runs_dir)

            self.assertIn("结果：未通过", report)
            self.assertIn("是否可以自动真实推送：否", report)
            self.assertIn("设置用户环境变量 FEISHU_WEBHOOK_URL", report)
            self.assertIn("push-test --output-dir planning/runs --live", report)

    def test_generate_push_go_live_writes_file(self):
        with TemporaryDirectory() as tmp:
            runs_dir = Path(tmp) / "runs"
            runs_dir.mkdir()
            (runs_dir / "push_status.md").write_text(
                """# 推送上线自检（2026-07-10）

## 总判断
- 真实推送通道：就绪
- 自动定时：就绪

## 检查项
| 项目 | 要求 | 当前 | 状态 |
|---|---|---|---|
| 飞书 webhook | 真实发送必须设置 FEISHU_WEBHOOK_URL | 已设置 | 通过 |
| 交易日清单 | expected_dates.txt 必须存在且可解析 | 1 天 | 通过 |
| 飞书通道测试 | 必须有一次当前 webhook 的 live 测试通过 | live | 通过 |
| 推送时间表 | push_schedule.md 必须已生成 | 已生成 | 通过 |
| Windows 定时脚本 | register_push_tasks.ps1 必须已生成；是否注册由系统任务决定 | 已生成 | 通过 |
| Windows 任务注册 | 全部 PersonalQuant 计划任务必须已注册 | 7/7 | 通过 |
| 持仓实时提醒输入 | data/positions.csv 必须是真实持仓文件 | 已就绪，持仓 1 个 | 通过 |
""",
                encoding="utf-8",
            )
            output_path = Path(tmp) / "push_go_live.md"

            path = generate_push_go_live(output_path, runs_dir=runs_dir)

            self.assertEqual(path, output_path)
            self.assertIn("结果：通过", path.read_text(encoding="utf-8"))

    def test_push_activation_report_turns_gate_gaps_into_steps(self):
        with TemporaryDirectory() as tmp:
            runs_dir = Path(tmp) / "runs"
            runs_dir.mkdir()
            _write_go_live_report(runs_dir, passed=False)

            report = build_push_activation_report(
                runs_dir=runs_dir,
                reports_dir=Path("reports"),
                expected_dates_path=Path("planning/runs/expected_dates.txt"),
                project_dir=Path("D:/project"),
            )

            self.assertIn("门禁：未通过", report)
            self.assertIn("设置飞书 webhook", report)
            self.assertIn("push-test --output-dir", report)
            self.assertIn("register_push_tasks.ps1", report)
            self.assertIn("push-go-live", report)

    def test_generate_push_activation_writes_file(self):
        with TemporaryDirectory() as tmp:
            runs_dir = Path(tmp) / "runs"
            runs_dir.mkdir()
            _write_go_live_report(runs_dir, passed=True)
            output_path = Path(tmp) / "push_activation.md"

            path = generate_push_activation(output_path, runs_dir=runs_dir, project_dir=Path("D:/project"))

            self.assertEqual(path, output_path)
            text = path.read_text(encoding="utf-8")
            self.assertIn("推送激活清单", text)
            self.assertIn("门禁：通过", text)

    def test_feishu_text_payload_shape(self):
        payload = build_feishu_text_payload("hello")

        self.assertEqual(payload, {"msg_type": "text", "content": {"text": "hello"}})

    def test_feishu_card_payload_shape(self):
        payload = build_feishu_card_payload(
            title="个人量化｜收盘复盘",
            markdown_blocks=("**先看重点**\n防守为主", "**边界**\n只提示，不自动下单"),
            template="orange",
        )

        self.assertEqual(payload["msg_type"], "interactive")
        card = payload["card"]
        self.assertEqual(card["config"], {"wide_screen_mode": True})
        self.assertEqual(card["header"]["template"], "orange")
        self.assertEqual(card["header"]["title"]["content"], "个人量化｜收盘复盘")
        self.assertEqual(card["elements"][0]["text"]["tag"], "lark_md")
        self.assertIn("先看重点", card["elements"][0]["text"]["content"])


def _write_preview(reports_dir: Path, day: str) -> None:
    archive_dir = reports_dir / day
    archive_dir.mkdir(parents=True)
    (archive_dir / f"push_preview_{day}.md").write_text(
        f"""# 每日推送预览（{day}）

- 数据：完整；缺口：暂无

## 盘中实时盘面
盘中内容

## 持仓个股操作
持仓内容
仓位调整建议

## 建仓候选推荐
候选内容

## 利好利空捕捉
新闻内容

## 晚间复盘
复盘内容

## 次日操作计划
次日计划

## 风险提示
风险内容
""",
        encoding="utf-8",
    )


def _write_human_preview(reports_dir: Path, day: str) -> None:
    archive_dir = reports_dir / day
    archive_dir.mkdir(parents=True)
    (archive_dir / f"push_preview_{day}.md").write_text(
        f"""# 每日推送预览（{day}）

- 数据：完整；缺口：暂无

## 盘中实时盘面
## 数据质量
- 数据：完整；失败项：暂无；时间 08:33；覆盖 板块992、个股5194、指数5、外围7、新闻50、持仓3

## 市场状态
- 盘面：高位兑现 + 低位反弹；上证指数 -1.00%；深证成指 -2.29%
- 广度：全市场成交 33884亿，上涨家数 3512，下跌家数 1612
- 资金：最强流入 商业航天 110.86亿；最强流出 科技风格 -459.51亿

## 主线判断
- 主线：暂无；反弹：横向通用软件、游戏Ⅲ；回避：昨日涨停_含一字、存储芯片
- 结论：无清晰主线，不硬做

## 当天热门板块
| 板块 | 今日 | 20日涨幅 | 成交变化 | 标签 |
|---|---:|---:|---:|---|
| 航天装备Ⅲ | 10.36% | 8.45% | 1.58 | 一日游风险 |

## 消息驱动分析
- 消息来源：东方财富快讯、政策新闻；新闻总数 50
- 商业航天：可能原因：政策新闻 09:10《商业航天产业政策利好 商业航天订单增长》（偏利好，标题包含：商业航天）；盘面证据：涨跌幅 6.20%，主力净流入 110.86亿；可能影响：商业航天；交易含义：消息与资金同向时看持续性，不追高。
- 科技风格：可能原因：暂无明确消息证据；盘面证据：涨跌幅 -4.20%，主力净流入 -459.51亿；可能影响：只能按资金和价格处理；交易含义：不把它当作新闻驱动交易依据。

## 早盘走势推演
- 外围表现：正向 0/2（暂无）；负向 2/2（纳斯达克、英伟达）
- 消息影响：商业航天：偏利好；关注股票：航天样本(000001)
- 高开：不追高，先看商业航天是否继续放量；没有持续资金就不扩大试错。
- 低开：先看商业航天是否抗跌；科技风格资金未回流前不补仓、不追反弹。
- 震荡：只观察主线扩散和资金回流；没有主升确认就维持防守。

## 今日操作建议
- 策略：防守为主，反弹只观察，不重仓试错

## 持仓个股操作
## 仓位调整建议
- 总体：防守为主，反弹只观察，不重仓试错。先处理清仓标的：中晶科技(003026)。
- 分组：清仓 中晶科技(003026)；减仓 惠科股份(001399)；观察 京东方Ａ(000725)

| 股票 | 数量 | 成本 | 现价 | 浮盈亏 | 板块状态 | 建议 | 止损 | 止盈 | 减仓条件 | 补仓条件 | 清仓条件 | 利好利空 |
|---|---:|---:|---:|---:|---|---|---|---|---|---|---|---|
| 京东方Ａ(000725) | 11000 | 8.05 | 7.59 | -5.74% | 观察候选 | 观察 | 跌破 7.21 | 反弹到 8.05 先减仓 | 反抽缩量 | 不补仓 | 跌破止损位 | 暂无匹配消息 |

## 建仓候选推荐
## 建仓候选
暂无满足主升条件的建仓候选。

## 利好利空捕捉
## 利好利空信息
暂无匹配消息。新闻总数：50。

## 晚间复盘
- 复盘：高位兑现 + 低位反弹；失败/回避 昨日涨停_含一字、存储芯片

## 次日操作计划
- 次日：高开不追；低开看抗跌；震荡只观察主线扩散。
- 关注：板块 暂无明确主升；动作 防守为主，反弹只观察

## 早盘走势推演
- 外围表现：正向 0/2（暂无）；负向 2/2（纳斯达克、英伟达）
- 消息影响：商业航天：偏利好；关注股票：航天样本(000001)
- 高开：不追高，先看商业航天是否继续放量；没有持续资金就不扩大试错。
- 低开：先看商业航天是否抗跌；科技风格资金未回流前不补仓、不追反弹。
- 震荡：只观察主线扩散和资金回流；没有主升确认就维持防守。

## 风险提示
- 风险：持仓 已读取真实持仓：京东方Ａ、惠科股份、中晶科技；主线 无主升候选，暂停新增；人工确认
""",
        encoding="utf-8",
    )


def _card_text(payload: dict[str, object]) -> str:
    card = payload["card"]
    elements = card["elements"]
    return "\n".join(element["text"]["content"] for element in elements)


def _write_task_status(runs_dir: Path, passed: bool) -> None:
    result = "通过" if passed else "未通过"
    count = "7/7" if passed else "0/7"
    (runs_dir / "push_windows_task_status.md").write_text(
        f"""# Windows 定时任务状态

## 总判断
- 结果：{result}
- 任务数：{count}
""",
        encoding="utf-8",
    )


def _write_go_live_report(runs_dir: Path, passed: bool) -> None:
    result = "通过" if passed else "未通过"
    allowed = "是" if passed else "否"
    row_status = "通过" if passed else "未通过"
    (runs_dir / "push_go_live.md").write_text(
        f"""# 推送上线门禁

## 总判断
- 结果：{result}
- 是否可以自动真实推送：{allowed}

## 检查项
| 项目 | 要求 | 当前 | 状态 | 下一步 |
|---|---|---|---|---|
| 飞书 webhook | 必须设置 FEISHU_WEBHOOK_URL | 未设置 | {row_status} | 设置用户环境变量 FEISHU_WEBHOOK_URL |
| 飞书通道测试 | 当前 webhook 必须 live 测试通过 | dry-run | {row_status} | 运行 python -m personal_quant.cli push-test --output-dir planning/runs --live |
| Windows 任务注册 | 全部 PersonalQuant 计划任务必须已注册 | 0/6 | {row_status} | 以用户确认后运行 planning/runs/register_push_tasks.ps1 |
""",
        encoding="utf-8",
    )


def _dataset(as_of: dt.date = dt.date(2026, 7, 9)) -> MarketDataset:
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
        data_sources=("东方财富板块", "东方财富个股", "新浪财经美股历史", "东方财富快讯"),
        fetched_at=dt.datetime(2026, 7, 9, 11, 30),
    )


if __name__ == "__main__":
    unittest.main()
