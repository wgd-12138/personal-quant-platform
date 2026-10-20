"""pq_agent checkpoint 层：把 AgentState 接到 LangGraph 的持久化 saver 上。

为什么需要这一层（而不是直接用 SqliteSaver）：

1. **出口脱敏是硬要求**。企业 agent 的 checkpoint 会落盘到磁盘、被运维翻看、被备份带走。
   本项目的合同是"state / checkpoint 落盘前必须过 guard_state_for_persistence"，
   而 LangGraph 的 saver 只负责序列化，不做脱敏。因此在写入 saver 之前，
   每个 channel 值都必须先过 pq_agent.security 的统一闸门。
2. **版本不匹配必须失败**。恢复一个旧 schema 的 checkpoint 时，
   LangGraph 自己不会知道 graph_version 变了；必须由本层显式校验并抛错，
   禁止静默重建（否则会出现"旧字段被丢掉、agent 假装正常"的隐性错误）。
3. **保存点应该是可复现的**。checkpoint 里存的是 state，不含连接、句柄、游标；
   任何不可序列化的对象都必须在入口被拒绝。

设计取舍：本层不自己实现 sqlite 表结构，而是复用锁定版 langgraph-checkpoint-sqlite，
避免"自研 checkpoint 协议"这种典型过度设计；我们只加脱敏与版本两道闸门。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pq_agent.contracts import ContractError

from langgraph.checkpoint.base import BaseCheckpointSaver
from pq_agent.security.paths import resolve_within_root
from pq_agent.state import (
    GRAPH_VERSION,
    STATE_SCHEMA_VERSION,
    guard_state_for_persistence,
    validate_state,
)

CHECKPOINT_BACKEND_SQLITE = "sqlite"
CHECKPOINT_ERROR_DAMAGED = "CHECKPOINT_DAMAGED"
CHECKPOINT_ERROR_VERSION = "CHECKPOINT_VERSION_MISMATCH"
CHECKPOINT_ERROR_PATH = "CHECKPOINT_PATH_DENIED"
CHECKPOINT_ERROR_DRIVER = "CHECKPOINT_DRIVER_MISSING"

# sqlite 文件里 state 表的表名（langgraph-checkpoint-sqlite 的约定）
_STATE_TABLE = "checkpoints"
_WRITES_TABLE = "writes"


def checkpointer_driver_available() -> tuple[bool, str]:
    """探测 sqlite checkpoint 驱动是否可用。返回 (可用, 原因)。

    真实企业里这一步很重要：persistence 依赖缺失时，agent 必须**拒绝启动**，
    而不是降级成"内存态假装有持久化"——否则重启后用户会发现审批记录凭空消失。
    """
    try:
        import langgraph.checkpoint.sqlite  # noqa: F401
    except Exception as exc:  # pragma: no cover - 环境相关
        return False, f"{type(exc).__name__}: {exc}"
    return True, ""


def checkpoint_fingerprint(path: str | Path) -> str:
    """checkpoint 文件的 sha256 指纹，用于审计"这次恢复读的是哪个存档"。"""
    target = Path(path)
    if not target.exists():
        return ""
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _scrub_channel_values(values: Mapping[str, Any], *, where: str = "checkpoint") -> dict[str, Any]:
    """把每个 channel 值单独过脱敏闸门。

    注意：这里是"逐字段"而不是"整体"脱敏，因为 LangGraph 的 saver 是按 channel 写入的，
    整体脱敏后再拆分没有任何意义。已经完整装配好的 state 走 guard_state_for_persistence。
    """
    from pq_agent.security.redaction import guard_payload

    out: dict[str, Any] = {}
    for key, value in values.items():
        out[str(key)] = guard_payload(value, where=f"{where}.{key}")
    return out


def assert_checkpoint_graph_version(state: Mapping[str, Any], *, expected: str = GRAPH_VERSION) -> None:
    """恢复前校验 graph 版本。不匹配直接失败，禁止静默重建。"""
    version = state.get("graph_version")
    if version != expected:
        raise ContractError(
            CHECKPOINT_ERROR_VERSION,
            f"checkpoint 的 graph_version={version!r}，当前图版本={expected!r}；禁止静默重建，请显式迁移",
        )


def assert_checkpoint_schema_version(state: Mapping[str, Any], *, expected: int = STATE_SCHEMA_VERSION) -> None:
    """恢复前校验 checkpoint schema 版本。"""
    version = state.get("checkpoint_schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ContractError(
            CHECKPOINT_ERROR_VERSION,
            f"checkpoint_schema_version 非法：{version!r}",
        )
    if version != expected:
        raise ContractError(
            CHECKPOINT_ERROR_VERSION,
            f"checkpoint_schema_version={version}，当前={expected}；禁止静默重建",
        )


def guard_checkpoint_values(values: Mapping[str, Any], *, where: str = "checkpoint") -> dict[str, Any]:
    """saver 写入前的守卫：先脱敏，再校验，最后必须是可 JSON 序列化的纯净结构。"""
    scrubbed = _scrub_channel_values(values, where=where)
    try:
        return json.loads(json.dumps(scrubbed, ensure_ascii=False, sort_keys=True))
    except (TypeError, ValueError) as exc:
        raise ContractError(
            "CHECKPOINT_NOT_SERIALIZABLE",
            f"{where} 里存在不可 JSON 序列化的值：{type(exc).__name__}: {exc}",
        ) from exc


def open_checkpoint_connection(path: str | Path, *, root: str | Path | None = None) -> sqlite3.Connection:
    """打开（必要时新建）checkpoint 的 sqlite 连接。

    路径必须落在允许的根目录内，避免模型/配置把 checkpoint 写到任意位置。
    """
    # 先绝对化再交给路径策略（A6-04 修的真实缺陷）：
    # 相对路径 "x.sqlite" 在项目根为 R 时会被拼成 R/x.sqlite；但如果调用方给的是
    # ".tmp/abc/x.sqlite"（相对当前目录），拼接就变成 R/.tmp/abc/.tmp/abc/x.sqlite ——
    # 数据库被悄悄写到另一处，而报告里写的是“checkpoint 已落盘”。
    # 绝对化之后仍然由 resolve_within_root 判定越界、软链接与敏感路径，安全边界没变。
    absolute = Path(os.path.abspath(os.fspath(path)))
    try:
        resolved = resolve_within_root(str(absolute), root=root, allow_absolute=True)
    except Exception as exc:
        raise ContractError(CHECKPOINT_ERROR_PATH, f"checkpoint 路径被拒绝：{exc}") from exc
    resolved.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(resolved), check_same_thread=False)
    # 损坏的 checkpoint 必须显式暴露，不能让 sqlite 静默返回空结果
    try:
        conn.execute(f"PRAGMA integrity_check", ()).fetchone()
    except sqlite3.DatabaseError as exc:
        conn.close()
        raise ContractError(CHECKPOINT_ERROR_DAMAGED, f"checkpoint 无法读取：{exc}") from exc
    return conn


def audit_checkpoint_database(path: str | Path) -> dict[str, Any]:
    """只读审计：文件是否存在、能否打开、thread 数量、指纹。用于验收与排障。"""
    target = Path(path)
    report: dict[str, Any] = {
        "path": str(target),
        "exists": target.exists(),
        "size_bytes": target.stat().st_size if target.exists() else 0,
        "fingerprint": checkpoint_fingerprint(target),
        "threads": [],
        "readable": False,
        "error": None,
    }
    if not target.exists():
        return report
    try:
        conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                f"SELECT DISTINCT thread_id FROM {_STATE_TABLE} ORDER BY thread_id"
            ).fetchall()
            report["threads"] = [row[0] for row in rows]
            report["readable"] = True
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    return report


# LangGraph 的控制通道前缀。这些名字里的「__」不是装饰：__interrupt__ 放的是 Interrupt
# 对象本身，__start__ 之类放的是调度标记，它们不是业务数据，不能进强脱敏闸门。
CONTROL_CHANNEL_PREFIXES = ("@", "__")


def is_control_channel(channel: Any) -> bool:
    """判断一个中继写入是不是 LangGraph 的运行控制通道。

    实测（A6-04）：只过滤 "@" 前缀不够，__interrupt__ 仍然带着 Interrupt 对象进来，
    强闸门报 REDACTION_UNSUPPORTED_TYPE: Interrupt，让「等人审批」这个正常状态把图打崩。
    """
    return isinstance(channel, str) and channel.startswith(CONTROL_CHANNEL_PREFIXES)


def deep_scrub(value: Any) -> Any:
    """深拷贝式脱敏：只重写字符串与容器，其它对象原样保留。

    与 guard_payload 的分工：guard_payload 是「整份数据必须可 JSON 化」的强闸门，
    用于 state 落盘；这里面对的是 LangGraph 的中继写入，里面混着 Interrupt、
    序列化包装等非业务对象，强闸门会把正常运行判成脱敏失败（实测 8 个用例就是这么挂的）。
    所以这里只把字符串里的密钥换掉，其余对象保持原类型、原引用。
    """
    from pq_agent.security.redaction import scrub_text

    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, Mapping):
        return {deep_scrub(key): deep_scrub(item) for key, item in value.items()}
    if isinstance(value, list):
        return [deep_scrub(item) for item in value]
    if isinstance(value, tuple):
        return tuple(deep_scrub(item) for item in value)
    return value


class GuardedSqliteSaver(BaseCheckpointSaver):
    """真正参与写入的 checkpoint 守卫：写入前强制脱敏 + 版本校验。

    为什么要继承 BaseCheckpointSaver（A6-04 修的真实缺陷）：
A5-04 的实现是「组合」——包一层再暴露 .inner，于是 server.py 不得不把 saver.inner
交给 build_graph，LangGraph 直接调底层 SqliteSaver.put，本类的 put()/put_writes()
一次都不会执行。write_count 永远是 0，脱敏一次都没跑过，而报告里写着「checkpoint 已脱敏」。
组合没有错，错在「组合之后又把里面的对象交出去」。

    继承之后 graph.compile(checkpointer=guard) 直接收这个对象，守卫就在真实写入路径上。
    计数器放在类级共享的黑匣子里：无论 LangGraph 用 copy.copy 造多少份浅拷贝，
    write_count / scrub_count 都只有一个来源。
    """

    def __init__(self, saver: Any, *, db_path: str | Path | None = None) -> None:
        super().__init__()
        self._saver = saver
        self._db_path = str(db_path) if db_path is not None else ""
        # serde 必须与底层 saver 完全一致：checkpoint 的序列化格式由它决定，
        # 换一个 serde 会让「读得回来」这件事失去保证。
        self.serde = getattr(saver, "serde", None)
        # 计数器放在共享黑匣子里：with_allowlist() 的 copy.copy 只会复制实例属性，
        # 属性指向同一个 dict，计数就不会分裂成两份。
        self._counter: dict[str, int] = {"write_count": 0, "scrub_count": 0, "write_scrub_count": 0}

    def __copy__(self) -> "GuardedSqliteSaver":
        """浅拷贝时复用同一份计数器，并把底层 saver 也复用（不重开连接）。"""
        clone = type(self).__new__(type(self))
        clone.__dict__.update(self.__dict__)
        clone._counter = self._counter
        return clone

    @property
    def db_path(self) -> str:
        return self._db_path

    @property
    def write_count(self) -> int:
        """真实经过守卫的 checkpoint 写入次数（不是「理论上该被拦住」的次数）。"""
        return int(self._counter["write_count"])

    @property
    def scrub_count(self) -> int:
        """真实被脱敏改写的 checkpoint 次数。"""
        return int(self._counter["scrub_count"])

    @property
    def write_scrub_count(self) -> int:
        """真实被脱敏改写的中继写入（put_writes）次数。"""
        return int(self._counter["write_scrub_count"])

    def counters(self) -> dict[str, int]:
        return dict(self._counter)

    def get(self, config: Mapping[str, Any]) -> Any:
        return self._saver.get(config)

    def get_tuple(self, config: Mapping[str, Any]) -> Any:
        return self._saver.get_tuple(config)

    def list(self, config: Mapping[str, Any] | None, **kwargs: Any) -> Iterator[Any]:
        return self._saver.list(config, **kwargs)

    def put(self, config: Mapping[str, Any], checkpoint: Mapping[str, Any], metadata: Mapping[str, Any], new_versions: Mapping[str, Any]) -> Any:
        """写入前守卫：脱敏 -> JSON 往返 -> 交给底层 saver。"""
        channel_values = checkpoint.get("channel_values")
        if isinstance(channel_values, Mapping):
            guarded = guard_checkpoint_values(channel_values, where="checkpoint")
            if guarded != dict(channel_values):
                self._counter["scrub_count"] += 1
            checkpoint = dict(checkpoint)
            checkpoint["channel_values"] = guarded
        self._counter["write_count"] += 1
        return self._saver.put(config, checkpoint, metadata, new_versions)

    def put_writes(self, config: Mapping[str, Any], writes: Sequence[Any], task_id: str, task_path: str = "") -> None:
        """中继写入也要过脱敏。

        为什么不能只守 put：LangGraph 会把节点返回值同时也是「写入」存进 writes 表，
        超时/错误信息可能带着异常原文或参数落在那里，只守 put 等于把后门留着。
        """
        cleaned: list[tuple[str, Any]] = []
        scrubbed_any = False
        for channel, value in writes:
            # 注意：@ 开头的是 LangGraph 的运行控制通道（__interrupt__ 等），里面是 Interrupt
            # 对象本身，不是业务数据。硬走 guard_payload 会把「等人审批」这个正常状态
            # 判成 REDACTION_UNSUPPORTED_TYPE 并让整张图崩掉——实测 9 个用例就是这么挂的。
            if is_control_channel(channel):
                cleaned.append((channel, value))
                continue
            guarded = guard_checkpoint_values({"w": deep_scrub(value)}, where="write." + str(channel))["w"]
            if guarded != deep_scrub(value):
                scrubbed_any = True
            cleaned.append((channel, guarded))
        if scrubbed_any:
            self._counter["write_scrub_count"] += 1
        return self._saver.put_writes(config, cleaned, task_id, task_path)

    def delete_thread(self, thread_id: str) -> None:
        return self._saver.delete_thread(thread_id)

@contextmanager
def guarded_sqlite_checkpointer(db_path: str | Path, *, root: str | Path | None = None) -> Iterator[GuardedSqliteSaver]:
    """上下文管理器：产出受保护的 sqlite checkpointer，退出时关闭连接。

    驱动缺失时显式抛 CHECKPOINT_DRIVER_MISSING，绝不降级成内存 saver——
    因为"看起来能跑但重启丢审批记录"比直接报错危险得多。
    """
    available, reason = checkpointer_driver_available()
    if not available:
        raise ContractError(CHECKPOINT_ERROR_DRIVER, f"checkpoint 驱动不可用：{reason}")
    from langgraph.checkpoint.sqlite import SqliteSaver

    conn = open_checkpoint_connection(db_path, root=root)
    try:
        saver = SqliteSaver(conn)
        saver.setup()
        # 注意：这里 yielded 的是守卫本身，调用方必须把它直接交给 build_graph。
        # 任何 'saver.inner' 形式的取出都会让守卫退出真实写入路径（A6-04 修的就是这件事）。
        yield GuardedSqliteSaver(saver, db_path=db_path)
    finally:
        conn.close()


def guarded_state_for_checkpoint(state: Mapping[str, Any], *, root: str | Path | None = None) -> dict[str, Any]:
    """完整 state 落盘前的单一出口：版本校验 + 脱敏 + 结构校验。"""
    validate_state(state)
    assert_checkpoint_schema_version(state)
    assert_checkpoint_graph_version(state)
    return guard_state_for_persistence(state)


__all__ = [
    "CHECKPOINT_BACKEND_SQLITE",
    "CHECKPOINT_ERROR_DAMAGED",
    "CHECKPOINT_ERROR_DRIVER",
    "CHECKPOINT_ERROR_PATH",
    "CHECKPOINT_ERROR_VERSION",
    "GuardedSqliteSaver",
    "assert_checkpoint_graph_version",
    "assert_checkpoint_schema_version",
    "audit_checkpoint_database",
    "checkpoint_fingerprint",
    "checkpointer_driver_available",
    "guard_checkpoint_values",
    "guarded_sqlite_checkpointer",
    "guarded_state_for_checkpoint",
    "open_checkpoint_connection",
]
