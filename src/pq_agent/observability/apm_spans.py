"""A4-02 span 打点助手：graph 与沙箱共用的一套「有就记、没有就跳过」的包装。

两条设计原则：

1. **观测层不许把主流程带崩。**tracer 为 None（本机常态：没有 Collector）时全部是空操作，
   而且任何 set_attribute 失败都被吞掉——span 记不上是观测问题，不是业务问题。
   这一条与 A1-06 的纪律一致：可观测层是旁路。
2. **span 名和属性名都不能自由发挥。**名字来自 apm_signals 的常量，属性名必须已在
   redaction.SPAN_ATTRIBUTE_ALLOWLIST 里。写错名字不会被静默接受，出口会把它丢掉并记降级事件，
   但这里更想早点拦住：_checked_attributes() 对未知键直接抛错。

另外提供 SpanCollector：不依赖 OTel 的本地 span 收集器。
本机没有 Collector，但「一次运行的 span 树长什么样」必须能被断言，
所以服务层用 SpanCollector 边跑边记，trace 详情页与验收脚本都读它。
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from pq_agent.contracts import ContractError

from pq_agent.observability.apm_signals import (
    ALLOWED_EDGES,
    SPAN_APPROVAL,
    SPAN_LLM,
    SPAN_NODE,
    SPAN_RAG,
    SPAN_RUN,
    SPAN_SANDBOX,
    SPAN_TOOL,
    SpanRecord,
    TraceShape,
    require_known_span,
    trace_shape,
)
from pq_agent.observability.redaction import SPAN_ATTRIBUTE_ALLOWLIST

SPANS_SCHEMA_VERSION = "pq_agent.apm_spans_helpers/1"

ERROR_SPAN_ATTRIBUTE = "APM_SPAN_ATTRIBUTE_UNKNOWN"


def _checked_attributes(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    """属性名必须在白名单里。写错名字当场抛错，不留给出口去悄悄丢掉。

    为什么这里更严：出口丢弃是「防泄漏」的最后一道，不是「防手滑」的第一道。
    一个拼错的属性名如果只被出口丢掉，现象是「看板上少了一列」，很难查。
    """
    out: dict[str, Any] = {}
    for key, value in dict(attributes or {}).items():
        name = str(key)
        if name not in SPAN_ATTRIBUTE_ALLOWLIST:
            raise ContractError(
                ERROR_SPAN_ATTRIBUTE,
                "span 属性 %s 不在白名单里（先加进 SPAN_ATTRIBUTE_ALLOWLIST 再用）" % name,
            )
        out[name] = value
    return out


def _set_attributes(span: Any, attributes: Mapping[str, Any]) -> None:
    if span is None:
        return
    for key, value in attributes.items():
        try:
            span.set_attribute(str(key), value)
        except Exception:
            # 观测层不许把主流程带崩：属性写不进去就跳过。
            continue


def _mark(span: Any, *, status: str, error_code: str, error_type: str, duration_ms: int) -> None:
    _set_attributes(span, {
        "status": status,
        "error_code": error_code,
        "error_type": error_type,
        "duration_ms": int(duration_ms),
    })


@contextmanager
def span_scope(
    tracer: Any,
    name: str,
    *,
    attributes: Mapping[str, Any] | None = None,
    status_holder: dict[str, str] | None = None,
) -> Iterator[Any]:
    """打开一个 span。tracer 为 None 时是空操作，yield None。

    退出时统一写 status / error_code / error_type / duration_ms，
    与 graph._finish_span 的字段保持一致，这样一条 trace 里的 span 长得一样。
    """
    require_known_span(name)
    payload = _checked_attributes(attributes)
    if tracer is None:
        yield None
        return
    started = time.monotonic()
    cm = tracer.start_as_current_span(str(name))
    with cm as span:
        _set_attributes(span, payload)
        try:
            yield span
        except BaseException as exc:
            elapsed = max(0, int((time.monotonic() - started) * 1000))
            code = getattr(exc, "code", "") or type(exc).__name__
            _mark(span, status="error", error_code=str(code), error_type=type(exc).__name__, duration_ms=elapsed)
            raise
        else:
            elapsed = max(0, int((time.monotonic() - started) * 1000))
            # status_holder 是给「被调用方比异常更懂结果」的场景留的：
            # 沙箱的拒绝/超时都是正常返回值，不是异常，只有它自己知道该标 ok 还是 error。
            final_status = "ok"
            final_code = ""
            if status_holder is not None:
                final_status = str(status_holder.get("status") or "ok")
                final_code = str(status_holder.get("error_code") or "")
            _mark(span, status=final_status, error_code=final_code, error_type="", duration_ms=elapsed)

def run_span(tracer: Any, *, thread_id: str = "", mode: str = "", as_of: str = "", graph_version: str = ""):
    """一次运行的根 span。所有节点 span 都挂在它下面。"""
    return span_scope(tracer, SPAN_RUN, attributes={
        "thread_id": str(thread_id),
        "agent.mode": str(mode),
        "as_of": str(as_of),
        "graph_version": str(graph_version),
    })


def tool_span(tracer: Any, *, tool_name: str, capability: str = "", dry_run: bool = True):
    """一次工具调用。dry_run 一定要记：否则事后分不清「真的发了」和「只是预览」。"""
    return span_scope(tracer, SPAN_TOOL, attributes={
        "tool.name": str(tool_name),
        "tool.capability": str(capability),
        "tool.dry_run": bool(dry_run),
    })


def sandbox_span(
    tracer: Any,
    *,
    runtime: str,
    isolation_level: str = "",
    status: str = "",
    status_holder: dict[str, str] | None = None,
):
    """一次沙箱执行。isolation_level 必须记：否则「跑在约束层还是容器里」无从判断。

    沙箱的结局（ok / static_blocked / policy_denied / failed）是**返回值**不是异常，
    所以调用方通过 status_holder 把最终状态告诉 span。
    """
    return span_scope(
        tracer,
        SPAN_SANDBOX,
        attributes={
            "runtime": str(runtime),
            "isolation_level": str(isolation_level),
            "sandbox.status": str(status),
        },
        status_holder=status_holder,
    )


def approval_span(tracer: Any, *, tool_name: str = "", capability: str = "", decision: str = ""):
    """一次人工确认。decision 为空表示「正在等」——这本身就是有用的信息。"""
    return span_scope(tracer, SPAN_APPROVAL, attributes={
        "approval.tool": str(tool_name),
        "tool.capability": str(capability),
        "approval.decision": str(decision),
    })


def rag_span(tracer: Any, *, mode: str = "", top_k: int = 0):
    return span_scope(tracer, SPAN_RAG, attributes={
        "rag.mode": str(mode),
        "rag.top_k": int(top_k),
    })


def llm_span(tracer: Any, *, model: str = "", source: str = ""):
    return span_scope(tracer, SPAN_LLM, attributes={
        "llm.model": str(model),
        "llm.source": str(source),
    })


@dataclass
class SpanCollector:
    """本地 span 收集器：不依赖 OTel，把一次运行的 span 树记在内存里。

    本机没有 Collector，但 trace 详情页、验收脚本、事实卡都需要「这次运行的层级长什么样」。
    服务层边跑边往这里记，最后 shape() 得到可断言的 TraceShape。
    """

    trace_id: str
    spans: list[SpanRecord] = field(default_factory=list)
    links: list[Any] = field(default_factory=list)
    interrupted: bool = False
    resumed_from: str = ""
    _open: list[str] = field(default_factory=list)
    _counter: int = 0

    def open(self, name: str, *, parent_id: str = "", attributes: Mapping[str, Any] | None = None) -> str:
        """开一个 span，返回 span_id。parent_id 为空时自动挂到当前打开的 span 下。

        自动挂父是为了少传参数：调用方通常在 with 块里，栈顶就是它的父。
        """
        require_known_span(name)
        payload = _checked_attributes(attributes)
        parent = str(parent_id)
        if not parent and self._open and name != SPAN_RUN:
            parent = self._open[-1]
        self._counter += 1
        span_id = "sp-%04d-%s" % (self._counter, name.replace(".", "-"))
        self.spans.append(SpanRecord(span_id=span_id, name=str(name), parent_id=parent, attributes=payload))
        self._open.append(span_id)
        return span_id

    def close(self, span_id: str, *, status: str = "ok", error_code: str = "", duration_ms: int = 0) -> None:
        """关闭一个 span。写入状态与耗时，并从栈里弹出。"""
        for index, item in enumerate(self.spans):
            if item.span_id == str(span_id):
                self.spans[index] = SpanRecord(
                    span_id=item.span_id,
                    name=item.name,
                    parent_id=item.parent_id,
                    status=str(status),
                    error_code=str(error_code),
                    duration_ms=max(0, int(duration_ms)),
                    attributes=item.attributes,
                    links=item.links,
                )
                break
        if self._open and self._open[-1] == str(span_id):
            self._open.pop()
        elif str(span_id) in self._open:
            self._open.remove(str(span_id))

    @contextmanager
    def scope(self, name: str, *, attributes: Mapping[str, Any] | None = None) -> Iterator[str]:
        """with 用法：进出自动配对，异常也会把 span 标成 error 并带上错误码。"""
        span_id = self.open(name, attributes=attributes)
        started = time.monotonic()
        try:
            yield span_id
        except BaseException as exc:
            elapsed = max(0, int((time.monotonic() - started) * 1000))
            code = getattr(exc, "code", "") or type(exc).__name__
            self.close(span_id, status="error", error_code=str(code), duration_ms=elapsed)
            raise
        else:
            elapsed = max(0, int((time.monotonic() - started) * 1000))
            self.close(span_id, status="ok", duration_ms=elapsed)

    def add_link(self, link: Any) -> None:
        self.links.append(link)

    def shape(self) -> TraceShape:
        return trace_shape(
            trace_id=self.trace_id,
            spans=tuple(self.spans),
            links=tuple(self.links),
            interrupted=bool(self.interrupted),
            resumed_from=str(self.resumed_from),
        )

    def to_dict(self) -> dict[str, Any]:
        return self.shape().to_dict()


def optional_tracer() -> Any:
    """取当前进程的 tracer。观测层没装或没配置时返回 None，绝不抛。

    沙箱在 execute() 里调它：没有 Collector 是本机常态，不是错误，
    但也不能因此把打点代码全删掉——那样等真接上 Collector 时就没有数据了。
    """
    try:
        from pq_agent.observability import get_tracer

        return get_tracer(default=None)
    except Exception:
        return None


def sandbox_status_holder(result: Any) -> dict[str, str]:
    """把沙箱执行结果翻译成 span 的收尾状态。

    只有 status == "failed"（真的跑了、真的失败了）才标 error。
    被静态层拦下、被策略拒绝、dry-run 都是**设计内的结果**，标成 error 会让
    「沙箱正常工作」在链路上看起来像「沙箱出故障了」。
    """
    status = str(getattr(result, "status", "") or "")
    code = str(getattr(result, "error_code", "") or "")
    if status == "failed":
        return {"status": "error", "error_code": code or "SANDBOX_FAILED"}
    return {"status": "ok", "error_code": ""}


__all__ = [
    "ERROR_SPAN_ATTRIBUTE",
    "SPANS_SCHEMA_VERSION",
    "SpanCollector",
    "approval_span",
    "llm_span",
    "rag_span",
    "run_span",
    "sandbox_span",
    "optional_tracer",
    "sandbox_status_holder",
    "span_scope",
    "tool_span",
]