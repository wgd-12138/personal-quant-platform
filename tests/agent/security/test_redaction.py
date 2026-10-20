"""redaction.py 的单元测试：脱敏、泄露扫描、统一闸门、错误摘要。

关键断言：五类产物（日志 / trace / checkpoint / ToolResult / API 响应）经过
guard_payload 后，金丝雀密钥与 webhook 的命中数必须为 0；同时必须先证明
未脱敏的同一产物确实会被扫描器命中（否则测试是空转的）。
"""

from __future__ import annotations

import json
import unittest

from pq_agent.security.redaction import (
    DEFAULT_MAX_TEXT_LEN,
    RedactionError,
    SENSITIVE_KEY_NAMES,
    ScrubPolicy,
    SecretLeakError,
    collect_secrets_from_environ,
    find_secrets,
    guard_payload,
    is_sensitive_key,
    normalize_key,
    safe_error_summary,
    scrub_text,
    scrub_value,
    secret_env_presence,
)

CANARY = "canary-9f2c4a1b7e"
WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/" + CANARY
SECRETS = (CANARY,)


def _artifacts() -> dict[str, object]:
    """五类会落盘或上报的产物，全部混入金丝雀密钥与真实 webhook 形状。"""
    return {
        "logs": [
            "push fail url=" + WEBHOOK,
            "auth=Bearer " + CANARY,
            "key=sk-abcdefgh12345678",
            "home=C:\\Users\\example-user\\x mail=someone@example.com",
        ],
        "trace": {
            "name": "push_send",
            "attributes": {
                "http.url": WEBHOOK,
                "authorization": "Bearer " + CANARY,
                "tool.name": "push_send",
            },
        },
        "checkpoint": {
            "state": {
                "user_query": "帮我推送今天的日报",
                "tool_args": {"webhook_url": WEBHOOK, "slot": "evening"},
            },
            "secret_env": {"FEISHU_WEBHOOK_URL": WEBHOOK},
        },
        "tool_result": {
            "ok": False,
            "error": "FEISHU_WEBHOOK_HTTP_ERROR:400:" + CANARY,
            "raw_body": '{"code":19001,"msg":"' + WEBHOOK + '"}',
        },
        "api_response": {
            "status": 400,
            "headers": {
                "Authorization": "Bearer " + CANARY,
                "X-Api-Key": "sk-abcdefgh12345678",
                "Content-Type": "application/json",
            },
            "body": '{"code":19001,"msg":"bad","detail":"' + WEBHOOK + '"}',
        },
    }


class ScrubTextTests(unittest.TestCase):
    def test_known_secret_is_replaced(self) -> None:
        cleaned = scrub_text("token=" + CANARY, secrets=SECRETS)
        self.assertNotIn(CANARY, cleaned)
        self.assertIn("[REDACTED:known_secret]", cleaned)

    def test_patterns_are_replaced(self) -> None:
        text = " ".join(
            [
                WEBHOOK,
                "auth=Bearer " + CANARY,
                "key=sk-abcdefgh12345678",
                "mail=someone@example.com",
                "path=C:\\Users\\example-user\\Desktop\\x",
                "home=/home/someone/x",
            ]
        )
        cleaned = scrub_text(text, secrets=SECRETS)
        self.assertNotIn(CANARY, cleaned)
        self.assertNotIn("sk-abcdefgh12345678", cleaned)
        self.assertNotIn("someone@example.com", cleaned)
        self.assertNotIn("C:\\Users\\example-user", cleaned)
        self.assertNotIn("/home/someone", cleaned)
        self.assertEqual(find_secrets(cleaned, secrets=SECRETS), [])

    def test_private_key_block_is_replaced(self) -> None:
        text = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----"
        cleaned = scrub_text(text)
        self.assertIn("[REDACTED:private_key]", cleaned)
        self.assertNotIn("MIIabc", cleaned)

    def test_truncation_marks_the_cut(self) -> None:
        cleaned = scrub_text("x" * 50, max_len=10)
        self.assertLessEqual(len(cleaned), 10 + len("…[truncated]"))
        self.assertIn("…[truncated]", cleaned)

    def test_default_max_len_is_applied(self) -> None:
        cleaned = scrub_text("y" * (DEFAULT_MAX_TEXT_LEN + 100))
        self.assertIn("…[truncated]", cleaned)

    def test_clean_text_is_unchanged(self) -> None:
        self.assertEqual(scrub_text("普通中文文本，没有秘密。"), "普通中文文本，没有秘密。")


class ScrubValueTests(unittest.TestCase):
    def test_sensitive_keys_are_replaced_wholesale(self) -> None:
        payload = {
            "authorization": "Bearer " + CANARY,
            "webhook_url": WEBHOOK,
            "api_key": "sk-abcdefgh12345678",
            "slot": "evening",
            "count": 3,
        }
        cleaned = scrub_value(payload, secrets=SECRETS)
        self.assertEqual(cleaned["authorization"], "[REDACTED:authorization]")
        self.assertEqual(cleaned["webhook_url"], "[REDACTED:webhook_url]")
        self.assertEqual(cleaned["api_key"], "[REDACTED:api_key]")
        self.assertEqual(cleaned["slot"], "evening")
        self.assertEqual(cleaned["count"], 3)
        self.assertEqual(find_secrets(cleaned, secrets=SECRETS), [])

    def test_nested_values_are_scrubbed(self) -> None:
        payload = {"outer": {"inner": ["a", WEBHOOK, {"deep": CANARY}]}}
        cleaned = scrub_value(payload, secrets=SECRETS)
        self.assertNotIn(CANARY, json.dumps(cleaned))
        self.assertNotIn("open.feishu.cn", json.dumps(cleaned))

    def test_bytes_become_length_descriptor(self) -> None:
        self.assertEqual(scrub_value(b"secret"), "<bytes len=6>")

    def test_unknown_type_fails_closed(self) -> None:
        with self.assertRaises(RedactionError) as ctx:
            scrub_value(object())
        self.assertIn("REDACTION_UNSUPPORTED_TYPE", str(ctx.exception))

    def test_unknown_type_nested_fails_closed(self) -> None:
        with self.assertRaises(RedactionError):
            scrub_value({"a": [object()]})

    def test_depth_limit_fails_closed(self) -> None:
        deep: dict[str, object] = {"v": CANARY}
        for _ in range(6):
            deep = {"n": deep}
        with self.assertRaises(RedactionError) as ctx:
            scrub_value(deep, secrets=SECRETS, policy=ScrubPolicy(max_depth=2))
        self.assertIn("REDACTION_DEPTH_EXCEEDED", str(ctx.exception))


class FindSecretsTests(unittest.TestCase):
    def test_known_secret_detected(self) -> None:
        self.assertEqual(
            find_secrets({"a": "token=" + CANARY}, secrets=SECRETS), ["known_secret"]
        )

    def test_pattern_detected_without_known_secret(self) -> None:
        self.assertIn("pattern:feishu_webhook", find_secrets(WEBHOOK))

    def test_sensitive_key_with_any_value_is_flagged(self) -> None:
        found = find_secrets({"webhook_url": "已配置"})
        self.assertIn("sensitive_key:webhookurl", found)

    def test_markers_do_not_trigger_false_positives(self) -> None:
        cleaned = scrub_value({"authorization": "Bearer " + CANARY}, secrets=SECRETS)
        self.assertEqual(find_secrets(cleaned, secrets=SECRETS), [])

    def test_scanner_is_not_vacuous_on_all_artifacts(self) -> None:
        for name, artifact in _artifacts().items():
            with self.subTest(artifact=name):
                self.assertNotEqual(
                    find_secrets(artifact, secrets=SECRETS),
                    [],
                    f"{name} 的样本未触发扫描器，测试将失去意义",
                )


class GuardPayloadTests(unittest.TestCase):
    def test_five_artifact_classes_are_clean_after_guard(self) -> None:
        for name, artifact in _artifacts().items():
            with self.subTest(artifact=name):
                guarded = guard_payload(artifact, secrets=SECRETS, where=name)
                self.assertEqual(find_secrets(guarded, secrets=SECRETS), [])
                dumped = json.dumps(guarded, ensure_ascii=False, default=str)
                self.assertNotIn(CANARY, dumped)
                self.assertNotIn("open.feishu.cn", dumped)
                self.assertNotIn("sk-abcdefgh12345678", dumped)

    def test_guard_rejects_unknown_type(self) -> None:
        with self.assertRaises(RedactionError):
            guard_payload({"bad": object()}, secrets=SECRETS, where="checkpoint")

    def test_guard_where_appears_in_failure(self) -> None:
        try:
            guard_payload({"bad": object()}, secrets=SECRETS, where="telemetry")
        except RedactionError as exc:
            self.assertIn("telemetry", str(exc))
        else:
            self.fail("guard_payload 未对未知类型 fail-closed")

    def test_response_headers_are_cleaned(self) -> None:
        guarded = guard_payload(_artifacts()["api_response"], secrets=SECRETS, where="api")
        self.assertEqual(guarded["headers"]["Authorization"], "[REDACTED:Authorization]")
        self.assertEqual(guarded["headers"]["X-Api-Key"], "[REDACTED:X-Api-Key]")
        self.assertEqual(guarded["headers"]["Content-Type"], "application/json")
        self.assertEqual(find_secrets(guarded, secrets=SECRETS), [])

    def test_guard_is_idempotent(self) -> None:
        once = guard_payload(_artifacts()["trace"], secrets=SECRETS, where="trace")
        twice = guard_payload(once, secrets=SECRETS, where="trace")
        self.assertEqual(once, twice)


class ErrorSummaryTests(unittest.TestCase):
    def test_error_code_and_redaction(self) -> None:
        exc = RuntimeError('FEISHU_WEBHOOK_HTTP_ERROR:400:{"msg":"bad ' + CANARY + '"}')
        summary = safe_error_summary(exc, secrets=SECRETS)
        self.assertEqual(summary["error_type"], "RuntimeError")
        self.assertEqual(summary["error_code"], "FEISHU_WEBHOOK_HTTP_ERROR")
        self.assertNotIn(CANARY, summary["message"])

    def test_non_code_error_uses_class_name(self) -> None:
        summary = safe_error_summary(ValueError("普通错误"), secrets=SECRETS)
        self.assertEqual(summary["error_code"], "ValueError")

    def test_long_message_is_truncated(self) -> None:
        summary = safe_error_summary(RuntimeError("E:" + "z" * 5000), secrets=SECRETS)
        self.assertLessEqual(len(summary["message"]), 300 + len("…[truncated]"))

    def test_summary_is_json_serialisable(self) -> None:
        summary = safe_error_summary(RuntimeError("X:1"), secrets=SECRETS)
        self.assertEqual(set(summary), {"error_type", "error_code", "message"})
        json.dumps(summary)


class EnvironmentTests(unittest.TestCase):
    def test_collect_returns_values_without_duplicates(self) -> None:
        values = collect_secrets_from_environ({"OPENAI_API_KEY": CANARY, "PATH": "/usr/bin"})
        self.assertEqual(values, (CANARY,))

    def test_empty_values_are_ignored(self) -> None:
        self.assertEqual(collect_secrets_from_environ({"OPENAI_API_KEY": ""}), ())

    def test_presence_reports_only_present_or_absent(self) -> None:
        report = secret_env_presence({"FEISHU_WEBHOOK_URL": WEBHOOK})
        self.assertEqual(report["FEISHU_WEBHOOK_URL"], "present")
        self.assertEqual(report["OPENAI_API_KEY"], "absent")
        self.assertNotIn(WEBHOOK, json.dumps(report))

    def test_sensitive_key_names_are_normalised(self) -> None:
        self.assertTrue(is_sensitive_key("X-API-Key"))
        self.assertTrue(is_sensitive_key("webhookUrl"))
        self.assertFalse(is_sensitive_key("slot"))
        self.assertEqual(normalize_key("X-API-Key"), "xapikey")
        self.assertIn("webhookurl", SENSITIVE_KEY_NAMES)


class SecretLeakErrorTests(unittest.TestCase):
    def test_leak_error_is_a_redaction_error(self) -> None:
        self.assertTrue(issubclass(SecretLeakError, RedactionError))


if __name__ == "__main__":
    unittest.main()
