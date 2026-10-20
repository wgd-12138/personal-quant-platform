"""工具层行为测试：审批门禁、参数校验、只读保证与 fail-closed 规则。"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import unittest

from pq_agent.tools import TOOL_REGISTRY, ToolError
from pq_agent.tools.contracts import build_result, truncate_payload
from pq_agent.tools.quant_tools import SLOT_KEYS, build_default_registry

FIXTURE_DIR = pathlib.Path(__file__).resolve().parents[3] / "tests" / "fixtures"


def _provider():
    from pq_agent.data import create_provider

    return create_provider("demo_offline")


class ApprovalGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reg = build_default_registry(
            provider=_provider(),
            reports_dir=pathlib.Path(".tmp/behavior-reports"),
            runs_dir=pathlib.Path(".tmp/behavior-runs"),
            allowed_dates=(dt.date(2026, 7, 9),),
        )

    def test_write_without_token_is_refused(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            self.reg.invoke("report_write", {"as_of": "2026-07-09"})
        self.assertEqual(ctx.exception.code, "TOOL_APPROVAL_REQUIRED")

    def test_notify_without_token_is_refused(self) -> None:
        reg = build_default_registry(
            provider=_provider(),
            runs_dir=pathlib.Path(".tmp/behavior-runs"),
            sender=lambda as_of, slot: "sent",
            allowed_dates=(dt.date(2026, 7, 9),),
        )
        with self.assertRaises(ToolError) as ctx:
            reg.invoke("push_send", {"as_of": "2026-07-09", "slot": "pre_open"})
        self.assertEqual(ctx.exception.code, "TOOL_APPROVAL_REQUIRED")

    def test_wrong_token_is_refused(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            self.reg.invoke(
                "push_preview",
                {"as_of": "2026-07-09", "slot": "pre_open"},
                approval_token="deadbeef",
            )
        self.assertEqual(ctx.exception.code, "TOOL_APPROVAL_REQUIRED")

    def test_token_bound_to_arguments_not_just_tool_name(self) -> None:
        token = self.reg.preview("push_preview", {"as_of": "2026-07-09", "slot": "pre_open"})["arguments_hash"]
        with self.assertRaises(ToolError) as ctx:
            self.reg.invoke(
                "push_preview",
                {"as_of": "2026-07-09", "slot": "evening"},
                approval_token=token,
            )
        self.assertEqual(ctx.exception.code, "TOOL_APPROVAL_REQUIRED")

    def test_matching_token_executes(self) -> None:
        arguments = {"as_of": "2026-07-09", "slot": "pre_open"}
        token = self.reg.preview("push_preview", arguments)["arguments_hash"]
        result = self.reg.invoke("push_preview", arguments, approval_token=token)
        self.assertTrue(result["ok"])
        self.assertEqual(result["side_effects"], ["write_control_state"])


class ArgumentValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reg = build_default_registry(
            provider=_provider(),
            reports_dir=pathlib.Path(".tmp/behavior-reports"),
            runs_dir=pathlib.Path(".tmp/behavior-runs"),
            allowed_dates=(dt.date(2026, 7, 9),),
        )

    def test_forbidden_fields_are_rejected_whole_call(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            self.reg.invoke(
                "market_snapshot",
                {"as_of": "2026-07-09", "live": True, "webhook_url": "http://x"},
            )
        self.assertEqual(ctx.exception.code, "TOOL_FIELD_FORBIDDEN")
        message = ctx.exception.message
        self.assertIn("live", message)
        self.assertIn("webhook_url", message)

    def test_bad_date_format_is_rejected(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            self.reg.invoke("market_snapshot", {"as_of": "2026/07/09"})
        self.assertEqual(ctx.exception.code, "INVALID_AS_OF")

    def test_date_outside_allowlist_is_rejected(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            self.reg.invoke("market_snapshot", {"as_of": "2020-01-01"})
        self.assertEqual(ctx.exception.code, "INVALID_AS_OF")
        self.assertIn("不在允许的交易日列表内", ctx.exception.message)

    def test_missing_required_field_is_rejected(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            self.reg.invoke("market_snapshot", {})
        self.assertEqual(ctx.exception.code, "TOOL_SCHEMA_INVALID")

    def test_unknown_slot_is_rejected(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            self.reg.invoke("push_preview", {"as_of": "2026-07-09", "slot": "lunch"})
        self.assertEqual(ctx.exception.code, "TOOL_SCHEMA_INVALID")


class ReadOnlyGuaranteeTests(unittest.TestCase):
    def test_read_tools_create_no_business_files(self) -> None:
        reg = build_default_registry(provider=_provider(), allowed_dates=(dt.date(2026, 7, 9),))
        reports = pathlib.Path("reports")
        before = sorted(p.name for p in reports.iterdir()) if reports.exists() else []
        result = reg.invoke("market_snapshot", {"as_of": "2026-07-09"})
        self.assertTrue(result["ok"])
        after = sorted(p.name for p in reports.iterdir()) if reports.exists() else []
        self.assertEqual(before, after, "只读工具不得改动 reports 目录")

    def test_adapter_imports_no_process_or_network_client(self) -> None:
        """用 AST 检查真实 import,而不是查文档字符串里的词。"""
        import ast

        source = pathlib.Path("src/pq_agent/tools/quant_tools.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        forbidden = ("subprocess", "urllib.request", "http.client", "requests", "httpx", "socket")
        for module in imported:
            with self.subTest(module=module):
                self.assertFalse(
                    any(module == item or module.startswith(item + ".") for item in forbidden),
                    "工具适配层不得直接依赖进程或网络客户端:%s" % module,
                )
        self.assertIn("personal_quant.report_builder.daily_pack", imported)

    def test_call_log_records_capability_and_side_effects(self) -> None:
        reg = build_default_registry(provider=_provider(), allowed_dates=(dt.date(2026, 7, 9),))
        reg.invoke("market_snapshot", {"as_of": "2026-07-09"})
        entry = reg.call_log[-1]
        self.assertEqual(entry["tool"], "market_snapshot")
        self.assertEqual(entry["capability"], "read_external")
        self.assertIn("side_effects", entry)


class TruncationTests(unittest.TestCase):
    def test_long_string_is_truncated_and_flagged(self) -> None:
        payload, truncated = truncate_payload({"note": "x" * 9000}, max_chars=100)
        self.assertTrue(truncated)
        self.assertLess(len(payload["note"]), 9000)
        self.assertIn("truncated", payload["note"])

    def test_long_list_is_truncated_and_flagged(self) -> None:
        payload, truncated = truncate_payload(list(range(500)), max_items=10)
        self.assertTrue(truncated)
        self.assertLessEqual(len(payload), 11)

    def test_small_payload_is_untouched(self) -> None:
        original = {"ok": True, "items": [1, 2, 3]}
        payload, truncated = truncate_payload(original)
        self.assertFalse(truncated)
        self.assertEqual(payload, original)


class DemoOfflineIsolationTests(unittest.TestCase):
    def test_demo_offline_registry_makes_no_outbound_call(self) -> None:
        from pq_agent.data.provider import NetworkGuard

        reg = build_default_registry(provider=_provider(), allowed_dates=(dt.date(2026, 7, 9),))
        guard = NetworkGuard()
        with guard:
            result = reg.invoke("market_snapshot", {"as_of": "2026-07-09"})
        self.assertTrue(result["ok"])
        self.assertEqual(guard.attempt_count, 0)
        self.assertTrue(result["data_source"].startswith("demo_offline:"))

    def test_demo_snapshot_is_marked_as_demo(self) -> None:
        reg = build_default_registry(provider=_provider(), allowed_dates=(dt.date(2026, 7, 9),))
        result = reg.invoke("market_snapshot", {"as_of": "2026-07-09"})
        self.assertTrue(result["data"]["demo"])


if __name__ == "__main__":
    unittest.main()
