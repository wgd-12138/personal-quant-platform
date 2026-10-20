"""A1-01：AgentState 与版本化序列化合同的验收测试。

不使用 tempfile（本机沙箱 os.mkdir(0o700) 自锁），需要临时目录时用 <repo>/.tmp/。
"""

from __future__ import annotations

import datetime as dt
import json
import unittest
from pathlib import Path

from pq_agent.contracts import (
    APPROVAL_JSON_SCHEMA,
    BUDGET_JSON_SCHEMA,
    CITATION_JSON_SCHEMA,
    DATA_QUALITY_JSON_SCHEMA,
    ERROR_JSON_SCHEMA,
    JSON_SCHEMAS,
    MESSAGE_JSON_SCHEMA,
    RECEIPT_JSON_SCHEMA,
    TOOL_CALL_JSON_SCHEMA,
    TOOL_RESULT_JSON_SCHEMA,
    Approval,
    Budget,
    Citation,
    ContractError,
    DataQuality,
    Error,
    Message,
    Receipt,
    ToolCall,
    ToolResult,
    require_int,
    validate_against_schema,
)
from pq_agent.state import (
    GRAPH_VERSION,
    STATE_FIELDS,
    STATE_JSON_SCHEMA,
    STATE_MIGRATIONS,
    STATE_SCHEMA_VERSION,
    STATE_START_REQUIRED_FIELDS,
    AgentState,
    guard_state_for_persistence,
    migrate_state,
    new_state,
    require_startable_state,
    state_from_dict,
    state_from_json,
    state_to_dict,
    state_to_json,
    validate_state,
)

ROOT = Path(__file__).resolve().parents[2]
V0_FIXTURE = ROOT / "tests" / "fixtures" / "state" / "state_v0_legacy.json"
NOW = dt.datetime(2026, 7, 9, 16, 10, tzinfo=dt.timezone(dt.timedelta(hours=8)))
HASH64 = "a" * 64


def base_state() -> AgentState:
    return new_state(thread_id="thread-1", user_query="演示：今天的持仓怎么样", now=NOW)


def assert_code(test: unittest.TestCase, code: str, func, *args, **kwargs) -> ContractError:
    with test.assertRaises(ContractError) as ctx:
        func(*args, **kwargs)
    test.assertEqual(ctx.exception.code, code, str(ctx.exception))
    return ctx.exception


class ContractRoundTripTests(unittest.TestCase):
    def test_message_round_trip(self) -> None:
        item = Message("m1", "user", "脱敏摘要")
        self.assertEqual(Message.from_dict(item.to_dict()), item)

    def test_tool_call_round_trip(self) -> None:
        item = ToolCall("c1", "quant.daily_pack", {"as_of": "2026-07-09"}, "business_write", False)
        self.assertEqual(ToolCall.from_dict(item.to_dict()), item)
        self.assertEqual(ToolCall.from_dict(item.to_dict()).arguments, {"as_of": "2026-07-09"})

    def test_tool_result_round_trip_success(self) -> None:
        item = ToolResult("c1", "quant.daily_pack", True, summary="生成成功", data={"sections": 16}, artifacts=("reports/2026-07-09/summary.md",))
        self.assertEqual(ToolResult.from_dict(item.to_dict()), item)

    def test_tool_result_round_trip_failure(self) -> None:
        item = ToolResult("c1", "quant.daily_pack", False, summary="缺数据", error_code="INPUTS_NOT_READY")
        self.assertEqual(ToolResult.from_dict(item.to_dict()), item)

    def test_citation_round_trip(self) -> None:
        item = Citation("chunk-1", "docs/agent/SECRET_POLICY.md", 10, 20, HASH64, title="密钥政策")
        self.assertEqual(Citation.from_dict(item.to_dict()), item)

    def test_approval_round_trip_pending(self) -> None:
        item = Approval("ap1", "c1", "notify.push_send", "abcdef123456", "notify_external", "pending", requested_at=NOW.isoformat())
        self.assertEqual(Approval.from_dict(item.to_dict()), item)
        self.assertFalse(item.decided)

    def test_approval_round_trip_approved(self) -> None:
        item = Approval(
            "ap1",
            "c1",
            "notify.push_send",
            "abcdef123456",
            "notify_external",
            "approved",
            requested_at=NOW.isoformat(),
            decided_at=NOW.isoformat(),
            actor="owner",
            reason="确认发送",
        )
        self.assertEqual(Approval.from_dict(item.to_dict()), item)
        self.assertTrue(item.decided)

    def test_receipt_round_trip(self) -> None:
        item = Receipt("r1", "ap1", "c1", "notify.push_send", "committed", "abcdef123456", created_at=NOW.isoformat(), external_id="msg-1")
        self.assertEqual(Receipt.from_dict(item.to_dict()), item)

    def test_error_round_trip(self) -> None:
        item = Error("check_inputs", "INPUTS_NOT_READY", "ValueError", "缺 positions.csv", False, NOW.isoformat())
        self.assertEqual(Error.from_dict(item.to_dict()), item)

    def test_data_quality_round_trip(self) -> None:
        item = DataQuality(
            "replay",
            "fixture:demo",
            field_quality={"boards": "ok", "news": "missing"},
            as_of="2026-07-09",
            fetched_at=NOW.isoformat(),
            data_snapshot_id="replay:2026-07-09:675f18e6f38c",
        )
        self.assertEqual(DataQuality.from_dict(item.to_dict()), item)

    def test_budget_round_trip(self) -> None:
        item = Budget(started_at=NOW.isoformat(), steps=3, tokens=500)
        self.assertEqual(Budget.from_dict(item.to_dict()), item)

    def test_every_contract_has_json_schema_with_schema_version(self) -> None:
        for name, schema in JSON_SCHEMAS.items():
            with self.subTest(schema=name):
                self.assertIn("schema_version", schema["properties"])
                self.assertIn("schema_version", schema["required"])


class SchemaValidatorTests(unittest.TestCase):
    def test_rejects_unknown_property(self) -> None:
        payload = ToolCall("c1", "t", {}, "read_local", True).to_dict()
        payload["webhook_url"] = "https://example.invalid"
        assert_code(self, "SCHEMA_ADDITIONAL_PROPERTY", validate_against_schema, payload, TOOL_CALL_JSON_SCHEMA, "tool_call")

    def test_rejects_wrong_type(self) -> None:
        payload = ToolCall("c1", "t", {}, "read_local", True).to_dict()
        payload["dry_run"] = "yes"
        assert_code(self, "SCHEMA_TYPE_MISMATCH", validate_against_schema, payload, TOOL_CALL_JSON_SCHEMA, "tool_call")

    def test_rejects_bad_enum(self) -> None:
        payload = ToolCall("c1", "t", {}, "read_local", True).to_dict()
        payload["capability"] = "root"
        assert_code(self, "SCHEMA_ENUM_MISMATCH", validate_against_schema, payload, TOOL_CALL_JSON_SCHEMA, "tool_call")

    def test_rejects_missing_required(self) -> None:
        payload = Message("m1", "user", "摘要").to_dict()
        payload.pop("role")
        assert_code(self, "SCHEMA_REQUIRED", validate_against_schema, payload, MESSAGE_JSON_SCHEMA, "message")

    def test_accepts_valid_payloads(self) -> None:
        samples = [
            ("message", MESSAGE_JSON_SCHEMA, Message("m1", "user", "摘要").to_dict()),
            ("tool_call", TOOL_CALL_JSON_SCHEMA, ToolCall("c1", "t", {"as_of": "2026-07-09"}, "read_local", True).to_dict()),
            ("tool_result", TOOL_RESULT_JSON_SCHEMA, ToolResult("c1", "t", True, summary="ok").to_dict()),
            ("citation", CITATION_JSON_SCHEMA, Citation("k1", "docs/a.md", 1, 2, HASH64).to_dict()),
            ("approval", APPROVAL_JSON_SCHEMA, Approval("ap1", "c1", "t", "abcdef123456", "business_write").to_dict()),
            ("receipt", RECEIPT_JSON_SCHEMA, Receipt("r1", "ap1", "c1", "t", "skipped", "abcdef123456").to_dict()),
            ("error", ERROR_JSON_SCHEMA, Error("n1", "CODE", "ValueError").to_dict()),
            ("data_quality", DATA_QUALITY_JSON_SCHEMA, DataQuality("replay", "fixture:demo").to_dict()),
            ("budget", BUDGET_JSON_SCHEMA, Budget().to_dict()),
        ]
        for name, schema, payload in samples:
            with self.subTest(schema=name):
                self.assertEqual(validate_against_schema(payload, schema, "x"), payload)


class ContractRejectionTests(unittest.TestCase):
    def test_tool_call_rejects_unknown_capability(self) -> None:
        assert_code(self, "ENUM_INVALID", ToolCall, "c1", "t", {}, "admin")

    def test_tool_call_rejects_non_json_arguments(self) -> None:
        assert_code(self, "JSON_TYPE_UNSUPPORTED", ToolCall, "c1", "t", {"when": NOW})

    def test_tool_call_rejects_bytes(self) -> None:
        assert_code(self, "JSON_TYPE_UNSUPPORTED", ToolCall, "c1", "t", {"blob": b"x"})

    def test_tool_result_rejects_ok_with_error_code(self) -> None:
        assert_code(self, "RESULT_INCONSISTENT", ToolResult, "c1", "t", True, "", "SOME_CODE")

    def test_tool_result_rejects_failure_without_error_code(self) -> None:
        assert_code(self, "RESULT_INCONSISTENT", ToolResult, "c1", "t", False, "坏了")

    def test_tool_result_rejects_absolute_artifact(self) -> None:
        assert_code(self, "PATH_NOT_RELATIVE", ToolResult, "c1", "t", True, "", None, {}, ("C:/tmp/x.md",))

    def test_citation_rejects_traversal(self) -> None:
        assert_code(self, "PATH_HAS_TRAVERSAL", Citation, "k1", "docs/../secret.md", 1, 2, HASH64)

    def test_citation_rejects_bad_hash(self) -> None:
        assert_code(self, "HASH_INVALID", Citation, "k1", "docs/a.md", 1, 2, "xyz")

    def test_citation_rejects_inverted_range(self) -> None:
        assert_code(self, "FIELD_OUT_OF_RANGE", Citation, "k1", "docs/a.md", 9, 3, HASH64)

    def test_approval_rejects_pending_with_decided_at(self) -> None:
        assert_code(self, "APPROVAL_INCONSISTENT", Approval, "ap1", "c1", "t", "abcdef123456", "business_write", "pending", NOW.isoformat(), NOW.isoformat())

    def test_approval_rejects_decided_without_timestamp(self) -> None:
        assert_code(self, "APPROVAL_INCONSISTENT", Approval, "ap1", "c1", "t", "abcdef123456", "business_write", "approved")

    def test_receipt_rejects_unknown_status(self) -> None:
        assert_code(self, "ENUM_INVALID", Receipt, "r1", "ap1", "c1", "t", "done", "abcdef123456")

    def test_budget_rejects_zero_max_steps(self) -> None:
        assert_code(self, "FIELD_OUT_OF_RANGE", Budget, 0)

    def test_message_rejects_full_prompt_field(self) -> None:
        payload = Message("m1", "user", "摘要").to_dict()
        payload["content"] = "完整 prompt"
        assert_code(self, "MESSAGE_FORBIDDEN_FIELD", Message.from_dict, payload)

    def test_require_int_rejects_bool(self) -> None:
        assert_code(self, "TYPE_INVALID", require_int, True, "x", minimum=1)

    def test_data_quality_rejects_bad_field_quality_value(self) -> None:
        assert_code(self, "TYPE_INVALID", DataQuality, "replay", "fixture:demo", {"boards": 1})

    def test_data_quality_rejects_bad_as_of(self) -> None:
        assert_code(self, "DATE_INVALID", DataQuality, "replay", "fixture:demo", {}, "2026-13-40")


class StateShapeTests(unittest.TestCase):
    def test_new_state_is_valid_and_startable(self) -> None:
        state = base_state()
        validate_state(state, require_startable=True)
        self.assertEqual(state["checkpoint_schema_version"], STATE_SCHEMA_VERSION)
        self.assertEqual(state["graph_version"], GRAPH_VERSION)
        self.assertEqual(state["mode"], "demo_offline")

    def test_state_fields_match_typed_dict(self) -> None:
        self.assertEqual(list(AgentState.__annotations__), list(STATE_FIELDS))
        self.assertEqual(len(STATE_FIELDS), 23)
        for name in STATE_FIELDS:
            self.assertIn(name, STATE_JSON_SCHEMA["properties"])
        self.assertEqual(list(STATE_JSON_SCHEMA["required"]), list(STATE_FIELDS))
        self.assertFalse(STATE_JSON_SCHEMA["additionalProperties"])

    def test_state_json_round_trip(self) -> None:
        state = base_state()
        self.assertEqual(state_from_json(state_to_json(state)), state)

    def test_state_to_dict_is_json_safe(self) -> None:
        state = base_state()
        self.assertEqual(json.loads(json.dumps(state_to_dict(state), ensure_ascii=False)), state)

    def test_new_state_requires_thread_id(self) -> None:
        assert_code(self, "STATE_NOT_STARTABLE", new_state, thread_id="", now=NOW)


class StateValidationTests(unittest.TestCase):
    def _broken(self, **changes):
        state = base_state()
        state.update(changes)
        return state

    def test_missing_thread_id_fails(self) -> None:
        state = base_state()
        state["thread_id"] = ""
        assert_code(self, "STATE_NOT_STARTABLE", validate_state, state, require_startable=True)

    def test_missing_mode_fails(self) -> None:
        state = base_state()
        del state["mode"]
        assert_code(self, "STATE_MISSING_FIELD", validate_state, state)

    def test_missing_schema_version_fails(self) -> None:
        state = base_state()
        del state["checkpoint_schema_version"]
        assert_code(self, "STATE_MISSING_FIELD", require_startable_state, state)

    def test_unknown_field_fails(self) -> None:
        state = self._broken(headers={"Authorization": "Bearer x"})
        assert_code(self, "STATE_UNKNOWN_FIELD", validate_state, state)

    def test_forbidden_key_inside_slots_fails(self) -> None:
        state = self._broken(slots={"webhook_url": "https://example.invalid"})
        assert_code(self, "STATE_FORBIDDEN_KEY", validate_state, state)

    def test_forbidden_key_headers_inside_slots_fails(self) -> None:
        state = self._broken(slots={"headers": {"a": "b"}})
        assert_code(self, "STATE_FORBIDDEN_KEY", validate_state, state)

    def test_wrong_type_cursor_fails(self) -> None:
        state = self._broken(cursor="3")
        assert_code(self, "TYPE_INVALID", validate_state, state)

    def test_bad_mode_fails(self) -> None:
        state = self._broken(mode="staging")
        assert_code(self, "ENUM_INVALID", validate_state, state)

    def test_bad_as_of_fails(self) -> None:
        state = self._broken(as_of="2026/07/09")
        assert_code(self, "DATE_INVALID", validate_state, state)

    def test_bad_intent_source_fails(self) -> None:
        state = self._broken(intent_source="model")
        assert_code(self, "ENUM_INVALID", validate_state, state)

    def test_message_with_unknown_field_fails(self) -> None:
        state = self._broken(messages=[{"message_id": "m1", "role": "user", "summary": "x", "schema_version": 1, "content": "完整提示词"}])
        assert_code(self, "MESSAGE_FORBIDDEN_FIELD", validate_state, state)

    def test_message_with_prompt_field_is_blocked_by_key_policy(self) -> None:
        state = self._broken(messages=[{"message_id": "m1", "role": "user", "summary": "x", "schema_version": 1, "prompt": "完整提示词"}])
        assert_code(self, "STATE_FORBIDDEN_KEY", validate_state, state)

    def test_message_bad_role_fails(self) -> None:
        state = self._broken(messages=[{"message_id": "m1", "role": "root", "summary": "x", "schema_version": 1}])
        assert_code(self, "ENUM_INVALID", validate_state, state)

    def test_plan_entry_invalid_fails(self) -> None:
        state = self._broken(plan=[{"call_id": "c1", "tool_name": "t", "arguments": {}, "capability": "admin", "dry_run": True, "schema_version": 1}])
        assert_code(self, "ENUM_INVALID", validate_state, state)

    def test_pending_approval_invalid_fails(self) -> None:
        state = self._broken(pending_approval={"approval_id": "ap1"})
        assert_code(self, "TYPE_INVALID", validate_state, state)

    def test_errors_entry_must_be_single_line(self) -> None:
        state = self._broken(errors=["INPUTS_NOT_READY\n堆栈第二行"])
        assert_code(self, "STATE_ERROR_MULTILINE", validate_state, state)

    def test_budget_invalid_fails(self) -> None:
        state = self._broken(budget={"max_steps": 0, "max_tokens": 1, "max_wall_clock_s": 1.0, "steps": 0, "tokens": 0, "started_at": None, "schema_version": 1})
        assert_code(self, "FIELD_OUT_OF_RANGE", validate_state, state)

    def test_graph_version_mismatch_fails(self) -> None:
        state = self._broken(graph_version="pq_agent.graph/0")
        assert_code(self, "GRAPH_VERSION_MISMATCH", validate_state, state, expected_graph_version=GRAPH_VERSION)


class StateSecretTests(unittest.TestCase):
    def test_webhook_in_user_query_is_rejected(self) -> None:
        state = base_state()
        state["user_query"] = "把这个地址发出去 https://open.feishu.cn/open-apis/bot/v2/hook/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        assert_code(self, "STATE_SECRET_DETECTED", validate_state, state)

    def test_api_key_like_value_is_rejected(self) -> None:
        state = base_state()
        state["slots"] = {"note": "sk-abcdefghijklmnopqrst"}
        assert_code(self, "STATE_SECRET_DETECTED", validate_state, state)

    def test_guard_scrubs_registered_secret(self) -> None:
        canary = "CANARY-8f3a2b1c-do-not-log"
        state = base_state()
        state["user_query"] = f"请帮我查一下 {canary} 这个值"
        scrubbed = guard_state_for_persistence(state, secrets=(canary,))
        self.assertNotIn(canary, json.dumps(scrubbed, ensure_ascii=False))
        self.assertIn("[REDACTED:known_secret]", scrubbed["user_query"])

    def test_guard_scrubs_webhook_url(self) -> None:
        state = base_state()
        state["user_query"] = "webhook 是 https://open.feishu.cn/open-apis/bot/v2/hook/11111111-2222-3333-4444-555555555555"
        scrubbed = guard_state_for_persistence(state)
        self.assertNotIn("open.feishu.cn", json.dumps(scrubbed, ensure_ascii=False))
        self.assertIn("[REDACTED:feishu_webhook]", scrubbed["user_query"])

    def test_guard_output_is_still_valid_state(self) -> None:
        state = base_state()
        state["messages"] = [Message("m1", "user", "联系我 test@example.invalid").to_dict()]
        scrubbed = guard_state_for_persistence(state)
        validate_state(scrubbed)
        self.assertIn("[REDACTED:email]", scrubbed["messages"][0]["summary"])


class StateVersionTests(unittest.TestCase):
    def test_fixture_exists(self) -> None:
        self.assertTrue(V0_FIXTURE.is_file(), str(V0_FIXTURE))

    def test_v0_fixture_requires_explicit_migration(self) -> None:
        payload = json.loads(V0_FIXTURE.read_text(encoding="utf-8"))
        assert_code(self, "CHECKPOINT_VERSION_MISMATCH", state_from_dict, payload, migrate=False)

    def test_v0_fixture_migrates_to_current(self) -> None:
        payload = json.loads(V0_FIXTURE.read_text(encoding="utf-8"))
        state = state_from_dict(payload)
        self.assertEqual(state["checkpoint_schema_version"], STATE_SCHEMA_VERSION)
        self.assertEqual(state["timezone"], "Asia/Shanghai")
        self.assertEqual(state["graph_version"], GRAPH_VERSION)
        self.assertEqual(state["request_id"], "legacy-thread-1:legacy-v0")
        self.assertEqual(state["messages"][0]["schema_version"], 1)
        self.assertEqual(state["intent"], "history_question")
        validate_state(state, require_startable=True)

    def test_migration_is_registered(self) -> None:
        self.assertIn((0, 1), STATE_MIGRATIONS)

    def test_future_version_is_rejected(self) -> None:
        payload = json.loads(V0_FIXTURE.read_text(encoding="utf-8"))
        payload["checkpoint_schema_version"] = 99
        assert_code(self, "CHECKPOINT_VERSION_MISMATCH", state_from_dict, payload)

    def test_non_integer_version_is_rejected(self) -> None:
        payload = json.loads(V0_FIXTURE.read_text(encoding="utf-8"))
        payload["checkpoint_schema_version"] = "1"
        assert_code(self, "STATE_FIELD_TYPE", state_from_dict, payload)

    def test_downgrade_is_rejected(self) -> None:
        payload = json.loads(V0_FIXTURE.read_text(encoding="utf-8"))
        payload["checkpoint_schema_version"] = 5
        assert_code(self, "CHECKPOINT_VERSION_MISMATCH", migrate_state, payload, target=1)

    def test_migrate_missing_version_is_rejected(self) -> None:
        assert_code(self, "CHECKPOINT_VERSION_MISMATCH", migrate_state, {"thread_id": "t"})

    def test_broken_json_is_rejected(self) -> None:
        assert_code(self, "STATE_JSON_INVALID", state_from_json, "{not json")


class StateBudgetTests(unittest.TestCase):
    def test_budget_within_limits(self) -> None:
        budget = Budget(started_at=NOW.isoformat(), steps=1, tokens=10)
        verdict = budget.evaluate(NOW + dt.timedelta(seconds=5))
        self.assertTrue(verdict["ok"])
        self.assertIsNone(verdict["code"])

    def test_budget_exceeded_by_steps(self) -> None:
        budget = Budget(max_steps=2, started_at=NOW.isoformat(), steps=2)
        verdict = budget.evaluate(NOW, extra_steps=1)
        self.assertFalse(verdict["ok"])
        self.assertEqual(verdict["code"], "BUDGET_EXCEEDED")
        self.assertIn("steps", verdict["reason"])

    def test_budget_exceeded_by_tokens(self) -> None:
        budget = Budget(max_tokens=10, started_at=NOW.isoformat())
        verdict = budget.evaluate(NOW, extra_tokens=11)
        self.assertEqual(verdict["code"], "BUDGET_EXCEEDED")
        self.assertIn("tokens", verdict["reason"])

    def test_budget_exceeded_by_wall_clock(self) -> None:
        budget = Budget(max_wall_clock_s=1.0, started_at=NOW.isoformat())
        verdict = budget.evaluate(NOW + dt.timedelta(seconds=2))
        self.assertEqual(verdict["code"], "BUDGET_EXCEEDED")
        self.assertIn("elapsed_s", verdict["reason"])

    def test_budget_evaluate_requires_aware_now(self) -> None:
        assert_code(self, "DATETIME_NAIVE", Budget().evaluate, dt.datetime(2026, 7, 9, 16, 10))


class StateStartupTests(unittest.TestCase):
    def test_start_required_fields_are_declared(self) -> None:
        for name in ("thread_id", "mode", "checkpoint_schema_version"):
            self.assertIn(name, STATE_START_REQUIRED_FIELDS)

    def test_require_startable_accepts_valid_state(self) -> None:
        state = base_state()
        self.assertEqual(require_startable_state(state), state)

    def test_require_startable_rejects_empty_thread_id(self) -> None:
        state = base_state()
        state["thread_id"] = ""
        assert_code(self, "STATE_NOT_STARTABLE", require_startable_state, state)


if __name__ == "__main__":
    unittest.main()
