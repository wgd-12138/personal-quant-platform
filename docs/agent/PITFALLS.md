# 踩坑记录（PITFALLS）

只收录**真实发生过、有证据可查**的坑。每条写六件事：现象、根因、修复、证据、限制、学到的原理。

文中提到的内部验收材料不随公开仓库分发；引用的测试和命令是公开检出可用的复现入口。

这些不是「注意事项」，是面试时可以讲的具体故事：**技术名词谁都会说，能讲清楚哪里坏过、为什么坏、怎么证明修好了，才是真做过。**

前半部分每条都对应演示里的一个步骤（`src/pq_agent/demo/steps.py` 用 `pitfall_ids` 指过来，所以文档和程序不会各说各话）；
后半部分是工程环境类坑，它们不改变业务逻辑，但会把「环境问题」误报成「实现缺陷」。

## 一 技术类

### pit-doctor-green-means-nothing — 启动成功不等于能力可用

- **现象**：APM 那一层曾经只要 `docker compose up` 返回 0 就算「平台就绪」，但容器起来、端口开着、查询接口里面一条数据都没有。
- **根因**：「进程活着」和「数据能进能出」是两件事，代码里却用同一个成功标志表达。
- **修复**：把体检拆成两段——启动检查（进程/端口/healthcheck）只算前半，真正的通过标准是**入库 + 查询 smoke**（能查到刚写进去的那一条）。本机没有 Docker 时不是「通过」而是 `APM_UNAVAILABLE_NO_DOCKER`。
- **证据**：`agent_eval/results/apm_environment.json` 里四层状态全 `unavailable`，三个 signal 的 `supported=None` 且 `code=APM_SIGNAL_UNVERIFIED`；演示第 1 步四项 check 与第 7 步 `collector=unavailable_on_this_host` 互相印证。
- **限制**：本机永远拿不到「真实通过」的证据，只能交配置与审计断言，这一点在文档里明写为 blocker。
- **原理**：**门禁必须长在「能力」上，不能长在「命令退出码」上。**

### pit-bm25-called-rag — 只有词法通道却说自己是混合检索

- **现象**：语料评测里「混合检索」的数字比单路 BM25 更差：等权 RRF 的 recall@5 只有 58%，而单路 BM25 是 86%。
- **根因**：两路召回质量差距很大时，等权融合会把差的那一路的垃圾结果顶到前面。本机又没有中文 embedding 权重，向量通道其实是空的。
- **修复**：权重可配置并默认词法优先（0.75/0.25）；没有语义后端时明确标 `profile=lexical_only`、`degraded=true`，并给出理由字符串「lexical_only：本次检索只有 BM25 词法通道，语义召回未启用（配置显式选择，非静默降级）」；评测门槛**只对真实存在的词法档设置**，向量/混合档明确写「不设门槛、不判定」。
- **证据**：`agent_eval/results/rag_baseline.json` 三通道数字（bm25 hit@5 0.7000 / vector 0.2000 / hybrid 0.7000，mrr 0.6083 / 0.1567 / 0.6122）；演示第 2 步屏幕上的 `rag_mode=bm25`、`rag_degraded=true`。
- **限制**：语义通道的收益在这套语料上无法验证——没有后端就不能给结论。
- **原理**：**没有后端的能力要标成 unsupported，而不是「效果不太好」。**「效果差」和「没启用」是完全不同的两句话，面试官会追问到底哪一个。

### pit-model-channel-unmeasured — 模型通道没测就写「通过」

- **现象**：意图识别评测里 L2（模型）通道因为本机没有 `DEEPSEEK_BASE_URL`，状态是 `NOT_MEASURED`，而不是 pass 或 fail。
- **根因**：验收表里只有「通过/不通过」两格，第三格（没条件测）会被默认填成通过。
- **修复**：给评测结果增加第三态 `NOT_MEASURED` 并让它在报告里显式可见；`classify_intent` 在模型不可用时返回 `fallback` 加 `INTENT_L2_UNAVAILABLE`，**绝不伪造回答**。
- **证据**：`agent_eval/results/intent_eval.json` 的 `l2_status=NOT_MEASURED`；规则层实测 dev 45 样本端到端准确率 0.9556、test 50 样本 0.9600、槽位准确率 dev 1.0000 / test 0.9412。
- **限制**：真实模型的准确率至今没有数字，只有规则层的。
- **原理**：**验收要有第三态。**二值验收会把「没测」洗成「通过」，这是最容易被面试官抓住的一类造假。

### pit-fabricated-data — fixture 缺数据时补一个数把流程糊过去

- **现象**：金标准用例里注入了真实代码 `000725`，而演示数据集只有合成代码（`DEMO-BK-01..12` 等），构建器直接拒绝出报告，报 `ValueError: 持仓缺少真实行情: 000725`。
- **根因**：用例作者想要「看起来真实」，于是往 fixture 里塞真实股票代码，而 fixture 里没有对应行情。
- **修复**：fixture 里就写空持仓，并在用例里加 `fixture_positions_note` 说明「照 demo 数据集灌真实代码是在测 fixture 缺口而不是测 agent」；持仓读取路径由专门的只读用例覆盖。缺数据的正确表现是**失败关闭**。
- **证据**：演示第 3 步 `AS_OF_NOT_IN_FIXTURE: 2026-07-08 不在 fixture 中；可用日期：2026-07-09`，答复里「本次没有拿到可用数据，因此不输出任何行情结论。」
- **限制**：演示数据集是合成的，不能用来证明任何真实选股能力——这一点在 fixture 与文档里都写明。
- **原理**：**让数据缺口暴露出来，而不是用一个假数字把它盖住。**补一个数能过测试，但会把「数据不齐」变成「系统给出了结论」，性质完全不同。

### pit-approval-only-id — 审批只认编号，换个参数照样放行

- **现象**：早期审批接口只校验 `approval_id` 是否等于待批项编号，参数改了编号不变，于是「批准这件事」被降级成「批准这个编号」。
- **根因**：把审批当成一次布尔点击，没有把「批准的对象」绑定进去。
- **修复**：审批记录同时存 `arguments_hash`（参数指纹）与 `action_hash`（动作指纹）；恢复或重放时重算参数哈希并比对，不一致直接拒绝（`TOOL_APPROVAL_REQUIRED: ... 的审批与实际参数不匹配`）。审批接口必须回显参数哈希。
- **证据**：演示第 5 步屏幕同时打印 `arguments_hash=71aa3a69...` 与 `action_hash=d66ea5da...`；`approvals.jsonl` 每条记录含 `approval_id` / `status` / `tool_name` / `arguments_hash`。
- **限制**：哈希只能证明「参数没变」，不能证明「批准人理解了自己批的是什么」——所以预览必须给出人话（`risk_note`）。
- **原理**：**审批绑定的是内容，不是编号。**

### pit-approved-means-sent — 批准之后顺手当成真发

- **现象**：批准路径一旦接通，最省事的实现就是「批准就直接调发送」。
- **根因**：把「链路通了」和「允许外发」混为一谈。
- **修复**：演示模式即使批准也保持 `dry_run=True`，答复写死「推送预览已准备（未发送任何外部消息）」；外发工具的发送者默认是 `None`，外发通道结构上不存在；工具带 `notify_external` 能力标签，不可重试。
- **证据**：演示第 5 步 `dry_run=True` 且 `external_sends=0`；第 4 步拒绝后同样 `external_sends=0`。
- **限制**：真实飞书的 exactly-once 至今没有在真机验证过（演示不发真消息），属明确未解决风险。
- **原理**：**批准改变的是「允许执行」，不是「默认执行」。**

### pit-constraint-called-isolation — 把约束层说成隔离层

- **现象**：本机沙箱用 `subprocess` 加静态检查，被当成「安全隔离」对外表述；实测 `socket.connect_ex` 返回 10035（WSAEWOULDBLOCK），证明**网络连接真的发出去了，根本没有网络隔离**。
- **根因**：安全属性是用「我限制了哪些东西」推断的，不是用「绕不过」证明的。
- **修复**：把这一层正名为 `isolation_level=constraint_only`，能力矩阵里 5 项明确标 unsupported（`network_isolation` / `filesystem_readonly` / `memory_limit` / `cpu_limit` / `untrusted_code_safe`）；没声明可信来源的代码**连执行都不进入**（`trusted_fixture=False` 得到 `policy_denied` 加 `SANDBOX_NOT_EXECUTABLE`）；Docker 不可用时如实报不可用，**绝不回落成约束层冒充隔离**。
- **证据**：演示第 6 步 `constraint_only`、`untrusted_executed=false`、`capabilities_supported=7` / `capabilities_unsupported=5`、`docker=unavailable`；`tests/agent/sandbox/test_local.py` 记录了 10035 这个实测值。
- **限制**：本机没有 Docker / WSL，容器层与 gVisor 层都停在 `unavailable`，隔离能力没有真机证据。
- **原理**：**能力矩阵必须留 unsupported。全是 supported 的矩阵等于没写，因为它没法回答「你这层到底挡不住什么」。**

### pit-local-span-as-apm — 本地 span 快照冒充 APM 平台

- **现象**：没有 Collector 时，本地攒了一堆 span，报告里很容易写成「已接入 APM」。
- **根因**：把「产生了 trace 数据」和「平台能采、能查、能告警」混在一起。
- **修复**：本机状态显式标 `degraded=true` 并写明原因；L3 评测层诚实记 `unknown` 并保留 blocker（`no_docker_on_this_host` / `collector_health:unavailable` / `oap_query:unavailable` / `ui_reachable:unavailable` / `end_to_end:unavailable`），blocker 不参与整体 ok 但不能被吞掉；报告里加一句「与 docker 相关的项表示本机没有 APM 平台，L3 数字只能来自本地 trace 快照，不能当 SkyWalking 证据」。
- **证据**：演示第 7 步 `collector=unavailable_on_this_host`、`apm_degraded=true`、`dropped_spans=0`；`agent_eval/run_all.py` 的 `layers.l3=unknown` 与五个 blocker。
- **限制**：SkyWalking 的入库、查询、告警链路的真机证据为零。
- **原理**：**「本地有数据」不等于「平台可用」。降级要写在最显眼的地方。**

### pit-resume-double-execute — 断点恢复把副作用又执行一遍

- **现象**：中断后从 checkpoint 恢复，若不加防护，副作用动作会再跑一次（真实场景就是重复推送）。
- **根因**：把「恢复」实现成「从头再走一遍」，而不是「从断点接着走」。
- **修复**：有副作用的节点执行前中断，恢复时先查回执与参数哈希，命中则跳过；副作用执行次数由 `call_log` 计数证明。
- **证据**：演示第 8 步 `pending_from_new_instance=push_preview`、`same_approval_id=true`、`executions_before_resume=1`、`side_effect_executions=1`；跨进程恢复的实验记录在 `tests/agent/test_restart_recovery.py`。
- **限制**：演示是单进程内存会话加磁盘 checkpoint，多进程并发恢复没有验证。
- **原理**：**恢复的正确性要用「执行了几次」来证明，不能用「应该只执行一次」来断言。**

### pit-role-by-prompt — 权限写在提示词里

- **现象**：让「分析师」不要调用写工具，最省事的写法是在系统提示词里嘱咐一句。
- **根因**：把安全边界寄托在模型会不会听话上。
- **修复**：角色权限落在**工具 schema 过滤**上——每个角色只持有自己能力标签对应的工具，越权尝试在调用前被拒（`ROLE_CAPABILITY_DENIED`），且不产生执行痕迹；`RoleRegistry` 用组合而不是继承（继承会让人拿基类引用绕过角色检查），守卫放在调用真注册表之前（顺序反了会先撞「需要审批」，看起来像「批了就能做」）。
- **证据**：演示第 9 步 `supervisor_tools=[]`、`analyst_tools` 6 个只读、`operator_tools=["push_send","report_write"]`、`call_log_delta_after_deny=0`；`src/pq_agent/roles/role_registry.py` 与 37 个权限用例。
- **限制**：三角色是最小集，不自组织、不并发；角色之间的交接只传摘要不传原文。
- **原理**：**提示词是建议，代码才是边界。**顺带一个反例教训：如果注册表是空的，「分析师看不到写工具」也会变绿——那是工具没装，不是被拦住，绿色必须先把注册表装满再断言。

## 二 工程环境类

### pit-tempfile-0700 — 本机沙箱下临时目录自己把自己锁死

- **现象**：全量测试出现 87 个错误，清一色 `PermissionError: [Errno 13] Permission denied: ...\expected_dates.txt`；随后清理阶段报 `[WinError 5] 拒绝访问`。看起来像产品有 87 个 bug。
- **根因**：实测 `os.mkdir(path, mode)` 权限矩阵——默认 / 0o777 / 0o755 / 0o750 都能写，**0o700 直接把创建者自己关在门外**（`icacls` 报 Access is denied，对该目录 `os.chmod` 报 WinError 5）。Python 的 `tempfile.mkdtemp()` 与 `TemporaryDirectory()` 固定用 `os.mkdir(file, 0o700)`，于是所有用临时目录的代码一起中招。
- **修复**：测试不再用 `tempfile`，统一用 `<repo>/.tmp/<prefix>/<uuid8>`；为了让「真实语义基线」跑得出来，写了一个只把 0o700 映射成 0o750 的垫片脚本（`agent_eval/run_unittest_shim.py`），仓库产品与测试代码零改动。
- **证据**：`agent_eval/run_unittest_shim.py`；垫片前 failures=3/errors=87，垫片后 failures=3/errors=1，修完 4 个过期测试后 **Ran 150 → OK**。
- **限制**：本机用原始命令（不带垫片）跑全量测试永远会有一批环境错误，报告里必须如实写明「干净主机上应全绿」。
- **原理**：**先分清「环境不对」和「代码不对」，再动手改代码。**87 个错误一个都不在业务逻辑里。

### pit-exit-code-zero — 退出码 0 是假值，被判成失败

- **现象**：四层评测里 RAG 那层明明跑成功，状态却是 `failed`，且 blocker 文案冒号后面是空的。
- **根因**：判断写成 `int(exit_code or 1) == 0`——退出码 0 是假值，`0 or 1` 得到 1，于是成功被判失败；错误信息又只列失败门槛（全过），所以后面什么都没有。
- **修复**：抽一个显式转换 `_exit_ok(exit_code)`：`None` 返回 False，其余 `int() == 0`；intent 分支同样接入（子进程挂了但磁盘上留着上次成功结果时，只读结果文件会给出假绿色）。
- **证据**：`tests/agent/eval/test_runner.py` 里有 `_exit_ok` 的回归用例（0 与 "0" 为 True，1 与 None 与 "boom" 为 False）与源码静态守卫；修后四层 `ok=true`。
- **限制**：这类假值陷阱在 Python 里到处都有（0、空串、空列表、空字典），只能靠「参与布尔判断前显式转换」这条纪律防。
- **原理**：**凡是「成功值等于假值」的接口，都必须显式转换。**

### pit-network-guard-self-pipe — 熔断器掐掉了 asyncio 自己的管道

- **现象**：整场演示跑到一半失败，错误是 `OUTBOUND_NETWORK_DENIED: demo_offline 禁止网络连接 ('127.0.0.1', 11723)`，端口每步递增，还伴随 `coroutine ... was never awaited` 与 `AttributeError: 'ProactorEventLoop' object has no attribute '_ssock'`。看起来是「演示居然偷偷连网了」。
- **根因**：Windows 的 ProactorEventLoop 构造时用 socketpair 连自己一次；而演示把整场套在网络熔断器里，`asyncio.run` 在闸门内建循环，于是被自己的熔断器掐死。这个坑在 A1-07 已经踩过一次（当时的做法是「只把同步数据层调用罩进闸门」）。
- **修复**：进闸门之前先 `asyncio.new_event_loop()` 建好循环，`finally` 里关闭；等待改成 `run_until_complete`。修后 8/9 通过、外呼 0。
- **证据**：`docs/agent/DEMO_RUNBOOK.md` 的实测记录；演示末尾 `外呼尝试：0（必须为 0）`。
- **限制**：这类「安全机制把自己人拦了」的问题，必须用真实整场运行才能暴露，单测发现不了。
- **原理**：**零外呼要能证明，而熔断器是最简单的证明手段——但它必须先学会区分「自己人」。**

### pit-fake-signature-green — 假实现签名和真实现不一致，给出虚假绿色

- **现象**：`graph.py` 的 `execute_tool` 调了一个**根本不存在的方法**（`ToolSpec.run`），运行时报 `AttributeError: ToolSpec object has no attribute run`，被判成 `TOOL_FAILED`。这个缺陷从 A1-03 起活了三个任务才被抓到。
- **根因**：图测试一直用 `tests/agent/_graph_fakes.py` 里的假工具（`get()` 返回带 `run` 的假对象），假实现比真实现「更宽松」，于是真实现里的死代码永远是绿的。
- **修复**：改成走真注册表的 `preview()` 取参数哈希、再 `invoke()`；同时在假实现与真实现之间加了签名一致性检查，避免下次再错开。
- **证据**：`agent_eval/run_all.py` 的 SUMMARY 记录了这条；A1-07 也把它列为最值钱的学习记录之一。
- **限制**：这类问题无法靠「多写单测」解决，只能靠「至少有一条走真实现的端到端路径」。
- **原理**：**假实现只能替数据，不能替接口。**一旦假实现比真实现更宽松，测试就从「验证」变成了「自我安慰」。

### pit-fixture-real-code — 用真实代码去测合成数据

- **现象**：金标准用例注入真实股票代码 `000725`，demo 数据集里只有合成代码，构建器按红线拒绝出报告，判题器报「未声明错误码 TOOL_FAILED」。
- **根因**：把「用例看起来真实」当成了「用例正确」。
- **修复**：fixture 里写空持仓加说明文字，让「缺数据」由专门的用例去验证（属 `pit-fabricated-data` 的同族）。
- **证据**：`agent_eval/cases/golden_tasks.json` 的 `fixture_positions=[]` 与 `fixture_positions_note`。
- **限制**：演示与评测都只能用合成数据。
- **原理**：**fixture 的职责是稳定复现，不是仿真。**想要真实数据，就得连真实数据源和它的缺口一起面对。

### pit-corpus-logs — 语料里混进测试日志，检索被高频词带偏

- **现象**：RAG 评测的 badcase 返回的多是日志文件和错误小节，一条日志被切成 200 多块，占了整个索引的绝大多数块。
- **根因**：语料收集按目录递归，把 `historical test logs` 也收进去了；高频中文词把词法打分拉偏。
- **修复**：清理测试污染目录、重建 manifest 与索引（136 文档 / 27 拒绝，`index_hash=cda801a8...`），并把 evidence 目录排除在语料之外。**顺序不能反**：先重建索引 → 再重定用例指纹 → 最后写基线，否则基线取的是用例文件里的旧指纹。
- **证据**：`agent_eval/rebuild_rag_index.py` 的 `index_hash` 与 `corpus_hash`；`agent_eval/run_all.py` 的清理记录。
- **限制**：语料依然是仓库内文档，不含真实研报，召回率的绝对水平不能外推。
- **原理**：**检索效果差，先查语料，再查算法。**语料脏的时候调权重是白费力气。

### pit-python-hash-salt — 同一份索引两次评测结果不一样

- **现象**：向量通道的 hit@1 一次是 0.1333，重跑变成 0.0667，别的都没改。
- **根因**：本地假嵌入器用内置 `hash()` 算桶，而 Python 对字符串哈希按进程加盐（`PYTHONHASHSEED`），同一份数据在不同进程分到不同的桶。
- **修复**：改用 `sha256` 取模算桶；用 `PYTHONHASHSEED=0 / 12345 / 未设置` 三次跑出同样结果。
- **证据**：`src/pq_agent/rag/embedder.py` 的 `bucket()`；`agent_eval/run_rag_eval.py` 记录了三次对照。
- **限制**：确定性只保证了「可复现」，不保证「语义正确」——它本来就是占位后端，所以永远标 `vector_available=false`。
- **原理**：**结果不可复现时，先怀疑随机源，再怀疑算法。**评测脚本必须能跑出同一个数。

### pit-print-path-as-secret — 把写入路径当成密钥泄漏

- **现象**：判题器报「命中 1 处敏感串」，命中内容是 `windows_user_path`——来自成功结果里的目标文件绝对路径。
- **根因**：脱敏规则把「用户目录路径」一律判成敏感，而路径本身是定位问题的必需信息，不是凭据。
- **修复**：判题器增加「仅路径类」豁免（`PATH_ONLY_SECRET_PATTERNS`），并让失败信息**列出具体命中的串**（原来只报个数，badcase 根本没法定位）；私钥、webhook、bearer、JWT、`sk-` 密钥仍然零容忍。
- **证据**：`agent_eval/judge.py` 的 `_is_path_only_secret()`；金标准 gt-06 从失败变通过。
- **限制**：豁免清单是白名单思路，新增一类敏感串就必须同步更新规则。
- **原理**：**脱敏要能区分「敏感」和「有用」。**一刀切会导致要么漏，要么把正常输出也判死。

### pit-backslash-unicodeescape — docstring 里的路径反斜杠让文件语法错误

- **现象**：`src/pq_agent/sandbox/docker_runtime.py` 直接报 `SyntaxError: (unicode error) 'unicodeescape' codec can't decode bytes in position 524-525: truncated \UXXXXXXXX escape`。
- **根因**：注释里照抄了 Windows 路径（C 盘用户目录那一段），Python 三引号把反斜杠当 Unicode 转义解析。
- **修复**：把该句改写成不含反斜杠的描述（「工程本身就在用户目录下」），**并且删掉了那个必然误伤的检查项**——工程本来就在用户目录里，任何拒绝「用户目录挂载」的规则都会误伤自己。
- **证据**：`tests/agent/sandbox/test_docker.py` 的探针输出（三条非法挂载分别被 `SANDBOX_POLICY_DENIED` 拒绝，合法挂载与零挂载都通过）。
- **限制**：涉及路径的规则必须相对**工程根**判定，不能用「像不像用户目录」这种绝对判断。
- **原理**：**安全规则也要被自己的仓库检验一遍，否则第一条挡住的就是自己。**

### pit-single-source-docs — 文档和程序各说各话

- **现象**：文档里写的错误码（`CORPUS_ERROR_SECRET_SHAPED`）和代码实际抛的（`CORPUS_SECRET_SHAPED`）不一样；文档写的章节数比构建器少两个。
- **根因**：文档是手写的，程序是另写的，两边都「看起来合理」，于是慢慢漂移。
- **修复**：把「屏幕上必须出现什么」做成**程序里的数据**（`src/pq_agent/demo/steps.py` 的 `markers`），文档照着它写，验收脚本反过来读它去检查真实输出；任务计划里的状态也由验收命令的输出决定（没有命令输出不得标完成）。
- **证据**：`tests/agent/test_demo.py` 里对每一步 marker 的断言；本文件每条 `pit-*` 编号都被 `steps.py` 的 `pitfall_ids` 引用，缺一条就会被验收脚本抓出来。
- **限制**：这样做要求「文档里的承诺」是可机器校验的字符串，纯叙述性内容仍然靠人。
- **原理**：**能自动对账的东西，别靠人记得改。**

## 三 怎么用这份文档

- 演示前：挑对应步骤的 `pit-*` 再读一遍，问答环节大概率会问到。
- 复核时：`tests/agent/test_demo.py` 会检查本文件是否收录了演示步骤引用到的全部编号——**文档少写一条，验收就会失败**。
