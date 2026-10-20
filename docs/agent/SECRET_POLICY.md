# SECRET_POLICY.md | pq_agent 密钥与敏感数据策略

> 任务：A0-04（密钥与路径策略）
> 适用范围：src/pq_agent/ 全部模块；对 src/personal_quant/ 只做读取与约束，不改其行为。
> 原则：默认不收集、不落盘、不上报；一旦要出门（日志 / trace / checkpoint / RAG / 外部 API），必须先过统一闸门；闸门失败就丢数据，不降级。

## 1. 结论

1. 敏感数据只有两类处置方式：整体替换为标记，或根本不进入系统。没有"部分保留"这种选项。
2. 所有"出门路径"共用一个闸门 guard_payload()，它先脱敏再自检；自检发现残留就抛 SecretLeakError，调用方无法绕过。
3. 路径是安全边界的一部分：所有文件访问必须走 resolve_within_root() / read_within_root()，..、绝对路径、UNC、设备路径、链接组件一律拒绝。
4. 凭证只在 notify adapter 内解析（resolve_feishu_webhook()），永远不作为模型的 schema、state、checkpoint 字段。模型看不见密钥，所以也编不出来、泄露不了。

## 2. 数据分级

| 级别 | 典型内容 | 能否进日志 | 能否进 trace | 能否进 checkpoint | 能否进 RAG |
|---|---|---|---|---|---|
| secret | API key、webhook URL、Authorization 头、私钥、token | 否（只留指纹或 present/absent） | 否 | 否 | 否 |
| user_data | data/positions.csv、真实持仓、导入的券商导出 | 否（只留数量与校验和） | 否 | 否 | 否 |
| generated_report | reports/、logs/ 里的日报与运行日志 | 可，但须过闸门 | 可，但须过闸门 | 可 | 可（公开版本） |
| control_plane | planning/runs/ 的运行审计、receipt、state | 可 | 可 | 可 | 可 |
| query | 用户原始提问、工具完整参数 | 只留摘要 | 只留意图与槽位 | 只留滑动窗口 | 需人工确认 |
| public | 文档、源码、示例 | 可 | 可 | 可 | 可 |
| restricted | 异常原文、HTTP 响应体、请求头、堆栈 | 否 | 否 | 否 | 否 |

分级由 pq_agent.security.paths.classify_path() 对路径判定，避免同一份文件在不同模块被当成不同级别。

## 3. 敏感字段清单

**字段名命中即整体替换**（比较前先统一小写并去掉非字母数字，所以 X-Api-Key、x_api_key、xapikey 是同一个键）：apikey、token、authorization、cookie、secret、password、privatekey、credential、sessionid、signingkey、webhook、webhookurl、xapikey、proxyauthorization 等，完整集合见 SENSITIVE_KEY_NAMES。

**已知密钥值**（环境变量里的）会被登记为"已知明文"，出现即整体替换：FEISHU_WEBHOOK_URL、OPENAI_API_KEY、DEEPSEEK_API_KEY、LLM_API_KEY、ANTHROPIC_API_KEY。

**形状命中**（不知道值时也能认出来）：私钥块、飞书 webhook URL、Bearer / token 后的长串、JWT、sk- 开头的 key、key=value 形式的赋值、Windows 与 Unix 用户目录、邮箱。

## 4. 统一闸门

```python
from pq_agent.security import guard_payload, collect_secrets_from_environ

secrets = collect_secrets_from_environ()          # 只登记值，不打印
safe = guard_payload(payload, secrets=secrets, where="checkpoint")
```

| 行为 | 结果 |
|---|---|
| 正常载荷 | 返回脱敏副本，原对象不变 |
| 出现已知密钥或敏感形状 | 替换为 [REDACTED:...] |
| 出现未知的对象类型 | 抛 REDACTION_UNSUPPORTED_TYPE（绝不 repr()） |
| 嵌套超过 ScrubPolicy.max_depth | 抛 REDACTION_DEPTH_EXCEEDED |
| 脱敏后仍有残留 | 抛 SecretLeakError("SECRET_LEAK_BLOCKED: <where>: ...") |
| 脱敏器自身异常 | 抛 REDACTION_FAILED: <where>: ... |

**这是 fail-closed，不是 best-effort**：闸门只会给出"干净的数据"或"异常"，不会给出"大概干净的数据"。


## 5. 异常处理

外部调用失败时，**不得把异常原文直接写进日志、trace、checkpoint 或返回给模型**。原因很实际：src/personal_quant/notifier/feishu.py:79 的 FEISHU_WEBHOOK_HTTP_ERROR:{code}:{response_text} 会把响应体带出来，src/personal_quant/data_center/providers.py:225 的 数据源请求失败: {url} 会把完整 URL 与 query 带出来，这些文本一旦进 trace 就等于把外部系统的响应写进了可观测系统。

统一改用：

```python
from pq_agent.security import safe_error_summary

summary = safe_error_summary(exc, secrets=secrets)
# {"error_type": "RuntimeError", "error_code": "FEISHU_WEBHOOK_HTTP_ERROR", "message": "400:..."}
```

- error_code 取异常文本里冒号前的全大写码（既有代码风格就是 CODE:detail），取不到就退化成异常类名。
- message 过完 scrubber 才返回，长度上限 300。
- 摘要生成自身失败时返回 ERROR_REDACTION_FAILED 空摘要，仍然 fail-closed。

## 6. 控制面数据的保存边界

| 数据 | 保存位置 | 访问权限 | 保留期 | 清理策略 |
|---|---|---|---|---|
| 密钥明文 | 只存 Windows 用户环境变量（既有做法） | 当前用户 | 用户自管 | 绝不写入仓库、日志、报告 |
| 密钥指纹 | 通道测试报告（12 位 sha256，既有做法） | 本地文件 | 随报告 | 覆盖式重写 |
| trace / span 属性 | OTLP exporter → Collector | 本地 | 由后端配置 | 属性白名单 + 禁止自动捕获 header |
| checkpoint | LangGraph SqliteSaver | 本地 | 按会话 | 必须进 .gitignore，不得提交 |
| 向量索引 | RAG 索引目录 | 本地 | 重建即覆盖 | 只索引仓库内可信文档 |
| 用户原始提问 | 仅内存 | - | 单次请求 | 不落盘；要落盘须过闸门且只剩意图与槽位 |

**真实用户资料不得默认进入 RAG 或演示**：data/ 下的真实持仓不进语料；演示数据一律用 tests/fixtures/ 里带"仅演示/评测"标识的构造数据。

## 7. 路径策略

```python
from pq_agent.security import resolve_within_root, read_within_root

path = resolve_within_root("planning/runs/run_summary.md", must_exist=True)
path, data = read_within_root("pyproject.toml", max_bytes=100_000)
```

| 输入 | 错误码 |
|---|---|
| 含 .. 段 | PATH_TRAVERSAL_DENIED |
| 绝对路径（默认） | PATH_ABSOLUTE_NOT_ALLOWED |
| C:src/x | PATH_DRIVE_RELATIVE_DENIED |
| UNC 与设备路径 | PATH_NETWORK_DENIED |
| 跨盘 | PATH_DRIVE_MISMATCH |
| CON / NUL / COM1 / LPT9 等设备名 | PATH_RESERVED_NAME |
| config/local.yaml、.env | PATH_SECRET_DENIED |
| 含 NUL 字节 / 空串 / 非字符串 | PATH_NUL_BYTE / PATH_EMPTY / PATH_INPUT_TYPE |
| 解析后落在项目根外 | PATH_ESCAPE_DENIED |
| 路径中任一组件是符号链接或 junction | PATH_SYMLINK_DENIED |
| must_exist 但不存在 | PATH_NOT_FOUND |
| 超过 max_bytes | PATH_TOO_LARGE |

**TOCTOU**：read_within_root() 先 os.stat 再 os.open，然后用文件标识（Windows 上用 st_dev+st_ino，拿不到 inode 时退化为大小+修改时间）比对两次结果，不一致即抛 PATH_CHANGED_DURING_OPEN。测试通过 _between_stat_and_open 钩子在 stat 与 open 之间替换文件来确定性复现。

**为什么链接一律拒绝而不是"解析后判断是否越界"**：链接的目标随时可被改指，检查与使用之间存在时间窗；对 Agent 生成的路径来说，拒绝一个链接的成本远低于放过一次越界写。


## 8. 凭证只走 notify adapter

| 规则 | 说明 |
|---|---|
| 凭证不进模型上下文 | 密钥不是 tool schema 字段、不是 state 字段、不是 checkpoint 字段 |
| 凭证不进工具参数 | 工具只接收 slot、as_of 这类业务参数；webhook 由 adapter 自己解析 |
| 凭证不进返回值 | 工具结果是结构化 JSON，不含任何 URL 或 token |
| 模型不得选择是否真发 | live 只能由人工确认节点置位，不接受模型输入 |
| 已配置与否可以告诉模型 | 只返回 present / absent |

## 9. 数据分级与提示注入是两件不同的事

这是本任务最想记下来的一条：

- **数据分级 / 脱敏**解决的是"我们的数据不要流出去"：控制的是出口，对象是密钥、持仓、内部路径。它的失效模式是"泄露"。
- **提示注入防护**解决的是"别人的文本不要当指令执行"：控制的是入口，对象是新闻、网页、文档。它的失效模式是"被操纵"。
- 二者都靠"隔离"，但方向相反：前者是输出方向的白名单，后者是输入方向的可信度降级。**只做其中一个都是漏的**：脱敏做得再好，注入的文本照样能指挥模型去调用工具；输入包裹做得再好，工具返回里的密钥照样会进 trace。
- 所以本项目的口径是：外部文本用 <DOC> 包裹并在 system 里声明"仅参考"，**同时**所有出口过 guard_payload()。前者的判定在提示词层，**但工具权限的判定必须在代码层**——因为提示词是可以被说服的，代码不行。

## 10. 验证方式

| 验证项 | 做法 | 结果 |
|---|---|---|
| 五类产物零命中 | 用金丝雀密钥构造 logs / trace / checkpoint / ToolResult / API response（含 Authorization 头与 X-Api-Key），过闸门后扫描命中数 | 0 |
| 扫描器非空转 | 同一批产物**未脱敏**时必须被命中 | 全部命中 |
| fail-closed | 未知类型、超深嵌套、脱敏残留 | 三种都抛异常，无原文返回 |
| 路径拒绝表 | 上表每一类输入逐一断言错误码 | 全部按预期拒绝 |
| 链接拒绝 | 注入检测器 + 真实 junction（mklink /J，无需管理员） | 均拒绝 |
| TOCTOU | stat/open 之间替换文件 | PATH_CHANGED_DURING_OPEN |
| 符号链接 | 真实创建需要特权，本机 WinError 1314 | 跳过并记录原因，由 junction 用例覆盖同一代码路径 |

运行方式：

```powershell
$env:PYTHONPATH='src;tests'; python -m unittest agent.security.test_redaction agent.security.test_paths
```

## 11. 已知未覆盖 / 风险

1. **检测依赖已知值 + 形状**：一个既不匹配任何形状、也没登记进 secrets 的私人字符串（比如自造的短口令）不会被认出来。缓解方式是启动时用 collect_secrets_from_environ() 把所有真实密钥登记进去。
2. **真实符号链接未实测**：本机权限不足（WinError 1314），只测到 junction 与注入检测器。二者走的是同一段拒绝逻辑，但"OS 能不能认出符号链接"在本机没有实测证据。
3. **scrub_text 的正则可能过宽**：比如 Bearer 规则会把任何 token 后的长串替换掉，正常文本里出现类似形状会被误伤。脱敏宁可误伤不可漏放，但调用方应注意不要在正文里使用这种形状。
4. **闸门只管调用点**：绕过 guard_payload() 直接写文件的代码不受保护。约束手段是代码评审 + 后续阶段的工具层统一出口。

## 12. 回滚

只撤销本任务新增的文件即可，不涉及 src/personal_quant/：

```powershell
Remove-Item -Recurse -Force src\pq_agent\security
Remove-Item -Recurse -Force tests\agent\security
Remove-Item docs\agent\SECRET_POLICY.md
Remove-Item -Recurse -Force planning\agent\evidence\A0-04
```
