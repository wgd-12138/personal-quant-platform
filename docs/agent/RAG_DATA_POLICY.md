# RAG 数据策略（A3-01）

这份文档回答三个问题：**哪些文档能进语料**、**进来了之后怎么保证引用能核对**、**查不到的时候怎么办**。
它不是建议，是代码里已经生效的约束（`src/pq_agent/rag/corpus.py`）。

## 1. 语料白名单与拒绝表

| 决定 | 值 | 代码位置 |
|---|---|---|
| 允许的目录前缀 | `docs`、`planning/agent` | `DEFAULT_INCLUDE_PREFIXES` |
| 明确拒绝的前缀 | `data`、`reports`、`logs`、`planning/runs`、`config`、`.git`、`.tmp`、`agent_eval/results` | `DENIED_PREFIXES` |
| 允许的扩展名 | `.md` `.txt` `.yaml` `.yml` `.json` | `ALLOWED_SUFFIXES` |
| 单文件上限 | 512 KB | `DEFAULT_MAX_FILE_BYTES` |

顺序是**先查拒绝表再查白名单**：`docs/agent` 在白名单里，但 `planning/runs` 在拒绝表里就是拒绝表赢。
真实持仓（`data/`）、运行回执（`planning/runs/`）、私有报告（`reports/`）、密钥（`config/local.yaml`、`.env`）都进不来。

## 2. 三道入口检查

| 闸门 | 做法 | 失败时 |
|---|---|---|
| 路径合法性 | 复用 `security.paths.read_within_root`（解析真实路径 + 挡穿越/符号链接/junction） | 整份文档拒收并记进 manifest 的 denied 列表 |
| 源码文本里带密钥 | 用已登记密钥（环境变量扫描 + 额外传入）逐条在正文里找 | 整份拒收，码 `CORPUS_ERROR_SECRET_SHAPED` |
| 体积 | 超过 `max_file_bytes` | 拒收，码 `CORPUS_ERROR_TOO_LARGE` |

第二条是"宁可少索引一篇，也不把密钥写进索引"，因为索引是会被复制、上传、写进演示截图的。

## 3. 切块与引用可核对

切块按 `#{1,6}` 标题切，单块超过 1200 字继续按行切。每块保存 `source_path`、`heading`、`heading_level`、
`start_line`、`end_line`、`content_hash`、`doc_type`、`index_version`，行号是 1-based 闭区间。

两条硬性质（都有测试断言，不靠嘴说）：

1. **可还原**：把一份文档所有块按行号顺序拼起来，等于原文。
2. **可校验**：每块的 `content_hash` 与 `chunk.text` 一致（`verify_chunk_hash`），块被改过就当场露馅。

引用串格式是 `路径:起始行-结束行 (标题)`。只给文件名不给行号的引用，不算引用。

## 4. 两档 profile：说清楚比跑得动更重要

| profile | 行为 | 结果标记 |
|---|---|---|
| `lexical_only` | 只用 BM25 词法通道，必须**显式选择** | `degraded=true`，原因写明"语义召回未启用（非静默降级）" |
| `dense_required` | 需要向量后端；选不出来就抛 `EMBEDDING_UNAVAILABLE`，**绝不回落到词法** | 抛异常，不产出索引 |

为什么默认是词法：本机没有 embedding 凭据，离线权重也装不上（面试目录 rag_quant 实测 fastembed 失败）。
更关键的是评测数字——等权 RRF（58%）明显差于单路 BM25（86%），所以先把词法基线做扎实，
融合权重必须由 30 题评测选出来，不能拍脑袋。

`HermeticEmbedder` 是离线确定性后端，**只用于测试融合与排序逻辑，不是语义模型**。把它当语义通道用就是自欺。

## 5. 索引原子重建与回滚

`build_index` 只产出结果不落盘，`write_index` 负责落盘，两件事分开才好测。落盘流程：

1. 序列化后先自己解析一遍，并重算指纹比对（`index_fingerprint` 排除 `built_at` 与 `index_hash`）；
2. 写临时文件、回读、比对块数；
3. 通过才 `os.replace` 覆盖；任何一步失败就删临时文件，**旧索引原样留下**。

指纹必须排除 `built_at`，否则同样的语料两次构建得到不同指纹，评测就不敢用；
也必须排除 `index_hash` 自己，否则自检永远失败。两个坑都很隐蔽，写在这里免得下次再踩。

回滚：把 manifest 指回上一个版本，或直接删掉索引文件让 `index_status` 报 `RAG_INDEX_MISSING`。
**任何情况下都不自动删除私有语料**——索引可以重建，用户数据不行。

## 6. 提示注入的位置

检索块是**数据**，不是指令。语料里刻意放了注入样本（"忽略之前的要求"之类），用来验证：
命中这类块时只把它当文本返回，不改变任何工具选择，也不触发任何工具调用。
权限判定在代码层（`tools/registry.py` 的能力分级 + 审批令牌），因为提示词可以被说服，代码不行。
