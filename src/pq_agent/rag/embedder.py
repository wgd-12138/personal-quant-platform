"""向量通道与能力 profile（A3-01 / A3-02 边界）。

这里只回答一个问题：**这一路检索到底有没有得用、能不能信。**

面试目录 rag_quant 的实测结论是：本机 fastembed 下载中文权重失败（Errno 11001 DNS + Errno 13），
也没有任何可用的 embedding 凭据。所以默认只能是词法；
缺少向量能力时，唯一正确的行为是"明确说出来"（EMBEDDING_UNAVAILABLE），
而不是"悄悄用 BM25 顶上"——静默降级会让召回率悄悄变差，还会让简历里那句话站不住。
"""
from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover
    from pq_agent.rag.chunk import Chunk

__all__ = [
    "EmbeddingUnavailableError",
    "Embedder",
    "HermeticEmbedder",
    "NO_EMBEDDER_AVAILABLE",
    "PROFILE_DENSE_REQUIRED",
    "PROFILE_LEXICAL_ONLY",
    "PROFILES",
    "resolve_profile",
    "select_embedder",
]

EMBEDDING_ERROR_UNAVAILABLE = "EMBEDDING_UNAVAILABLE"
PROFILE_DENSE_REQUIRED = "dense_required"
PROFILE_LEXICAL_ONLY = "lexical_only"
PROFILES = (PROFILE_DENSE_REQUIRED, PROFILE_LEXICAL_ONLY)
NO_EMBEDDER_AVAILABLE = "当前没有可用的向量后端（未配置 embedding 凭据，也没有离线权重），词法通道可用"


@dataclass
class EmbeddingUnavailableError(RuntimeError):
    """向量能力不可用。**不**回落到词法：调用方必须自己决定是失败还是显式改 profile。"""

    detail: str = NO_EMBEDDER_AVAILABLE
    code: str = EMBEDDING_ERROR_UNAVAILABLE

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


def resolve_profile(value: str | None, *, default: str = PROFILE_LEXICAL_ONLY) -> str:
    """把配置值收敛到两个合法 profile。空值取 default，非法值直接报错（不猜）。"""
    raw = str(value or "").strip().lower() or default
    if raw not in PROFILES:
        raise ValueError("未知的检索 profile：" + raw + "；只支持 " + " / ".join(PROFILES))
    return raw


@runtime_checkable
class Embedder(Protocol):
    """向量后端。名字和维度都要能自报，方便写进索引 manifest。"""

    name: str
    dimension: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        ...

@dataclass
class HermeticEmbedder:
    """离线确定性后端：只用于测试融合与排序逻辑。

    它**不是**语义模型，把它当语义通道用就是自欺。
    真语义通道必须走中转站 /v1/embeddings 或本地 ONNX 权重（A3-02 落地）。
    """

    name: str = "hermetic"
    dimension: int = 64
    seed: int = 0

    @staticmethod
    def bucket(token: str, *, seed: int, dimension: int) -> int:
        """把词映射到桶号。**刻意不用内置 hash()**：

        Python 的 hash() 对字符串是按进程随机加盐的（PYTHONHASHSEED），
        用它算桶号会让同一段文本在不同进程里得到不同向量——那这个后端就不能叫「确定性」了，
        评测数字也会每次都不一样、无法复现。sha256 是跨进程跨机器稳定的。
        """
        import hashlib

        digest = hashlib.sha256(f"{int(seed)}:{token}".encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "big") % int(dimension)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        from pq_agent.rag.bm25 import tokenize

        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dimension
            for token in tokenize(text):
                vector[self.bucket(token, seed=self.seed, dimension=self.dimension)] += 1.0
            norm = sum(value * value for value in vector) ** 0.5
            vectors.append([value / norm for value in vector] if norm else vector)
        return vectors

def select_embedder(*, profile: str, environ: Mapping[str, str] | None = None, embedder: Embedder | None = None):
    """按 profile 选后端。dense_required 选不出来就抛，绝不返回 None 假装没事。"""
    if embedder is not None:
        return embedder
    source = environ if environ is not None else os.environ
    if str(source.get("PQ_AGENT_EMBEDDING_BASE_URL", "") or "").strip():
        # 中转站 /v1/embeddings 的实现在 A3-02 接；这里先如实说"还没接"。
        return None
    if profile == PROFILE_DENSE_REQUIRED:
        raise EmbeddingUnavailableError(NO_EMBEDDER_AVAILABLE)
    return None
