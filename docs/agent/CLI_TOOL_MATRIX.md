# CLI 副作用矩阵与工具参数白名单（A0-02）

- 生成日期：2026-10-14
- 输入：`src/personal_quant/cli.py`（1045 行）、`src/personal_quant/**` 各底层模块
- 结论用途：A1-04 工具 registry 的 allowlist 依据；**本文件不实现任何工具，也不向模型开放任何命令**

## 1. 一句话结论

**命令名不等于无副作用。** 26 个命令里，只有 0 个是"纯只读"；11 个会外网取数，15 个会写文件，"dry-run" 也不等于零写入。

## 2. 分类口径

| 类别 | 含义 |
|---|---|
| `builder` | 纯函数，不含 I/O（本清单中为 0 个） |
| `report` | 生成分析报告并落盘 |
| `control` | 只写控制面（运行记录、门禁、清单、回执） |
| `business` | 写业务数据文件（真实持仓） |
| `notify` | 具备对外发送能力（live 时真发飞书） |
| `sysquery` | 查询操作系统状态（派生 PowerShell 进程） |
| `loop` | 无限循环，不会自行退出 |

标志列取值只有 `是` / `否`。带 `(live)` 表示"仅当传入 `--live` 时才发生"。

## 3. 副作用矩阵

| command | 类别 | 底层函数 | 读本地 | 读外网 | 写业务 | 写报告 | 写控制态 | 外发 | 子进程 | 循环 | 依赖时钟 | 人工确认 | 目标路径 | 证据 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| summary | report | generate_summary | 是 | 是 | 否 | 是 | 否 | 否 | 否 | 否 | 是 | 否 | reports/<date>/summary_<date>.md | src/personal_quant/cli.py:446 |
| daily-pack | report | generate_daily_pack | 是 | 是 | 否 | 是 | 否 | 否 | 否 | 否 | 是 | 否 | reports/<date>/ 下 9 个分报告 + manifest | src/personal_quant/cli.py:457 |
| check-inputs | report | generate_readiness_report | 是 | 否 | 否 | 是 | 否 | 否 | 否 | 否 | 是 | 否 | reports/<date>/readiness_<date>.md | src/personal_quant/cli.py:519 |
| positions-import | business | import_positions_file | 是 | 否 | 是 | 否 | 否 | 否 | 否 | 否 | 否 | 是 | data/positions.csv | src/personal_quant/cli.py:535 |
| positions-watch | loop | watch_positions_exports | 是 | 否 | 是 | 否 | 否 | 否 | 否 | 是 | 是 | 是 | data/positions.csv | src/personal_quant/cli.py:539 |
| run-audit | control | generate_run_audit | 是 | 否 | 否 | 否 | 是 | 否 | 否 | 否 | 是 | 否 | planning/runs/run_audit_<date>.md | src/personal_quant/cli.py:560 |
| run-summary | control | generate_run_summary | 是 | 否 | 否 | 否 | 是 | 否 | 否 | 否 | 否 | 否 | planning/runs/run_summary.md | src/personal_quant/cli.py:569 |
| trial-run | control | generate_trial_run | 是 | 是 | 否 | 是 | 是 | 否 | 否 | 否 | 是 | 否 | reports/<date>/ + planning/runs/run_audit_<date>.md | src/personal_quant/cli.py:948 |
| trial-run-next | control | generate_trial_run_next | 是 | 是 | 否 | 是 | 是 | 否 | 否 | 否 | 是 | 否 | 同 trial-run | src/personal_quant/cli.py:997 |
| trial-gate | control | generate_trial_gate | 是 | 否 | 否 | 否 | 是 | 否 | 否 | 否 | 否 | 否 | planning/runs/stage_gate_09.md | src/personal_quant/cli.py:578 |
| trial-status | control | generate_trial_status | 是 | 否 | 否 | 否 | 是 | 否 | 否 | 否 | 否 | 否 | planning/runs/trial_status.md | src/personal_quant/cli.py:582 |
| expected-dates-add | control | add_expected_trading_dates | 是 | 否 | 否 | 否 | 是 | 否 | 否 | 否 | 否 | 是 | planning/runs/expected_dates.txt | src/personal_quant/cli.py:598 |
| push-schedule | control | generate_push_schedule | 是 | 否 | 否 | 否 | 是 | 否 | 否 | 否 | 否 | 否 | planning/runs/push_schedule.md | src/personal_quant/cli.py:603 |
| push-send | notify | generate_push_send | 是 | 否 | 否 | 否 | 是 | 是 | 否 | 否 | 是 | 是 | planning/runs/push_receipt_<date>_<slot>.md | src/personal_quant/notifier/push.py:85 |
| push-run | notify | generate_push_run | 是 | 是 | 否 | 是 | 是 | 是 | 否 | 否 | 是 | 是 | reports/<date>/ + push_receipt_<date>_<slot>.md | src/personal_quant/cli.py:641 |
| push-daily | notify | generate_push_daily | 是 | 是 | 否 | 是 | 是 | 是 | 否 | 否 | 是 | 是 | 同 push-run | src/personal_quant/cli.py:671 |
| push-windows-tasks | control | generate_windows_task_script | 是 | 否 | 否 | 否 | 是 | 否 | 否 | 否 | 否 | 是 | planning/runs/register_push_tasks.ps1 | src/personal_quant/cli.py:699 |
| position-monitor-once | notify | generate_position_monitor_once | 是 | 是 | 否 | 是 | 是 | 是 | 否 | 否 | 是 | 是 | planning/runs/position_monitor_<ts>.md + position_monitor_state.json | src/personal_quant/notifier/position_monitor.py:81 |
| position-monitor | loop | generate_position_monitor_loop | 是 | 是 | 否 | 是 | 是 | 是 | 否 | 是 | 是 | 是 | 同 position-monitor-once | src/personal_quant/notifier/position_monitor.py:119 |
| market-monitor-once | notify | generate_market_monitor_once | 是 | 是 | 否 | 是 | 是 | 是 | 否 | 否 | 是 | 是 | planning/runs/market_monitor_<ts>.md + market_monitor_state.json | src/personal_quant/notifier/market_monitor.py:103 |
| market-monitor | loop | generate_market_monitor_loop | 是 | 是 | 否 | 是 | 是 | 是 | 否 | 是 | 是 | 是 | 同 market-monitor-once | src/personal_quant/notifier/market_monitor.py:141 |
| push-status | control | generate_push_status | 是 | 否 | 否 | 否 | 是 | 否 | 否 | 否 | 是 | 否 | planning/runs/push_status.md | src/personal_quant/notifier/status.py:148 |
| push-test | notify | generate_push_test | 是 | 否 | 否 | 否 | 是 | 是 | 否 | 否 | 是 | 是 | planning/runs/push_channel_test.md | src/personal_quant/notifier/channel_test.py:26 |
| push-windows-task-status | sysquery | generate_windows_task_status | 否 | 否 | 否 | 否 | 是 | 否 | 是 | 否 | 否 | 否 | planning/runs/push_windows_task_status.md | src/personal_quant/notifier/windows_tasks.py:131 |
| push-go-live | control | generate_push_go_live | 是 | 否 | 否 | 否 | 是 | 否 | 否 | 否 | 否 | 否 | planning/runs/push_go_live.md | src/personal_quant/notifier/go_live.py:54 |
| push-activation | control | generate_push_activation | 是 | 否 | 否 | 否 | 是 | 否 | 否 | 否 | 否 | 否 | planning/runs/push_activation.md | src/personal_quant/notifier/activation.py:64 |

统计（由 `check_cli_matrix.py` 逐行核对）：26 行；读外网 10；写业务 2；写报告 11；写控制态 21；外发 8；子进程 1；循环 3；需要人工确认 12。

**关于"外发"列**：标 `是` 只表示"具备发送能力"，实际发送必须同时满足 `--live` + 已解析到 webhook；缺 webhook 时底层直接抛 `FEISHU_WEBHOOK_URL_REQUIRED`（`src/personal_quant/notifier/push.py:98-100`）。

**关于"写控制态"列**：推送回执、门禁、清单、运行记录**无论 dry-run 与否都写**；只有持仓/市场的 `*_state.json` 冷却状态是 **live 且有触发时才写**（`src/personal_quant/notifier/position_monitor.py:106-107`、`src/personal_quant/notifier/market_monitor.py:128-129`）。

## 4. 三处必须写进合同的陷阱（逐条源码佐证）

### 4.1 dry-run 仍然写文件

`--live` 只控制"是否发飞书"，不控制"是否落盘"。

- `src/personal_quant/notifier/push.py:85-110`：`send_push_slot` 里 `if live:` 只包住发送逻辑；函数最后无条件 `return write_push_receipt_report(result, output_dir)`。
- `src/personal_quant/notifier/push.py:113-116`：回执写到 `planning/runs/push_receipt_<date>_<slot>.md`。
- 同类：`src/personal_quant/notifier/channel_test.py:50-56`（dry-run 也写 `push_channel_test.md`）；`src/personal_quant/notifier/position_monitor.py:116`、`src/personal_quant/notifier/market_monitor.py:138`（dry-run 也写监控报告）。

**规则**：工具契约里 dry-run 必须写成"不发外呼"，不能写成"无副作用"。

### 4.2 check-inputs 会先写 readiness 再报错

- `src/personal_quant/cli.py:519-532`：`generate_readiness_report` 先 `write_text` 写 `readiness_<date>.md`（:529），**然后**才在 `:530-531` 判断 `if not result.ready: raise ValueError`。
- 即"检查类命令"失败退出前已经写了盘。

**规则**：把 check-inputs 当只读工具是错的；它至少是 `write_report`。

### 4.3 push-run 会先补写当日日报（触网）

- `src/personal_quant/cli.py:641-668`：`generate_push_run` 在 `if slot != "pre_open"` 时先调 `generate_daily_pack(..., split=True)`（:652-660），这一步会走 `fetch_market_dataset` 触外网并写 9 个报告文件；`pre_open` 是唯一跳过日报生成的 slot。
- `src/personal_quant/cli.py:671-696`：`push-daily` 额外用时钟与 `planning/runs/expected_dates.txt` 推算 `as_of`（`_push_daily_report_date`，`cli.py:881-889`），日期不符会抛 `PUSH_DAILY_DATE_NOT_EXPECTED`。

**规则**：push-run / push-daily 是"取数 + 生成 + 外发"的组合动作，不能当作单一"发送"工具。

## 5. 监控类命令的 live 边界

- `src/personal_quant/notifier/position_monitor.py:288-300` 与 `src/personal_quant/notifier/market_monitor.py:349-361`：`_send_alerts` 首行 `if not live: return []`。
- 冷却状态只在 live 且有触发时落盘：`position_monitor.py:106-107`、`market_monitor.py:128-129` → `_record_alerts`（`position_monitor.py:371-376`）。

**规则**：dry-run 监控命令 = 只写报告、不写冷却状态、不外发；live 监控命令 = 外发 + 写状态。

## 6. 未来工具的允许参数与禁止字段（A1-04 依据）

### 6.1 允许字段（全部由代码层校验，不接受模型自由文本）

| 字段 | 类型 | 约束 |
|---|---|---|
| `as_of` | string | 必须 `YYYY-MM-DD`，且必须命中 `planning/runs/expected_dates.txt` 或 replay manifest |
| `slot` | enum | 必须是 `PUSH_SLOTS` 的 key（pre_open / morning_build / morning / midday / overnight_arbitrage / tail / evening） |
| `ignore_positions` | bool | 仅影响是否生成逐股内容，不允许触碰 `data/` |
| `approval_token` | string | 仅写盘/外发类工具需要；由 HITL 节点签发，绑定 `hash(工具名 + 规范化参数)` |

### 6.2 禁止字段（出现即拒绝，不解释、不降级）

| 禁止字段 | 原因 |
|---|---|
| `webhook_url` | 凭证只允许在 notify adapter 内解析（`src/personal_quant/notifier/webhook_env.py:17-47`），不得进入模型 schema / state / checkpoint |
| `live` | 发送开关只能由 HITL 节点在确认后置位，模型不得传 |
| `output` / `output_dir` / `reports_dir` / `runs_dir` / `path` / `source` / `project_dir` | 任意路径写入 |
| `encoding` / `pattern` / `interval` / `interval_seconds` / `once` | 关闭循环保护或改变文件匹配 |
| `python` / `command` / `source_command` / 任意命令行字符串 | 命令注入 |
| `script` / `code` | 代码执行 |

### 6.3 统一结果 schema

```json
{
  "ok": true,
  "error_code": null,
  "command": "daily-pack",
  "as_of": "2026-07-09",
  "slot": null,
  "dry_run": true,
  "data_source": "replay",
  "degraded": [],
  "artifacts": [{"path": "reports/2026-07-09/daily_pack_2026-07-09.md", "bytes": 12345, "sha256": "..."}],
  "warnings": [],
  "duration_ms": 812
}
```

失败时 `ok=false`、`error_code` 取底层的错误码前缀（如 `INPUTS_NOT_READY`、`PUSH_PREVIEW_FILE_MISSING`、`POSITION_MONITOR_DATE_NOT_EXPECTED`），**不回传原始异常文本**，只回传安全摘要。

### 6.4 拒绝策略

| 情况 | 行为 |
|---|---|
| 未知 / 未登记命令 | `TOOL_NOT_ALLOWED`，绝不落到 shell 或 `getattr` 动态调用 |
| 出现禁止字段 | `TOOL_FIELD_FORBIDDEN`，整次调用拒绝 |
| 循环类命令（positions-watch / position-monitor / market-monitor） | `TOOL_LOOP_COMMAND_REJECTED`；只允许 `*-once` 变体 |
| 写盘 / 外发类命令缺少 `approval_token` | `TOOL_APPROVAL_REQUIRED`，fail-closed |
| `as_of` 不在期望交易日 / 不在 replay manifest | `INVALID_AS_OF` |

## 7. fail-closed 规则

1. **未知命令**：allowlist 是静态字典，取不到就打 `TOOL_NOT_ALLOWED`；不做前缀匹配、不做模糊纠错。
2. **路径穿越**：入参路径先 `Path.resolve()`，再断言 `resolved.is_relative_to(project_root.resolve())`；不满足即 `PATH_OUTSIDE_ROOT`。
3. **符号链接 / junction**：对 `resolved` 的每一级父目录检查 `os.path.islink` 与 Windows 重解析点（`os.lstat().st_reparse_tag`）；命中即 `PATH_REPARSE_POINT`。
4. **UNC / 网络路径**：以 `\\\\` 或 `//` 开头一律 `PATH_UNC_REJECTED`。
5. **不存在路径**：写类工具只允许创建在项目根内的新文件；父目录越界或已存在同名目录即拒绝。
6. **循环命令**：静态拒绝，不提供超时兜底——因为 `while True` 里没有退出条件，超时杀掉会留下半个状态文件。
7. **超时**：单次工具调用设硬超时（默认 30s），超时按失败返回，不重试写盘类动作。

## 8. 学习产出（可写进面试）

1. **"只读命令"是幻觉**：本仓 26 个命令没有一个是纯只读；连 `check-inputs` 和 dry-run 推送都会落盘。
2. **dry-run 的正确语义是"不发外呼"，不是"无副作用"**：把两者混为一谈，工具权限分级就会做错。
3. **命令名 ≠ 工具粒度**：`push-run` 实为"取数 + 生成日报 + 外发"三合一，必须拆成三个工具，否则人工确认放在哪一步都说不清。
4. **确认要绑参数**：审批记录存 `hash(工具名 + 规范化参数)`，resume 时重算比对，才能防"确认 A 却执行 B"。

## Public verification and rollback

Run the public test suite with `uv run --no-sync --extra agent python agent_eval/run_unittest_shim.py`. Use Git to revert a reviewed commit when needed; do not manually delete shared source files or internal evidence paths.
