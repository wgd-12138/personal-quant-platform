from pathlib import Path
import os
import tempfile
import unittest

from personal_quant.data_center.positions import load_positions
from personal_quant.data_center.positions_importer import import_latest_positions_export, import_positions_csv


class PositionsImporterTests(unittest.TestCase):
    def test_imports_eastmoney_export_csv_to_standard_positions(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "eastmoney_positions.csv"
            output = Path(tmp) / "positions.csv"
            source.write_text(
                "证券代码,证券名称,持仓数量,成本价,可用数量,持仓市值\n"
                "600000,浦发银行,1000,10.50,800,10800\n",
                encoding="utf-8",
            )

            import_positions_csv(source, output)
            positions = load_positions(output)

            self.assertEqual(positions[0].code, "600000")
            self.assertEqual(positions[0].name, "浦发银行")
            self.assertEqual(positions[0].quantity, 1000)
            self.assertEqual(positions[0].cost_price, 10.5)
            self.assertEqual(positions[0].available_quantity, 800)
            self.assertEqual(positions[0].position_value, 10800)

    def test_imports_csv_with_header_spaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "eastmoney_positions.csv"
            output = Path(tmp) / "positions.csv"
            source.write_text(
                " 证券代码 , 证券名称 , 持仓数量 , 成本价 \n"
                "000001,平安银行,200,12.30\n",
                encoding="utf-8",
            )

            import_positions_csv(source, output)
            positions = load_positions(output)

            self.assertEqual(positions[0].code, "000001")
            self.assertEqual(positions[0].name, "平安银行")

    def test_missing_required_source_column_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "bad_positions.csv"
            output = Path(tmp) / "positions.csv"
            source.write_text(
                "证券代码,证券名称,持仓数量\n"
                "600000,浦发银行,1000\n",
                encoding="utf-8",
            )

            with self.assertRaises(ValueError) as exc:
                import_positions_csv(source, output)

            self.assertIn("缺少持仓字段", str(exc.exception))

    def test_imports_latest_export_from_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_dir = Path(tmp) / "exports"
            source_dir.mkdir()
            output = Path(tmp) / "positions.csv"
            older = source_dir / "older.csv"
            latest = source_dir / "latest.csv"
            older.write_text(
                "证券代码,证券名称,持仓数量,成本价\n"
                "600000,浦发银行,100,10.50\n",
                encoding="utf-8",
            )
            latest.write_text(
                "证券代码,证券名称,持仓数量,成本价\n"
                "000001,平安银行,200,12.30\n",
                encoding="utf-8",
            )
            older_mtime = older.stat().st_mtime - 10
            latest_mtime = older.stat().st_mtime + 10
            os.utime(older, (older_mtime, older_mtime))
            os.utime(latest, (latest_mtime, latest_mtime))

            result = import_latest_positions_export(source_dir, output)
            positions = load_positions(output)

            self.assertEqual(result, latest)
            self.assertEqual(positions[0].code, "000001")
            self.assertEqual(positions[0].name, "平安银行")


if __name__ == "__main__":
    unittest.main()
