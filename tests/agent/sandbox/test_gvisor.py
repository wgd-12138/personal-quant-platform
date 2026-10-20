"""A2-03 测试：gVisor 层。

本机没有 Docker、不是 Linux，所以这里分两层验证：
  1. 判定层：平台判据、fail-closed、能力矩阵、审计断言——这些不需要真容器。
  2. 资产层：argv 生成、deploy 脚本、bench 模板的诚实性。

注意：不把「argv 里写了 --runtime runsc」说成「gVisor 隔离已验证」。
真正的隔离验证需要在装了 Docker 与 runsc 的 Linux 机器上跑，本机做不到。
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

from pq_agent.sandbox import (
    DEFAULT_IMAGE,
    DockerProbe,
    DockerRuntime,
    ExecRequest,
    GvisorRuntime,
    STATUS_POLICY_DENIED,
    audit_argv,
    build_run_argv,
    decide_platform,
    gvisor_report,
    platform_blocker,
    probe_host,
)
from pq_agent.sandbox.gvisor_runtime import (
    BENCH_METRICS,
    BENCH_TEMPLATE_PATH,
    GVISOR_CAPABILITIES,
    CLOUD_BIND_HOST,
    GVISOR_RUNTIME_NAME,
    ISOLATION_LEVEL,
    KVM_DEVICE,
    PLATFORM_KVM,
    PLATFORM_PTRACE,
    PUBLIC_EXPOSURE_ALLOWED,
    SSH_TUNNEL_REQUIRED,
    GvisorProbe,
    bench_plan,
)
from pq_agent.sandbox.docker_runtime import ISOLATION_LEVEL as DOCKER_ISOLATION_LEVEL


def _tmp_dir(name: str) -> pathlib.Path:
    """本机沙箱把 0o700 目录变成自己进不去的目录，所以不用 tempfile。"""
    path = ROOT / ".tmp" / "a2-03-tests" / (name + "-" + uuid4().hex[:8])
    path.mkdir(parents=True, exist_ok=True)
    return path


def _unavailable_docker_probe() -> DockerProbe:
    """手工造一个「Docker 不可用」的探针，不依赖本机是否真的装了 Docker。"""
    return DockerProbe(
        available=False,
        binary="",
        version="",
        server_version="",
        error="测试构造：Docker 不可用",
        checked_at="2026-07-09T09:00:00+08:00",
    )


def _facts(**overrides):
    """造一组探测事实，默认是「Linux + Docker 可用 + runsc 已注册 + KVM 可读」。"""
    from pq_agent.sandbox.gvisor_runtime import HostFacts
    base = dict(
        system="Linux",
        kernel_release="5.10.0-28-amd64",
        distro_id="centos",
        distro_version="7",
        distro_pretty="CentOS Linux 7 (Core)",
        centos_eol="2024-06-30",
        docker_probe=DockerProbe(True, "/usr/bin/docker", "24.0.7", "24.0.7", "", "2026-07-09T09:00:00+08:00"),
        runsc_binary="/usr/local/bin/runsc",
        runsc_version="runsc version release-20240813.0",
        runsc_registered=True,
        registered_runtimes=("runc", "runsc"),
        kvm_present=True,
        kvm_readable=True,
        rootless=False,
        security_options=("name=seccomp,profile=builtin",),
        probed_at="2026-07-09T09:00:00+08:00",
    )
    base.update(overrides)
    return HostFacts(**base)


class A2_03_GvisorRuntime(unittest.TestCase):
    """gVisor 层：隔离级别、argv、平台判定、失败关闭。"""

    def test_isolation_level_is_explicit(self):
        self.assertEqual(ISOLATION_LEVEL, "gvisor")
        report = gvisor_report(root=ROOT)
        self.assertEqual(report["isolation_level"], "gvisor")
        self.assertIn("不是虚拟机", report["isolation_level_note"])

    def test_class_attributes_are_docker_inherited_not_copied(self):
        self.assertTrue(issubclass(GvisorRuntime, DockerRuntime))
        self.assertEqual(GvisorRuntime.name, "gvisor")
        self.assertEqual(GvisorRuntime.display_name, "GVisorRuntime")
        self.assertEqual(GvisorRuntime.container_runtime, GVISOR_RUNTIME_NAME)
        self.assertEqual(GvisorRuntime.containment_subject, "gvisor_isolation")
        # 基类不能被这次继承改掉：DockerRuntime 仍必须是容器层。
        self.assertEqual(DockerRuntime.name, "docker")
        self.assertEqual(DockerRuntime.isolation_level, DOCKER_ISOLATION_LEVEL)
        self.assertEqual(DockerRuntime.container_runtime, "")

    def test_argv_contains_runtime_flag_and_all_hardening_flags(self):
        argv = build_run_argv(container_runtime=GVISOR_RUNTIME_NAME)
        joined = " ".join(argv)
        self.assertIn("--runtime " + GVISOR_RUNTIME_NAME, joined)
        self.assertIn("--network none", joined)
        self.assertIn("--read-only", joined)
        self.assertIn("--cap-drop ALL", joined)
        self.assertIn("no-new-privileges", joined)
        findings = audit_argv(argv, required_runtime=GVISOR_RUNTIME_NAME)
        self.assertTrue(findings["ok"], findings)
        self.assertTrue(findings["runtime_flag_correct"])

    def test_audit_catches_missing_runtime_flag(self):
        """漏掉 --runtime 时 Docker 会静默退回 runc，审计必须抓住这件事。"""
        argv = build_run_argv()  # 故意不传 container_runtime
        self.assertNotIn("--runtime", argv)
        findings = audit_argv(argv, required_runtime=GVISOR_RUNTIME_NAME)
        self.assertFalse(findings["ok"])
        self.assertFalse(findings["runtime_flag_correct"])
        # 不加 required_runtime 时不该凭空报错：这一条是调用方的问题。
        self.assertTrue(audit_argv(argv)["ok"])

    def test_argv_still_requires_pinned_digest(self):
        self.assertIn("@sha256:", DEFAULT_IMAGE)
        argv = build_run_argv(container_runtime=GVISOR_RUNTIME_NAME)
        self.assertIn(DEFAULT_IMAGE, argv)

    def test_platform_decision_matrix(self):
        self.assertEqual(decide_platform(_facts()), PLATFORM_KVM)
        self.assertEqual(decide_platform(_facts(system="Windows")), "")
        self.assertEqual(decide_platform(_facts(kvm_readable=False)), PLATFORM_PTRACE)
        self.assertEqual(decide_platform(_facts(kvm_present=False, kvm_readable=False)), PLATFORM_PTRACE)
        # 判据只看 runsc_registered；列表是给人看的证据，两者由 probe_host 保证一致。
        self.assertEqual(decide_platform(_facts(runsc_registered=False)), "")
        self.assertEqual(decide_platform(_facts(runsc_registered=True)), PLATFORM_KVM)
        self.assertEqual(decide_platform(_facts(docker_probe=_unavailable_docker_probe())), "")

    def test_platform_blocker_names_the_first_missing_thing(self):
        self.assertIn("Linux", platform_blocker(_facts(system="Windows")))
        self.assertIn("Docker", platform_blocker(_facts(docker_probe=_unavailable_docker_probe())))
        self.assertIn("runsc", platform_blocker(_facts(runsc_registered=False)))
        self.assertEqual(platform_blocker(_facts()), "")

    def test_gvisor_probe_fails_closed(self):
        from pq_agent.sandbox.gvisor_runtime import build_gvisor_probe
        probe = build_gvisor_probe(facts=_facts(system="Windows", kvm_present=False, kvm_readable=False))
        self.assertIsInstance(probe, GvisorProbe)
        self.assertFalse(probe.available)
        self.assertEqual(probe.platform, "")
        self.assertIn("Linux", probe.blocker)
        self.assertEqual(probe.runtime_name, GVISOR_RUNTIME_NAME)
        ok = build_gvisor_probe(facts=_facts())
        self.assertTrue(ok.available)
        self.assertEqual(ok.platform, PLATFORM_KVM)

    def test_unavailable_never_falls_back(self):
        runtime = GvisorRuntime(root=ROOT)
        result = runtime.execute(ExecRequest(code="print(1)", trusted_fixture=True))
        self.assertEqual(result.status, STATUS_POLICY_DENIED)
        self.assertFalse(result.executed)
        self.assertEqual(result.isolation_level, "gvisor")
        self.assertIn("不回落到 runc", result.message)
        self.assertIn("LocalRuntime", result.message)

    def test_capabilities_are_not_overclaimed_when_unavailable(self):
        runtime = GvisorRuntime(root=ROOT)
        caps = runtime.capabilities()
        if not runtime.available:
            self.assertEqual(caps["supported"], [])
            self.assertTrue(caps["unsupported"])
        self.assertIn(GVISOR_RUNTIME_NAME, json.dumps(caps, ensure_ascii=False))

    def test_capability_matrix_declares_a_known_gap(self):
        matrix = GvisorRuntime(root=ROOT).capability_matrix()
        for key in ("user_space_kernel", "syscall_filtering", "reduced_host_kernel_surface"):
            self.assertIn(key, matrix)
        self.assertIn("kernel_side_channel_proof", matrix)
        self.assertFalse(matrix["kernel_side_channel_proof"].supported)
        unsupported = [c.key for c in GVISOR_CAPABILITIES if not c.supported]
        self.assertEqual(unsupported, ["kernel_side_channel_proof"])

    def test_docker_capability_matrix_is_not_polluted_by_gvisor(self):
        docker_matrix = DockerRuntime(root=ROOT).capability_matrix()
        self.assertNotIn("user_space_kernel", docker_matrix)
        self.assertNotIn("kernel_side_channel_proof", docker_matrix)
        self.assertEqual(len(docker_matrix), 12)

    def test_blocked_result_does_not_inherit_local_capabilities(self):
        """拒绝结果也必须报告自己的隔离级别与能力清单，不能套 LocalRuntime 的。"""
        from pq_agent.sandbox.base import blocked_result
        request = ExecRequest(code="print(1)", trusted_fixture=True)
        result = blocked_result(
            request,
            status=STATUS_POLICY_DENIED,
            error_code="SANDBOX_CAPABILITY_UNSUPPORTED",
            message="测试",
            runtime="gvisor",
            isolation_level="gvisor",
            capabilities={"isolation_level": "gvisor"},
        )
        self.assertEqual(result.isolation_level, "gvisor")
        self.assertEqual(result.capabilities["isolation_level"], "gvisor")

    def test_capability_matrix_wiring_matches_request(self):
        """要求 network_isolation 时，Docker 家族应当用自己的矩阵判定，而不是本地矩阵。

        本机没有 Docker，所以结果必然是被拒绝的；要证明的是「拒绝的理由不是能力不存在」，
        也就是这条限制真的被登记为 Docker 支持过。
        """
        from pq_agent.sandbox.base import check_requested_capabilities
        request = ExecRequest(code="print(1)", trusted_fixture=True, require=("network_isolation",))
        docker_matrix = DockerRuntime(root=ROOT).capability_matrix()
        # 用本机矩阵会当场抛「没有登记这一项」；用 Docker 矩阵必须安静通过。
        with self.assertRaises(Exception):
            check_requested_capabilities(request)
        check_requested_capabilities(request, matrix=docker_matrix)
        result = DockerRuntime(root=ROOT).execute(request)
        self.assertEqual(result.error_code, "SANDBOX_CAPABILITY_UNSUPPORTED")
        self.assertIn("DockerRuntime 不可用", result.message)

    def test_redteam_corpus_gate_is_not_claimed(self):
        corpus = json.loads((ROOT / "agent_eval" / "cases" / "sandbox_redteam.json").read_text(encoding="utf-8"))
        note = corpus["isolation_note"]
        self.assertIn("不是安全隔离", note)
        cases = corpus["cases"]
        self.assertEqual(len(cases), 20)
        self.assertEqual(len({c["case_id"] for c in cases}), 20)
        report = gvisor_report(root=ROOT)
        self.assertFalse(report["available"])
        self.assertFalse(report["gate"]["passed"])
        self.assertFalse(report["gate"]["alternative_allowed"])

    def test_exposure_rules_are_declared(self):
        self.assertEqual(CLOUD_BIND_HOST, "127.0.0.1")
        self.assertFalse(PUBLIC_EXPOSURE_ALLOWED)
        self.assertTrue(SSH_TUNNEL_REQUIRED)
        report = gvisor_report(root=ROOT)
        exposure = report["exposure"]
        self.assertEqual(exposure["bind_host"], "127.0.0.1")
        self.assertFalse(exposure["public_exposure_allowed"])
        self.assertIn("ssh -L", exposure["note"])
        self.assertTrue(exposure["ssh_tunnel_required"])

    def test_bench_template_has_no_fabricated_numbers(self):
        path = ROOT / BENCH_TEMPLATE_PATH
        self.assertTrue(path.is_file(), path)
        text = path.read_text(encoding="utf-8")
        self.assertIn("没有任何实测数字", text)
        self.assertIn("恶意样本阻断率", text)
        self.assertIn("良性样本通过率", text)
        plan = bench_plan()
        self.assertEqual(plan["warmup_runs"], 3)
        self.assertEqual(plan["repeat_runs"], 30)
        self.assertEqual(plan["sample_count"], 20)
        self.assertEqual(tuple(plan["runtimes"]), ("runc", "runsc"))
        self.assertEqual(len(BENCH_METRICS), 9)
        for metric in BENCH_METRICS:
            self.assertIn(metric, json.dumps(plan, ensure_ascii=False))

    def test_deploy_assets_are_honest_about_never_running(self):
        deploy = ROOT / "deploy" / "gvisor"
        self.assertTrue(deploy.is_dir(), deploy)
        for name in (
            "README.md",
            "probe_host.sh",
            "install_runsc.sh",
            "register_runtime.sh",
            "verify_gvisor.sh",
            "uninstall_runsc.sh",
            "bench.sh",
            "daemon.json.example",
        ):
            self.assertTrue((deploy / name).is_file(), name)
        bench = (deploy / "bench.sh").read_text(encoding="utf-8")
        self.assertIn("exit 2", bench)
        register = (deploy / "register_runtime.sh").read_text(encoding="utf-8")
        # 只允许在注释里提到 default-runtime，不允许真的写进 JSON。
        self.assertNotIn('"default-runtime"', register)
        daemon = json.loads((deploy / "daemon.json.example").read_text(encoding="utf-8"))
        self.assertIn("runsc", daemon["runtimes"])
        self.assertNotIn("default-runtime", daemon)

    def test_local_platform_report_is_honest(self):
        facts = probe_host()
        self.assertIsInstance(facts.to_dict(), dict)
        report = gvisor_report(root=ROOT)
        if facts.system != "Linux":
            self.assertFalse(report["available"])
            self.assertIn("Linux", report["probe"]["blocker"])
        self.assertEqual(KVM_DEVICE, "/dev/kvm")

    def test_evidence_bounds_are_documented(self):
        text = (ROOT / "docs" / "agent" / "GVISOR_DEPLOYMENT.md").read_text(encoding="utf-8")
        self.assertIn("证据边界", text)
        self.assertIn("无**：本机不是 Linux", text)
        self.assertIn("--runtime runsc", text)


if __name__ == "__main__":
    unittest.main()
