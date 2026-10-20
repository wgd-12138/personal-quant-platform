"""A5-01：多角色权限隔离测试。

测试要证明的核心命题只有一句：
角色的权限差异是代码里的硬约束，不是提示词里的劝告。
所以这里的断言全部落在「调用真的被拒」和「schema 真的看不到」，
而不是「返回文案里写了不允许」。

三个角色的边界：
- supervisor：能力集为空，连只读工具都看不到；
- analyst：只有 read_local / read_external；
- operator：只有 business_write / notify_external，且仍受 registry 的审批约束。
"""

from __future__ import annotations

import pathlib
import sys
import unittest
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from pq_agent.roles import (
    HANDOFF_MAX_TASK_CHARS,
    ROLE_ANALYST,
    ROLE_ERROR_CAPABILITY,
    ROLE_ERROR_FIELD,
    ROLE_ERROR_HANDOFF,
    ROLE_ERROR_UNKNOWN,
    ROLE_OPERATOR,
    ROLE_SUPERVISOR,
    ROLES,
    RoleError,
    allowed_tool_names,
    build_handoff,
    capabilities_for,
    context_digest,
    describe_roles,
    may_use_capability,
    reject_reason,
    role_definition,
    visible_schemas,
)
from pq_agent.roles import analyst, operator, supervisor
from pq_agent.roles.role_registry import RoleRegistry, all_role_views, role_registry
from pq_agent.tools.contracts import ToolError
from pq_agent.tools.quant_tools import build_default_registry


def _tmp_dir(name: str) -> pathlib.Path:
    """本机沙箱把 0o700 目录变成自己进不去的目录，所以不用 tempfile。"""

    path = ROOT / ".tmp" / "a5-01-tests" / (name + "-" + uuid.uuid4().hex[:8])
    path.mkdir(parents=True, exist_ok=True)
    return path


class _FakeProvider:
    """角色权限测试不需要真数据；这里刻意让它一被调用就抛错，
    这样任何「不该发生的取数」都会立刻暴露，而不是静默成功。"""

    def __init__(self) -> None:
        self.calls: list[object] = []

    def snapshot(self, as_of):
        self.calls.append(as_of)
        raise RuntimeError("角色测试不允许真的取数")


class _FakeSender:
    """记录每一次外发。断言 sender.calls 为空就是「未审批零副作用」的直接证据。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def send(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return {"ok": True}


# 注册表装齐后应当出现的工具（含 A5-02 补上的只读 report_search）。
# 用名字集合断言「装满了」，而不是断言「恰好这 8 个」：
# 断言总数会把「新增一个工具」误报成失败，而且失败信息说不清是漏装还是多装。
FULL_TOOL_NAMES = frozenset(
    {
        "daily_pack_preview",
        "market_snapshot",
        "policy_rules_read",
        "positions_snapshot",
        "push_preview",
        "push_send",
        "readiness_check",
        "report_search",
        "report_write",
    }
)


def _full_registry(name: str = "registry"):
    """装配一份满工具的注册表：缺少写/外发工具就测不出越权。

    build_default_registry 无参时只登记 3 个只读工具，
    只读角色看起来「权限正确」其实是工具根本没装——那种绿色是假的。
    """

    work = _tmp_dir(name)
    provider = _FakeProvider()
    sender = _FakeSender()
    registry = build_default_registry(
        provider=provider,
        reports_dir=work / "reports",
        runs_dir=work / "runs",
        sender=sender,
    )
    return registry, provider, sender, work

class RoleDefinitionTests(unittest.TestCase):
    """角色定义本身的自洽性。"""

    def test_three_roles_are_registered(self):
        self.assertEqual(tuple(ROLES), (ROLE_SUPERVISOR, ROLE_ANALYST, ROLE_OPERATOR))
        self.assertEqual([item["role"] for item in describe_roles()], list(ROLES))

    def test_capability_sets_do_not_overlap(self):
        """三个角色的能力集合必须互不重叠：
        重叠意味着同一个动作有两个角色都能做，追责时就说不清是谁做的。"""

        seen: set[str] = set()
        for role in ROLES:
            caps = set(capabilities_for(role))
            self.assertEqual(caps & seen, set(), f"{role} 与其它角色能力重叠：{caps & seen}")
            seen |= caps
        self.assertEqual(seen, {"read_local", "read_external", "business_write", "notify_external"})

    def test_supervisor_has_no_capability(self):
        self.assertEqual(capabilities_for(ROLE_SUPERVISOR), ())
        definition = role_definition(ROLE_SUPERVISOR)
        self.assertTrue(definition.read_only)
        self.assertFalse(definition.has_side_effects)

    def test_analyst_is_read_only(self):
        definition = role_definition(ROLE_ANALYST)
        self.assertEqual(set(definition.capabilities), {"read_local", "read_external"})
        self.assertTrue(definition.read_only)
        self.assertFalse(definition.has_side_effects)

    def test_operator_has_side_effects(self):
        definition = role_definition(ROLE_OPERATOR)
        self.assertEqual(set(definition.capabilities), {"business_write", "notify_external"})
        self.assertFalse(definition.read_only)
        self.assertTrue(definition.has_side_effects)

    def test_operator_excludes_control_write(self):
        """control_write（写控制态，如 push_preview 写 planning/runs）不给 operator，
        也不给 analyst：它既不是只读，也不是对外的业务动作，
        放进任一角色都会让「写盘」这件事在审批之外发生。"""

        for role in ROLES:
            self.assertNotIn("control_write", capabilities_for(role))

    def test_unknown_role_is_rejected(self):
        for bad in ("", "admin", "Supervisor", "root"):
            with self.assertRaises(RoleError) as ctx:
                role_definition(bad)
            self.assertEqual(ctx.exception.code, ROLE_ERROR_UNKNOWN)

    def test_unknown_capability_is_denied(self):
        self.assertFalse(may_use_capability(ROLE_ANALYST, "code_exec"))
        self.assertFalse(may_use_capability(ROLE_OPERATOR, "read_local"))
        self.assertFalse(may_use_capability(ROLE_SUPERVISOR, "read_local"))


class VisibilityTests(unittest.TestCase):
    """可见面：schema 拿不到的工具，模型连它存在都不知道。"""

    def setUp(self):
        self.registry, self.provider, self.sender, self.work = _full_registry("visible")

    def test_all_full_tools_are_registered(self):
        """先证明注册表是满的，否则下面的「analyst 看不到写工具」没有意义。"""

        self.assertEqual(set(self.registry.names()), set(FULL_TOOL_NAMES))

    def test_supervisor_sees_no_tool(self):
        self.assertEqual(allowed_tool_names(self.registry, ROLE_SUPERVISOR), [])
        self.assertEqual(visible_schemas(self.registry, ROLE_SUPERVISOR), [])

    def test_analyst_sees_only_read_tools(self):
        names = allowed_tool_names(self.registry, ROLE_ANALYST)
        self.assertEqual(
            names,
            [
                "daily_pack_preview",
                "market_snapshot",
                "policy_rules_read",
                "positions_snapshot",
                "readiness_check",
                "report_search",
            ],
        )
        for hidden in ("push_preview", "push_send", "report_write"):
            self.assertNotIn(hidden, names)

    def test_operator_sees_only_write_tools(self):
        names = allowed_tool_names(self.registry, ROLE_OPERATOR)
        self.assertEqual(names, ["push_send", "report_write"])
        for hidden in ("market_snapshot", "policy_rules_read", "readiness_check"):
            self.assertNotIn(hidden, names)

    def test_schemas_match_names(self):
        for role in ROLES:
            view = role_registry(self.registry, role)
            self.assertEqual(
                [item["name"] for item in view.schemas()],
                view.names(),
                f"{role} 的 schema 与工具名不一致",
            )

    def test_schema_filtering_is_by_capability_not_by_name(self):
        """新增一个只读工具时 analyst 必须自动看得见。

        这条是防止有人把角色权限写成硬编码名字列表：
        写死名字的那一天起，权限表就和注册表脱节了。
        """

        from pq_agent.tools.contracts import ToolSpec

        def handler(arguments, *, dry_run=True):
            return {"ok": True}

        registry = build_default_registry()
        registry.register(
            ToolSpec(
                name="brand_new_reader",
                capability="read_local",
                side_effects=(),
                args=(),
                returns={"type": "object"},
                timeout_s=5.0,
                idempotency="safe",
                risk="none",
                summary="测试用的新只读工具",
                handler=handler,
            )
        )
        self.assertIn("brand_new_reader", allowed_tool_names(registry, ROLE_ANALYST))
        self.assertNotIn("brand_new_reader", allowed_tool_names(registry, ROLE_OPERATOR))

class EnforcementTests(unittest.TestCase):
    """执行面：可见面之外必须再拦一次。schema 看不到不等于调用被拒。"""

    def setUp(self):
        self.registry, self.provider, self.sender, self.work = _full_registry("enforce")
        self.analyst_view = role_registry(self.registry, ROLE_ANALYST)
        self.operator_view = role_registry(self.registry, ROLE_OPERATOR)
        self.supervisor_view = role_registry(self.registry, ROLE_SUPERVISOR)

    def test_analyst_write_requests_are_rejected(self):
        # control_write（push_preview）不在这一组：它只需要人工确认、不写业务盘也不外发，
        # 角色视图会放行它去走审批（A6-07）。真正要证明「越权被拒」的是业务写入与外发。
        cases = (
            ("report_write", {"as_of": "2026-07-09"}, "business_write"),
            ("push_send", {"as_of": "2026-07-09", "slot": "evening"}, "notify_external"),
        )
        for tool, args, capability in cases:
            with self.subTest(tool=tool):
                with self.assertRaises(RoleError) as ctx:
                    self.analyst_view.invoke(tool, args, dry_run=True)
                self.assertEqual(ctx.exception.code, ROLE_ERROR_CAPABILITY)
                self.assertIn(capability, ctx.exception.detail)
                self.assertIn(tool, ctx.exception.detail)

    def test_analyst_get_write_tool_is_rejected(self):
        with self.assertRaises(RoleError) as ctx:
            self.analyst_view.get("report_write")
        self.assertEqual(ctx.exception.code, ROLE_ERROR_CAPABILITY)

    def test_analyst_preview_write_tool_is_rejected(self):
        """preview 也会返回 arguments_hash，所以要一起拦住：
        否则 analyst 能替 operator 生成审批令牌。"""

        with self.assertRaises(RoleError) as ctx:
            self.analyst_view.preview("report_write", {"as_of": "2026-07-09"})
        self.assertEqual(ctx.exception.code, ROLE_ERROR_CAPABILITY)

    def test_supervisor_cannot_invoke_anything(self):
        for tool, args in (("policy_rules_read", {}), ("market_snapshot", {"as_of": "2026-07-09"})):
            with self.subTest(tool=tool):
                with self.assertRaises(RoleError) as ctx:
                    self.supervisor_view.invoke(tool, args)
                self.assertEqual(ctx.exception.code, ROLE_ERROR_CAPABILITY)

    def test_operator_cannot_read(self):
        for tool in ("policy_rules_read", "positions_snapshot", "readiness_check"):
            with self.subTest(tool=tool):
                with self.assertRaises(RoleError) as ctx:
                    self.operator_view.invoke(tool, {})
                self.assertEqual(ctx.exception.code, ROLE_ERROR_CAPABILITY)
        with self.assertRaises(RoleError):
            self.operator_view.invoke("market_snapshot", {"as_of": "2026-07-09"})

    def test_role_check_happens_before_approval_check(self):
        """越权必须在审批之前被拒。

        顺序反了的话，一次越权尝试会先撞上「需要审批」的提示，
        看起来像是「只要批了就能做」，而真正的结论是「这个角色永远不能做」。
        """

        with self.assertRaises(RoleError) as ctx:
            self.analyst_view.invoke(
                "report_write", {"as_of": "2026-07-09"}, approval_token="whatever", dry_run=False
            )
        self.assertEqual(ctx.exception.code, ROLE_ERROR_CAPABILITY)

    def test_rejected_attempts_never_reach_the_call_log(self):
        """被角色层拦下的调用不能出现在真注册表的 call_log 里，
        否则事后翻日志会看到一条「工具被调用过」的记录。"""

        before = len(self.analyst_view.call_log())
        for _ in range(3):
            with self.assertRaises(RoleError):
                self.analyst_view.invoke("report_write", {"as_of": "2026-07-09"}, dry_run=False)
        self.assertEqual(len(self.analyst_view.call_log()), before)

    def test_operator_without_approval_has_zero_side_effects(self):
        """未审批时：业务目录零变化、sender 零调用。"""

        with self.assertRaises(ToolError) as ctx:
            self.operator_view.invoke("report_write", {"as_of": "2026-07-09"}, dry_run=False)
        self.assertEqual(ctx.exception.code, "TOOL_APPROVAL_REQUIRED")
        with self.assertRaises(ToolError):
            self.operator_view.invoke(
                "push_send", {"as_of": "2026-07-09", "slot": "evening"}, dry_run=False
            )
        self.assertEqual(self.sender.calls, [])
        self.assertEqual(list(self.work.rglob("*.md")), [])

    def test_operator_with_wrong_token_is_rejected(self):
        """审批令牌与参数绑定：换了参数就得重新确认，不能拿旧令牌执行新动作。"""

        with self.assertRaises(ToolError) as ctx:
            self.operator_view.invoke(
                "report_write", {"as_of": "2026-07-09"}, approval_token="deadbeef", dry_run=False
            )
        self.assertEqual(ctx.exception.code, "TOOL_APPROVAL_REQUIRED")
        self.assertEqual(self.sender.calls, [])

    def test_operator_with_correct_token_is_allowed_by_role_layer(self):
        """角色层只回答「能不能碰」；令牌正确与否由 registry 决定。

        这里用 dry_run=True，验证令牌链路能走通而不是真的写盘。
        """

        token = self.operator_view.preview("report_write", {"as_of": "2026-07-09"})["arguments_hash"]
        out = self.operator_view.invoke(
            "report_write", {"as_of": "2026-07-09"}, approval_token=token, dry_run=True
        )
        self.assertTrue(out.get("ok", True))
        self.assertEqual(list(self.work.rglob("*.md")), [])

    def test_views_are_independent(self):
        views = all_role_views(self.registry)
        self.assertEqual(set(views), set(ROLES))
        self.assertNotEqual(views[ROLE_ANALYST].names(), views[ROLE_OPERATOR].names())
        self.assertEqual(views[ROLE_SUPERVISOR].names(), [])
        for role, view in views.items():
            self.assertEqual(view.role, role)
            self.assertIs(view.registry, self.registry)

    def test_role_registry_rejects_unknown_role(self):
        with self.assertRaises(RoleError) as ctx:
            RoleRegistry(self.registry, "admin")
        self.assertEqual(ctx.exception.code, ROLE_ERROR_UNKNOWN)

class HandoffTests(unittest.TestCase):
    """交接包：只传摘要与结构，不传原文。"""

    def setUp(self):
        self.registry, self.provider, self.sender, self.work = _full_registry("handoff")

    def test_handoff_has_exactly_four_payload_fields(self):
        """契约里写死的四个字段就是全部业务字段，多一个都不给。"""

        handoff, _ = build_handoff(
            task="查一下今天大盘", context={"user_query": "今天大盘怎么样"}, registry=self.registry,
            to_role=ROLE_ANALYST,
        )
        payload = handoff.to_dict()
        business = set(payload) - {"from_role", "to_role", "schema_version"}
        self.assertEqual(business, {"task", "context_digest", "allowed_tools", "return_schema"})

    def test_handoff_carries_no_raw_context(self):
        """原文只用来算摘要，绝不进交接包。"""

        secret_text = "我的持仓是 000725 京东方Ａ，成本 4.12"
        handoff, receipt = build_handoff(
            task="对比持仓与大盘", context={"raw_notes": secret_text}, registry=self.registry,
            to_role=ROLE_ANALYST,
        )
        blob = str(handoff.to_dict()) + str(receipt.to_dict())
        self.assertNotIn(secret_text, blob)
        self.assertNotIn("000725", blob)
        self.assertNotIn("京东方", blob)
        self.assertTrue(handoff.context_digest.startswith("ck_"))

    def test_digest_is_stable_and_order_insensitive(self):
        a = context_digest({"b": 2, "a": 1})
        b = context_digest({"a": 1, "b": 2})
        self.assertEqual(a, b)
        self.assertNotEqual(a, context_digest({"a": 1, "b": 3}))

    def test_digest_does_not_leak_length(self):
        short = context_digest("x")
        long = context_digest("x" * 5000)
        self.assertEqual(len(short), len(long))

    def test_forbidden_fields_are_rejected(self):
        """黑名单覆盖原文、参数、秘密三类。"""

        for key in ("prompt", "raw_prompt", "messages", "conversation", "arguments", "positions", "webhook"):
            with self.subTest(key=key):
                with self.assertRaises(RoleError) as ctx:
                    build_handoff(
                        task="t", context="c", registry=self.registry, to_role=ROLE_ANALYST,
                        extra={key: "x"},
                    )
                self.assertEqual(ctx.exception.code, ROLE_ERROR_FIELD)

    def test_allowed_tools_follow_target_role(self):
        analyst_handoff, _ = build_handoff(
            task="读数据", context="c", registry=self.registry, to_role=ROLE_ANALYST,
        )
        operator_handoff, _ = build_handoff(
            task="发推送", context="c", registry=self.registry, to_role=ROLE_OPERATOR,
            from_role=ROLE_ANALYST,
        )
        self.assertIn("market_snapshot", analyst_handoff.allowed_tools)
        self.assertNotIn("report_write", analyst_handoff.allowed_tools)
        self.assertEqual(operator_handoff.allowed_tools, ("push_send", "report_write"))

    def test_handoff_rejects_oversized_task(self):
        with self.assertRaises(RoleError) as ctx:
            build_handoff(
                task="a" * (HANDOFF_MAX_TASK_CHARS + 1), context="c", registry=self.registry,
                to_role=ROLE_ANALYST,
            )
        self.assertEqual(ctx.exception.code, ROLE_ERROR_HANDOFF)

    def test_handoff_rejects_empty_task(self):
        for bad in ("", "   ", None, 123):
            with self.subTest(bad=repr(bad)):
                with self.assertRaises(RoleError):
                    build_handoff(task=bad, context="c", registry=self.registry, to_role=ROLE_ANALYST)

    def test_handoff_rejects_unknown_target_role(self):
        with self.assertRaises(RoleError) as ctx:
            build_handoff(task="t", context="c", registry=self.registry, to_role="admin")
        self.assertEqual(ctx.exception.code, ROLE_ERROR_UNKNOWN)

    def test_receipt_records_both_roles_and_digest(self):
        handoff, receipt = build_handoff(
            task="查今天大盘", context="c", registry=self.registry, to_role=ROLE_ANALYST,
            trace_id="tr-abc", created_at="2026-10-14T16:00:00+08:00",
        )
        self.assertEqual(receipt.from_role, ROLE_SUPERVISOR)
        self.assertEqual(receipt.to_role, ROLE_ANALYST)
        self.assertEqual(receipt.context_digest, handoff.context_digest)
        self.assertTrue(receipt.accepted)
        self.assertEqual(receipt.trace_id, "tr-abc")
        self.assertEqual(receipt.allowed_tools, tuple(handoff.allowed_tools))

    def test_reject_reason_names_role_tool_and_capability(self):
        text = reject_reason(ROLE_ANALYST, "report_write", capability="business_write")
        self.assertIn("analyst", text)
        self.assertIn("report_write", text)
        self.assertIn("business_write", text)
        self.assertIn("read_local", text)


if __name__ == "__main__":
    unittest.main()
