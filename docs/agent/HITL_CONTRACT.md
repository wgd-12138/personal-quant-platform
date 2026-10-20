# HITL 与出件箱合同（A1-05）

## 1. 为什么需要这一层

- 有副作用的动作（写盘、外发、跑代码）不能靠提示词约束，权限判定必须写在代码层。
- 图节点只按 capability 决定是否中断：`APPROVAL_REQUIRED_CAPABILITIES = control_write + business_write + notify_external + code_exec`。
- 幂等与"结果未知"必须显式建模，否则进程重启就会重复推送。

## 2. 一次副作用调用的时序

1. `budget_check` 取出 `plan[cursor]`，能力命中审批集合 → 路由到 `request_approval`。
2. `request_approval_node` 计算 `arguments_hash`（canonical 参数）与 `action_hash`（工具名 + 参数 + 数据快照 + runtime）。
3. 有 `approval_store` 时先把 pending 记录落盘，再 `interrupt(payload)` 挂起，checkpoint 保存整图状态。
4. 人审批后用 `Command(resume=...)` 继续；`_interpret_decision` 先做三条 fail-closed 校验，再二次比对 `action_hash`。
5. 通过后 `approval_store.decide` + `approval_store.consume`（一次性消费，CAS）。
6. `execute_tool_node` 先用幂等键查 outbox：`SENT` → 记 `ALREADY_COMMITTED` 直接跳过；`UNKNOWN` → 记 `OUTBOX_UNKNOWN_REQUIRES_HUMAN` 拒绝自动重发；其余才真正执行。
7. 执行结果写回 outbox（`SENT` / `FAILED` / `UNKNOWN`），`cursor + 1`，回到 `budget_check`。

## 3. 审批记录字段

`approval_id`、`thread_id`、`call_id`、`tool_name`、`capability`、`arguments_hash`、`action_hash`、`data_snapshot_id`、`runtime`、`requested_at`、`expires_at`、`actor`、`preview`、`risk_note`、`status`、`decided_at`、`decided_actor`、`consumed_at`、`reason`、`schema_version`。

| 绑定关系 | 说明 | 不匹配时 |
|---|---|---|
| `approval_id = sha256(thread_id:call_id:arguments_hash)[:12]` | 带线程号：同线程 resume 复用旧记录，不同线程必须重新审批 | 生成新记录并再次中断 |
| `action_hash` 覆盖工具名 + 参数 + 数据快照 + runtime | 防止批了 A 执行 B 之后换快照溜过去 | `APPROVAL_ACTION_MISMATCH` |
| 线程号 | 审批不能跨会话借用 | `APPROVAL_THREAD_MISMATCH` |
| 决策人 | 审批指定给谁就谁批 | `APPROVAL_ACTOR_MISMATCH` |
| TTL 900 秒 | 过期即失效 | `APPROVAL_EXPIRED` |
| 一次性消费 | 同一条审批不能执行两次 | `APPROVAL_ALREADY_CONSUMED` |

## 4. fail-closed 规则

- 没收到决定（`None` 或空）→ 拒绝。
- `approved` 非真值 → 拒绝。
- 决定里的 `arguments_hash` 或 `action_hash` 与当前调用不一致 → 拒绝。
- 审批落库本身抛错 → 一律拒绝，并记 `APPROVAL_STORE_FAILED`，绝不放行。

## 5. 出件箱

- 阶段：`prepare` / `commit` / `notify`；条目状态 `PREPARED` / `SENT` / `FAILED` / `UNKNOWN`。
- 幂等键字段固定为 `RECEIPT_KEY_FIELDS`：`tool_name`、`action_hash`、`thread_id`、`as_of`、`slot`、`payload_digest`、`code_hash`、`prompt_hash`、`data_snapshot_id`、`runtime`；多一个字段直接 `OUTBOX_ENTRY_INVALID`。
- 不可自动重试的能力：`NOT_RETRYABLE_CAPABILITIES = ("notify_external",)`；失败即 `UNKNOWN`，必须人工核对后调用 `retry(approval=...)`。
- 归档保留 `OUTBOX_RETENTION_DAYS = 90` 天，只归档已结算条目。

## 6. 实测结果

| 场景 | 实测 |
|---|---|
| 副作用前停下 | `interrupted=True`，执行 0 次，审批记录为 `status=pending` 并带有过期时间 |
| 进程重启续跑 | 执行恰好 1 次，`consumed_at` 非空，outbox 从 `PREPARED` 变 `SENT` |
| 审批拒绝 / 参数哈希不一致 / 动作哈希不一致 | 执行 0 次，`approvals` 状态为 `rejected` |
| 另一个线程发起同一动作 | 生成新 `approval_id` 并再次中断，不允许借用旧审批 |
| 已 SENT 条目重放 | 执行 0 次，记 `ALREADY_COMMITTED` |
| 已 UNKNOWN 条目重放 | 执行 0 次，记 `OUTBOX_UNKNOWN_REQUIRES_HUMAN` |
| 外发工具抛异常 | outbox 状态 `UNKNOWN`，图记 `TOOL_FAILED` |
| preview 脱敏 | 内存与落盘都不含 webhook 明文，重载前后 `fingerprint` 相同 |

## 7. 已知边界与遗留风险

- 审批账本与出件箱都是本地 JSONL，追加写没有加锁，本阶段只保证单机单进程。
- 真实飞书的 exactly-once 无法由本地回执保证，`UNKNOWN` 只能靠人工核对。
- `RUNTIME_LOCAL = "local"` 参与动作哈希；上云换实现时必须同步改，否则云端与本机会算出同一个哈希、审批跨环境复用。
- `code_exec` 已接入计划、审批和沙箱执行路径；没有声明 `untrusted_code_safe` 的运行时会拒绝执行。当前环境未验证 Docker/gVisor 的真实隔离能力。

## 8. 回滚

本文记录当前合同。需要撤销改动时，请使用仓库版本控制恢复到已审阅的提交；不要按文件清单手动删除，也不要恢复旧的内部阶段快照。
