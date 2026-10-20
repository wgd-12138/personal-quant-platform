"""索引构建与原子重建（A3-01）。

一个索引最容易骗人的地方是"看起来建好了"：
写到一半崩掉、旧块没删干净、manifest 里的 hash 和实际内容对不上。
所以这里的做法是：**先生成完整内容，再一次性落盘；落盘前先校验，校验不过就把旧索引原样留下。**
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from pq_agent.rag import corpus as corpus_module
from pq_agent.rag.bm25 import BM25_INDEX_VERSION, Bm25Index, build_bm25
from pq_agent.rag.chunk import CHUNK_SCHEMA_VERSION, Chunk, chunk_documents
from pq_agent.rag.embedder import (
    PROFILE_DENSE_REQUIRED,
    PROFILE_LEXICAL_ONLY,
    PROFILES,
    EmbeddingUnavailableError,
    resolve_profile,
    select_embedder,
)
from pq_agent.security.paths import resolve_within_root

if TYPE_CHECKING:  # pragma: no cover
    from pq_agent.rag.corpus import CorpusManifest

__all__ = [
    "INDEX_SCHEMA_VERSION", "INDEX_VERSION", "DEFAULT_INDEX_PATH",
    "IndexBuildResult", "IndexBuildError", "build_index", "load_index",
    "index_status", "write_index", "verify_chunk_hash", "index_fingerprint",
]

INDEX_SCHEMA_VERSION = "pq_agent.rag_index/1"
INDEX_VERSION = "pq_agent.rag_index/1"
DEFAULT_INDEX_PATH = "agent_eval/results/rag_index.json"
INDEX_ERROR_REBUILD_FAILED = "RAG_INDEX_REBUILD_FAILED"
INDEX_ERROR_CHUNK_HASH = "RAG_CHUNK_HASH_MISMATCH"
INDEX_ERROR_INDEX_MISSING = "RAG_INDEX_MISSING"


@dataclass
class IndexBuildError(RuntimeError):
    code: str = INDEX_ERROR_REBUILD_FAILED
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


@dataclass
class IndexBuildResult:
    """一次构建的全部可审计信息。degraded 不是失败，是"能跑但不完整"。"""

    chunk_count: int
    document_count: int
    corpus_hash: str
    index_hash: str
    profile: str
    degraded: bool
    degraded_reason: str = ""
    embedding_backend: str = ""
    embedding_dimension: int = 0
    manifest: Mapping[str, object] = field(default_factory=dict)
    def to_dict(self, *, with_manifest: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "chunk_count": self.chunk_count,
            "document_count": self.document_count,
            "corpus_hash": self.corpus_hash,
            "index_hash": self.index_hash,
            "profile": self.profile,
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
            "embedding_backend": self.embedding_backend,
            "embedding_dimension": self.embedding_dimension,
        }
        if with_manifest:
            payload["manifest"] = dict(self.manifest)
        return payload


def index_fingerprint(payload: Mapping[str, object]) -> str:
    """索引指纹：把 built_at 与 index_hash 自己都排除掉再算。

    不排除 built_at 会让"同样的语料两次构建得到不同指纹"，不排除 index_hash 会让
    自检永远失败——记下这个坑，因为两条都很隐蔽。
    """
    import hashlib

    core = {
        key: value
        for key, value in payload.items()
        if key not in ("built_at", "index_hash")
    }
    canonical = json.dumps(core, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def verify_chunk_hash(chunk: Chunk) -> bool:
    """块内容与它自报的 content_hash 是否一致。引用核对的最后一道闸。"""
    return corpus_module.document_content_hash(chunk.text) == chunk.content_hash


def build_index(
    *,
    root: Path | None = None,
    profile: str = PROFILE_LEXICAL_ONLY,
    manifest: "CorpusManifest | None" = None,
    chunks: Sequence[Chunk] | None = None,
    embedder=None,
    environ: Mapping[str, str] | None = None,
    max_chars: int | None = None,
) -> IndexBuildResult:
    """构建索引结果。**不落盘**——落盘是 write_index 的事，两者分开才好测。"""
    selected = resolve_profile(profile)
    base = Path(root) if root is not None else corpus_module.DEFAULT_ROOT
    corpus = manifest if manifest is not None else corpus_module.collect_documents(root=base)
    blocks = list(chunks) if chunks is not None else chunk_documents(corpus.documents)
    if selected == PROFILE_DENSE_REQUIRED:
        backend = select_embedder(profile=selected, environ=environ, embedder=embedder)
        if backend is None:
            raise EmbeddingUnavailableError()
        backend_name = str(getattr(backend, "name", "") or "")
        dimension = int(getattr(backend, "dimension", 0) or 0)
        degraded = False
        reason = ""
    else:
        backend_name = ""
        dimension = 0
        degraded = True
        # 这是唯一诚实的说法：能查，但只有词法一路，语义召回不在里面。
        reason = "lexical_only：只建了 BM25 词法索引，语义召回未启用（配置显式选择，非静默降级）"
    payload = {
        "schema_version": INDEX_SCHEMA_VERSION,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "profile": selected,
        "degraded": degraded,
        "degraded_reason": reason,
        "chunk_schema_version": CHUNK_SCHEMA_VERSION,
        "bm25_version": BM25_INDEX_VERSION,
        "corpus_hash": corpus.corpus_hash,
        "document_count": len(corpus.documents),
        "chunk_count": len(blocks),
        "embedding_backend": backend_name,
        "embedding_dimension": dimension,
        "chunks": [chunk.to_dict() for chunk in blocks],
    }
    digest = index_fingerprint(payload)
    payload["index_hash"] = digest
    return IndexBuildResult(
        chunk_count=len(blocks),
        document_count=len(corpus.documents),
        corpus_hash=corpus.corpus_hash,
        index_hash=digest,
        profile=selected,
        degraded=degraded,
        degraded_reason=reason,
        embedding_backend=backend_name,
        embedding_dimension=dimension,
        manifest=payload,
    )


def write_index(
    result: IndexBuildResult,
    *,
    path: str | Path = DEFAULT_INDEX_PATH,
    root: Path | None = None,
) -> Path:
    """原子落盘。先写临时文件再替换；替换前校验要写的字节能完整解析回来。

    校验失败就把临时文件删掉、保留旧索引——**绝不用半成品覆盖一个能用的版本**。
    """
    base = Path(root) if root is not None else corpus_module.DEFAULT_ROOT
    target = Path(path)
    if not target.is_absolute():
        # 相对路径必须以「当前工作目录」为基准，不能以 root 为基准再拼一遍（A6-02 实测缺陷）：
        # 传 ".tmp/a6-02/x/index.json" 且 root=".tmp/a6-02/x" 时会写成
        # ".tmp/a6-02/x/.tmp/a6-02/x/index.json"，索引文件凭空出现在别处。
        absolute = Path(os.path.abspath(os.fspath(path)))
        # 绝对化之后仍走安全路径解析：目标目录还不存在（must_exist=False），但要挡穿越与符号链接。
        resolved = Path(resolve_within_root(str(absolute), root=base, must_exist=False, allow_absolute=True))
    else:
        resolved = target.resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    tmp = resolved.with_name(resolved.name + ".tmp")
    body = json.dumps(result.manifest, ensure_ascii=False, indent=2, sort_keys=False)
    try:
        _ = json.loads(body)
        if index_fingerprint(json.loads(body)) != result.index_hash:
            raise IndexBuildError(INDEX_ERROR_REBUILD_FAILED, "写盘前自检失败：index_hash 与内容不一致")
        tmp.write_text(body, encoding="utf-8")
        reloaded = json.loads(tmp.read_text(encoding="utf-8"))
        if len(reloaded.get("chunks") or []) != result.chunk_count:
            raise IndexBuildError(INDEX_ERROR_REBUILD_FAILED, "写盘后回读的块数与构建结果不一致")
        os.replace(tmp, resolved)
    except Exception as exc:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        if isinstance(exc, IndexBuildError):
            raise
        raise IndexBuildError(INDEX_ERROR_REBUILD_FAILED, str(exc)) from exc
    return resolved


def load_index(*, path: str | Path = DEFAULT_INDEX_PATH, root: Path | None = None) -> dict[str, object]:
    """读回索引。文件不存在时明确报 RAG_INDEX_MISSING，不返回空索引。"""
    base = Path(root) if root is not None else corpus_module.DEFAULT_ROOT
    target = Path(path)
    if not target.is_absolute():
        resolved = Path(resolve_within_root(str(path), root=base, must_exist=False))
    else:
        resolved = target.resolve()
    if not resolved.exists():
        raise IndexBuildError(INDEX_ERROR_INDEX_MISSING, "索引文件不存在：" + str(path))
    return dict(json.loads(resolved.read_text(encoding="utf-8")))


def index_status(*, path: str | Path = DEFAULT_INDEX_PATH, root: Path | None = None) -> dict[str, object]:
    """只读状态：给 UI 和评测用。缺文件时不抛异常，如实说 missing。"""
    try:
        payload = load_index(path=path, root=root)
    except IndexBuildError as exc:
        return {"status": exc.code, "detail": exc.detail, "degraded": True, "profile": ""}
    profile = str(payload.get("profile") or "")
    return {
        "status": "ok",
        "profile": profile,
        "degraded": bool(payload.get("degraded")),
        "degraded_reason": str(payload.get("degraded_reason") or ""),
        "chunk_count": int(payload.get("chunk_count") or 0),
        "document_count": int(payload.get("document_count") or 0),
        "corpus_hash": str(payload.get("corpus_hash") or ""),
        "index_hash": str(payload.get("index_hash") or ""),
        "profiles_supported": list(PROFILES),
        "buildable": list(PROFILES),
    }
