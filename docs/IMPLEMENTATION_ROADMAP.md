# 开发路线与验收标准

## 总体顺序

| 顺序 | 阶段 | 目标 | 验收 |
|---|---|---|---|
| 1 | 本地完整报告 | 在本地先看到未来要推送的完整内容 | 报告包含盘面、持仓、建仓、新闻、晚间复盘、次日计划 |
| 2 | 数据中心补齐 | 个股、指数、新闻、持仓全部接入 | 缺数据直接失败 |
| 3 | 持仓建议 | 每只持仓有操作建议和触发条件 | 止盈、止损、减仓、补仓、清仓都有明确条件 |
| 4 | 建仓候选 | 只从主升方向里找核心标的 | 没有主升输出暂无 |
| 5 | 新闻影响 | 捕捉持仓和候选股利好利空 | 每条新闻有来源、时间、影响对象 |
| 6 | 晚间复盘 | 输出当天总结和次日计划 | 次日按高开、低开、震荡、破位分情景 |
| 7 | 推送渠道 | 接飞书/企业微信 | 推送内容与本地报告一致 |
| 8 | 连续试运行 | 连续 10 个交易日运行 | 不漏报、不假报、失败可追踪 |
| 9 | 模拟交易 | 纸面交易记录 | 不调用真实下单 |
| 10 | 自动交易 | 最后阶段，默认关闭 | 必须人工确认和强风控 |

## 第一阶段必须补齐的报告

| 文件 | 内容 | 命令 |
|---|---|---|
| `reports/intraday_日期.md` | 盘中市场、热门板块、外围、今日建议 | `summary` 后续升级 |
| `reports/positions_日期.md` | 持仓操作建议 | 新增 `positions` |
| `reports/watchlist_日期.md` | 建仓候选和失效条件 | 新增 `watchlist` |
| `reports/news_日期.md` | 利好利空捕捉 | 新增 `news` |
| `reports/evening_日期.md` | 晚间复盘和次日计划 | 新增 `evening` |
| `reports/daily_pack_日期.md` | 汇总成一份可推送报告 | 新增 `daily-pack` |
| `reports/push_preview_日期.md` | 按未来推送顺序展开每条消息 | 新增 `push_preview` |

## 技术选型

| 模块 | 技术 | 原因 |
|---|---|---|
| 主语言 | Python 3.11+ | 数据生态成熟，当前项目已固定 |
| 包管理 | uv | 本地环境可复现 |
| 行情获取 | requests + 后续 AkShare | 先保持依赖少，再逐步接结构化库 |
| 数据校验 | dataclass + 显式校验，后续可加 Pydantic | 当前够简单，先不引入复杂依赖 |
| 本地报告 | Markdown + CSV | 易读、易审计、方便后续推送 |
| 定时任务 | APScheduler | 后续盘中扫描和晚间复盘 |
| 本地服务 | FastAPI | 后续做网页看板和手动触发 |
| 推送 | 飞书 / 企业微信 Webhook | 实现简单，适合个人提醒 |
| 自动交易 | 独立交易层 | 分析和下单隔离，默认关闭 |

## 模块拆分

| 模块 | 第一版任务 |
|---|---|
| `data_center` | 拉取个股、指数、外围、新闻，读取持仓 CSV |
| `market_analysis` | 输出主升、反弹、高位兑现、一日游、无主线 |
| `position_advisor` | 生成持仓止盈、止损、减仓、补仓、清仓建议 |
| `watchlist_engine` | 生成建仓候选；没有主升则输出暂无 |
| `news_ai` | 新闻匹配持仓、候选和板块，只基于证据解释 |
| `report_builder` | 生成完整本地报告、汇总报告和推送预览 |
| `notifier` | 后期只负责发送，不负责生成结论 |
| `risk_control` | 控制仓位、回撤、单票风险、自动交易红线 |

## 验收标准

| 类型 | 标准 |
|---|---|
| 内容完整 | 报告必须出现所有合同模块 |
| 数据真实 | 不允许 mock 数据进入正式报告 |
| 空数据处理 | 关键数据缺失直接失败 |
| 建仓纪律 | 没有主升时不推荐股票 |
| 持仓纪律 | 每只持仓必须有明确操作边界 |
| 新闻纪律 | 无来源、无时间的新闻不能用于结论 |
| 推送一致 | 推送内容必须等于本地报告内容 |
| 回放审计 | 每次报告能查到原始数据摘要 |

## 开发任务顺序

| 任务 | 文件 | 验证 |
|---|---|---|
| 持仓 CSV 读取和校验 | `src/personal_quant/data_center/positions.py` | 缺字段测试失败 |
| 持仓建议模型 | `src/personal_quant/position_advisor/` | 每只持仓都有建议 |
| 个股行情接入 | `src/personal_quant/data_center/providers.py` | 个股数不足失败 |
| 建仓候选筛选 | `src/personal_quant/watchlist_engine/` | 无主升输出暂无 |
| 新闻抓取和匹配 | `src/personal_quant/news_ai/` | 新闻必须有来源和时间 |
| 晚间复盘报告 | `src/personal_quant/report_builder/evening.py` | 四种次日情景完整 |
| 汇总报告 | `src/personal_quant/report_builder/daily_pack.py` | 本地输出完整可推送版本 |
| 推送预览 | `src/personal_quant/report_builder/push_preview.py` | 本地能看到每条未来推送消息 |
| 推送适配 | `src/personal_quant/notifier/` | 测试消息和正式消息分离 |
