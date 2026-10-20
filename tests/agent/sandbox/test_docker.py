"""DockerRuntime 安全边界测试。

本机没有 Docker，所以这里分两层验证：
1. 配置层：argv 是否包含全部安全开关、挂载是否只允许 fixture、资源限制是否齐全。这些不需要容器。
2. 能力层：Docker 不可用时必须返回能力 gate failed，绝不能悄悄换成 LocalRuntime。

注意：不把「argv 生成正确」说成「容器隔离已验证」。真正的隔离验证需要一台装了 Docker 的机器。
"""

import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from pq_agent.sandbox import static_check
from pq_agent.sandbox.docker_runtime import (
    CAPABILITIES_SUPPORTED,
    CONTAINER_USER,
    DEFAULT_MEMORY,
    DEFAULT_IMAGE,
    DEFAULT_PIDS_LIMIT,
    DOCKER_ALLOWED_MOUNT_ROOTS,
    ISOLATION_LEVEL,
    DockerMountRejected,
    DockerProbe,
    DockerRuntime,
    DockerUnavailableError,
    MountSpec,
    audit_argv,
    build_run_argv,
    docker_report,
    probe_docker,
    require_docker,
    validate_mounts,
)
from pq_agent.sandbox.base import (
    SANDBOX_CAPABILITY_UNSUPPORTED,
    STATUS_POLICY_DENIED,
    ExecRequest,
)

CORPUS = ROOT / "agent_eval" / "cases" / "sandbox_redteam.json"


def _tmp_dir(name: str) -> pathlib.Path:
    """本机沙箱把 0o700 目录变成自己进不去的目录，所以不用 tempfile。"""
    import uuid
    path = ROOT / ".tmp" / "a2-02-tests" / (name + "-" + uuid.uuid4().hex[:8])
    path.mkdir(parents=True, exist_ok=True)
    return path


class A2_02_DockerIsolation(unittest.TestCase):
    """A2-02：容器安全边界。"""

    @classmethod
    def setUpClass(cls):
        cls.probe = probe_docker()
        cls.available = cls.probe.available

    def test_isolation_level_is_explicit(self):
        """容器级别与约束级别必须能区分开。"""
        self.assertEqual(ISOLATION_LEVEL, "container")
        runtime = DockerRuntime(root=ROOT, probe=self.probe) if self.available else None
        if runtime is None:
            report = docker_report(root=ROOT)
            self.assertEqual(report["isolation_level"], "container")
            self.assertIn("不是虚拟机", report["isolation_level_note"])

    def test_docker_absent(self):
        """本机事实：Docker 不可用。这条测试记录环境而不是通过能力。"""
        runtime = DockerRuntime(root=ROOT, probe=self.probe)
        self.assertFalse(runtime.available)
        gate = runtime.containment_gate()
        self.assertFalse(gate["passed"])
        self.assertFalse(gate["alternative_allowed"])

    def test_absent_does_not_fall_back_to_local(self):
        """Docker 不可用时必须拒绝执行，绝不换成 LocalRuntime。"""
        runtime = DockerRuntime(root=ROOT, probe=self.probe)
        result = runtime.execute(ExecRequest(code="print(1)", trusted_fixture=True))
        self.assertEqual(result.status, STATUS_POLICY_DENIED)
        self.assertEqual(result.error_code, SANDBOX_CAPABILITY_UNSUPPORTED)
        self.assertFalse(result.executed)
        self.assertEqual(result.isolation_level, "container")
        self.assertIn("不使用 LocalRuntime 冒充隔离", result.message)

    def test_require_docker_raises_when_absent(self):
        """缺能力要抛错，不能静默降级。"""
        try:
            require_docker()
        except DockerUnavailableError as exc:
            self.assertIn("已拒绝执行", str(exc))
        else:
            self.skipTest("本机 Docker 可用，跳过缺能力分支")


    def test_argv_contains_all_hardening_flags(self):
        """安全开关一个都不能少：这是容器的全部边界。"""
        argv = build_run_argv()
        findings = audit_argv(argv)
        self.assertTrue(findings["network_disabled"])
        self.assertTrue(findings["read_only_root"])
        self.assertTrue(findings["non_root_user"])
        self.assertTrue(findings["caps_dropped"])
        self.assertTrue(findings["no_new_privileges"])
        self.assertTrue(findings["pids_limited"])
        self.assertTrue(findings["memory_limited"])
        self.assertTrue(findings["cpu_limited"])
        self.assertTrue(findings["tmpfs_mounted"])
        self.assertTrue(findings["ok"])

    def test_argv_uses_pinned_digest(self):
        """镜像必须按 digest 锁死，浮动 tag 是供应链风险。"""
        argv = build_run_argv()
        self.assertIn("@sha256:", DEFAULT_IMAGE)
        self.assertIn(DEFAULT_IMAGE, argv)
        with self.assertRaises(DockerMountRejected):
            DockerRuntime(root=ROOT, image="python:3.12-slim", probe=self.probe)

    def test_audit_rejects_unsafe_configs(self):
        """审计函数要能把危险配置挑出来，否则它就是摆设。"""
        self.assertFalse(audit_argv(build_run_argv() + ["--privileged"])["ok"])
        self.assertFalse(audit_argv(build_run_argv(network="host"))["ok"])
        self.assertFalse(audit_argv(build_run_argv(user="0"))["ok"])
        self.assertFalse(audit_argv(build_run_argv(readonly_root=False))["ok"])

    def test_audit_reports_mount_count_without_polluting_ok(self):
        """mount_count 是计数不是结论：空列表必须仍然 ok。"""
        findings = audit_argv(build_run_argv())
        self.assertEqual(findings["mount_count"], 0)
        self.assertTrue(findings["ok"])
        mounts = validate_mounts([MountSpec("tests/fixtures", "/fixtures")], root=ROOT)
        findings2 = audit_argv(build_run_argv(mounts=mounts))
        self.assertEqual(findings2["mount_count"], 1)
        self.assertTrue(findings2["ok"])

    def test_mounts_are_readonly_and_whitelisted(self):
        """只允许 fixture 子目录，且必须只读。"""
        mounts = validate_mounts([MountSpec("tests/fixtures", "/fixtures")], root=ROOT)
        argv = build_run_argv(mounts=mounts)
        spec = argv[argv.index("--mount") + 1]
        self.assertIn("readonly", spec)
        self.assertIn("target=/fixtures", spec)
        for bad in ("C:/Users/example-user/data", "../outside", "data", "reports"):
            with self.assertRaises(DockerMountRejected):
                validate_mounts([MountSpec(bad, "/x")], root=ROOT)

    def test_docker_socket_never_mounted(self):
        """挂 docker.sock 等于把宿主交给容器。"""
        argv = build_run_argv()
        self.assertNotIn("docker.sock", " ".join(argv))
        with self.assertRaises(DockerMountRejected):
            validate_mounts([MountSpec("//./pipe/docker_engine", "/s")], root=ROOT)

    def test_capabilities_not_overclaimed_when_unavailable(self):
        """不可用时能力清单必须为空，不能谎报 supported。"""
        report = docker_report(root=ROOT)
        if not report["available"]:
            self.assertFalse(report["gate"]["passed"])
            self.assertIn("alternative_allowed", report["gate"])
            self.assertFalse(report["gate"]["alternative_allowed"])
        else:
            self.assertEqual(len(CAPABILITIES_SUPPORTED), 12)


    def test_static_layer_blocks_declared_malicious_code(self):
        """10 条恶意里 8 条由静态层挡；另外 2 条资源耗尽类交给执行层。"""
        doc = json.loads(CORPUS.read_text(encoding="utf-8"))
        cases = doc["cases"]
        self.assertEqual(len(cases), 20)
        self.assertEqual(len({c["case_id"] for c in cases}), 20)
        static_blocked = 0
        runtime_blocked = 0
        for case in cases:
            if case["kind"] != "malicious":
                continue
            if case["expectation"] == "blocked":
                report = static_check.analyze(case["code"])
                self.assertTrue(report.blocked, case["case_id"] + " 未被静态层拒绝")
                static_blocked += 1
            else:
                # 资源耗尽类：静态层不负责，必须显式记录由哪一层兜。
                # 两条分别靠超时与 pids 上限，断言必须接受这两种声明，否则就是把
                # 「这一层兜住了」写成了「只有这一层能兜」。
                self.assertIn(case["block_layer"], ("runtime_timeout", "runtime_pids_limit"))
                runtime_blocked += 1
        self.assertEqual(static_blocked, 8)
        self.assertEqual(runtime_blocked, 2)

    def test_benign_corpus_all_clear(self):
        """良性样本必须一条都不被静态层误杀。"""
        doc = json.loads(CORPUS.read_text(encoding="utf-8"))
        benign = [c for c in doc["cases"] if c["kind"] == "benign"]
        self.assertEqual(len(benign), 10)
        for case in benign:
            report = static_check.analyze(case["code"])
            self.assertFalse(report.blocked, case["case_id"] + " 被误杀")

    def test_corpus_declares_layering_honestly(self):
        """语料必须写明哪一层负责，避免把「没拦住」说成「拦住了」。"""
        doc = json.loads(CORPUS.read_text(encoding="utf-8"))
        self.assertIn("不是安全隔离", doc["isolation_note"])
        self.assertIn("静态层", doc["layering"])
        for case in doc["cases"]:
            self.assertIn("block_layer", case)
            self.assertIn("expectation", case)

    def test_resource_limits_are_recorded(self):
        """内存、pids、CPU 限制必须出现在 argv 里，且可复现。"""
        first = build_run_argv()
        second = build_run_argv()
        self.assertEqual(first, second)
        joined = " ".join(first)
        self.assertIn("--memory " + DEFAULT_MEMORY, joined)
        self.assertIn("--pids-limit " + str(DEFAULT_PIDS_LIMIT), joined)
        self.assertIn("--user " + CONTAINER_USER, joined)
    def test_run_argv_is_reproducible(self):
        """同一配置重复生成必须逐字相同，否则证据不可复现。"""
        self.assertEqual(build_run_argv(), build_run_argv())


if __name__ == "__main__":
    unittest.main()
