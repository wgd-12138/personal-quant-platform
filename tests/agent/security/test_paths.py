"""paths.py 的单元测试：项目根内路径校验、数据分级、链接拒绝、TOCTOU。

刻意不使用 tempfile（本机 DSH 沙箱把 os.mkdir 的 0o700 映射成自锁 DACL，
tempfile 会 PermissionError），临时目录统一放在仓库内 .tmp/ 下。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import unittest
import uuid
from pathlib import Path

from pq_agent.security.paths import (
    DEFAULT_ROOT,
    PathChangedError,
    PathEscapeError,
    PathLinkDeniedError,
    PathNotFoundError,
    PathPolicyError,
    PathTraversalError,
    classify_path,
    detect_link_component,
    is_network_or_device_path,
    read_within_root,
    resolve_within_root,
)


def _scratch(name: str = "case") -> Path:
    root = DEFAULT_ROOT / ".tmp" / "a0-04-tests" / f"{name}-{uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True, exist_ok=True)
    return root


class ResolveWithinRootTests(unittest.TestCase):
    def test_legal_project_file_resolves(self) -> None:
        resolved = resolve_within_root("pyproject.toml", must_exist=True)
        self.assertIsInstance(resolved, Path)
        self.assertTrue(resolved.is_file())
        self.assertEqual(resolved.name, "pyproject.toml")

    def test_classify_flag_returns_pair(self) -> None:
        resolved, label = resolve_within_root("pyproject.toml", must_exist=True, classify=True)
        self.assertEqual(label, "documentation")
        self.assertTrue(resolved.name == "pyproject.toml")

    def test_missing_file_raises_not_found(self) -> None:
        with self.assertRaises(PathNotFoundError) as ctx:
            resolve_within_root("docs/agent/__not_exists__.md", must_exist=True)
        self.assertIn("PATH_NOT_FOUND", str(ctx.exception))

    def test_missing_file_allowed_without_must_exist(self) -> None:
        resolved = resolve_within_root("docs/agent/__not_exists__.md")
        self.assertTrue(str(resolved).endswith("__not_exists__.md"))


class TraversalTests(unittest.TestCase):
    def test_parent_segment_is_denied(self) -> None:
        for raw in ["../x", "docs/../x", "..\\x", "a/../../b"]:
            with self.subTest(raw=raw):
                with self.assertRaises(PathTraversalError):
                    resolve_within_root(raw)

    def test_absolute_path_is_denied_by_default(self) -> None:
        with self.assertRaises(PathEscapeError) as ctx:
            resolve_within_root(str(DEFAULT_ROOT / "pyproject.toml"))
        self.assertIn("PATH_ABSOLUTE_NOT_ALLOWED", str(ctx.exception))

    def test_absolute_inside_root_is_allowed_when_opted_in(self) -> None:
        resolved = resolve_within_root(
            str(DEFAULT_ROOT / "pyproject.toml"), allow_absolute=True, must_exist=True
        )
        self.assertTrue(resolved.is_file())

    def test_absolute_outside_root_is_denied_even_when_opted_in(self) -> None:
        with self.assertRaises(PathEscapeError):
            resolve_within_root("C:\\Windows\\win.ini", allow_absolute=True)

    def test_drive_relative_is_denied(self) -> None:
        with self.assertRaises(PathEscapeError) as ctx:
            resolve_within_root("C:src/x")
        self.assertIn("PATH_DRIVE_RELATIVE_DENIED", str(ctx.exception))

    def test_empty_and_nul_are_denied(self) -> None:
        with self.assertRaises(PathPolicyError):
            resolve_within_root("")
        with self.assertRaises(PathPolicyError) as ctx:
            resolve_within_root("a\x00b")
        self.assertIn("PATH_NUL_BYTE", str(ctx.exception))

    def test_non_string_is_denied(self) -> None:
        with self.assertRaises(PathPolicyError):
            resolve_within_root(123)  # type: ignore[arg-type]


class NetworkAndDeviceTests(unittest.TestCase):
    def test_network_paths_are_denied(self) -> None:
        for raw in ["\\\\server\\share\\x", "//server/share/x"]:
            with self.subTest(raw=raw):
                with self.assertRaises(PathEscapeError) as ctx:
                    resolve_within_root(raw)
                self.assertIn("PATH_NETWORK_DENIED", str(ctx.exception))

    def test_device_paths_are_denied(self) -> None:
        for raw in ["\\\\?\\C:\\x", "\\\\.\\PhysicalDrive0"]:
            with self.subTest(raw=raw):
                with self.assertRaises(PathEscapeError):
                    resolve_within_root(raw)

    def test_detector_helper(self) -> None:
        self.assertTrue(is_network_or_device_path("\\\\srv\\share"))
        self.assertTrue(is_network_or_device_path("//srv/share"))
        self.assertFalse(is_network_or_device_path("docs/readme.md"))


class ReservedAndSecretTests(unittest.TestCase):
    def test_reserved_device_names_are_denied(self) -> None:
        for raw in ["CON", "nul", "COM1.txt", "lpt9"]:
            with self.subTest(raw=raw):
                with self.assertRaises(PathPolicyError) as ctx:
                    resolve_within_root(raw)
                self.assertIn("PATH_RESERVED_NAME", str(ctx.exception))

    def test_secret_paths_are_denied(self) -> None:
        for raw in ["config/local.yaml", ".env"]:
            with self.subTest(raw=raw):
                with self.assertRaises(PathEscapeError) as ctx:
                    resolve_within_root(raw)
                self.assertIn("PATH_SECRET_DENIED", str(ctx.exception))

    def test_secret_allowed_only_when_explicit(self) -> None:
        resolved = resolve_within_root("config/local.yaml", allow_secret=True)
        self.assertTrue(str(resolved).endswith("local.yaml"))


class ClassifyTests(unittest.TestCase):
    def test_expected_labels(self) -> None:
        cases = {
            "config/local.yaml": "secret",
            "data/positions.csv": "user_data",
            "reports/2026-07-09/daily_pack.md": "generated_report",
            "logs/run.log": "generated_report",
            "planning/runs/run_audit_2026-07-09.md": "control_plane",
            "tests/fixtures/manifest.json": "fixture",
            "tests/agent/security/test_paths.py": "test",
            "src/pq_agent/security/paths.py": "source_code",
            "docs/agent/SECRET_POLICY.md": "documentation",
            "pyproject.toml": "documentation",
            "some/unknown.bin": "unknown",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(classify_path(raw), expected)


class LinkTests(unittest.TestCase):
    def test_injected_detector_is_honoured(self) -> None:
        with self.assertRaises(PathLinkDeniedError) as ctx:
            resolve_within_root("docs/agent/x.md", link_detector=lambda p: True)
        self.assertIn("PATH_SYMLINK_DENIED", str(ctx.exception))

    def test_detect_link_component_returns_first_link(self) -> None:
        scratch = _scratch("links")
        try:
            (scratch / "real").mkdir()
            (scratch / "real" / "f.txt").write_text("x", encoding="utf-8")
            target = scratch / "real" / "f.txt"
            found = detect_link_component(target, stop=scratch, link_detector=lambda p: p.name == "real")
            self.assertEqual(found, scratch / "real")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_real_symlink_is_denied(self) -> None:
        scratch = _scratch("symlink")
        try:
            (scratch / "real").mkdir()
            (scratch / "real" / "f.txt").write_text("x", encoding="utf-8")
            link = scratch / "link"
            try:
                os.symlink(scratch / "real", link, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"当前环境不允许创建符号链接：{exc}")
            self.assertTrue(link.is_symlink())
            with self.assertRaises(PathLinkDeniedError):
                resolve_within_root("link/f.txt", root=scratch)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_real_junction_is_denied(self) -> None:
        # junction 在 Windows 上不需要管理员权限（mklink /J），因此这条必须真实执行
        scratch = _scratch("junction")
        junction = scratch / "junc"
        try:
            (scratch / "real").mkdir()
            (scratch / "real" / "f.txt").write_text("x", encoding="utf-8")
            proc = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(scratch / "real")],
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0:
                self.skipTest(
                    "当前环境不允许创建 junction："
                    + (proc.stderr.strip() or proc.stdout.strip())
                )
            self.assertTrue(getattr(junction, "is_junction", lambda: False)())
            self.assertFalse(junction.is_symlink())
            with self.assertRaises(PathLinkDeniedError):
                resolve_within_root("junc/f.txt", root=scratch)
        finally:
            if junction.exists():
                os.rmdir(junction)  # 只摘链接，不递归目标
            shutil.rmtree(scratch, ignore_errors=True)


class ReadWithinRootTests(unittest.TestCase):
    def test_reads_legal_file(self) -> None:
        path, data = read_within_root("pyproject.toml")
        self.assertEqual(path.name, "pyproject.toml")
        self.assertIn(b"[project]", data)

    def test_missing_file_fails(self) -> None:
        with self.assertRaises(PathNotFoundError):
            read_within_root("docs/agent/__not_exists__.md")

    def test_size_limit_is_enforced(self) -> None:
        with self.assertRaises(PathPolicyError) as ctx:
            read_within_root("pyproject.toml", max_bytes=5)
        self.assertIn("PATH_TOO_LARGE", str(ctx.exception))

    def test_invalid_limit_fails(self) -> None:
        with self.assertRaises(PathPolicyError):
            read_within_root("pyproject.toml", max_bytes=0)

    def test_traversal_never_reaches_read(self) -> None:
        with self.assertRaises(PathTraversalError):
            read_within_root("../pyproject.toml")

    def test_toctou_replacement_is_detected(self) -> None:
        scratch = _scratch("toctou")
        try:
            target = scratch / "payload.txt"
            target.write_text("original", encoding="utf-8")

            def swap(path: Path) -> None:
                os.remove(path)
                path.write_text("replaced-content", encoding="utf-8")

            with self.assertRaises(PathChangedError) as ctx:
                read_within_root("payload.txt", root=scratch, _between_stat_and_open=swap)
            self.assertIn("PATH_CHANGED_DURING_OPEN", str(ctx.exception))
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def test_untouched_file_reads_fine_with_hook(self) -> None:
        scratch = _scratch("nochange")
        try:
            target = scratch / "payload.txt"
            target.write_text("original", encoding="utf-8")
            _, data = read_within_root(
                "payload.txt", root=scratch, _between_stat_and_open=lambda p: None
            )
            self.assertEqual(data, b"original")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
