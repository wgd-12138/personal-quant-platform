"""A5-02 评测运行器单测。

重点锁两件容易犯的错：退出码 0 被 `or 1` 当成失败（本轮真实踩到），
以及 unittest 汇总行解析出来的数字与判定。
"""
import pathlib
import unittest

from ._eval_loader import REPO_ROOT, load_eval_module

RUN = load_eval_module("run_all")


class ExitCodeTests(unittest.TestCase):
    def test_zero_is_success(self):
        self.assertTrue(RUN._exit_ok(0))
        self.assertTrue(RUN._exit_ok("0"))

    def test_nonzero_is_failure(self):
        self.assertFalse(RUN._exit_ok(1))
        self.assertFalse(RUN._exit_ok("1"))
        self.assertFalse(RUN._exit_ok("boom"))

    def test_missing_exit_code_is_failure_not_default_success(self):
        self.assertFalse(RUN._exit_ok(None))

    def test_source_has_no_falsy_zero_exit_code_pattern(self):
        text = (REPO_ROOT / "agent_eval" / "run_all.py").read_text(encoding="utf-8")
        # 只看代码行：注释里为了说明这个坑，会原样写出错误写法。
        code = chr(10).join(
            line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")
        )
        self.assertNotIn("or 1) == 0", code)
        self.assertNotIn(chr(34) + "exit_code" + chr(34) + ") or 1", code)
        self.assertIn("_exit_ok(", code)

    def test_golden_runner_keeps_checkpoint_guard_on_write_path(self):
        text = (REPO_ROOT / "agent_eval" / "run_all.py").read_text(encoding="utf-8")
        code = chr(10).join(
            line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")
        )
        self.assertNotIn("checkpointer=saver.inner", code)


class SummaryParseTests(unittest.TestCase):
    def test_parses_ok_line(self):
        text = "Ran 754 tests in 12.345s" + chr(10) + chr(10) + "OK (skipped=1)" + chr(10)
        summary = RUN.parse_unittest_summary(text)
        self.assertEqual(754, summary["tests"])
        self.assertTrue(summary["ok"])
        self.assertEqual(0, summary["failures"])
        self.assertEqual(0, summary["errors"])
        self.assertEqual(1, summary["skipped"])

    def test_parses_failed_line_with_all_counters(self):
        text = "Ran 150 tests in 0.161s" + chr(10) + chr(10) + "FAILED (failures=3, errors=87, skipped=1)" + chr(10)
        summary = RUN.parse_unittest_summary(text)
        self.assertEqual(150, summary["tests"])
        self.assertFalse(summary["ok"])
        self.assertEqual(3, summary["failures"])
        self.assertEqual(87, summary["errors"])
        self.assertEqual(1, summary["skipped"])

    def test_empty_text_is_zero_not_a_pass(self):
        summary = RUN.parse_unittest_summary("")
        self.assertEqual(0, summary["tests"])
        self.assertFalse(summary["ok"])


class BaselineTests(unittest.TestCase):
    def test_repo_baseline_is_available_and_matches_a0_00(self):
        baseline = RUN.load_baseline()
        self.assertTrue(baseline["available"])
        self.assertEqual(150, baseline["l0"]["tests"])
        self.assertEqual(0, baseline["l0"]["failures"])
        self.assertEqual(0, baseline["l0"]["errors"])

    def test_missing_baseline_file_is_unavailable_not_a_pass(self):
        baseline = RUN.load_baseline(path=REPO_ROOT / ".tmp" / "a5-02-tests" / "no-such-baseline.json")
        self.assertFalse(baseline["available"])
        self.assertIn("基线文件缺失", baseline["note"])

    def test_layers_tuple(self):
        self.assertEqual(("l0", "l1", "l2", "l3"), RUN.LAYERS)

    def test_shim_path_exists_so_l0_uses_the_real_runner(self):
        self.assertTrue(RUN.SHIM_PATH.exists(), RUN.SHIM_PATH)


class StatusSemanticsTests(unittest.TestCase):
    def test_unknown_is_a_distinct_status_from_failed(self):
        self.assertEqual("unknown", RUN.STATUS_UNKNOWN)
        self.assertNotEqual(RUN.STATUS_UNKNOWN, RUN.STATUS_FAILED)

    def test_threshold_sources_cover_all_four_layers(self):
        keys = set(RUN.THRESHOLD_SOURCES)
        for expected in ("l0_no_new_failures", "l1_intent", "l1_rag", "l1_sandbox", "l2_golden", "l3_apm"):
            self.assertIn(expected, keys)


if __name__ == "__main__":
    unittest.main()
