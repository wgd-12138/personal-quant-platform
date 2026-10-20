
"""A1-02 意图识别离线评测脚本。

设计要点：
- 只用真实的 intent 模块接口，不复制一份规则；
- L2 需要真实凭证，缺凭证时标记为 NOT_MEASURED，绝不用造数据代替；
- dev 与 test 的阈值分开记录，test 只在最终评测使用，不用于调参；
- 输出对应用户已有习惯：results 目录 + report + badcases。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from pq_agent.intent import (  # noqa: E402
    DeepSeekIntentModel,
    RecordingIntentModel,
    classify_intent,
    classify_l1,
    credentials_from_environ,
    sha256_text,
)
from pq_agent.intent_schema import (  # noqa: E402
    DEFAULT_MIN_CONFIDENCE,
    INTENT_CLASSES,
    IntentCase,
    case_content_hash,
)

CASES_DIR = ROOT / "agent_eval" / "cases"
RESULTS_DIR = ROOT / "agent_eval" / "results"
DATASET_META_PATH = CASES_DIR / "intent_dataset.json"

# dev 阈值：开发期调参用，达到即可进入下一步。
DEV_THRESHOLDS: dict[str, float] = {
    "l1_coverage": 0.80,
    "l1_precision_on_hit": 0.90,
    "pipeline_accuracy": 0.80,
    "slot_accuracy": 0.90,
}
# test 阈值：与 dev 相同但在评测前冻结，test 结果只记录不回改参数。
TEST_THRESHOLDS: dict[str, float] = dict(DEV_THRESHOLDS)


def sha256_file(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_dataset_meta() -> dict[str, Any]:
    """读取语料密封信息；hash 对不上就直接失败，避免语料静默漂移。"""
    if not DATASET_META_PATH.exists():
        raise SystemExit("DATASET_META_MISSING: %s（请先运行 seal_intent_dataset.py）" % DATASET_META_PATH)
    meta = json.loads(DATASET_META_PATH.read_text(encoding="utf-8"))
    if meta.get("schema_version") != "pq_agent.intent_dataset/1":
        raise SystemExit("DATASET_META_SCHEMA: %s" % meta.get("schema_version"))
    for split, info in meta["splits"].items():
        path = CASES_DIR / info["file"]
        actual = sha256_file(path)
        if actual != info["sha256"]:
            raise SystemExit(
                "CASES_HASH_MISMATCH: %s 期望 %s 实际 %s（语料被改动，请重新密封并复核评测结论）"
                % (path.name, info["sha256"], actual)
            )
        count = len([line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()])
        if count != info["count"]:
            raise SystemExit("CASES_COUNT_MISMATCH: %s 期望 %d 实际 %d" % (path.name, info["count"], count))
    return meta


def load_cases(split: str) -> list[IntentCase]:
    path = CASES_DIR / ("intent_%s.jsonl" % split)
    if not path.exists():
        raise SystemExit("CASES_MISSING: %s" % path)
    cases: list[IntentCase] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit("CASES_JSON_INVALID: %s:%d: %s" % (path.name, lineno, exc))
        case = IntentCase.from_dict(payload)  # 校验字段、split、content_hash
        if case.split != split:
            raise SystemExit("CASES_SPLIT_MISMATCH: %s:%d -> %s" % (path.name, lineno, case.split))
        cases.append(case)
    return cases


def macro_scores(pairs: list[tuple[str, str]], labels: tuple[str, ...]) -> dict[str, float]:
    per_label: dict[str, dict[str, float]] = {}
    for label in labels:
        tp = sum(1 for gold, pred in pairs if gold == label and pred == label)
        fp = sum(1 for gold, pred in pairs if gold != label and pred == label)
        fn = sum(1 for gold, pred in pairs if gold == label and pred != label)
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        per_label[label] = {"precision": precision, "recall": recall, "f1": f1, "support": tp + fn}
    macro_p = sum(v["precision"] for v in per_label.values()) / len(labels)
    macro_r = sum(v["recall"] for v in per_label.values()) / len(labels)
    macro_f1 = sum(v["f1"] for v in per_label.values()) / len(labels)
    return {
        "macro_precision": macro_p,
        "macro_recall": macro_r,
        "macro_f1": macro_f1,
        "per_label": per_label,
    }


class ReplayIntentModel:
    """用 RecordingIntentModel 录制的文件离线重放 L2，保证评测可复现。"""

    def __init__(self, path: Path) -> None:
        self.name = "replay:" + path.name
        self._by_hash: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            self._by_hash[record["prompt_hash"]] = record["raw"]
        self.missing = 0

    def complete(self, prompt: str) -> str:
        key = sha256_text(prompt)
        if key not in self._by_hash:
            self.missing += 1
            raise RuntimeError("INTENT_L2_UNAVAILABLE: replay 中缺少该 prompt 的记录")
        return self._by_hash[key]


def evaluate(
    split: str, *, today: dt.date, model: Any = None, min_confidence: float = DEFAULT_MIN_CONFIDENCE
) -> dict[str, Any]:
    cases = load_cases(split)
    l1_hits = 0
    l1_correct = 0
    slot_total = 0
    slot_correct = 0
    pairs: list[tuple[str, str]] = []
    confusion: dict[str, Counter] = defaultdict(Counter)
    badcases: list[dict[str, Any]] = []
    source_counter: Counter = Counter()
    latencies: list[float] = []

    for case in cases:
        result = classify_intent(case.text, today=today, model=model, min_confidence=min_confidence)
        gold, pred = case.label, result.intent
        pairs.append((gold, pred))
        confusion[gold][pred] += 1
        source_counter[result.source] += 1
        latencies.append(result.latency_ms)

        l1 = classify_l1(case.text, today=today)
        if l1 is not None:
            l1_hits += 1
            if l1.intent == gold:
                l1_correct += 1
        if case.slots:
            slot_total += 1
            if all(result.slots.get(k) == v for k, v in case.slots.items()):
                slot_correct += 1
        if pred != gold:
            badcases.append(
                {
                    "case_id": case.case_id,
                    "text": case.text,
                    "label": gold,
                    "predicted": pred,
                    "source": result.source,
                    "confidence": result.confidence,
                    "l1_hit": l1 is not None,
                    "l1_intent": l1.intent if l1 else "",
                    "errors": list(result.errors),
                    "slots_expected": case.slots,
                    "slots_got": dict(result.slots),
                    "ambiguous": bool(case.annotation.get("disagreement")),
                }
            )

    total = len(cases)
    correct = sum(1 for gold, pred in pairs if gold == pred)
    scores = macro_scores(pairs, INTENT_CLASSES)
    metrics = {
        "split": split,
        "total": total,
        "correct": correct,
        "pipeline_accuracy": correct / total if total else 0.0,
        "macro_f1": scores["macro_f1"],
        "macro_precision": scores["macro_precision"],
        "macro_recall": scores["macro_recall"],
        "per_label": scores["per_label"],
        "l1_hits": l1_hits,
        "l1_coverage": l1_hits / total if total else 0.0,
        "l1_precision_on_hit": (l1_correct / l1_hits) if l1_hits else 0.0,
        "rejection_rate": sum(1 for _, pred in pairs if pred == "fallback") / total if total else 0.0,
        "slot_total": slot_total,
        "slot_accuracy": (slot_correct / slot_total) if slot_total else 1.0,
        "source_breakdown": dict(source_counter),
        "avg_latency_ms": (sum(latencies) / len(latencies)) if latencies else 0.0,
        "confusion": {gold: dict(counter) for gold, counter in sorted(confusion.items())},
    }
    return {"metrics": metrics, "badcases": badcases}


def thresholds_for(split: str) -> dict[str, float]:
    return DEV_THRESHOLDS if split == "dev" else TEST_THRESHOLDS


def gate(metrics: dict[str, Any], thresholds: dict[str, float]) -> list[dict[str, Any]]:
    rows = []
    for key, minimum in thresholds.items():
        value = float(metrics.get(key, 0.0))
        rows.append({"metric": key, "threshold": minimum, "value": value, "passed": value >= minimum})
    return rows


def render_report(payloads: dict[str, Any], l2_status: str, l2_reason: str) -> str:
    lines = ["# 意图识别评测报告（A1-02）", ""]
    lines.append(
        "数据集：agent_eval/cases/intent_{train,dev,test}.jsonl（模板合成演示语料，非真实用户日志）；"
        "版本与 hash 见 agent_eval/cases/intent_dataset.json，评测前强制校验。"
    )
    lines.append("")
    lines.append("## L2 状态")
    lines.append("")
    lines.append("- 状态：%s" % l2_status)
    lines.append("- 说明：%s" % l2_reason)
    lines.append("")
    lines.append("## 指标总览")
    lines.append("")
    header = "| 指标 | " + " | ".join(payloads.keys()) + " |"
    lines.append(header)
    lines.append("|" + "---|" * (len(payloads) + 1))
    keys = [
        ("total", "样本数"),
        ("l1_coverage", "L1 命中率"),
        ("l1_precision_on_hit", "L1 命中时准确率"),
        ("pipeline_accuracy", "端到端准确率"),
        ("macro_f1", "macro-F1"),
        ("rejection_rate", "拒识率"),
        ("slot_accuracy", "槽位准确率"),
        ("avg_latency_ms", "平均延迟 ms"),
    ]
    for key, name in keys:
        row = "| %s | " % name
        cells = []
        for payload in payloads.values():
            value = payload["metrics"].get(key, 0.0)
            if isinstance(value, float):
                cells.append("%.4f" % value if key != "avg_latency_ms" else "%.2f" % value)
            else:
                cells.append(str(value))
        row += " | ".join(cells) + " |"
        lines.append(row)
    lines.append("")
    lines.append("## 阈值（dev 与 test 分开记录）")
    lines.append("")
    lines.append("| 数据集 | 指标 | 阈值 | 实测 | 结果 |")
    lines.append("|---|---|---|---|---|")
    for split, payload in payloads.items():
        for row in payload["gate"]:
            lines.append(
                "| %s | %s | %.2f | %.4f | %s |"
                % (split, row["metric"], row["threshold"], row["value"], "通过" if row["passed"] else "未达标")
            )
    lines.append("")
    lines.append("## 分类别指标")
    lines.append("")
    for split, payload in payloads.items():
        lines.append("### %s" % split)
        lines.append("")
        lines.append("| 类别 | precision | recall | f1 | support |")
        lines.append("|---|---|---|---|---|")
        for label, row in payload["metrics"]["per_label"].items():
            lines.append(
                "| %s | %.4f | %.4f | %.4f | %d |" % (label, row["precision"], row["recall"], row["f1"], row["support"])
            )
        lines.append("")
    lines.append("## 口径说明")
    lines.append("")
    lines.append("- 端到端准确率在 L2 未测量时等价于「L1 命中且正确 + 其余落 fallback」，会低估真实能力，必须与 L2 状态一起看。")
    lines.append("- 拒识率指最终预测为 fallback 的比例，离线状态下等于 L1 未命中率。")
    lines.append("- 槽位准确率只统计带期望槽位的样本，要求期望槽位全部命中。")
    return "\n".join(lines) + "\n"


def render_badcases(payloads: dict[str, Any]) -> str:
    lines = ["# 意图识别 badcase 清单（A1-02）", ""]
    for split, payload in payloads.items():
        lines.append("## %s（%d 条错例）" % (split, len(payload["badcases"])))
        lines.append("")
        if not payload["badcases"]:
            lines.append("无。")
            lines.append("")
            continue
        lines.append("| case_id | 文本 | 期望 | 预测 | 来源 | 置信度 | L1 是否命中 | 是否歧义 | 错误码 |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for row in payload["badcases"][:80]:
            lines.append(
                "| %s | %s | %s | %s | %s | %.2f | %s | %s | %s |"
                % (
                    row["case_id"],
                    row["text"][:40],
                    row["label"],
                    row["predicted"],
                    row["source"],
                    row["confidence"],
                    "是" if row["l1_hit"] else "否",
                    "是" if row["ambiguous"] else "否",
                    "; ".join(row["errors"]) or "-",
                )
            )
        lines.append("")
    return "\n".join(lines) + "\n"


def render_confusion(payloads: dict[str, Any]) -> str:
    lines = ["# 意图识别混淆矩阵（A1-02）", ""]
    for split, payload in payloads.items():
        lines.append("## %s" % split)
        lines.append("")
        lines.append("| 期望 \\ 预测 | " + " | ".join(INTENT_CLASSES) + " |")
        lines.append("|" + "---|" * (len(INTENT_CLASSES) + 1))
        for gold in INTENT_CLASSES:
            row = payload["metrics"]["confusion"].get(gold, {})
            cells = [str(row.get(pred, 0)) for pred in INTENT_CLASSES]
            lines.append("| %s | " % gold + " | ".join(cells) + " |")
        lines.append("")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="pq_agent 意图识别离线评测")
    parser.add_argument("--splits", default="dev,test")
    parser.add_argument("--l2-mode", choices=("off", "replay", "live"), default="off")
    parser.add_argument("--replay", default="")
    parser.add_argument("--record", default="")
    parser.add_argument("--min-confidence", type=float, default=DEFAULT_MIN_CONFIDENCE)
    parser.add_argument("--out", default=str(RESULTS_DIR))
    args = parser.parse_args(argv)

    model = None
    l2_status = "NOT_MEASURED"
    l2_reason = "未提供 L2 模型；本机缺 base_url，无法调用真实中转站。"
    if args.l2_mode == "replay":
        if not args.replay:
            raise SystemExit("REPLAY_PATH_REQUIRED")
        model = ReplayIntentModel(Path(args.replay))
        l2_status = "REPLAYED"
        l2_reason = "用录制文件重放 L2，只验证编排与解析，不代表线上模型准确率。"
    elif args.l2_mode == "live":
        credentials = credentials_from_environ()
        if credentials is None:
            raise SystemExit("L2_CREDENTIALS_MISSING: 需要同时提供 API key 与 base_url，脚本拒绝回退到默认公网地址")
        inner = DeepSeekIntentModel(credentials)
        model = RecordingIntentModel(inner, Path(args.record)) if args.record else inner
        l2_status = "LIVE"
        l2_reason = "调用真实模型端点，结果写入录制文件后可离线重放。"

    meta = load_dataset_meta()
    reference_date = dt.date.fromisoformat(meta["reference_date"])

    payloads: dict[str, Any] = {}
    for split in [s.strip() for s in args.splits.split(",") if s.strip()]:
        result = evaluate(split, today=reference_date, model=model, min_confidence=args.min_confidence)
        result["gate"] = gate(result["metrics"], thresholds_for(split))
        payloads[split] = result

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "task": "A1-02",
        "dataset_version": meta["dataset_version"],
        "dataset_reference_date": reference_date.isoformat(),
        "dataset_hashes": {name: info["sha256"] for name, info in meta["splits"].items()},
        "dataset_counts": {name: info["count"] for name, info in meta["splits"].items()},
        "l2_status": l2_status,
        "l2_reason": l2_reason,
        "l2_missing_prompts": getattr(model, "missing", 0) if model is not None else 0,
        "dev_thresholds": DEV_THRESHOLDS,
        "test_thresholds": TEST_THRESHOLDS,
        "splits": {name: payload["metrics"] for name, payload in payloads.items()},
        "gates": {name: payload["gate"] for name, payload in payloads.items()},
        "badcase_counts": {name: len(payload["badcases"]) for name, payload in payloads.items()},
    }
    (out_dir / "intent_eval.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8", newline="\n"
    )
    (out_dir / "intent_report.md").write_text(render_report(payloads, l2_status, l2_reason), encoding="utf-8", newline="\n")
    (out_dir / "intent_badcases.md").write_text(render_badcases(payloads), encoding="utf-8", newline="\n")
    (out_dir / "intent_confusion.md").write_text(render_confusion(payloads), encoding="utf-8", newline="\n")

    print("L2 状态：%s" % l2_status)
    for split, payload in payloads.items():
        m = payload["metrics"]
        passed = all(row["passed"] for row in payload["gate"])
        print(
            "%-4s 样本=%d 端到端准确率=%.4f macro-F1=%.4f L1 命中率=%.4f 拒识率=%.4f 槽位准确率=%.4f 阈值=%s"
            % (
                split,
                m["total"],
                m["pipeline_accuracy"],
                m["macro_f1"],
                m["l1_coverage"],
                m["rejection_rate"],
                m["slot_accuracy"],
                "通过" if passed else "未达标",
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
