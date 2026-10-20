import datetime as dt
import unittest

from personal_quant.data_center.models import BoardSnapshot, ExternalSnapshot, IndexSnapshot, MarketDataset, NewsItem, StockSnapshot
from personal_quant.market_analysis.classifier import summarize_market
from personal_quant.strategy_signals import AuctionSnapshot, render_morning_build_report, render_overnight_arbitrage_report


class StrategySignalTests(unittest.TestCase):
    def test_morning_build_needs_auction_data(self):
        summary = summarize_market(_dataset())

        report = render_morning_build_report(summary, [], auctions=None)

        self.assertIn("## 早盘建仓推荐", report)
        self.assertIn("暂无", report)
        self.assertIn("缺集合竞价数据", report)
        self.assertNotIn("建仓价", report)

    def test_morning_build_recommends_only_suitable_stock_with_link(self):
        summary = summarize_market(_dataset())
        news = [_news()]
        auctions = [
            AuctionSnapshot(
                code="600000",
                name="浦发银行",
                auction_price=10.82,
                previous_close=10.59,
                auction_amount=80000000,
                auction_volume_ratio=2.2,
            )
        ]

        report = render_morning_build_report(summary, news, auctions=auctions)

        self.assertIn("浦发银行(600000)", report)
        self.assertIn("建仓价", report)
        self.assertIn("止盈", report)
        self.assertIn("止损", report)
        self.assertIn("原因：集合竞价温和高开", report)
        self.assertIn("链接：https://example.test/news/bank", report)

    def test_overnight_arbitrage_hides_high_stock(self):
        summary = summarize_market(_dataset(high_stock=True))

        report = render_overnight_arbitrage_report(summary, [_news()])

        self.assertIn("## 尾盘隔夜套利", report)
        self.assertIn("暂无满足隔夜套利条件的标的", report)
        self.assertNotIn("浦发银行(600000)", report)

    def test_overnight_arbitrage_recommends_one_to_two_stocks(self):
        summary = summarize_market(_dataset())

        report = render_overnight_arbitrage_report(summary, [_news()])

        self.assertIn("浦发银行(600000)", report)
        self.assertIn("尾盘建仓价", report)
        self.assertIn("次日冲高止盈", report)
        self.assertIn("止损", report)
        self.assertIn("第二天冲高全部抛出", report)


def _dataset(high_stock: bool = False) -> MarketDataset:
    change_pct = 6.8 if high_stock else 2.0
    return MarketDataset(
        as_of=dt.date(2026, 7, 10),
        boards=[
            BoardSnapshot(
                code="BK0001",
                name="银行",
                board_type="行业",
                change_pct=5.0,
                amount=2000000000,
                main_net_inflow=160000000,
                five_day_return=12.0,
                sixty_day_return=18.0,
                up_count=18,
                down_count=2,
            )
        ],
        externals=[ExternalSnapshot("纳斯达克", "^IXIC", 4.0, 2.0)],
        indexes=[IndexSnapshot("000001", "上证指数", 3952.49, -0.46, -18.39, 790773118087.2)],
        stocks=[
            StockSnapshot(
                code="600000",
                name="浦发银行",
                price=10.8,
                change_pct=change_pct,
                amount=100000000,
                turnover_rate=2.0,
                industry="银行",
                high=10.9,
                low=10.5,
                previous_close=10.59,
            )
        ],
        data_sources=("东方财富板块", "东方财富个股", "东方财富快讯"),
        fetched_at=dt.datetime(2026, 7, 10, 14, 30),
    )


def _news() -> NewsItem:
    return NewsItem(
        source="政策新闻",
        published_at=dt.datetime(2026, 7, 10, 9, 10),
        title="银行政策利好 浦发银行成交放大",
        summary="政策支持银行板块，浦发银行成交放大。",
        related_names=("银行", "浦发银行"),
        url="https://example.test/news/bank",
    )


if __name__ == "__main__":
    unittest.main()
