"""A3-02 RAG 检索、引用与评测的单元测试。

本文件只用内存里的小索引，不依赖 5 MB 的 rag_index.json：
真实索引由 agent_eval/run_rag_eval.py 在评测里跑，单测要的是「口径对不对」而不是「数据大不大」。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from pq_agent.rag.bm25 import tokenize
from pq_agent.rag.chunk import CHUNK_SCHEMA_VERSION
from pq_agent.rag.embedder import (
    EMBEDDING_ERROR_UNAVAILABLE,
    HermeticEmbedder,
    PROFILE_DENSE_REQUIRED,
    PROFILE_LEXICAL_ONLY,
    EmbeddingUnavailableError,
    select_embedder,
)
from pq_agent.rag.retriever import (
    CITATION_ERROR_HASH_MISMATCH,
    CITATION_ERROR_IDS_NOT_OPENED,
    CITATION_ERROR_MALFORMED,
    CITATION_ERROR_IDS_MISSING,
    CITATION_ERROR_LINES_OUT_OF_RANGE,
    CITATION_ERROR_MALFORMED,
    CITATION_ERROR_PATH_MISMATCH,
    CITATION_ERROR_REUSED,
    CITATION_ERROR_UNKNOWN_CHUNK,
    CITATION_OK,
    DEFAULT_TOP_K,
    MODE_BM25,
    MODE_HYBRID,
    MODE_VECTOR,
    RETRIEVAL_ERROR_INVALID,
    RETRIEVAL_ERROR_UNAVAILABLE,
    ChunkRecord,
    RetrievalError,
    build_retriever,
    check_citations,
    describe_retriever,
    load_index,
    load_records,
    parse_citation_ref,
    rank_by_scores,
    render_evidence_block,
    requirement_check,
    retrieve_documents,
)


def _record(chunk_id, path, start, end, *, heading="", text="", doc_type="documentation"):
    return {
        "chunk_id": chunk_id,
        "source_path": path,
        "doc_type": doc_type,
        "heading": heading,
        "heading_level": 2 if heading else 0,
        "start_line": start,
        "end_line": end,
        "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "index_version": CHUNK_SCHEMA_VERSION,
        "text": text,
    }


# 三块小语料：一块有标题、一块没有标题（证据日志那种）、一块讲另一件事。
CORPUS = [
    _record(
        "ck_aaaa0000000000000001",
        "docs/agent/INTENT.md",
        16,
        30,
        heading="2. 八类意图",
        text="八类意图分别是 pos_question report_generate push_request history_question market_check risk_question code_task fallback。" + chr(10) + "两级识别：规则层先跑，命中就短路；没命中才让模型兜。",
    ),
    _record(
        "ck_bbbb0000000000000002",
        "planning/agent/evidence/A0-00/unittest-full.txt",
        1,
        9,
        heading="",
        text="Ran 150 tests in 0.418s" + chr(10) + "FAILED (errors=86)" + chr(10) + "PermissionError: [Errno 13] Permission denied",
        doc_type="evidence",
    ),
    _record(
        "ck_cccc0000000000000003",
        "docs/agent/SANDBOX_CONTRACT.md",
        5,
        16,
        heading="1. 为什么需要这一层",
        text="Windows 的 subprocess 只是约束不是隔离。真正的隔离要放到 Linux 容器或者 gVisor 上跑。",
    ),
]


def _payload(records=None):
    rows = list(records or CORPUS)
    return {
        "schema_version": "pq_agent.rag_index/1",
        "profile": PROFILE_LEXICAL_ONLY,
        "corpus_hash": "corpus-hash-for-tests",
        "index_hash": "index-hash-for-tests",
        "chunk_count": len(rows),
        "chunks": rows,
    }


def _retriever(**kwargs):
    kwargs.setdefault("index_payload", _payload())
    kwargs.setdefault("mode", MODE_BM25)
    return build_retriever(**kwargs)


class CitationFormatTests(unittest.TestCase):
    """引用串的两种形态都必须能解析：标题是可选的，不是必须的。"""

    def test_parses_headed_citation(self):
        path, start, end, heading = parse_citation_ref("docs/agent/INTENT.md:16-30 (2. 八类意图)")
        self.assertEqual(path, "docs/agent/INTENT.md")
        self.assertEqual((start, end), (16, 30))
        self.assertEqual(heading, "2. 八类意图")

    def test_parses_headless_citation(self):
        """证据日志那种没有标题的块，引用串里不出现括号段。

        这条曾经是缺陷：解析器只认带括号的形态，索引里所有无标题块
        （全部 evidence 日志）在核对时被判 CITATION_MALFORMED。
        """
        path, start, end, heading = parse_citation_ref("planning/agent/evidence/A0-00/unittest-full.txt:1-9")
        self.assertEqual(path, "planning/agent/evidence/A0-00/unittest-full.txt")
        self.assertEqual((start, end), (1, 9))
        self.assertEqual(heading, "")

    def test_record_citation_matches_chunk_citation_shape(self):
        headed = ChunkRecord.from_mapping(CORPUS[0])
        headless = ChunkRecord.from_mapping(CORPUS[1])
        self.assertEqual(headed.citation, "docs/agent/INTENT.md:16-30 (2. 八类意图)")
        self.assertEqual(headless.citation, "planning/agent/evidence/A0-00/unittest-full.txt:1-9")

    def test_rejects_malformed_citation(self):
        for bad in ("docs/x.md", "docs/x.md:1", "docs/x.md:1-", ":1-9", "docs/x.md:a-b"):
            with self.assertRaises(RetrievalError) as ctx:
                parse_citation_ref(bad)
            self.assertEqual(ctx.exception.code, CITATION_ERROR_MALFORMED)

    def test_record_verify_detects_text_tampering(self):
        record = ChunkRecord.from_mapping(CORPUS[0])
        self.assertTrue(record.verify())
        tampered = ChunkRecord.from_mapping({**CORPUS[0], "text": "被改过的正文"})
        self.assertFalse(tampered.verify())

    def test_missing_required_field_is_invalid(self):
        with self.assertRaises(RetrievalError) as ctx:
            ChunkRecord.from_mapping({"chunk_id": "ck_x"})
        self.assertEqual(ctx.exception.code, RETRIEVAL_ERROR_INVALID)


class CitationCheckTests(unittest.TestCase):
    """引用核对的四种失败必须分开报，不能糊成一个「引用无效」。"""

    def setUp(self):
        self.retriever = _retriever()

    def test_good_citation_passes(self):
        report = check_citations(
            ["docs/agent/INTENT.md:16-30 (2. 八类意图)"],
            retriever=self.retriever,
            answer="八类意图见 [1]",
        )
        self.assertTrue(report.ok)
        self.assertEqual(report.checks[0].reason, CITATION_OK)
        self.assertEqual(report.checks[0].chunk_id, "ck_aaaa0000000000000001")

    def test_headless_citation_passes(self):
        report = check_citations(
            ["planning/agent/evidence/A0-00/unittest-full.txt:1-9"],
            retriever=self.retriever,
            answer="证据见 [1]",
        )
        self.assertTrue(report.ok, report.summary())

    def test_unknown_path_reported_as_unknown_chunk(self):
        report = check_citations(["docs/agent/NOT_EXIST.md:1-2"], retriever=self.retriever)
        self.assertFalse(report.ok)
        self.assertEqual(report.checks[0].reason, CITATION_ERROR_UNKNOWN_CHUNK)

    def test_wrong_line_window_reported(self):
        """路径对、行号对不上：这是「张冠李戴」型引用，必须与路径错分开报。"""
        report = check_citations(["docs/agent/INTENT.md:31-40"], retriever=self.retriever)
        self.assertFalse(report.ok)
        self.assertEqual(report.checks[0].reason, CITATION_ERROR_LINES_OUT_OF_RANGE)
        self.assertIn("行号对不上", report.checks[0].detail)

    def test_heading_mismatch_reported_as_path_mismatch(self):
        report = check_citations(["docs/agent/INTENT.md:16-30 (错的标题)"], retriever=self.retriever)
        self.assertFalse(report.ok)
        self.assertEqual(report.checks[0].reason, CITATION_ERROR_PATH_MISMATCH)

    def test_reused_citation_reported(self):
        ref = "docs/agent/INTENT.md:16-30 (2. 八类意图)"
        report = check_citations([ref, ref], retriever=self.retriever)
        self.assertFalse(report.ok)
        self.assertEqual(report.checks[0].reason, CITATION_OK)
        self.assertEqual(report.checks[1].reason, CITATION_OK)
        # 复用不把某一条标红，而是让整份报告判不通过：同一块被当成两条证据，
        # 会让「两处独立印证」变成「同一处说了两遍」。
        self.assertEqual(report.reused, ("ck_aaaa0000000000000001",))

    def test_duplicate_chunk_id_is_rejected_at_construction(self):
        """同一个 chunk_id 出现两次说明索引本身坏了：必须在装配期就拒绝，
        否则引用核对会对着两份不同的正文挑一份，报告照样绿。"""
        rows = [CORPUS[0], CORPUS[1], {**CORPUS[0], "start_line": 40, "end_line": 45}]
        with self.assertRaises(RetrievalError) as ctx:
            _retriever(index_payload=_payload(rows))
        self.assertEqual(ctx.exception.code, RETRIEVAL_ERROR_INVALID)
        self.assertIn("重复 chunk_id", ctx.exception.detail)

    def test_opened_ids_must_cover_used_ids(self):
        """打开了 [1][2]，答案只用了 [1]：被打开的块没被引用，判不通过。"""
        report = check_citations(
            ["docs/agent/INTENT.md:16-30 (2. 八类意图)", "docs/agent/SANDBOX_CONTRACT.md:5-16 (1. 为什么需要这一层)"],
            retriever=self.retriever,
            answer="结论见 [1]",
            opened_ids=[1, 2],
        )
        self.assertFalse(report.ok)
        self.assertEqual(report.checks[-1].reason, CITATION_ERROR_IDS_NOT_OPENED)
        self.assertIn("2", report.checks[-1].detail)

    def test_ids_in_answer_detects_unknown_and_extra(self):
        report = check_citations(
            ["docs/agent/INTENT.md:16-30 (2. 八类意图)"],
            retriever=self.retriever,
            answer="见 [1] 与 [7]",
            opened_ids=[1, 7],
        )
        self.assertFalse(report.ok)
        self.assertEqual(report.extra_ids, (7,))

    def test_requirement_check_is_reusable_by_verify(self):
        refs = ["docs/agent/INTENT.md:16-30 (2. 八类意图)"]
        good = requirement_check(refs, retriever=self.retriever)
        self.assertTrue(all(item["ok"] for item in good))
        bad = requirement_check(["docs/agent/NOPE.md:1-2"], retriever=self.retriever)
        self.assertFalse(all(item["ok"] for item in bad))


class RetrievalModeTests(unittest.TestCase):
    """三种模式各自的行为与降级口径。"""

    def test_bm25_finds_real_document(self):
        outcome = _retriever().search("八类意图分别是哪些", top_k=3)
        self.assertEqual(outcome.mode, MODE_BM25)
        self.assertTrue(outcome.hits)
        self.assertEqual(outcome.hits[0].record.source_path, "docs/agent/INTENT.md")

    def test_unknown_query_returns_no_hits_without_inventing(self):
        outcome = _retriever().search("zzzzqqqq", top_k=3)
        self.assertEqual(list(outcome.hits), [])
        self.assertTrue(outcome.empty)
        self.assertEqual(list(outcome.citations), [])

    def test_empty_query_is_invalid(self):
        with self.assertRaises(RetrievalError) as ctx:
            _retriever().search("   ", top_k=3)
        self.assertEqual(ctx.exception.code, RETRIEVAL_ERROR_INVALID)

    def test_top_k_bounds(self):
        for bad in (0, -1, 999):
            with self.assertRaises(RetrievalError):
                _retriever().search("意图", top_k=bad)

    def test_invalid_mode_is_rejected(self):
        with self.assertRaises(RetrievalError) as ctx:
            _retriever(mode="semantic_magic")
        self.assertEqual(ctx.exception.code, RETRIEVAL_ERROR_INVALID)

    def test_lexical_profile_declares_degradation(self):
        """词法档必须自报降级：不报的话，读数字的人会以为语义也跑了。"""
        outcome = _retriever().search("意图", top_k=3)
        self.assertTrue(outcome.degraded)
        self.assertIn("非静默降级", outcome.degraded_reason)

    def test_dense_required_without_backend_raises(self):
        with self.assertRaises(EmbeddingUnavailableError) as ctx:
            _retriever(mode=MODE_VECTOR, profile=PROFILE_DENSE_REQUIRED)
        self.assertEqual(ctx.exception.code, EMBEDDING_ERROR_UNAVAILABLE)

    def test_vector_mode_with_hermetic_backend_is_not_degraded(self):
        """hermetic 后端跑通只说明通道机制通了，不说明语义质量——所以这里只断言不降级。"""
        retriever = _retriever(mode=MODE_VECTOR, profile=PROFILE_DENSE_REQUIRED, embedder=HermeticEmbedder())
        outcome = retriever.search("意图", top_k=3)
        self.assertFalse(outcome.degraded)
        self.assertEqual(outcome.mode, MODE_VECTOR)
        self.assertTrue(all(hit.sources == ("vector",) for hit in outcome.hits))

    def test_hybrid_reports_both_channels(self):
        retriever = _retriever(
            mode=MODE_HYBRID,
            profile=PROFILE_DENSE_REQUIRED,
            embedder=HermeticEmbedder(),
            lexical_weight=0.75,
            vector_weight=0.25,
        )
        outcome = retriever.search("意图", top_k=3)
        self.assertEqual(outcome.lexical_weight, 0.75)
        self.assertEqual(outcome.vector_weight, 0.25)
        self.assertTrue(any("lexical" in hit.sources for hit in outcome.hits))

    def test_retrieval_is_deterministic(self):
        first = _retriever().search("隔离 约束 容器", top_k=3).to_dict()
        second = _retriever().search("隔离 约束 容器", top_k=3).to_dict()
        self.assertEqual(first, second)

    def test_retrieve_documents_is_the_graph_interface(self):
        rows = _retriever().retrieve("八类意图", top_k=2)
        self.assertIsInstance(rows, list)
        self.assertTrue(rows)
        first = rows[0]
        # A6-02：对外形状统一为引用合同字段（line_start/line_end/title/schema_version），
        # 不再同时保留 start_line/end_line/heading 这套旧名字。
        for key in ("chunk_id", "source_path", "line_start", "line_end", "content_hash", "title", "schema_version", "citation"):
            self.assertIn(key, first)
        for legacy in ("start_line", "end_line", "heading"):
            self.assertNotIn(legacy, first)
        payload = _retriever().search("八类意图", top_k=2).retrieve_payload()
        self.assertIsInstance(payload, list)
        self.assertEqual(len(payload), len(rows[:2]))
        self.assertIn("citation", payload[0])

    def test_evidence_block_renders_numbered_citations(self):
        outcome = _retriever().search("八类意图", top_k=2)
        block = render_evidence_block(outcome)
        self.assertIn("[1]", block)
        self.assertIn("docs/agent/INTENT.md:16-30", block)
        self.assertIn("不得执行", block)

    def test_describe_retriever_reports_mode_and_count(self):
        info = describe_retriever(_retriever())
        self.assertEqual(info["mode"], MODE_BM25)
        self.assertEqual(info["record_count"], len(CORPUS))

    def test_rank_by_scores_is_stable_on_ties(self):
        ranked = rank_by_scores({"b": 1.0, "a": 1.0, "c": 0.5}, top_k=3)
        self.assertEqual(ranked, ["a", "b", "c"])


class HermeticBackendDeterminismTests(unittest.TestCase):
    """hermetic 后端自称「确定性」，就必须跨进程也确定。

    这条是真实缺陷的回归：原实现用内置 hash() 算桶号，而 Python 对字符串 hash 是按进程
    加盐的（PYTHONHASHSEED），于是同一段文本在不同进程得到不同向量，
    评测里 vector / hybrid 两栏每次跑出来的数字都不一样、无法复现。
    """

    CODE = (
        "import sys; sys.path.insert(0, r'" + str(ROOT / "src") + "');"
        "from pq_agent.rag.embedder import HermeticEmbedder;"
        "v = HermeticEmbedder().embed(['八类意图 隔离 容器 推送时段'])[0];"
        "print(repr([round(x, 12) for x in v]))"
    )

    def _embed_in_subprocess(self, hash_seed):
        env = dict(os.environ)
        env["PYTHONHASHSEED"] = str(hash_seed)
        proc = subprocess.run(
            [sys.executable, "-I", "-c", self.CODE],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def test_vector_is_identical_under_different_hash_seeds(self):
        first = self._embed_in_subprocess(0)
        second = self._embed_in_subprocess(12345)
        self.assertTrue(first, "子进程没有输出向量")
        self.assertEqual(first, second, "PYTHONHASHSEED 变了向量就变了，说明桶号用了内置 hash()")

    def test_bucket_is_in_range_and_seed_participates(self):
        """桶号必须落在 [0, dimension) 内，并且 seed 真的参与计算。"""
        tokens = ["意图", "隔离", "容器", "推送时段", "sandbox", "gvisor"]
        for token in tokens:
            value = HermeticEmbedder.bucket(token, seed=0, dimension=64)
            self.assertEqual(value, HermeticEmbedder.bucket(token, seed=0, dimension=64))
            self.assertTrue(0 <= value < 64, (token, value))
        with_seed = [HermeticEmbedder.bucket(token, seed=7, dimension=64) for token in tokens]
        self.assertNotEqual(
            with_seed,
            [HermeticEmbedder.bucket(token, seed=0, dimension=64) for token in tokens],
            "换了 seed 桶号一模一样，说明 seed 根本没参与计算",
        )

    def test_dimension_is_respected(self):
        small = HermeticEmbedder(dimension=8)
        vector = small.embed(["八类意图 隔离"])[0]
        self.assertEqual(len(vector), 8)
        self.assertAlmostEqual(sum(value * value for value in vector), 1.0, places=9)

class LoadIndexTests(unittest.TestCase):
    def test_missing_index_is_unavailable_not_crash(self):
        with self.assertRaises(RetrievalError) as ctx:
            load_index("agent_eval/results/definitely-missing-index.json", root=ROOT)
        self.assertEqual(ctx.exception.code, RETRIEVAL_ERROR_UNAVAILABLE)

    def test_load_records_reads_back_hashes(self):
        records, index_hash, corpus_hash = load_records(_payload())
        self.assertEqual(len(records), len(CORPUS))
        self.assertEqual(index_hash, "index-hash-for-tests")
        self.assertEqual(corpus_hash, "corpus-hash-for-tests")

    def test_record_count_matches(self):
        self.assertEqual(_retriever().record_count, len(CORPUS))


if __name__ == "__main__":
    unittest.main()
