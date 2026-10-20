from pathlib import Path
import tempfile
import unittest

from personal_quant.data_center.readiness import check_inputs, render_readiness_report


class ReadinessTests(unittest.TestCase):
    def test_missing_positions_is_not_ready(self):
        result = check_inputs(Path("missing_positions.csv"))

        self.assertFalse(result.ready)
        self.assertIn("POSITIONS_FILE_MISSING", result.errors)

    def test_example_positions_is_not_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "positions.example.csv"
            path.write_text(
                "code,name,quantity,cost_price,note\n"
                "300454,深信服,100,115.00,示例持仓请复制后替换\n",
                encoding="utf-8",
            )

            result = check_inputs(path)

            self.assertFalse(result.ready)
            self.assertIn("POSITIONS_FILE_IS_EXAMPLE", result.errors)

    def test_valid_positions_is_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "positions.csv"
            path.write_text(
                "code,name,quantity,cost_price,buy_date\n"
                "600000,浦发银行,100,10.5,2026-07-01\n",
                encoding="utf-8",
            )

            result = check_inputs(path)

            self.assertTrue(result.ready)
            self.assertEqual(result.position_count, 1)

    def test_report_contains_missing_reason(self):
        result = check_inputs(Path("missing_positions.csv"))

        report = render_readiness_report(result)

        self.assertIn("## 输入就绪检查", report)
        self.assertIn("POSITIONS_FILE_MISSING", report)
        self.assertIn("不能进入严格完整输出", report)
        self.assertIn("## 持仓文件要求", report)
        self.assertIn("code、name、quantity、cost_price", report)
        self.assertIn("buy_date、available_quantity、position_value、note", report)
        self.assertIn("positions.example.csv", report)
        self.assertIn("不能直接用于正式报告", report)
        self.assertIn("## 修复动作", report)
        self.assertIn("创建或更新真实持仓文件", report)
        self.assertIn("data/positions.csv", report)
        self.assertIn("## 验收命令", report)
        self.assertIn("check-inputs", report)
        self.assertIn("--require-positions", report)

    def test_can_skip_positions_for_current_scope(self):
        result = check_inputs(Path("missing_positions.csv"), focus_positions=False)

        report = render_readiness_report(result)

        self.assertTrue(result.ready)
        self.assertEqual(result.errors, [])
        self.assertIn("暂不关注持仓个股", report)
        self.assertIn("可以进入严格完整输出", report)
        self.assertNotIn("POSITIONS_FILE_MISSING", report)


if __name__ == "__main__":
    unittest.main()
