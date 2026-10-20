"""A6-02：引用合同统一 + 索引 freshness 的集成测试。

三件事必须在同一条链路上被证明：
1. 真实 Retriever 的输出能直接喂给 Citation.from_dict：不是差不多的字段，是同一份合同；
2. 真实 Retriever 到 LangGraph state 再到 compose_answer 的回答里真的有可校验的引用；
3. 源文档改了之后，旧索引必须被拒绝，而不是继续返回过期知识。
"""

from __future__ import annotations

import pathlib
import sys
import unittest
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from pq_agent.contracts import Citation  # noqa: E402
from pq_agent.graph import GraphDeps, build_graph, run_graph  # noqa: E402
from pq_agent.rag import freshness as freshness_module  # noqa: E402
from pq_agent.rag.build_index import build_index, write_index  # noqa: E402
from pq_agent.rag.retriever import build_retriever, verify_citation_against  # noqa: E402
from tests.agent._graph_fakes import FakeProvider, FakeTools, fixed_clock, make_state  # noqa: E402


def _tmp_dir(name: str) -> pathlib.Path:
    path = ROOT / ".tmp" / "a6-02-tests" / (name + "-" + uuid.uuid4().hex[:8])
    path.mkdir(parents=True, exist_ok=True)
    return path


def _make_corpus(root: pathlib.Path) -> pathlib.Path:
    """造一个两篇小文档的语料；只用于测合同，不冒充真实知识库。"""
    docs = root / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    risk = "\n".join(["# 风控规则", "", "单票仓位上限为百分之二十。", "禁止在无法冻结资金的情况下加仓。", ""])
    push = "\n".join(["# 推送时段", "", "早盘建仓推荐在九点二十六分生成。", "尾盘隔夜套利在十四点三十五分生成。", ""])
    (docs / "risk.md").write_text(risk, encoding="utf-8")
    (docs / "push.md").write_text(push, encoding="utf-8")
    return docs


def _build_index(root: pathlib.Path, index_path: pathlib.Path) -> None:
    result = build_index(root=root, profile="lexical_only")
    write_index(result, path=str(index_path), root=root)
    freshness_module.write_snapshot(index_path, root=root)


class CitationContractTests(unittest.TestCase):
    def test_retrieval_payload_roundtrips_through_citation(self) -> None:
        root = _tmp_dir("corpus")
        _make_corpus(root)
        index_path = root / "index.json"
        _build_index(root, index_path)
        retriever = build_retriever(index_path=index_path, root=root, freshness="require")
        payload = retriever.retrieve("风控 仓位 上限", top_k=3)
        self.assertTrue(payload, "检索不到任何块，合同测试无从谈起")
        for item in payload:
            citation = Citation.from_dict(item)
            self.assertEqual(citation.source_path, item["source_path"])
            self.assertEqual(citation.line_start, item["line_start"])
            self.assertEqual(citation.line_end, item["line_end"])
            self.assertEqual(citation.content_hash, item["content_hash"])
            self.assertTrue(citation.chunk_id)


    def test_citation_fields_are_not_duplicated_aliases(self) -> None:
        root = _tmp_dir("alias")
        _make_corpus(root)
        index_path = root / "index.json"
        _build_index(root, index_path)
        retriever = build_retriever(index_path=index_path, root=root, freshness="require")
        item = retriever.retrieve("推送 时段", top_k=1)[0]
        for legacy in ("start_line", "end_line", "heading"):
            self.assertNotIn(legacy, item, "不允许同时保留旧字段名 " + legacy)
        for field in ("line_start", "line_end", "title", "schema_version"):
            self.assertIn(field, item)


    def test_tampered_citation_is_rejected(self) -> None:
        root = _tmp_dir("tamper")
        _make_corpus(root)
        index_path = root / "index.json"
        _build_index(root, index_path)
        retriever = build_retriever(index_path=index_path, root=root, freshness="require")
        item = dict(retriever.retrieve("风控", top_k=1)[0])
        records = {record.chunk_id: record for record in retriever.records}
        ok, reason = verify_citation_against(records, item)
        self.assertTrue(ok, reason)
        broken = dict(item)
        broken["line_end"] = int(item["line_end"]) + 99
        ok2, reason2 = verify_citation_against(records, broken)
        self.assertFalse(ok2)
        self.assertTrue(reason2)


class FreshnessTests(unittest.TestCase):
    def test_changed_source_document_invalidates_the_index(self) -> None:
        root = _tmp_dir("fresh")
        docs = _make_corpus(root)
        index_path = root / "index.json"
        _build_index(root, index_path)
        build_retriever(index_path=index_path, root=root, freshness="require")
        target = docs / "risk.md"
        target.write_text(target.read_text(encoding="utf-8") + chr(10) + "新增一条风控红线。" + chr(10), encoding="utf-8")
        with self.assertRaises(freshness_module.FreshnessError) as ctx:
            build_retriever(index_path=index_path, root=root, freshness="require")
        self.assertEqual(getattr(ctx.exception, "code", ""), freshness_module.ERROR_INDEX_STALE, str(ctx.exception))


    def test_freshness_off_is_explicit(self) -> None:
        root = _tmp_dir("off")
        docs = _make_corpus(root)
        index_path = root / "index.json"
        _build_index(root, index_path)
        (docs / "risk.md").write_text("改了" + chr(10), encoding="utf-8")
        retriever = build_retriever(index_path=index_path, root=root, freshness="off")
        self.assertIsNotNone(retriever)


    def test_warn_mode_marks_degraded(self) -> None:
        root = _tmp_dir("warn")
        docs = _make_corpus(root)
        index_path = root / "index.json"
        _build_index(root, index_path)
        (docs / "risk.md").write_text("又改了" + chr(10), encoding="utf-8")
        retriever = build_retriever(index_path=index_path, root=root, freshness="warn")
        outcome = retriever.search("风控", top_k=3)
        self.assertTrue(outcome.degraded_reason, "warn 档必须留下过期理由")


class GraphCitationTests(unittest.TestCase):
    def test_answer_uses_real_retrieved_citations(self) -> None:
        root = _tmp_dir("graph")
        _make_corpus(root)
        index_path = root / "index.json"
        _build_index(root, index_path)
        retriever = build_retriever(index_path=index_path, root=root, freshness="require")
        deps = GraphDeps(provider=FakeProvider(), tools=FakeTools("market_snapshot"), retriever=retriever, clock=fixed_clock)
        app = build_graph(deps)
        out, interrupted = run_graph(app, make_state(user_query="风控红线里仓位上限是多少"), thread_id="t-cite")
        self.assertFalse(interrupted)
        errors = " ".join(out.get("errors") or [])
        self.assertNotIn("RAG_UNAVAILABLE", errors)
        self.assertNotIn("CITATION_INVALID", errors)
        citations = list(out.get("citations") or [])
        self.assertTrue(citations, "主图必须真的用上检索结果")
        answer = str(out.get("final_answer") or "")
        self.assertIn("引用", answer)
        self.assertIn(citations[0]["source_path"], answer)


if __name__ == "__main__":
    unittest.main()
