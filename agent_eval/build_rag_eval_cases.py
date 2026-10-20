"""生成 30 题分桶 RAG 评测集（A3-02）。

为什么要有这个脚本：题目、金标、分桶、划分都必须是可重建的，
不能是手写一份 JSON 就算数——否则"这题为什么算对"永远说不清。

金标形状是 (路径, 关键词)：检索到的高分块只要来自该路径且正文含该关键词，就算命中。
这样做有两个好处：
  1. 不绑定具体 chunk_id，索引重建后题目仍然有效；
  2. 关键词就是"答案数字/术语"，所以"数字一致性"可以直接由命中判定承担，
     不需要再让模型判分（也就没有模型自评的循环论证）。

三道自检（缺一不可，失败就退出，不写文件）：
  A. 每题的金标路径必须真的在索引里；
  B. 每题的金标路径下必须至少有一个块含该关键词；
  C. 题目文本不得与任何块的正文高度重合（防止 query leakage：把原文当问题，
     词法检索当然第一，那种分数毫无意义）。
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from pq_agent.rag.retriever import load_index, DEFAULT_INDEX_PATH  # noqa: E402

CASES_PATH = ROOT / "agent_eval" / "cases" / "rag_eval_cases.json"
DATASET_VERSION = "rag-eval-cases/1"

# (id, bucket, split, question, gold_path, gold_keyword)
QUESTIONS: tuple[tuple[str, str, str, str, str, str], ...] = (
    # ---- 事实类：直接问文档里的一句话 ----
    ("fact-01", "fact", "train", "意图识别为什么做成两级，L1 的命中阈值是多少？", "docs/agent/INTENT.md", "L1_HIT_THRESHOLD"),
    ("fact-02", "fact", "train", "等级字段被写死成了什么值，为什么不能把它说成隔离？", "docs/agent/SANDBOX_CONTRACT.md", "constraint_only"),
    ("fact-03", "fact", "dev", "引用串的格式长什么样，只给文件名算不算引用？", "docs/agent/RAG_DATA_POLICY.md", "起始行"),
    ("fact-04", "fact", "test", "审批记录的 TTL 是多少，过期之后会怎样？", "docs/agent/HITL_CONTRACT.md", "TTL"),
    ("fact-05", "fact", "train", "API 层的鉴权与跨站防护一共几道闸门，顺序是什么？", "docs/agent/API_CONTRACT.md", "鉴权"),
    ("fact-06", "fact", "dev", "哪些目录前缀被明确拒绝进入 RAG 语料？", "docs/agent/RAG_DATA_POLICY.md", "DENIED_PREFIXES"),
    ("fact-07", "fact", "test", "工具能力是怎么分级的，为什么要按能力分级？", "docs/agent/CLI_TOOL_MATRIX.md", "能力"),
    ("fact-08", "fact", "train", "为什么核心包必须保持零第三方依赖，agent 依赖怎么隔离？", "docs/agent/EVAL.md", "optional Agent"),

    # ---- 数字类：答案是一个具体数字或阈值 ----
    ("number-01", "number", "train", "CLI 一共有多少个子命令，其中多少个有副作用？", "docs/agent/CLI_TOOL_MATRIX.md", "26"),
    ("number-02", "number", "dev", "能力清单总共多少条，其中能真正拿到保证的有多少条？", "docs/agent/SANDBOX_CONTRACT.md", "12 项"),
    ("number-03", "number", "train", "审批过期时间是多少秒？", "docs/agent/HITL_CONTRACT.md", "900"),
    ("number-04", "number", "test", "单个文件进入语料的大小上限是多少？", "docs/agent/RAG_DATA_POLICY.md", "512"),
    ("number-05", "number", "train", "L1 规则层的强词和弱词分别加多少分？", "docs/agent/INTENT.md", "+3"),
    ("number-06", "number", "dev", "切块时单块超过多少字会继续按行切？", "docs/agent/RAG_DATA_POLICY.md", "1200"),
    ("number-07", "number", "test", "意图一共分成几类？", "docs/agent/INTENT.md", "八类"),
    ("number-08", "number", "train", "用户态内核那层一共有多少条能力，比容器层多出几条？", "docs/agent/GVISOR_DEPLOYMENT.md", "16 条"),

    # ---- 表格类：答案在文档的表格里 ----
    ("table-01", "table", "train", "八类意图各自的优先级打分分别是多少？", "docs/agent/INTENT.md", "risk_question"),
    ("table-02", "table", "dev", "本机根本做不到的那些限制分别是什么？", "docs/agent/SANDBOX_CONTRACT.md", "network_isolation"),
    ("table-03", "table", "test", "审批的参数绑定不匹配时会报什么错？", "docs/agent/HITL_CONTRACT.md", "APPROVAL_ACTION_MISMATCH"),
    ("table-04", "table", "train", "API 都有哪些端点，SSE 事件怎么排序？", "docs/agent/API_CONTRACT.md", "SSE"),
    ("table-05", "table", "dev", "CLI 里哪些命令需要人工确认？", "docs/agent/CLI_TOOL_MATRIX.md", "人工确认"),
    ("table-06", "table", "test", "如果没有沙箱这一层，会出现哪些具体问题？", "docs/agent/SANDBOX_CONTRACT.md", "死循环"),
    ("table-07", "table", "train", "RAG 的两档 profile 分别是什么，降级时怎么标记？", "docs/agent/RAG_DATA_POLICY.md", "lexical_only"),

    # ---- 多跳类：要跨小节甚至跨文档把几件事串起来 ----
    ("multi-01", "multi_hop", "train", "有副作用的工具调用会在哪个节点被拦下来等人确认？", "docs/agent/HITL_CONTRACT.md", "interrupt"),
    ("multi-02", "multi_hop", "dev", "为什么本机这层只叫约束层，不能叫安全隔离层？", "docs/agent/SANDBOX_CONTRACT.md", "安全隔离"),
    ("multi-03", "multi_hop", "test", "一条引用要被判为有效，必须核对哪几项？", "docs/agent/RAG_DATA_POLICY.md", "content_hash"),
    ("multi-04", "multi_hop", "train", "什么情况下流程图会先停下来等人点头？", "docs/agent/GRAPH_CONTRACT.md", "approval"),
    ("multi-05", "multi_hop", "dev", "遥测里绝对不允许出现哪些内容？", "docs/agent/TELEMETRY_SCHEMA.md", "prompt"),
    ("multi-06", "multi_hop", "test", "密钥策略对 webhook 的存储有什么硬规定？", "docs/agent/SECRET_POLICY.md", "webhook"),
    ("multi-07", "multi_hop", "train", "平台需要分别接收和查询哪些观测项？", "docs/agent/APM_DEPLOYMENT.md", "traces, metrics, and logs"),
)


def _normalize(text: str) -> str:
    """只留字母数字和汉字，用来做重合度比较。"""
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", str(text)).lower()


def _ngrams(text: str, size: int = 8) -> set[str]:
    normalized = _normalize(text)
    if len(normalized) < size:
        return {normalized} if normalized else set()
    return {normalized[index : index + size] for index in range(len(normalized) - size + 1)}


def content_hash(question: str, gold_path: str, gold_keyword: str, bucket: str) -> str:
    payload = json.dumps(
        {
            "question": question,
            "gold_path": gold_path,
            "gold_keyword": gold_keyword,
            "bucket": bucket,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def main() -> int:
    payload = load_index(DEFAULT_INDEX_PATH, root=ROOT)
    chunks = payload.get("chunks") or []
    paths = {str(item.get("source_path")) for item in chunks}
    by_path: dict[str, list[str]] = {}
    for item in chunks:
        by_path.setdefault(str(item.get("source_path")), []).append(str(item.get("text") or ""))

    errors: list[str] = []
    seen_ids: set[str] = set()
    cases: list[dict[str, object]] = []
    for case_id, bucket, split, question, gold_path, gold_keyword in QUESTIONS:
        if case_id in seen_ids:
            errors.append(f"{case_id}: 用例 id 重复")
        seen_ids.add(case_id)
        if gold_path not in paths:
            errors.append(f"{case_id}: 金标路径不在索引里：{gold_path}")
            continue
        texts = by_path.get(gold_path, [])
        if not any(gold_keyword in text for text in texts):
            errors.append(f"{case_id}: 金标路径 {gold_path} 下没有任何块包含关键词 {gold_keyword!r}")
            continue
        # C 项自检：题目与块正文的重合度。8-gram 重合超过 2 个就算泄漏嫌疑。
        question_grams = _ngrams(question)
        worst = 0
        worst_path = ""
        for path, doc_texts in by_path.items():
            for text in doc_texts:
                overlap = len(question_grams & _ngrams(text))
                if overlap > worst:
                    worst = overlap
                    worst_path = path
        if worst > 2:
            errors.append(
                f"{case_id}: 题目与 {worst_path} 的块正文有 {worst} 个 8-gram 重合，疑似 query leakage"
            )
            continue
        cases.append(
            {
                "case_id": case_id,
                "bucket": bucket,
                "split": split,
                "question": question,
                "gold_path": gold_path,
                "gold_keyword": gold_keyword,
                "content_hash": content_hash(question, gold_path, gold_keyword, bucket),
            }
        )

    if errors:
        print("自检失败，未写文件：")
        for item in errors:
            print("  - " + item)
        return 1

    by_split: dict[str, int] = {}
    by_bucket: dict[str, int] = {}
    for item in cases:
        by_split[str(item["split"])] = by_split.get(str(item["split"]), 0) + 1
        by_bucket[str(item["bucket"])] = by_bucket.get(str(item["bucket"]), 0) + 1

    document = {
        "schema_version": DATASET_VERSION,
        "dataset_version": DATASET_VERSION,
        "generated_by": "agent_eval/build_rag_eval_cases.py",
        "index_path": DEFAULT_INDEX_PATH,
        "index_hash": str(payload.get("index_hash") or ""),
        "corpus_hash": str(payload.get("corpus_hash") or ""),
        "profile": str(payload.get("profile") or ""),
        "honesty_note": (
            "题目由本仓库文档内容人工撰写，金标是(路径, 关键词)，不是模型自动生成；",
            "分桶为 fact/number/table/multi_hop；train/dev/test 固定划分；",
            "调参只用 train 与 dev，test 只用于最终报告。"
        ),
        "counts": {"total": len(cases), "by_split": by_split, "by_bucket": by_bucket},
        "cases": cases,
    }
    text = json.dumps(document, ensure_ascii=False, indent=2) + chr(10)
    CASES_PATH.parent.mkdir(parents=True, exist_ok=True)
    CASES_PATH.write_text(text, encoding="utf-8")
    print(f"wrote {CASES_PATH.name}: total={len(cases)} splits={by_split} buckets={by_bucket}")
    print(f"index_hash={document['index_hash'][:16]} corpus_hash={document['corpus_hash'][:16]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
