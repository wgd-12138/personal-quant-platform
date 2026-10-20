"""重建 RAG 索引并同步它自己的 freshness 指纹（A6-02）。

为什么必须成对执行：索引换了，指纹必须跟着换。少写一次指纹，加载侧就会
（正确地）把索引判为过期。所以重建入口只留这一个，不给人只重建一半的机会。

用法：
    uv run --no-sync --extra agent python agent_eval/rebuild_rag_index.py
    uv run --no-sync --extra agent python agent_eval/rebuild_rag_index.py --check
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pq_agent.rag.build_index import build_index, index_status, write_index  # noqa: E402
from pq_agent.rag.corpus import collect_documents  # noqa: E402
from pq_agent.rag.freshness import (  # noqa: E402
    ERROR_INDEX_STALE,
    FreshnessError,
    assert_index_fresh,
    write_snapshot,
)

INDEX_PATH = "agent_eval/results/rag_index.json"
MANIFEST_PATH = "agent_eval/cases/rag_manifest.json"


def check() -> int:
    """只体检不重建：源文档和索引对不上就退出码 1。"""
    try:
        report = assert_index_fresh(ROOT / INDEX_PATH, root=ROOT)
    except FreshnessError as exc:
        print("STALE", exc.code)
        print(str(exc))
        return 1
    print("FRESH checked=%d built_at=%s" % (int(report["checked"]), report["built_at"]))
    return 0


def rebuild() -> int:
    manifest = collect_documents(root=ROOT)
    payload = manifest.to_dict()
    payload["generated_by"] = "agent_eval/rebuild_rag_index.py"
    (ROOT / MANIFEST_PATH).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8"
    )
    result = build_index(root=ROOT, profile="lexical_only")
    write_index(result, path=INDEX_PATH, root=ROOT)
    snapshot_path = write_snapshot(ROOT / INDEX_PATH, root=ROOT)
    status = index_status(path=INDEX_PATH, root=ROOT)
    # 重建完必须立刻自检：写指纹这一步漏了就会让所有加载点一起失败。
    report = assert_index_fresh(ROOT / INDEX_PATH, root=ROOT)
    print("documents=%d denied=%d chunks=%d" % (len(payload["documents"]), len(payload["denied"]), result.chunk_count))
    print("index_status=%s" % status["status"])
    print("index_hash=%s" % (status.get("index_hash") or result.manifest.get("index_hash", "")))
    print("corpus_hash=%s" % (status.get("corpus_hash") or result.manifest.get("corpus_hash", "")))
    print("snapshot=%s checked=%d" % (snapshot_path.name, int(report["checked"])))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rebuild_rag_index", description="重建 RAG 索引并同步 freshness 指纹")
    parser.add_argument("--check", action="store_true", help="只校验索引是否过期，不重建")
    args = parser.parse_args(argv)
    if args.check:
        return check()
    return rebuild()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except FreshnessError as exc:
        print("FAILED %s" % exc.code)
        print(str(exc))
        raise SystemExit(2)
