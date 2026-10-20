"""索引 freshness：源文档变了，旧索引就不能再当真相（A6-02）。

为什么单独成模块：索引是"某一刻的语料快照"。如果源文档在索引之后被改过，
检索仍然会返回旧正文和旧行号，引用看起来完全合法——这是最危险的一种错。
所以这里把"源文件指纹"落盘成索引的伴随文件，加载索引时逐文件比对。

刻意不做的事：不做后台定时刷新（那会让"过期"变成静默的），
不在比对失败时自动重建（重建必须由人显式执行，且要先看到差异清单）。
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

FRESHNESS_SCHEMA_VERSION = "pq_agent.rag_freshness/1"
FRESHNESS_SUFFIX = ".freshness.json"

ERROR_FRESHNESS_MISSING = "RAG_INDEX_FRESHNESS_MISSING"
ERROR_INDEX_STALE = "RAG_INDEX_STALE"
ERROR_SOURCE_UNREADABLE = "RAG_SOURCE_UNREADABLE"

REBUILD_COMMAND = "uv run --no-sync --extra agent python agent_eval/rebuild_rag_index.py"


class FreshnessError(Exception):
    """freshness 失败一律带错误码、差异清单和重建命令，方便直接照着修。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        detail: str = "",
        changes: Mapping[str, list[str]] | None = None,
        rebuild_command: str = REBUILD_COMMAND,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail
        self.changes = {key: list(value) for key, value in (changes or {}).items()}
        self.rebuild_command = rebuild_command

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": False,
            "error_code": self.code,
            "message": self.message,
            "detail": self.detail,
            "changes": self.changes,
            "rebuild_command": self.rebuild_command,
        }

    def __str__(self) -> str:
        parts = [self.message]
        if self.detail:
            parts.append(self.detail)
        for key in ("changed", "missing", "added", "unreadable"):
            values = self.changes.get(key) or []
            if values:
                parts.append("%s=%s" % (key, ",".join(values[:10])))
        parts.append("重建命令：" + self.rebuild_command)
        return "；".join(parts)


@dataclass(frozen=True)
class SourceFingerprint:
    """一个源文档在索引时刻的指纹。"""

    path: str
    sha256: str
    size: int

    def to_dict(self) -> dict[str, object]:
        return {"sha256": self.sha256, "size": int(self.size)}


def fingerprint_file(path: Path) -> tuple[str, int]:
    """按字节算 sha256。读不出来直接抛，不用"读不到就当没变"糊过去。"""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise FreshnessError(
            ERROR_SOURCE_UNREADABLE,
            "源文档读不出来：" + str(path),
            detail=str(exc),
        ) from exc
    return hashlib.sha256(data).hexdigest(), len(data)


def freshness_path(index_path: str | Path) -> Path:
    """索引的伴随指纹文件：<索引文件名>.freshness.json。"""
    target = Path(index_path)
    return target.with_name(target.name + FRESHNESS_SUFFIX)


def index_source_paths(index_payload: Mapping[str, object]) -> list[str]:
    """索引里出现过的源文档（去重且排序，保证指纹文件可复现）。"""
    chunks = index_payload.get("chunks") or []
    if not isinstance(chunks, Iterable):
        raise FreshnessError(ERROR_FRESHNESS_MISSING, "索引 chunks 不是列表，无法计算 freshness")
    paths = set()
    for chunk in chunks:
        if isinstance(chunk, Mapping):
            value = str(chunk.get("source_path") or "")
            if value:
                paths.add(value)
    return sorted(paths)


def build_snapshot(root: str | Path, source_paths: Iterable[str]) -> dict[str, object]:
    """对给定源文档清单计算指纹快照。"""
    base = Path(root)
    sources: dict[str, object] = {}
    for rel in sorted(set(str(item) for item in source_paths)):
        digest, size = fingerprint_file(base / rel)
        sources[rel] = SourceFingerprint(path=rel, sha256=digest, size=size).to_dict()
    return {
        "schema_version": FRESHNESS_SCHEMA_VERSION,
        "built_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_count": len(sources),
        "sources": sources,
    }


def write_snapshot(index_path: str | Path, *, root: str | Path) -> Path:
    """按索引当前内容写指纹文件（原子替换）。索引本身不改。"""
    target = Path(index_path)
    if not target.is_file():
        raise FreshnessError(ERROR_FRESHNESS_MISSING, "索引文件不存在，无法写 freshness：" + str(target))
    payload = json.loads(target.read_text(encoding="utf-8"))
    snapshot = build_snapshot(root, index_source_paths(payload))
    out = freshness_path(target)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")
    os.replace(tmp, out)
    return out


def load_snapshot(index_path: str | Path) -> dict[str, object]:
    """读指纹文件。缺失时明确报错，不假装"没有指纹就是没问题"。"""
    path = freshness_path(index_path)
    if not path.is_file():
        raise FreshnessError(
            ERROR_FRESHNESS_MISSING,
            "索引缺少 freshness 指纹文件：" + str(path),
            detail="没有指纹就无法判断源文档是否变过，拒绝把旧索引当真相。",
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FreshnessError(ERROR_FRESHNESS_MISSING, "freshness 指纹读不出来：" + str(path), detail=str(exc)) from exc
    if not isinstance(payload, dict):
        raise FreshnessError(ERROR_FRESHNESS_MISSING, "freshness 指纹顶层不是对象：" + str(path))
    return dict(payload)


def compare_snapshot(root: str | Path, snapshot: Mapping[str, object]) -> dict[str, list[str]]:
    """逐文件比对，返回 changed / missing / added / unreadable 四类差异。"""
    base = Path(root)
    recorded = snapshot.get("sources") or {}
    if not isinstance(recorded, Mapping):
        raise FreshnessError(ERROR_FRESHNESS_MISSING, "freshness 指纹缺少 sources 段")
    changed: list[str] = []
    missing: list[str] = []
    unreadable: list[str] = []
    for rel, item in sorted(recorded.items()):
        expected = ""
        if isinstance(item, Mapping):
            expected = str(item.get("sha256") or "")
        path = base / str(rel)
        if not path.is_file():
            missing.append(str(rel))
            continue
        try:
            digest, _size = fingerprint_file(path)
        except FreshnessError:
            unreadable.append(str(rel))
            continue
        if expected and digest != expected:
            changed.append(str(rel))
    return {"changed": changed, "missing": missing, "added": [], "unreadable": unreadable}


def assert_index_fresh(index_path: str | Path, *, root: str | Path) -> dict[str, object]:
    """加载索引前的闸门：任何差异都抛 RAG_INDEX_STALE，并给出重建命令。"""
    snapshot = load_snapshot(index_path)
    changes = compare_snapshot(root, snapshot)
    problems = {key: value for key, value in changes.items() if value}
    if problems:
        raise FreshnessError(
            ERROR_INDEX_STALE,
            "索引已过期：源文档在索引之后发生了变化",
            detail="指纹文件：" + str(freshness_path(index_path)),
            changes=changes,
        )
    return {
        "ok": True,
        "schema_version": FRESHNESS_SCHEMA_VERSION,
        "checked": len(snapshot.get("sources") or {}),
        "snapshot_path": str(freshness_path(index_path)),
        "built_at": str(snapshot.get("built_at") or ""),
    }
