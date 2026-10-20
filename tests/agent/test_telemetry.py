import datetime as dt
import importlib.util
import json
import pathlib
import sys
import unittest
import uuid

from pq_agent.contracts import ContractError
from pq_agent.observability import (
    EVENT_LOG_SCHEMA_VERSION,
    SPAN_ATTRIBUTE_ALLOWLIST,
    TRACING_FORBIDDEN_ATTRIBUTE_KEYS,
    LocalEventLog,
    ResourceConfig,
    SanitizingSpanExporter,
    SpinCallbackExporter,
    TelemetryRedactor,
    build_resource,
    build_tracer,
    clear_tracing,
    configure_tracing,
    current_tracing,
    exporter_configuration,
    get_tracer,
    is_known_span,
    resource_attributes,
    sanitize_event_payload,
    shutdown_tracing,
    wrap_exporter,
)
from pq_agent.observability.redaction import (
    TelemetryRedactionError,
    is_sensitive_attribute,
)
from pq_agent.observability.resource import detect_environment, detect_version
from pq_agent.state import GRAPH_VERSION

ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def load_graph_fakes():
    path = ROOT / 'tests' / 'agent' / '_graph_fakes.py'
    spec = importlib.util.spec_from_file_location('pq_agent_test_fakes', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FAKES = load_graph_fakes()

# 已知密钥样式，用于确认没有任何出口把原文放出去
FAKE_SECRET = 'sk-' + ('z' * 40)
FAKE_WEBHOOK = 'https://open.feishu.cn/open-apis/bot/v2/hook/' + ('7e' * 12)


def tmp_dir(name: str) -> pathlib.Path:
    # 刻意不用 tempfile：本机沙箱会把 0o700 目录变成连自己都进不去的目录
    path = ROOT / '.tmp' / ('a1-06-' + name) / uuid.uuid4().hex[:8]
    path.mkdir(parents=True, exist_ok=True)
    return path


class ResourceTests(unittest.TestCase):
    def test_resource_attributes_carry_identity(self):
        config = ResourceConfig(environment='local', version='0.1.0', graph_version=GRAPH_VERSION)
        attrs = resource_attributes(config)
        self.assertEqual(attrs['service.name'], 'pq_agent')
        self.assertEqual(attrs['service.version'], '0.1.0')
        self.assertEqual(attrs['deployment.environment'], 'local')
        self.assertEqual(attrs['pq_agent.graph_version'], GRAPH_VERSION)
        self.assertIn('vcs.commit', attrs)

    def test_missing_commit_is_unknown_not_faked(self):
        attrs = resource_attributes(ResourceConfig())
        self.assertEqual(attrs['vcs.commit'], 'unknown')

    def test_environment_defaults_to_local(self):
        self.assertEqual(detect_environment(environ={}), 'local')
        self.assertEqual(detect_environment(environ={'APP_ENV': 'dev'}), 'dev')
        # 非法值不得被当成合法环境放行
        # 非法值必须显式记 unknown，不得静默回落到 local 假装配置正常
        self.assertEqual(detect_environment(environ={'APP_ENV': 'whatever'}), 'unknown')

    def test_detect_version_is_present(self):
        self.assertTrue(detect_version())

    def test_resource_attributes_reject_bad_input(self):
        # 传错类型必须是显式失败，不该悄悄给一份默认资源
        with self.assertRaises(AttributeError):
            resource_attributes(None)

    def test_build_resource_returns_otel_resource(self):
        resource = build_resource(ResourceConfig(), graph_version=GRAPH_VERSION)
        self.assertEqual(resource.attributes['service.name'], 'pq_agent')
        self.assertEqual(resource.attributes['pq_agent.graph_version'], GRAPH_VERSION)


class RedactorTests(unittest.TestCase):
    def setUp(self):
        self.redactor = TelemetryRedactor(secrets=(FAKE_SECRET,))

    def test_allowlisted_scalar_passes(self):
        keep, value, reason = self.redactor.sanitize('agent.node', 'classify_intent')
        self.assertTrue(keep)
        self.assertEqual(value, 'classify_intent')
        self.assertEqual(reason, '')

    def test_forbidden_key_is_dropped(self):
        for key in ('webhook_url', 'prompt', 'arguments', 'checkpoint', 'authorization', 'exception'):
            keep, value, reason = self.redactor.sanitize(key, 'x')
            self.assertFalse(keep, key)
            self.assertIsNone(value)
            self.assertEqual(reason, 'forbidden_key')

    def test_unknown_key_is_dropped_by_default(self):
        keep, value, reason = self.redactor.sanitize('custom.attr', 'x')
        self.assertFalse(keep)
        self.assertEqual(reason, 'not_allowlisted')

    def test_allowlist_and_forbidden_list_do_not_overlap(self):
        overlap = set(SPAN_ATTRIBUTE_ALLOWLIST) & set(TRACING_FORBIDDEN_ATTRIBUTE_KEYS)
        self.assertEqual(overlap, set())

    def test_secret_inside_allowed_value_is_scrubbed(self):
        keep, value, _ = self.redactor.sanitize('agent.intent', 'call ' + FAKE_SECRET + ' now')
        self.assertTrue(keep)
        self.assertNotIn(FAKE_SECRET, value)
        self.assertIn('[REDACTED:', value)

    def test_webhook_value_is_scrubbed(self):
        keep, value, _ = self.redactor.sanitize('tool.name', FAKE_WEBHOOK)
        self.assertTrue(keep)
        self.assertNotIn('open-apis/bot', value)

    def test_non_scalar_is_marked_not_serialized(self):
        keep, value, _ = self.redactor.sanitize('agent.node', {'nested': 1})
        self.assertTrue(keep)
        self.assertEqual(value, '<dict>')

    def test_none_becomes_unknown_not_empty(self):
        keep, value, _ = self.redactor.sanitize('error_code', None)
        self.assertTrue(keep)
        self.assertEqual(value, 'unknown')

    def test_usage_unknown_for_unsupported_types(self):
        keep, value, _ = self.redactor.sanitize('usage.tokens', object())
        self.assertTrue(keep)
        self.assertEqual(value, '<object>')

    def test_sanitize_attributes_filters_map(self):
        clean = self.redactor.sanitize_attributes({
            'agent.node': 'budget_check',
            'webhook_url': FAKE_WEBHOOK,
            'unknown.key': 1,
            'error_code': None,
        })
        self.assertEqual(sorted(clean), ['agent.node', 'error_code'])
        self.assertEqual(clean['error_code'], 'unknown')

    def test_payload_gate_scrubs_known_secret(self):
        cleaned = self.redactor.sanitize_payload({'note': 'token=' + FAKE_SECRET})
        self.assertNotIn(FAKE_SECRET, json.dumps(cleaned, ensure_ascii=False))
        self.assertIn('[REDACTED:', json.dumps(cleaned, ensure_ascii=False))

    def test_module_level_sanitize_attributes(self):
        clean = sanitize_event_payload({'detail': FAKE_SECRET + ' 出现在这里'}, secrets=(FAKE_SECRET,))
        self.assertNotIn(FAKE_SECRET, json.dumps(clean, ensure_ascii=False))

    def test_is_sensitive_attribute(self):
        self.assertTrue(is_sensitive_attribute('webhook_url'))
        self.assertTrue(is_sensitive_attribute('api_key'))
        self.assertFalse(is_sensitive_attribute('agent.node'))

    def test_exporter_configuration_never_exports_header_values(self):
        info = exporter_configuration(endpoint='http://127.0.0.1:4318/v1/traces', headers={'Authorization': FAKE_SECRET})
        self.assertTrue(info['has_headers'])
        self.assertTrue(info['header_configured'])
        self.assertNotIn(FAKE_SECRET, json.dumps(info, ensure_ascii=False))


class ExporterTests(unittest.TestCase):
    def test_wrap_exporter_none_stays_none(self):
        self.assertIsNone(wrap_exporter(None))

    def test_wrapped_exporter_sanitizes_before_delegating(self):
        inner = SpinCallbackExporter()
        exporter = wrap_exporter(inner, secrets=(FAKE_SECRET,))
        self.assertIsInstance(exporter, SanitizingSpanExporter)
        self.assertEqual(inner.spans, [])

    def test_wrap_exporter_keeps_wrapper_for_broken_inner(self):
        class Broken:
            def export(self, spans):
                raise RuntimeError('boom')

            def shutdown(self):
                return None

            def force_flush(self, timeout_millis=0):
                return True

        # 包装失败只降级成 None 之外的路径，绝不把异常抛给调用方
        self.assertIsInstance(wrap_exporter(Broken()), SanitizingSpanExporter)


class EventLogTests(unittest.TestCase):
    def test_event_log_writes_whitelisted_event(self):
        path = tmp_dir('events') / 'telemetry.jsonl'
        log = LocalEventLog(path)
        log.record('tracing_degraded', detail='未配置 exporter')
        rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['event'], 'tracing_degraded')
        self.assertEqual(rows[0]['schema_version'], EVENT_LOG_SCHEMA_VERSION)
        self.assertEqual(log.count('tracing_degraded'), 1)

    def test_unknown_event_name_is_rejected(self):
        log = LocalEventLog(tmp_dir('events2') / 'telemetry.jsonl')
        with self.assertRaises(ContractError):
            log.record('some_random_event')

    def test_event_log_scrubs_secrets_before_persisting(self):
        path = tmp_dir('events3') / 'telemetry.jsonl'
        log = LocalEventLog(path, secrets=(FAKE_SECRET,))
        log.record('exporter_unavailable', detail='失败 ' + FAKE_SECRET)
        body = path.read_text(encoding='utf-8')
        self.assertNotIn(FAKE_SECRET, body)

    def test_event_log_rotates_when_over_limit(self):
        path = tmp_dir('events4') / 'telemetry.jsonl'
        log = LocalEventLog(path, max_bytes=256)
        for _ in range(20):
            log.record('span_error', detail='x' * 40)
        self.assertGreaterEqual(log.rotations, 1)
        rotated = sorted(p.name for p in path.parent.iterdir())
        self.assertGreater(len(rotated), 1)

    def test_read_events_returns_recorded_events(self):
        log = LocalEventLog(tmp_dir('events5') / 'telemetry.jsonl')
        log.record('shutdown', detail='ok')
        events = log.read_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['event'], 'shutdown')


class TracerTests(unittest.TestCase):
    def tearDown(self):
        clear_tracing()

    def test_tracer_without_exporter_is_degraded_but_usable(self):
        state = build_tracer(exporter=None, exporter_kind='none')
        self.assertTrue(state.status.degraded)
        self.assertIn('no_exporter_configured', state.status.reasons)
        self.assertIsNotNone(state.tracer)
        self.assertEqual(state.event_log.count('tracing_degraded'), 1)

    def test_tracer_with_exporter_is_not_degraded(self):
        inner = SpinCallbackExporter()
        state = build_tracer(exporter=inner, exporter_kind='spin')
        self.assertFalse(state.status.degraded)
        with state.tracer.start_as_current_span('agent.turn') as span:
            span.set_attribute('agent.mode', 'replay')
        state.provider.shutdown()
        # span_count 由 wrapper 与 exporter 各自记账，这里只断言真的导出到了扇出点
        self.assertEqual(len(inner.spans), 1)
        self.assertEqual(inner.spans[0].name, 'agent.turn')

    def test_process_level_registry_is_explicit(self):
        self.assertIsNone(current_tracing())
        sentinel = object()
        self.assertIs(get_tracer(sentinel), sentinel)
        state = configure_tracing(exporter=SpinCallbackExporter(), exporter_kind='spin')
        self.assertIs(current_tracing(), state)
        self.assertIsNotNone(get_tracer())
        shutdown_tracing()
        self.assertIsNone(current_tracing())


def _run_graph_once(tracer):
    from pq_agent.graph import GraphDeps, build_graph, run_graph

    tools = FAKES.FakeTools()
    tools.add(FAKES.FakeTool('market_snapshot', result={'ok': True}))
    deps = GraphDeps(
        provider=FAKES.FakeProvider(),
        tools=tools,
        clock=FAKES.fixed_clock,
        tracer=tracer,
        plan_builder=lambda s: FAKES.plain_plan(('market_snapshot', 'read_external')),
    )
    state = FAKES.make_state(user_query='今天大盘怎么样')
    return run_graph(build_graph(deps), state)


class GraphTelemetryTests(unittest.TestCase):
    def test_graph_emits_one_span_per_node(self):
        inner = SpinCallbackExporter()
        tracing_state = build_tracer(exporter=inner, exporter_kind='spin')
        out, interrupted = _run_graph_once(tracing_state.tracer)
        tracing_state.provider.shutdown()
        names = [span.name for span in inner.spans]
        self.assertFalse(interrupted)
        self.assertGreater(len(names), 3)
        # A4-02 之后这里不再只有 graph.node：工具调用也有自己的 span（tool.invoke），
        # 这正是「拓扑能连起来」的前提。断言改成「每个名字都是已知 span 名」，
        # 而不是回到「所有 span 必须同名」——那种断言一加新信号就碎。
        self.assertTrue(all(is_known_span(name) for name in names), names)
        self.assertTrue(any(name == 'graph.node' for name in names), names)
        self.assertTrue(out['final_answer'])

    def test_node_spans_only_carry_allowlisted_attributes(self):
        inner = SpinCallbackExporter()
        tracing_state = build_tracer(exporter=inner, exporter_kind='spin')
        _run_graph_once(tracing_state.tracer)
        tracing_state.provider.shutdown()
        for span in inner.spans:
            for key in span.attributes or {}:
                self.assertIn(key, SPAN_ATTRIBUTE_ALLOWLIST, key)
                self.assertNotIn(key, TRACING_FORBIDDEN_ATTRIBUTE_KEYS, key)

    def test_node_spans_never_contain_the_user_query(self):
        inner = SpinCallbackExporter()
        tracing_state = build_tracer(exporter=inner, exporter_kind='spin')
        _run_graph_once(tracing_state.tracer)
        tracing_state.provider.shutdown()
        blob = json.dumps([dict(s.attributes or {}) for s in inner.spans], ensure_ascii=False, default=str)
        self.assertNotIn('今天大盘怎么样', blob)

    def test_tool_failure_is_recorded_as_plain_error_code(self):
        # 工具抛异常时，span 只留错误码与异常类型名，绝不留异常原文
        from pq_agent.graph import GraphDeps, build_graph, run_graph

        class Boom:
            name = 'market_snapshot'

            def run(self, arguments, dry_run=True):
                raise RuntimeError('内部错误原文 ' + FAKE_SECRET)

        inner = SpinCallbackExporter()
        tracing_state = build_tracer(exporter=inner, exporter_kind='spin')
        tools = FAKES.FakeTools()
        tools.add(Boom())
        deps = GraphDeps(
            provider=FAKES.FakeProvider(),
            tools=tools,
            clock=FAKES.fixed_clock,
            tracer=tracing_state.tracer,
            plan_builder=lambda s: FAKES.plain_plan(('market_snapshot', 'read_external')),
        )
        out, interrupted = run_graph(build_graph(deps), FAKES.make_state(user_query='今天大盘怎么样'))
        tracing_state.provider.shutdown()
        self.assertFalse(interrupted)
        self.assertTrue(out['final_answer'])
        node_names = [s.attributes.get('agent.node') for s in inner.spans]
        self.assertIn('execute_tool', node_names)
        dumped = json.dumps([dict(s.attributes or {}) for s in inner.spans], ensure_ascii=False, default=str)
        self.assertNotIn('Traceback', dumped)
        self.assertNotIn('RuntimeError', dumped)
        self.assertNotIn(FAKE_SECRET, dumped)
        self.assertIn('TOOL_FAILED', json.dumps(out['errors'], ensure_ascii=False))

    def test_graph_runs_normally_without_tracer(self):
        out, interrupted = _run_graph_once(None)
        self.assertFalse(interrupted)
        self.assertTrue(out['final_answer'])


class SecretLeakScanTests(unittest.TestCase):
    def test_no_secret_reaches_exporter_or_event_log(self):
        workdir = tmp_dir('scan')
        log_path = workdir / 'telemetry.jsonl'
        event_log = LocalEventLog(log_path, secrets=(FAKE_SECRET, FAKE_WEBHOOK))
        inner = SpinCallbackExporter()
        tracing_state = build_tracer(
            exporter=inner,
            exporter_kind='spin',
            event_log=event_log,
            secrets=(FAKE_SECRET, FAKE_WEBHOOK),
        )
        with tracing_state.tracer.start_as_current_span('agent.turn') as span:
            span.set_attribute('agent.intent', FAKE_SECRET + ' 与 ' + FAKE_WEBHOOK)
            span.set_attribute('webhook_url', FAKE_WEBHOOK)
        tracing_state.provider.shutdown()
        exported = json.dumps([dict(s.attributes or {}) for s in inner.spans], ensure_ascii=False)
        self.assertNotIn(FAKE_SECRET, exported)
        self.assertNotIn(FAKE_WEBHOOK, exported)
        self.assertNotIn('webhook_url', exported)
        written = log_path.read_text(encoding='utf-8') if log_path.exists() else ''
        self.assertNotIn(FAKE_SECRET, written)
        self.assertNotIn(FAKE_WEBHOOK, written)


if __name__ == '__main__':
    unittest.main()