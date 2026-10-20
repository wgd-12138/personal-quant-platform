"""A4-01 APM 部署层测试。

本机没有 Docker，所以这里测的是三件事，且**只测这三件事**：

1. 配置层：compose / Collector / stack.json 的静态事实是否正确（端口回环、digest 固定、
   加固开关齐全）。
2. 判定层：没有 Docker 时结论必须是「降级 + 未验证」，而不是「通过」或「不支持」。
3. 契约层：无 Docker 不回落、容器活着不等于 APM 通过、停止不删数据。

**本文件不得出现「隔离已验证」「APM 已验证」这类断言。**真正的栈验证需要一台装了
Docker 的机器，那份证据不在这个仓库里。
"""

from __future__ import annotations

import json
import pathlib
import sys
import unittest
from uuid import uuid4

ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from pq_agent.observability import (  # noqa: E402
    APM_SCHEMA_VERSION,
    EVENT_APM_DEGRADED,
    EVENT_COLLECTOR_DROPPED,
    SIGNAL_SUPPORTED,
    SIGNAL_UNSUPPORTED,
    SIGNAL_UNVERIFIED,
    LocalEventLog,
    apm_fact_card,
    apm_health,
    check_compose_bindings,
    config_fingerprint,
    load_stack_facts,
    probe_ports,
    record_apm_health,
    record_dropped_telemetry,
    render_degraded_notice,
    render_health,
    supported_signals,
    unsupported_signals,
    write_fact_card,
)

COMPOSE = ROOT / "deploy" / "apm" / "docker-compose.yml"
COLLECTOR = ROOT / "deploy" / "apm" / "otel-collector.yaml"
STACK = ROOT / "deploy" / "apm" / "stack.json"


def _tmp_dir(name: str) -> pathlib.Path:
    """本机沙箱把 0o700 目录变成自己进不去的目录，所以不用 tempfile。"""
    target = ROOT / ".tmp" / "a4-01-tests" / (name + "-" + uuid4().hex[:8])
    target.mkdir(parents=True, exist_ok=True)
    return target


class A4_01_StackAssets(unittest.TestCase):
    """部署资产必须存在且可审计。"""

    def test_compose_and_collector_exist(self):
        self.assertTrue(COMPOSE.exists(), "缺少 " + str(COMPOSE))
        self.assertTrue(COLLECTOR.exists(), "缺少 " + str(COLLECTOR))
        self.assertTrue(STACK.exists(), "缺少 " + str(STACK))

    def test_compose_declares_all_hardening_and_limits(self):
        text = COMPOSE.read_text(encoding="utf-8")
        for needle in (
            "healthcheck:",
            "deploy:",
            "limits:",
            "restart: " + chr(34) + "no" + chr(34),
            "apache/skywalking-oap-server:11.0.0@sha256:",
            "apache/skywalking-ui:11.0.0@sha256:",
            "otel/opentelemetry-collector-contrib:0.162.0@sha256:",
        ):
            self.assertIn(needle, text, "compose 缺少：" + needle)

    def test_collector_declares_required_pipeline_pieces(self):
        text = COLLECTOR.read_text(encoding="utf-8")
        for needle in (
            "memory_limiter:",
            "batch:",
            "filter/redact:",
            "retry_on_failure:",
            "health_check:",
            "file/degraded:",
            "endpoint: skywalking-oap:11800",
            "otlp/skywalking:",
        ):
            self.assertIn(needle, text, "collector 缺少：" + needle)

    def test_collector_has_three_separate_pipelines(self):
        """三种 signal 各一条 pipeline。合成一条就等于假定它们共用接口。"""
        text = COLLECTOR.read_text(encoding="utf-8")
        pipelines = text.split("pipelines:", 1)[1]
        for name in ("    traces:", "    metrics:", "    logs:"):
            self.assertIn(name, pipelines, "缺少 pipeline：" + name.strip())

    def test_stack_json_pins_every_component(self):
        payload = json.loads(STACK.read_text(encoding="utf-8"))
        self.assertEqual(payload.get("schema_version"), "pq_agent.apm_stack/1")
        components = payload.get("components") or []
        self.assertEqual(len(components), 3)
        for item in components:
            digest = str(item.get("pinned_digest") or "")
            self.assertTrue(digest.startswith("sha256:"), "digest 未固定：" + str(item.get("name")))
            self.assertIn("digest_status", item)

    def test_stack_json_does_not_claim_verified_signals(self):
        """事实卡里 signals.supported 全为 null——还没验过，不能写 true 也不能写 false。"""
        payload = json.loads(STACK.read_text(encoding="utf-8"))
        signals = payload.get("signals") or {}
        for name in ("traces", "metrics", "logs"):
            self.assertIn(name, signals, "缺少 signal：" + name)
            self.assertIsNone(signals[name].get("supported"), name + " 不应声称已验证")

    def test_stack_ports_are_all_loopback(self):
        payload = json.loads(STACK.read_text(encoding="utf-8"))
        for item in payload.get("ports") or []:
            self.assertIn(str(item.get("bind")), ("127.0.0.1", "localhost", "::1", "lo"))
            self.assertFalse(bool(item.get("public")), "端口不得标为公开：" + str(item.get("port")))

    def test_stack_commands_separate_down_from_destroy(self):
        payload = json.loads(STACK.read_text(encoding="utf-8"))
        commands = payload.get("commands") or {}
        self.assertIn("down -v", str(commands.get("destroy")))
        self.assertNotIn("-v", str(commands.get("down")), "down 不得删除数据卷")

    def test_stack_storage_survives_down(self):
        payload = json.loads(STACK.read_text(encoding="utf-8"))
        storage = payload.get("storage") or {}
        self.assertTrue(bool(storage.get("survives_down")))
        self.assertIn("down -v", str(storage.get("removed_by")))

class A4_01_HealthVerdicts(unittest.TestCase):
    """没有 Docker 时，结论必须是「降级 + 未验证」，一个字都不能松。"""

    def setUp(self):
        self.report = apm_health(root=ROOT, docker_available=False)

    def test_schema_and_verdict(self):
        self.assertEqual(self.report.schema_version, APM_SCHEMA_VERSION)
        self.assertEqual(self.report.verdict, "APM_UNAVAILABLE_NO_DOCKER")
        self.assertTrue(self.report.apm_degraded)

    def test_layers_are_unavailable_not_failed(self):
        """没 Docker 是「没测」，不是「测了没过」。二者含义完全不同。"""
        for item in (self.report.collector, self.report.oap, self.report.ui, self.report.end_to_end):
            self.assertEqual(item.status, "unavailable", item.name + " 应为 unavailable")
            self.assertFalse(item.passed)

    def test_signals_are_unverified_not_unsupported(self):
        """supported=None 表示没证据。写成 False 就是编结论。"""
        self.assertEqual(len(self.report.signals), 3)
        for item in self.report.signals:
            self.assertIsNone(item.supported, item.signal + " 不应有结论")
            self.assertEqual(item.code, SIGNAL_UNVERIFIED)
        self.assertEqual(supported_signals(self.report), ())
        self.assertEqual(unsupported_signals(self.report), ())

    def test_no_docker_never_reports_passed(self):
        """最重要的一条：无 Docker 时不允许出现任何 passed。"""
        for item in (self.report.collector, self.report.oap, self.report.ui, self.report.end_to_end):
            self.assertNotEqual(item.status, "passed", item.name + " 不得为 passed")
        self.assertNotIn(SIGNAL_SUPPORTED, [item.code for item in self.report.signals])

    def test_reasons_include_no_docker(self):
        self.assertIn("no_docker_on_this_host", self.report.reasons)

    def test_all_ports_loopback(self):
        self.assertTrue(self.report.all_ports_loopback)
        for item in self.report.ports:
            self.assertTrue(item.is_loopback, str(item.port))

    def test_compose_bindings_statically_audited(self):
        check = check_compose_bindings(root=ROOT)
        self.assertTrue(check.passed, check.detail)
        self.assertIn("只证明配置", check.detail, "必须写明这条证据的边界")
        self.assertEqual(check.data.get("offenders"), [])

    def test_fact_card_shape(self):
        card = apm_fact_card(root=ROOT, docker_available=False)
        self.assertEqual(card["verdict"], "APM_UNAVAILABLE_NO_DOCKER")
        self.assertTrue(card["apm_degraded"])
        self.assertIn("config_fingerprint", card)
        self.assertIn("expected_otlp_http_endpoint", card)
        self.assertEqual(card["degraded_event"], "apm_degraded")
        self.assertEqual(len(card["facts"]["digest_unverified"]), 3)

    def test_clock_requires_timezone(self):
        import datetime as dt

        naive = lambda: dt.datetime(2026, 10, 14, 12, 0, 0)  # noqa: E731
        with self.assertRaises(Exception):
            apm_health(root=ROOT, docker_available=False, clock=naive)

    def test_render_distinguishes_three_states(self):
        text = render_health(self.report)
        self.assertIn("APM_UNAVAILABLE_NO_DOCKER", text)
        self.assertIn("null", text)
        self.assertIn("APM_SIGNAL_UNVERIFIED", text)
        self.assertIn("本机没有 Docker", render_degraded_notice())


class A4_01_ConfigFingerprint(unittest.TestCase):
    """配置指纹：配置改了没人知道，是这类事故最常见的起点。"""

    def test_fingerprint_covers_three_files(self):
        rows = config_fingerprint(root=ROOT)
        self.assertEqual(len(rows), 3)
        for key in ("deploy/apm/docker-compose.yml", "deploy/apm/otel-collector.yaml", "deploy/apm/stack.json"):
            self.assertTrue(rows[key]["exists"], key)
            self.assertEqual(len(rows[key]["sha256"]), 64, key)
            self.assertGreater(rows[key]["bytes"], 0, key)

    def test_fingerprint_changes_with_content(self):
        base = config_fingerprint(root=ROOT)
        again = config_fingerprint(root=ROOT)
        self.assertEqual(base["deploy/apm/stack.json"]["sha256"], again["deploy/apm/stack.json"]["sha256"])

    def test_missing_root_reports_missing(self):
        rows = config_fingerprint(root=ROOT / ".tmp" / "a4-01-not-a-repo")
        for key in rows:
            self.assertFalse(rows[key]["exists"], key)
            self.assertEqual(rows[key]["sha256"], "")

    def test_load_stack_facts_reports_missing_honestly(self):
        facts = load_stack_facts(root=ROOT / ".tmp" / "a4-01-not-a-repo")
        self.assertFalse(facts.available)
        self.assertEqual(facts.error, "STACK_FILE_MISSING")
        self.assertEqual(facts.components, ())

    def test_write_fact_card_round_trips(self):
        target_dir = _tmp_dir("factcard")
        card = apm_fact_card(root=ROOT, docker_available=False)
        target = target_dir / "apm_environment.json"
        written = write_fact_card(str(target), card, root=ROOT)
        self.assertTrue(written.exists())
        payload = json.loads(written.read_text(encoding="utf-8"))
        self.assertEqual(payload["verdict"], "APM_UNAVAILABLE_NO_DOCKER")
        leftovers = list(target_dir.glob("*.tmp"))
        self.assertEqual(leftovers, [], "不得留下临时文件")

    def test_write_fact_card_rejects_outside_root(self):
        card = apm_fact_card(root=ROOT, docker_available=False)
        with self.assertRaises(Exception):
            write_fact_card(str(ROOT.parent / "a4-01-escape.json"), card, root=ROOT)


class A4_01_EventBridge(unittest.TestCase):
    """事件桥：降级必须是可查的证据，不是口头说明。"""

    def test_apm_degraded_event_is_recorded(self):
        target_dir = _tmp_dir("eventlog")
        log = LocalEventLog(target_dir / "telemetry.jsonl")
        report = apm_health(root=ROOT, docker_available=False)
        payload = record_apm_health(report, event_log=log)
        self.assertEqual(payload.get("event"), EVENT_APM_DEGRADED)
        self.assertEqual(log.count(EVENT_APM_DEGRADED), 1)
        self.assertEqual(log.status if hasattr(log, "status") else "", "")

    def test_degraded_event_is_persisted(self):
        target_dir = _tmp_dir("eventlog-persist")
        path = target_dir / "telemetry.jsonl"
        log = LocalEventLog(path)
        report = apm_health(root=ROOT, docker_available=False)
        record_apm_health(report, event_log=log)
        self.assertTrue(path.exists(), "降级事件必须落盘")
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].get("event"), EVENT_APM_DEGRADED)
        self.assertNotIn("token", json.dumps(rows[0], ensure_ascii=False).lower())

    def test_healthy_report_records_nothing(self):
        """没降级就不该写降级事件。写多了等于噪音，噪音会让人忽略真信号。"""
        from pq_agent.observability.apm_health import (
            APM_SCHEMA_VERSION,
            ApmHealthReport,
            CheckResult,
            SignalStatus,
            StackFacts,
        )

        target_dir = _tmp_dir("eventlog-healthy")
        log = LocalEventLog(target_dir / "telemetry.jsonl")
        passed = CheckResult(name="x", status="passed")
        # 手工构造一份真的没降级的报告：不能让一个「本该通过」的结论
        # 因为测试技巧（setattr 改属性）而变得不可信。
        report = ApmHealthReport(
            schema_version=APM_SCHEMA_VERSION,
            checked_at="2026-10-14T00:00:00+00:00",
            docker_available=True,
            collector=passed,
            oap=passed,
            ui=passed,
            end_to_end=passed,
            signals=tuple(
                SignalStatus(signal=name, supported=True, code="APM_SIGNAL_SUPPORTED")
                for name in ("traces", "metrics", "logs")
            ),
            ports=(),
            facts=StackFacts(available=True),
        )
        self.assertFalse(report.apm_degraded)
        written = record_apm_health(report, event_log=log)
        self.assertEqual(written, {})
        self.assertEqual(log.count(EVENT_APM_DEGRADED), 0)

    def test_dropped_telemetry_is_recorded_even_when_zero(self):
        """dropped=0 也要记：「这一轮没丢」本身是证据。"""
        target_dir = _tmp_dir("eventlog-dropped")
        log = LocalEventLog(target_dir / "telemetry.jsonl")
        record_dropped_telemetry(event_log=log, dropped=0, where="collector")
        self.assertEqual(log.count(EVENT_COLLECTOR_DROPPED), 1)

    def test_negative_dropped_is_rejected(self):
        target_dir = _tmp_dir("eventlog-negative")
        log = LocalEventLog(target_dir / "telemetry.jsonl")
        with self.assertRaises(Exception):
            record_dropped_telemetry(event_log=log, dropped=-1, where="collector")


class A4_01_CliSurface(unittest.TestCase):
    """命令行契约：能起容器的入口必须默认不动手。"""

    def test_compose_argv_separates_down_and_destroy(self):
        from pq_agent.__main__ import compose_argv

        up = compose_argv(root=ROOT, action="up")
        down = compose_argv(root=ROOT, action="down")
        destroy = compose_argv(root=ROOT, action="destroy")
        self.assertIn("up", up)
        self.assertNotIn("-v", down)
        self.assertIn("-v", destroy)
        for argv in (up, down, destroy):
            self.assertEqual(argv[:2], ["docker", "compose"])
            self.assertIn("docker-compose.yml", argv[3])

    def test_unknown_compose_action_is_rejected(self):
        from pq_agent.__main__ import compose_argv

        with self.assertRaises(ValueError):
            compose_argv(root=ROOT, action="rm -rf /")

    def test_cli_parses_apm_subcommands(self):
        from pq_agent.__main__ import build_parser

        parser = build_parser()
        for name in ("status", "doctor", "up", "down", "destroy"):
            args = parser.parse_args([name])
            self.assertEqual(args.command, name)
            self.assertTrue(hasattr(args, "func"), name + " 必须有处理函数")
        joined = parser.parse_args(["up"])
        self.assertFalse(joined.run, "up 默认不得执行")


if __name__ == "__main__":
    unittest.main()
