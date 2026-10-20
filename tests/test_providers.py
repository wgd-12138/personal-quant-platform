import datetime as dt
import unittest

from personal_quant.data_center.providers import EXTERNAL_SYMBOLS, _parse_board_row, _parse_sina_us_bars, fetch_external_snapshots


class ProviderTests(unittest.TestCase):
    def test_external_symbols_include_contract_tech_names(self):
        self.assertIn("博通", EXTERNAL_SYMBOLS)
        self.assertEqual(EXTERNAL_SYMBOLS["博通"], "AVGO")
        self.assertNotIn("三星电子", EXTERNAL_SYMBOLS)
        self.assertNotIn("SK海力士", EXTERNAL_SYMBOLS)

    def test_parse_sina_us_bars(self):
        payload = (
            '[{"d":"2026-04-30","c":"100.00"},'
            '{"d":"2026-05-01","c":"110.00"},'
            '{"d":"2026-07-08","c":"121.00"}]'
        )

        bars = _parse_sina_us_bars(payload)

        self.assertEqual(bars[0], {"date": dt.date(2026, 4, 30), "close": 100.0})
        self.assertEqual(bars[-1], {"date": dt.date(2026, 7, 8), "close": 121.0})

    def test_fetch_external_snapshots_uses_sina_symbols(self):
        calls = []

        def fake_fetch(symbol, start, end):
            calls.append((symbol, start, end))
            return [
                {"date": dt.date(2026, 4, 30), "close": 100.0},
                {"date": dt.date(2026, 5, 1), "close": 110.0},
                {"date": dt.date(2026, 6, 30), "close": 120.0},
                {"date": dt.date(2026, 7, 1), "close": 125.0},
                {"date": dt.date(2026, 7, 8), "close": 130.0},
            ]

        snapshots = fetch_external_snapshots(dt.date(2026, 7, 9), fetch_bars=fake_fetch)

        self.assertEqual(len(snapshots), len(EXTERNAL_SYMBOLS))
        self.assertEqual(calls[0][1], dt.date(2026, 5, 1))
        self.assertEqual(calls[0][2], dt.date(2026, 7, 9))
        self.assertTrue(all(item.symbol in EXTERNAL_SYMBOLS.values() for item in snapshots))

    def test_board_row_maps_20_day_and_60_day_returns(self):
        row = {
            "f12": "BK1325",
            "f14": "半导体材料",
            "f3": 9.69,
            "f6": 75571208832.0,
            "f10": 0.98,
            "f24": 108.44,
            "f62": 2937654272.0,
            "f104": 31,
            "f105": 0,
            "f109": 0.29,
            "f110": 36.52,
        }

        board = _parse_board_row(row, "industry")

        self.assertIsNotNone(board)
        self.assertEqual(board.twenty_day_return, 36.52)
        self.assertEqual(board.sixty_day_return, 108.44)


if __name__ == "__main__":
    unittest.main()
