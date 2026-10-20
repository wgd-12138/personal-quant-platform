import datetime as dt
import unittest

from personal_quant.data_center.models import BoardSnapshot, ExternalSnapshot, MarketDataset
from personal_quant.market_analysis.classifier import classify_board, summarize_market
from personal_quant.report_builder.summary import build_summary_report


class SummaryReportTests(unittest.TestCase):
    def test_classifies_rebound_separately_from_mainline(self):
        rebound = BoardSnapshot(
            code="BK0001",
            name="低位反弹",
            board_type="industry",
            change_pct=3.0,
            amount=1000000000,
            main_net_inflow=10000000,
            five_day_return=3.5,
            sixty_day_return=-10.0,
            up_count=20,
            down_count=3,
        )

        self.assertEqual(classify_board(rebound).label, "反弹候选")

    def test_classifies_strong_continuation_as_mainline(self):
        mainline = BoardSnapshot(
            code="BK0002",
            name="强势主升",
            board_type="concept",
            change_pct=5.0,
            amount=2000000000,
            main_net_inflow=160000000,
            five_day_return=12.0,
            sixty_day_return=18.0,
            up_count=18,
            down_count=2,
        )

        self.assertEqual(classify_board(mainline).label, "主升候选")

    def test_rejects_incomplete_dataset(self):
        dataset = MarketDataset(as_of=dt.date(2026, 7, 9), boards=[], externals=[])

        with self.assertRaises(ValueError):
            summarize_market(dataset)

    def test_report_states_no_watchlist_when_no_mainline(self):
        dataset = MarketDataset(
            as_of=dt.date(2026, 7, 9),
            boards=[
                BoardSnapshot(
                    code="BK0001",
                    name="低位反弹",
                    board_type="industry",
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
                )
            ],
        )

        summary = summarize_market(dataset)
        report = build_summary_report(summary)

        self.assertIn("## 当天热门板块", report)
        self.assertIn("20日涨幅", report)
        self.assertIn("60日涨幅", report)
        self.assertIn("暂无满足主升条件的建仓候选", report)
        self.assertIn("低位反弹", report)
        self.assertIn("纳斯达克", report)


if __name__ == "__main__":
    unittest.main()
