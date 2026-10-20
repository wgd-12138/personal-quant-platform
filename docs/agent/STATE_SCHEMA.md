# STATE_SCHEMA | pq_agent 的 state 与序列化合同

A1-01 交付物。代码：`src/pq_agent/contracts.py`、`src/pq_agent/state.py`；测试：`tests/agent/test_state_contract.py`。

## 1. 为什么 state schema 是企业 Agent 的持久化 API

state 不只是"图里的一个字典"。在企业 Agent 里它同时承担四个身份：

| 身份 | 后果 |
|---|---|
| LangGraph 通道定义 | 字段一改，所有节点签名和所有历史 checkpoint 一起失效 |
| checkpoint 落盘格式 | 线上重启、人工确认后恢复、崩溃续跑都读它 |
| API / 前端契约 | 前端展示的意图、工具调用、引用、预算都来自它 |
| 审计与复现依据 | 审批记录、工具回执、错误码都落在里面 |

所以它必须像数据库表一样对待：字段显式声明、类型显式校验、版本显式迁移、非法数据显式拒绝。反过来，把 state 当"随便放东西的桶"，就会在恢复线上会话时才发现字段对不上，而且无法判断是数据被污染还是代码被改动。这也是为什么本模块宁可让写入方多传一个参数，也不肯用默认值兜底。

## 2. 字段表（23 个，顺序即 STATE_FIELDS 顺序）

| 字段 | 类型 | 说明 |
|---|---|---|
| messages | array | 只存脱敏摘要 + ID（`Message`），禁止完整 prompt |
| user_query | string | 用户原始诉求（入口必须先过脱敏闸门） |
| thread_id | string | 会话线程，必填非空 |
| request_id | string | 单次请求，默认 `thread_id:时间戳` |
| mode | enum | `demo_offline` / `replay` / `live`，默认 `demo_offline` |
| as_of | date 或 null | 固定评测日期；live 模式必须等于当天 |
| timezone | string | 时区名，默认 `Asia/Shanghai` |
| data_snapshot_id | string 或 null | 数据快照 ID，形如 `replay:2026-07-09:675f18e6f38c` |
| checkpoint_schema_version | int ≥1 | checkpoint 版本 |
| graph_version | string | 图版本 `pq_agent.graph/1` |
| intent | string | 意图标签（可为空，表示未识别） |
| intent_source | enum | `l1`（规则）/ `l2`（模型）/ `fallback` |
| slots | object | 抽取到的槽位 |
| plan | array | `ToolCall` 列表 |
| cursor | int ≥0 | 计划执行游标 |
| pending_approval | object 或 null | 当前待人工确认的 `Approval` |
| approvals | array | `Receipt` 列表（审批与执行回执） |
| tool_results | array | `ToolResult` 列表 |
| citations | array | `Citation` 列表 |
| budget | object | `Budget`：步数 / token / 墙钟三重上限 |
| errors | array | 单行错误码摘要，禁止多行堆栈 |
| final_answer | string 或 null | 最终答复 |
| trace_id | string | 链路 ID |

启动必填：`thread_id`、`request_id`、`mode`、`timezone`、`checkpoint_schema_version`、`graph_version`。缺任一项 → `STATE_NOT_STARTABLE`。

## 3. 可序列化契约（9 个，各自带 schema_version）

| 契约 | 关键字段 | 重要约束 |
|---|---|---|
| Message | message_id, role, summary | 任何额外字段（如 `content`）→ `MESSAGE_FORBIDDEN_FIELD` |
| ToolCall | call_id, tool_name, arguments, capability, dry_run | capability 必须命中 5 级枚举；arguments 必须是 JSON 原生类型 |
| ToolResult | ok, summary, error_code, data, artifacts, degraded, duration_ms | 成功不许带 error_code、失败必须带 error_code，否则 `RESULT_INCONSISTENT` |
| Citation | chunk_id, source_path, line_start, line_end, content_hash | 路径必须相对且不含穿越；hash 必须 64 位；区间不得倒置 |
| Approval | status, arguments_hash, requested_at, decided_at | pending 不许有 decided_at；已决必须有 decided_at，否则 `APPROVAL_INCONSISTENT` |
| Receipt | status, idempotency_key, external_id | 幂等键是"重启不重复推送"的依据 |
| Error | node, error_code, retryable, timestamp | 只存错误码与类型，不存响应体 |
| DataQuality | profile, source, field_quality, as_of, fetched_at, stale, degraded | 用来向用户说明"这份数据是什么、够不够新" |
| Budget | max_steps, max_tokens, max_wall_clock_s, steps, tokens | 超限返回 `BUDGET_EXCEEDED`，不静默截断 |

能力分级 5 级：`read_local` / `read_external` / `control_write` / `business_write` / `notify_external`。

## 4. 版本与迁移规则

1. 恢复时 `checkpoint_schema_version` 不等于当前版本，默认走显式迁移函数（`STATE_MIGRATIONS` 注册表）。
2. 没有注册迁移路径 → `CHECKPOINT_VERSION_MISMATCH`，**不静默重建**。
3. checkpoint 版本高于当前支持 → 同样 `CHECKPOINT_VERSION_MISMATCH`（禁止降级重建）。
4. `graph_version` 不匹配 → `GRAPH_VERSION_MISMATCH`。
5. 迁移必须只补结构、不猜业务：`_migrate_state_v0_to_v1` 把 `as_of` / `data_snapshot_id` 置空、`intent_source` 记为 `fallback`。
6. 迁移函数重复注册 → `STATE_MIGRATION_DUPLICATE`；迁移超过 32 跳视为自环。

旧版本样例：`tests/fixtures/state/state_v0_legacy.json`（`checkpoint_schema_version=0`）。

## 5. 脱敏边界

- 唯一出口：`guard_state_for_persistence(state, secrets=...)`。顺序固定为**先过 `guard_payload`（fail-closed，残留即抛 `SecretLeakError`）再校验结构**。
- 递归键名策略：`headers` / `authorization` / `cookie` / `prompt` / `raw_prompt` / `system_prompt` / `traceback` / `stack` / `exception` / `webhook` / `webhook_url` / `api_key` / `password` / `secret` / `file_content` 命中任一 → `STATE_FORBIDDEN_KEY`。
- 值级扫描：`find_secrets` 命中明文密钥 → `STATE_SECRET_DETECTED`。
- messages 摘要落库前经 `scrub_text`（上限 2000 字符）；errors 每项上限 300 字符且必须单行。
- 因此入口流程是：**先脱敏 → 再写 state**。state 里永远不出现明文密钥。

## 6. 序列化与 JSON Schema

- `state_to_dict` / `state_to_json`：先校验再 JSON 归一；`state_from_dict` / `state_from_json`：先版本处理再校验。
- `STATE_JSON_SCHEMA`：`additionalProperties=false`，`required` 等于全部 23 个字段。
- 9 个契约各自导出 `*_JSON_SCHEMA`，汇总在 `JSON_SCHEMAS`。
- 内置最小校验器 `validate_against_schema` 支持 type / required / additionalProperties / enum / minimum / minLength / items。

## 7. 验收命令

```powershell
$env:PYTHONPATH='src;tests'; python -m unittest agent.test_state_contract
$env:PYTHONPATH='src'; python -m unittest discover -s tests
python planning\agent\evidence\A0-00\run_unittest_shim.py
python planning\agent\evidence\A1-01\verify_a1_01.py
python -m compileall -q src tests
git diff --check
```

## 8. 学习产出

1. state 是企业 Agent 的持久化 API：字段、类型、版本都要当数据库表来治理，不能当临时字典。
2. 版本不匹配必须失败而不是"尽力恢复"；静默重建会让线上会话产生无法解释的行为差异。
3. 迁移函数只补结构、不猜业务，否则等于用默认值伪造历史。
4. 两个闸门有先后：脱敏必须先于结构校验，因为结构校验会拒绝明文密钥。
5. 敏感键名策略（`prompt`、`headers`）比值扫描更早生效，属于"按名字直接拒绝"的第一道防线。

## 9. 回滚

```powershell
Remove-Item src\pq_agent\state.py
Remove-Item tests\agent\test_state_contract.py
Remove-Item -Recurse -Force tests\fixtures\state
Remove-Item docs\agent\STATE_SCHEMA.md
Remove-Item -Recurse -Force planning\agent\evidence\A1-01
```

`src/pq_agent/contracts.py` 同时被 A1-01 使用，回滚时需确认没有其他模块引用后再删除。
