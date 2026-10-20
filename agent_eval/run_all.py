"""A5-02 四层评测的总入口：L0 单测 / L1 通道 / L2 golden task / L3 APM。

三条不许违反的规矩：
1. 缺条件就是缺条件。没有 Docker、没有 SkyWalking、没有模型凭据时，对应层写 UNKNOWN 与 blocker，
   不用 mock 数字冒充「跑过了」。
2. 每一层都要留下输入指纹（数据集、语料、索引、工具 schema），否则「同样的配置重跑」只是口号。
3. L2 的通过与否只由 judge.py 的确定性断言决定；LLM judge 只在报告里做辅证。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import subprocess
import sys
import uuid
from typing import Any, Mapping, Sequence

ROOT = pathlib.Path(__file__).resolve().parents[1]

EVAL_SCHEMA_VERSION = "pq_agent.eval_all/1"
RESULTS_DIR = ROOT / "agent_eval" / "results"
CASES_DIR = ROOT / "agent_eval" / "cases"
GOLDEN_PATH = CASES_DIR / "golden_tasks.json"
BASELINE_PATH = CASES_DIR / "eval_baseline.json"
APM_SNAPSHOT_PATH = RESULTS_DIR / "apm_environment.json"
SHIM_PATH = ROOT / "agent_eval" / "run_unittest_shim.py"

LAYERS: tuple[str, ...] = ("l0", "l1", "l2", "l3")

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_UNKNOWN = "unknown"
STATUS_UNAVAILABLE = "unavailable"

#: 各层门槛的来源必须写清楚，否则「通过」只是一个没有出处的形容词。
THRESHOLD_SOURCES: Mapping[str, str] = {
    "l0_no_new_failures": "agent_eval/cases/eval_baseline.json 中记录的全量测试基线",
    "l1_intent": "agent_eval/run_intent_eval.py 内 DEV_THRESHOLDS / TEST_THRESHOLDS，来源是 A1-02 的验收合同",
    "l1_rag": "agent_eval/run_rag_eval.py 的词法档门槛（hit@5 >= 0.60、mrr@5 >= 0.45、引用有效率 = 1.0），来源是 A3-02 的验收合同",
    "l1_sandbox": "agent_eval/cases/sandbox_redteam.json 的静态层期望（8 条静态拦截、2 条交给运行时兜），来源是 A2-02 的验收合同",
    "l2_golden": "judge.py 的 14 条确定性断言，全部由本任务定义并在 tests/agent/eval 里逐条打假",
    "l3_apm": "A4-01/A4-02 的 APM 合同：容器启动不等于通过，必须有入库与查询证据",
}


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha256(path: pathlib.Path) -> str:
    if not path.exists():
        return ""
    return _sha256_bytes(path.read_bytes())


def _load_json(path: pathlib.Path, *, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _run(argv: Sequence[str], *, timeout: float = 1800.0) -> dict[str, Any]:
    """跑一个子进程并把结果原样留下。不做「失败就吞掉」的包装。"""
    started = _now()
    try:
        proc = subprocess.run(
            list(argv),
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return {
            "argv": [str(item) for item in argv],
            "exit_code": int(proc.returncode),
            "stdout_tail": (proc.stdout or "")[-4000:],
            "stderr_tail": (proc.stderr or "")[-2000:],
            "started_at": started,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired:
        return {
            "argv": [str(item) for item in argv],
            "exit_code": None,
            "stdout_tail": "",
            "stderr_tail": "timeout after %ss" % timeout,
            "started_at": started,
            "timed_out": True,
        }


def _tail_lines(text: str, *, count: int = 6) -> list[str]:
    lines = [line for line in (text or "").splitlines() if line.strip()]
    return lines[-count:]


def _exit_ok(exit_code: Any) -> bool:
    """子进程退出码是否代表成功。

    退出码 0 是合法成功值，但它在 Python 里是假值：任何 `exit_code or 默认值` 的写法
    都会把成功判成失败（0 or 1 -> 1）。这里只对 None 判失败，其余按整数比 0。
    """
    if exit_code is None:
        return False
    try:
        return int(exit_code) == 0
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# L0：全量 unittest（对比 A0-00 基线）
# ---------------------------------------------------------------------------


def load_baseline(*, path: pathlib.Path | None = None) -> dict[str, Any]:
    target = path or BASELINE_PATH
    payload = _load_json(target)
    if not isinstance(payload, Mapping):
        return {
            "schema_version": "pq_agent.eval_baseline/1",
            "l0": {"runner": "shim", "tests": 0, "failures": 0, "errors": 0, "skipped": 0},
            "note": "基线文件缺失，比较结果不可用（不允许假装通过）",
            "available": False,
        }
    data = dict(payload)
    data["available"] = True
    return data


def parse_unittest_summary(text: str) -> dict[str, Any]:
    """从 unittest 输出里解析测试数、失败数、错误数与跳过数。"""
    import re

    ran = re.search(r"^Ran (\d+) tests?", text or "", re.MULTILINE)
    failed = re.search(r"FAILED \((.*?)\)", text or "", re.DOTALL)
    passed = re.search(r"^OK(?: \((.*?)\))?$", text or "", re.MULTILINE)
    counters = ""
    if failed:
        counters = failed.group(1)
    elif passed and passed.group(1):
        counters = passed.group(1)
    result: dict[str, Any] = {
        "tests": int(ran.group(1)) if ran else 0,
        "failures": 0,
        "errors": 0,
        "skipped": 0,
        "ok": bool(passed),
    }
    for key in ("failures", "errors", "skipped"):
        found = re.search(key + r"=(\d+)", counters)
        if found:
            result[key] = int(found.group(1))
    return result


def layer_l0(*, baseline: Mapping[str, Any] | None = None, runner: str = "shim") -> dict[str, Any]:
    """L0：本机沙箱把 0o700 目录变成自己进不去的目录，所以走垫片跑全量；""" """原始命令的状态另外记录，不混进结论。"""
    base = dict(baseline) if baseline is not None else load_baseline()
    expected = dict((base.get("l0") or {}))
    python = sys.executable
    shim = SHIM_PATH
    if runner == "shim" and shim.exists():
        result = _run([python, str(shim)])
    else:
        result = _run([python, "-m", "unittest", "discover", "-s", "tests"])
    summary = parse_unittest_summary(result.get("stdout_tail", "") + chr(10) + result.get("stderr_tail", ""))
    expected_tests = int(expected.get("tests") or 0)
    new_failures = int(summary["failures"]) + int(summary["errors"]) - int(expected.get("failures", 0) or 0) - int(expected.get("errors", 0) or 0)
    tests_delta = int(summary["tests"]) - expected_tests if expected_tests else 0
    ok = bool(summary["ok"]) and new_failures <= 0 and not result.get("timed_out")
    status = STATUS_OK if ok else STATUS_FAILED
    blockers: list[str] = []
    if not base.get("available"):
        blockers.append("基线文件缺失：" + str(BASELINE_PATH.relative_to(ROOT)).replace(chr(92), "/"))
        status = STATUS_UNKNOWN
    if new_failures > 0:
        blockers.append("相对基线出现 %d 个新失败" % new_failures)
    if not result.get("exit_code") == 0 and ok:
        status = STATUS_FAILED
    return {
        "layer": "l0",
        "status": status,
        "runner": runner if (runner == "shim" and shim.exists()) else "discover",
        "tests": summary["tests"],
        "failures": summary["failures"],
        "errors": summary["errors"],
        "skipped": summary["skipped"],
        "baseline_tests": expected_tests,
        "tests_delta": tests_delta,
        "new_failures": new_failures,
        "ok": ok,
        "exit_code": result.get("exit_code"),
        "command": [str(item) for item in result.get("argv") or []],
        "tail": _tail_lines(result.get("stdout_tail", ""), count=4),
        "stderr_tail": result.get("stderr_tail", "")[-600:],
        "blockers": blockers,
        "threshold_source": THRESHOLD_SOURCES["l0_no_new_failures"],
    }


# ---------------------------------------------------------------------------
# L1：通道级评测 + 输入指纹
# ---------------------------------------------------------------------------


def _intent_dataset_hashes() -> dict[str, str]:
    hashes: dict[str, str] = {}
    for split in ("train", "dev", "test"):
        hashes[split] = _file_sha256(CASES_DIR / ("intent_%s.jsonl" % split))
    return hashes


def _tool_schema_hash() -> tuple[str, int]:
    """工具 schema 指纹：用不含外部依赖的假 provider 装一份满工具注册表。"""
    sys.path.insert(0, str(ROOT / "src"))
    from pq_agent.tools.quant_tools import build_default_registry

    class _StubProvider:
        def snapshot(self, as_of):
            raise RuntimeError("schema 指纹不需要真数据")

    def _stub_sender(*args, **kwargs):
        return "stub"

    registry = build_default_registry(
        provider=_StubProvider(),
        reports_dir=ROOT / ".tmp" / "a5-02-schema" / "reports",
        runs_dir=ROOT / ".tmp" / "a5-02-schema" / "runs",
        positions=(),
        news_items=(),
        sender=_stub_sender,
    )
    schemas = registry.schemas()
    body = json.dumps(schemas, ensure_ascii=False, sort_keys=True)
    return _sha256_bytes(body.encode("utf-8")), len(schemas)


def _sandbox_layer() -> dict[str, Any]:
    """沙箱这一层只做静态层统计与语料指纹：运行时结论由 Windows 那台机器上的单测负责。"""
    corpus_path = CASES_DIR / "sandbox_redteam.json"
    corpus = _load_json(corpus_path, default={}) or {}
    cases = list((corpus.get("cases") or []))
    sys.path.insert(0, str(ROOT / "src"))
    from pq_agent.sandbox.static_check import analyze

    malicious = [item for item in cases if item.get("kind") == "malicious"]
    benign = [item for item in cases if item.get("kind") == "benign"]
    static_blocked = 0
    runtime_expected = 0
    for item in malicious:
        report = analyze(str(item.get("code") or ""))
        if report.blocked:
            static_blocked += 1
        else:
            runtime_expected += 1
    benign_clear = 0
    for item in benign:
        report = analyze(str(item.get("code") or ""))
        if report.clean:
            benign_clear += 1
    listed = int((corpus.get("counts") or {}).get("malicious") or 0)
    ok = (
        bool(malicious)
        and static_blocked == int((corpus.get("counts") or {}).get("static_expected") or 0)
        and runtime_expected == int((corpus.get("counts") or {}).get("runtime_expected") or 0)
        and benign_clear == len(benign)
        and len(malicious) == listed
    )
    return {
        "layer": "l1-sandbox",
        "status": STATUS_OK if ok else STATUS_FAILED,
        "ok": ok,
        "malicious_cases": len(malicious),
        "static_blocked": static_blocked,
        "runtime_expected": runtime_expected,
        "benign_cases": len(benign),
        "benign_clear": benign_clear,
        "corpus_sha256": _file_sha256(corpus_path),
        "runtime_note": "运行时层结论只可能来自真的能起进程/容器的主机：本机没有 Docker，Windows 上的 LocalRuntime 是约束层不是隔离",
        "threshold_source": THRESHOLD_SOURCES["l1_sandbox"],
        "blockers": [] if ok else ["静态层统计与语料声明不一致"],
    }


def layer_l1(*, with_rag: bool = True) -> dict[str, Any]:
    python = sys.executable
    intent = _run([python, str(ROOT / "agent_eval" / "run_intent_eval.py"), "--splits", "dev,test", "--l2-mode", "off"])
    intent_payload = _load_json(RESULTS_DIR / "intent_eval.json", default={}) or {}
    rag_result: dict[str, Any] = {"status": STATUS_UNKNOWN, "reason": "未运行"}
    if with_rag:
        rag_run = _run([python, str(ROOT / "agent_eval" / "run_rag_eval.py"), "--write"])
        rag_payload = _load_json(RESULTS_DIR / "rag_baseline.json", default={}) or {}
        # 基线文件的结构是 results.<mode>.metrics，不是顶层 metrics：
        # 顶层只有 entries 的元信息（指纹、profile、阈值），指标挂在每个通道下面。
        modes = dict(rag_payload.get("results") or {})
        bm25 = dict((modes.get("bm25") or {}).get("metrics") or {})
        thresholds = dict(rag_payload.get("thresholds") or {})
        lexical_gate = dict(thresholds.get("lexical") or {})
        checks = {
            "hit@5": float(bm25.get("hit@5") or 0.0) >= float(lexical_gate.get("hit@5") or 0.0),
            "mrr@5": float(bm25.get("mrr@5") or 0.0) >= float(lexical_gate.get("mrr@5") or 0.0),
            "citation_valid_rate": float(bm25.get("citation_valid_rate") or 0.0) >= float(lexical_gate.get("citation_valid_rate") or 0.0),
            "citation_failures": int(bm25.get("citation_failures") or 0) == 0,
        }
        # 退出码 0 是假值，不能写 `rag_run.get("exit_code") or 1`：0 or 1 得到 1，
        # 会把一次成功的 RAG 评测判成「门槛未通过」，而且报错信息为空（失败不在 checks 里）。
        exit_code = rag_run.get("exit_code")
        exit_ok = _exit_ok(exit_code)
        ok = bool(bm25) and all(checks.values()) and exit_ok
        rag_result = {
            "status": STATUS_OK if ok else STATUS_FAILED,
            "ok": ok,
            "exit_code": rag_run.get("exit_code"),
            "bm25_metrics": bm25,
            "modes": {name: dict((item or {}).get("metrics") or {}) for name, item in modes.items()},
            "gate_checks": checks,
            "thresholds": thresholds,
            "backend_note": rag_payload.get("backend_note", ""),
            "counts": dict(rag_payload.get("counts") or {}),
            "index_hash": rag_payload.get("index_hash", ""),
            "corpus_hash": rag_payload.get("corpus_hash", ""),
            "only_lexical_has_a_gate": True,
            "threshold_source": THRESHOLD_SOURCES["l1_rag"],
            "blockers": [] if ok else [
                "RAG 词法档门槛未通过或未产出：门槛失败项="
                + (", ".join(sorted(name for name, passed in checks.items() if not passed)) or "无")
                + "，exit_code=%r" % (exit_code,)
                + ("" if bm25 else "，没有 bm25 指标")
            ],
        }
    else:
        rag_result = {"status": STATUS_UNKNOWN, "ok": False, "reason": "--with-rag 未开启", "blockers": ["RAG 通道未运行（用 --with-rag 打开）"]}
    sandbox = _sandbox_layer()
    schema_hash, schema_count = _tool_schema_hash()
    # gates 的结构是 {split: [{metric, threshold, value, passed}, ...]}，不是一个扁平的布尔表。
    gates_raw = dict(intent_payload.get("gates") or {})
    gates: dict[str, list[dict[str, Any]]] = {
        str(split): [dict(item) for item in (rows or [])] for split, rows in gates_raw.items()
    }
    gate_failures = [
        "%s.%s" % (split, item.get("metric"))
        for split, rows in gates.items()
        for item in rows
        if not item.get("passed")
    ]
    splits = dict(intent_payload.get("splits") or {})
    # 退出码必须进判定：子进程挂了但磁盘上留着上一次的成功结果时，只读结果文件会给出假的绿色。
    intent_exit_ok = _exit_ok(intent.get("exit_code"))
    intent_ok = intent_exit_ok and bool(intent_payload) and not gate_failures and all(
        (item.get("pipeline_accuracy") or 0) > 0 for item in splits.values() if isinstance(item, Mapping)
    ) and bool(splits)
    intent_layer = {
        "layer": "l1-intent",
        "status": STATUS_OK if intent_ok else STATUS_FAILED,
        "ok": intent_ok,
        "exit_code": intent.get("exit_code"),
        "splits": dict(intent_payload.get("splits") or {}),
        "gates": gates,
        "gate_failures": gate_failures,
        "l2_status": intent_payload.get("l2_status", STATUS_UNKNOWN),
        "dataset_sha256": _intent_dataset_hashes(),
        "threshold_source": THRESHOLD_SOURCES["l1_intent"],
        "blockers": [] if intent_ok else ["意图通道门槛未通过或没有产出：exit_code=%r，门槛失败=" % (intent.get("exit_code"),) + (", ".join(gate_failures) or "无")],
    }
    blockers: list[str] = []
    for item in (intent_layer, rag_result, sandbox):
        blockers.extend(list(item.get("blockers") or []))
    ok = bool(intent_layer["ok"]) and bool(sandbox["ok"]) and bool(rag_result.get("ok"))
    return {
        "layer": "l1",
        "status": STATUS_OK if ok else STATUS_FAILED,
        "ok": ok,
        "intent": intent_layer,
        "rag": rag_result,
        "sandbox": sandbox,
        "tool_schema": {"sha256": schema_hash, "tool_count": schema_count},
        "blockers": blockers,
    }


# ---------------------------------------------------------------------------
# L2：golden task（确定性判题）
# ---------------------------------------------------------------------------


def _fixed_clock():
    fixed = dt.datetime(2026, 7, 9, 16, 30, tzinfo=dt.timezone(dt.timedelta(hours=8)))

    def clock() -> dt.datetime:
        return fixed

    return clock


def _workspace(case_id: str) -> pathlib.Path:
    """本机沙箱把 0o700 目录变成自己进不去的目录，所以不用 tempfile。"""
    base = ROOT / ".tmp" / "a5-02-eval" / (case_id + "-" + uuid.uuid4().hex[:8])
    base.mkdir(parents=True, exist_ok=True)
    return base


def run_golden_case(case: Any, suite: Mapping[str, Any]) -> dict[str, Any]:
    """跑一条 golden task，返回构造 Observation 需要的原始事实。"""
    sys.path.insert(0, str(ROOT / "src"))
    from pq_agent.approval import ApprovalStore
    from pq_agent.checkpoint import guarded_sqlite_checkpointer
    from pq_agent.data.provider import create_provider
    from pq_agent.graph import GraphDeps, build_graph, pending_interrupt, resume_graph, run_graph
    from pq_agent.outbox import OutboxStore
    from pq_agent.state import new_state
    from pq_agent.tools.quant_tools import build_default_registry

    clock = _fixed_clock()
    work = _workspace(case.case_id)
    reports_dir = work / "reports"
    runs_dir = work / "runs"
    reports_dir.mkdir(parents=True, exist_ok=True)
    runs_dir.mkdir(parents=True, exist_ok=True)

    sent: list[tuple] = []

    def _sender(*args, **kwargs):
        sent.append((args, kwargs))
        return {"ok": True}

    fixture_root = ROOT / "tests" / "fixtures"
    manifest = ROOT / str(suite.get("fixture_manifest") or "tests/fixtures/manifest.json")
    provider = create_provider("demo_offline", manifest_path=manifest, fixtures_root=fixture_root, clock=clock)
    allowed = [dt.date.fromisoformat(str(suite.get("as_of") or "2026-07-09"))]
    positions = [
        type("Row", (), {"code": item.get("code", ""), "name": item.get("name", "")})()
        for item in (suite.get("fixture_positions") or [])
    ]
    registry = build_default_registry(
        provider=provider,
        reports_dir=reports_dir,
        runs_dir=runs_dir,
        positions=positions,
        news_items=(),
        sender=_sender,
        allowed_dates=allowed,
    )
    deps = GraphDeps(
        provider=provider,
        tools=registry,
        clock=clock,
        approval_store=ApprovalStore(str(work / "approval.jsonl"), clock=clock, ttl_s=900),
        outbox=OutboxStore(str(work / "outbox.jsonl"), clock=clock),
    )
    state = new_state(
        thread_id="t-" + case.case_id,
        mode=str(suite.get("mode") or "replay"),
        as_of=str(suite.get("as_of") or "2026-07-09"),
        user_query=case.query,
        slots=dict(case.slots),
    )
    db = str(work / "cp.sqlite")
    out: dict[str, Any] = {}
    interrupted = False
    with guarded_sqlite_checkpointer(db) as saver:
        app = build_graph(deps, checkpointer=saver)
        out, interrupted = run_graph(app, state, thread_id="t-" + case.case_id)
        if interrupted and case.approval in ("approve", "reject"):
            pending = pending_interrupt(app, thread_id="t-" + case.case_id)
            value = dict((pending or {}).get("value") or {})
            decision = {
                "approved": case.approval == "approve",
                "actor": "eval-harness",
                "reason": "评测：%s" % case.approval,
                "arguments_hash": value.get("arguments_hash"),
                "action_hash": value.get("action_hash"),
            }
            out, interrupted = resume_graph(app, decision, thread_id="t-" + case.case_id)

    written: list[str] = []
    for item in sorted(work.rglob("*")):
        if item.is_file():
            written.append(str(item.relative_to(work)).replace(chr(92), "/"))
    snapshot_id = str(out.get("data_snapshot_id") or "")
    network_reads: list[str] = []
    if not snapshot_id.startswith("demo_offline"):
        for item in registry.specs():
            if getattr(item, "reads_network", False) and item.name in {
                str(step.get("tool_name")) for step in (out.get("tool_results") or [])
            }:
                network_reads.append(item.name)
    return {
        "case_id": case.case_id,
        "raw": out,
        "interrupted": bool(interrupted),
        "written_files": written,
        "sender_calls": len(sent),
        "network_reads": network_reads,
        "workspace": str(work.relative_to(ROOT)).replace(chr(92), "/"),
    }


def _observation_from(result: Mapping[str, Any]) -> Any:
    sys.path.insert(0, str(ROOT / "agent_eval"))
    from judge import Observation

    raw = dict(result.get("raw") or {})
    return Observation(
        case_id=str(result.get("case_id")),
        intent=str(raw.get("intent") or ""),
        intent_source=str(raw.get("intent_source") or ""),
        planned=tuple(item for item in (raw.get("plan") or []) if isinstance(item, Mapping)),
        tools=tuple(item for item in (raw.get("tool_results") or []) if isinstance(item, Mapping)),
        approvals=tuple(item for item in (raw.get("approvals") or []) if isinstance(item, Mapping)),
        errors=tuple(str(item) for item in (raw.get("errors") or [])),
        written_files=tuple(str(item) for item in (result.get("written_files") or [])),
        citations=tuple(item for item in (raw.get("citations") or []) if isinstance(item, Mapping)),
        answer=str(raw.get("final_answer") or ""),
        interrupted=bool(result.get("interrupted")),
        network_reads=tuple(str(item) for item in (result.get("network_reads") or [])),
        provider_calls=1 if raw.get("data_snapshot_id") else 0,
        cost_status=str(result.get("cost_status") or "unknown"),
    )


def layer_l2(*, suite: Mapping[str, Any] | None = None, cases: Sequence[str] = ()) -> dict[str, Any]:
    sys.path.insert(0, str(ROOT / "agent_eval"))
    from judge import LLM_JUDGE_NOTE, describe_checks, judge_suite, load_suite

    loaded = dict(suite) if suite is not None else load_suite(GOLDEN_PATH, root=ROOT)
    selected = [item for item in (loaded.get("cases") or []) if not cases or item.case_id in set(cases)]
    results: list[dict[str, Any]] = []
    failures: list[str] = []
    for case in selected:
        try:
            result = run_golden_case(case, loaded)
        except Exception as exc:  # 运行期异常必须记录下来，不能当成「跳过」
            failures.append(case.case_id)
            results.append({"case_id": case.case_id, "error": type(exc).__name__ + ": " + str(exc)[:300]})
            continue
        results.append(result)
    observations = []
    for item in results:
        if "error" in item:
            continue
        observations.append(_observation_from(item))
    summary = judge_suite(loaded, observations) if observations else {
        "ok": False,
        "blockers": ["没有任何用例产生观测"],
        "verdicts": [],
        "case_count": len(selected),
    }
    if failures:
        summary.setdefault("blockers", []).append("运行期异常的用例：" + ", ".join(failures))
        summary["ok"] = False
    ok = bool(summary.get("ok"))
    return {
        "layer": "l2",
        "status": STATUS_OK if ok else STATUS_FAILED,
        "ok": ok,
        "judge": summary,
        "checks_documented": describe_checks(),
        "llm_judge_note": LLM_JUDGE_NOTE,
        "case_count": len(selected),
        "ran": len(results),
        "run_errors": failures,
        "blockers": list(summary.get("blockers") or []),
        "threshold_source": THRESHOLD_SOURCES["l2_golden"],
    }


# ---------------------------------------------------------------------------
# L3：APM 快照
# ---------------------------------------------------------------------------


def layer_l3(*, root: pathlib.Path | None = None) -> dict[str, Any]:
    base = root or ROOT
    sys.path.insert(0, str(base / "src"))
    from pq_agent.observability.apm_health import apm_health

    snapshot = _load_json(base / "agent_eval" / "results" / "apm_environment.json", default={}) or {}
    health = apm_health(root=base, docker_available=False)
    health_payload = health.to_dict() if hasattr(health, "to_dict") else dict(health)
    metrics = dict(snapshot.get("metrics") or {})
    points = list(metrics.get("points") or [])
    usage = [item for item in points if item.get("name") == "pq_agent.usage.total"]
    cost_status = "unknown"
    for item in usage:
        labels = dict(item.get("labels") or {})
        cost_status = str(labels.get("usage_status") or "unknown")
    alerts = dict(snapshot.get("alerts") or {})
    reasons = list(health_payload.get("reasons") or [])
    ok = bool(health_payload.get("ok"))
    blockers = list(reasons)
    if not snapshot:
        blockers.append("没有 APM 快照：" + str(APM_SNAPSHOT_PATH.relative_to(ROOT)).replace(chr(92), "/"))
    status = STATUS_OK if ok else STATUS_UNKNOWN
    return {
        "layer": "l3",
        "status": status,
        "ok": ok,
        "collector": "docker_compose" if ok else STATUS_UNAVAILABLE + "_on_this_host",
        "health": health_payload,
        "snapshot": {
            "exists": bool(snapshot),
            "run_id": snapshot.get("run_id", ""),
            "trace_id": snapshot.get("trace_id", ""),
            "otel_trace_id": snapshot.get("otel_trace_id", ""),
            "point_count": len(points),
            "firing_alerts": list(alerts.get("firing") or []),
        },
        "cost": {"status": cost_status, "note": "没有 usage 数据就是 unknown，禁止按 token 估算成本"},
        "latency_ms": {
            "turn_duration": [item.get("value") for item in points if item.get("name") == "pq_agent.turn.duration_ms"],
            "node_duration": [item.get("value") for item in points if item.get("name") == "pq_agent.node.duration_ms"],
        },
        "errors": [item.get("labels", {}).get("error_code") for item in points if item.get("name") == "pq_agent.error.total"],
        "dropped_telemetry": [item.get("value") for item in points if item.get("name") == "pq_agent.telemetry.dropped.total"],
        "blockers": blockers,
        "threshold_source": THRESHOLD_SOURCES["l3_apm"],
    }


def run_all(
    *,
    layers: Sequence[str] = LAYERS,
    with_rag: bool = True,
    cases: Sequence[str] = (),
    root: pathlib.Path | None = None,
) -> dict[str, Any]:
    base = root or ROOT
    selected = [item for item in layers if item in LAYERS]
    payload: dict[str, Any] = {
        "schema_version": EVAL_SCHEMA_VERSION,
        "generated_at": _now(),
        "layers_run": selected,
        "threshold_sources": dict(THRESHOLD_SOURCES),
        "environment": {
            "python": sys.version.split()[0],
            "executable": sys.executable,
            "platform": sys.platform,
        },
    }
    if "l0" in selected:
        payload["l0"] = layer_l0()
    if "l1" in selected:
        payload["l1"] = layer_l1(with_rag=with_rag)
    if "l2" in selected:
        payload["l2"] = layer_l2(cases=cases)
    if "l3" in selected:
        payload["l3"] = layer_l3(root=base)
    blockers: list[str] = []
    for name in selected:
        item = payload.get(name) or {}
        blockers.extend(list(item.get("blockers") or []))
    payload["blockers"] = blockers
    # l3 在缺 Docker/Collector 的主机上只能是 UNKNOWN，它不参与「通过与否」；
    # 但它必须出现在 blockers 里，否则「环境不满足」会被静默吞掉。
    hard_layers = [item for item in selected if item != "l3"]
    payload["ok"] = all(bool((payload.get(name) or {}).get("ok")) for name in hard_layers)
    payload["suite_digest"] = _suite_digest(payload)
    return payload


def _suite_digest(payload: Mapping[str, Any]) -> str:
    body = json.dumps(
        {key: payload.get(key) for key in ("layers_run", "threshold_sources", "environment")},
        ensure_ascii=False,
        sort_keys=True,
    )
    return _sha256_bytes(body.encode("utf-8"))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="A5-02 四层评测")
    parser.add_argument("--layers", default=",".join(LAYERS), help="逗号分隔：l0,l1,l2,l3")
    parser.add_argument("--without-rag", action="store_true", help="跳过 L1 的 RAG 通道（默认运行）")
    parser.add_argument("--cases", default="", help="只跑指定 case_id，逗号分隔")
    parser.add_argument("--write", action="store_true", help="落盘 eval_all.json 与报告")
    parser.add_argument("--json", action="store_true", help="只打印 JSON 摘要")
    args = parser.parse_args(argv)
    layers = [item.strip() for item in str(args.layers).split(",") if item.strip()]
    cases = [item.strip() for item in str(args.cases).split(",") if item.strip()]
    payload = run_all(layers=layers, with_rag=not args.without_rag, cases=cases)
    if args.write:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        (RESULTS_DIR / "eval_all.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + chr(10),
            encoding="utf-8",
            newline=chr(10),
        )
        sys.path.insert(0, str(ROOT / "agent_eval"))
        from report import write_reports

        written = write_reports(payload, root=ROOT)
        print("written", ", ".join(written))
    summary = {
        "ok": payload.get("ok"),
        "layers": {name: (payload.get(name) or {}).get("status") for name in payload.get("layers_run") or []},
        "blockers": payload.get("blockers"),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
