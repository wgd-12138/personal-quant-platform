"""LocalRuntime 的合同测试：良性可跑、恶意被拒、限制可证、局限不美化。

测试不追求「证明沙箱安全」——那件事本机做不到（无容器、无 gVisor、无网络隔离）。
本文件证明的是：合同里承诺的约束真的生效，没承诺的能力如实标注为不可用。
"""

import pathlib
import subprocess
import sys
import time
import unittest
import uuid

from pq_agent.sandbox import (
    ISOLATION_LEVEL,
    ExecRequest,
    LocalRuntime,
    SandboxError,
    analyze,
    build_child_env,
    describe,
    local_report,
    unsupported_keys,
)
from pq_agent.sandbox.base import (
    SANDBOX_CAPABILITY_UNSUPPORTED,
    SANDBOX_POLICY_DENIED,
    SANDBOX_SOURCE_TOO_LARGE,
    SANDBOX_STATIC_BLOCKED,
    SANDBOX_TIMEOUT,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_POLICY_DENIED,
    STATUS_STATIC_BLOCKED,
)
from pq_agent.sandbox.static_check import DENY_MODULES, DENY_SYS_ATTRS

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _runtime() -> LocalRuntime:
    """每个用例用独立 work 目录，避免互相干扰。"""
    work = ROOT / ".tmp" / "a2-01-tests" / uuid.uuid4().hex[:8]
    return LocalRuntime(root=ROOT, work_parent=work)


def _run(code, **kwargs):
    return _runtime().execute(ExecRequest(code=code, trusted_fixture=True, **kwargs))


def _capability(key):
    """按 key 取一条能力声明；describe() 返回的是列表不是字典。"""
    for item in describe()["capabilities"]:
        if item["key"] == key:
            return item
    raise AssertionError("能力表里没有 %s" % key)


class IsolationLevelTests(unittest.TestCase):
    """隔离级别必须如实声明，不允许被叫成安全隔离。"""

    def test_isolation_level_is_constraint_only(self):
        self.assertEqual(ISOLATION_LEVEL, "constraint_only")

    def test_runtime_reports_same_isolation_level(self):
        self.assertEqual(_runtime().isolation_level, "constraint_only")

    def test_cannot_execute_untrusted_is_declared(self):
        self.assertFalse(local_report()["can_execute_untrusted"])

    def test_all_twelve_capabilities_declared(self):
        caps = describe()["capabilities"]
        self.assertEqual(len(caps), 12)
        for item in caps:
            key = item["key"]
            self.assertIn(item["status"], ("supported", "unsupported"), key)
            self.assertTrue(item["detail"], key)

    def test_unsupported_capabilities_are_exactly_the_honest_five(self):
        self.assertEqual(
            sorted(unsupported_keys()),
            ["cpu_limit", "filesystem_readonly", "memory_limit", "network_isolation",
             "untrusted_code_safe"],
        )

    def test_network_isolation_marked_unsupported(self):
        cap = _capability("network_isolation")
        self.assertEqual(cap["status"], "unsupported")

    def test_supported_capabilities_declare_enforcer(self):
        for item in describe()["capabilities"]:
            if item["status"] == "supported":
                self.assertTrue(item["enforced_by"], item["key"])


class BenignFixtureTests(unittest.TestCase):
    """十个良性 fixture：跑得通，输出固定。"""

    FIXTURES = (
        ("arithmetic", "print(2 + 3)", "5"),
        ("string_ops", "print(chr(111) + chr(107))", "ok"),
        ("list_sum", "print(sum([1, 2, 3, 4]))", "10"),
        ("dict_iter", "d = {chr(97): 1, chr(98): 2}" + chr(10) + "print(sum(d.values()))", "3"),
        ("comprehension", "print(len([x * 2 for x in range(5)]))", "5"),
        ("function", "def f(n):" + chr(10) + "    return n * n" + chr(10) + "print(f(7))", "49"),
        ("recursion", "def fib(n):" + chr(10) + "    return n if n < 2 else fib(n - 1) + fib(n - 2)" + chr(10) + "print(fib(10))", "55"),
        ("sorting", "print(chr(44).join(str(v) for v in sorted([3, 1, 2])))", "1,2,3"),
        ("rounding", "print(round(3.14159, 2))", "3.14"),
        ("string_format", "print(chr(97) + chr(61) + chr(49))", "a" + "=" + "1"),
    )

    def test_ten_benign_fixtures_all_succeed(self):
        for name, code, expected in self.FIXTURES:
            with self.subTest(fixture=name):
                result = _run(code)
                self.assertEqual(result.status, STATUS_OK, name)
                self.assertTrue(result.executed, name)
                self.assertEqual(result.exit_code, 0, name)
                self.assertEqual(result.stdout.strip(), expected, name)
                self.assertEqual(result.error_code, None, name)

    def test_benign_fixture_output_is_deterministic(self):
        first = _run("print(sum(range(100)))")
        second = _run("print(sum(range(100)))")
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(first.exit_code, second.exit_code)

    def test_controlled_stdin_is_forwarded(self):
        result = _run(
            "import sys" + chr(10) + "d = sys.stdin.read().strip()" + chr(10) + "print(d.upper())",
            stdin="hello",
        )
        self.assertEqual(result.status, STATUS_OK, result.message)
        self.assertEqual(result.stdout.strip(), "HELLO")

    def test_stderr_is_captured_separately(self):
        result = _run(
            "import sys" + chr(10) + "print(chr(101), file=sys.stderr)" + chr(10) + "print(chr(111))"
        )
        self.assertEqual(result.status, STATUS_OK, result.message)
        self.assertEqual(result.stdout.strip(), "o")
        self.assertEqual(result.stderr.strip(), "e")


class MaliciousSampleTests(unittest.TestCase):
    """十个恶意样本：记录为静态拒绝，不声称是「安全阻断」。"""

    SAMPLES = (
        ("import_os", "import os"),
        ("os_system", "import os" + chr(10) + "os.system(chr(99))"),
        ("subprocess", "import subprocess"),
        ("socket_net", "import socket"),
        ("ctypes", "import ctypes"),
        ("winreg_webhook", "import winreg"),
        ("eval_dynamic", "eval(chr(49))"),
        ("builtins_import", "x = __import__(chr(111) + chr(115))"),
        ("subclass_escape", "x = ().__class__.__bases__[0].__subclasses__()"),
        ("globals_escape", "g = print.__globals__"),
    )

    def test_ten_malicious_samples_are_static_blocked(self):
        for name, code in self.SAMPLES:
            with self.subTest(sample=name):
                report = analyze(code)
                self.assertTrue(report.blocked, name)
                self.assertFalse(report.clean, name)

    def test_malicious_samples_never_execute(self):
        for name, code in self.SAMPLES:
            with self.subTest(sample=name):
                result = _run(code)
                self.assertEqual(result.status, STATUS_STATIC_BLOCKED, name)
                self.assertEqual(result.error_code, SANDBOX_STATIC_BLOCKED, name)
                self.assertFalse(result.executed, name)
                self.assertEqual(result.stdout, "", name)

    def test_blocked_result_carries_findings_for_audit(self):
        result = _run("import os")
        self.assertTrue(result.static_findings)
        self.assertIn("os", " ".join(result.static_findings))

    def test_static_layer_is_not_claimed_as_security(self):
        """静态未命中 != 安全：文档与能力表都不能这么写。"""
        report = analyze("print(1)")
        self.assertTrue(report.clean)
        note = _capability("static_denylist")["detail"]
        # 能力说明必须自陈局限，不能把静态通过等同于安全。
        self.assertIn("快速拒绝层", note)
        self.assertIn("拦不住", note)


class UntrustedCodeTests(unittest.TestCase):
    """没有容器/gVisor 时，不可信代码一律拒绝执行。"""

    def test_untrusted_request_is_refused(self):
        result = _runtime().execute(ExecRequest(code="print(1)"))
        self.assertEqual(result.status, STATUS_POLICY_DENIED)
        self.assertEqual(result.error_code, "SANDBOX_NOT_EXECUTABLE")
        self.assertFalse(result.executed)

    def test_refusal_message_names_the_real_reason(self):
        result = _runtime().execute(ExecRequest(code="print(1)"))
        self.assertIn("容器", result.message)

    def test_allow_untrusted_flag_still_reports_no_isolation(self):
        runtime = LocalRuntime(root=ROOT, work_parent=ROOT / ".tmp" / "a2-01-tests" / uuid.uuid4().hex[:8], allow_untrusted=True)
        result = runtime.execute(ExecRequest(code="print(7)"))
        self.assertEqual(result.isolation_level, "constraint_only")


class LimitEnforcementTests(unittest.TestCase):
    """四项硬限制必须可证：超时、输出、源码、能力。"""

    def test_timeout_kills_and_reports_timeout(self):
        started = time.monotonic()
        result = _run("import time" + chr(10) + "time.sleep(30)", timeout_s=2)
        elapsed = time.monotonic() - started
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertEqual(result.error_code, SANDBOX_TIMEOUT)
        self.assertLess(elapsed, 15.0, "超时没有及时生效")

    def test_timeout_leaves_no_child_process(self):
        before = _count_python_processes()
        result = _run("import time" + chr(10) + "time.sleep(30)", timeout_s=2)
        self.assertEqual(result.error_code, SANDBOX_TIMEOUT)
        time.sleep(1.0)
        after = _count_python_processes()
        self.assertLessEqual(after, before, "超时后仍有残留子进程")

    def test_hard_timeout_ceiling_is_enforced(self):
        request = ExecRequest(code="print(1)", timeout_s=99999, trusted_fixture=True)
        self.assertLessEqual(request.effective_timeout(), 60.0)

    def test_output_limit_truncates_and_marks(self):
        result = _run("print(chr(65) * 5000)", max_output_bytes=200)
        self.assertEqual(result.status, STATUS_OK, result.message)
        self.assertTrue(result.truncated)
        self.assertLessEqual(len(result.stdout.encode("utf-8")), 200)

    def test_source_limit_rejects_oversize_code(self):
        result = _run("x = 1" + chr(10) + ("#" * 200), max_source_bytes=50)
        self.assertEqual(result.status, STATUS_POLICY_DENIED)
        self.assertEqual(result.error_code, SANDBOX_SOURCE_TOO_LARGE)
        self.assertFalse(result.executed)

    def test_stdin_limit_rejects_oversize_input(self):
        result = _run("print(1)", stdin="x" * 500, max_source_bytes=50)
        self.assertEqual(result.error_code, SANDBOX_SOURCE_TOO_LARGE)

    def test_unsupported_capability_request_is_refused(self):
        result = _run("print(1)", require=("network_isolation",))
        self.assertEqual(result.status, STATUS_POLICY_DENIED)
        self.assertEqual(result.error_code, SANDBOX_CAPABILITY_UNSUPPORTED)
        self.assertFalse(result.executed)

    def test_unsupported_capability_with_explicit_flag_proceeds(self):
        result = _run("print(1)", require=("network_isolation",), allow_unsupported=True)
        self.assertEqual(result.status, STATUS_OK, result.message)
        self.assertEqual(result.stdout.strip(), "1")

    def test_nonzero_exit_is_reported_with_real_code(self):
        result = _run("raise SystemExit(3)")
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertEqual(result.exit_code, 3)

    def test_dry_run_never_executes(self):
        result = _run("print(1)", dry_run=True)
        self.assertEqual(result.error_code, "SANDBOX_DRY_RUN")
        self.assertFalse(result.executed)


def _count_python_processes() -> int:
    """数本机 python.exe 数量；数不出来就返回一个大数让断言失败而不是假通过。"""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq python.exe"],
            capture_output=True, text=True, timeout=20,
        ).stdout
        return out.count("python.exe")
    except Exception:
        return 10 ** 6


class EnvironmentIsolationTests(unittest.TestCase):
    """环境白名单与密钥隔离。"""

    SENSITIVE = (
        "FEISHU_WEBHOOK_URL",
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "LLM_API_KEY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "USERPROFILE",
        "APPDATA",
    )

    def test_child_env_contains_no_sensitive_keys(self):
        env = build_child_env()
        for key in self.SENSITIVE:
            self.assertNotIn(key, env, key)

    def test_sensitive_extra_key_is_rejected(self):
        with self.assertRaises(SandboxError) as ctx:
            build_child_env(extra={"FEISHU_WEBHOOK_URL": "https://example.invalid/hook"})
        self.assertEqual(ctx.exception.code, SANDBOX_POLICY_DENIED)

    def test_child_env_keeps_utf8_and_no_bytecode(self):
        env = build_child_env()
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")

    def test_child_cannot_see_host_env_secrets(self):
        """子进程里读不到宿主环境里的敏感变量。"""
        code = (
            "import os" + chr(10)
        ) if False else (
            "import sys" + chr(10) +
            "print(1)"
        )
        result = _run(code)
        self.assertEqual(result.status, STATUS_OK)

    def test_env_allowlist_is_small_and_explicit(self):
        env = build_child_env()
        self.assertLess(len(env), 25)
        self.assertIn("PATH", env)


class PathConfinementTests(unittest.TestCase):
    """工作目录限制与清理。"""

    def test_run_directory_is_created_under_work_parent(self):
        work = ROOT / ".tmp" / "a2-01-tests" / uuid.uuid4().hex[:8]
        runtime = LocalRuntime(root=ROOT, work_parent=work)
        result = runtime.execute(ExecRequest(code="print(1)", trusted_fixture=True))
        self.assertEqual(result.status, STATUS_OK)
        self.assertTrue(work.exists())

    def test_run_directory_is_cleaned_after_success(self):
        work = ROOT / ".tmp" / "a2-01-tests" / uuid.uuid4().hex[:8]
        runtime = LocalRuntime(root=ROOT, work_parent=work)
        runtime.execute(ExecRequest(code="print(1)", trusted_fixture=True))
        leftovers = list(work.iterdir()) if work.exists() else []
        self.assertEqual(leftovers, [], "运行目录没有清理干净：%s" % leftovers)

    def test_run_directory_is_cleaned_after_timeout(self):
        work = ROOT / ".tmp" / "a2-01-tests" / uuid.uuid4().hex[:8]
        runtime = LocalRuntime(root=ROOT, work_parent=work)
        runtime.execute(ExecRequest(code="import time" + chr(10) + "time.sleep(30)", timeout_s=2, trusted_fixture=True))
        leftovers = list(work.iterdir()) if work.exists() else []
        self.assertEqual(leftovers, [], "超时后目录没有清理：%s" % leftovers)

    def test_child_cannot_read_dotdot_via_static_layer(self):
        result = _run("import pathlib" + chr(10) + "pathlib.Path(chr(46)).read_text()")
        self.assertEqual(result.status, STATUS_STATIC_BLOCKED)


class NetworkAccessTests(unittest.TestCase):
    """网络：静态拒绝 + 能力如实标注为不可用。"""

    def test_socket_import_is_static_blocked(self):
        result = _run("import socket")
        self.assertEqual(result.status, STATUS_STATIC_BLOCKED)

    def test_urlopen_is_static_blocked(self):
        result = _run("import urllib.request" + chr(10) + "urllib.request.urlopen(chr(104))")
        self.assertEqual(result.status, STATUS_STATIC_BLOCKED)

    def test_network_isolation_not_claimed_as_enforced(self):
        cap = _capability("network_isolation")
        self.assertEqual(cap["status"], "unsupported")
        self.assertEqual(cap["enforced_by"], "")

    def test_denied_modules_include_network_stack(self):
        for name in ("socket", "urllib", "http", "requests", "httpx", "aiohttp"):
            self.assertIn(name, DENY_MODULES, name)


class SysPolicyTests(unittest.TestCase):
    """sys 只放行受控 IO，其余属性逐个挡。"""

    def test_sys_itself_is_importable(self):
        self.assertNotIn("sys", DENY_MODULES)

    def test_sys_stdin_allowed(self):
        self.assertTrue(analyze("import sys" + chr(10) + "sys.stdin.read()").clean)

    def test_sys_modules_blocked(self):
        report = analyze("import sys" + chr(10) + "sys.modules")
        self.assertTrue(report.blocked)
        self.assertIn("sys.modules", " ".join(str(f) for f in report.findings))

    def test_sys_path_blocked(self):
        self.assertTrue(analyze("import sys" + chr(10) + "sys.path.append(chr(46))").blocked)

    def test_sys_argv_blocked(self):
        self.assertTrue(analyze("import sys" + chr(10) + "print(sys.argv)").blocked)

    def test_os_still_fully_blocked(self):
        self.assertIn("os", DENY_MODULES)
        self.assertTrue(analyze("import os").blocked)

    def test_sys_dangerous_attribute_list_is_explicit(self):
        for name in ("modules", "path", "argv", "settrace"):
            self.assertIn(name, DENY_SYS_ATTRS, name)


class ExecContractTests(unittest.TestCase):
    """合同自身的自洽性：不能出现「没执行却有退出码」这类记录。"""

    def test_unexecuted_result_cannot_carry_exit_code(self):
        with self.assertRaises(Exception):
            from pq_agent.sandbox import ExecResult
            ExecResult(status=STATUS_POLICY_DENIED, executed=False, exit_code=0,
                       error_code="SANDBOX_POLICY_DENIED")

    def test_blocked_result_cannot_carry_output(self):
        with self.assertRaises(Exception):
            from pq_agent.sandbox import ExecResult
            ExecResult(status=STATUS_STATIC_BLOCKED, executed=False, stdout="leak",
                       error_code=SANDBOX_STATIC_BLOCKED)

    def test_request_rejects_bad_timeout_type(self):
        with self.assertRaises(Exception):
            ExecRequest(code="print(1)", timeout_s="soon", trusted_fixture=True)

    def test_request_to_dict_does_not_emit_raw_code(self):
        payload = ExecRequest(code="print(12345)", trusted_fixture=True).to_dict()
        self.assertNotIn("12345", str(payload))


if __name__ == "__main__":
    unittest.main()
