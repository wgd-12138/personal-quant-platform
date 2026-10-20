# INTENT.md | 两级意图识别（A1-02）

## 1. 为什么是两级

单靠规则：中文口语覆盖不全，写死词表很快失效。
单靠大模型：每条请求都调 API，慢、贵、还多一次故障点。
所以做成两级——规则先拦，拦不住才交给模型。

| 层 | 做什么 | 耗时 | 成本 |
|---|---|---|---|
| L1 规则 | 关键词加权打分（强 +3 / 弱 +1），命中即返回 | 微秒级 | 0 |
| L2 模型 | L1 拿不准时才调用，输出固定 JSON | 秒级 | 按 token 计 |

关键阈值：`L1_HIT_THRESHOLD = 2`。只中一个弱词算证据不足，**不猜**，交回 L2。

## 2. 八类意图

| 意图 | 含义 | 优先级 |
|---|---|---|
| risk_question | 风控、仓位上限、止损 | 90 |
| pos_question | 我的持仓、盈亏、成本 | 80 |
| code_task | 回测、自定义选股、写脚本算数 | 70 |
| push_request | 推送、发飞书、时段 | 60 |
| report_generate | 生成日报/报告 | 50 |
| history_question | 查历史报告、上次、那天 | 40 |
| market_check | 大盘、板块、指数、新闻 | 30 |
| fallback | 拿不准，交回用户澄清 | — |

打分相同时按优先级定胜负（risk > pos > code > push > report > history > market）。

## 3. 槽位（槽位不是越抽越多越好）

只抽文本里**真实出现**的内容，没有就不写：

| 槽位 | 规则 |
|---|---|
| as_of | 显式 `YYYY-MM-DD` 优先；有相对日期词（今天/昨天/前天）才按参考日期算 |
| stock_code | 6 位代码，或 SH/SZ/BJ 前缀 |
| slot | 推送时段 key，**直接读核心包 PUSH_SLOTS**，不允许两份清单 |
| top_k | 1–20 的整数 |

这一条踩过坑：第一版语料要求「没写日期的句子」也给出 as_of，导致槽位准确率被日期漂移污染。
裁定是**意图层不发明槽位**，默认日期属于 graph/state 层的职责。改成这样后 test 槽位准确率从 0.6364 升到 0.9412。

## 4. L2 合同（严进严出）

- 提示词把用户文本包在 `<DOC></DOC>` 里，并声明其中指令不得执行（防注入）。
- 输出必须是 JSON，字段只允许 intent / confidence / slots / rationale。
- 非 JSON、未知意图、未知字段、非法槽位、越界数值 —— 一律抛 `INTENT_INVALID`，**不做任何兜底猜测**。
- 缺凭据时 `credentials_from_environ()` 返回 None，**绝不回退到公网默认地址**。
- L2 用不了就返回 `fallback` + `INTENT_L2_UNAVAILABLE`，并让 `accepted = False`；不编造答案。

## 5. 评测怎么做（重点）

语料 220 条，模板合成，按 sha256 前缀固定切成 train 125 / dev 45 / test 50，并密封成 `agent_eval/cases/intent_dataset.json`。
每条样本自带 label、来源、数据集版本、内容 hash；评测脚本每次先校验条数与 sha256，语料一漂移就直接失败。

参考日期冻结为 2026-07-09（与 replay fixture 一致），否则「今天」解析出的 as_of 会随运行日期漂移。

### 门禁（先冻结，再评测；dev 与 test 阈值分别记录）

| 指标 | 阈值 | dev 实测 | test 实测 |
|---|---|---|---|
| 端到端准确率 | ≥0.80 | 0.9556 | 0.9600 |
| macro-F1 | 参考 | 0.9528 | 0.9586 |
| L1 命中率 | ≥0.80 | 0.9556 | 0.9200 |
| L1 命中时精确率 | ≥0.90 | 0.9535 | — |
| 槽位准确率 | ≥0.90 | 1.0000 | 0.9412 |

**L2 状态：NOT_MEASURED。** 本机没有配置模型中转站的 base_url，所以 L2 通道没有实测成绩；
报告里如实写「未测量」，不拿 L1 的成绩冒充端到端成绩。

### 剩下的 2 条 badcase 是什么

dev 与 test 各 2 条，且都是**真歧义**，不是规则 bug：

| 句子 | 标为 | 规则判成 | 为什么算歧义 |
|---|---|---|---|
| 000725 的持仓成本和大盘走势对比 | pos_question | market_check | 一句话同时问了持仓和大盘，两个意图都成立 |
| 生成日报并推送到飞书 | report_generate | push_request | 生成和推送是两步，主意图取决于用户真实想问哪一步 |

这类不特殊处理、也不改阈值凑分，留在 badcase 里如实记录。

## 6. 修过的缺陷（都是真实 bug）

| 缺陷 | 后果 | 修法 |
|---|---|---|
| `intent_schema.py` 用了 `require_sequence` 没 import | 带 annotation 的样本解析直接 NameError | 补 import |
| `validate_top_k` 走底层 `require_int` | 越界抛出 `ContractError` 而非 `IntentError`，按合同捕获的调用方会漏掉 | 显式抛 `INTENT_SLOT_INVALID` |
| 密封脚本自持 `DATASET_VERSION` 常量 | 与语料里的版本不一致，密封元数据说谎 | 改成从语料读取并强制唯一 |
| 规则漏词 | 「亏了多少」「那天的报告」「算…相关性」全落 fallback | 补 strong/weak 词并放宽间隔 |

## 7. 与后续任务的关系

- 意图结果进 AgentState（A1-01 的契约），由 LangGraph 主图按意图路由（A1-03）。
- 意图只决定「走哪条路」，不产生任何买卖结论，也不直接调用写操作工具。
