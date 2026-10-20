# 面试演示手册（DEMO_RUNBOOK）

这份手册只解决一件事：**让别人照着念，就能把 A0~A5 的技术一条条演示出来，并且每句话都有屏幕上的证据支撑。**

演示全程离线：不连 LLM、不连外网、没有 Docker、没有 SkyWalking，一样能跑完 9 步。

## 0 三十秒准备

```powershell
# 1) 先看环境（不跑任何东西）：目录在哪、语料在不在、索引在不在
uv run --no-sync --extra agent python -m pq_agent demo --status

# 2) 清掉上次演示留下的临时状态（只删 .tmp/pq_agent_demo 里的内容）
uv run --no-sync --extra agent python -m pq_agent demo --reset

# 3) 跑完整场，终端直接看到 9 屏
uv run --no-sync --extra agent python -m pq_agent demo
```

产物落在两个地方：终端是给人看的，磁盘上留两样证据；

- `.tmp/pq_agent_demo/transcript.txt` —— 逐字记录，方便回看和贴给别人；
- `.tmp/pq_agent_demo/demo_run.json` —— 结构化结果（每步 ok/markers/duration），机器可读。

> 目录纪律：演示只写 `.tmp/pq_agent_demo/`。`data/`、`reports/`、`logs/`、`planning/` 一律不碰，
> 重置也只会清演示目录本身；把演示目录指到别处会被拒绝（错误码 `DEMO_PATH_DENIED` / `DEMO_RESET_REFUSED`）。

## 1 演示总览

| 步 | 技术点 | 讲解秒数 | 屏幕上的关键证据 |
|---|---|---|---|
| 1 doctor | 可诊断性 | 30 | `doctor_ok=true` 与四项 check |
| 2 replay_qa | 意图识别 + 工具调用 + RAG 引用 | 60 | `intent=market_check`、`citation=路径:行-行` |
| 3 missing_data | 缺数据失败关闭 | 35 | `AS_OF_NOT_IN_FIXTURE` |
| 4 reject_side_effects | HITL 拒绝路径 | 40 | `APPROVAL_REJECTED`、`external_sends=0` |
| 5 approve_two_phase | HITL 批准路径 + 参数绑定 | 45 | `arguments_hash=`、`dry_run=True` |
| 6 sandbox_layer | 沙箱三层与能力矩阵 | 70 | `constraint_only`、`SANDBOX_NOT_EXECUTABLE` |
| 7 trace_alerts | OpenTelemetry + APM 降级 | 55 | `node_sequence=`、`collector=unavailable_on_this_host` |
| 8 resume_checkpoint | 断点恢复与幂等 | 45 | `pending_from_new_instance=`、`side_effect_executions=1` |
| 9 role_denied | 多角色权限隔离 | 30 | `ROLE_CAPABILITY_DENIED`、`call_log_delta_after_deny=0` |

讲解预算合计 410 秒（脚本自身的实测时间约 3.5 秒，其余都是人说话的时间）。
脚本会自己算这两件事，所以「超时」不用靠感觉：末尾一行 `讲解预算：410 秒；脚本实测：xxxx ms`。

## 2 逐步脚本

每步统一按五段讲：**命令 → 屏幕预期 → 技术原理 → trade-off → 追问怎么答**。
末尾还有「失败备用路径」：现场真出问题，不至于卡住。

### 第 1 步 doctor：先证明这台机器是好的

- 命令：`uv run --no-sync --extra agent python -m pq_agent demo --step doctor`
- 屏幕预期：`doctor_ok=true`，下面四行 `check.port=true`、`check.checkpoint_driver=true`、`check.demo_fixture=true`、`check.tracing=true`。
- 原理：端口是否空闲、sqlite checkpoint 驱动是否装好、演示 fixture 是否在、tracing 是否配置成功——四件事都在跑之前查，避免「演示到一半才发现环境不对」。
- trade-off：体检只查「能不能跑」，不查「跑得好不好」。它是一个开关量，不是性能报告，故意做轻。
- 追问「为什么不直接跑？」：真实项目里失败最常见的原因是环境，不是代码。先体检能把 90% 的现场意外挡在第一屏。
- 失败备用：如果 `checkpoint_driver=false`，说明没装 `langgraph-checkpoint-sqlite`，先 `uv sync --extra agent` 再演示；如果 `check.port=false`，说明上次的服务没关，换个端口或关掉旧进程。

### 第 2 步 replay 问答：听懂、选工具、给引用

- 命令：`... demo --step replay_qa`
- 屏幕预期（逐行对）：
  - `intent=market_check` 和 `intent_message=意图=market_check 来源=l1 置信度=0.9`：意图由规则层命中，没花模型钱；
  - `tool_event={"ok": true, "tool_name": "market_snapshot"}`：只读工具真被调了一次；
  - `graph_citations=...`：引用来自 LangGraph 主图的 `citations` state，不是演示层单独检索拼出来的；
  - `rag_mode=bm25`、`rag_degraded=true`、`rag_degraded_reason=lexical_only：...`：检索只有词法通道，程序自己标了降级；
  - `citation=docs/PRODUCT_REQUIREMENTS.md:78-87 (晚间复盘内容合同)` 等：图状态里的每条引用都带**文件名 + 行号区间**，实际命中随索引内容变化。
- 原理：意图识别是两级的——先用规则（命中就打标），规则拿不准才交给模型；检索这一层把「文档 → 切块 → chunk 指纹 → 引用串」串起来，引用串能反查到原文那几行。
- trade-off：等权混合检索在这套小语料上是负收益（实测等权 RRF 的 recall@5 只有 58%，单路 BM25 是 86%），所以默认词法优先、权重可标定。宁可用一个讲得清的数字，也不用一个漂亮但虚假的「混合检索」。
- 追问「为什么不用向量？」：本机没有中文 embedding 权重（fastembed 安装时 DNS 与权限都失败，有失败报告存档），所以语义通道没有真实后端。**没有后端就标 degraded，而不是悄悄退回词法还叫它混合检索。**
- 失败备用：`rag_mode=bm25` 已是最差情况；如果连 `citation=` 都没有，先跑一次语料重建（`agent_eval/build_rag_eval_cases.py` / 索引重建），索引文件在 `agent_eval/results/rag_index.json`。

### 第 3 步 缺数据：宁可失败，也不给结论

- 命令：`... demo --step missing_data`
- 屏幕预期：`requested_as_of=2026-07-08`、`error=check_inputs | INPUTS_NOT_READY: FreshnessError: AS_OF_NOT_IN_FIXTURE: 2026-07-08 不在 fixture 中；可用日期：2026-07-09`，答复里有一句「本次没有拿到可用数据，因此不输出任何行情结论。」
- 原理：数据的「有没有」和「对不对」必须在进分析之前判定。日期不在 fixture 里就直接失败关闭，不补数、不外推。
- trade-off：失败关闭会让演示少一个漂亮结果，但这是这个项目的红线（AGENTS.md 写了「不吞数据错误」）。面试里宁可展示一次诚实的失败。
- 追问「那用户要的日期怎么办？」：报错信息里会给出可用日期，让用户选，而不是猜一个日期糊过去。
- 失败备用：如果这一步居然成功出了报告，说明 fixture 被改坏了——这是要当 bug 处理的信号，不是「运气好」。

### 第 4 步 拒绝写与外发：拒绝之后必须真的一点都没发生

- 命令：`... demo --step reject_side_effects`
- 屏幕预期：`pending_tool=push_preview`、`pending_capability=control_write`、`arguments_hash=71aa3a...`、`approved=false`，随后 `error_codes=["APPROVAL_REJECTED"]`、`business_writes=0`、`external_sends=0`。
- 原理：**有副作用的工具（写控制状态、写业务文件、对外发送）在执行前必须中断等人批准。**拒绝不是「什么都不做」——它要留下可审计的记录，同时保证业务目录零变化、外发通道零调用。
- trade-off：多一次人工确认，换来的是「永远不会自己把消息发出去」。演示里 sender 直接是 `None`，外发通道根本不存在，所以 `external_sends=0` 是结构上保证的，不是靠自觉。
- 追问「拒绝记录存哪？」：`approvals.jsonl` 里每条都有 `approval_id`、`status`、`tool_name`、`arguments_hash`、`action_hash`——**审批绑定的是参数，不是编号**。只校验编号等于「批准这个编号」，别人换个参数复用编号就漏了。
- 失败备用：`business_writes` 靠对比步骤前后目录快照，如果非 0，直接看 `demo_run.json` 里那一步的 `lines`，对比的路径会写清楚。

### 第 5 步 双阶段批准：批准≠真发

- 命令：`... demo --step approve_two_phase`
- 屏幕预期：`approval_id=...`、`arguments_hash=71aa3a...`、`action_hash=d66ea5da...`、`dry_run=True`、`approved=true`，答复是「push_preview：推送预览已准备（未发送任何外部消息）」，末尾仍是 `external_sends=0`。
- 原理：两阶段 = 先出预览（可审计、可核对参数），再等批准。批准后执行的是**同一个参数哈希**对应的动作；`action_hash` 与 `arguments_hash` 同时出现，是为了让「回显的参数」和「真正执行的参数」能对上。
- trade-off：演示模式即使批准也保持 dry-run。这是刻意的——演示环境的批准只证明链路通，不证明真能发。要真发必须换成真实通道并在受控环境再验一次。
- 追问「怎么防重复发？」：幂等键由动作哈希 + 回执构成，恢复或重放时先查回执，同一参数不会发第二次。
- 失败备用：如果 `external_sends` 不是 0，说明 sender 被注入进来了；检查演示装配是否误传了真实 sender。

### 第 6 步 沙箱三层：先承认它不是隔离

- 命令：`... demo --step sandbox_layer`
- 屏幕预期：
  - `isolation_level=constraint_only`：这一层是**约束层**，不是安全隔离；
  - `untrusted_status=policy_denied`、`untrusted_error=SANDBOX_NOT_EXECUTABLE`、`untrusted_executed=false`：没声明可信来源的代码**连跑都不跑**；
  - `static_blocked=True`、`static_finding=attribute:__subclasses__(line 2)`：恶意样本在静态检查就被点名（精确到行）；
  - `benign_status=ok`、`benign_stdout="14\n"`：良性样本正常出结果；
  - `slow_error=SANDBOX_TIMEOUT`：死循环被超时兜住；
  - `capabilities_supported=7`（process_timeout / stdout_stderr_limit / source_size_limit / environment_allowlist / cwd_confined / static_denylist / process_tree_kill）、`capabilities_unsupported=5`（network_isolation / filesystem_readonly / memory_limit / cpu_limit / untrusted_code_safe）；
  - `docker=unavailable`、`docker_detail=命令不存在：docker`。
- 原理：三层——**信任闸门**（没声明可信就不执行）→ **静态检查**（危险导入/调用直接拒）→ **运行时兜底**（超时、杀进程树、输出上限）。能力矩阵里那 5 个 unsupported 是重点：网络没隔离就写没隔离。
- trade-off：本机没有 Docker/WSL，本该用容器的那一层只能如实报不可用。**绝不回落成约束层冒充隔离**——那会把「没隔离」讲成「有隔离」，是最坏的一种不诚实。
- 追问「那这层到底有什么用？」：它挡的是「模型生成的脚本乱来」这件事里的绝大多数（危险 API、超大输出、死循环、越界路径、读环境变量取密钥），但挡不住真正恶意的本地代码——所以能力矩阵把 `untrusted_code_safe` 标成 unsupported，云端才用容器/gVisor 补。
- 失败备用：如果没有 Docker 又想演示隔离层，讲 `deploy/gvisor/` 与 `deploy/apm/` 的部署脚本和审计断言（它们本机实测停在 unavailable，属于诚实 blocker）。

### 第 7 步 遥测与告警：span 是结构，不是日志

- 命令：`... demo --step trace_alerts`
- 屏幕预期：`trace_id=tr...`、`otel_trace_id=<32 位十六进制>`、`span_count=10`、`node_sequence=["classify_intent", "check_inputs", "rag_retrieve", "plan_tools", "budget_check", "execute_tool", "budget_check", "compose_answer"]`、`recorded_spans=55`、`dropped_spans=0`、`collector=unavailable_on_this_host`、`firing_alerts=[]`、`apm_degraded=true`。
- 原理：一次运行 = 一串 span，节点序列固定，可用来验证「图真的按契约走」。属性走白名单（24 项），提示词、原始参数、header、密钥一律不进 trace。没有 Collector 就标 `apm_degraded=true`，本地留 trace 快照。
- trade-off：`span_count` 的语义是「真被采录并导出的 span 数」，不是「节点数」。宁可老实报 0，也不拿节点数冒充采集数。
- 追问「为什么 trace_id 有两个？」：运行编号（`tr...`）是业务侧的可读编号，`otel_trace_id` 才是 OTel 的 32 位十六进制。**拿运行编号去过滤 span 永远是 0 条**——这个坑踩过，所以两个都打印出来。
- 失败备用：本机没有 Collector 是既定状态；要演示 APM 栈，用 `deploy/apm/docker-compose.yml` 在装了 Docker 的机器上起，再回来看这一屏。

### 第 8 步 断点恢复：恢复不能变成重放副作用

- 命令：`... demo --step resume_checkpoint`
- 屏幕预期：`interrupted=true`、`pending_tool=push_preview`、`pending_from_new_instance=push_preview`、`same_approval_id=true`、`executions_before_resume=1`、`resumed=true`、`side_effect_executions=1`。
- 原理：中断时状态（含待审批项）落在 sqlite checkpoint 里，**新进程重新打开同一个库就能接着等**，不需要原进程还在。恢复后副作用只执行一次，次数由 call_log 计数证明，不靠「应该只有一次」的直觉。
- trade-off：持久化会带来状态文件（`checkpoints.sqlite`、`approvals.jsonl`），所以演示目录必须可清空，且不能和真实业务目录混在一起。
- 追问「中途重启会不会重复发？」：会——如果不用幂等键。所以每个副作用动作都有参数哈希与回执，重放前先查回执。
- 失败备用：`pending_from_new_instance` 为空说明新实例读不到 pending；先确认 checkpoint 路径一致，再确认 `checkpoint_driver=true`。

### 第 9 步 角色隔离：权限靠代码，不靠提示词

- 命令：`... demo --step role_denied`
- 屏幕预期：`supervisor_tools=[]`、`analyst_tools=["daily_pack_preview", "market_snapshot", "policy_rules_read", "positions_snapshot", "readiness_check", "report_search"]`（6 个只读）、`operator_tools=["push_send", "report_write"]`、`role_denied_code=ROLE_CAPABILITY_DENIED`、`role_denied_message=ROLE_CAPABILITY_DENIED: 角色 analyst 不持有能力 control_write，拒绝执行 push_preview；该角色可用能力：read_local, read_external`、`call_log_delta_after_deny=0`、`call_log_total=4`。
- 原理：每个角色只持有自己的能力标签对应的工具 schema。analyst 只持有 `read_local` / `read_external`，写与外发的工具**根本不在它的工具表里**；越权尝试在调用前被挡，而且不留下执行痕迹（`call_log` 增量 0）。
- trade-off：多角色会带来协调成本，所以只做三角色最小集（supervisor 只路由、analyst 只读、operator 只管审批后的写与外发），不做自组织 swarm。
- 追问「和提示词里写『你只能读』有什么区别？」：提示词能被注入绕过，代码层的 schema 过滤不能。这一屏就是证据：工具表里没有那个工具，越权只换来一个错误码。
- 失败备用：如果 `analyst_tools` 里出现写工具，先看注册表装配——空注册表也会让工具缺失，断言必须先把注册表装满再证明「被拦」。

## 3 离线备用模式

| 缺什么 | 演示怎么走 | 屏幕上的诚实标记 |
|---|---|---|
| 没有 LLM / 没有密钥 | 走规则层（L1）与录制回放 | `来源=l1`、模型通道 `NOT_MEASURED` |
| 没有 Docker / WSL | 约束层 + 如实报不可用 | `docker=unavailable`、`docker_detail=命令不存在：docker` |
| 没有 SkyWalking / Collector | 本地 trace 快照 | `collector=unavailable_on_this_host`、`apm_degraded=true` |
| 没有中文 embedding | 只开词法通道 | `rag_mode=bm25`、`rag_degraded=true` |

## 4 演示纪律（说给听众听的三句）

1. **数字只能来自工具返回。** agent 负责听懂、选工具、把结果讲成人话，不负责编数字。
2. **有副作用就必须有人点头。** 写业务文件、对外发送、跑代码，一律先中断等批准。
3. **不可用就说不可用。** 没有 Docker 就说没有 Docker，不要用约束层冒充隔离，不要用本地快照冒充 APM。

## 5 已知限制（主动交代比被问出来强）

| 限制 | 现象 | 为什么现在这样 | 后续怎么补 |
|---|---|---|---|
| 演示目录单进程假设 | 状态文件以单进程演示为目标，未验证多进程并发写 | 演示场景单人单进程 | 增加多进程锁与并发恢复测试 |
| 真实外发未演练 | 演示 sender 为 None | 不在演示环境发真实消息是纪律 | 受控环境用真实通道做一次 exactly-once 验证 |
| 云端隔离未实测 | gVisor 与 APM 栈本机 unavailable | 本机无 Docker、云端 SSH 入口未提供 | 拿到入口后在腾讯云 CentOS 上跑 `deploy/gvisor` 与 `deploy/apm` 的 verify 脚本 |

## 6 复现与回滚

- 复现：`demo --reset` → `demo`（整场）→ `demo --step <id>`（单步）；结构化结果看 `demo_run.json`。
- 回滚：删 `src/pq_agent/demo/`、`tests/agent/test_demo.py`、`docs/agent/DEMO_RUNBOOK.md`、`docs/agent/PITFALLS.md`，并还原 `src/pq_agent/__main__.py` 的 demo 子命令。**不需要清理用户任何真实数据。**

配套阅读：踩坑记录见 [PITFALLS.md](PITFALLS.md)，评测口径见 [EVAL.md](EVAL.md)。
