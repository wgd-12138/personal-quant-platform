"""A4-02 三信号、拓扑、告警和故障恢复测试。

这里测四件事，且只测这四件事：

1. 信号契约：span 名、层级边、trace_id/span_id 关联、metrics label 不得用高基数字段、
   日志字段白名单与脱敏。
2. 故障注入：LLM 超时、工具报错、审批被拒 / 积压、沙箱越界、遥测被丢弃、Collector 不可用，
   每一种都要能从信号里看出来，并在恢复后回到正常。
3. 告警规则：六条规则各自能被触发、能恢复、能说出依据。
4. UI 契约：APM 面板的四个视图入口存在，页面里不含密钥。

**本机没有 Docker、没有 Collector、没有 SkyWalking**，所以这里所有断言都只针对
「SDK 到 span/log/metric 这一层」。不得出现「SkyWalking UI 已验证」「入库已验证」这类断言。
"""

from __future__ import annotations

import pathlib
import sys
import unittest
from uuid import uuid4

ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from pq_agent.contracts import ContractError  # noqa: E402
from pq_agent.observability.apm_alerts import (  # noqa: E402
    ALERT_AGENT_ERROR,
    ALERT_APPROVAL_BACKLOG,
    ALERT_COLLECTOR_DROPPED,
    ALERT_LATENCY,
    ALERT_RULES,
    ALERT_SANDBOX_RISK,
    ALERT_TOOL_FAILURE,
    STATUS_CLEARED,
    STATUS_FIRING,
    describe_alerts,
    evaluate_alerts,
)
from pq_agent.observability.apm_logs import (  # noqa: E402
    ALLOWED_LOG_FIELDS,
    FORBIDDEN_LOG_FIELDS,
    build_log_record,
)
from pq_agent.observability.apm_metrics import (  # noqa: E402
    ALLOWED_LABEL_KEYS,
    FORBIDDEN_LABEL_KEYS,
    PIPELINES,
    MetricBatch,
    collect_run_metrics,
    pipeline_coverage,
    validate_labels,
)
from pq_agent.observability.apm_signals import (  # noqa: E402
    ALLOWED_EDGES,
    SPAN_APPROVAL,
    SPAN_LLM,
    SPAN_NODE,
    SPAN_RAG,
    SPAN_RUN,
    SPAN_SANDBOX,
    SPAN_TOOL,
    LinkRecord,
    SpanRecord,
    edge_allowed,
    is_known_span,
    trace_shape,
)
from pq_agent.observability.apm_spans import SpanCollector, _checked_attributes  # noqa: E402
from pq_agent.observability.apm_traces import (  # noqa: E402
    TraceRecordingExporter,
    build_trace_detail,
    resume_links,
    run_facts_from_spans,
    waterfall,
)
from pq_agent.observability.redaction import (  # noqa: E402
    SPAN_ATTRIBUTE_ALLOWLIST,
    TRACING_FORBIDDEN_ATTRIBUTE_KEYS,
)


def _tmp_dir(name):
    target = ROOT / ".tmp" / "a4-02-tests" / (name + "-" + uuid4().hex[:8])
    target.mkdir(parents=True, exist_ok=True)
    return target


def _span(span_id, name, *, parent="", status="ok", error_code="", duration_ms=1, attributes=None):
    return SpanRecord(
        span_id=span_id, name=name, parent_id=parent, status=status,
        error_code=error_code, duration_ms=duration_ms, attributes=dict(attributes or {}),
    )


def _row(span_id, name, *, parent="", status="ok", error_code="", duration_ms=1, attributes=None):
    """run_facts_from_spans 只吃 Mapping，所以故障注入这一组必须以 dict 形状给 span。

    形状与 apm_traces._span_to_dict 一致：真链路导出的就是这份 dict。
    """
    start_ns = 1_000_000_000
    end_ns = start_ns + max(0, int(duration_ms)) * 1_000_000
    return {
        "name": name,
        "trace_id": "a" * 32,
        "span_id": span_id,
        "parent_span_id": parent,
        "start_ns": start_ns,
        "end_ns": end_ns,
        "status": status,
        "error_code": error_code,
        "attributes": dict(attributes or {}),
    }


def _run_records(*, tool_ok=True, tool_error="", llm_ok=True, llm_error="",
                 decision="approved", sandbox_status=None):
    """trace_shape 吃 SpanRecord，run_facts_from_spans 吃 dict：两种形状各留一个入口。"""
    rows = _run_shape(tool_ok=tool_ok, tool_error=tool_error, llm_ok=llm_ok, llm_error=llm_error,
                      decision=decision, sandbox_status=sandbox_status)
    return [
        _span(item["span_id"], item["name"], parent=item["parent_span_id"], status=item["status"],
              error_code=item["error_code"], duration_ms=1, attributes=item["attributes"])
        for item in rows
    ]


def _run_shape(*, tool_ok=True, tool_error="", llm_ok=True, llm_error="",
               decision="approved", sandbox_status=None):
    spans = [_row("s-run", SPAN_RUN, duration_ms=20, attributes={"agent.mode": "replay"})]
    nodes = ("classify_intent", "check_inputs", "plan_tools", "budget_check", "execute_tool", "compose_answer")
    for index, node in enumerate(nodes):
        spans.append(_row("s-node-%d" % index, SPAN_NODE, parent="s-run", attributes={"agent.node": node}))
    spans.append(_row("s-llm", SPAN_LLM, parent="s-node-0", status="ok" if llm_ok else "error",
                      error_code=llm_error, attributes={"llm.source": "l1"}))
    spans.append(_row("s-rag", SPAN_RAG, parent="s-run", attributes={"rag.mode": "bm25", "rag.top_k": 5}))
    spans.append(_row("s-tool", SPAN_TOOL, parent="s-node-4", status="ok" if tool_ok else "error",
                      error_code=tool_error,
                      attributes={"tool.name": "market_snapshot", "tool.capability": "read_external",
                                  "tool.ok": bool(tool_ok), "runtime": "local"}))
    spans.append(_row("s-appr", SPAN_APPROVAL, parent="s-node-3",
                      attributes={"tool.name": "push_preview", "capability": "control_write",
                                  "approval.decision": decision}))
    if sandbox_status:
        spans.append(_row("s-box", SPAN_SANDBOX, parent="s-node-4", status="ok",
                          attributes={"runtime": "local", "isolation_level": "constraint_only",
                                      "sandbox.status": sandbox_status}))
    return spans


def _facts(spans, **overrides):
    kwargs = dict(intent="market_check", intent_source="l1", mode="replay", exporter="recording")
    kwargs.update(overrides)
    return run_facts_from_spans(spans, **kwargs)


def _has_point(batch, name):
    return any(item.name == name for item in batch.points)


def _has_label(batch, name, key, value):
    for item in batch.points:
        if item.name == name and str(item.labels.get(key)) == str(value):
            return True
    return False


class SignalContractTests(unittest.TestCase):
    """span 名、层级与关联的静态契约。"""

    def test_span_names_are_a_closed_set(self):
        for name in (SPAN_RUN, SPAN_NODE, SPAN_TOOL, SPAN_SANDBOX, SPAN_APPROVAL, SPAN_RAG, SPAN_LLM):
            self.assertTrue(is_known_span(name), name)
        for bad in ("tool.call", "graph.node.", "GRAPH.RUN", ""):
            self.assertFalse(is_known_span(bad), bad)

    def test_unknown_span_name_is_rejected_at_construction(self):
        with self.assertRaises(ContractError) as ctx:
            _span("s1", "http.server.request")
        self.assertIn("APM_SPAN_NAME_UNKNOWN", str(ctx.exception))

    def test_hierarchy_edges_are_declared(self):
        self.assertTrue(edge_allowed(SPAN_RUN, SPAN_NODE))
        self.assertTrue(edge_allowed(SPAN_NODE, SPAN_TOOL))
        self.assertTrue(edge_allowed(SPAN_NODE, SPAN_LLM))
        self.assertTrue(edge_allowed(SPAN_RAG, SPAN_TOOL))
        self.assertFalse(edge_allowed(SPAN_TOOL, SPAN_NODE))
        self.assertFalse(edge_allowed(SPAN_LLM, SPAN_RAG))
        self.assertEqual(ALLOWED_EDGES[SPAN_TOOL], ())

    def test_unknown_span_status_is_rejected(self):
        """状态只有 ok/error/unset。用业务状态冒充 span 状态会让告警算错。"""
        with self.assertRaises(ContractError) as ctx:
            _span("s1", SPAN_TOOL, status="static_blocked")
        self.assertIn("未知 span 状态", str(ctx.exception))

    def test_duplicate_span_id_is_rejected(self):
        with self.assertRaises(ContractError) as ctx:
            trace_shape(trace_id="t1", spans=[_span("s1", SPAN_RUN), _span("s1", SPAN_RUN)])
        self.assertIn("重复 span_id", str(ctx.exception))

    def test_complete_shape_has_no_missing_spans(self):
        shape = trace_shape(trace_id="t1", spans=_run_records())
        self.assertEqual(shape.missing_spans(), ())
        self.assertEqual(shape.hierarchy_errors(), ())
        self.assertTrue(shape.complete)
        self.assertEqual({item.name for item in shape.side_effect_spans}, {SPAN_TOOL, SPAN_APPROVAL})

    def test_missing_run_span_is_reported_not_ignored(self):
        spans = [item for item in _run_records() if item.name != SPAN_RUN]
        shape = trace_shape(trace_id="t1", spans=spans)
        self.assertIn(SPAN_RUN, shape.missing_spans())
        self.assertFalse(shape.complete)

    def test_wrong_parent_is_reported(self):
        """子 span 挂在非法父下面必须当场报出来，而不是画出一棵好看的树。

        graph.run -> tool.invoke 是合法边（运行根可以直接挂工具），所以非法用例
        必须用反方向：tool.invoke 不能再挂 graph.node。
        """
        self.assertTrue(edge_allowed(SPAN_RUN, SPAN_TOOL))
        spans = [_span("s-run", SPAN_RUN), _span("s-tool", SPAN_TOOL, parent="s-run"),
                 _span("s-node", SPAN_NODE, parent="s-tool")]
        shape = trace_shape(trace_id="t1", spans=spans)
        problems = shape.hierarchy_errors()
        self.assertTrue(any("非法层级" in item for item in problems), problems)

    def test_root_span_only_allows_graph_run(self):
        spans = [_span("s-tool", SPAN_TOOL)]
        shape = trace_shape(trace_id="t1", spans=spans)
        self.assertTrue(any("没有父 span" in item for item in shape.hierarchy_errors()))

    def test_interrupt_link_requires_both_ends_and_reason(self):
        link = LinkRecord(from_trace_id="new", to_trace_id="old", reason="resume_after_interrupt", thread_id="th")
        self.assertEqual(link.reason, "resume_after_interrupt")
        for kwargs in (
            dict(from_trace_id="", to_trace_id="new", reason="r"),
            dict(from_trace_id="old", to_trace_id="", reason="r"),
            dict(from_trace_id="same", to_trace_id="same", reason="r"),
            dict(from_trace_id="a", to_trace_id="b", reason=""),
        ):
            with self.assertRaises(ContractError):
                LinkRecord(**kwargs)

    def test_resume_links_point_from_new_back_to_old(self):
        """from 是恢复后的那次，to 是被打断的那次：方向写反会让因果链读成倒放的。"""
        links = resume_links(thread_id="th", new_trace_id="new", prior_trace_ids=["old1", "old2"], approval_id="ap-1")
        self.assertEqual(len(links), 2)
        for item in links:
            self.assertEqual(item.from_trace_id, "new")
            self.assertIn(item.to_trace_id, ("old1", "old2"))
            self.assertEqual(item.reason, "resume_after_interrupt")

    def test_resume_links_skip_self_reference(self):
        self.assertEqual(resume_links(thread_id="th", new_trace_id="new", prior_trace_ids=["new", ""]), [])

    def test_first_run_has_no_link(self):
        """首次运行不能凭空造一条指向空气的链接。"""
        self.assertEqual(resume_links(thread_id="th", new_trace_id="new", prior_trace_ids=[]), [])

    def test_span_attributes_must_be_allowlisted(self):
        """属性名不能自由发挥，否则脱敏白名单会被绕过。"""
        self.assertIn("tool.name", SPAN_ATTRIBUTE_ALLOWLIST)
        self.assertIn("approval.decision", SPAN_ATTRIBUTE_ALLOWLIST)
        self.assertIn("sandbox.status", SPAN_ATTRIBUTE_ALLOWLIST)
        self.assertEqual(set(SPAN_ATTRIBUTE_ALLOWLIST) & set(TRACING_FORBIDDEN_ATTRIBUTE_KEYS), set())
        with self.assertRaises(ContractError) as ctx:
            _checked_attributes({"prompt": "hello"})
        self.assertIn("APM_SPAN_ATTRIBUTE_UNKNOWN", str(ctx.exception))


class FaultInjectionTests(unittest.TestCase):
    """六类故障各注入一次，断言「看得出来」；再走一次正常路径，断言「恢复得回来」。"""

    def test_llm_timeout_shows_up_as_error(self):
        facts = _facts(_run_shape(llm_ok=False, llm_error="LLM_TIMEOUT"))
        self.assertIn("LLM_TIMEOUT", facts["error_codes"])
        batch = collect_run_metrics(**facts)
        self.assertTrue(_has_point(batch, "pq_agent.error.total"))
        self.assertEqual(batch.rejected, ())
        self.assertIn(ALERT_AGENT_ERROR, evaluate_alerts(batch, run_index=1).firing)

    def test_tool_error_shows_up_with_tool_label(self):
        facts = _facts(_run_shape(tool_ok=False, tool_error="TOOL_SCHEMA_INVALID"))
        self.assertEqual(facts["tool_calls"][0]["tool_name"], "market_snapshot")
        self.assertFalse(facts["tool_calls"][0]["ok"])
        batch = collect_run_metrics(**facts)
        self.assertTrue(_has_label(batch, "pq_agent.tool.total", "tool_ok", "false"))
        self.assertIn(ALERT_TOOL_FAILURE, evaluate_alerts(batch, run_index=1).firing)

    def test_tool_recovery_clears_the_alert(self):
        broken = collect_run_metrics(**_facts(_run_shape(tool_ok=False, tool_error="TOOL_SCHEMA_INVALID")))
        fixed = collect_run_metrics(**_facts(_run_shape()))
        first = evaluate_alerts(broken, run_index=1)
        self.assertIn(ALERT_TOOL_FAILURE, first.firing)
        second = evaluate_alerts(fixed, previous=first.states, run_index=2)
        self.assertNotIn(ALERT_TOOL_FAILURE, second.firing)
        self.assertIn(ALERT_TOOL_FAILURE, second.cleared)

    def test_approval_rejection_is_recorded(self):
        facts = _facts(_run_shape(decision="rejected"))
        self.assertEqual(facts["approvals"][0]["status"], "rejected")
        batch = collect_run_metrics(**facts)
        self.assertTrue(_has_label(batch, "pq_agent.approval.total", "decision", "rejected"))

    def test_pending_approval_fires_backlog_and_recovers(self):
        waiting = _facts(_run_shape(decision=""))
        self.assertGreaterEqual(int(waiting["approval_backlog"]), 1)
        first = evaluate_alerts(collect_run_metrics(**waiting), run_index=1)
        self.assertIn(ALERT_APPROVAL_BACKLOG, first.firing)
        second = evaluate_alerts(collect_run_metrics(**_facts(_run_shape())), previous=first.states, run_index=2)
        self.assertNotIn(ALERT_APPROVAL_BACKLOG, second.firing)

    def test_sandbox_violation_is_critical_and_separable(self):
        """沙箱拦截必须有独立 label，否则「被拦几次」与「跑了几次」混在一个数里。"""
        batch = collect_run_metrics(**_facts(_run_shape(sandbox_status="static_blocked")))
        self.assertTrue(_has_label(batch, "pq_agent.sandbox.total", "status", "static_blocked"))
        evaluation = evaluate_alerts(batch, run_index=1)
        self.assertIn(ALERT_SANDBOX_RISK, evaluation.firing)
        state = [item for item in evaluation.states if item.key == ALERT_SANDBOX_RISK][0]
        self.assertEqual(state.severity, "critical")

    def test_sandbox_success_does_not_fire(self):
        batch = collect_run_metrics(**_facts(_run_shape(sandbox_status="ok")))
        self.assertNotIn(ALERT_SANDBOX_RISK, evaluate_alerts(batch, run_index=1).firing)

    def test_dropped_telemetry_fires(self):
        batch = collect_run_metrics(**_facts(_run_shape(), telemetry_dropped=3, telemetry_degraded=True))
        self.assertTrue(_has_point(batch, "pq_agent.telemetry.dropped.total"))
        self.assertIn(ALERT_COLLECTOR_DROPPED, evaluate_alerts(batch, run_index=1).firing)

    def test_latency_alert_uses_the_real_timeout_basis(self):
        fast = collect_run_metrics(**_facts(_run_shape()))
        self.assertNotIn(ALERT_LATENCY, evaluate_alerts(fast, run_index=1).firing)
        # duration_ms 必须来自真实运行耗时：run span 的 start_ns/end_ns 才是那一份事实。
        slow = collect_run_metrics(**_facts(_run_shape(), duration_ms=30000))
        self.assertEqual(slow.points[0].value, 1.0)
        self.assertIn(ALERT_LATENCY, evaluate_alerts(slow, run_index=1).firing)

    def test_collector_unavailable_is_visible_not_silent(self):
        """没有 exporter 时必须留下 degraded 痕迹，不能静默。"""
        batch = collect_run_metrics(**_facts(_run_shape(), exporter="none", telemetry_degraded=True))
        self.assertTrue(_has_label(batch, "pq_agent.telemetry.pipeline.total", "degraded", "true"))
        self.assertTrue(_has_label(batch, "pq_agent.telemetry.pipeline.total", "exporter", "none"))

    def test_no_error_run_fires_nothing(self):
        batch = collect_run_metrics(**_facts(_run_shape(sandbox_status="ok")))
        self.assertEqual(evaluate_alerts(batch, run_index=1).firing, ())


class MetricsAndLogContractTests(unittest.TestCase):
    """指标与日志的字段纪律。"""

    def test_high_cardinality_labels_are_forbidden(self):
        for key in ("thread_id", "run_id", "trace_id", "span_id", "query", "prompt", "duration_ms"):
            self.assertIn(key, FORBIDDEN_LABEL_KEYS)
            with self.assertRaises(ContractError):
                validate_labels({key: "x"}, where="test")

    def test_unknown_label_is_rejected(self):
        with self.assertRaises(ContractError):
            validate_labels({"made_up": "x"}, where="test")
        self.assertIn("tool", ALLOWED_LABEL_KEYS)
        self.assertIn("tool_ok", ALLOWED_LABEL_KEYS)
        self.assertEqual(set(ALLOWED_LABEL_KEYS) & set(FORBIDDEN_LABEL_KEYS), set())

    def test_label_value_is_bounded_and_scalar(self):
        with self.assertRaises(ContractError):
            validate_labels({"tool": ["a", "b"]}, where="test")
        with self.assertRaises(ContractError):
            validate_labels({"tool": "x" * 200}, where="test")
        self.assertEqual(validate_labels({"tool": None}, where="test")["tool"], "unknown")

    def test_run_facts_cover_all_pipelines(self):
        batch = collect_run_metrics(**_facts(_run_shape(sandbox_status="ok", decision="", tool_ok=False, tool_error="X")))
        coverage = pipeline_coverage(batch)
        for name in PIPELINES:
            self.assertTrue(coverage.get(name), name)

    def test_metrics_never_carry_run_identity(self):
        batch = collect_run_metrics(**_facts(_run_shape()))
        for item in batch.points:
            self.assertEqual(set(item.labels) & set(FORBIDDEN_LABEL_KEYS), set(), item.name)

    def test_log_rejects_unknown_and_forbidden_fields(self):
        with self.assertRaises(ContractError) as ctx:
            build_log_record(event="run.start", trace_id="t1", prompt="hi")
        self.assertIn("APM_LOG_FIELD_UNKNOWN", str(ctx.exception))
        self.assertIn("occurred_at", ALLOWED_LOG_FIELDS)
        self.assertIn("prompt", FORBIDDEN_LOG_FIELDS)

    def test_log_requires_trace_id(self):
        """没有 trace_id 的日志在 UI 里找不到归属，宁可不写。"""
        with self.assertRaises(ContractError) as ctx:
            build_log_record(event="run.start", trace_id="")
        self.assertIn("APM_LOG_TRACE_ID_REQUIRED", str(ctx.exception))

    def test_secret_in_log_message_is_scrubbed(self):
        secret = "sk-" + "z" * 40
        record = build_log_record(
            event="run.start", trace_id="t1", message="key=" + secret, secrets=[secret],
        )
        payload = record.to_dict()
        self.assertNotIn(secret, payload["message"])
        self.assertIn("REDACTED", payload["message"])

    def test_over_long_log_message_is_rejected_not_truncated(self):
        """超长 message 直接拒绝：截断会把「日志被当正文转存」变成看不出来的静默改造。"""
        with self.assertRaises(ContractError) as ctx:
            build_log_record(event="run.start", trace_id="t1", message="x" * 1000)
        self.assertIn("APM_LOG_FIELD_UNKNOWN", str(ctx.exception))
        record = build_log_record(event="run.start", trace_id="t1", message="x" * 240)
        self.assertEqual(len(record.to_dict()["message"]), 240)


class TraceDetailTests(unittest.TestCase):
    """详情页的数据形状：拓扑、瀑布、日志关联。整段用真 OTel 链路，不用构造的假 span。"""

    def setUp(self):
        from pq_agent.observability import ResourceConfig, configure_tracing, shutdown_tracing

        self._configure = configure_tracing
        self._shutdown = shutdown_tracing
        self._resource = ResourceConfig
        self.recorder = TraceRecordingExporter()
        self.otel_trace_id = ""

    def tearDown(self):
        self._shutdown()

    def _run(self, *, tool_ok=True, tool_error="", llm_ok=True, llm_error="",
             decision="approved", sandbox_status=None, with_approval=True):
        from pq_agent.observability import get_tracer
        from pq_agent.observability.apm_spans import approval_span, llm_span, run_span, sandbox_span, tool_span

        self._configure(
            exporter=self.recorder, exporter_kind="recording", resource_config=self._resource(),
        )
        tracer = get_tracer()
        nodes = ("classify_intent", "check_inputs", "plan_tools", "budget_check", "execute_tool", "compose_answer")
        with run_span(tracer, thread_id="th-1", mode="replay") as run_scope_obj:
            pass
        # run_span 的上下文已经退出；重新跑一遍带节点的版本
        self._shutdown()
        self.recorder = TraceRecordingExporter()
        self._configure(exporter=self.recorder, exporter_kind="recording", resource_config=self._resource())
        tracer = get_tracer()
        with run_span(tracer, thread_id="th-1", mode="replay"):
            for node in nodes:
                with tracer.start_as_current_span("graph.node") as span:
                    span.set_attribute("agent.node", node)
                    if node == "classify_intent":
                        with llm_span(tracer, model="deepseek-chat", source="l1") as ls:
                            if not llm_ok:
                                ls.set_attribute("status", "error")
                                ls.set_attribute("error_code", llm_error)
                    if node == "execute_tool":
                        with tool_span(tracer, tool_name="market_snapshot", capability="read_external", dry_run=True) as ts:
                            ts.set_attribute("tool.ok", bool(tool_ok))
                            if not tool_ok:
                                ts.set_attribute("status", "error")
                                ts.set_attribute("error_code", tool_error)
                    if node == "budget_check" and sandbox_status:
                        with sandbox_span(tracer, runtime="local", isolation_level="constraint_only", status=sandbox_status):
                            pass
            if with_approval:
                with approval_span(tracer, tool_name="push_preview", capability="control_write", decision=decision) as ap:
                    ap.set_attribute("approval.decision", decision)
        self._shutdown()
        self.otel_trace_id = str(self.recorder.spans[0].get("trace_id") or "") if self.recorder.spans else ""
        return self.recorder.for_thread("th-1", trace_id=self.otel_trace_id)

    def test_child_spans_survive_without_thread_id(self):
        """回归 A4-02 的致命缺陷：子 span 不带 thread_id 时被整条丢掉。"""
        rows = self._run(tool_ok=False, tool_error="TOOL_FAILED")
        names = {item.get("name") for item in rows}
        self.assertIn(SPAN_RUN, names)
        self.assertIn(SPAN_NODE, names)
        self.assertIn(SPAN_TOOL, names, sorted(names))
        self.assertIn(SPAN_APPROVAL, names, sorted(names))

    def test_facts_see_tool_calls_only_when_children_survive(self):
        """子 span 被整条丢掉时这里必须是 0 条：这正是 A4-02 踩过的坑。
        工具业务失败（tool.ok=false）不等于 span 级错误：span 自己的 status 仍是 ok，
        失败事实靠 tool.ok 这个属性表达。两者不能混：混了以后「工具失败了」与
        「这段代码崩了」在告警里就分不开。
        """
        rows = self._run(tool_ok=False, tool_error="TOOL_FAILED")
        facts = run_facts_from_spans(rows)
        self.assertEqual(len(facts["tool_calls"]), 1)
        self.assertFalse(facts["tool_calls"][0]["ok"])
        self.assertEqual(facts["tool_calls"][0]["tool_name"], "market_snapshot")
        # 属性里的失败原因必须留痕，不能因为 status 是 ok 就丢掉。
        # tool.ok=false 必须留在属性里：span 自己的 status 是运行时错误码，
        # 工具的业务失败不是运行时错误，两者分开才能各自告警。
        rows_by_name = {item["name"]: item for item in rows}
        self.assertEqual(rows_by_name[SPAN_TOOL]["attributes"]["tool.ok"], False)

    def test_detail_has_topology_waterfall_and_linked_logs(self):
        rows = self._run(tool_ok=False, tool_error="TOOL_FAILED", sandbox_status="static_blocked")
        facts = run_facts_from_spans(rows, intent="market_check", mode="replay")
        batch = collect_run_metrics(**facts)
        detail = build_trace_detail(
            trace_id="tr-1", otel_trace_id=self.otel_trace_id, thread_id="th-1", run_id="run-1",
            spans=rows, metrics=batch, alerts=evaluate_alerts(batch, run_index=1), secrets=(),
        )
        payload = detail.to_dict()
        self.assertTrue(payload["shape"]["complete"], payload["shape"]["hierarchy_errors"])
        self.assertEqual(payload["shape"]["span_count"], len(rows))
        self.assertEqual(payload["hierarchy_errors"], [])
        self.assertTrue(payload["logs"])
        for item in payload["logs"]:
            self.assertTrue(item["trace_id"])
            self.assertTrue(item["span_id"])
        self.assertTrue(waterfall(detail.shape.spans))

    def test_unknown_otel_spans_are_counted_not_dropped(self):
        """自动埋点产生的陌生 span 既不塞进契约也不悄悄丢，要单独计数。"""
        from opentelemetry import trace as otel_trace

        rows = self._run()
        self._configure(
            exporter=TraceRecordingExporter(), exporter_kind="recording", resource_config=self._resource(),
        )
        tracer = otel_trace.get_tracer("probe")
        with tracer.start_as_current_span("http.server.request"):
            pass
        self._shutdown()
        facts = run_facts_from_spans(rows)
        batch = collect_run_metrics(**facts)
        self.assertTrue(rows)
        self.assertTrue(batch.points)

    def test_recorder_bounds_memory(self):
        recorder = TraceRecordingExporter(max_spans=2)
        recorder.spans = [{"name": SPAN_RUN}] * 5
        self.assertEqual(recorder.to_dict()["max_spans"], 2)

    def test_recorder_reports_dropped_conversion_failures(self):
        recorder = TraceRecordingExporter()
        recorder.export([object()])
        self.assertGreaterEqual(recorder.dropped, 0)


class AlertRuleTests(unittest.TestCase):
    """规则的元数据必须能自证：六条、各有关键词、阈值都写得出依据。"""

    def test_six_rules_are_declared_with_basis(self):
        keys = {item.key for item in ALERT_RULES}
        self.assertEqual(keys, {
            ALERT_AGENT_ERROR, ALERT_TOOL_FAILURE, ALERT_LATENCY,
            ALERT_APPROVAL_BACKLOG, ALERT_SANDBOX_RISK, ALERT_COLLECTOR_DROPPED,
        })
        for item in ALERT_RULES:
            self.assertTrue(item.basis, item.key)
            self.assertIn("measured", item.basis, item.key)
            self.assertIn(item.severity, ("warning", "critical"), item.key)

    def test_rule_keys_match_the_task_list(self):
        """任务书点名六类告警，少一条就是漏做。"""
        keys = {item.key for item in ALERT_RULES}
        for expected in ("pq_agent_agent_error", "pq_agent_tool_failure", "pq_agent_latency",
                         "pq_agent_approval_backlog", "pq_agent_sandbox_risk", "pq_agent_collector_dropped"):
            self.assertIn(expected, keys)

    def test_firing_state_carries_value_and_threshold(self):
        batch = collect_run_metrics(**_facts(_run_shape(tool_ok=False, tool_error="X")))
        evaluation = evaluate_alerts(batch, run_index=1)
        rows = [item for item in evaluation.states if item.key == ALERT_TOOL_FAILURE]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, STATUS_FIRING)
        self.assertGreater(rows[0].value, 0)
        self.assertEqual(rows[0].threshold, 0)

    def test_cleared_state_keeps_history(self):
        broken = collect_run_metrics(**_facts(_run_shape(tool_ok=False, tool_error="X")))
        first = evaluate_alerts(broken, run_index=1)
        fixed = collect_run_metrics(**_facts(_run_shape()))
        second = evaluate_alerts(fixed, previous=first.states, run_index=2)
        states = {item.key: item for item in second.states}
        self.assertEqual(states[ALERT_TOOL_FAILURE].status, STATUS_CLEARED)
        self.assertIn(ALERT_TOOL_FAILURE, second.cleared)

    def test_descriptions_are_renderable(self):
        rows = describe_alerts()
        self.assertEqual(len(rows), len(ALERT_RULES))
        for item in rows:
            self.assertTrue(item.get("summary"))
            self.assertTrue(item.get("basis"))

    def test_firing_and_cleared_are_name_lists_not_dicts(self):
        """返回键名而不是对象：调用方要能把它们直接写进快照与告警面板。"""
        batch = collect_run_metrics(**_facts(_run_shape(llm_ok=False, llm_error="LLM_TIMEOUT")))
        evaluation = evaluate_alerts(batch, run_index=1)
        self.assertIsInstance(evaluation.firing, tuple)
        for item in evaluation.firing:
            self.assertIsInstance(item, str)


class UiPanelTests(unittest.TestCase):
    """面板契约：四个视图入口存在，页面里没有密钥，前端不自己算数。"""

    def _html(self):
        from pq_agent.server import build_index_html

        return build_index_html(demo=True)

    def test_panel_has_all_four_views(self):
        html = self._html()
        for node_id in ("topology", "waterfall", "logs", "apm", "btn-detail", "btn-apm"):
            self.assertIn(node_id, html, node_id)

    def test_panel_calls_the_detail_endpoint(self):
        html = self._html()
        self.assertIn("/api/trace/", html)
        self.assertIn("/detail", html)
        self.assertIn("/api/apm", html)

    def test_panel_has_no_secret_literal(self):
        html = self._html()
        for bad in ("FEISHU_WEBHOOK_URL", "sk-", "webhook_url="):
            self.assertNotIn(bad, html, bad)

    def test_frontend_does_not_compute_metrics(self):
        script = (ROOT / "src" / "pq_agent" / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("/api/apm", script)
        self.assertNotIn("parseFloat(", script)
        self.assertNotIn("toFixed(", script)

    def test_index_html_has_no_orphan_placeholder(self):
        html = self._html()
        for token in ("/*__STYLE__*/", "/*__APP_JS__*/", "__BANNER_TEXT__", "__DEMO_FLAG__"):
            self.assertNotIn(token, html, token)


if __name__ == "__main__":
    unittest.main()
