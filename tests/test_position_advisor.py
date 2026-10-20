import unittest

from personal_quant.data_center.models import StockSnapshot
from personal_quant.data_center.positions import PositionSnapshot
from personal_quant.position_advisor.advisor import advise_position


class PositionAdvisorTests(unittest.TestCase):
    def test_high_position_drawdown_requires_clear(self):
        position = PositionSnapshot("600000", "浦发银行", 100, 10.0, "2026-07-01")
        stock = StockSnapshot(
            code="600000",
            name="浦发银行",
            price=9.2,
            change_pct=-3.0,
            amount=100000000,
            turnover_rate=2.0,
            industry="银行",
        )

        advice = advise_position(position, stock, "高位兑现")

        self.assertEqual(advice.action, "清仓")
        self.assertIn("9.50", advice.stop_loss)
        self.assertTrue(advice.take_profit)
        self.assertTrue(advice.reduce_condition)
        self.assertTrue(advice.add_condition)
        self.assertTrue(advice.clear_condition)

    def test_mainline_position_can_hold_but_still_has_boundaries(self):
        position = PositionSnapshot("600000", "浦发银行", 100, 10.0, "2026-07-01")
        stock = StockSnapshot(
            code="600000",
            name="浦发银行",
            price=10.8,
            change_pct=2.0,
            amount=100000000,
            turnover_rate=2.0,
            industry="银行",
        )

        advice = advise_position(position, stock, "主升候选")

        self.assertEqual(advice.action, "持有")
        self.assertIn("10.26", advice.stop_loss)
        self.assertIn("11.88", advice.take_profit)
        self.assertIn("主升", advice.add_condition)
        self.assertIn("退出主升", advice.clear_condition)
        self.assertNotIn("继续处于主升候选", advice.clear_condition)


if __name__ == "__main__":
    unittest.main()
