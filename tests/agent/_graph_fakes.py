"""帮助函数：给图测试用的假 provider / 假工具 / 固定时钟。

注意：这些 fake 只用于图的路由与预算逻辑测试，不用于验证数据层——
数据层的真实性由 A0-03 的 ReplayProvider + 密封 fixture 保证。
这里刻意让 provider 直接返回一个固定 snapshot，避免图测试依赖 fixture 细节。
"""

from __future__ import annotations

import datetime as dt

FIXED_NOW = dt.datetime(2026, 7, 9, 16, 30, tzinfo=dt.timezone(dt.timedelta(hours=8)))


class FakeProvenance:
    def __init__(self, as_of="2026-07-09"):
        self.source = "fixture:demo"
        self.demo = True
        self.as_of = as_of
        self.is_replay = True
        self.stale = False
        self.degraded = False


class FakeSnapshot:
    def __init__(self, as_of="2026-07-09"):
        self.data_snapshot_id = "replay:" + as_of + ":deadbeefcafe"
        self.provenance = FakeProvenance(as_of)
        self.dataset = None


class FakeProvider:
    """按 as_of 返回固定 snapshot；fail=True 用于制造取数失败。"""

    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = []

    def snapshot(self, as_of):
        self.calls.append(as_of)
        if self.fail:
            raise RuntimeError("REPLAY_FIXTURE_MISSING: 没有这个日期的数据")
        return FakeSnapshot(as_of.isoformat())


class FakeTool:
    """可注入行为的假工具。calls 记录每次执行，便于断言审批前零副作用。"""

    def __init__(self, name, *, result=None, raises=None):
        self.name = name
        self.calls = []
        self._result = result
        self._raises = raises

    def run(self, arguments, dry_run=True):
        self.calls.append({"arguments": dict(arguments), "dry_run": dry_run})
        if self._raises is not None:
            raise self._raises
        if self._result is not None:
            return self._result
        return {"ok": True, "summary": self.name + " 完成"}


class FakeTools:
    def __init__(self, *names):
        self._tools = {}
        for name in names:
            self._tools[name] = FakeTool(name)

    def add(self, tool):
        self._tools[tool.name] = tool
        return tool

    def names(self):
        return list(self._tools)

    def get(self, name):
        return self._tools[name]

    def preview(self, name, arguments=None):
        """与真 registry 对齐：图只用 preview 拿参数哈希，不执行任何东西。"""
        self.get(name)
        return {"tool": name, "arguments_hash": "fake-" + name}

    def invoke(self, name, arguments=None, *, approval_token=None, dry_run=True):
        """与真 registry 对齐：真实现会在这里校验 approval_token，fake 只转发。"""
        return self.get(name).run(dict(arguments or {}), dry_run=dry_run)

    def calls(self, name):
        return self._tools[name].calls


def fixed_clock():
    return FIXED_NOW


def make_state(**overrides):
    from pq_agent.state import new_state

    params = dict(thread_id="t-graph", mode="replay", as_of="2026-07-09", user_query="今天大盘怎么样")
    params.update(overrides)
    return new_state(**params)


def plain_plan(*calls):
    """把 (tool_name, capability) 列表变成 ToolCall 字典列表。"""
    out = []
    for index, item in enumerate(calls):
        name, capability = item
        out.append({
            "call_id": "c" + str(index),
            "tool_name": name,
            "arguments": {},
            "capability": capability,
            "dry_run": True,
            "schema_version": 1,
        })
    return out
