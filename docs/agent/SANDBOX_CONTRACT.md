# SANDBOX_CONTRACT | LocalRuntime 的约束合同

> 一句话：本机能给的约束是「限时、限量、限环境、限目录、静态挡刀」，给不了的是「隔离」。

## 1. 为什么需要这一层

agent 迟早要跑代码：算相关性、跑回测、解析不可信新闻。没有这一层，模型生成的代码就以当前用户身份、带着完整环境变量、在项目根目录里跑。有了这一层，至少能把「能跑什么、跑多久、留下什么」变成代码层的硬约束，而不是提示词里的嘱咐。

| 问题 | 没有这一层 | 有这一层 |
|---|---|---|
| 死循环 | agent 卡死 | 超时后终止进程树 |
| 打印海量输出 | 撑爆内存与日志 | 按字节截断并标记 |
| 读到飞书 webhook | 密钥随手可取 | 环境白名单，注入即拒 |
| 写到项目任意位置 | 污染真实数据 | 每次执行独立临时目录，用完删除 |
| 模型被注入后乱写代码 | 直接执行 | 静态层先挡，且不声称这是安全 |

## 2. isolation_level 说明（必须如实转述）

`isolation_level` 固定为 **`constraint_only`**（约束层），**不是** `isolation`（隔离）。

这个字段会一路传到 API 响应、UI、trace 与验收证据里。任何地方都不允许把它写成「安全隔离」「沙箱隔离」。理由：本机没有容器、没有 gVisor、没有独立内核，进程仍以当前用户身份运行、仍能读写用户能读写的文件、仍能访问网络。把它叫安全隔离，就是在面试里说一个当场会被拆穿的谎。

`local_report()` 里附带 `can_execute_untrusted: False`，意思是：不可信代码在本机一律拒绝执行，不是因为被隔离安全了，而是因为根本没执行。

## 3. 能力矩阵（12 项，逐条可查）

| 能力 | 状态 | 由谁保证 |
|---|---|---|
| `process_timeout` | supported | 子进程超时后终止进程树 |
| `stdout_stderr_limit` | supported | 按字节上限截断 |
| `source_size_limit` | supported | 源码与 stdin 字节上限 |
| `environment_allowlist` | supported | 只传白名单环境变量 |
| `cwd_confined` | supported | 每次执行独立临时目录 |
| `static_denylist` | supported | AST 黑名单快速拒绝 |
| `process_tree_kill` | supported | Job Object + taskkill /T |
| `network_isolation` | **unsupported** | 无（实测 connect 会真的发出去） |
| `filesystem_readonly` | **unsupported** | 无 |
| `memory_limit` | **unsupported** | 无 |
| `cpu_limit` | **unsupported** | 无 |
| `untrusted_code_safe` | **unsupported** | 无 |

请求里 `require=("network_isolation",)` 这类不可用能力时，默认直接拒绝执行（`SANDBOX_CAPABILITY_UNSUPPORTED`）；只有显式写 `allow_unsupported=True` 才放行，并且结果里照旧标 `constraint_only`。**绝不静默降级。**

## 4. 四种结果不能混为一谈

| status | 含义 | executed |
|---|---|---|
| `ok` | 真的跑了，退出码为 0 | True |
| `failed` | 真的跑了，退出码非 0 或超时 | True |
| `static_blocked` | 静态层拒绝，**代码从未运行** | False |
| `policy_denied` | 策略拒绝（不可信代码、超限、dry-run、能力不支持） | False |

这条区分是踩坑换来的：早期把脚本路径写成相对路径，子进程连文件都没打开就以退出码 2 结束，结果被记成 `SANDBOX_NONZERO_EXIT` + `executed=True`——「没跑起来」被误报成了「跑失败」。任何"没执行"的情况都不允许带上退出码或输出，`ExecResult` 在构造时会直接拦住这种记录。

## 5. 静态层不是安全层

`static_check` 是 AST 黑名单：拦危险导入、动态执行、对象逃逸属性。它只有两种含义：

- 命中 → 确定拒绝（这一步有效）；
- 未命中 → 只代表「静态没看出来」，**不代表安全**。

字符串拼接、`getattr` 拼名字、编码后 `exec` 解回来都能绕过。所以静态通过**不能**作为放行不可信代码的理由，上层也不允许把 `clean` 当成安全结论。

`sys` 是刻意放行的，因为契约要求能喂受控 stdin，而读 stdin 只能走 `sys.stdin`。放行的同时 `sys.modules`、`sys.path`、`sys.argv`、`sys.settrace` 等逐个挡住；`os`、`subprocess`、`socket`、`ctypes`、`winreg` 仍然整体禁止。

## 6. 环境与目录

- 子进程环境**只**含白名单键（SYSTEMROOT、PATH、TEMP/TMP 等）加三个强制项（UTF-8、不写字节码、不用用户 site）。宿主环境里的密钥不会继承过去。
- `extra=` 注入命中密钥名单时直接抛 `SANDBOX_POLICY_DENIED`，不静默剔除。
- 每次执行在 `<root>/.tmp/sandbox-runs/run-<随机>` 里建独立目录，写 `main.py`、以该目录为 cwd、结束（含超时与异常）后删除。
- 脚本路径必须以**绝对路径**传给子进程：cwd 已经切过去了，再给相对路径会被二次解析成不存在的位置。

## 7. 验收命令

```powershell
$env:UV_CACHE_DIR = Join-Path $env:TEMP "pq-uv-cache"
$env:PYTHONIOENCODING="utf-8"
[Console]::OutputEncoding=[System.Text.Encoding]::UTF8
uv run --no-sync --extra agent python -m unittest tests.agent.sandbox.test_local
```

## 8. 学习产出

1. **相对路径 + cwd 是双重解析陷阱**：`cwd=work` 配上相对脚本路径，子进程会在 `work` 下再找一遍 `work/main.py`。症状是退出码 2、stdout 为空，看起来像"代码有 bug"，实际是"根本没跑"。
2. **「没执行」和「执行失败」必须是两种结果**：混在一起会让静态拒绝、策略拒绝、启动失败全部伪装成"业务失败"，把可解释性彻底毁掉。
3. **能跑通 stdin 才叫真的能跑代码**：放行 `sys` 却把 `sys.stdin` 也列进黑名单，是把白名单写成了空操作。逐属性审一遍比整模块放行或整模块禁用都靠谱。
4. **能力矩阵要敢写 unsupported**：网络隔离本机确实做不到（实测 `connect_ex` 返回 10035，说明连接真的发出去了），写清楚不可用比含糊其辞更适合面试——面试官会问"你怎么证明隔离"，正确答案是"我没证明，我标注了做不到"。
5. **自陈局限的文案也要被测**：能力说明里"快速拒绝层""拦不住绕过写法"这些字眼是设计承诺，用断言锁住它，防止后人把文案改漂亮。

## 9. 已知边界与遗留风险

| 边界 | 说明 |
|---|---|
| 不是隔离 | 进程以当前用户身份运行，能访问用户能访问的一切 |
| 无网络隔离 | 静态层能挡住常见写法，但绕过静态层就没有第二道防线 |
| 无内存/CPU 上限 | Job Object 已建但未设置资源限制 |
| 静态可绕过 | 黑名单本质，只作为快速拒绝层 |
| 本机不可信代码一律不跑 | 有容器或 gVisor 后由那条 runtime 承担，`SandboxRuntime` 接口不变 |

## Public verification and rollback

Run the public test suite with `uv run --no-sync --extra agent python agent_eval/run_unittest_shim.py`. Use Git to revert a reviewed commit when needed; do not manually delete shared source files or internal evidence paths.
