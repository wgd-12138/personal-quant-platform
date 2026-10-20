import unittest

from personal_quant.report_builder.audit import ReportAuditError, audit_daily_report


class ReportAuditTests(unittest.TestCase):
    def test_audit_accepts_all_sections_when_positions_not_required(self):
        report = self._report("未提供真实持仓文件，不能生成逐股止盈、止损、减仓、补仓、清仓建议。")

        audit = audit_daily_report(report, require_positions=False)

        self.assertTrue(audit.passed)
        self.assertEqual(audit.failures, [])

    def test_audit_rejects_missing_positions_when_required(self):
        report = self._report("未提供真实持仓文件，不能生成逐股止盈、止损、减仓、补仓、清仓建议。")

        with self.assertRaises(ReportAuditError):
            audit_daily_report(report, require_positions=True)

    def test_audit_rejects_missing_section(self):
        report = self._report("已按真实持仓逐股输出。").replace("## 风险提示\n", "")

        with self.assertRaises(ReportAuditError):
            audit_daily_report(report, require_positions=False)

    def _report(self, position_text: str) -> str:
        return "\n".join(
            [
                "# 每日推送完整报告",
                "## 数据质量",
                "## 市场状态",
                "## 主线判断",
                "## 当天热门板块",
                "## 外围市场",
                "## 消息驱动分析",
                "## 早盘走势推演",
                "## 早盘建仓推荐",
                "## 尾盘隔夜套利",
                "## 今日操作建议",
                "## 持仓操作建议",
                position_text,
                "## 建仓候选",
                "## 利好利空信息",
                "## 晚间复盘",
                "## 次日操作计划",
                "## 风险提示",
                "",
            ]
        )


if __name__ == "__main__":
    unittest.main()
