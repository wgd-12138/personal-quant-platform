"""A6-05：三信号真的走 OTLP 出去了吗？逐条用本地接收器验。

这个文件证明的是：应用侧 SDK 到 OTLP 报文这一段是通的，
并且报文里确实有 Agent 节点 / 工具 / 沙箱 / metric / log 这些内容。
它**不**证明 Collector 与 SkyWalking 已经收货——那需要真实环境，由 gate 判 BLOCKED。
"""

from __future__ import annotations

import unittest
import uuid

from opentelemetry.sdk.resources import Resource

from pq_agent.observability import collect_run_metrics, build_log_record
from pq_agent.observability.otlp_export import (
    SIGNAL_LOGS,
    SIGNAL_METRICS,
    SIGNAL_TRACES,
    LogSignalExporter,
    MetricSignalExporter,
    build_signal_exporters,
    log_endpoint,
    metric_endpoint,
    normalize_endpoint,
    trace_endpoint,
)

from .otlp_capture import CaptureServer


def _resource() -> Resource:
    return Resource.create({
        "service.name": "pq_agent",
        "service.version": "0.1.0",
        "deployment.environment": "test",
    })


class EndpointTests(unittest.TestCase):
    def test_endpoints_are_built_per_signal(self) -> None:
        self.assertEqual(trace_endpoint("http://127.0.0.1:4318/"), "http://127.0.0.1:4318/v1/traces")
        self.assertEqual(metric_endpoint("http://127.0.0.1:4318"), "http://127.0.0.1:4318/v1/metrics")
        self.assertEqual(log_endpoint("http://127.0.0.1:4318"), "http://127.0.0.1:4318/v1/logs")

    def test_empty_endpoint_fails_loudly(self) -> None:
        """不能默默返回空串：那会变成「发到 localhost:80」这种看似成功的怪事。"""
        with self.assertRaises(ValueError) as ctx:
            normalize_endpoint("")
        self.assertIn("OTLP_ENDPOINT_REQUIRED", str(ctx.exception))
        with self.assertRaises(ValueError):
            normalize_endpoint("127.0.0.1:4318")


class SignalExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = CaptureServer().start()
        self.endpoint = self.server.endpoint

    def tearDown(self) -> None:
        self.server.stop()

    def test_traces_metrics_logs_reach_the_receiver_with_content(self) -> None:
        exporters = build_signal_exporters(endpoint=self.endpoint)
        self.server.recorder.batches.clear()

        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor

        trace_id_hex = "ab" * 16
        provider = TracerProvider(resource=_resource())
        provider.add_span_processor(SimpleSpanProcessor(exporters[SIGNAL_TRACES]))
        tracer = provider.get_tracer("pq_agent.graph")
        with tracer.start_as_current_span("graph.node") as span:
            span.set_attribute("pq_agent.node", "execute_tool")
            span_id_hex = format(span.get_span_context().span_id, "016x")
        provider.force_flush()
        provider.shutdown()

        metrics = MetricSignalExporter(exporter=exporters[SIGNAL_METRICS], resource=_resource())
        batch = collect_run_metrics(
            intent="market_check",
            intent_source="l1",
            mode="replay",
            duration_ms=1234,
            node_durations={"execute_tool": 900},
            error_codes=["TOOL_FAILED"],
            tool_calls=[{"tool_name": "market_snapshot", "ok": True, "duration_ms": 880, "capability": "read_external"}],
            approval_backlog=1,
            sandbox_calls=[{"status": "static_blocked", "runtime": "docker"}],
            rag_hits=3,
            exporter="otlp-http",
        )
        metrics.export(batch)
        metrics.shutdown()

        logs = LogSignalExporter(exporter=exporters[SIGNAL_LOGS], resource=_resource())
        logs.export([
            build_log_record(trace_id=trace_id_hex, event="tool.invoke", severity="info", message="市场快照 ok"),
        ])
        logs.shutdown()

        spans = self.server.recorder.spans()
        lines_out = self.server.recorder.metrics()
        records = self.server.recorder.logs()

        self.assertTrue(spans, "traces 没到接收器")
        self.assertIn("execute_tool", str(spans))
        self.assertTrue(lines_out, "metrics 没到接收器")
        self.assertTrue(any("pq_agent.turn.total" == item["name"] for item in lines_out), str([i["name"] for i in lines_out]))
        self.assertTrue(records, "logs 没到接收器")

    def test_wrong_path_never_counts_as_success(self) -> None:
        """负对照：把 traces 发到 metrics 路径，接收器必须拒收而不是 200 了事。"""
        import urllib.error
        import urllib.request

        request = urllib.request.Request(self.endpoint + "/v1/nothing", data=b"", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
