"""A6-07 positions_snapshot 的业务合同：只承诺真能返回的字段。

为什么要有这一组：工具以前把 returns 写成 {"type": "object"}，
等于什么都没承诺。模型看到「持仓快照」会去要成本、盈亏、市值，
拿不到就开始编——这是项目红线里明令禁止的事。
这里的断言分三层：
1. 声明：returns 里写明的只有 code / name；
2. 返回：真实调用返回的每条持仓也只有 code / name；
3. 反证：任何成本/盈亏字段一旦出现，都必须被这组断言抓出来。
"""

from __future__ import annotations

from dataclasses import dataclass

import unittest

from pq_agent.tools.quant_tools import (
    POSITIONS_ROW_FIELDS,
    POSITIONS_SNAPSHOT_RETURNS,
    POSITIONS_UNAVAILABLE_FIELDS,
    build_default_registry,
    make_positions_snapshot_spec,
)

# returns 合同里明确不承诺的字段。宁可少给，也不给一个看起来很像真的数字。
FORBIDDEN_POSITION_FIELDS = ("cost", "cost_price", "pnl", "profit", "profit_loss", "market_value", "shares", "quantity")


@dataclass
class _Holding:
    code: str
    name: str
    # 下面这两个字段是刻意加的：注入数据里就算带上了，工具也不许透出去。
    cost_price: float = 10.0
    pnl: float = 1.5


class DeclaredContractTests(unittest.TestCase):
    def test_returns_schema_lists_only_what_is_really_returned(self) -> None:
        returns = POSITIONS_SNAPSHOT_RETURNS
        self.assertEqual(returns["type"], "object")
        self.assertEqual(returns["additionalProperties"], False, "返回合同必须封闭，不允许出现未声明字段")
        self.assertEqual(set(returns["properties"]), {"count", "positions"})
        row = returns["properties"]["positions"]["items"]
        self.assertEqual(row["additionalProperties"], False)
        self.assertEqual(tuple(row["required"]), POSITIONS_ROW_FIELDS)
        self.assertEqual(set(row["properties"]), {"code", "name"})

    def test_unavailable_fields_are_named_explicitly(self) -> None:
        """「没有」要写出来，而不是靠读者猜。"""
        for name in POSITIONS_UNAVAILABLE_FIELDS:
            self.assertNotIn(name, POSITIONS_SNAPSHOT_RETURNS["properties"])
            self.assertNotIn(name, POSITIONS_SNAPSHOT_RETURNS["properties"]["positions"]["items"]["properties"])

    def test_registered_spec_carries_the_same_contract(self) -> None:
        registry = build_default_registry()
        spec = registry.get("positions_snapshot")
        self.assertEqual(spec.returns, POSITIONS_SNAPSHOT_RETURNS)
        # 以前这里是 {"type": "object"}：等价于不承诺任何字段。这个断言就是那条修复。
        self.assertNotEqual(spec.returns, {"type": "object"})
        self.assertIn("不提供成本与盈亏", spec.summary)


class ReturnedValueTests(unittest.TestCase):
    def _invoke(self, positions):
        registry = build_default_registry(positions=positions)
        return registry.invoke("positions_snapshot", {}, dry_run=True)

    def test_rows_contain_only_code_and_name(self) -> None:
        payload = self._invoke([_Holding("600519", "贵州茅台")])
        self.assertTrue(payload["ok"])
        rows = payload["data"]["positions"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0]), set(POSITIONS_ROW_FIELDS))
        self.assertEqual(rows[0]["code"], "600519")
        self.assertEqual(rows[0]["name"], "贵州茅台")

    def test_injected_cost_and_pnl_are_never_exposed(self) -> None:
        """注入对象带着成本与盈亏，工具也不许透出去 —— 透出去就等于承诺了能算盈亏。"""
        payload = self._invoke([_Holding("000001", "平安银行", cost_price=8.8, pnl=12.3)])
        blob = str(payload["data"])
        for name in FORBIDDEN_POSITION_FIELDS:
            self.assertNotIn(name, blob, "返回里出现了未承诺字段 " + name)

    def test_returned_keys_match_the_declared_schema_exactly(self) -> None:
        """逐个持仓过一遍声明的 schema：多一个键就算违约。"""
        from pq_agent.contracts import validate_against_schema

        payload = self._invoke([_Holding("600036", "招商银行"), _Holding("601318", "中国平安")])
        validate_against_schema(payload["data"], POSITIONS_SNAPSHOT_RETURNS, "positions_snapshot.data")
        for row in payload["data"]["positions"]:
            validate_against_schema(
                row,
                POSITIONS_SNAPSHOT_RETURNS["properties"]["positions"]["items"],
                "positions_snapshot.data.positions[]",
            )
        self.assertEqual(payload["data"]["count"], 2)

    def test_empty_positions_is_zero_not_a_fake_number(self) -> None:
        payload = self._invoke([])
        self.assertEqual(payload["data"], {"count": 0, "positions": []})
        self.assertIn("0", payload["summary"])

    def test_missing_fields_are_empty_strings_not_invented(self) -> None:
        """只有代码、没有名字时：名字留空，不拿代码去冒充名称。"""

        @dataclass
        class Bare:
            code: str = "600000"

        payload = self._invoke([Bare()])
        row = payload["data"]["positions"][0]
        self.assertEqual(row, {"code": "600000", "name": ""})

    def test_alternative_code_attribute_is_accepted(self) -> None:
        """stock_code 是另一种真实存在的字段名，要认；但仍然只输出 code/name。"""

        @dataclass
        class Alt:
            stock_code: str = "300750"
            name: str = "宁德时代"

        payload = self._invoke([Alt()])
        self.assertEqual(payload["data"]["positions"], [{"code": "300750", "name": "宁德时代"}])

    def test_spec_can_be_built_standalone(self) -> None:
        spec = make_positions_snapshot_spec(positions=())
        self.assertEqual(spec.returns, POSITIONS_SNAPSHOT_RETURNS)
        self.assertEqual(spec.arg_names, (), "positions_snapshot 不该接受参数：路径与数据都由调用方注入")


if __name__ == "__main__":
    unittest.main()
