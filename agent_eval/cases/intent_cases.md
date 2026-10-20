# 意图评测语料说明（A1-02）

## 这是什么

220 条中文句子加人工标注的意图和槽位，用来回答一个具体问题：
**这套意图识别到底准不准，准到什么程度，错在哪。**

没有这份东西，意图识别只能靠「我觉得挺好」，面试里也讲不出数字。

## 诚实声明（必须写清楚）

| 项 | 实际情况 |
|---|---|
| 来源 | 模板合成，**不是真实用户日志** |
| 标注 | 单人两轮：第一轮按模板归属，第二轮用独立挑战词表复核 |
| 是否人工双人标注 | **不是**，项目只有一名标注者 |
| 分歧 | 两轮标签不一致的样本如实登记在 annotation.disagreement |

所以这份数据能证明「规则和模型的行为符合预期」，
但不能当成「线上真实分布上的准确率」。面试时要主动说明这一点。

## 切分与密封

| 切分 | 条数 | 用途 |
|---|---|---|
| train | 125 | 调规则词表 |
| dev | 45 | 调参时看 |
| test | 50 | 冻结后只看一次 |

切分按 case_id 的 sha256 前缀取模固定分配，不随机、不手挑，避免切分泄漏。
密封文件 agent_eval/cases/intent_dataset.json 记录每个切分的条数与 sha256；
评测脚本每次跑之前先核对，语料一改就报 CASES_HASH_MISMATCH 直接失败。

## 一条样本长什么样

字段：case_id、text、label、slots、source、dataset_version、split、content_hash、annotation。

- content_hash 由 text + label + slots 算出，**改任何一个字都会对不上**，防止手改语料不更新 hash。
- dataset_version 现在是 intent-cases/2；v1 到 v2 的唯一变化是**没写日期的句子不再要求 as_of 槽位**。

## 为什么会有混合歧义句

语料里故意放了「持仓成本和大盘走势对比」这类一句两个意图的句子。
它们不是标注错误，而是真实用户会说的话。留着才能暴露规则层的先后顺序问题。

## 怎么用

```
python agent_eval/run_intent_eval.py --splits dev,test       # 跑评测
```

产物在 agent_eval/results/：intent_eval.json（机器读）、intent_report.md（人读）、
intent_badcases.md（错在哪）、intent_confusion.md（混淆矩阵）。
