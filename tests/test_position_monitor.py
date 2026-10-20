import datetime as dt
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from personal_quant.data_center.models import BoardSnapshot, ExternalSnapshot, MarketDataset, StockSnapshot
from personal_quant.data_center.positions import PositionSnapshot
from personal_quant.notifier.feishu import FeishuWebhookResult
from personal_quant.notifier.position_monitor import (
    PositionMonitorConfig,
    build_position_alerts,
    run_position_monitor_once,
)


class PositionMonitorTests(unittest.TestCase):
    def test_build_position_alerts_triggers_stop_loss_from_real_snapshot(self):
        alerts = build_position_alerts(
            [PositionSnapshot("600000", "浦发银行", 1000, 10.0)],
            _dataset(price=9.4, change_pct=-1.0),
            dt.datetime(2026, 7, 10, 10, 0, 0),
        )

        self.assertEqual(alerts[0].kind, "stop_loss")
        self.assertIn("浮盈亏 -6.00%", alerts[0].trigger)
        self.assertEqual(alerts[0].action, "止损检查")

    def test_run_position_monitor_once_sends_due_alert_and_records_cooldown(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            positions_path = root / "positions.csv"
            expected_dates = root / "expected_dates.txt"
            state_path = root / "state.json"
            output_dir = root / "runs"
            sent: list[dict[str, object]] = []
            positions_path.write_text(
                "code,name,quantity,cost_price\n600000,浦发银行,1000,10\n",
                encoding="utf-8-sig",
            )
            expected_dates.write_text("2026-07-10\n", encoding="utf-8")

            def fake_sender(_url: str, payload: dict[str, object]) -> FeishuWebhookResult:
                sent.append(payload)
                return FeishuWebhookResult(200, '{"code":0,"msg":"success"}')

            first = run_position_monitor_once(
                positions_path,
                output_dir,
                state_path,
                expected_dates_path=expected_dates,
                live=True,
                webhook_url="https://example.test/webhook",
                now=dt.datetime(2026, 7, 10, 10, 0, 0),
                data_fetcher=lambda _date: _dataset(price=9.4, change_pct=-1.0),
                sender=fake_sender,
            )
            second = run_position_monitor_once(
                positions_path,
                output_dir,
                state_path,
                expected_dates_path=expected_dates,
                live=True,
                webhook_url="https://example.test/webhook",
                now=dt.datetime(2026, 7, 10, 10, 5, 0),
                data_fetcher=lambda _date: _dataset(price=9.4, change_pct=-1.0),
                sender=fake_sender,
            )

            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0]["msg_type"], "interactive")
            header_title = sent[0]["card"]["header"]["title"]["content"]
            element_texts = [element["text"]["content"] for element in sent[0]["card"]["elements"]]
            self.assertTrue(header_title.endswith("2026-07-10 10:00:00"))
            self.assertNotIn("**时间**", "\n".join(element_texts))
            serialized = json.dumps(sent[0], ensure_ascii=False)
            self.assertIn("持仓实时提醒", serialized)
            self.assertIn("下一步", serialized)
            self.assertIn("触发原因", serialized)
            self.assertIn("关键数据", serialized)
            self.assertIn("处理纪律", serialized)
            self.assertIn("position_monitor_20260710_100000.md", str(first))
            self.assertIn("冷却跳过：1", second.read_text(encoding="utf-8"))
            self.assertIn("2026-07-10:600000:stop_loss", state_path.read_text(encoding="utf-8"))

    def test_run_position_monitor_once_rejects_unexpected_date(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            positions_path = root / "positions.csv"
            expected_dates = root / "expected_dates.txt"
            positions_path.write_text(
                "code,name,quantity,cost_price\n600000,浦发银行,1000,10\n",
                encoding="utf-8-sig",
            )
            expected_dates.write_text("2026-07-09\n", encoding="utf-8")

            with self.assertRaises(ValueError) as exc:
                run_position_monitor_once(
                    positions_path,
                    root,
                    root / "state.json",
                    expected_dates_path=expected_dates,
                    now=dt.datetime(2026, 7, 10, 10, 0, 0),
                    data_fetcher=lambda _date: _dataset(price=9.4, change_pct=-1.0),
                )

            self.assertIn("POSITION_MONITOR_DATE_NOT_EXPECTED:2026-07-10", str(exc.exception))


def _dataset(price: float, change_pct: float) -> MarketDataset:
    return MarketDataset(
        as_of=dt.date(2026, 7, 10),
        boards=[
            BoardSnapshot(
                code="BK0001",
                name="银行",
                board_type="行业",
                change_pct=0.5,
                amount=1000000000,
                main_net_inflow=1000000,
                five_day_return=1.0,
                sixty_day_return=2.0,
                up_count=8,
                down_count=4,
            )
        ],
        externals=[ExternalSnapshot("纳斯达克", ".IXIC", 1.0, 1.0)],
        stocks=[
            StockSnapshot(
                code="600000",
                name="浦发银行",
                price=price,
                change_pct=change_pct,
                amount=100000000,
                turnover_rate=1.0,
                industry="银行",
                high=10.1,
                low=price,
                previous_close=10.0,
            )
        ],
        data_sources=("测试行情",),
        fetched_at=dt.datetime(2026, 7, 10, 10, 0, 0),
    )


if __name__ == "__main__":
    unittest.main()
