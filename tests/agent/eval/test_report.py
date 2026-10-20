"""A5-02 报告渲染单测。

报告是交付物：必须能看出基线、样本量、门槛来源和 blocker，未通过的用例必须能在
badcase 清单里定位到具体断言与原文 detail。
"""
import json
import pathlib
import unittest
import uuid

from ._eval_loader import REPO_ROOT, load_eval_module

R = load_eval_module("report")


def _payload(*, ok=True, blockers=(), verdicts=None):
    return {
        "schema_version": "pq_agent.eval_all/1",
        "generated_at": "2026-10-14T18:54:51+08:00",
        "suite_digest": "a" * 64,
        "ok": ok,
        "layers_run": ["l0", "l1", "l2", "l3"],
        "blockers": list(blockers),
        "environment": {"platform": "win32", "python": "3.12.7"},
        "threshold_sources": {"l2_golden": "judge.py 的 14 条确定性断言"},
        "l0": {"status": "ok", "tests": 754, "failures": 0, "errors": 0, "skipped": 0, "baseline_tests": 150, "new_failures": 0, "tests_delta": 604},
        "l1": {
            "status": "ok",
            "intent": {
                "status": "ok",
                "gate_failures": [],
                "l2_status": "NOT_MEASURED",
                "dataset_sha256": {"train": "c" * 64, "dev": "d" * 64, "test": "e" * 64},
                "splits": {"dev": {"total": 45, "pipeline_accuracy": 0.9556, "macro_f1": 0.9528, "l1_coverage": 0.9556, "slot_accuracy": 1.0}},
            },
            "rag": {
                "status": "ok",
                "bm25_metrics": {"hit@5": 0.7, "mrr@5": 0.6083},
                "modes": {"bm25": {"hit@1": 0.5667, "hit@5": 0.7, "mrr@5": 0.6083, "citation_valid_rate": 1.0, "degraded_flagged_cases": 30}},
                "index_hash": "f" * 64,
                "corpus_hash": "0" * 64,
                "backend_note": "词法档：BM25，本机真实后端。",
            },
            "sandbox": {"status": "ok", "static_blocked": 8, "malicious_cases": 10, "benign_clear": 10, "benign_cases": 10, "corpus_sha256": "1" * 64},
            "tool_schema": {"sha256": "2" * 64, "tool_count": 9},
        },
        "l2": {
            "status": "ok",
            "case_count": 10,
            "ran": 10,
            "judge": {
                "passed": 10 if not verdicts else 9,
                "case_count": 10,
                "failed_checks": {} if not verdicts else {"intent_matches": 1},
                "llm_judge_note": "LLM judge 只作为辅证输出，永远不参与判定。",
                "verdicts": list(verdicts or []),
            },
        },
        "l3": {"status": "unknown", "cost": {"status": "unknown"}},
    }


class ReportRenderTests(unittest.TestCase):
    def test_report_has_baseline_scope_and_reproduce_command(self):
        text = R.render_report(_payload())
        for expected in ("## 1. 基线与口径", "## 2. 分层结果", "## 5. 未解决风险与 blocker", "## 6. 复现命令", "| L0 基线 |", "754", "150"):
            self.assertIn(expected, text)

    def test_report_prints_sample_count_not_empty_cell(self):
        text = R.render_report(_payload())
        self.assertIn("| dev | 45 |", text)

    def test_report_lists_blockers(self):
        text = R.render_report(_payload(ok=False, blockers=("no_docker_on_this_host",)))
        self.assertIn("- no_docker_on_this_host", text)
        self.assertIn("整体结论：未通过", text)

    def test_report_says_no_blockers_when_clean(self):
        text = R.render_report(_payload())
        self.assertIn("## 5. 未解决风险与 blocker", text)
        self.assertIn("- 无", text)

    def test_badcases_lists_failing_check_with_detail(self):
        verdicts = [
            {
                "case_id": "gt-99-demo",
                "split": "dev",
                "ok": False,
                "failed_checks": ["intent_matches"],
                "checks": [{"name": "intent_matches", "ok": False, "detail": "期望 market_check，实际 pos_question（来源 L1）"}],
            }
        ]
        suite = {"cases": [{"case_id": "gt-99-demo", "query": "今天大盘怎么样", "why": "守意图", "trap": "别名不够"}]}
        text = R.render_badcases(_payload(ok=False, verdicts=verdicts), suite=suite)
        self.assertIn("gt-99-demo", text)
        self.assertIn("intent_matches", text)
        self.assertIn("期望 market_check，实际 pos_question", text)
        self.assertIn("这条用例守的是：守意图", text)

    def test_badcases_says_none_when_all_passed(self):
        text = R.render_badcases(_payload(), suite={"cases": []})
        self.assertIn("本次没有判题未通过的用例。", text)

    def test_badcases_flags_known_ambiguity_from_suite_why(self):
        suite = {"cases": [{"case_id": "gt-04-history-report", "query": "昨天那份日报讲了什么", "why": "本用例同时是已知 badcase：历史类被判成 report_generate", "trap": "别名覆盖不足"}]}
        text = R.render_badcases(_payload(), suite=suite)
        self.assertIn("## 已知歧义（不算失败，但必须记着）", text)
        self.assertIn("gt-04-history-report", text)

    def test_write_reports_writes_two_files_under_the_given_root(self):
        root = REPO_ROOT / ".tmp" / "a5-02-tests" / ("report-" + uuid.uuid4().hex[:8])
        root.mkdir(parents=True, exist_ok=True)
        written = R.write_reports(_payload(), root=root)
        self.assertEqual(2, len(written))
        for rel in written:
            target = root / rel
            self.assertTrue(target.exists(), rel)
            self.assertGreater(target.stat().st_size, 100)


if __name__ == "__main__":
    unittest.main()
