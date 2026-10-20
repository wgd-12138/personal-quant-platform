"""工具 schema 测试：每个 allowlist 工具的参数契约必须显式、封闭、可校验。"""

from __future__ import annotations

import datetime as dt
import pathlib
import unittest

from pq_agent.tools import TOOL_REGISTRY, ToolError, ToolRegistry
from pq_agent.tools.contracts import CAPABILITIES, ToolSpec, arguments_hash
from pq_agent.tools.quant_tools import SLOT_KEYS, build_default_registry


class FakeProvider:
    def snapshot(self, as_of):
        raise RuntimeError("schema 测试不应触发取数")


def _full_registry():
    return build_default_registry(
        provider=FakeProvider(),
        reports_dir=pathlib.Path(".tmp/schema-reports"),
        runs_dir=pathlib.Path(".tmp/schema-runs"),
        positions_path=pathlib.Path(".tmp/schema.csv"),
        allowed_dates=(dt.date(2026, 7, 9),),
    )


class SpecContractTests(unittest.TestCase):
    def test_every_spec_declares_required_contract_fields(self) -> None:
        for spec in _full_registry().specs():
            with self.subTest(tool=spec.name):
                self.assertTrue(spec.summary, "每个工具必须有中文说明")
                self.assertIn(spec.capability, CAPABILITIES)
                self.assertIn(spec.idempotency, ("safe", "requires_approval", "non_retryable"))
                self.assertGreater(spec.timeout_s, 0)
                self.assertIsNotNone(spec.handler)

    def test_side_effect_tools_must_require_approval(self) -> None:
        for spec in _full_registry().specs():
            with self.subTest(tool=spec.name):
                if spec.side_effects:
                    self.assertTrue(spec.requires_approval, spec.name)

    def test_notify_tools_must_declare_side_effect(self) -> None:
        for spec in _full_registry().specs():
            with self.subTest(tool=spec.name):
                if spec.notifies_external:
                    self.assertIn("notify_external", spec.side_effects)

    def test_read_only_tools_never_write_or_notify(self) -> None:
        for spec in _full_registry().specs():
            with self.subTest(tool=spec.name):
                if spec.is_read_only:
                    self.assertFalse(spec.writes_business_files)
                    self.assertFalse(spec.notifies_external)
                    self.assertEqual(spec.side_effects, ())

    def test_args_schema_is_closed(self) -> None:
        for spec in _full_registry().specs():
            with self.subTest(tool=spec.name):
                schema = spec.args_schema()
                self.assertFalse(schema["additionalProperties"])
                self.assertEqual(set(schema["properties"]), set(spec.arg_names))

    def test_slot_argument_is_enumerated(self) -> None:
        spec = next(s for s in _full_registry().specs() if s.name == "push_preview")
        slot = next(a for a in spec.args if a.name == "slot")
        self.assertEqual(tuple(slot.choices), SLOT_KEYS)
        self.assertEqual(len(SLOT_KEYS), 7)

    def test_registry_schemas_are_json_serializable(self) -> None:
        import json

        json.dumps(_full_registry().schemas(), ensure_ascii=False)


class RegistryBuildTests(unittest.TestCase):
    def test_default_registry_without_dependencies_is_read_only(self) -> None:
        names = TOOL_REGISTRY.names()
        self.assertEqual(names, ["policy_rules_read", "positions_snapshot", "readiness_check"])
        for spec in TOOL_REGISTRY.specs():
            self.assertTrue(spec.is_read_only, spec.name)

    def test_missing_sender_removes_push_send_entirely(self) -> None:
        reg = build_default_registry(runs_dir=pathlib.Path(".tmp/schema-runs"))
        self.assertIn("push_preview", reg.names())
        self.assertNotIn("push_send", reg.names())

    def test_missing_provider_removes_data_tools(self) -> None:
        reg = build_default_registry()
        self.assertNotIn("market_snapshot", reg.names())
        self.assertNotIn("daily_pack_preview", reg.names())

    def test_unknown_tool_is_denied(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            TOOL_REGISTRY.get("push_run")
        self.assertEqual(ctx.exception.code, "TOOL_NOT_ALLOWED")

    def test_trading_layer_prefix_is_rejected(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            TOOL_REGISTRY.register(
                ToolSpec(
                    name="broker_order",
                    capability="read_local",
                    side_effects=(),
                    args=(),
                    returns={},
                    timeout_s=1.0,
                    idempotency="safe",
                    risk="none",
                    summary="x",
                    handler=lambda a, dry_run=True: {},
                )
            )
        self.assertEqual(ctx.exception.code, "TOOL_TRADING_LAYER_REJECTED")

    def test_loop_command_is_rejected(self) -> None:
        with self.assertRaises(ToolError) as ctx:
            TOOL_REGISTRY.register(
                ToolSpec(
                    name="position_watch",
                    capability="read_local",
                    side_effects=(),
                    args=(),
                    returns={},
                    timeout_s=1.0,
                    idempotency="safe",
                    risk="none",
                    summary="x",
                    cli_command="position-monitor",
                    handler=lambda a, dry_run=True: {},
                )
            )
        self.assertEqual(ctx.exception.code, "TOOL_LOOP_COMMAND_REJECTED")


if __name__ == "__main__":
    unittest.main()
