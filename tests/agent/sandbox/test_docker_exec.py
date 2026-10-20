"""A6-03：DockerRuntime 执行路径与 argv 安全审计的合同测试。

本机没有 Docker，所以这些用例不假装容器真的跑过。它们做两件事：
1. 用可注入的 spawn 驱动 _run_container 的真实代码路径，断言源码怎么送进容器、
   用户 stdin 怎么到用户代码、输出怎么被限住、超时后怎么清理容器。
2. 直接在本机跑一遍 CONTAINER_BOOTSTRAP 引导程序（除了容器边界，这条链路是真的），
   证明「源码 + stdin 走 JSON 信封」不是纸上设计。

真实容器验证仍然是 UNKNOWN，见 planning/agent/evidence/A6-03/ 的 blocker 说明。
"""

from __future__ import annotations

import io
import json
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

from pq_agent.sandbox.base import (
    HARD_MAX_SOURCE_BYTES,
    SANDBOX_CAPABILITY_UNSUPPORTED,
    SANDBOX_SOURCE_TOO_LARGE,
    SANDBOX_STATIC_BLOCKED,
    SANDBOX_TIMEOUT,
    STATUS_OK,
    ExecRequest,
)
from pq_agent.sandbox.docker_runtime import (
    CONTAINER_BOOTSTRAP,
    DockerProbe,
    DockerRuntime,
    MountSpec,
    audit_argv,
    build_run_argv,
)

ROOT = pathlib.Path(__file__).resolve().parents[3]
Q = chr(39)


def available_probe() -> DockerProbe:
    """一个「Docker 可用」的探测结果：本机没有 Docker，只能这样驱动执行路径。"""
    return DockerProbe(True, "docker", "27.0.0", "27.0.0", "", "2026-10-16T00:00:00+0800")


def absent_probe() -> DockerProbe:
    return DockerProbe(False, "docker", "", "", "命令不存在：docker", "2026-10-16T00:00:00+0800")


class CaptureStdin:
    """假的子进程 stdin：写完关闭后仍然能读到内容。"""

    def __init__(self) -> None:
        self.data = b""
        self.closed = False

    def write(self, chunk: bytes) -> int:
        self.data += chunk
        return len(chunk)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    """假的子进程：只需要 Popen 被用到的那几个成员。"""

    def __init__(self, *, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0, hang: bool = False) -> None:
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.stdin = CaptureStdin()
        self.returncode = None if hang else returncode
        self.hang = hang
        self.terminated = False

    def wait(self, timeout: float | None = None):
        if self.hang:
            raise subprocess.TimeoutExpired(cmd="docker run", timeout=timeout)
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.terminated = True


class SpawnRecorder:
    """记录每次启动的 argv，并交出预置的假进程。"""

    def __init__(self, process: FakeProcess) -> None:
        self.process = process
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        return self.process


def make_runtime(spawn) -> DockerRuntime:
    # allow_untrusted 是部署方开关：本机没有 Docker，测试里显式打开才能驱动执行路径。
    return DockerRuntime(probe=available_probe(), spawn=spawn, allow_untrusted=True)


def allowed_request(**overrides) -> ExecRequest:
    payload = {
        "code": "print(1)",
        "timeout_s": 5.0,
    }
    payload.update(overrides)
    return ExecRequest(**payload)


class A6_03_ArgvAudit(unittest.TestCase):
    """argv 安全审计：每一种等价写法都必须被拒，不能只认一种拼法。"""

    def assert_rejected(self, extra, key: str) -> None:
        findings = audit_argv(build_run_argv() + list(extra))
        self.assertFalse(findings[key], key + " 应该判为不安全：" + repr(extra))
        self.assertFalse(findings["ok"], "整体结论应该是不安全：" + repr(extra))

    def test_baseline_argv_passes(self) -> None:
        findings = audit_argv(build_run_argv())
        self.assertTrue(findings["ok"], "合规配置必须判为安全，否则下面的拒绝测试没有意义")
        self.assertEqual(findings["mount_count"], 0)

    def test_rejects_privileged_bare_and_inline(self) -> None:
        self.assert_rejected(["--privileged"], "no_privileged")
        self.assert_rejected(["--privileged=true"], "no_privileged")

    def test_rejects_root_user_forms(self) -> None:
        self.assert_rejected(["--user", "0"], "non_root_user")
        self.assert_rejected(["--user=0"], "non_root_user")
        self.assert_rejected(["--user", "0:0"], "non_root_user")
        self.assert_rejected(["--user", "root"], "non_root_user")
        self.assert_rejected(["--user=root"], "non_root_user")

    def test_rejects_non_positive_pids_limit(self) -> None:
        self.assert_rejected(["--pids-limit=-1"], "pids_limited")
        self.assert_rejected(["--pids-limit", "0"], "pids_limited")

    def test_rejects_cap_add_any_form(self) -> None:
        self.assert_rejected(["--cap-add", "SYS_ADMIN"], "no_cap_add")
        self.assert_rejected(["--cap-add=SYS_ADMIN"], "no_cap_add")

    def test_rejects_host_network_inline_form(self) -> None:
        self.assert_rejected(["--network=host"], "no_host_network")
        self.assert_rejected(["--network", "host"], "no_host_network")

    def test_rejects_host_pid_inline_form(self) -> None:
        self.assert_rejected(["--pid=host"], "no_pid_host")
        self.assert_rejected(["--pid", "host"], "no_pid_host")

    def test_rejects_arbitrary_host_mount(self) -> None:
        argv = build_run_argv() + [
            "--mount",
            "type=bind,source=C:/Windows/System32,target=/host,readonly",
        ]
        findings = audit_argv(argv)
        self.assertFalse(findings["no_arbitrary_host_mount"])
        self.assertFalse(findings["ok"])

    def test_rejects_docker_socket_mount(self) -> None:
        argv = build_run_argv() + [
            "--mount",
            "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock,readonly",
        ]
        findings = audit_argv(argv)
        self.assertFalse(findings["no_docker_socket"])
        self.assertFalse(findings["ok"])

    def test_inline_forms_of_safe_flags_still_pass(self) -> None:
        """规范化必须双向成立：安全配置写成 --flag=value 也要算通过。"""
        plain = build_run_argv()
        converted: list[str] = []
        index = 0
        value_flags = {
            "--network", "--user", "--cap-drop", "--security-opt", "--pids-limit",
            "--memory", "--memory-swap", "--cpus", "--tmpfs", "--workdir", "--env",
        }
        while index < len(plain):
            token = plain[index]
            if token in value_flags and index + 1 < len(plain):
                converted.append(token + "=" + plain[index + 1])
                index += 2
                continue
            converted.append(token)
            index += 1
        findings = audit_argv(converted)
        self.assertTrue(findings["ok"], "等价的 --flag=value 写法不该被判失败")

    def test_required_runtime_must_be_explicit(self) -> None:
        findings = audit_argv(build_run_argv(), required_runtime="runsc")
        self.assertFalse(findings["runtime_flag_correct"])
        self.assertFalse(findings["ok"])
        findings_ok = audit_argv(build_run_argv(container_runtime="runsc"), required_runtime="runsc")
        self.assertTrue(findings_ok["runtime_flag_correct"])
        self.assertTrue(findings_ok["ok"])


class A6_03_ContainerEntry(unittest.TestCase):
    """容器入口：源码不能靠 /work/main.py，必须走 stdin 信封。"""

    def test_entry_does_not_reference_work_main_py(self) -> None:
        argv = build_run_argv()
        joined = " ".join(argv)
        self.assertNotIn("main.py", joined, "容器命令不能再读一个从未创建的 /work/main.py")
        self.assertNotIn("chr(47)", joined, "旧的 chr() 拼路径写法必须消失")
        self.assertEqual(argv[-1], CONTAINER_BOOTSTRAP)
        self.assertIn("-X", argv, "容器必须显式声明 utf8 模式，-I 会屏蔽 PYTHONIOENCODING")
        self.assertEqual(argv[argv.index("-X") + 1], "utf8")

    def test_bootstrap_really_delivers_source_and_stdin(self) -> None:
        """在本机跑一遍引导程序：除了容器边界，这条链路是真的。"""
        code = "import sys" + chr(10) + "print(" + Q + "GOT:" + Q + " + sys.stdin.read().strip())"
        envelope = json.dumps({"source": code, "stdin": "hello-世界" + chr(10)}).encode("utf-8")
        completed = subprocess.run(
            [sys.executable, "-I", "-B", "-X", "utf8", "-c", CONTAINER_BOOTSTRAP],
            input=envelope, capture_output=True, timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", errors="replace"))
        self.assertEqual(completed.stdout.decode("utf-8", errors="replace").strip(), "GOT:hello-世界")

    def test_bootstrap_propagates_nonzero_exit(self) -> None:
        """用户代码抛异常时不能被引导程序吞掉，退出码必须非 0。"""
        code = "raise SystemExit(3)"
        envelope = json.dumps({"source": code, "stdin": ""}).encode("utf-8")
        completed = subprocess.run(
            [sys.executable, "-I", "-B", "-X", "utf8", "-c", CONTAINER_BOOTSTRAP],
            input=envelope, capture_output=True, timeout=30,
        )
        self.assertEqual(completed.returncode, 3)


class A6_03_ExecPath(unittest.TestCase):
    """执行路径：源码 / stdin / 输出上限 / 超时清理 / 能力拒绝。"""

    def test_envelope_carries_source_and_stdin(self) -> None:
        process = FakeProcess(stdout=b"ok" + chr(10).encode("utf-8"))
        spawn = SpawnRecorder(process)
        runtime = make_runtime(spawn)
        result = runtime.execute(allowed_request(code="print(7)", stdin="data-1"))
        self.assertEqual(result.status, STATUS_OK)
        self.assertEqual(result.stdout.strip(), "ok")
        self.assertEqual(len(spawn.calls), 1)
        envelope = json.loads(process.stdin.data.decode("utf-8"))
        self.assertEqual(envelope["source"], "print(7)")
        self.assertEqual(envelope["stdin"], "data-1")
        self.assertTrue(process.stdin.closed, "信封写完必须关闭 stdin，否则容器会一直等")

    def test_output_is_bounded_while_reader_drains(self) -> None:
        process = FakeProcess(stdout=b"y" * 200000)
        spawn = SpawnRecorder(process)
        runtime = make_runtime(spawn)
        result = runtime.execute(allowed_request(max_output_bytes=1024))
        self.assertTrue(result.truncated)
        self.assertLessEqual(len(result.stdout.encode("utf-8")), 1024)
        self.assertEqual(result.status, STATUS_OK)

    def test_timeout_removes_the_container(self) -> None:
        process = FakeProcess(hang=True)
        spawn = SpawnRecorder(process)
        runtime = make_runtime(spawn)
        with mock.patch("pq_agent.sandbox.docker_runtime.subprocess.run") as fake_run:
            result = runtime.execute(allowed_request(timeout_s=0.05))
        self.assertEqual(result.error_code, SANDBOX_TIMEOUT)
        self.assertTrue(result.executed)
        self.assertTrue(process.terminated, "超时后必须终止子进程")
        container_name = runtime.startup_records[-1]["container_name"]
        self.assertTrue(fake_run.called, "超时后必须真的执行清理命令")
        cleanup = fake_run.call_args_list[-1][0][0]
        self.assertEqual(cleanup[1:4], ["rm", "-f", container_name])
        self.assertEqual(runtime.startup_records[-1]["outcome"], SANDBOX_TIMEOUT)

    def test_startup_record_captures_argv_and_container_name(self) -> None:
        process = FakeProcess()
        spawn = SpawnRecorder(process)
        runtime = make_runtime(spawn)
        runtime.execute(allowed_request())
        record = runtime.startup_records[-1]
        name = record["container_name"]
        self.assertTrue(name.startswith("docker-"))
        self.assertIn(name, record["argv"])
        self.assertEqual(record["outcome"], "exit_0")
        self.assertIsInstance(record["elapsed_ms"], float)

    def test_source_limit_blocks_before_spawn(self) -> None:
        process = FakeProcess()
        spawn = SpawnRecorder(process)
        runtime = make_runtime(spawn)
        result = runtime.execute(allowed_request(code="x = 1" + chr(10), max_source_bytes=4))
        self.assertEqual(result.error_code, SANDBOX_SOURCE_TOO_LARGE)
        self.assertEqual(spawn.calls, [], "源码超限必须在启动容器之前就拒绝")

    def test_stdin_limit_blocks_before_spawn(self) -> None:
        process = FakeProcess()
        spawn = SpawnRecorder(process)
        runtime = make_runtime(spawn)
        result = runtime.execute(allowed_request(stdin="z" * 64, max_source_bytes=8))
        self.assertEqual(result.error_code, SANDBOX_SOURCE_TOO_LARGE)
        self.assertEqual(spawn.calls, [])

    def test_static_block_never_spawns(self) -> None:
        process = FakeProcess()
        spawn = SpawnRecorder(process)
        runtime = make_runtime(spawn)
        result = runtime.execute(allowed_request(code="import os" + chr(10) + "os.system(" + Q + "x" + Q + ")"))
        self.assertEqual(result.error_code, SANDBOX_STATIC_BLOCKED)
        self.assertEqual(spawn.calls, [])

    def test_unavailable_runtime_never_falls_back_and_never_spawns(self) -> None:
        process = FakeProcess()
        spawn = SpawnRecorder(process)
        runtime = DockerRuntime(probe=absent_probe(), spawn=spawn, allow_untrusted=True)
        result = runtime.execute(allowed_request())
        self.assertEqual(result.error_code, SANDBOX_CAPABILITY_UNSUPPORTED)
        self.assertEqual(result.isolation_level, "container")
        self.assertEqual(spawn.calls, [], "Docker 不可用时绝不能改走别的运行时")

    def test_effective_max_source_is_capped(self) -> None:
        request = ExecRequest(code="print(1)", max_source_bytes=10 ** 9)
        self.assertEqual(request.effective_max_source(), HARD_MAX_SOURCE_BYTES)
        self.assertEqual(ExecRequest(code="print(1)", max_source_bytes=16).effective_max_source(), 16)


if __name__ == "__main__":
    unittest.main()