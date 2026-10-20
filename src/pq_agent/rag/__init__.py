"""RAG 语料治理、切块、词法检索与索引（A3-01 / A3-02）。

包内三块职责分开：corpus 决定"哪些文档能进来"，chunk 决定"怎么切且能指回行号"，
bm25 / embedder 决定"有哪几路可以查"，build_index 决定"怎么原子落盘且不骗人"。
"""
from __future__ import annotations

from pq_agent.rag.bm25 import (
    BM25_INDEX_VERSION, DEFAULT_B, DEFAULT_K1, Bm25Index, ScoredChunk, build_bm25, tokenize,
)
from pq_agent.rag.build_index import (
    DEFAULT_INDEX_PATH, INDEX_SCHEMA_VERSION, IndexBuildError, IndexBuildResult, build_index,
    index_fingerprint, index_status, load_index, verify_chunk_hash, write_index,
)
from pq_agent.rag.chunk import CHUNK_SCHEMA_VERSION, Chunk, chunk_document, chunk_documents
from pq_agent.rag.corpus import (
    ALLOWED_SUFFIXES, CORPUS_SCHEMA_VERSION, DEFAULT_INCLUDE_PREFIXES, DEFAULT_MAX_FILE_BYTES,
    DENIED_PREFIXES, CorpusDocument, CorpusError, CorpusManifest, DeniedDocument,
    collect_documents, document_content_hash, manifest_hash, rejected_reason,
)
from pq_agent.rag.embedder import (
    EMBEDDING_ERROR_UNAVAILABLE, NO_EMBEDDER_AVAILABLE, PROFILE_DENSE_REQUIRED,
    PROFILE_LEXICAL_ONLY, PROFILES, Embedder, EmbeddingUnavailableError, HermeticEmbedder,
    resolve_profile, select_embedder,
)

__all__ = [
    "ALLOWED_SUFFIXES", "BM25_INDEX_VERSION", "Bm25Index", "CHUNK_SCHEMA_VERSION", "CORPUS_SCHEMA_VERSION",
    "Chunk", "CorpusDocument", "CorpusError", "CorpusManifest", "DEFAULT_B", "DEFAULT_INDEX_PATH",
    "DEFAULT_INCLUDE_PREFIXES", "DEFAULT_K1", "DEFAULT_MAX_FILE_BYTES", "DENIED_PREFIXES",
    "DeniedDocument", "EMBEDDING_ERROR_UNAVAILABLE", "Embedder", "EmbeddingUnavailableError",
    "HermeticEmbedder", "INDEX_SCHEMA_VERSION", "IndexBuildError", "IndexBuildResult",
    "NO_EMBEDDER_AVAILABLE", "PROFILES", "PROFILE_DENSE_REQUIRED", "PROFILE_LEXICAL_ONLY",
    "ScoredChunk", "build_bm25", "build_index", "chunk_document", "chunk_documents",
    "collect_documents", "document_content_hash", "index_fingerprint", "index_status",
    "load_index", "manifest_hash", "rejected_reason", "resolve_profile", "select_embedder",
    "tokenize", "verify_chunk_hash", "write_index",
]
