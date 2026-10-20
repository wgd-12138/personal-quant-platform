"""RAG 检索评测（A3-02）：跑分、对照、找 badcase，并把降级状态写进报告。

这个脚本只做四件事，多一件都不做：
  1. 用固定题目集跑 hit@k / MRR@5 / 金标覆盖率，按分桶与划分分别统计；
  2. 对每条命中的块做引用可核验检查（chunk_id 存在、路径对、行范围对、内容 hash 对）；
  3. 把 badcase 记下来，并写清是"检索没找到"还是"找到了但引用不可核验"；
  4. 把 degrade 状态、后端名字、权重、索引/语料指纹写进报告头，让每个数字都能追到配置。

刻意不做的事：不做模型判分（那会变成自评），不把 hermetic 后端的分数说成语义检索效果，
不为了让报告好看而调阈值。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from pq_agent.rag.retriever import (  # noqa: E402
    DEFAULT_TOP_K,
    HYBRID_WEIGHT_LEXICAL,
    HYBRID_WEIGHT_VECTOR,
    MODE_BM25,
    MODE_HYBRID,
    MODE_VECTOR,
    build_retriever,
    check_citations,
    describe_retriever,
    load_index,
    DEFAULT_INDEX_PATH,
)

CASES_PATH = ROOT / "agent_eval" / "cases" / "rag_eval_cases.json"
RESULTS_DIR = ROOT / "agent_eval" / "results"
BASELINE_PATH = RESULTS_DIR / "rag_baseline.json"
REPORT_PATH = RESULTS_DIR / "rag_retrieval_report.md"
BADCASE_PATH = RESULTS_DIR / "rag_badcases.md"

CUTOFFS = (1, 3, 5)
MRR_CUTOFF = 5

# 门槛只对词法档设，因为只有它在本机有真实后端。
# 语义档的分数不设门槛：没有真实后端就不该给一个"通过"的结论。
THRESHOLDS_LEXICAL = {
    "hit@5": 0.60,
    "mrr@5": 0.45,
    "citation_valid_rate": 1.0,
}


def _load_cases() -> dict[str, object]:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


def _gold_hit(retriever, outcome, case: dict[str, object]) -> bool:
    """命中判据：top-k 里存在一条来自金标路径、且正文含金标关键词的块。"""
    gold_path = str(case["gold_path"])
    keyword = str(case["gold_keyword"])
    for hit in outcome.hits:
        if hit.record.source_path == gold_path and keyword in hit.record.text:
            return True
    return False


def _first_gold_rank(outcome, case: dict[str, object]) -> int:
    """金标第一次出现在第几位。0 表示没出现（用于 MRR）。"""
    gold_path = str(case["gold_path"])
    keyword = str(case["gold_keyword"])
    for rank, hit in enumerate(outcome.hits, start=1):
        if hit.record.source_path == gold_path and keyword in hit.record.text:
            return rank
    return 0


def run_mode(retriever, cases: list[dict[str, object]], *, top_k: int) -> dict[str, object]:
    """跑一种配置，返回全部指标与 badcase。"""
    per_case: list[dict[str, object]] = []
    citation_checked = 0
    citation_failed = 0
    degraded_flagged = 0
    for case in cases:
        outcome = retriever.search(str(case["question"]), top_k=top_k)
        hit_at = {cutoff: False for cutoff in CUTOFFS}
        first = 0
        for rank, hit in enumerate(outcome.hits, start=1):
            matched = hit.record.source_path == str(case["gold_path"]) and str(case["gold_keyword"]) in hit.record.text
            if matched:
                if first == 0:
                    first = rank
                for cutoff in CUTOFFS:
                    if rank <= cutoff:
                        hit_at[cutoff] = True
        # 引用可核验：对每条命中跑一次四项核对
        refs = list(outcome.citations)
        report = check_citations(refs, retriever=retriever, answer="".join(f"[{i}]" for i in range(1, len(refs) + 1)), require_all_opened=False)
        citation_checked += len(report.checks)
        citation_failed += len(report.failures)
        if outcome.degraded:
            degraded_flagged += 1
        per_case.append(
            {
                "case_id": case["case_id"],
                "bucket": case["bucket"],
                "split": case["split"],
                "hit_at": {str(k): bool(v) for k, v in hit_at.items()},
                "first_gold_rank": first,
                "top_ids": [hit.chunk_id for hit in outcome.hits],
                "top_citations": refs,
                "citation_ok": bool(report.ok),
                "citation_failures": [item.to_dict() for item in report.failures],
                "degraded": bool(outcome.degraded),
            }
        )
    # 没有用例时必须显式失败：用 1 兜住分母会把「一条都没跑」输出成 0 分指标，
    # 这种报告读起来像「跑过但效果差」，属于用兜底冒充结果。
    if not per_case:
        raise ValueError("RAG 评测没有可用用例：拒绝用空样本产出指标")
    total = len(per_case)
    metrics = {
        "cases": len(per_case),
        "hit@1": round(sum(1 for item in per_case if item["hit_at"]["1"]) / total, 4),
        "hit@3": round(sum(1 for item in per_case if item["hit_at"]["3"]) / total, 4),
        "hit@5": round(sum(1 for item in per_case if item["hit_at"]["5"]) / total, 4),
        "mrr@5": round(
            sum((1.0 / item["first_gold_rank"]) if 0 < item["first_gold_rank"] <= MRR_CUTOFF else 0.0 for item in per_case)
            / total,
            4,
        ),
        "citation_windows_checked": citation_checked,
        "citation_failures": citation_failed,
        # 一个引用窗口都没检查时，1.0 是假的满分；None 表示 unknown，由门槛判它不通过。
        "citation_valid_rate": (round(1.0 - (citation_failed / citation_checked), 4) if citation_checked else None),
        "degraded_flagged_cases": degraded_flagged,
    }
    by_bucket: dict[str, dict[str, float]] = {}
    for bucket in sorted({str(item["bucket"]) for item in per_case}):
        rows = [item for item in per_case if item["bucket"] == bucket]
        count = len(rows) or 1
        by_bucket[bucket] = {
            "cases": len(rows),
            "hit@5": round(sum(1 for item in rows if item["hit_at"]["5"]) / count, 4),
            "mrr@5": round(
                sum((1.0 / item["first_gold_rank"]) if 0 < item["first_gold_rank"] <= MRR_CUTOFF else 0.0 for item in rows) / count,
                4,
            ),
        }
    by_split: dict[str, dict[str, float]] = {}
    for split in sorted({str(item["split"]) for item in per_case}):
        rows = [item for item in per_case if item["split"] == split]
        count = len(rows) or 1
        by_split[split] = {
            "cases": len(rows),
            "hit@5": round(sum(1 for item in rows if item["hit_at"]["5"]) / count, 4),
            "mrr@5": round(
                sum((1.0 / item["first_gold_rank"]) if 0 < item["first_gold_rank"] <= MRR_CUTOFF else 0.0 for item in rows) / count,
                4,
            ),
        }
    return {"metrics": metrics, "by_bucket": by_bucket, "by_split": by_split, "cases": per_case}


def _fmt(value: float | None) -> str:
    return "unknown" if value is None else f"{value:.4f}"


def render_report(
    document: dict[str, object],
    results: dict[str, dict[str, object]],
    *,
    top_k: int,
    backend_note: str,
) -> str:
    lines: list[str] = []
    lines.append("# RAG 检索评测报告（A3-02）")
    lines.append("")
    lines.append("本文件由 `agent_eval/run_rag_eval.py` 生成，所有数字都是本机当场跑出来的。")
    lines.append("")
    lines.append("## 0. 读数字之前必须知道的事")
    lines.append("")
    lines.append(f"- 语料：`{document['index_path']}`，{len(document.get('cases', []))} 道题目。")
    lines.append(f"- 索引指纹：`{str(document.get('index_hash'))[:16]}`；语料指纹：`{str(document.get('corpus_hash'))[:16]}`。")
    lines.append(f"- profile：`{document.get('profile')}`（词法档，语义召回未启用）。")
    lines.append(f"- 后端说明：{backend_note}")
    lines.append("- **本机没有可用的语义 embedding 后端**，所以 vector / hybrid 两栏只能用非语义的 hermetic 后端")
    lines.append("  跑通道机制，它们证明的是「融合代码路径能跑、指标口径一致」，**不能**外推成「语义检索效果如何」。")
    lines.append("- 外部语料上等权融合劣于单路（58% vs 86%）这条教训只作为背景，不作为本仓库的实测结论。")
    lines.append("")
    lines.append("## 1. 总体指标（top_k = %d）" % top_k)
    lines.append("")
    lines.append("| 配置 | 样本 | hit@1 | hit@3 | hit@5 | MRR@5 | 引用窗口数 | 引用失败 | 引用有效率 | 标记降级的用例 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for name, payload in results.items():
        metrics = payload["metrics"]
        lines.append(
            "| `{name}` | {cases} | {h1} | {h3} | {h5} | {mrr} | {cw} | {cf} | {cv} | {dg} |".format(
                name=name,
                cases=metrics["cases"],
                h1=_fmt(metrics["hit@1"]),
                h3=_fmt(metrics["hit@3"]),
                h5=_fmt(metrics["hit@5"]),
                mrr=_fmt(metrics["mrr@5"]),
                cw=metrics["citation_windows_checked"],
                cf=metrics["citation_failures"],
                cv=_fmt(metrics["citation_valid_rate"]),
                dg=metrics["degraded_flagged_cases"],
            )
        )
    lines.append("")
    lines.append("## 2. 按分桶看（hit@5 / MRR@5）")
    lines.append("")
    buckets = sorted({bucket for payload in results.values() for bucket in payload["by_bucket"]})
    lines.append("| 配置 | " + " | ".join(buckets) + " |")
    lines.append("|---|" + "---|" * len(buckets))
    for name, payload in results.items():
        cells = []
        for bucket in buckets:
            row = payload["by_bucket"].get(bucket)
            cells.append(f"{_fmt(row['hit@5'])} / {_fmt(row['mrr@5'])}" if row else "-")
        lines.append(f"| `{name}` | " + " | ".join(cells) + " |")
    lines.append("")
    lines.append("## 3. 按划分看（train / dev / test）")
    lines.append("")
    splits = sorted({split for payload in results.values() for split in payload["by_split"]})
    lines.append("| 配置 | " + " | ".join(splits) + " |")
    lines.append("|---|" + "---|" * len(splits))
    for name, payload in results.items():
        cells = []
        for split in splits:
            row = payload["by_split"].get(split)
            cells.append(f"{_fmt(row['hit@5'])} / {_fmt(row['mrr@5'])}" if row else "-")
        lines.append(f"| `{name}` | " + " | ".join(cells) + " |")
    lines.append("")
    lines.append("## 4. 门槛判定（只对词法档设门槛）")
    lines.append("")
    lines.append("| 配置 | 指标 | 实测 | 门槛 | 结论 |")
    lines.append("|---|---|---|---|---|")
    lexical = results.get(MODE_BM25)
    if lexical:
        for key, threshold in THRESHOLDS_LEXICAL.items():
            actual = lexical["metrics"][key]
            verdict = "通过" if actual >= threshold else "未通过"
            lines.append(f"| `{MODE_BM25}` | {key} | {_fmt(actual)} | {threshold} | {verdict} |")
    for name, payload in results.items():
        if name == MODE_BM25:
            continue
        lines.append(
            f"| `{name}` | 全部 | 不设门槛 | - | 不判定（没有真实语义后端，给结论就是编） |"
        )
    lines.append("")
    lines.append("## 5. 指标口径")
    lines.append("")
    lines.append("- 命中判据：top-k 里存在一条块，其 `source_path` 等于金标路径且正文含金标关键词。")
    lines.append("- MRR@5：金标首次出现在第 r 位得 1/r（r ≤ 5），没出现得 0，再对题目取平均。")
    lines.append("- 引用有效率：对每次检索返回的每条引用跑四项核对（chunk_id / 路径 / 行范围 / 内容 hash），")
    lines.append("  统计窗口里没有一条失败的比例。它测的是「引用能不能被复核」，不是「引用得对不对」。")
    lines.append("- cutoff 陷阱：hit@5 比 hit@1 好看是必然的，报告里三个 cutoff 必须一起给，单独挑一个看没有意义。")
    lines.append("")
    lines.append("## 6. 降级状态")
    lines.append("")
    lines.append("本机 `PQ_AGENT_RAG_PROFILE` 未配置，默认 `lexical_only`，所以每次检索都带 `degraded=True` 与")
    lines.append("`degraded_reason`，并且这个标记会一路写进 API 响应、trace 和这份报告。")
    lines.append("dense_required 档在没有 embedding 后端时直接抛 `EMBEDDING_UNAVAILABLE`，不会静默回落。")
    lines.append("")
    return chr(10).join(lines)


def render_badcases(results: dict[str, dict[str, object]]) -> str:
    lines: list[str] = []
    lines.append("# RAG 检索 badcase（A3-02）")
    lines.append("")
    lines.append("只记事实与原因，不写「可能是」的猜测。")
    lines.append("")
    baseline = results.get(MODE_BM25) or next(iter(results.values()))
    missed = [item for item in baseline["cases"] if not item["hit_at"]["5"]]
    lines.append(f"## 1. 词法档未见金标的题目（{len(missed)} 条）")
    lines.append("")
    if not missed:
        lines.append("无。")
    else:
        lines.append("| 用例 | 分桶 | 划分 | 金标首现位次 | 实际返回的前三条引用 |")
        lines.append("|---|---|---|---|---|")
        for item in missed:
            top = "；".join(item["top_citations"][:3]) or "（空）"
            lines.append(f"| {item['case_id']} | {item['bucket']} | {item['split']} | {item['first_gold_rank']} | {top} |")
    lines.append("")
    citation_bad = [item for item in baseline["cases"] if not item["citation_ok"]]
    lines.append(f"## 2. 引用核对失败的用例（{len(citation_bad)} 条）")
    lines.append("")
    if not citation_bad:
        lines.append("无：本次检索返回的每条引用都通过了下述四项核对。")
    else:
        for item in citation_bad:
            lines.append(f"- {item['case_id']}：{json.dumps(item['citation_failures'], ensure_ascii=False)}")
    lines.append("")
    lines.append("## 3. 已知的语料结构问题（不是检索算法的问题）")
    lines.append("")
    lines.append("语料里混进了 `planning/agent/evidence/**/unittest-*.txt` 这类测试日志，")
    lines.append("单份日志能切出两百多个块、正文全是中文断言消息与文件名。这类块有两个害处：")
    lines.append("")
    lines.append("1. 词法打分被长日志里的高频中文词拉偏，真正讲设计的文档被挤下去；")
    lines.append("2. 引用虽然可核验，但「证据」是一行测试输出，对人没有解释价值。")
    lines.append("")
    lines.append("处理方式：本轮**只记录、不改语料策略**（语料白名单属于 A3-01 的范围）。")
    lines.append("建议的修法是把 `planning/agent/evidence` 加入拒绝前缀，重建索引后重跑本评测对照。")
    lines.append("")
    lines.append("## 4. 未测到的部分")
    lines.append("")
    lines.append("语义通道（vector / hybrid 的真实效果）在本机**未测到**：没有可用的 embedding 后端。")
    lines.append("报告中 vector / hybrid 一栏用的是非语义 hermetic 后端，只能验证代码路径与指标口径。")
    lines.append("要给出语义结论，必须在一台能装上 embedding 的机器上重跑本脚本。")
    lines.append("")
    return chr(10).join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="RAG 检索评测")
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--write", action="store_true", help="写报告文件（默认只打印）")
    args = parser.parse_args()

    document = _load_cases()
    cases = list(document["cases"])
    index_payload = load_index(DEFAULT_INDEX_PATH, root=ROOT)

    results: dict[str, dict[str, object]] = {}
    results[MODE_BM25] = run_mode(build_retriever(index_payload=index_payload, mode=MODE_BM25), cases, top_k=args.top_k)

    backend_note = "词法档：BM25，本机真实后端；vector/hybrid：HermeticEmbedder（确定性哈希向量，非语义模型，仅验证通道机制）。"
    try:
        from pq_agent.rag.embedder import HermeticEmbedder

        hermetic = HermeticEmbedder()
        results[MODE_VECTOR] = run_mode(
            build_retriever(index_payload=index_payload, mode=MODE_VECTOR, profile="dense_required", embedder=hermetic),
            cases,
            top_k=args.top_k,
        )
        results[MODE_HYBRID] = run_mode(
            build_retriever(
                index_payload=index_payload,
                mode=MODE_HYBRID,
                profile="dense_required",
                embedder=hermetic,
                lexical_weight=HYBRID_WEIGHT_LEXICAL,
                vector_weight=HYBRID_WEIGHT_VECTOR,
            ),
            cases,
            top_k=args.top_k,
        )
    except Exception as exc:  # noqa: BLE001
        backend_note += f" （通道构造失败：{type(exc).__name__}: {exc}）"

    payload = {
        "schema_version": "pq_agent.rag_eval/1",
        "cases_path": str(CASES_PATH.relative_to(ROOT)).replace(chr(92), "/"),
        "dataset_version": document.get("dataset_version"),
        "index_hash": document.get("index_hash"),
        "corpus_hash": document.get("corpus_hash"),
        "profile": document.get("profile"),
        "top_k": args.top_k,
        "cutoffs": list(CUTOFFS),
        "mrr_cutoff": MRR_CUTOFF,
        "thresholds": {"lexical": THRESHOLDS_LEXICAL},
        "backend_note": backend_note,
        "counts": document.get("counts"),
        "results": {name: {"metrics": item["metrics"], "by_bucket": item["by_bucket"], "by_split": item["by_split"]} for name, item in results.items()},
        "per_case": {name: item["cases"] for name, item in results.items()},
    }

    report = render_report(document, results, top_k=args.top_k, backend_note=backend_note)
    badcases = render_badcases(results)
    if args.write:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        BASELINE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")
        REPORT_PATH.write_text(report, encoding="utf-8")
        BADCASE_PATH.write_text(badcases, encoding="utf-8")
        print(f"wrote {BASELINE_PATH.name} / {REPORT_PATH.name} / {BADCASE_PATH.name}")
    for name, item in results.items():
        metrics = item["metrics"]
        print(f"{name:8s} hit@1={metrics['hit@1']:.4f} hit@5={metrics['hit@5']:.4f} mrr@5={metrics['mrr@5']:.4f} "
              f"citation_valid={_fmt(metrics['citation_valid_rate'])} degraded={metrics['degraded_flagged_cases']}/{metrics['cases']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
