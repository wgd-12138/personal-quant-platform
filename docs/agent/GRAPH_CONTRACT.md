# GRAPH_CONTRACT · LangGraph 主图契约

本文件是 A1-03 的对外契约：图长什么样、每个节点改哪些字段、失败怎么走、恢复靠什么。
实现见 `src/pq_agent/graph.py` 与 `src/pq_agent/checkpoint.py`。

## 1. 为什么用 graph 而不是 chain

| 需求 | chain 做不到的地方 | graph 怎么解决 |
|---|---|---|
| 看数据齐不齐再决定下一步 | 只能线性往下 | `check_inputs` 后接条件边，缺数据直接收口 |
| 一次计划排 N 个工具、逐个执行 | 没有循环 | `execute_tool -> budget_check` 构成 cursor 循环 |
| 有副作用的工具要停下等人批 | 无法停在中间 | `interrupt()` 挂起 + `Command(resume=...)` 续跑 |
| 超预算要终止并说明原因 | 没有中间体 | `budget_check` 是显式节点，超限写终止结论 |

## 2. 节点与字段契约

| 节点 | 改哪些 state 字段 | 说明 |
|---|---|---|
| `classify_intent` | intent / intent_source / slots / messages / errors | L1 规则先拦，拦不住才调 L2 |
| `check_inputs` | data_snapshot_id / messages / errors | 只通过 provider 取数，不直接联网 |
| `rag_retrieve` | citations / messages / errors | 配置检索器后写入已校验引用；缺失时记 RAG_UNAVAILABLE，不硬撑 |
| `plan_tools` | plan / cursor | 默认按意图给确定性只读计划 |
| `budget_check` | errors / final_answer | 取消与超限都在这短路 |
| `request_approval` | approvals / pending_approval / cursor / errors | 有副作用的能力必须先过这里 |
| `execute_tool` | cursor / tool_results / errors / messages | 每次执行一个，然后 cursor+1 |
| `compose_answer` | final_answer / messages | 只复述工具给过的事实 |
| `handle_error` | errors / final_answer | 意外异常的显式去处 |

## 3. 能力分级与审批

| 能力 | 是否需人工确认 | 说明 |
|---|---|---|
| `read_local` | 否 | 只读本地文件 |
| `read_external` | 否 | 读外部数据；replay 下读 fixture |
| `control_write` | 是 | 写控制面（报告、回执） |
| `business_write` | 是 | 写业务数据 |
| `notify_external` | 是，且**不可自动重试** | 外发未知结果重试会重复推送 |
| `code_exec` | 是 | 执行代码（Contract 五个能力之外单独列出） |

审批记录必须绑定 `arguments_hash`，resume 时重算比对；不一致直接拒绝，
这就是防 “批准 A 却执行 B” 的机制。

## 4. 显式边（不允许静默失败）

| 情形 | 走法 |
|---|---|
| 缺数据 | `check_inputs -> compose_answer`，明确说不输出行情结论 |
| 预算超限 | `budget_check -> compose_answer`，带 BUDGET_EXCEEDED 原因 |
| 取消 | errors 记 RUN_CANCELLED，同样短路收口 |
| 节点意外异常 | `_wrap` 记 GRAPH_INTERNAL_ERROR，`_error_first` 路由到 handle_error |
| 工具失败 | 记 TOOL_FAILED，外发类不重试 |
| 审批拒绝 | 记 APPROVAL_REJECTED，跳过该工具且 cursor 前进 |
| checkpoint 损坏 / 版本不匹配 | 在恢复入口抛错，不进入图 |

## 5. checkpoint 与恢复

| 项 | 约定 |
|---|---|
| 后端 | `langgraph-checkpoint-sqlite`（锁定版） |
| 配置 | 必须含 `thread_id`，否则抛 THREAD_ID_REQUIRED |
| 写入前 | `GuardedSqliteSaver.put` 对 channel_values 过脱敏闸门 |
| 版本 | graph_version 与 checkpoint_schema_version 不匹配一律失败 |
| 恢复 | `resume_graph(app, decision, thread_id=...)` = `Command(resume=decision)` |
| 驱动缺失 | 抛 CHECKPOINT_DRIVER_MISSING，不降级成内存 saver |

实测：中断后新建进程、重开 checkpoint、重新 compile，同 thread_id 恢复，副作用恰好执行 1 次。

## 6. 已修的坑（面试可讲）

| 坑 | 现象 | 修法 |
|---|---|---|
| 吞掉 GraphInterrupt | HITL 静默失效：interrupted=False、pending=None | `_wrap` 里 `except GraphInterrupt: raise` 放最前 |
| handle_error 不可达 | 画了失败边却永远走不到 | 新增 `_error_first` 工厂，复用 errors 通道路由 |
| 终止结论被覆盖 | 预算超限的 final_answer 被收口重建 | 已存在终止结论时不再重建，只追加 message |
| 错误码重复 | 日志出现 CODE: CODE: detail | 加 `_strip_code_prefix` 剥掉前缀 |
| 共享 helper 导入失败 | discover 下 ModuleNotFoundError: No module named tests | 改相对导入 `from ._graph_fakes import ...` |

## 7. 回滚

删除 `src/pq_agent/graph.py`、`src/pq_agent/checkpoint.py` 与 `tests/agent/` 下四个文件即可，
核心包 `src/personal_quant/` 本次零改动，回滚不影响原有量化功能。
