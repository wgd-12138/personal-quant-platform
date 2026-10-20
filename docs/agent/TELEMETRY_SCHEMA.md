# TELEMETRY_SCHEMA.md | A1-06 最小 OpenTelemetry 合同

目标：让一次对话的每一步都能被追溯，同时保证**任何提示词、参数、密钥、checkpoint 内容都不会进入遥测**。

## 1. 为什么需要这一层

| 没有遥测的后果 | 有了之后怎么解决 |
|---|---|
| 图跑完只知道"结果是这个"，不知道在哪一步偏了 | 每个节点一个 graph.node span，节点名与耗时可直接看 |
| 密钥、webhook、提问原文可能随 trace 落到第三方平台 | 出口只有一条：属性白名单 + 脱敏闸门 + 自检 |
| 没有 Collector 就没法开发 | exporter 可替换，缺 Collector 时显式降级但不阻断业务 |
| 成本数字不可得，容易被编出来 | token 与 cost 一律记 unknown，绝不估算 |

## 2. 三个模块的职责

| 文件 | 职责 |
|---|---|
| src/pq_agent/observability/resource.py | 统一 service.name / version / environment / commit / graph_version |
| src/pq_agent/observability/redaction.py | 属性白名单、黑名单、脱敏、出口包装 SanitizingSpanExporter |
| src/pq_agent/observability/event_log.py | tracer 装配、限权轮转事件日志、进程级注册表 |
| src/pq_agent/observability/__init__.py | 对外唯一入口，调用方只从这里 import |
| src/pq_agent/graph.py | _wrap 里给每个节点开 span（观测层是旁路，失败不影响业务） |

## 3. span 结构

| span 名 | 位置 | 说明 |
|---|---|---|
| graph.node | graph.py 的 _wrap | 每个节点一个，节点名写在 agent.node 属性里 |
| agent.turn / intent / tool / llm / answer | 后续 A1-07 与 A4 阶段 | 本阶段只提供 tracer 与装配，不提前造 span |

graph.node 实际属性（实测，共 7 个键）：

| 属性 | 含义 |
|---|---|
| agent.node | 节点名 |
| thread_id | 会话线程 |
| agent.mode | demo_offline / replay / live |
| as_of | 数据日期 |
| graph_version | 图版本 |
| status | ok 或 error |
| duration_ms | 节点耗时 |

出错时额外写 error_code（取 ContractError 的 code，否则异常类型名）与 error_type。**不写异常原文，不写 traceback。**

## 4. 属性白名单与黑名单

| 名单 | 数量 | 处理方式 |
|---|---|---|
| SPAN_ATTRIBUTE_ALLOWLIST | 24 项 | 只有名单内的键可能进入 span |
| TRACING_FORBIDDEN_ATTRIBUTE_KEYS | 21 项 | 命中即整键丢弃（prompt、arguments、checkpoint、authorization、exception 等） |
| 其它键 | - | 默认丢弃，不因为"看起来无害"就放行 |

值本身还要再过一遍 pq_agent.security 的 scrub_text：已知密钥替换成 [REDACTED:known_secret]，飞书 webhook、JWT、bearer、sk- 开头的 key、邮箱、本机用户名路径都会被替换。

## 5. 出口唯一性

| 出口 | 是否过闸门 | 说明 |
|---|---|---|
| 真 OTLP exporter | 是 | 由 wrap_exporter 包成 SanitizingSpanExporter 后再 delegate |
| 本地扇出点 SpinCallbackExporter | 是 | 同一个包装路径，单测与无 Collector 环境也走 |
| 本地事件日志 | 是 | 每条事件先 guard_payload 再落盘 |
| checkpoint / 审批账本 | 是 | A1-01 与 A1-05 已各自接入 guard_payload |

span 属性在写入 exporter 前被再次清洗，任何键被丢弃时只累加 dropped_attributes 计数，不把被丢的键名写进 trace。

## 6. 降级与成本语义

| 情况 | 行为 |
|---|---|
| 没有配置 exporter | status.degraded=True、reasons=[no_exporter_configured]，同时记一条 tracing_degraded 事件，业务照常 |
| 配置了但构造失败（例如 Collector 没起） | 记 exporter_unavailable 事件，返回 None，不抛异常 |
| usage 里没有 token 或 cost | 一律写字符串 unknown，绝不估算、绝不写 0 冒充 |
| 进程没有配置过 tracer | get_tracer() 返回调用方给的默认值，不偷偷造一个 tracer |

事件日志默认路径在仓库 .tmp 下，单文件上限 1000000 字节，超限自动轮转，落盘失败只计数不阻断。

## 7. 验收命令

```powershell
$env:UV_CACHE_DIR = Join-Path $env:TEMP "pq-uv-cache"
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

uv run --no-sync --extra agent python -m unittest tests.agent.test_telemetry
```

## 8. 学习产出

1. **观测层必须是旁路**：span 创建、属性写入、exporter 包装全部 try/except 吞掉，否则"看日志"这件事本身会把 Agent 搞挂。
2. **出口要收敛到一处**：脱敏写在 exporter 包装里而不是各个调用点，才能保证新增代码不会绕过去。
3. **白名单比黑名单可靠**：黑名单永远漏，白名单默认拒绝。这次同时用了黑名单（快速拦高频雷）和白名单（兜底）。
4. **未知就写 unknown**：token 与成本拿不到时写 unknown，比写 0 更诚实，也避免面试时被问"这个数字哪来的"。
5. **没有 Collector 不等于没有可观测性**：spans 生成、属性脱敏、降级事件三件事在没有 OTLP 后端时都能本地验证。
6. **闭环依赖命名**：SpanExportResult 枚举在不同版本里位置不同，包装 exporter 时只做委托不做假设，避免锁死版本。

## Public verification and rollback

Run the public test suite with `uv run --no-sync --extra agent python agent_eval/run_unittest_shim.py`. Use Git to revert a reviewed commit when needed; do not manually delete shared source files or internal evidence paths.
