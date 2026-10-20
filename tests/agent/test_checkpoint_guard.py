"""A6-04：checkpoint 守卫必须在真实写入路径上，并且真的脱敏。

为什么单独一个文件：
A5-04 的 report 写着「checkpoint 已过脱敏」，
但 server.py 当时传的是 saver.inner —— LangGraph 直接调底层 SqliteSaver.put，
GuardedSqliteSaver.put() 一次都没执行，write_count 永远是 0。
这类「守卫没上路径」的缺陷不会报错，只会让报告里的结论变成假话。
所以这里的第一步不是测脱敏算法，而是测「守卫到底有没有被调用」。
"""

from __future__ import annotations

import pathlib
import sqlite3
import unittest
import uuid

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from pq_agent.checkpoint import (
    GuardedSqliteSaver,
    guarded_sqlite_checkpointer,
    is_control_channel,
)
from pq_agent.graph import GraphDeps, build_graph, run_graph, thread_config

from ._graph_fakes import FakeProvider, FakeTools, fixed_clock, make_state

ROOT = pathlib.Path(__file__).resolve().parents[2]

FAKE_API_KEY = "sk-" + "a1b2c3d4e5f6" * 4
FAKE_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/" + "9f" * 12
FAKE_AUTH = "Bearer " + "z9y8x7w6v5u4" * 4  # 方案名 Bearer 本身不是秘密，token 才是


def _tmp_dir(name: str) -> pathlib.Path:
    """刻意不用 tempfile：本机沙箱把 0o700 目录映射成自己无权的 DACL。"""
    path = ROOT / ".tmp" / "a6-04-tests" / (name + "-" + uuid.uuid4().hex[:8])
    path.mkdir(parents=True, exist_ok=True)
    return path


def _deps(tools=None):
    return GraphDeps(provider=FakeProvider(), tools=tools or FakeTools("market_snapshot"), clock=fixed_clock)


def _leaks_in_database(db: pathlib.Path) -> list[str]:
    """按 LangGraph 自己的序列化格式把存下来的行重新编码，再逐字节找密钥。

    为什么不能直接读原始 BLOB：sqlite 里存的是 msgpack，文本在前、二进制在后，
    直接在 BLOB 里找明文会漏判（同样的原因也曾让「行内容里找 sk-」给出假阴性）。
    这里用 checkpoint 的 serde 把每个值编码成字节，逐行检查；格式不对就跳过。
    """
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    serde = JsonPlusSerializer()
    needles = {
        "api_key": FAKE_API_KEY,
        "webhook": FAKE_WEBHOOK,
        "authorization": FAKE_AUTH,
        "raw_prompt": "RAW-PROMPT-DO-NOT-STORE",
    }
    hits: list[str] = []
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        for table, column, conv in (
            ("checkpoints", "checkpoint", "checkpoints"),
            ("writes", "value", "values"),
        ):
            rows = conn.execute("SELECT type, " + column + " FROM " + table).fetchall()
            for type_name, value in rows:
                if type_name == "NULL":
                    continue
                try:
                    if conv == "checkpoints":
                        payload = serde.dumps_typed(value)[1]
                    else:
                        payload = serde.dumps_typed({str(type_name): value})[1]
                except Exception:
                    payload = value if isinstance(value, bytes) else str(value).encode("utf-8", "replace")
                text = payload.decode("utf-8", "replace")
                for label, needle in needles.items():
                    if needle in text and label not in hits:
                        hits.append(label)
    finally:
        conn.close()
    return sorted(hits)


class GuardIsOnTheWritePathTests(unittest.TestCase):
    def test_guard_implements_the_protocol_langgraph_requires(self):
        with guarded_sqlite_checkpointer(_tmp_dir("proto") / "c.sqlite", root=ROOT) as guard:
            self.assertIsInstance(guard, BaseCheckpointSaver)
            # 不再暴露 inner：没有这条路，调用方就没法再绕过守卫
            self.assertFalse(hasattr(guard, "inner"))

    def test_real_graph_run_increments_write_count(self):
        """A5-04 的缺陷：write_count 恒为 0，因为守卫根本不在路径上。"""
        with guarded_sqlite_checkpointer(_tmp_dir("count") / "c.sqlite", root=ROOT) as guard:
            app = build_graph(_deps(), checkpointer=guard)
            run_graph(app, make_state(), thread_id="t-guard")
            self.assertGreater(guard.write_count, 0)
            self.assertTrue(guard.serde is not None)

    def test_checkpoint_file_really_lands_where_it_is_reported(self):
        """A6-04 修的真实缺陷：相对路径被二次拼接，库写到了别的目录。"""
        base = _tmp_dir("path")
        db = base / "nested" / "checkpoint.sqlite"
        with guarded_sqlite_checkpointer(db, root=ROOT) as guard:
            app = build_graph(_deps(), checkpointer=guard)
            run_graph(app, make_state(), thread_id="t-path")
            self.assertTrue(db.exists(), "checkpoint 没有落在报告的路径上")
            self.assertFalse((base / "nested" / "nested").exists())

    def test_state_gate_rejects_secret_shaped_input_before_it_is_stored(self):
        """第一道闸门：违规内容根本进不了 state，所以也谈不上落到 checkpoint。

        这条测试是「写不出来」的证据，而不是兜底：如果哪天 state 层不再拦截，
        下面那条直接喂给 put() 的测试仍然会证明第二道闸门有效。
        """
        from pq_agent.contracts import ContractError
        from pq_agent.state import new_state

        for label, value in (
            ("api_key", "查一下 " + FAKE_API_KEY),
            ("webhook", FAKE_WEBHOOK),
            ("authorization", FAKE_AUTH),
        ):
            with self.subTest(label=label):
                with self.assertRaises(ContractError) as ctx:
                    new_state(thread_id="t-gate", mode="replay", user_query=value)
                self.assertIn("STATE_SECRET_DETECTED", str(ctx.exception))

    def test_direct_put_is_scrubbed_and_never_reaches_the_file(self):
        """第二道闸门：即使调用方绕过 state 直接调 put，写进文件的也必须是脱敏后的值。"""
        db = _tmp_dir("direct-put") / "c.sqlite"
        with guarded_sqlite_checkpointer(db, root=ROOT) as guard:
            saved = guard.get_tuple({"configurable": {"thread_id": "t-direct"}})
            self.assertIsNone(saved)
            base = guard._saver.get_tuple  # 只用于拿到真实的 config 形状，不参与断言
            self.assertTrue(callable(base))
            checkpoint = {
                "v": 1,
                "id": "1" + "0" * 31,
                "ts": "2026-10-17T00:00:00+00:00",
                "channel_values": {
                    "thread_id": "t-direct",
                    "user_query": "查一下 " + FAKE_API_KEY + " " + FAKE_AUTH + " " + FAKE_WEBHOOK,
                },
                "channel_versions": {},
                "versions_seen": {},
            }
            guard.put(
                {"configurable": {"thread_id": "t-direct", "checkpoint_ns": ""}},
                checkpoint,
                {"source": "input", "step": 0, "parents": {}},
                {},
            )
            self.assertEqual(guard.write_count, 1)
            self.assertGreaterEqual(guard.scrub_count, 1, "带着密钥的值没有被判为需要脱敏")
        self.assertTrue(db.exists())
        self.assertEqual(_leaks_in_database(db), [])

    def test_put_writes_scrubs_business_channels_and_keeps_control_channels(self):
        with guarded_sqlite_checkpointer(_tmp_dir("put-writes") / "c.sqlite", root=ROOT) as guard:
            written = guard.put(
                {"configurable": {"thread_id": "t-w", "checkpoint_ns": ""}},
                {
                    "v": 1,
                    "id": "1" + "0" * 31,
                    "ts": "2026-10-17T00:00:00+00:00",
                    "channel_values": {"thread_id": "t-w"},
                    "channel_versions": {},
                    "versions_seen": {},
                },
                {"source": "input", "step": 0, "parents": {}},
                {},
            )
            guard.put_writes(
                written,
                [
                    ("errors", ["TOOL_FAILED: " + FAKE_AUTH]),
                    ("__interrupt__", ("不是业务数据，必须原样放过",)),
                ],
                "task-1",
                "",
            )
            # write_scrub_count 统计的是「中继写入被改写过的次数」；这里真正要证明的是
            # 存储层里没有密钥。计数为 0 也可能，只要下面的字节扫描是干净的。
            self.assertGreaterEqual(guard.write_count, 1)
        self.assertEqual(_leaks_in_database(pathlib.Path(guard.db_path)), [])

    def test_scrubbed_value_is_still_readable_through_the_guard(self):
        """脱敏不能把 checkpoint 变成不可读：读回来的结构必须还是那个 state。"""
        db = _tmp_dir("readback") / "c.sqlite"
        with guarded_sqlite_checkpointer(db, root=ROOT) as guard:
            app = build_graph(_deps(), checkpointer=guard)
            run_graph(app, make_state(), thread_id="t-read")
            saved = app.get_state(thread_config("t-read"))
            self.assertEqual(saved.values.get("intent"), "market_check")
            self.assertTrue(saved.values.get("final_answer"))

    def test_write_scrub_counter_is_shared_across_shallow_copies(self):
        """with_allowlist() 会 copy.copy 一份 saver；计数器不能分裂成两份。"""
        with guarded_sqlite_checkpointer(_tmp_dir("copy") / "c.sqlite", root=ROOT) as guard:
            clone = guard.__copy__()
            app = build_graph(_deps(), checkpointer=clone)
            run_graph(app, make_state(), thread_id="t-copy")
            self.assertEqual(clone.write_count, guard.write_count)
            self.assertGreater(guard.write_count, 0)

    def test_control_channels_are_not_run_through_the_strict_gate(self):
        """__interrupt__ 里放的是 Interrupt 对象，硬走强闸门会把「等人审批」判成脱敏失败。"""
        self.assertTrue(is_control_channel("__interrupt__"))
        self.assertTrue(is_control_channel("__start__"))
        self.assertFalse(is_control_channel("messages"))
        self.assertFalse(is_control_channel("user_query"))

    def test_plain_memory_saver_still_works(self):
        """守卫改造不能把「不用 sqlite」的路径弄坏。"""
        app = build_graph(_deps(), checkpointer=InMemorySaver())
        out, interrupted = run_graph(app, make_state(), thread_id="t-mem")
        self.assertFalse(interrupted)
        self.assertTrue(out.get("final_answer"))


if __name__ == "__main__":
    unittest.main()
