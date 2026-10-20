"""人工审批存储：一次性消费、动作哈希绑定、过期与重放防护。

为什么需要独立模块（A1-05 任务合同）：

1. LangGraph 的 interrupt 只负责"挂起与恢复"，不负责"这次恢复用的还是不是当初那个动作"。
   审批一旦脱离动作内容，就会退化成"批了 A 却执行了 B"。
2. 审批必须**一次性消费**：同一个 approval_id 被重放时，第二次必须失败，而不是再写一次业务文件。
3. 审批会过期。超时未决策按 fail-closed 处理（拒绝），不默认通过。

本模块只依赖标准库与 pq_agent 内部模块，不引入第三方依赖。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from pq_agent.contracts import CAPABILITIES, ContractError
from pq_agent.data.models import CST, canonical_json, sha256_text
from pq_agent.security.redaction import guard_payload

APPROVAL_RECORD_SCHEMA_VERSION = 1

#: 默认审批有效期（秒）。15 分钟足够人看到卡片并决定，又不至于让旧审批长期可被重放。
APPROVAL_TTL_S = 900.0

#: 参与动作哈希的字段。任何一项变了，原来的审批就作废。
#: 之所以包含 code/mount/runtime/data，是因为"同一个工具名 + 同一组参数"在不同代码版本、
#: 不同挂载、不同运行时或不同数据快照下，实际执行的是不同的动作。
ACTION_HASH_FIELDS = (
    "tool_name",
    "arguments_hash",
    "data_snapshot_id",
    "runtime",
    "code_hash",
    "prompt_hash",
    "mount_hash",
)

APPROVAL_STATUS_PENDING = "pending"
APPROVAL_STATUS_APPROVED = "approved"
APPROVAL_STATUS_REJECTED = "rejected"
APPROVAL_STATUS_EXPIRED = "expired"

ERROR_APPROVAL_NOT_FOUND = "APPROVAL_NOT_FOUND"
ERROR_APPROVAL_ALREADY_DECIDED = "APPROVAL_ALREADY_DECIDED"
ERROR_APPROVAL_ALREADY_CONSUMED = "APPROVAL_ALREADY_CONSUMED"
ERROR_APPROVAL_NOT_APPROVED = "APPROVAL_NOT_APPROVED"
ERROR_APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
ERROR_APPROVAL_ACTION_MISMATCH = "APPROVAL_ACTION_MISMATCH"
ERROR_APPROVAL_THREAD_MISMATCH = "APPROVAL_THREAD_MISMATCH"
ERROR_APPROVAL_ACTOR_MISMATCH = "APPROVAL_ACTOR_MISMATCH"
ERROR_APPROVAL_REQUEST_INVALID = "APPROVAL_REQUEST_INVALID"
ERROR_APPROVAL_STORE_CORRUPT = "APPROVAL_STORE_CORRUPT"
ERROR_APPROVAL_TTL_INVALID = "APPROVAL_TTL_INVALID"


def _require_text(value: Any, where: str, *, max_len: int = 200, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ContractError(ERROR_APPROVAL_REQUEST_INVALID, where + " 必须是字符串，实际 " + type(value).__name__)
    text = value.strip()
    if not text and not allow_empty:
        raise ContractError(ERROR_APPROVAL_REQUEST_INVALID, where + " 不能为空")
    if len(value) > max_len:
        raise ContractError(ERROR_APPROVAL_REQUEST_INVALID, where + " 超长（上限 " + str(max_len) + "）")
    return value


def _now(clock: Callable[[], dt.datetime] | None) -> dt.datetime:
    moment = clock() if clock is not None else dt.datetime.now(CST)
    if moment.tzinfo is None:
        raise ContractError("DATETIME_NAIVE", "审批时钟必须带时区，禁止使用本地裸时间")
    return moment


def _iso(moment: dt.datetime) -> str:
    return moment.isoformat()


def _parse_iso(value: Any, where: str) -> dt.datetime:
    text = _require_text(value, where)
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise ContractError(ERROR_APPROVAL_REQUEST_INVALID, where + " 不是合法 ISO 时间：" + text) from exc
    if parsed.tzinfo is None:
        raise ContractError("DATETIME_NAIVE", where + " 缺少时区：" + text)
    return parsed


def aware_now(clock: Callable[[], dt.datetime] | None = None) -> dt.datetime:
    """取带时区的当前时间。裸时间一律拒绝，避免跨时区误判审批过期。"""
    return _now(clock)


def to_iso(moment: dt.datetime) -> str:
    """转成 ISO 字符串（保留时区）。"""
    return _iso(moment)


def from_iso(value: Any, where: str) -> dt.datetime:
    """解析 ISO 字符串，缺时区直接拒绝。"""
    return _parse_iso(value, where)


def compute_action_hash(
    *,
    tool_name: str,
    arguments_hash: str,
    data_snapshot_id: str = "",
    runtime: str = "",
    code_hash: str = "",
    prompt_hash: str = "",
    mount_hash: str = "",
) -> str:
    """把"要执行的动作"压成一个稳定哈希。

    审批绑的是这个哈希，而不是工具名。任何一项输入变了，哈希就变，旧审批自动失效。
    """
    parts = {
        "tool_name": _require_text(tool_name, "action.tool_name", max_len=128),
        "arguments_hash": _require_text(arguments_hash, "action.arguments_hash", max_len=128),
        "data_snapshot_id": str(data_snapshot_id or ""),
        "runtime": str(runtime or ""),
        "code_hash": str(code_hash or ""),
        "prompt_hash": str(prompt_hash or ""),
        "mount_hash": str(mount_hash or ""),
    }
    return sha256_text(canonical_json(parts))


@dataclass(frozen=True)
class ApprovalRecord:
    """一条审批的完整生命周期记录（请求 -> 决策 -> 消费）。"""

    approval_id: str
    thread_id: str
    call_id: str
    tool_name: str
    capability: str
    action_hash: str
    arguments_hash: str
    requested_at: str
    expires_at: str
    actor: str = ""
    preview: Mapping[str, str] = field(default_factory=dict)
    risk_note: str = ""
    status: str = APPROVAL_STATUS_PENDING
    decided_at: str | None = None
    decided_actor: str = ""
    consumed_at: str | None = None
    reason: str = ""
    schema_version: int = APPROVAL_RECORD_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_text(self.approval_id, "approval_id", max_len=128)
        _require_text(self.thread_id, "thread_id", max_len=128)
        _require_text(self.call_id, "call_id", max_len=128)
        _require_text(self.tool_name, "tool_name", max_len=128)
        if self.capability not in CAPABILITIES:
            raise ContractError(ERROR_APPROVAL_REQUEST_INVALID, "未知 capability：" + str(self.capability))
        _require_text(self.action_hash, "action_hash", max_len=128)
        _require_text(self.arguments_hash, "arguments_hash", max_len=128)
        _parse_iso(self.requested_at, "requested_at")
        _parse_iso(self.expires_at, "expires_at")
        if self.status not in (APPROVAL_STATUS_PENDING, APPROVAL_STATUS_APPROVED, APPROVAL_STATUS_REJECTED, APPROVAL_STATUS_EXPIRED):
            raise ContractError(ERROR_APPROVAL_REQUEST_INVALID, "未知审批状态：" + str(self.status))
        if self.status == APPROVAL_STATUS_PENDING and self.decided_at is not None:
            raise ContractError(ERROR_APPROVAL_REQUEST_INVALID, "pending 状态不允许带 decided_at")
        if self.status != APPROVAL_STATUS_PENDING and self.decided_at is None:
            raise ContractError(ERROR_APPROVAL_REQUEST_INVALID, self.status + " 状态必须带 decided_at")
        if self.decided_at is not None:
            _parse_iso(self.decided_at, "decided_at")
        if self.consumed_at is not None:
            _parse_iso(self.consumed_at, "consumed_at")
        if self.consumed_at is not None and self.status != APPROVAL_STATUS_APPROVED:
            raise ContractError(ERROR_APPROVAL_REQUEST_INVALID, "只有已批准的审批才能被消费")

    @property
    def consumed(self) -> bool:
        return self.consumed_at is not None

    def is_expired_at(self, moment: dt.datetime) -> bool:
        return moment >= _parse_iso(self.expires_at, "expires_at")

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "thread_id": self.thread_id,
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "capability": self.capability,
            "action_hash": self.action_hash,
            "arguments_hash": self.arguments_hash,
            "requested_at": self.requested_at,
            "expires_at": self.expires_at,
            "actor": self.actor,
            "preview": dict(self.preview),
            "risk_note": self.risk_note,
            "status": self.status,
            "decided_at": self.decided_at,
            "decided_actor": self.decided_actor,
            "consumed_at": self.consumed_at,
            "reason": self.reason,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ApprovalRecord":
        if not isinstance(data, Mapping):
            raise ContractError(ERROR_APPROVAL_REQUEST_INVALID, "审批记录必须是对象")
        preview = data.get("preview") or {}
        if not isinstance(preview, Mapping):
            raise ContractError(ERROR_APPROVAL_REQUEST_INVALID, "preview 必须是对象")
        return cls(
            approval_id=str(data.get("approval_id") or ""),
            thread_id=str(data.get("thread_id") or ""),
            call_id=str(data.get("call_id") or ""),
            tool_name=str(data.get("tool_name") or ""),
            capability=str(data.get("capability") or ""),
            action_hash=str(data.get("action_hash") or ""),
            arguments_hash=str(data.get("arguments_hash") or ""),
            requested_at=str(data.get("requested_at") or ""),
            expires_at=str(data.get("expires_at") or ""),
            actor=str(data.get("actor") or ""),
            preview={str(k): str(v) for k, v in preview.items()},
            risk_note=str(data.get("risk_note") or ""),
            status=str(data.get("status") or APPROVAL_STATUS_PENDING),
            decided_at=(None if data.get("decided_at") is None else str(data.get("decided_at"))),
            decided_actor=str(data.get("decided_actor") or ""),
            consumed_at=(None if data.get("consumed_at") is None else str(data.get("consumed_at"))),
            reason=str(data.get("reason") or ""),
            schema_version=int(data.get("schema_version") or APPROVAL_RECORD_SCHEMA_VERSION),
        )


class ApprovalStore:
    """审批存储：内存索引 + 可选 JSONL 追加审计。

    追加式 JSONL 的好处是每条决策都留痕；读取时按 approval_id 后写覆盖前写。
    落盘前一律过 guard_payload，避免密钥经审批记录泄漏。
    """

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        clock: Callable[[], dt.datetime] | None = None,
        ttl_s: float = APPROVAL_TTL_S,
        secrets: Iterable[str] | None = None,
    ) -> None:
        if not isinstance(ttl_s, (int, float)) or isinstance(ttl_s, bool) or ttl_s <= 0:
            raise ContractError(ERROR_APPROVAL_TTL_INVALID, "ttl_s 必须是正数，实际 " + repr(ttl_s))
        self._clock = clock
        self._ttl_s = float(ttl_s)
        self._secrets = tuple(secrets) if secrets else None
        self._path = Path(path) if path is not None else None
        self._records: dict[str, ApprovalRecord] = {}
        self._order: list[str] = []
        if self._path is not None and self._path.exists():
            self._load()

    # ---------------------------------------------------------------- 基础
    @property
    def path(self) -> Path | None:
        return self._path

    def now(self) -> dt.datetime:
        return _now(self._clock)

    def get(self, approval_id: str) -> ApprovalRecord:
        record = self._records.get(str(approval_id))
        if record is None:
            raise ContractError(ERROR_APPROVAL_NOT_FOUND, "找不到审批 " + str(approval_id))
        return record

    def find(self, approval_id: str) -> ApprovalRecord | None:
        return self._records.get(str(approval_id))

    def records(self) -> tuple[ApprovalRecord, ...]:
        return tuple(self._records[key] for key in self._order)

    def pending(self, *, now: dt.datetime | None = None) -> tuple[ApprovalRecord, ...]:
        moment = now or self.now()
        return tuple(r for r in self.records() if r.status == APPROVAL_STATUS_PENDING and not r.is_expired_at(moment))

    def fingerprint(self) -> str:
        payload = [r.to_dict() for r in self.records()]
        return sha256_text(canonical_json(payload))

    # ---------------------------------------------------------------- 请求
    def request(
        self,
        *,
        thread_id: str,
        call_id: str,
        tool_name: str,
        capability: str,
        arguments_hash: str,
        action_hash: str | None = None,
        preview: Mapping[str, Any] | None = None,
        risk_note: str = "",
        actor: str = "",
        data_snapshot_id: str = "",
        runtime: str = "",
        code_hash: str = "",
        prompt_hash: str = "",
        mount_hash: str = "",
        now: dt.datetime | None = None,
        approval_id: str | None = None,
    ) -> ApprovalRecord:
        """登记一次待审批动作，返回带 expires_at 的审批记录。"""
        moment = now or self.now()
        resolved_hash = action_hash or compute_action_hash(
            tool_name=tool_name,
            arguments_hash=arguments_hash,
            data_snapshot_id=data_snapshot_id,
            runtime=runtime,
            code_hash=code_hash,
            prompt_hash=prompt_hash,
            mount_hash=mount_hash,
        )
        final_id = approval_id or ("a" + uuid.uuid4().hex[:16])
        existing = self._records.get(str(final_id))
        if existing is not None:
            # resume 会让审批节点重跑，同一 approval_id 必须复用旧记录。
            # 否则会把已过期或已决策的审批重置回 pending，等于重新打开闸门。
            if existing.action_hash != resolved_hash:
                raise ContractError(
                    ERROR_APPROVAL_REQUEST_INVALID,
                    "审批 {} 已绑定一个动作哈希，不能改绑".format(final_id),
                )
            return existing
        safe_preview = _preview(preview or {}, secrets=self._secrets)
        record = ApprovalRecord(
            approval_id=final_id,
            thread_id=_require_text(thread_id, "thread_id", max_len=128),
            call_id=_require_text(call_id, "call_id", max_len=128),
            tool_name=_require_text(tool_name, "tool_name", max_len=128),
            capability=capability,
            action_hash=resolved_hash,
            arguments_hash=_require_text(arguments_hash, "arguments_hash", max_len=128),
            requested_at=_iso(moment),
            expires_at=_iso(moment + dt.timedelta(seconds=self._ttl_s)),
            actor=str(actor or ""),
            preview=safe_preview,
            risk_note=str(risk_note or "")[:500],
            status=APPROVAL_STATUS_PENDING,
        )
        self._put(record)
        return record

    # ---------------------------------------------------------------- 决策
    def decide(
        self,
        approval_id: str,
        *,
        approved: bool,
        actor: str = "",
        thread_id: str | None = None,
        action_hash: str | None = None,
        reason: str = "",
        now: dt.datetime | None = None,
    ) -> ApprovalRecord:
        """记录人类决策。所有不匹配都 fail-closed（抛错，不静默改成拒绝）。"""
        moment = now or self.now()
        record = self.get(approval_id)
        if record.consumed:
            raise ContractError(ERROR_APPROVAL_ALREADY_CONSUMED, "审批 " + record.approval_id + " 已被消费，不能再决策")
        if record.status != APPROVAL_STATUS_PENDING:
            raise ContractError(ERROR_APPROVAL_ALREADY_DECIDED, "审批 " + record.approval_id + " 已经是 " + record.status)
        if thread_id is not None and str(thread_id) != record.thread_id:
            raise ContractError(ERROR_APPROVAL_THREAD_MISMATCH, "审批属于线程 " + record.thread_id + "，不是 " + str(thread_id))
        if action_hash is not None and str(action_hash) != record.action_hash:
            raise ContractError(ERROR_APPROVAL_ACTION_MISMATCH, "动作哈希不匹配，拒绝决策")
        if record.actor and str(actor) != record.actor:
            raise ContractError(ERROR_APPROVAL_ACTOR_MISMATCH, "审批指定给 " + record.actor + "，不是 " + str(actor))
        if record.is_expired_at(moment):
            expired = self._replace(record, status=APPROVAL_STATUS_EXPIRED, decided_at=_iso(moment), decided_actor=str(actor or ""), reason="审批超时，按 fail-closed 处理")
            raise ContractError(ERROR_APPROVAL_EXPIRED, "审批已过期（expires_at=" + expired.expires_at + "）")
        if not isinstance(approved, bool):
            raise ContractError(ERROR_APPROVAL_REQUEST_INVALID, "approved 必须是布尔值")
        status = APPROVAL_STATUS_APPROVED if approved else APPROVAL_STATUS_REJECTED
        return self._replace(
            record,
            status=status,
            decided_at=_iso(moment),
            decided_actor=str(actor or ""),
            reason=str(reason or ("人工同意" if approved else "人工拒绝"))[:500],
        )

    # ---------------------------------------------------------------- 消费
    def consume(
        self,
        approval_id: str,
        *,
        thread_id: str,
        action_hash: str,
        actor: str | None = None,
        now: dt.datetime | None = None,
    ) -> ApprovalRecord:
        """CAS 一次性消费：同一个 approval_id 只能成功一次。

        这是防重放的核心。第二次调用必然抛 APPROVAL_ALREADY_CONSUMED，
        所以"同一 action 重复提交只产生一个成功业务写"。
        """
        moment = now or self.now()
        record = self.get(approval_id)
        if record.consumed:
            raise ContractError(ERROR_APPROVAL_ALREADY_CONSUMED, "审批 " + record.approval_id + " 已被消费于 " + str(record.consumed_at))
        if record.status != APPROVAL_STATUS_APPROVED:
            raise ContractError(ERROR_APPROVAL_NOT_APPROVED, "审批状态是 " + record.status + "，不能执行")
        if str(thread_id) != record.thread_id:
            raise ContractError(ERROR_APPROVAL_THREAD_MISMATCH, "审批属于线程 " + record.thread_id + "，不是 " + str(thread_id))
        if str(action_hash) != record.action_hash:
            raise ContractError(ERROR_APPROVAL_ACTION_MISMATCH, "动作哈希与审批不一致，拒绝执行")
        if actor is not None and record.decided_actor and str(actor) != record.decided_actor:
            raise ContractError(ERROR_APPROVAL_ACTOR_MISMATCH, "消费人 " + str(actor) + " 与决策人 " + record.decided_actor + " 不一致")
        if record.is_expired_at(moment):
            self._replace(record, status=APPROVAL_STATUS_EXPIRED, decided_at=_iso(moment), decided_actor=record.decided_actor, reason="审批在消费前过期")
            raise ContractError(ERROR_APPROVAL_EXPIRED, "审批在消费前已过期")
        return self._replace(record, consumed_at=_iso(moment))

    # ---------------------------------------------------------------- 过期
    def expire_due(self, *, now: dt.datetime | None = None) -> tuple[str, ...]:
        """把到期的 pending 审批统一标记为 expired，返回被标记的 approval_id。"""
        moment = now or self.now()
        touched: list[str] = []
        for record in self.records():
            if record.status == APPROVAL_STATUS_PENDING and record.is_expired_at(moment):
                self._replace(record, status=APPROVAL_STATUS_EXPIRED, decided_at=_iso(moment), reason="审批超时，按 fail-closed 处理")
                touched.append(record.approval_id)
        return tuple(touched)

    # ---------------------------------------------------------------- 内部
    def _put(self, record: ApprovalRecord) -> None:
        if record.approval_id not in self._records:
            self._order.append(record.approval_id)
        self._records[record.approval_id] = record
        self._append(record)

    def _replace(self, record: ApprovalRecord, **changes: Any) -> ApprovalRecord:
        updated = ApprovalRecord(**{**record.to_dict(), **changes})
        self._records[record.approval_id] = updated
        self._append(updated)
        return updated

    def _append(self, record: ApprovalRecord) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        safe = guard_payload(record.to_dict(), secrets=self._secrets, where="approval")
        line = json.dumps(safe, ensure_ascii=False, sort_keys=True)
        with self._path.open("a", encoding="utf-8", newline=chr(10)) as handle:
            handle.write(line + chr(10))

    def _load(self) -> None:
        assert self._path is not None
        for number, raw in enumerate(self._path.read_text(encoding="utf-8").splitlines(), start=1):
            text = raw.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ContractError(ERROR_APPROVAL_STORE_CORRUPT, "第 " + str(number) + " 行不是合法 JSON") from exc
            record = ApprovalRecord.from_dict(payload)
            if record.approval_id not in self._records:
                self._order.append(record.approval_id)
            self._records[record.approval_id] = record


def _preview(payload: Mapping[str, Any], *, secrets: Any = None) -> dict[str, str]:
    """给审批人看的参数预览：只留短标量，避免把整段文件或密钥抛到界面。

    顺序固定：先按标量收敛 -> 过 guard_payload 脱敏 -> 最后截断。
    反过来先截断，长密钥会被切断成无法匹配的残片，脱敏就失效了；
    同时内存里的 preview 必须和落盘的一致，否则重载后指纹对不上。
    """
    collected: dict[str, str] = {}
    for key, value in payload.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            collected[str(key)] = str(value)
        else:
            collected[str(key)] = "<" + type(value).__name__ + ">"
    scrubbed = guard_payload(collected, secrets=secrets, where="approval_preview")
    return {str(key): str(value)[:120] for key, value in scrubbed.items()}


__all__ = [
    "ACTION_HASH_FIELDS",
    "APPROVAL_RECORD_SCHEMA_VERSION",
    "APPROVAL_STATUS_APPROVED",
    "APPROVAL_STATUS_EXPIRED",
    "APPROVAL_STATUS_PENDING",
    "APPROVAL_STATUS_REJECTED",
    "APPROVAL_TTL_S",
    "ApprovalRecord",
    "ApprovalStore",
    "compute_action_hash",
]
