import datetime as dt
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from personal_quant.report_builder.manifest import EXPECTED_REPORTS, build_manifest_report, write_manifest_report


class ManifestTests(unittest.TestCase):
    def test_manifest_lists_expected_daily_outputs(self):
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            for report in EXPECTED_REPORTS:
                (output_dir / f"{report.prefix}_2026-07-09.md").write_text("# ok\n", encoding="utf-8")

            report = build_manifest_report(
                dt.date(2026, 7, 9),
                output_dir,
                strict_ready=False,
                readiness_errors=["POSITIONS_FILE_MISSING"],
            )

            self.assertIn("## 每日输出清单", report)
            self.assertIn("daily_pack_2026-07-09.md", report)
            self.assertIn("push_preview_2026-07-09.md", report)
            self.assertIn("push_contract_2026-07-09.md", report)
            self.assertIn("readiness_2026-07-09.md", report)
            self.assertIn("intraday_2026-07-09.md", report)
            self.assertIn("positions_2026-07-09.md", report)
            self.assertIn("严格完整：否", report)
            self.assertIn("POSITIONS_FILE_MISSING", report)

    def test_manifest_marks_missing_file(self):
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            (output_dir / "daily_pack_2026-07-09.md").write_text("# ok\n", encoding="utf-8")

            report = build_manifest_report(
                dt.date(2026, 7, 9),
                output_dir,
                strict_ready=False,
                readiness_errors=[],
            )

            self.assertIn("缺失", report)

    def test_write_manifest_report_creates_file(self):
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp)

            path = write_manifest_report(
                dt.date(2026, 7, 9),
                output_dir,
                strict_ready=True,
                readiness_errors=[],
            )

            self.assertEqual(path.name, "manifest_2026-07-09.md")
            self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
