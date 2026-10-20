"""切块（A3-01）。

切块最重要的一件事不是"切得好看"，是**每一块都能指回原文的行号**：
没有行号映射，引用就无法核对，RAG 就变成"看起来有出处"。

所以这里不做花哨的语义切分，只按标题层级切，并保证：
  1. 每块带 source_path / heading / start_line / end_line / content_hash / doc_type / index_version；
  2. 把块拼回去能还原原文（行范围连续、无重叠、无缝）；
  3. 超长段落按行继续切，切点只落在行边界上（不会把一行劈成两半）。
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pq_agent.rag.corpus import document_content_hash

if TYPE_CHECKING:  # pragma: no cover
    from pq_agent.rag.corpus import CorpusDocument

__all__ = [
    "CHUNK_SCHEMA_VERSION",
    "Chunk",
    "DEFAULT_MAX_CHARS",
    "chunk_document",
    "chunk_documents",
    "normalize_newlines",
]

CHUNK_SCHEMA_VERSION = "pq_agent.rag_chunk/1"
DEFAULT_MAX_CHARS = 1200

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


@dataclass(frozen=True)
class Chunk:
    """可检索单元。start_line / end_line 是 1-based 闭区间。"""

    chunk_id: str
    source_path: str
    doc_type: str
    heading: str
    heading_level: int
    start_line: int
    end_line: int
    content_hash: str
    text: str
    index_version: str = CHUNK_SCHEMA_VERSION

    def to_dict(self, *, with_text: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "chunk_id": self.chunk_id,
            "source_path": self.source_path,
            "doc_type": self.doc_type,
            "heading": self.heading,
            "heading_level": self.heading_level,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "content_hash": self.content_hash,
            "index_version": self.index_version,
        }
        if with_text:
            payload["text"] = self.text
        return payload

    @property
    def citation(self) -> str:
        """给人看的引用串：路径 + 行范围 + 标题。"""
        label = f"{self.source_path}:{self.start_line}-{self.end_line}"
        return f"{label} ({self.heading})" if self.heading else label


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _chunk_id(source_path: str, start_line: int, end_line: int, text: str) -> str:
    raw = "|".join([source_path, str(start_line), str(end_line), _digest(text)])
    return "ck_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _heading_spans(lines: Sequence[str]) -> list[tuple[int, int, str, int]]:
    """把行切成标题段。返回 (start_line, end_line, heading, level)，1-based 闭区间。"""
    spans: list[tuple[int, int, str, int]] = []
    current_start = 1
    current_heading = ""
    current_level = 0
    for index, raw in enumerate(lines, start=1):
        match = _HEADING_RE.match(raw.strip())
        if match and index > 1:
            spans.append((current_start, index - 1, current_heading, current_level))
            current_start = index
            current_heading = match.group(2).strip()
            current_level = len(match.group(1))
        elif match and index == 1:
            current_heading = match.group(2).strip()
            current_level = len(match.group(1))
    spans.append((current_start, len(lines), current_heading, current_level))
    return spans


def _split_span(start_line: int, end_line: int, lines: Sequence[str], *, max_chars: int) -> list[tuple[int, int]]:
    """把一个标题段按行装进 max_chars。切点只落在行边界上。"""
    pieces: list[tuple[int, int]] = []
    piece_start = start_line
    size = 0
    for line_no in range(start_line, end_line + 1):
        text = lines[line_no - 1]
        length = len(text) + 1
        if size and size + length > max_chars:
            pieces.append((piece_start, line_no - 1))
            piece_start = line_no
            size = 0
        size += length
    pieces.append((piece_start, end_line))
    return pieces


def chunk_document(document: "CorpusDocument", *, max_chars: int = DEFAULT_MAX_CHARS) -> list[Chunk]:
    """把一份文档切成可检索块。

    契约：返回块的 [start_line, end_line] 必须覆盖全文且不重叠，
    把每块的 text 按行号顺序拼起来等于原文（用 test 断言，不靠嘴说）。
    """
    if max_chars <= 0:
        raise ValueError("max_chars 必须为正数")
    text = normalize_newlines(document.text)
    lines = text.split("\n")
    chunks: list[Chunk] = []
    for start_line, end_line, heading, level in _heading_spans(lines):
        for piece_start, piece_end in _split_span(start_line, end_line, lines, max_chars=max_chars):
            body = "\n".join(lines[piece_start - 1:piece_end])
            if not body.strip():
                continue
            chunks.append(
                Chunk(
                    chunk_id=_chunk_id(document.relative_path, piece_start, piece_end, body),
                    source_path=document.relative_path,
                    doc_type=document.doc_type,
                    heading=heading,
                    heading_level=level,
                    start_line=piece_start,
                    end_line=piece_end,
                    content_hash=document_content_hash(body),
                    text=body,
                )
            )
    return chunks


def chunk_documents(documents: Sequence["CorpusDocument"], *, max_chars: int = DEFAULT_MAX_CHARS) -> list[Chunk]:
    """整库切块。按 relative_path 排序，保证同一语料得到同一顺序。"""
    result: list[Chunk] = []
    for document in sorted(documents, key=lambda item: item.relative_path):
        result.extend(chunk_document(document, max_chars=max_chars))
    return result
