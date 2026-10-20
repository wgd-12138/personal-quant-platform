import datetime as dt
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from personal_quant.data_center.models import BoardSnapshot, ExternalSnapshot, MarketDataset, NewsItem, StockSnapshot
from personal_quant.data_center.positions import PositionSnapshot
from personal_quant.notifier.feishu import FeishuWebhookResult
from personal_quant.notifier.market_monitor import (
    MarketMonitorConfig,
    build_market_alerts,
    run_market_monitor_once,
)


class MarketMonitorTests(unittest.TestCase):
    def test_negative_news_matching_position_triggers_risk_alert(self):
        alerts = build_market_alerts(
            _dataset(
                board=_board("银行", main_net_inflow=100000000, change_pct=0.5),
                stocks=[_stock("600000", "浦发银行", "银行", 10.0, 1.0)],
                news=[
                    _news(
                        "浦发银行收到监管问询",
                        "监管问询涉及经营风险。",
                        ("浦发银行",),
                    )
                ],
            ),
            [PositionSnapshot("600000", "浦发银行", 1000, 10.0)],
            dt.datetime(2026, 7, 10, 10, 0, 0),
        )

        self.assertEqual(alerts[0].kind, "negative_news")
        self.assertEqual(alerts[0].action, "风险检查")
        self.assertIn("浦发银行", alerts[0].target)
        self.assertIn("监管问询", alerts[0].evidence)

    def test_capital_outflow_triggers_board_alert(self):
        alerts = build_market_alerts(
            _dataset(
                board=_board("科技风格", main_net_inflow=-1000000000, change_pct=-3.0),
                stocks=[_stock("000001", "科技样本", "科技风格", 12.0, -2.0)],
                news=[],
            ),
            [],
            dt.datetime(2026, 7, 10, 10, 0, 0),
            config=MarketMonitorConfig(capital_outflow_threshold=-500000000, capital_outflow_drop_pct=-1.0),
        )

        self.assertEqual(alerts[0].kind, "capital_outflow")
        self.assertEqual(alerts[0].action, "降低仓位观察")
        self.assertIn("科技风格", alerts[0].target)
        self.assertIn("主力净流出", alerts[0].trigger)

    def test_positive_news_only_triggers_build_watch_alert_when_watchlist_exists(self):
        alerts = build_market_alerts(
            _dataset(
                board=_board("机器人", main_net_inflow=800000000, change_pct=4.5, five_day_return=12.0, up_count=12),
                stocks=[_stock("002747", "埃斯顿", "机器人", 18.0, 6.0)],
                news=[
                    _news(
                        "机器人政策利好 埃斯顿中标大单",
                        "政策支持机器人产业，埃斯顿中标订单。",
                        ("机器人", "埃斯顿"),
                    )
                ],
            ),
            [],
            dt.datetime(2026, 7, 10, 10, 0, 0),
        )

        self.assertEqual(alerts[0].kind, "positive_watchlist")
        self.assertEqual(alerts[0].action, "建仓观察")
        self.assertIn("埃斯顿", alerts[0].target)
        self.assertIn("主升候选", alerts[0].evidence)

    def test_run_market_monitor_once_sends_due_alert_and_records_cooldown(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            positions_path = root / "positions.csv"
            expected_dates = root / "expected_dates.txt"
            state_path = root / "state.json"
            output_dir = root / "runs"
            sent: list[dict[str, object]] = []
            positions_path.write_text("code,name,quantity,cost_price\n600000,浦发银行,1000,10\n", encoding="utf-8-sig")
            expected_dates.write_text("2026-07-10\n", encoding="utf-8")

            def fake_sender(_url: str, payload: dict[str, object]) -> FeishuWebhookResult:
                sent.append(payload)
                return FeishuWebhookResult(200, '{"code":0,"msg":"success"}')

            first = run_market_monitor_once(
                output_dir,
                state_path,
                expected_dates_path=expected_dates,
                positions_path=positions_path,
                live=True,
                webhook_url="https://example.test/webhook",
                now=dt.datetime(2026, 7, 10, 10, 0, 0),
                data_fetcher=lambda _date: _dataset(
                    board=_board("银行", main_net_inflow=100000000, change_pct=0.5),
                    stocks=[_stock("600000", "浦发银行", "银行", 10.0, 1.0)],
                    news=[_news("浦发银行收到监管问询", "监管问询。", ("浦发银行",))],
                ),
                sender=fake_sender,
            )
            second = run_market_monitor_once(
                output_dir,
                state_path,
                expected_dates_path=expected_dates,
                positions_path=positions_path,
                live=True,
                webhook_url="https://example.test/webhook",
                now=dt.datetime(2026, 7, 10, 10, 5, 0),
                data_fetcher=lambda _date: _dataset(
                    board=_board("银行", main_net_inflow=100000000, change_pct=0.5),
                    stocks=[_stock("600000", "浦发银行", "银行", 10.0, 1.0)],
                    news=[_news("浦发银行收到监管问询", "监管问询。", ("浦发银行",))],
                ),
                sender=fake_sender,
            )

            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0]["msg_type"], "interactive")
            header_title = sent[0]["card"]["header"]["title"]["content"]
            element_texts = [element["text"]["content"] for element in sent[0]["card"]["elements"]]
            self.assertTrue(header_title.endswith("2026-07-10 10:00:00"))
            self.assertNotIn("**时间**", "\n".join(element_texts))
            serialized = json.dumps(sent[0], ensure_ascii=False)
            self.assertIn("市场实时提醒", serialized)
            self.assertIn("下一步", serialized)
            self.assertIn("触发原因", serialized)
            self.assertIn("证据", serialized)
            self.assertIn("只提示，不自动下单", serialized)
            self.assertIn("market_monitor_20260710_100000.md", str(first))
            self.assertIn("冷却跳过：1", second.read_text(encoding="utf-8"))
            self.assertIn("2026-07-10:negative_news:浦发银行", state_path.read_text(encoding="utf-8"))

    def test_run_market_monitor_once_rejects_unexpected_date(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            expected_dates = root / "expected_dates.txt"
            expected_dates.write_text("2026-07-09\n", encoding="utf-8")

            with self.assertRaises(ValueError) as exc:
                run_market_monitor_once(
                    root,
                    root / "state.json",
                    expected_dates_path=expected_dates,
                    positions_path=None,
                    now=dt.datetime(2026, 7, 10, 10, 0, 0),
                    data_fetcher=lambda _date: _dataset(
                        board=_board("银行", main_net_inflow=100000000, change_pct=0.5),
                        stocks=[_stock("600000", "浦发银行", "银行", 10.0, 1.0)],
                        news=[],
                    ),
                )

            self.assertIn("MARKET_MONITOR_DATE_NOT_EXPECTED:2026-07-10", str(exc.exception))


def _dataset(board: BoardSnapshot, stocks: list[StockSnapshot], news: list[NewsItem]) -> MarketDataset:
    return MarketDataset(
        as_of=dt.date(2026, 7, 10),
        boards=[board],
        externals=[ExternalSnapshot("纳斯达克", ".IXIC", 1.0, 1.0)],
        stocks=stocks,
        news_items=news,
        data_sources=("测试行情", "测试新闻"),
        fetched_at=dt.datetime(2026, 7, 10, 10, 0, 0),
    )


def _board(
    name: str,
    main_net_inflow: float,
    change_pct: float,
    five_day_return: float = 1.0,
    up_count: int = 8,
) -> BoardSnapshot:
    return BoardSnapshot(
        code="BK0001",
        name=name,
        board_type="行业",
        change_pct=change_pct,
        amount=1000000000,
        main_net_inflow=main_net_inflow,
        five_day_return=five_day_return,
        sixty_day_return=2.0,
        up_count=up_count,
        down_count=4,
    )


def _stock(code: str, name: str, industry: str, price: float, change_pct: float) -> StockSnapshot:
    return StockSnapshot(
        code=code,
        name=name,
        price=price,
        change_pct=change_pct,
        amount=100000000,
        turnover_rate=1.0,
        industry=industry,
        high=price,
        low=price,
        previous_close=price / (1 + change_pct / 100),
    )


def _news(title: str, summary: str, related_names: tuple[str, ...]) -> NewsItem:
    return NewsItem(
        source="东方财富快讯",
        published_at=dt.datetime(2026, 7, 10, 10, 0, 0),
        title=title,
        summary=summary,
        related_names=related_names,
        url="",
    )


if __name__ == "__main__":
    unittest.main()
