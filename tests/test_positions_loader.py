from pathlib import Path
import tempfile
import unittest

from personal_quant.data_center.positions import load_positions


class PositionsLoaderTests(unittest.TestCase):
    def test_loads_valid_positions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "positions.csv"
            path.write_text(
                "code,name,quantity,cost_price,buy_date\n"
                "600000,浦发银行,100,10.5,2026-07-01\n",
                encoding="utf-8",
            )

            positions = load_positions(path)

            self.assertEqual(positions[0].code, "600000")
            self.assertEqual(positions[0].name, "浦发银行")
            self.assertEqual(positions[0].quantity, 100)
            self.assertEqual(positions[0].cost_price, 10.5)

    def test_missing_required_column_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "positions.csv"
            path.write_text("code,name,quantity\n600000,浦发银行,100\n", encoding="utf-8")

            with self.assertRaises(ValueError):
                load_positions(path)

    def test_non_positive_values_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "positions.csv"
            path.write_text(
                "code,name,quantity,cost_price\n600000,浦发银行,0,10.5\n",
                encoding="utf-8",
            )

            with self.assertRaises(ValueError):
                load_positions(path)

    def test_example_positions_file_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "positions.example.csv"
            path.write_text(
                "code,name,quantity,cost_price,note\n"
                "300454,深信服,100,115.00,示例持仓请复制后替换\n",
                encoding="utf-8",
            )

            with self.assertRaises(ValueError) as exc:
                load_positions(path)

            self.assertIn("POSITIONS_FILE_IS_EXAMPLE", str(exc.exception))

    def test_example_marker_in_positions_file_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "positions.csv"
            path.write_text(
                "code,name,quantity,cost_price,note\n"
                "300454,深信服,100,115.00,示例持仓请复制后替换\n",
                encoding="utf-8",
            )

            with self.assertRaises(ValueError) as exc:
                load_positions(path)

            self.assertIn("POSITIONS_FILE_IS_EXAMPLE", str(exc.exception))


if __name__ == "__main__":
    unittest.main()
