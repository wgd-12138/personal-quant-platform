import datetime as dt
import unittest

from personal_quant.data_center.models import BoardSnapshot, ExternalSnapshot, IndexSnapshot, MarketDataset, NewsItem, StockSnapshot
from personal_quant.data_center.positions import PositionSnapshot
from personal_quant.market_analysis.classifier import summarize_market
from personal_quant.report_builder.audit import audit_daily_report
from personal_quant.report_builder.daily_pack import REQUIRED_SECTIONS, build_daily_pack_report, build_daily_report_sections


class DailyPackReportTests(unittest.TestCase):
    def test_required_sections_are_fixed(self):
        self.assertEqual(
            REQUIRED_SECTIONS,
            [
                "数据质量",
                "市场状态",
                "主线判断",
                "当天热门板块",
                "外围市场",
                "消息驱动分析",
                "早盘走势推演",
                "早盘建仓推荐",
                "尾盘隔夜套利",
                "今日操作建议",
                "持仓操作建议",
                "建仓候选",
                "利好利空信息",
                "晚间复盘",
                "次日操作计划",
                "风险提示",
            ],
        )

    def test_built_report_passes_its_own_audit(self):
        # 回归：构建器新增章节后审计必须仍然通过，防止 REQUIRED_SECTIONS 与构建器脱节
        summary = summarize_market(self._dataset_without_mainline())

        report = build_daily_pack_report(summary, positions=[], news_items=[])

        audit = audit_daily_report(report, require_positions=False)

        self.assertTrue(audit.passed)
        self.assertEqual(audit.failures, [])

    def test_report_contains_every_push_section_without_fake_positions(self):
        summary = summarize_market(self._dataset_without_mainline())

        report = build_daily_pack_report(summary, positions=[], news_items=[])

        for section in REQUIRED_SECTIONS:
            self.assertIn(f"## {section}", report)
        for required_text in [
            "数据：不完整",
            "时间 11:30",
            "失败项",
            "上证指数",
            "全市场成交",
            "上涨家数",
            "下跌家数",
            "涨停数量",
            "跌停数量",
            "炸板率",
            "20日涨幅",
            "成交变化",
        ]:
            self.assertIn(required_text, report)
        self.assertIn("未提供真实持仓文件", report)
        self.assertIn("数据：不完整", report)
        self.assertIn("失败项：POSITIONS_FILE_MISSING", report)
        self.assertNotIn("失败项：暂无", report)
        self.assertIn("暂无满足主升条件的建仓候选", report)
        self.assertIn("高开", report)
        self.assertIn("低开", report)
        self.assertIn("震荡", report)
        self.assertIn("破位", report)

    def test_report_can_ignore_positions_for_current_scope(self):
        summary = summarize_market(self._dataset_without_mainline())

        sections = build_daily_report_sections(summary, positions=[], news_items=[], focus_positions=False)

        self.assertIn("数据：完整", sections["daily_pack"])
        self.assertIn("失败项：暂无", sections["daily_pack"])
        self.assertIn("本阶段暂不关注持仓个股", sections["positions"])
        self.assertIn("严格完整：是", sections["push_contract"])
        self.assertIn("缺口：暂无", sections["push_contract"])
        self.assertIn("本阶段跳过", sections["push_contract"])
        self.assertNotIn("POSITIONS_FILE_MISSING", sections["push_contract"])
        self.assertNotIn("缺真实持仓", sections["push_contract"])
        self.assertNotIn("缺真实持仓", sections["push_preview"])
        self.assertNotIn("未提供真实持仓", sections["evening"])
        self.assertIn("本阶段暂不关注持仓个股", sections["evening"])

    def test_daily_pack_and_push_preview_are_personal_brief(self):
        summary = summarize_market(self._dataset_without_mainline())

        sections = build_daily_report_sections(summary, positions=[], news_items=[], focus_positions=False)
        report = sections["daily_pack"]
        preview = sections["push_preview"]

        for section in REQUIRED_SECTIONS:
            self.assertIn(f"## {section}", report)
        self.assertIn("## 今日结论", report)
        self.assertNotIn("| 指数 | 点位 | 涨跌幅 | 成交额 |", report)
        self.assertNotIn("| 全市场成交 | 上涨家数 | 下跌家数 |", report)
        self.assertNotIn("## 资金强弱", report)
        self.assertNotIn("## 资金状态", report)
        self.assertIn("盘中实时盘面", preview)
        self.assertNotIn("| 指数 | 点位 | 涨跌幅 | 成交额 |", preview)
        self.assertLess(len(report), len(sections["intraday"]) + len(sections["evening"]))

    def test_personal_daily_pack_keeps_only_decision_content(self):
        summary = summarize_market(self._dataset_without_mainline())

        sections = build_daily_report_sections(summary, positions=[], news_items=[], focus_positions=False)
        report = sections["daily_pack"]

        self.assertIn("- 数据：完整", report)
        self.assertIn("时间 11:30", report)
        self.assertIn("- 盘面：", report)
        self.assertIn("- 主线：", report)
        self.assertIn("- 策略：", report)
        self.assertIn("- 次日：", report)
        self.assertNotIn("数据源：", report)
        self.assertNotIn("数据时间：", report)
        self.assertNotIn("## 明日关注", report)
        self.assertNotIn("## 持仓次日处理", report)

    def test_push_preview_is_user_message_without_delivery_metadata(self):
        summary = summarize_market(self._dataset_without_mainline())

        sections = build_daily_report_sections(summary, positions=[], news_items=[], focus_positions=False)
        preview = sections["push_preview"]

        self.assertIn("## 盘中实时盘面", preview)
        self.assertIn("## 持仓个股操作", preview)
        self.assertNotIn("## 完整性状态", preview)
        self.assertNotIn("触发时间", preview)
        self.assertNotIn("发送状态", preview)
        self.assertNotIn("# 盘中简报", preview)
        self.assertNotIn("# 持仓简报", preview)
        self.assertNotIn("## 晚间复盘\n## 晚间复盘", preview)
        self.assertNotIn("## 风险提示\n## 风险提示", preview)

    def test_missing_position_report_contains_input_requirements(self):
        summary = summarize_market(self._dataset_without_mainline())

        sections = build_daily_report_sections(summary, positions=[], news_items=[])
        positions_report = sections["positions"]

        self.assertIn("## 持仓输入要求", positions_report)
        self.assertIn("data/positions.csv", positions_report)
        self.assertIn("code", positions_report)
        self.assertIn("name", positions_report)
        self.assertIn("quantity", positions_report)
        self.assertIn("cost_price", positions_report)
        self.assertIn("buy_date", positions_report)
        self.assertIn("available_quantity", positions_report)
        self.assertIn("position_value", positions_report)
        self.assertIn("note", positions_report)
        self.assertIn("positions.example.csv", positions_report)
        self.assertIn("不能直接用于正式报告", positions_report)
        self.assertIn("check-inputs", positions_report)
        self.assertIn("## 持仓补齐后输出", positions_report)
        self.assertIn("止盈", positions_report)
        self.assertIn("止损", positions_report)
        self.assertIn("减仓", positions_report)
        self.assertIn("补仓", positions_report)
        self.assertIn("清仓", positions_report)
        self.assertIn("利好利空", positions_report)

    def test_report_contains_position_advice_and_matched_news(self):
        dataset = self._dataset_with_mainline()
        summary = summarize_market(dataset)
        position = PositionSnapshot("600000", "浦发银行", 100, 10.0, "2026-07-01")
        news = NewsItem(
            source="东方财富快讯",
            published_at=dt.datetime(2026, 7, 9, 10, 0),
            title="银行板块走强 浦发银行成交放大",
            summary="银行板块继续走强，浦发银行成交放大。",
            related_names=("浦发银行", "银行"),
            url="",
        )

        report = build_daily_pack_report(summary, positions=[position], news_items=[news])

        self.assertIn("浦发银行", report)
        self.assertIn("持有", report)
        self.assertIn("仓位调整建议", report)
        self.assertIn("银行板块走强", report)
        self.assertIn("主升候选", report)
        self.assertIn("数据：完整", report)
        self.assertIn("失败项：暂无", report)

    def test_position_section_contains_matched_news(self):
        dataset = self._dataset_with_mainline()
        summary = summarize_market(dataset)
        position = PositionSnapshot("600000", "浦发银行", 100, 10.0, "2026-07-01")
        news = NewsItem(
            source="东方财富快讯",
            published_at=dt.datetime(2026, 7, 9, 10, 0),
            title="浦发银行成交放大",
            summary="浦发银行成交放大。",
            related_names=("浦发银行",),
            url="",
        )

        sections = build_daily_report_sections(summary, positions=[position], news_items=[news])

        self.assertIn("利好利空", sections["positions"])
        self.assertIn("浦发银行成交放大", sections["positions"])
        self.assertIn("东方财富快讯", sections["positions"])
        self.assertIn("2026-07-09 10:00", sections["positions"])
        self.assertIn("无法判断", sections["positions"])
        self.assertIn("匹配依据：标题包含：浦发银行", sections["positions"])

    def test_report_matches_news_for_watchlist_candidates(self):
        dataset = self._dataset_with_candidate_name_not_overlapping_board()
        summary = summarize_market(dataset)
        news = NewsItem(
            source="东方财富快讯",
            published_at=dt.datetime(2026, 7, 9, 10, 30),
            title="埃斯顿成交放大 机构关注趋势核心",
            summary="埃斯顿成交放大，资金继续流入。",
            related_names=("埃斯顿",),
            url="",
        )

        report = build_daily_pack_report(summary, positions=[], news_items=[news])

        self.assertIn("埃斯顿", report)
        self.assertIn("机构关注趋势核心", report)

    def test_news_section_uses_explicit_unknown_impact_direction(self):
        dataset = self._dataset_with_candidate_name_not_overlapping_board()
        summary = summarize_market(dataset)
        news = NewsItem(
            source="东方财富快讯",
            published_at=dt.datetime(2026, 7, 9, 10, 30),
            title="埃斯顿成交放大 机构关注趋势核心",
            summary="埃斯顿成交放大，资金继续流入。",
            related_names=("埃斯顿",),
            url="",
        )

        sections = build_daily_report_sections(summary, positions=[], news_items=[news])

        self.assertIn("影响方向", sections["news"])
        self.assertIn("匹配依据", sections["news"])
        self.assertIn("已匹配 1 条", sections["news"])
        self.assertIn("无法判断", sections["news"])
        self.assertIn("标题包含：埃斯顿", sections["news"])
        self.assertNotIn("初步影响", sections["news"])

    def test_news_section_explains_no_match_reason(self):
        summary = summarize_market(self._dataset_without_mainline())
        news = NewsItem(
            source="东方财富快讯",
            published_at=dt.datetime(2026, 7, 9, 10, 30),
            title="市场成交放大",
            summary="市场成交放大，但未提及持仓、建仓候选或主升板块。",
            related_names=(),
            url="",
        )

        sections = build_daily_report_sections(summary, positions=[], news_items=[news])
        news_report = sections["news"]

        self.assertIn("## 新闻匹配状态", news_report)
        self.assertIn("新闻总数", news_report)
        self.assertIn("1", news_report)
        self.assertIn("匹配对象", news_report)
        self.assertIn("持仓 0 个、主升板块 0 个、建仓候选 0 个", news_report)
        self.assertIn("未匹配原因", news_report)
        self.assertIn("没有新闻同时命中持仓、主升板块或建仓候选", news_report)
        self.assertIn("处理", news_report)
        self.assertIn("不作为交易依据", news_report)

    def test_can_split_daily_push_content_into_named_reports(self):
        summary = summarize_market(self._dataset_without_mainline())

        sections = build_daily_report_sections(summary, positions=[], news_items=[])

        self.assertEqual(
            set(sections),
            {
                "daily_pack",
                "intraday",
                "positions",
                "watchlist",
                "news",
                "morning_build",
                "overnight_arbitrage",
                "evening",
                "push_preview",
                "push_contract",
            },
        )
        self.assertIn("## 市场状态", sections["intraday"])
        self.assertIn("## 持仓操作建议", sections["positions"])
        self.assertIn("## 建仓候选", sections["watchlist"])
        self.assertIn("## 利好利空信息", sections["news"])
        self.assertIn("## 晚间复盘", sections["evening"])
        self.assertIn("## 次日操作计划", sections["evening"])
        self.assertIn("## 风险提示", sections["daily_pack"])

    def test_push_contract_lists_each_required_push_content_and_gap(self):
        summary = summarize_market(self._dataset_without_mainline())

        sections = build_daily_report_sections(summary, positions=[], news_items=[])
        contract = sections["push_contract"]

        self.assertIn("# 推送内容验收清单", contract)
        self.assertIn("严格完整：否", contract)
        self.assertIn("POSITIONS_FILE_MISSING", contract)
        self.assertNotIn("FIELD_MISSING:盘中实时盘面:20日涨幅", contract)
        for title in [
            "盘中实时盘面",
            "持仓个股操作",
            "建仓候选推荐",
            "利好利空捕捉",
            "晚间复盘",
            "次日操作计划",
            "风险提示",
        ]:
            self.assertIn(title, contract)
        self.assertIn("证据文件", contract)
        self.assertIn("positions_2026-07-09.md", contract)
        self.assertIn("20日涨幅", contract)
        self.assertIn("缺真实持仓，不能完整发送", contract)
        self.assertIn("暂无主升时输出暂无，不硬推", contract)
        self.assertIn("高开、低开、震荡、破位", contract)

    def test_intraday_report_contains_fund_strength(self):
        summary = summarize_market(self._dataset_with_failed_directions())

        sections = build_daily_report_sections(summary, positions=[], news_items=[])
        intraday = sections["intraday"]

        self.assertIn("## 资金强弱", intraday)
        self.assertIn("流入靠前", intraday)
        self.assertIn("流出靠前", intraday)
        self.assertIn("主力净流入", intraday)
        self.assertIn("低位修复", intraday)
        self.assertIn("高位科技", intraday)

    def test_intraday_report_contains_mainline_judgement(self):
        summary = summarize_market(self._dataset_with_failed_directions())

        sections = build_daily_report_sections(summary, positions=[], news_items=[])
        intraday = sections["intraday"]

        self.assertIn("## 主线判断", intraday)
        self.assertIn("主升候选", intraday)
        self.assertIn("反弹候选", intraday)
        self.assertIn("高位兑现", intraday)
        self.assertIn("一日游风险", intraday)
        self.assertIn("无清晰主线", intraday)
        self.assertIn("暂无", intraday)
        self.assertIn("低位修复", intraday)
        self.assertIn("高位科技", intraday)
        self.assertIn("短线脉冲", intraday)
        self.assertIn("前期涨幅过大，短期强度不足", intraday)
        self.assertIn("当日强但周期强度不足", intraday)

    def test_intraday_report_contains_external_resonance(self):
        summary = summarize_market(self._dataset_with_failed_directions())

        sections = build_daily_report_sections(summary, positions=[], news_items=[])
        intraday = sections["intraday"]

        self.assertIn("## 外围共振", intraday)
        self.assertIn("外围科技正向", intraday)
        self.assertIn("0/1", intraday)
        self.assertIn("外围科技负向", intraday)
        self.assertIn("1/1", intraday)
        self.assertIn("A股科技状态", intraday)
        self.assertIn("高位科技", intraday)
        self.assertIn("外围偏弱，A股科技不按主升处理", intraday)

    def test_intraday_report_contains_news_driven_cause_analysis(self):
        summary = summarize_market(self._dataset_with_space_and_tech_pressure())
        news = [
            NewsItem(
                source="政策新闻",
                published_at=dt.datetime(2026, 7, 9, 9, 10),
                title="商业航天产业政策利好 商业航天订单增长",
                summary="政策支持商业航天产业链发展，订单和发射需求增长。",
                related_names=("商业航天",),
                url="https://example.test/news/space",
            )
        ]

        sections = build_daily_report_sections(summary, positions=[], news_items=news)
        intraday = sections["intraday"]
        preview = sections["push_preview"]

        self.assertIn("## 消息驱动分析", intraday)
        self.assertIn("商业航天", intraday)
        self.assertIn("政策新闻", intraday)
        self.assertIn("可能影响", intraday)
        self.assertIn("科技风格", intraday)
        self.assertIn("暂无明确消息证据", intraday)
        self.assertIn("## 早盘走势推演", intraday)
        self.assertIn("外围表现", intraday)
        self.assertNotIn("消息影响：商业航天", intraday)
        self.assertNotIn("消息影响：利好 政策新闻", intraday)
        self.assertIn("高开", intraday)
        self.assertIn("低开", intraday)
        self.assertIn("震荡", intraday)
        self.assertIn("消息驱动分析", preview)
        self.assertIn("早盘走势推演", preview)

    def test_morning_news_effect_is_hidden_without_impacted_board(self):
        summary = summarize_market(self._dataset_with_space_and_tech_pressure())
        news = [
            NewsItem(
                source="东方财富快讯",
                published_at=dt.datetime(2026, 7, 9, 9, 10),
                title="智飞生物：控股子公司利拉鲁肽注射液获药品注册证书",
                summary="公司提示投资者注意市场风险。",
                related_names=("90.BK0465", "0.300122"),
                url="https://example.test/news/zf",
            )
        ]

        sections = build_daily_report_sections(summary, positions=[], news_items=news)
        intraday = sections["intraday"]

        self.assertNotIn("消息影响：", intraday)
        self.assertNotIn("智飞生物：控股子公司利拉鲁肽注射液获药品注册证书", intraday)

    def test_morning_news_effect_is_hidden_when_direction_is_unclear(self):
        summary = summarize_market(self._dataset_with_space_and_tech_pressure())
        news = [
            NewsItem(
                source="东方财富快讯",
                published_at=dt.datetime(2026, 7, 9, 9, 10),
                title="商业航天产业会议召开",
                summary="会议讨论商业航天产业发展情况。",
                related_names=("商业航天",),
                url="https://example.test/news/space-meeting",
            )
        ]

        sections = build_daily_report_sections(summary, positions=[], news_items=news)
        intraday = sections["intraday"]

        self.assertNotIn("消息影响：", intraday)

    def test_positive_news_impact_requires_suitable_entry_and_link(self):
        summary = summarize_market(self._dataset_with_mainline())
        news = [
            NewsItem(
                source="政策新闻",
                published_at=dt.datetime(2026, 7, 9, 9, 10),
                title="银行政策利好 浦发银行成交放大",
                summary="政策支持银行板块，浦发银行成交放大。",
                related_names=("银行", "浦发银行"),
                url="https://example.test/news/bank",
            )
        ]

        sections = build_daily_report_sections(summary, positions=[], news_items=news)
        intraday = sections["intraday"]

        self.assertIn("消息影响：银行：偏利好", intraday)
        self.assertIn("原因：政策支持", intraday)
        self.assertIn("建仓观察：浦发银行(600000)", intraday)
        self.assertIn("高位判断：不在高位", intraday)
        self.assertIn("适合建仓观察", intraday)
        self.assertIn("链接：https://example.test/news/bank", intraday)
        self.assertNotIn("银行政策利好 浦发银行成交放大", intraday.split("## 早盘走势推演", 1)[1])

    def test_positive_news_impact_is_hidden_when_stock_is_high(self):
        summary = summarize_market(self._dataset_with_mainline_high_stock())
        news = [
            NewsItem(
                source="政策新闻",
                published_at=dt.datetime(2026, 7, 9, 9, 10),
                title="银行政策利好 浦发银行成交放大",
                summary="政策支持银行板块，浦发银行成交放大。",
                related_names=("银行", "浦发银行"),
                url="https://example.test/news/bank",
            )
        ]

        sections = build_daily_report_sections(summary, positions=[], news_items=news)
        intraday = sections["intraday"]

        self.assertNotIn("消息影响：银行", intraday)

    def test_push_preview_makes_every_future_push_message_visible(self):
        summary = summarize_market(self._dataset_without_mainline())

        sections = build_daily_report_sections(summary, positions=[], news_items=[])
        preview = sections["push_preview"]

        for title in [
            "盘中实时盘面",
            "持仓个股操作",
            "建仓候选推荐",
            "利好利空捕捉",
            "晚间复盘",
            "次日操作计划",
            "风险提示",
        ]:
            self.assertIn(title, preview)
        self.assertIn("## 风险提示", preview)
        self.assertNotIn("发送状态", preview)
        self.assertNotIn("触发时间", preview)
        self.assertIn("POSITIONS_FILE_MISSING", preview)
        self.assertIn("未提供真实持仓文件", preview)
        self.assertIn("暂无满足主升条件的建仓候选", preview)
        self.assertIn("高开", preview)
        self.assertIn("破位", preview)

    def test_risk_section_contains_actionable_boundaries(self):
        summary = summarize_market(self._dataset_with_failed_directions())

        sections = build_daily_report_sections(summary, positions=[], news_items=[])
        evening = sections["evening"]

        self.assertIn("## 风险提示", evening)
        self.assertIn("| 风险项 | 当前状态 | 处理 |", evening)
        self.assertIn("持仓风险", evening)
        self.assertIn("未提供真实持仓", evening)
        self.assertIn("主线风险", evening)
        self.assertIn("无主升候选", evening)
        self.assertIn("暂停新增建仓", evening)
        self.assertIn("退潮风险", evening)
        self.assertIn("高位科技", evening)
        self.assertIn("短线脉冲", evening)
        self.assertIn("回避高位兑现和一日游方向", evening)
        self.assertIn("事件风险", evening)
        self.assertIn("解禁", evening)
        self.assertIn("减持", evening)
        self.assertIn("业绩兑现", evening)
        self.assertIn("龙虎榜", evening)
        self.assertIn("外围风险", evening)
        self.assertIn("外围背离", evening)
        self.assertIn("交易风险", evening)
        self.assertIn("不会自动下单", evening)

    def test_evening_report_contains_failed_directions_and_next_day_focus(self):
        summary = summarize_market(self._dataset_with_failed_directions())

        sections = build_daily_report_sections(summary, positions=[], news_items=[])
        evening = sections["evening"]

        self.assertIn("## 失败方向", evening)
        self.assertIn("## 资金状态", evening)
        self.assertIn("主力净流入", evening)
        self.assertIn("高位科技", evening)
        self.assertIn("前期涨幅过大，短期强度不足", evening)
        self.assertIn("短线脉冲", evening)
        self.assertIn("当日强但周期强度不足", evening)
        self.assertIn("## 明日关注", evening)
        self.assertIn("关键板块", evening)
        self.assertIn("关键风险", evening)
        self.assertIn("关键动作", evening)

    def test_next_day_focus_contains_key_stocks_and_key_prices(self):
        summary = summarize_market(self._dataset_with_mainline())

        sections = build_daily_report_sections(summary, positions=[], news_items=[])
        evening = sections["evening"]

        self.assertIn("关键个股", evening)
        self.assertIn("浦发银行", evening)
        self.assertIn("关键价位", evening)
        self.assertIn("10.90", evening)
        self.assertIn("10.59", evening)

    def test_watchlist_candidates_contain_reason_price_trigger_and_risk_boundary(self):
        summary = summarize_market(self._dataset_with_mainline())

        sections = build_daily_report_sections(summary, positions=[], news_items=[])
        watchlist = sections["watchlist"]

        self.assertIn("选择原因", watchlist)
        self.assertIn("风险提示", watchlist)
        self.assertIn("浦发银行", watchlist)
        self.assertIn("板块：短期强度高，资金为正，板块有扩散", watchlist)
        self.assertIn("个股涨幅 2.00%", watchlist)
        self.assertIn("突破今日高点 10.90", watchlist)
        self.assertIn("回踩不破前收 10.59", watchlist)
        self.assertIn("未接入解禁、减持、业绩兑现、龙虎榜数据", watchlist)

    def test_evening_report_contains_position_next_day_plan(self):
        dataset = self._dataset_with_mainline()
        summary = summarize_market(dataset)
        position = PositionSnapshot("600000", "浦发银行", 100, 10.0, "2026-07-01")

        sections = build_daily_report_sections(summary, positions=[position], news_items=[])
        evening = sections["evening"]

        self.assertIn("## 持仓次日处理", evening)
        self.assertIn("浦发银行", evening)
        self.assertIn("开盘", evening)
        self.assertIn("冲高", evening)
        self.assertIn("下杀", evening)
        self.assertIn("破位", evening)
        self.assertIn("10.26", evening)
        self.assertIn("11.88", evening)

    def _dataset_without_mainline(self) -> MarketDataset:
        return MarketDataset(
            as_of=dt.date(2026, 7, 9),
            boards=[
                BoardSnapshot(
                    code="BK0001",
                    name="低位反弹",
                    board_type="行业",
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
                ),
                ExternalSnapshot(
                    name="博通",
                    symbol="AVGO",
                    may_june_return=-10.0,
                    july_return=2.0,
                )
            ],
            indexes=[
                IndexSnapshot("000001", "上证指数", 3952.49, -0.46, -18.39, 790773118087.2),
            ],
            stocks=[
                StockSnapshot("600000", "浦发银行", 10.0, 1.0, 100000000, 1.0, "银行", 10.0, 9.8, 9.9),
                StockSnapshot("000001", "平安银行", 11.0, -1.0, 200000000, 1.2, "银行", 11.2, 10.8, 11.1),
                StockSnapshot("300001", "特锐德", 24.0, 20.0, 300000000, 4.0, "电气设备", 24.0, 20.0, 20.0),
                StockSnapshot("600001", "邯郸钢铁", 9.0, -10.0, 100000000, 2.0, "钢铁", 10.0, 9.0, 10.0),
                StockSnapshot("600002", "齐鲁石化", 10.5, 5.0, 100000000, 2.0, "化工", 11.0, 10.0, 10.0),
            ],
            data_sources=("东方财富板块", "东方财富个股", "Yahoo Finance", "东方财富快讯"),
            fetched_at=dt.datetime(2026, 7, 9, 11, 30),
        )

    def _dataset_with_failed_directions(self) -> MarketDataset:
        return MarketDataset(
            as_of=dt.date(2026, 7, 9),
            boards=[
                BoardSnapshot(
                    code="BK9001",
                    name="高位科技",
                    board_type="概念",
                    change_pct=-3.0,
                    amount=3000000000,
                    main_net_inflow=-200000000,
                    five_day_return=4.0,
                    sixty_day_return=48.0,
                    up_count=2,
                    down_count=28,
                ),
                BoardSnapshot(
                    code="BK9002",
                    name="短线脉冲",
                    board_type="概念",
                    change_pct=4.6,
                    amount=1200000000,
                    main_net_inflow=1000000,
                    five_day_return=2.0,
                    sixty_day_return=1.0,
                    up_count=12,
                    down_count=8,
                ),
                BoardSnapshot(
                    code="BK9003",
                    name="低位修复",
                    board_type="行业",
                    change_pct=2.8,
                    amount=1000000000,
                    main_net_inflow=12000000,
                    five_day_return=3.5,
                    sixty_day_return=-9.0,
                    up_count=16,
                    down_count=4,
                ),
            ],
            externals=[
                ExternalSnapshot(
                    name="纳斯达克",
                    symbol="^IXIC",
                    may_june_return=4.0,
                    july_return=-2.0,
                )
            ],
            indexes=[
                IndexSnapshot("000001", "上证指数", 3952.49, -0.46, -18.39, 790773118087.2),
            ],
            stocks=[
                StockSnapshot("600000", "浦发银行", 10.0, 1.0, 100000000, 1.0, "低位修复", 10.0, 9.8, 9.9),
            ],
            data_sources=("东方财富板块", "东方财富个股", "Yahoo Finance", "东方财富快讯"),
            fetched_at=dt.datetime(2026, 7, 9, 11, 30),
        )

    def _dataset_with_mainline(self) -> MarketDataset:
        return MarketDataset(
            as_of=dt.date(2026, 7, 9),
            boards=[
                BoardSnapshot(
                    code="BK0002",
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
            externals=[
                ExternalSnapshot(
                    name="纳斯达克",
                    symbol="^IXIC",
                    may_june_return=4.0,
                    july_return=2.0,
                )
            ],
            indexes=[
                IndexSnapshot("000001", "上证指数", 3952.49, -0.46, -18.39, 790773118087.2),
            ],
            stocks=[
                StockSnapshot(
                    code="600000",
                    name="浦发银行",
                    price=10.8,
                    change_pct=2.0,
                    amount=100000000,
                    turnover_rate=2.0,
                    industry="银行",
                    high=10.9,
                    low=10.5,
                    previous_close=10.59,
                )
            ],
            data_sources=("东方财富板块", "东方财富个股", "Yahoo Finance", "东方财富快讯"),
            fetched_at=dt.datetime(2026, 7, 9, 11, 30),
        )

    def _dataset_with_mainline_high_stock(self) -> MarketDataset:
        return MarketDataset(
            as_of=dt.date(2026, 7, 9),
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
            externals=[
                ExternalSnapshot(
                    name="纳斯达克",
                    symbol="^IXIC",
                    may_june_return=4.0,
                    july_return=2.0,
                )
            ],
            indexes=[
                IndexSnapshot("000001", "上证指数", 3952.49, -0.46, -18.39, 790773118087.2),
            ],
            stocks=[
                StockSnapshot(
                    code="600000",
                    name="浦发银行",
                    price=11.5,
                    change_pct=8.0,
                    amount=100000000,
                    turnover_rate=2.0,
                    industry="银行",
                    high=11.6,
                    low=10.5,
                    previous_close=10.65,
                )
            ],
            data_sources=("东方财富板块", "东方财富个股", "Yahoo Finance", "东方财富快讯"),
            fetched_at=dt.datetime(2026, 7, 9, 11, 30),
        )

    def _dataset_with_space_and_tech_pressure(self) -> MarketDataset:
        return MarketDataset(
            as_of=dt.date(2026, 7, 9),
            boards=[
                BoardSnapshot(
                    code="BK8001",
                    name="商业航天",
                    board_type="概念",
                    change_pct=6.2,
                    amount=2600000000,
                    main_net_inflow=11086000000,
                    five_day_return=9.0,
                    sixty_day_return=18.0,
                    up_count=30,
                    down_count=3,
                    volume_ratio=1.8,
                    twenty_day_return=8.4,
                ),
                BoardSnapshot(
                    code="BK8002",
                    name="科技风格",
                    board_type="概念",
                    change_pct=-4.2,
                    amount=9000000000,
                    main_net_inflow=-45951000000,
                    five_day_return=4.0,
                    sixty_day_return=45.0,
                    up_count=3,
                    down_count=80,
                    volume_ratio=1.6,
                    twenty_day_return=28.0,
                ),
            ],
            externals=[
                ExternalSnapshot("纳斯达克", "^IXIC", 4.0, -2.0),
                ExternalSnapshot("英伟达", "NVDA", 10.0, -4.0),
            ],
            indexes=[
                IndexSnapshot("000001", "上证指数", 3952.49, -1.0, -40.0, 790773118087.2),
            ],
            stocks=[
                StockSnapshot("000001", "航天样本", 10.0, 7.0, 300000000, 3.0, "商业航天", 10.2, 9.8, 9.3),
                StockSnapshot("000002", "科技样本", 20.0, -5.0, 500000000, 4.0, "科技风格", 20.5, 19.5, 21.0),
            ],
            data_sources=("东方财富板块", "东方财富个股", "新浪财经美股历史", "东方财富快讯"),
            fetched_at=dt.datetime(2026, 7, 9, 11, 30),
        )

    def _dataset_with_candidate_name_not_overlapping_board(self) -> MarketDataset:
        return MarketDataset(
            as_of=dt.date(2026, 7, 9),
            boards=[
                BoardSnapshot(
                    code="BK0003",
                    name="工业母机",
                    board_type="概念",
                    change_pct=5.0,
                    amount=2000000000,
                    main_net_inflow=160000000,
                    five_day_return=12.0,
                    sixty_day_return=18.0,
                    up_count=18,
                    down_count=2,
                )
            ],
            externals=[
                ExternalSnapshot(
                    name="纳斯达克",
                    symbol="^IXIC",
                    may_june_return=4.0,
                    july_return=2.0,
                )
            ],
            indexes=[
                IndexSnapshot("000001", "上证指数", 3952.49, -0.46, -18.39, 790773118087.2),
            ],
            stocks=[
                StockSnapshot(
                    code="002747",
                    name="埃斯顿",
                    price=18.8,
                    change_pct=4.0,
                    amount=180000000,
                    turnover_rate=3.0,
                    industry="工业母机",
                    high=19.0,
                    low=18.0,
                    previous_close=18.08,
                )
            ],
            data_sources=("东方财富板块", "东方财富个股", "Yahoo Finance", "东方财富快讯"),
            fetched_at=dt.datetime(2026, 7, 9, 11, 30),
        )


if __name__ == "__main__":
    unittest.main()
