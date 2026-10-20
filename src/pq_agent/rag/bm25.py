"""BM25 词法检索（A3-01）。

为什么先做 BM25 而不是直接上向量：面试目录里 rag_quant 的实测结论是
等权 RRF（58%）比单路 BM25（86%）差得多。融合只有在每一路都靠得住时才有意义。
所以这里先把词法基线做扎实，并且把它标成"基线"——不假装它是语义检索。
"""
from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from pq_agent.rag.chunk import Chunk

__all__ = [
    "BM25_INDEX_VERSION",
    "Bm25Index",
    "DEFAULT_K1",
    "DEFAULT_B",
    "ScoredChunk",
    "tokenize",
]

BM25_INDEX_VERSION = "pq_agent.rag_bm25/1"
DEFAULT_K1 = 1.5
DEFAULT_B = 0.75

_LATIN_RE = re.compile(r"[a-z0-9_]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def tokenize(text: str) -> list[str]:
    """中英混排分词：英文按词，中文按单字 + 相邻双字。

    中文按字切会让"走势"和"走"无法区分，所以额外补二元组；
    没有引入分词库，是为了不让核心包多一个第三方依赖。
    """
    lowered = text.lower()
    tokens = _LATIN_RE.findall(lowered)
    chars = _CJK_RE.findall(lowered)
    tokens.extend(chars)
    for index in range(len(chars) - 1):
        tokens.append(chars[index] + chars[index + 1])
    return tokens


@dataclass(frozen=True)
class ScoredChunk:
    """一条检索结果。score 是 BM25 原始分，不归一化也不同路可比。"""

    chunk_id: str
    score: float
    source_path: str
    heading: str
    start_line: int
    end_line: int
    text: str
    doc_type: str = ""
    index_version: str = BM25_INDEX_VERSION

    @property
    def citation(self) -> str:
        label = f"{self.source_path}:{self.start_line}-{self.end_line}"
        return f"{label} ({self.heading})" if self.heading else label

    def to_dict(self, *, with_text: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "chunk_id": self.chunk_id,
            "score": round(self.score, 6),
            "source_path": self.source_path,
            "heading": self.heading,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "doc_type": self.doc_type,
            "citation": self.citation,
            "index_version": self.index_version,
        }
        if with_text:
            payload["text"] = self.text
        return payload


@dataclass
class Bm25Index:
    """内存 BM25 索引。够用就好：个人项目语料只有几百到几千块。"""

    k1: float = DEFAULT_K1
    b: float = DEFAULT_B
    index_version: str = BM25_INDEX_VERSION
    _documents: list["Chunk"] = field(default_factory=list, repr=False)
    _tokens: list[list[str]] = field(default_factory=list, repr=False)
    _freqs: list[Counter] = field(default_factory=list, repr=False)
    _lengths: list[int] = field(default_factory=list, repr=False)
    _df: Counter = field(default_factory=Counter, repr=False)
    def __len__(self) -> int:
        return len(self._documents)

    @property
    def document_count(self) -> int:
        return len(self._documents)

    @property
    def average_length(self) -> float:
        if not self._lengths:
            return 0.0
        return sum(self._lengths) / len(self._lengths)

    def add(self, chunks: Sequence["Chunk"]) -> None:
        """追加块。重复 chunk_id 会被跳过，避免索引里出现两份同样的证据。"""
        known = {item.chunk_id for item in self._documents}
        for chunk in chunks:
            if chunk.chunk_id in known:
                continue
            known.add(chunk.chunk_id)
            tokens = tokenize(chunk.text)
            self._documents.append(chunk)
            self._tokens.append(tokens)
            self._freqs.append(Counter(tokens))
            self._lengths.append(len(tokens))
            for token in set(tokens):
                self._df[token] += 1

    def search(self, query: str, *, top_k: int = 5) -> list[ScoredChunk]:
        """返回 top_k 条。查询词全都不在语料里时返回空列表，不编造结果。"""
        if top_k <= 0:
            raise ValueError("top_k 必须为正数")
        query_tokens = tokenize(query)
        if not query_tokens or not self._documents:
            return []
        total = len(self._documents)
        average = self.average_length or 1.0
        scores = [0.0] * total
        for token in set(query_tokens):
            df = self._df.get(token, 0)
            if df == 0:
                continue
            idf = math.log(1.0 + (total - df + 0.5) / (df + 0.5))
            for index in range(total):
                freq = self._freqs[index].get(token, 0)
                if freq == 0:
                    continue
                length = self._lengths[index] or 1
                denominator = freq + self.k1 * (1.0 - self.b + self.b * length / average)
                scores[index] += idf * freq * (self.k1 + 1.0) / denominator
        ranked = sorted(
            range(total),
            key=lambda i: (-scores[i], self._documents[i].chunk_id),
        )
        results: list[ScoredChunk] = []
        for index in ranked:
            if scores[index] <= 0.0:
                continue
            document = self._documents[index]
            results.append(
                ScoredChunk(
                    chunk_id=document.chunk_id,
                    score=float(scores[index]),
                    source_path=document.source_path,
                    heading=document.heading,
                    start_line=document.start_line,
                    end_line=document.end_line,
                    text=document.text,
                    doc_type=document.doc_type,
                )
            )
            if len(results) >= top_k:
                break
        return results


def build_bm25(chunks: Iterable["Chunk"], *, k1: float = DEFAULT_K1, b: float = DEFAULT_B) -> Bm25Index:
    index = Bm25Index(k1=k1, b=b)
    index.add(list(chunks))
    return index
