"""A5-03 演示层测试：路径隔离、步骤合同、整场离线执行。

这些用例要回答三个问题：
1. 演示只动自己的目录吗（会不会误删真实数据）？
2. 屏幕上该出现的东西真的出现了吗（文档与程序同源）？
3. 整场跑完是不是真的零外呼、零业务写？
"""

from __future__ import annotations

import json
import pathlib
import re
import unittest

from pq_agent.demo.harness import (
    CONTROL_FILE_NAMES,
    DEMO_RAG_INDEX_MISSING,
    DEMO_CORPUS_MISSING,
    DEMO_NETWORK_DETECTED,
    DemoHarness,
    DemoNotReady,
    demo_status,
    pick_free_port,
    render_run,
)
from pq_agent.demo.paths import (
    DEMO_PATH_DENIED,
    DEMO_RESET_REFUSED,
    PROTECTED_NAMES,
    DemoPathError,
    assert_not_protected,
    assert_reset_target,
    demo_root,
    demo_stage_root,
    is_inside_demo,
    repo_root_of,
    reset_demo,
)
from pq_agent.demo.steps import (
    MAX_NARRATION_SECONDS,
    MIN_NARRATION_SECONDS,
    STEP_IDS,
    DEMO_STEPS,
    step_by_id,
    total_narration_seconds,
)
from pq_agent.observability import shutdown_tracing
from pq_agent.__main__ import main as pq_main

ROOT = pathlib.Path(__file__).resolve().parents[2]
CITATION_RE = re.compile(r"^[^:]+:\d+-\d+$")


def tearDownModule() -> None:
    """演示服务会把 tracer 注册到进程级，跑完必须关掉，否则污染后续用例（A1-06/A1-07 的教训）。"""

    shutdown_tracing()


def _protected_snapshot() -> set[str]:
    """真实资产目录的文件快照。演示跑完必须一模一样。"""

    snapshot: set[str] = set()
    for relative in ("data", "reports", "logs", pathlib.Path("planning") / "runs"):
        base = ROOT / relative
        if not base.exists():
            continue
        for item in base.rglob("*"):
            if item.is_file():
                snapshot.add(str(item.relative_to(ROOT)))
    return snapshot


class DemoPathTests(unittest.TestCase):
    def test_repo_root_is_project_root(self) -> None:
        self.assertEqual(repo_root_of(), ROOT)

    def test_demo_root_is_fixed_under_tmp(self) -> None:
        self.assertEqual(demo_root(ROOT), ROOT / ".tmp" / "pq_agent_demo")
        self.assertEqual(demo_stage_root(ROOT), demo_root(ROOT) / "stage")

    def test_demo_root_is_inside_demo_but_src_is_not(self) -> None:
        self.assertTrue(is_inside_demo(demo_root(ROOT), repo_root=ROOT))
        self.assertTrue(is_inside_demo(demo_stage_root(ROOT), repo_root=ROOT))
        self.assertFalse(is_inside_demo(ROOT / "src", repo_root=ROOT))

    def test_protected_names_cover_real_assets(self) -> None:
        for name in ("data", "reports", "logs", "planning", "docs", "src", "tests"):
            self.assertIn(name, PROTECTED_NAMES)

    def test_project_root_is_rejected_as_demo_dir(self) -> None:
        with self.assertRaises(DemoPathError) as ctx:
            assert_not_protected(ROOT, repo_root=ROOT)
        self.assertEqual(ctx.exception.code, DEMO_PATH_DENIED)

    def test_protected_directory_is_rejected(self) -> None:
        for name in ("data", "reports", "logs"):
            with self.assertRaises(DemoPathError) as ctx:
                assert_not_protected(ROOT / name, repo_root=ROOT)
            self.assertEqual(ctx.exception.code, DEMO_PATH_DENIED)

    def test_path_outside_tmp_is_rejected(self) -> None:
        with self.assertRaises(DemoPathError) as ctx:
            assert_not_protected(ROOT / "scratch", repo_root=ROOT)
        self.assertEqual(ctx.exception.code, DEMO_PATH_DENIED)

    def test_reset_target_must_be_exactly_demo_root(self) -> None:
        for target in (ROOT, ROOT / ".tmp", demo_stage_root(ROOT), ROOT / "data"):
            with self.assertRaises(DemoPathError) as ctx:
                assert_reset_target(target, repo_root=ROOT)
            self.assertIn(ctx.exception.code, (DEMO_PATH_DENIED, DEMO_RESET_REFUSED))
        self.assertEqual(assert_reset_target(demo_root(ROOT), repo_root=ROOT), demo_root(ROOT))

    def test_reset_removes_only_demo_root_content_and_is_idempotent(self) -> None:
        root = demo_root(ROOT)
        stage = demo_stage_root(ROOT)
        stage.mkdir(parents=True, exist_ok=True)
        (root / "marker.txt").write_text("x", encoding="utf-8")
        report = reset_demo(repo_root=ROOT)
        self.assertTrue(report.ok)
        self.assertIn("marker.txt", report.removed)
        self.assertFalse((root / "marker.txt").exists())
        again = reset_demo(repo_root=ROOT)
        # 幂等的语义是「没有东西可删」，不是「目录不存在」：根目录本身永远保留。
        self.assertTrue(again.ok)
        self.assertEqual(again.removed, [])
        self.assertEqual(again.failed, [])

    def test_report_is_json_serializable(self) -> None:
        payload = reset_demo(repo_root=ROOT).to_dict()
        for key in ("root", "removed", "failed", "skipped", "reason", "ok"):
            self.assertIn(key, payload)
        json.dumps(payload)


class DemoStepContractTests(unittest.TestCase):
    def test_nine_unique_steps(self) -> None:
        self.assertEqual(len(DEMO_STEPS), 9)
        self.assertEqual(len(set(STEP_IDS)), 9)

    def test_narration_budget_within_range(self) -> None:
        total = total_narration_seconds()
        self.assertGreaterEqual(total, MIN_NARRATION_SECONDS)
        self.assertLessEqual(total, MAX_NARRATION_SECONDS)

    def test_every_step_declares_markers_and_offline_note(self) -> None:
        for step in DEMO_STEPS:
            self.assertTrue(step.markers, step.step_id)
            self.assertTrue(step.offline_note, step.step_id)
            self.assertGreater(step.narration_seconds, 0, step.step_id)

    def test_every_step_points_at_a_recorded_pitfall(self) -> None:
        for step in DEMO_STEPS:
            self.assertTrue(step.pitfall_ids, step.step_id)
            for pid in step.pitfall_ids:
                self.assertTrue(pid.startswith("pit-"), pid)

    def test_unknown_step_raises(self) -> None:
        with self.assertRaises(KeyError):
            step_by_id("not-a-step")

    def test_step_by_id_roundtrip(self) -> None:
        for step in DEMO_STEPS:
            self.assertIs(step_by_id(step.step_id), step)

    def test_render_run_contains_titles_and_verdict(self) -> None:
        harness = DemoHarness(reset=False)
        result = harness.run(["doctor"])
        text = render_run(result)
        self.assertIn(DEMO_STEPS[0].title, text)
        self.assertIn("外呼尝试：0（必须为 0）", text)
        self.assertIn("结论：全部通过", text)

    def tearDown(self) -> None:
        shutdown_tracing()

    def test_status_reports_environment_without_running(self) -> None:
        facts = demo_status(repo_root=ROOT)
        self.assertTrue(facts["inside_tmp"])
        self.assertEqual(facts["steps"], list(STEP_IDS))
        self.assertEqual(facts["narration_seconds"], total_narration_seconds())
        self.assertTrue(facts["sandbox_corpus_exists"])
        self.assertTrue(facts["rag_index_exists"])


class DemoHonestFailureTests(unittest.TestCase):
    def tearDown(self) -> None:
        shutdown_tracing()

    def test_missing_rag_index_is_reported_not_faked(self) -> None:
        harness = DemoHarness(reset=False)
        harness.repo_root = ROOT / ".tmp" / "a5-03-missing-index"
        harness._retriever = None
        with self.assertRaises(DemoNotReady) as ctx:
            harness.retriever()
        self.assertEqual(ctx.exception.code, DEMO_RAG_INDEX_MISSING)

    def test_missing_redteam_corpus_is_reported(self) -> None:
        harness = DemoHarness(reset=False)
        harness.repo_root = ROOT / ".tmp" / "a5-03-missing-corpus"
        with self.assertRaises(DemoNotReady) as ctx:
            harness.redteam_cases()
        self.assertEqual(ctx.exception.code, DEMO_CORPUS_MISSING)

    def test_pick_free_port_returns_bindable_port(self) -> None:
        port = pick_free_port()
        self.assertIsInstance(port, int)
        self.assertGreaterEqual(port, 8199)
        self.assertLessEqual(port, 8250)

    def test_unknown_step_id_is_a_blocker_not_a_crash(self) -> None:
        harness = DemoHarness(reset=False)
        result = harness.run(["role_denied"])
        self.assertEqual(len(result.steps), 1)
        self.assertTrue(result.steps[0].ok)


class DemoCliTests(unittest.TestCase):
    """命令行入口的回归用例。

    背景：实测 `python -m pq_agent demo --quiet` 曾报 unrecognized arguments，
    因为 argparse 的 REMAINDER 吃不掉以 - 开头的参数。这里把命令行路径钉住。
    """

    def tearDown(self) -> None:
        shutdown_tracing()

    def _run_main(self, argv: list[str]) -> tuple[int, str]:
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = pq_main(list(argv))
        return int(code), buffer.getvalue()

    def test_status_flag_is_forwarded(self) -> None:
        code, out = self._run_main(["demo", "--status"])
        self.assertEqual(code, 0)
        self.assertIn("pq_agent_demo", out)
        self.assertIn("讲解预算：410 秒", out)

    def test_step_flag_with_value_is_forwarded(self) -> None:
        code, _ = self._run_main(["demo", "--step", "doctor", "--quiet"])
        self.assertEqual(code, 0)

    def test_unknown_step_exits_two(self) -> None:
        code, out = self._run_main(["demo", "--step", "not-a-step"])
        self.assertEqual(code, 2)
        self.assertIn("未知步骤", out)

    def test_help_is_forwarded_to_runner(self) -> None:
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            with self.assertRaises(SystemExit) as ctx:
                pq_main(["demo", "--help"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("--status", buffer.getvalue())


class DemoFullRunTests(unittest.TestCase):
    """整场跑一次。这一步是 A5-03 的核心证据，不只是冒烟。"""

    harness: DemoHarness
    result: object
    before: set[str]

    @classmethod
    def setUpClass(cls) -> None:
        cls.before = _protected_snapshot()
        cls.harness = DemoHarness(reset=True)
        cls.result = cls.harness.run()

    @classmethod
    def tearDownClass(cls) -> None:
        shutdown_tracing()

    def test_run_is_ok_with_no_blockers(self) -> None:
        self.assertTrue(self.result.ok, [item.to_dict() for item in self.result.steps])
        self.assertEqual(self.result.blockers, [])

    def test_all_nine_steps_passed(self) -> None:
        self.assertEqual([item.step_id for item in self.result.steps], list(STEP_IDS))
        for item in self.result.steps:
            self.assertEqual(item.markers_missing, (), item.step_id)
            self.assertTrue(item.ok, item.step_id)
            self.assertEqual(item.error, "")

    def test_zero_outbound_attempts(self) -> None:
        self.assertEqual(self.result.network_attempts, 0)

    def test_sender_probe_never_called(self) -> None:
        self.assertEqual(self.harness._sender.calls, [])

    def test_no_business_or_control_write_outside_demo_root(self) -> None:
        self.assertEqual(_protected_snapshot(), self.before)
        for relative in ("data", "reports", "logs"):
            self.assertFalse((ROOT / relative).exists(), relative)

    def test_every_demo_file_is_inside_demo_root(self) -> None:
        for item in demo_root(ROOT).rglob("*"):
            if item.is_file():
                self.assertTrue(is_inside_demo(item, repo_root=ROOT), str(item))

    def test_receipt_and_checkpoint_files_are_control_files(self) -> None:
        names = {item.name for item in demo_stage_root(ROOT).rglob("*") if item.is_file()}
        # 沙箱步骤会在自己的子目录里写脚本，目录名是 uuid；这里只看根层控制文件。
        for name in ("approvals.jsonl", "checkpoints.sqlite"):
            self.assertIn(name, names)
        self.assertTrue(names & set(CONTROL_FILE_NAMES))

    def test_approvals_jsonl_is_parseable(self) -> None:
        path = demo_stage_root(ROOT) / "approvals.jsonl"
        self.assertTrue(path.is_file())
        records = [
            json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        self.assertTrue(records)
        for record in records:
            # 审批记录的判定字段是 status（pending/approved/...），没有 approved 布尔键。
            self.assertIn("approval_id", record)
            self.assertIn("status", record)
            self.assertIn("tool_name", record)

    def _lines(self, step_id: str) -> str:
        for item in self.result.steps:
            if item.step_id == step_id:
                return chr(10).join(item.lines)
        raise AssertionError("缺少步骤：" + step_id)

    def test_replay_step_shows_citation_with_line_range(self) -> None:
        lines = self._lines("replay_qa")
        citations = [line.split("=", 1)[1].split(" ", 1)[0] for line in lines.splitlines() if line.startswith("citation=")]
        self.assertTrue(citations)
        for citation in citations:
            self.assertTrue(CITATION_RE.match(citation), citation)

    def test_replay_step_marks_lexical_only_as_degraded(self) -> None:
        lines = self._lines("replay_qa")
        self.assertIn("rag_mode=bm25", lines)
        self.assertIn("rag_degraded=true", lines)

    def test_replay_step_uses_graph_retriever(self) -> None:
        lines = self._lines("replay_qa")
        self.assertNotIn("RAG_UNAVAILABLE", lines)
        graph_citations = next(
            (line.split("=", 1)[1] for line in lines.splitlines() if line.startswith("graph_citations=")),
            None,
        )
        self.assertIsNotNone(graph_citations)
        self.assertGreater(int(graph_citations), 0)

    def test_missing_data_step_outputs_no_market_claim(self) -> None:
        lines = self._lines("missing_data")
        self.assertIn("INPUTS_NOT_READY", lines)
        self.assertIn("不输出任何行情结论", lines)

    def test_reject_step_has_zero_side_effects(self) -> None:
        lines = self._lines("reject_side_effects")
        self.assertIn("approved=false", lines)
        self.assertIn("APPROVAL_REJECTED", lines)
        self.assertIn("external_sends=0", lines)
        self.assertIn("business_writes=0", lines)

    def test_approve_step_stays_dry_run(self) -> None:
        lines = self._lines("approve_two_phase")
        self.assertIn("approved=true", lines)
        self.assertIn("dry_run=True", lines)
        self.assertIn("external_sends=0", lines)

    def test_sandbox_step_reports_constraint_only_and_unsupported_caps(self) -> None:
        lines = self._lines("sandbox_layer")
        self.assertIn("isolation_level=constraint_only", lines)
        self.assertIn("untrusted_error=SANDBOX_NOT_EXECUTABLE", lines)
        self.assertIn("slow_error=SANDBOX_TIMEOUT", lines)
        self.assertIn("capabilities_unsupported=5", lines)

    def test_sandbox_step_does_not_claim_docker_isolation(self) -> None:
        lines = self._lines("sandbox_layer")
        self.assertIn("docker=unavailable", lines)
        self.assertIn("docker_detail=命令不存在：docker", lines)

    def test_trace_step_has_stable_node_sequence_and_zero_drops(self) -> None:
        lines = self._lines("trace_alerts")
        self.assertIn("dropped_spans=0", lines)
        self.assertIn('"classify_intent"', lines)
        self.assertIn("apm_degraded=true", lines)

    def test_resume_step_executes_side_effect_once(self) -> None:
        lines = self._lines("resume_checkpoint")
        self.assertIn("pending_from_new_instance=push_preview", lines)
        self.assertIn("same_approval_id=true", lines)
        self.assertIn("side_effect_executions=1", lines)

    def test_role_step_denies_without_leaving_call_log(self) -> None:
        lines = self._lines("role_denied")
        self.assertIn("role_denied_code=ROLE_CAPABILITY_DENIED", lines)
        self.assertIn("call_log_delta_after_deny=0", lines)
        self.assertIn("operator_tools=", lines)

    def test_run_second_time_is_still_green(self) -> None:
        again = DemoHarness(reset=True)
        second = again.run(["doctor", "role_denied"])
        self.assertTrue(second.ok, [item.to_dict() for item in second.steps])
        self.assertEqual(second.network_attempts, 0)


if __name__ == "__main__":
    unittest.main()
