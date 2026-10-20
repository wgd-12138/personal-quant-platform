"""两阶段提交与 outbox：让"业务写"和"外发"在崩溃后仍可解释。

为什么需要独立模块（A1-05 任务合同）：

1. 生成日报和发送通知是三件不同的事：prepare（准备内容）、commit（写业务文件）、notify（外发）。
   把它们合成一个动作，崩溃后就分不清"文件写了没写、消息发了没发"。
2. 外发的 exactly-once **不能由本地 receipt 单独保证**。本地只知道"我发出去了"，
   不知道"对面收到没有"。所以超时/崩溃后必须记 UNKNOWN，并且**禁止自动重试**——
   自动重试可能让群里收到两条一样的推送。
3. 幂等键要覆盖动作的全部输入（工具、动作哈希、线程、日期、时段、载荷、代码、提示词、
   数据快照、运行时版本）。少一项，换一项就会被当成新动作重复执行。

本模块只依赖标准库与 pq_agent 内部模块，不引入第三方依赖。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pq_agent.approval import ApprovalRecord, aware_now, to_iso, from_iso
from pq_agent.contracts import ContractError
from pq_agent.data.models import canonical_json, sha256_text
from pq_agent.security.redaction import guard_payload

OUTBOX_ENTRY_SCHEMA_VERSION = 1

#: outbox 的四种状态。UNKNOWN 是一等公民，不是错误码的附属品。
OUTBOX_STATUSES = ("PREPARED", "SENT", "FAILED", "UNKNOWN")

#: 三个提交阶段，每一步都可以单独审批。
OUTBOX_PHASES = ("prepare", "commit", "notify")

#: 单条消息（notify 阶段的一个收件人/一张卡片）的状态。
ITEM_STATUSES = ("PENDING", "SENT", "FAILED", "UNKNOWN")

#: 归档保留天数。超过这个天数的已结算记录可以清理，清理动作本身写审计。
OUTBOX_RETENTION_DAYS = 90

#: 幂等键覆盖的字段。任务合同要求包含 tool/action/thread/as_of/slot/payload/code/prompt/data/runtime 版本。
RECEIPT_KEY_FIELDS = (
    "tool_name",
    "action_hash",
    "thread_id",
    "as_of",
    "slot",
    "payload_digest",
    "code_hash",
    "prompt_hash",
    "data_snapshot_id",
    "runtime",
)

ERROR_OUTBOX_NOT_FOUND = "OUTBOX_NOT_FOUND"
ERROR_OUTBOX_UNKNOWN_REQUIRES_HUMAN = "OUTBOX_UNKNOWN_REQUIRES_HUMAN"
ERROR_OUTBOX_ALREADY_SENT = "OUTBOX_ALREADY_SENT"
ERROR_OUTBOX_PHASE_INVALID = "OUTBOX_PHASE_INVALID"
ERROR_OUTBOX_ITEM_INDEX_INVALID = "OUTBOX_ITEM_INDEX_INVALID"
ERROR_OUTBOX_ITEM_ALREADY_SETTLED = "OUTBOX_ITEM_ALREADY_SETTLED"
ERROR_OUTBOX_ITEM_STATUS_INVALID = "OUTBOX_ITEM_STATUS_INVALID"
ERROR_OUTBOX_RETRY_FORBIDDEN = "OUTBOX_RETRY_FORBIDDEN"
ERROR_OUTBOX_STORE_CORRUPT = "OUTBOX_STORE_CORRUPT"
ERROR_OUTBOX_ENTRY_INVALID = "OUTBOX_ENTRY_INVALID"


def compute_idempotency_key(**parts: Any) -> str:
    """把动作的全部输入压成一个稳定幂等键。

    未知字段直接拒绝：多传一个字段往往意味着调用方以为它参与幂等判定，实际没有。
    """
    unknown = sorted(set(parts) - set(RECEIPT_KEY_FIELDS))
    if unknown:
        raise ContractError(ERROR_OUTBOX_ENTRY_INVALID, "幂等键包含未知字段：" + ", ".join(unknown))
    payload = {name: str(parts.get(name) or "") for name in RECEIPT_KEY_FIELDS}
    return sha256_text(canonical_json(payload))


def compute_payload_digest(payload: Any) -> str:
    """载荷摘要。用于幂等键，也用于判断"内容变了没有"。"""
    return sha256_text(canonical_json(payload))


def _require_text(value: Any, where: str, *, max_len: int = 200) -> str:
    if not isinstance(value, str):
        raise ContractError(ERROR_OUTBOX_ENTRY_INVALID, where + " 必须是字符串，实际 " + type(value).__name__)
    if not value.strip():
        raise ContractError(ERROR_OUTBOX_ENTRY_INVALID, where + " 不能为空")
    if len(value) > max_len:
        raise ContractError(ERROR_OUTBOX_ENTRY_INVALID, where + " 超长（上限 " + str(max_len) + "）")
    return value


def _entry_status(items: Sequence[Mapping[str, Any]]) -> str:
    """由单条消息状态推导整条 outbox 记录的状态。

    优先级：只要有一条 UNKNOWN 就是 UNKNOWN（不能自动重试）；
    其次有 FAILED 就是 FAILED；全部 SENT 才是 SENT；否则仍是 PREPARED。
    """
    statuses = [str(item.get("status")) for item in items]
    if not statuses:
        return "PREPARED"
    if "UNKNOWN" in statuses:
        return "UNKNOWN"
    if "FAILED" in statuses:
        return "FAILED"
    if all(s == "SENT" for s in statuses):
        return "SENT"
    return "PREPARED"


@dataclass(frozen=True)
class OutboxEntry:
    """一条 outbox 记录：一个阶段动作的准备、结算与审计。"""

    entry_id: str
    idempotency_key: str
    thread_id: str
    call_id: str
    tool_name: str
    phase: str
    status: str = "PREPARED"
    created_at: str = ""
    updated_at: str = ""
    attempts: int = 0
    external_id: str | None = None
    items: tuple[Mapping[str, Any], ...] = ()
    reason: str = ""
    approval_id: str = ""
    schema_version: int = OUTBOX_ENTRY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_text(self.entry_id, "entry_id", max_len=128)
        _require_text(self.idempotency_key, "idempotency_key", max_len=128)
        _require_text(self.thread_id, "thread_id", max_len=128)
        _require_text(self.call_id, "call_id", max_len=128)
        _require_text(self.tool_name, "tool_name", max_len=128)
        if self.phase not in OUTBOX_PHASES:
            raise ContractError(ERROR_OUTBOX_PHASE_INVALID, "未知阶段：" + str(self.phase))
        if self.status not in OUTBOX_STATUSES:
            raise ContractError(ERROR_OUTBOX_ENTRY_INVALID, "未知状态：" + str(self.status))
        from_iso(self.created_at, "created_at")
        from_iso(self.updated_at, "updated_at")
        if not isinstance(self.attempts, int) or isinstance(self.attempts, bool) or self.attempts < 0:
            raise ContractError(ERROR_OUTBOX_ENTRY_INVALID, "attempts 必须是非负整数")
        for index, item in enumerate(self.items):
            if not isinstance(item, Mapping):
                raise ContractError(ERROR_OUTBOX_ENTRY_INVALID, "items[" + str(index) + "] 必须是对象")
            if str(item.get("status")) not in ITEM_STATUSES:
                raise ContractError(ERROR_OUTBOX_ITEM_STATUS_INVALID, "items[" + str(index) + "] 状态非法：" + str(item.get("status")))

    @property
    def settled(self) -> bool:
        return self.status in ("SENT", "FAILED")

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "idempotency_key": self.idempotency_key,
            "thread_id": self.thread_id,
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "phase": self.phase,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "attempts": self.attempts,
            "external_id": self.external_id,
            "items": [dict(item) for item in self.items],
            "reason": self.reason,
            "approval_id": self.approval_id,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OutboxEntry":
        if not isinstance(data, Mapping):
            raise ContractError(ERROR_OUTBOX_ENTRY_INVALID, "outbox 记录必须是对象")
        raw_items = data.get("items") or []
        if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
            raise ContractError(ERROR_OUTBOX_ENTRY_INVALID, "items 必须是数组")
        return cls(
            entry_id=str(data.get("entry_id") or ""),
            idempotency_key=str(data.get("idempotency_key") or ""),
            thread_id=str(data.get("thread_id") or ""),
            call_id=str(data.get("call_id") or ""),
            tool_name=str(data.get("tool_name") or ""),
            phase=str(data.get("phase") or ""),
            status=str(data.get("status") or "PREPARED"),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
            attempts=int(data.get("attempts") or 0),
            external_id=(None if data.get("external_id") is None else str(data.get("external_id"))),
            items=tuple(_item(item) for item in raw_items),
            reason=str(data.get("reason") or ""),
            approval_id=str(data.get("approval_id") or ""),
            schema_version=int(data.get("schema_version") or OUTBOX_ENTRY_SCHEMA_VERSION),
        )


def _item(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ContractError(ERROR_OUTBOX_ENTRY_INVALID, "item 必须是对象")
    index = raw.get("index")
    if not isinstance(index, int) or isinstance(index, bool) or index < 0:
        raise ContractError(ERROR_OUTBOX_ITEM_INDEX_INVALID, "item.index 必须是非负整数")
    return {
        "index": index,
        "key": str(raw.get("key") or ""),
        "status": str(raw.get("status") or "PENDING"),
        "external_id": (None if raw.get("external_id") is None else str(raw.get("external_id"))),
        "reason": str(raw.get("reason") or ""),
    }


class OutboxStore:
    """outbox 存储：内存索引 + 可选 JSONL 追加审计。

    追加式 JSONL 保留每一次状态迁移；读取时按 entry_id 后写覆盖前写。
    落盘前过 guard_payload，避免密钥经 outbox 记录泄漏。
    """

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        clock: Callable[[], dt.datetime] | None = None,
        secrets: Sequence[str] | None = None,
    ) -> None:
        self._clock = clock
        self._secrets = tuple(secrets) if secrets else None
        self._path = Path(path) if path is not None else None
        self._entries: dict[str, OutboxEntry] = {}
        self._by_key: dict[str, str] = {}
        self._order: list[str] = []
        if self._path is not None and self._path.exists():
            self._load()

    # ---------------------------------------------------------------- 基础
    @property
    def path(self) -> Path | None:
        return self._path

    def now(self) -> dt.datetime:
        return aware_now(self._clock)

    def get(self, entry_id: str) -> OutboxEntry:
        entry = self._entries.get(str(entry_id))
        if entry is None:
            raise ContractError(ERROR_OUTBOX_NOT_FOUND, "找不到 outbox 记录 " + str(entry_id))
        return entry

    def find_by_key(self, idempotency_key: str) -> OutboxEntry | None:
        entry_id = self._by_key.get(str(idempotency_key))
        return None if entry_id is None else self._entries.get(entry_id)

    def entries(self) -> tuple[OutboxEntry, ...]:
        return tuple(self._entries[key] for key in self._order)

    def pending(self) -> tuple[OutboxEntry, ...]:
        """未结算的记录：PREPARED（还没跑完）和 UNKNOWN（需要人来看）。"""
        return tuple(e for e in self.entries() if e.status in ("PREPARED", "UNKNOWN"))

    def unknown(self) -> tuple[OutboxEntry, ...]:
        return tuple(e for e in self.entries() if e.status == "UNKNOWN")

    def fingerprint(self) -> str:
        return sha256_text(canonical_json([e.to_dict() for e in self.entries()]))

    # ---------------------------------------------------------------- prepare
    def prepare(
        self,
        *,
        thread_id: str,
        call_id: str,
        tool_name: str,
        phase: str,
        idempotency_key: str,
        item_keys: Sequence[str] = (),
        now: dt.datetime | None = None,
    ) -> tuple[OutboxEntry, bool]:
        """登记一个阶段动作。返回 (记录, 是否新建)。

        幂等语义：同一个 idempotency_key 第二次进来，返回已有记录且 created=False。
        - 已 SENT：直接复用，调用方据此跳过重复业务写；
        - 已 FAILED：复用但**不自动重试**，需要显式 retry()；
        - 已 UNKNOWN：抛 OUTBOX_UNKNOWN_REQUIRES_HUMAN，交给人判断；
        - 仍 PREPARED：复用，让调用方接着往下跑。
        """
        if phase not in OUTBOX_PHASES:
            raise ContractError(ERROR_OUTBOX_PHASE_INVALID, "未知阶段：" + str(phase))
        _require_text(idempotency_key, "idempotency_key", max_len=128)
        existing = self.find_by_key(idempotency_key)
        if existing is not None:
            if existing.status == "UNKNOWN":
                raise ContractError(
                    ERROR_OUTBOX_UNKNOWN_REQUIRES_HUMAN,
                    "outbox " + existing.entry_id + " 结果未知，禁止自动重试，请人工确认后调用 retry()",
                )
            return existing, False
        moment = now or self.now()
        keys = list(item_keys) or [""]
        entry = OutboxEntry(
            entry_id="o" + uuid.uuid4().hex[:16],
            idempotency_key=_require_text(idempotency_key, "idempotency_key", max_len=128),
            thread_id=_require_text(thread_id, "thread_id", max_len=128),
            call_id=_require_text(call_id, "call_id", max_len=128),
            tool_name=_require_text(tool_name, "tool_name", max_len=128),
            phase=phase,
            status="PREPARED",
            created_at=to_iso(moment),
            updated_at=to_iso(moment),
            attempts=1,
            items=tuple(
                {"index": i, "key": str(k), "status": "PENDING", "external_id": None, "reason": ""}
                for i, k in enumerate(keys)
            ),
        )
        self._put(entry)
        return entry, True

    # ---------------------------------------------------------------- 结算
    def mark_item(
        self,
        entry_id: str,
        index: int,
        status: str,
        *,
        external_id: str | None = None,
        reason: str = "",
        now: dt.datetime | None = None,
    ) -> OutboxEntry:
        """结算单条消息。已结算的条目不能改（防止事后篡改投递结果）。"""
        if status not in ITEM_STATUSES:
            raise ContractError(ERROR_OUTBOX_ITEM_STATUS_INVALID, "未知条目状态：" + str(status))
        entry = self.get(entry_id)
        if not isinstance(index, int) or isinstance(index, bool) or index < 0 or index >= len(entry.items):
            raise ContractError(ERROR_OUTBOX_ITEM_INDEX_INVALID, "条目下标越界：" + repr(index))
        items = [dict(item) for item in entry.items]
        current = str(items[index].get("status"))
        if current != "PENDING":
            raise ContractError(
                ERROR_OUTBOX_ITEM_ALREADY_SETTLED,
                "条目 " + str(index) + " 已经是 " + current + "，不允许改写",
            )
        items[index]["status"] = status
        items[index]["external_id"] = external_id
        items[index]["reason"] = str(reason or "")[:300]
        moment = now or self.now()
        return self._replace(
            entry,
            items=tuple(items),
            status=_entry_status(items),
            updated_at=to_iso(moment),
            external_id=external_id or entry.external_id,
            reason=str(reason or "")[:300] if status in ("FAILED", "UNKNOWN") else entry.reason,
        )

    def mark_unknown(self, entry_id: str, *, reason: str = "", now: dt.datetime | None = None) -> OutboxEntry:
        """整条记录标记为结果未知（外发超时或本地崩溃）。

        未结算的条目一律记为 UNKNOWN。这是"不确定"的正确表达：
        既不能说成功（对面可能没收到），也不能说失败（对面可能收到了）。
        """
        entry = self.get(entry_id)
        items = [dict(item) for item in entry.items]
        for item in items:
            if str(item.get("status")) == "PENDING":
                item["status"] = "UNKNOWN"
                item["reason"] = str(reason or "结果未知")[:300]
        moment = now or self.now()
        return self._replace(
            entry,
            items=tuple(items),
            status="UNKNOWN",
            updated_at=to_iso(moment),
            reason=str(reason or "结果未知")[:300],
        )

    def retry(
        self,
        entry_id: str,
        *,
        approval: ApprovalRecord,
        now: dt.datetime | None = None,
    ) -> OutboxEntry:
        """人工确认后的显式重试。UNKNOWN 永远不允许自动重试，只允许人工批准后重试。"""
        entry = self.get(entry_id)
        if not isinstance(approval, ApprovalRecord):
            raise ContractError(ERROR_OUTBOX_RETRY_FORBIDDEN, "重试必须携带审批记录")
        if approval.status != "approved":
            raise ContractError(ERROR_OUTBOX_RETRY_FORBIDDEN, "审批状态是 " + approval.status + "，不允许重试")
        if approval.thread_id != entry.thread_id:
            raise ContractError(ERROR_OUTBOX_RETRY_FORBIDDEN, "审批属于线程 " + approval.thread_id + "，与 outbox 记录不一致")
        if entry.status == "SENT":
            raise ContractError(ERROR_OUTBOX_ALREADY_SENT, "记录已发送成功，不允许重试")
        items = [dict(item) for item in entry.items]
        for item in items:
            if str(item.get("status")) in ("FAILED", "UNKNOWN"):
                item["status"] = "PENDING"
                item["reason"] = ""
        moment = now or self.now()
        return self._replace(
            entry,
            items=tuple(items),
            status=_entry_status(items),
            attempts=entry.attempts + 1,
            updated_at=to_iso(moment),
            approval_id=approval.approval_id,
            reason="人工确认后重试",
        )

    # ---------------------------------------------------------------- 投递视图
    def delivery_state(self, entry_id: str) -> dict[str, Any]:
        """给界面/报告用的投递视图：哪些消息发了、哪些没发。"""
        entry = self.get(entry_id)
        buckets: dict[str, list[int]] = {"SENT": [], "PENDING": [], "FAILED": [], "UNKNOWN": []}
        for item in entry.items:
            buckets[str(item.get("status"))].append(int(item.get("index")))
        return {
            "entry_id": entry.entry_id,
            "phase": entry.phase,
            "status": entry.status,
            "attempts": entry.attempts,
            "sent": buckets["SENT"],
            "pending": buckets["PENDING"],
            "failed": buckets["FAILED"],
            "unknown": buckets["UNKNOWN"],
            "total": len(entry.items),
            "summary": (
                "已发送 " + str(len(buckets["SENT"])) + "/" + str(len(entry.items))
                + ("；未发送 " + str(len(buckets["PENDING"])) if buckets["PENDING"] else "")
                + ("；失败 " + str(len(buckets["FAILED"])) if buckets["FAILED"] else "")
                + ("；结果未知 " + str(len(buckets["UNKNOWN"])) if buckets["UNKNOWN"] else "")
            ),
        }

    # ---------------------------------------------------------------- 归档
    def archive(
        self,
        *,
        now: dt.datetime | None = None,
        retention_days: int = OUTBOX_RETENTION_DAYS,
    ) -> dict[str, Any]:
        """归档并清理超过保留期的**已结算**记录，返回可审计的清理摘要。

        只有 SENT / FAILED 能被清理；PREPARED 和 UNKNOWN 一律保留，
        因为"还没跑完"和"不知道跑没跑成"都不该被清掉。
        """
        if not isinstance(retention_days, int) or isinstance(retention_days, bool) or retention_days <= 0:
            raise ContractError(ERROR_OUTBOX_ENTRY_INVALID, "retention_days 必须是正整数")
        moment = now or self.now()
        cutoff = moment - dt.timedelta(days=retention_days)
        archived: list[str] = []
        for entry in self.entries():
            if not entry.settled:
                continue
            if from_iso(entry.updated_at, "updated_at") < cutoff:
                archived.append(entry.entry_id)
        if not archived:
            return {"archived": [], "retained": len(self._entries), "digest": self.fingerprint(), "archive_path": None}
        digest = sha256_text(canonical_json([self._entries[e].to_dict() for e in archived]))
        record = {
            "kind": "outbox_archive",
            "archived_at": to_iso(moment),
            "retention_days": retention_days,
            "entry_ids": archived,
            "digest": digest,
            "count": len(archived),
        }
        archive_path: str | None = None
        if self._path is not None:
            target = self._path.with_name(self._path.name + ".archive.jsonl")
            target.parent.mkdir(parents=True, exist_ok=True)
            safe = guard_payload(record, secrets=self._secrets, where="outbox_archive")
            with target.open("a", encoding="utf-8", newline=chr(10)) as handle:
                handle.write(json.dumps(safe, ensure_ascii=False, sort_keys=True) + chr(10))
            archive_path = str(target)
        for entry_id in archived:
            entry = self._entries.pop(entry_id)
            self._by_key.pop(entry.idempotency_key, None)
            self._order.remove(entry_id)
        return {"archived": archived, "retained": len(self._entries), "digest": digest, "archive_path": archive_path}

    # ---------------------------------------------------------------- 内部
    def _put(self, entry: OutboxEntry) -> None:
        if entry.entry_id not in self._entries:
            self._order.append(entry.entry_id)
        self._entries[entry.entry_id] = entry
        self._by_key[entry.idempotency_key] = entry.entry_id
        self._append(entry)

    def _replace(self, entry: OutboxEntry, **changes: Any) -> OutboxEntry:
        updated = OutboxEntry(**{**entry.to_dict(), **changes})
        self._entries[entry.entry_id] = updated
        self._append(updated)
        return updated

    def _append(self, entry: OutboxEntry) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        safe = guard_payload(entry.to_dict(), secrets=self._secrets, where="outbox")
        with self._path.open("a", encoding="utf-8", newline=chr(10)) as handle:
            handle.write(json.dumps(safe, ensure_ascii=False, sort_keys=True) + chr(10))

    def _load(self) -> None:
        assert self._path is not None
        for number, raw in enumerate(self._path.read_text(encoding="utf-8").splitlines(), start=1):
            text = raw.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ContractError(ERROR_OUTBOX_STORE_CORRUPT, "第 " + str(number) + " 行不是合法 JSON") from exc
            entry = OutboxEntry.from_dict(payload)
            if entry.entry_id not in self._entries:
                self._order.append(entry.entry_id)
            self._entries[entry.entry_id] = entry
            self._by_key[entry.idempotency_key] = entry.entry_id


__all__ = [
    "ITEM_STATUSES",
    "OUTBOX_ENTRY_SCHEMA_VERSION",
    "OUTBOX_PHASES",
    "OUTBOX_RETENTION_DAYS",
    "OUTBOX_STATUSES",
    "RECEIPT_KEY_FIELDS",
    "OutboxEntry",
    "OutboxStore",
    "compute_idempotency_key",
    "compute_payload_digest",
]
