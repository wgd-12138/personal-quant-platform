"""把 eval_all.json 渲染成可阅读的报告与 badcase 清单。

报告的第一段必须写清楚三件事：基线是什么、样本有多大、哪些结论是环境不满足导致的 UNKNOWN。
一份不说清楚这三样的评测报告，数字再漂亮也不能作为证据。
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, Mapping, Sequence

REPORT_SCHEMA_VERSION = "pq_agent.eval_report/1"


def _status_of(layer: Mapping[str, Any] | None) -> str:
    return str((layer or {}).get("status") or "unknown")


def _pct(value: Any) -> str:
    try:
        return "%.4f" % float(value)
    except (TypeError, ValueError):
        return str(value)


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    lines = ["| " + " | ".join(str(item) for item in headers) + " |"]
    lines.append("|" + "|".join("---" for _ in headers) + "|")
    for row in rows:
        lines.append("| " + " | ".join("" if item is None else str(item) for item in row) + " |")
    return lines


def render_report(payload: Mapping[str, Any]) -> str:
    layers = list(payload.get("layers_run") or [])
    l0 = dict(payload.get("l0") or {})
    l1 = dict(payload.get("l1") or {})
    l2 = dict(payload.get("l2") or {})
    l3 = dict(payload.get("l3") or {})
    lines: list[str] = []
    lines.append("# A5-02 四层评测报告")
    lines.append("")
    lines.append("生成时间：%s" % payload.get("generated_at", "unknown"))
    # 这里有两个不同的指纹，别混：
    #   用例集指纹  证明 golden 用例本身没变（来自 l2.judge.suite_digest）
    #   评测配置指纹 证明分层配置与门槛来源没变（来自 payload.suite_digest）
    lines.append("用例集指纹：%s" % ((l2.get("judge") or {}).get("suite_digest") or "unknown"))
    lines.append("评测配置指纹：%s" % payload.get("suite_digest", ""))
    lines.append("整体结论：%s" % ("通过" if payload.get("ok") else "未通过"))
    lines.append("")
    lines.append("## 1. 基线与口径")
    lines.append("")
    lines.append("| 项 | 值 |")
    lines.append("|---|---|")
    lines.append("| L0 基线 | 全量 %s 个用例，0 failure 0 error（来源见下） |" % l0.get("baseline_tests", "?"))
    lines.append("| L0 本次 | %s 个用例，failures=%s errors=%s skipped=%s |" % (l0.get("tests", 0), l0.get("failures", 0), l0.get("errors", 0), l0.get("skipped", 0)))
    lines.append("| L2 样本量 | 用例 %s 条，实际产生观测 %s 条 |" % (l2.get("case_count", 0), l2.get("ran", 0)))
    lines.append("| L3 状态 | %s（本机没有 Docker/Collector，成本与延迟只能读到快照） |" % _status_of(l3))
    lines.append("| 平台 | %s / Python %s |" % ((payload.get("environment") or {}).get("platform", "?"), (payload.get("environment") or {}).get("python", "?")))
    lines.append("")
    lines.append("门槛来源：")
    for key, value in sorted(dict(payload.get("threshold_sources") or {}).items()):
        lines.append("- %s：%s" % (key, value))
    lines.append("")
    lines.append("## 2. 分层结果")
    lines.append("")
    rows = [
        ["L0 单测", _status_of(l0), "新增失败 %s" % l0.get("new_failures", "?"), "L0 用例数差值 %s" % l0.get("tests_delta", "?")],
        ["L1 意图", _status_of(l1.get("intent")), "门槛失败 %s" % (", ".join((l1.get("intent") or {}).get("gate_failures") or []) or "无"), "L2 模型通道 %s" % (l1.get("intent") or {}).get("l2_status", "unknown")],
        ["L1 RAG", _status_of(l1.get("rag")), "bm25 hit@5=%s mrr@5=%s" % ((l1.get("rag") or {}).get("bm25_metrics", {}).get("hit@5"), (l1.get("rag") or {}).get("bm25_metrics", {}).get("mrr@5")), "只有词法档设门槛"],
        ["L1 沙箱", _status_of(l1.get("sandbox")), "静态拦截 %s/%s，良性放行 %s/%s" % ((l1.get("sandbox") or {}).get("static_blocked"), (l1.get("sandbox") or {}).get("malicious_cases"), (l1.get("sandbox") or {}).get("benign_clear"), (l1.get("sandbox") or {}).get("benign_cases")), "运行时结论不在本层"],
        ["L2 golden", _status_of(l2), "通过 %s/%s" % ((l2.get("judge") or {}).get("passed"), (l2.get("judge") or {}).get("case_count")), "判题断言 14 条"],
        ["L3 APM", _status_of(l3), "成本状态 %s" % ((l3.get("cost") or {}).get("status")), "本机不可用即 blocker"],
    ]
    lines.extend(_table(["层", "状态", "关键数字", "备注"], rows))
    lines.append("")
    lines.append("### L1 输入指纹")
    lines.append("")
    intent = dict(l1.get("intent") or {})
    hashes = dict(intent.get("dataset_sha256") or {})
    schema = dict(l1.get("tool_schema") or {})
    lines.append("- 意图数据集：train=%s dev=%s test=%s" % (hashes.get("train", "")[:16], hashes.get("dev", "")[:16], hashes.get("test", "")[:16]))
    lines.append("- RAG：index_hash=%s corpus_hash=%s" % (str((l1.get("rag") or {}).get("index_hash", ""))[:16], str((l1.get("rag") or {}).get("corpus_hash", ""))[:16]))
    lines.append("- 沙箱语料指纹：%s" % str((l1.get("sandbox") or {}).get("corpus_sha256", ""))[:16])
    lines.append("- 工具 schema 指纹：%s（%s 个工具）" % (str(schema.get("sha256", ""))[:16], schema.get("tool_count", 0)))
    lines.append("")
    lines.append("### L1 意图分档指标")
    lines.append("")
    split_rows = []
    for name, item in sorted(dict(intent.get("splits") or {}).items()):
        if not isinstance(item, Mapping):
            continue
        # splits 里的样本数字段叫 total，不是 cases；两个都试，避免报告出现空单元格。
        sample_count = item.get("cases", item.get("total", ""))
        split_rows.append([name, sample_count, _pct(item.get("pipeline_accuracy")), _pct(item.get("macro_f1")), _pct(item.get("l1_coverage")), _pct(item.get("slot_accuracy"))])
    lines.extend(_table(["split", "样本", "端到端准确率", "macro-F1", "L1 命中率", "槽位准确率"], split_rows))
    lines.append("")
    lines.append("### L1 RAG 三通道")
    lines.append("")
    rag_rows = []
    for name, item in sorted(dict((l1.get("rag") or {}).get("modes") or {}).items()):
        rag_rows.append([name, item.get("hit@1"), item.get("hit@5"), item.get("mrr@5"), item.get("citation_valid_rate"), item.get("degraded_flagged_cases")])
    lines.extend(_table(["通道", "hit@1", "hit@5", "mrr@5", "引用有效率", "标记降级的用例"], rag_rows))
    rag_note = str((l1.get("rag") or {}).get("backend_note") or "")
    if rag_note:
        lines.append("")
        lines.append("后端说明：" + rag_note)
        lines.append("")
        lines.append("结论边界：只有词法档（BM25）有真实后端并设门槛；vector/hybrid 用的是确定性哈希向量，只能证明通道机制，不能当成语义检索效果。")
    lines.append("")
    lines.append("## 3. L2 golden task 逐条结果")
    lines.append("")
    judge = dict(l2.get("judge") or {})
    case_rows = []
    for item in judge.get("verdicts") or []:
        failed = ", ".join(item.get("failed_checks") or []) or "无"
        case_rows.append([item.get("case_id"), item.get("split"), "通过" if item.get("ok") else "未通过", failed])
    lines.extend(_table(["用例", "split", "结果", "未通过的断言"], case_rows))
    lines.append("")
    if judge.get("failed_checks"):
        lines.append("按断言汇总的失败次数：")
        for name, count in sorted(dict(judge.get("failed_checks") or {}).items()):
            lines.append("- %s：%d" % (name, count))
        lines.append("")
    lines.append("判题说明：" + str(judge.get("llm_judge_note") or l2.get("llm_judge_note") or ""))
    lines.append("")
    lines.append("## 4. split 与「能不能调参」")
    lines.append("")
    lines.append("L2 的 10 条用例在 A5-02 开发期被逐一观察过，因此它整体是**回归集**，不是留出集：它能证明「修过的问题没有再犯」，不能证明泛化。")
    lines.append("整个仓库里唯一在开发期没有被使用的留出集是 A1-02 封存的 intent test 分档（sha256 已固定在上面的指纹里）。")
    lines.append("评测脚本不改任何阈值：门槛常量来自各任务的验收合同，改门槛必须先在合同里改，再重跑。")
    lines.append("")
    lines.append("## 5. 未解决风险与 blocker")
    lines.append("")
    blockers = list(payload.get("blockers") or [])
    if blockers:
        for item in blockers:
            lines.append("- " + str(item))
    else:
        lines.append("- 无")
    if any("docker" in str(item).lower() for item in blockers):
        # 说明这些 blocker 到底否掉了什么，避免读者把平台缺失当成「跑过了但效果差」。
        lines.append("")
        lines.append("上面与 docker 相关的项表示：这台机器上没有 APM 平台，L3 的数字只能来自本地 trace 快照，不能当成 SkyWalking 证据；要拿平台级证据必须在有 Docker 的机器上重跑。")
    lines.append("")
    lines.append("## 6. 复现命令")
    lines.append("")
    lines.append("```")
    lines.append("$env:UV_CACHE_DIR = Join-Path $env:TEMP 'pq-uv-cache'")
    lines.append("uv run --no-sync --extra agent python agent_eval/run_all.py --layers %s --write" % ",".join(layers))
    lines.append("```")
    lines.append("")
    lines.append("单层复跑：把 --layers 换成 l2（只跑 golden task）或 l1（只跑通道级评测）。")
    lines.append("")
    return chr(10).join(lines) + chr(10)


def render_badcases(payload: Mapping[str, Any], *, suite: Mapping[str, Any] | None = None) -> str:
    """badcase 清单：每条都要有现象、根因、防回归动作，不允许只写「失败」。"""
    by_id = {}
    for item in (suite or {}).get("cases") or []:
        spec = item if isinstance(item, Mapping) else item.to_dict()
        by_id[str(spec.get("case_id"))] = spec
    lines: list[str] = []
    lines.append("# A5-02 badcase 清单")
    lines.append("")
    lines.append("生成时间：%s" % payload.get("generated_at", "unknown"))
    lines.append("")
    lines.append("只收录未通过判题或已记录为歧义的用例。每条都给出根因与下一步动作。")
    lines.append("")
    judge = dict((payload.get("l2") or {}).get("judge") or {})
    failed = [item for item in judge.get("verdicts") or [] if not item.get("ok")]
    if not failed:
        lines.append("本次没有判题未通过的用例。")
        lines.append("")
    for item in failed:
        case_id = str(item.get("case_id"))
        spec = by_id.get(case_id) or {}
        lines.append("## %s（%s）" % (case_id, item.get("split")))
        lines.append("")
        lines.append("- 查询：%s" % spec.get("query", ""))
        lines.append("- 现象：未通过断言 %s" % ", ".join(item.get("failed_checks") or []))
        for check in item.get("checks") or []:
            if not check.get("ok"):
                lines.append("  - %s：%s" % (check.get("name"), check.get("detail")))
        lines.append("- 这条用例守的是：%s" % spec.get("why", ""))
        lines.append("- 工程陷阱：%s" % spec.get("trap", ""))
        lines.append("- 下一步动作：先定位是计划层、权限层还是判题器自己的期望写错了；期望写错就改用例并在本文档记录原因。")
        lines.append("")
    ambiguous = [item for item in (suite or {}).get("cases") or [] if "badcase" in str((item if isinstance(item, Mapping) else item.to_dict()).get("why", ""))]
    if ambiguous:
        lines.append("## 已知歧义（不算失败，但必须记着）")
        lines.append("")
        for item in ambiguous:
            spec = item if isinstance(item, Mapping) else item.to_dict()
            lines.append("- %s：%s" % (spec.get("case_id"), spec.get("why", "")))
            lines.append("  - 陷阱：%s" % spec.get("trap", ""))
        lines.append("")
    lines.append("## 本任务修掉的产品缺陷（不是 badcase，但要留下痕迹）")
    lines.append("")
    lines.append("- 计划层用空参数造计划，必填 as_of/slot 缺失，只读路径直接报 TOOL_SCHEMA_INVALID。")
    lines.append("- history_question 映射到从未注册的 report_search，只读路径报 TOOL_UNKNOWN（A5-02 补上 report_search 只读工具）。")
    lines.append("- risk_question 映射到 risk_rules_read，而注册表里的名字是 policy_rules_read。")
    lines.append("- 零参数工具被当成「参数未知」而透传调用方槽位，报 TOOL_FIELD_FORBIDDEN。")
    return chr(10).join(lines) + chr(10)


def write_reports(payload: Mapping[str, Any], *, root: pathlib.Path | None = None) -> list[str]:
    base = pathlib.Path(root) if root is not None else pathlib.Path.cwd()
    results = base / "agent_eval" / "results"
    results.mkdir(parents=True, exist_ok=True)
    suite: Mapping[str, Any] = {}
    golden = base / "agent_eval" / "cases" / "golden_tasks.json"
    if golden.exists():
        suite = json.loads(golden.read_text(encoding="utf-8"))
    report_path = results / "eval_report.md"
    badcase_path = results / "eval_badcases.md"
    report_path.write_text(render_report(payload), encoding="utf-8", newline=chr(10))
    badcase_path.write_text(render_badcases(payload, suite=suite), encoding="utf-8", newline=chr(10))
    return [str(report_path.relative_to(base)).replace(chr(92), "/"), str(badcase_path.relative_to(base)).replace(chr(92), "/")]


__all__ = ["REPORT_SCHEMA_VERSION", "render_badcases", "render_report", "write_reports"]
