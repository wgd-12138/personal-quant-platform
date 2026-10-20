"""A5-03 演示入口：一条命令跑完 9 步，把文字记录落在演示目录里。

命令行（供 docs/agent/DEMO_RUNBOOK.md 引用）：
    uv run --no-sync --extra agent python -m pq_agent demo
    uv run --no-sync --extra agent python -m pq_agent demo --status
    uv run --no-sync --extra agent python -m pq_agent demo --step doctor --step role_denied
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, Sequence

from pq_agent.demo.harness import DemoHarness, DemoRun, demo_status, render_run
from pq_agent.demo.paths import ResetReport, demo_root, repo_root_of, reset_demo
from pq_agent.demo.steps import DEMO_STEPS, STEP_IDS

TRANSCRIPT_NAME = "transcript.txt"
RUN_JSON_NAME = "demo_run.json"


def write_artifact(harness: DemoHarness, name: str, text: str) -> pathlib.Path:
    """演示产物只写演示根目录内，绝不落到 data/reports/logs。"""

    harness.root.mkdir(parents=True, exist_ok=True)
    path = harness.root / name
    path.write_text(text, encoding="utf-8")
    return path


def reset_only(*, repo_root: pathlib.Path | None = None) -> ResetReport:
    return reset_demo(repo_root=repo_root)


def status_text(*, repo_root: pathlib.Path | None = None) -> str:
    facts = demo_status(repo_root=repo_root)
    lines = [
        "演示根目录：" + str(facts["demo_root"]),
        "已存在：" + str(bool(facts["demo_root_exists"])).lower(),
        "在 .tmp 内：" + str(bool(facts["inside_tmp"])).lower(),
        "RAG 索引：" + str(facts["rag_index"]) + "（存在：" + str(bool(facts["rag_index_exists"])).lower() + "）",
        "沙箱样例：" + str(facts["sandbox_corpus"]) + "（存在：" + str(bool(facts["sandbox_corpus_exists"])).lower() + "）",
        "步骤：" + ", ".join(facts["steps"]),
        "讲解预算：" + str(facts["narration_seconds"]) + " 秒",
    ]
    return chr(10).join(lines)


def run_demo(
    *,
    repo_root: pathlib.Path | None = None,
    step_ids: Sequence[str] | None = None,
    quiet: bool = False,
) -> int:
    harness = DemoHarness(repo_root=repo_root, reset=True)
    result: DemoRun = harness.run(step_ids)
    text = render_run(result)
    transcript = write_artifact(harness, TRANSCRIPT_NAME, text)
    write_artifact(
        harness,
        RUN_JSON_NAME,
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True),
    )
    if not quiet:
        print(text)
        print("")
        print("文字记录：" + str(transcript))
    return 0 if result.ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m pq_agent demo")
    parser.add_argument("--status", action="store_true", help="只报告演示环境，不执行")
    parser.add_argument("--reset", action="store_true", help="只清理演示目录，不执行")
    parser.add_argument("--step", action="append", default=[], help="只跑指定步骤，可重复")
    parser.add_argument("--json", action="store_true", help="以 JSON 打印运行摘要")
    parser.add_argument("--quiet", action="store_true", help="不打印逐步记录")
    parser.add_argument("--root", default="", help="工程根，默认自动推断")
    args = parser.parse_args(list(argv) if argv is not None else None)
    root = pathlib.Path(args.root) if args.root else None

    if args.status:
        print(status_text(repo_root=root))
        return 0
    if args.reset:
        report = reset_only(repo_root=root)
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if report.ok else 1
    steps = [str(item) for item in args.step]
    unknown = [item for item in steps if item not in STEP_IDS]
    if unknown:
        print("未知步骤：" + ", ".join(unknown))
        print("可用步骤：" + ", ".join(STEP_IDS))
        return 2
    if args.json:
        harness = DemoHarness(repo_root=root, reset=True)
        result = harness.run(steps)
        write_artifact(harness, TRANSCRIPT_NAME, render_run(result))
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result.ok else 1
    return run_demo(repo_root=root, step_ids=steps, quiet=bool(args.quiet))


__all__ = [
    "RUN_JSON_NAME",
    "TRANSCRIPT_NAME",
    "main",
    "reset_only",
    "run_demo",
    "status_text",
    "write_artifact",
]
