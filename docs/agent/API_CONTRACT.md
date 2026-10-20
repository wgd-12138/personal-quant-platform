# A1-07 本地 API 与演示面板合同

## 1. 为什么需要这一层

这一层不是「顺手加的页面」，而是把已经跑通的图、人工确认、遥测三段能力变成可被外人验证的接口。
没有它，面试时只能看代码和测试；有了它，浏览器里点一下就能看到意图识别、条件路由、
审批中断、断点续跑、trace 与成本统计的真实行为。

同时这一层是**唯一**对外暴露的面，所以它必须自己扛住鉴权、跨站、跨会话越权和参数篡改，
不能指望前端自觉。

## 2. 端点合同

| 方法 | 路径 | 是否写操作 | 成功码 | 作用 | 关键错误码 |
|---|---|---|---|---|---|
| GET | `/` | 否 | 200 | 返回演示面板 HTML，并下发会话 cookie | SERVER_TOKEN_MISSING / SERVER_TOKEN_INVALID / SERVER_HOST_DENIED |
| GET | `/api/health` | 否 | 200 | 自检报告（数据来源、驱动、tracing 开关） | 同上 |
| POST | `/api/chat` | 是 | 200 | 发起一次 Agent 运行，返回 SSE 事件流 | SERVER_CSRF_HEADER_REQUIRED / SERVER_QUERY_INVALID / SERVER_QUERY_TOO_LONG |
| GET | `/api/state/{thread_id}` | 否 | 200 | 查该线程的图状态与待审批动作 | SERVER_THREAD_NOT_FOUND / SERVER_THREAD_FORBIDDEN |
| POST | `/api/approve` | 是 | 200 | 批准或拒绝当前待审批动作，并续跑图 | SERVER_APPROVAL_STALE / SERVER_APPROVAL_DUPLICATE / SERVER_APPROVAL_NOT_PENDING |
| GET | `/api/trace/{trace_id}` | 否 | 200 | 查一条 trace 的摘要 | SERVER_TRACE_NOT_FOUND / SERVER_THREAD_FORBIDDEN |
| GET | `/api/cost` | 否 | 200 | 成本与耗时统计（本阶段 token/cost 恒为 unknown） | 同上 |

## 3. 鉴权与跨站防护

请求要同时过五道闸门，顺序就是代码里的顺序：

| 顺序 | 检查 | 失败码 | 说明 |
|---|---|---|---|
| 1 | Host 必须是本机 | SERVER_HOST_DENIED | 防 DNS rebinding |
| 2 | 写操作必须带自定义头 `X-PQ-Token` | SERVER_CSRF_HEADER_REQUIRED | 跨站表单带不上自定义头，所以这一步就是 CSRF 防线 |
| 3 | 读出 token 且必须匹配 | SERVER_TOKEN_MISSING / SERVER_TOKEN_INVALID | 用 `hmac.compare_digest` 比对，不用 `==` |
| 4 | Origin 若存在必须是本机 | SERVER_ORIGIN_DENIED | 允许无 Origin（curl） |
| 5 | 请求体不超上限 | SERVER_BODY_TOO_LARGE | 默认上限由 ServerSettings 控制 |

会话 cookie 名字是 `pq_session`，属性 `HttpOnly` + `SameSite=Strict` + `Path=/`，
页面本身读不到它，脚本也带不出去。

读操作允许用查询串 `?token=...` 传 token（只为让控制台里贴一条 URL 就能打开页面），
写操作一律只认请求头；这个开关是 `ServerSettings.allow_query_token`，默认 True，可以关掉。

状态摘要只返回受控字段，包括已校验的引用元数据；不会返回完整图状态、原始数据或模型提示词。

## 4. SSE 事件合同

`POST /api/chat` 返回 `text/event-stream`，每帧形如 `event: <名字>` 加 `data: <JSON>`。

| 事件 | 何时发 | 主要字段 |
|---|---|---|
| `meta` | 流的最开头，恒发 | run_id / thread_id / trace_id / mode / as_of / demo / banner |
| `intent` | classify_intent 节点完成后 | intent / source |
| `node` | 任意节点完成后 | node / fields |
| `tool` | execute_tool 真的执行了工具后 | tool_name / ok / error_code / duration_ms / summary |
| `approval_required` | 图在 request_approval 处挂起 | 13 个绑定字段，见第 6 节 |
| `approval` | 决策节点跑完 | tool_name / status / reason |
| `error` | 图线程抛异常 | error_code（恒为 SERVER_RUN_FAILED）/ exception（只有类型名） |
| `aborted` | 客户端断开 | reason / thread_id |
| `state` | 收口，恒发 | thread_id / status / next_nodes / error_codes |
| `done` | 流的最后一帧，恒发 | status / trace_id / duration_ms / answer |

顺序保证：`meta` 第一，`done` 最后，`intent` 早于任何 `approval_required`。
断线只是标记 `aborted`，**不假装任务被回滚**，图的 checkpoint 仍然在，可以接着查状态。

## 5. 会话与线程归属

每个会话（cookie）可以拥有多个线程；线程一旦被某个会话绑定，其他会话访问该线程的
状态或 trace 一律 403 `SERVER_THREAD_FORBIDDEN`。没有运行记录的线程返回 404
`SERVER_THREAD_NOT_FOUND`，非法 thread_id（含斜杠、反斜杠、空格、点点）也当 404，
不做任何模糊纠正。

## 6. 审批接口与参数绑定

`POST /api/approve` 的请求体：

```json
{
  "thread_id": "t-xxxx",
  "approved": true,
  "approval_id": "a125ae63f7832",
  "action_hash": "...",
  "arguments_hash": "...",
  "reason": "可选说明"
}
```

三道 fail-closed 规则：

1. `approved` 必须是布尔值，缺失或类型不对一律 400，**不默认成批准**。
2. `approval_id` 必须等于图当前挂起的那个，不一致返回 409 `SERVER_APPROVAL_STALE`。
3. `action_hash` 与 `arguments_hash` 若提供，必须与挂起值一致；审批库里记录的动作哈希
   也必须一致，否则 409。这样「批了 A 却执行 B」在接口层就挡住。

同一个 `approval_id` 处理第二次返回 409（`SERVER_APPROVAL_DUPLICATE` 或
`SERVER_APPROVAL_NOT_PENDING`，取决于图是否已跑完），绝不重复执行副作用。

## 7. 演示隔离与秘密边界

- 默认 `demo=True`，数据一律来自 `demo_offline` fixture，页面上挂演示横幅；
  不接真实行情、真实 webhook、真实持仓。
- 所有出站 JSON 都过 `_guard_out`（底层是 `pq_agent.security.guard_payload`），
  发现残留秘密直接抛错，不静默放过。
- token 只在鉴权时读入内存比对，**不写进任何运行时文件**；实测扫描
  `approvals.jsonl` / `checkpoints.sqlite` / `telemetry.jsonl` / `traces.jsonl` 命中数为 0。
- 成本与 token 数在拿不到真实用量时记字符串 `unknown`，**不估算**。

## 8. 验收命令

```powershell
$env:UV_CACHE_DIR = Join-Path $env:TEMP "pq-uv-cache"
uv run --no-sync --extra agent python -m unittest tests.agent.test_api
```

真实服务启动（会自动检测端口）：

```powershell
uv run --no-sync --extra agent python -m pq_agent serve --demo --port 8111
```

## 9. 学习产出

1. **鉴权的顺序本身就是设计**：Host → CSRF 自定义头 → token → Origin → body。
   把 Origin 放在前面会让 curl 和跨站都撞同一个错误码，排障时看不出真因。
2. **跨站防线不是 Origin 而是自定义头**：Origin 可以被省略，自定义头不能。
3. **审批接口必须回显参数哈希**：只对 `approval_id` 做校验，等于把「批准这一件事」
   降级成「批准这个编号」，中间的参数替换就漏了。
4. **审批 id 由（线程号, 调用号, 参数哈希）确定性生成**：好处是同一线程重跑能复用记录，
   坏处是复用运行时目录会导致旧审批再次出现；服务层因此要额外做「已决策」去重，
   测试和探针则要用全新运行时目录。
5. **断线不等于回滚**：把 aborted 当成回滚，就会在真实系统里留下没人知道的半截副作用。

## Public verification and rollback

Run the public test suite with `uv run --no-sync --extra agent python agent_eval/run_unittest_shim.py`. Use Git to revert a reviewed commit when needed; do not manually delete shared source files or internal evidence paths.
