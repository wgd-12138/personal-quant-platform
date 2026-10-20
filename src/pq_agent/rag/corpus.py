"""RAG 语料治理（A3-01）。

只做一件事：把「哪些文件可以进入检索语料」这件事变成**代码里的白名单 + 路径校验**，
而不是文档里的一句话。

三条纪律：
  1. 白名单按**目录前缀 + 扩展名**双重限制；不在白名单里的一律不索引，不是「警告一下」。
  2. 每个候选路径都过 pq_agent.security.paths：..、绝对路径、UNC、设备路径、链接组件、
     secret 级别（config/local.yaml、.env）全部当场拒绝。
  3. 拒绝是要留痕的：拒了哪些、为什么拒，必须进 manifest，可审计。
；
刻意不做的事：不索引 data/（真实持仓）、reports/（含真实数字的报告）、
logs/、planning/runs/（控制面运行记录）。这些即使「看起来像文档」也不进语料。
"""
from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pq_agent.security.paths import (
    DEFAULT_ROOT,
    PathPolicyError,
    classify_path,
    read_within_root,
    resolve_within_root,
)
from pq_agent.security.redaction import collect_secrets_from_environ, find_secrets

__all__ = [
    "CORPUS_ERROR_INCLUDE_DENIED",
    "CORPUS_ERROR_NOT_FOUND",
    "CORPUS_ERROR_READ_FAILED",
    "CORPUS_ERROR_SECRET_SHAPED",
    "CORPUS_ERROR_TOO_LARGE",
    "CORPUS_SCHEMA_VERSION",
    "DEFAULT_INCLUDE_PREFIXES",
    "DEFAULT_MAX_FILE_BYTES",
    "DENIED_PREFIXES",
    "DeniedDocument",
    "ALLOWED_SUFFIXES",
    "CorpusDocument",
    "CorpusError",
    "CorpusManifest",
    "collect_documents",
    "document_content_hash",
    "manifest_hash",
    "rejected_reason",
]

CORPUS_SCHEMA_VERSION = "pq_agent.rag_corpus/1"

# 允许进语料的目录前缀（相对项目根，POSIX 形式）。
# 这是白名单：不在表里的目录，即使扩展名对，也不进语料。
DEFAULT_INCLUDE_PREFIXES: tuple[str, ...] = (
    "docs",
    "planning/agent",
)

# 明确拒绝的目录前缀。写在这里不是为了"过滤"，是为了留下"我考虑过这一项"的证据，
# 也让测试可以断言这些目录确实被拒。
DENIED_PREFIXES: tuple[str, ...] = (
    "data",
    "reports",
    "logs",
    "planning/runs",
    "config",
    ".git",
    ".tmp",
    "agent_eval/results",
    # A6-02 加入：evidence 下是验收脚本输出与测试日志，不是知识文档。
    # 两个理由：① 实测它占了全库 84% 的块（3640/4341），把词法打分拉偏；
    # ② 它每个任务都会新增，留在语料里会让索引必然过期，freshness 就永远报警。
    "planning/agent/evidence",
)

ALLOWED_SUFFIXES: tuple[str, ...] = (".md", ".txt", ".yaml", ".yml", ".json")

DEFAULT_MAX_FILE_BYTES = 512_000

CORPUS_ERROR_INCLUDE_DENIED = "CORPUS_INCLUDE_DENIED"
CORPUS_ERROR_TOO_LARGE = "CORPUS_FILE_TOO_LARGE"
CORPUS_ERROR_READ_FAILED = "CORPUS_READ_FAILED"
CORPUS_ERROR_NOT_FOUND = "CORPUS_NOT_FOUND"
CORPUS_ERROR_SECRET_SHAPED = "CORPUS_SECRET_SHAPED"


class CorpusError(ValueError):
    """语料治理错误。code 是稳定标识，message 只给人看。"""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class DeniedDocument:
    """一条被拒的候选及其理由。拒绝也要进 manifest，否则等于没有治理。"""

    relative_path: str
    code: str
    detail: str

    def to_dict(self) -> dict[str, str]:
        return {"relative_path": self.relative_path, "code": self.code, "detail": self.detail}


@dataclass(frozen=True)
class CorpusDocument:
    """一份通过治理的语料文档。文本在内存里，落盘时按需重取。"""

    relative_path: str
    doc_type: str
    suffix: str
    size_bytes: int
    content_hash: str
    mtime_ns: int
    text: str = field(repr=False, default="")

    def to_dict(self) -> dict[str, object]:
        return {
            "relative_path": self.relative_path,
            "doc_type": self.doc_type,
            "suffix": self.suffix,
            "size_bytes": self.size_bytes,
            "content_hash": self.content_hash,
            "mtime_ns": self.mtime_ns,
        }


@dataclass(frozen=True)
class CorpusManifest:
    """语料清单：收录了什么、拒了什么、整体指纹是多少。"""

    schema_version: str
    root: str
    include_prefixes: tuple[str, ...]
    denied_prefixes: tuple[str, ...]
    allowed_suffixes: tuple[str, ...]
    documents: tuple[CorpusDocument, ...]
    denied: tuple[DeniedDocument, ...]
    corpus_hash: str

    def to_dict(self, *, with_text: bool = False) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "root": self.root,
            "include_prefixes": list(self.include_prefixes),
            "denied_prefixes": list(self.denied_prefixes),
            "allowed_suffixes": list(self.allowed_suffixes),
            "corpus_hash": self.corpus_hash,
            "document_count": len(self.documents),
            "denied_count": len(self.denied),
            "documents": [d.to_dict() for d in self.documents],
            "denied": [d.to_dict() for d in self.denied],
        }
        if with_text:
            payload["texts"] = {d.relative_path: d.text for d in self.documents}
        return payload


def _posix(relative: str) -> str:
    return relative.replace("\\", "/").strip("/")


def _under_prefix(posix: str, prefix: str) -> bool:
    prefix = _posix(prefix).lower()
    lowered = posix.lower()
    return lowered == prefix or lowered.startswith(prefix + "/")


def rejected_reason(
    relative_path: str,
    *,
    include_prefixes: Sequence[str] = DEFAULT_INCLUDE_PREFIXES,
    denied_prefixes: Sequence[str] = DENIED_PREFIXES,
    allowed_suffixes: Sequence[str] = ALLOWED_SUFFIXES,
) -> str | None:
    """返回拒绝码；None 表示可以通过。先看拒绝表，再看白名单。"""
    posix = _posix(relative_path)
    if not posix:
        return CORPUS_ERROR_NOT_FOUND
    for prefix in denied_prefixes:
        if _under_prefix(posix, prefix):
            return CORPUS_ERROR_INCLUDE_DENIED
    if not any(_under_prefix(posix, prefix) for prefix in include_prefixes):
        return CORPUS_ERROR_INCLUDE_DENIED
    suffix = Path(posix).suffix.lower()
    if suffix not in tuple(s.lower() for s in allowed_suffixes):
        return CORPUS_ERROR_INCLUDE_DENIED
    return None


def document_content_hash(text: str) -> str:
    """内容指纹：先归一化换行，避免 CRLF/LF 差异让同一个文件算出两个 hash。"""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def manifest_hash(documents: Iterable[CorpusDocument]) -> str:
    """语料整体指纹：按路径排序后串 hash，与遍历顺序无关。"""
    lines = [f"{d.relative_path}\t{d.content_hash}" for d in sorted(documents, key=lambda d: d.relative_path)]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _iter_candidates(
    root: Path,
    include_prefixes: Sequence[str],
) -> list[str]:
    """按白名单前缀遍历候选文件，返回相对 POSIX 路径（已排序，保证可复现）。"""
    found: list[str] = []
    for prefix in include_prefixes:
        base = root / Path(_posix(prefix))
        if not base.is_dir():
            continue
        for current, dirnames, filenames in os.walk(base):
            dirnames.sort()
            for name in sorted(filenames):
                absolute = Path(current) / name
                try:
                    relative = absolute.relative_to(root).as_posix()
                except ValueError:
                    continue
                found.append(relative)
    return sorted(set(found))


def collect_documents(
    *,
    root: str | os.PathLike[str] | None = None,
    include_prefixes: Sequence[str] = DEFAULT_INCLUDE_PREFIXES,
    denied_prefixes: Sequence[str] = DENIED_PREFIXES,
    allowed_suffixes: Sequence[str] = ALLOWED_SUFFIXES,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    extra_candidates: Sequence[str] = (),
    secrets: Mapping[str, str] | None = None,
    scan_secrets: bool = True,
) -> CorpusManifest:
    """采集语料并产出 manifest。

    extra_candidates 用于把白名单之外的单份文件送进来做判断（测试与显式放行用）；
    它同样要过前缀白名单，不是后门。
    """
    root_path = Path(root).resolve() if root is not None else DEFAULT_ROOT
    if not root_path.is_dir():
        raise CorpusError(CORPUS_ERROR_NOT_FOUND, f"项目根不存在：{root_path}")

    # 两种入参都收：调用方既可以给 {名字: 值} 字典，也可以给一串值（环境变量扫描返回的就是元组）。
    if secrets is None:
        known_secrets = collect_secrets_from_environ() if scan_secrets else ()
    elif isinstance(secrets, Mapping):
        known_secrets = list(secrets.values())
    else:
        known_secrets = list(secrets)

    candidates = _iter_candidates(root_path, include_prefixes)
    for extra in extra_candidates:
        posix = _posix(str(extra))
        if posix and posix not in candidates:
            candidates.append(posix)
    candidates.sort()

    documents: list[CorpusDocument] = []
    denied: list[DeniedDocument] = []

    for relative in candidates:
        posix = _posix(relative)
        reason = rejected_reason(
            posix,
            include_prefixes=include_prefixes,
            denied_prefixes=denied_prefixes,
            allowed_suffixes=allowed_suffixes,
        )
        if reason is not None:
            denied.append(DeniedDocument(posix, reason, "不在语料白名单内"))
            continue

        try:
            resolved, payload = read_within_root(posix, root=root_path, max_bytes=max_file_bytes)
        except PathPolicyError as exc:
            code = CORPUS_ERROR_INCLUDE_DENIED
            if "PATH_TOO_LARGE" in str(exc):
                code = CORPUS_ERROR_TOO_LARGE
            elif "PATH_NOT_FOUND" in str(exc):
                code = CORPUS_ERROR_NOT_FOUND
            denied.append(DeniedDocument(posix, code, str(exc)))
            continue
        except OSError as exc:
            denied.append(DeniedDocument(posix, CORPUS_ERROR_READ_FAILED, type(exc).__name__))
            continue

        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            denied.append(DeniedDocument(posix, CORPUS_ERROR_READ_FAILED, "不是 UTF-8 文本"))
            continue

        # 语料是"要被检索出来喂给模型"的东西，所以这里必须比普通读取更严：
        # 文档正文里出现已登记密钥值时，整份文档拒收，而不是脱敏后继续——
        # 一份含密钥的文档说明它本来就不该在语料目录里。
        if known_secrets:
            hits = find_secrets(text, secrets=known_secrets)
            if hits:
                denied.append(
                    DeniedDocument(posix, CORPUS_ERROR_SECRET_SHAPED, f"正文命中 {len(hits)} 处已登记密钥")
                )
                continue

        stat = os.stat(resolved)
        documents.append(
            CorpusDocument(
                relative_path=posix,
                doc_type=classify_path(posix),
                suffix=Path(posix).suffix.lower(),
                size_bytes=len(payload),
                content_hash=document_content_hash(text),
                mtime_ns=int(stat.st_mtime_ns),
                text=text,
            )
        )

    return CorpusManifest(
        schema_version=CORPUS_SCHEMA_VERSION,
        root=str(root_path),
        include_prefixes=tuple(include_prefixes),
        denied_prefixes=tuple(denied_prefixes),
        allowed_suffixes=tuple(allowed_suffixes),
        documents=tuple(documents),
        denied=tuple(denied),
        corpus_hash=manifest_hash(documents),
    )
