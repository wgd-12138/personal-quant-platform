"""项目根路径校验（fail-closed）。

解决四类问题：
1. traversal：任何 ".." 段一律拒绝，而不是"归一化后再比较"。
2. symlink / junction 越界：项目根之下的任何链接组件都直接拒绝（链接可被改指，属 TOCTOU 面）。
3. UNC / 网络路径与 Windows 设备路径：一律拒绝。
4. TOCTOU：读文件时先 stat 再 open，用文件标识比对，变化即失败。

数据分级（classify_path）与密钥策略共用同一套路径口径，避免"这里算敏感、那里算普通"。
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "DEFAULT_ROOT",
    "PathPolicyError",
    "PathTraversalError",
    "PathEscapeError",
    "PathLinkDeniedError",
    "PathNotFoundError",
    "PathChangedError",
    "classify_path",
    "is_network_or_device_path",
    "detect_link_component",
    "resolve_within_root",
    "read_within_root",
]

# src/pq_agent/security/paths.py -> security / pq_agent / src / 项目根
DEFAULT_ROOT = Path(__file__).resolve().parents[3]


class PathPolicyError(ValueError):
    """路径违反策略。"""


class PathTraversalError(PathPolicyError):
    """包含 ".." 段。"""


class PathEscapeError(PathPolicyError):
    """绝对路径 / 跨盘 / 网络路径 / 越界 / 敏感路径。"""


class PathLinkDeniedError(PathPolicyError):
    """路径中含 symlink 或 junction 组件。"""


class PathNotFoundError(PathPolicyError):
    """要求存在但不存在。"""


class PathChangedError(PathPolicyError):
    """文件在 stat 与 open 之间被替换（TOCTOU）。"""


_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_NETWORK_PREFIXES = ("\\\\", "//")
_DEVICE_PREFIXES = ("\\\\?\\", "\\\\.\\", "//?/", "//./")
_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)

# 数据分级规则：顺序即优先级；相对路径统一用 POSIX 形式比对
_CLASS_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("secret", ("config/local.yaml", ".env")),
    ("user_data", ("data",)),
    ("generated_report", ("reports", "logs")),
    ("control_plane", ("planning/runs",)),
    ("fixture", ("tests/fixtures",)),
    ("test", ("tests",)),
    ("source_code", ("src",)),
    ("documentation", ("docs", "planning")),
)

_DOC_ROOT_FILES = frozenset(
    {
        "pyproject.toml",
        "uv.lock",
        ".gitignore",
        "AGENTS.md",
        "CLAUDE.md",
        "CONTEXT.md",
        "README.md",
        "ARCHITECTURE.md",
        "task_plan.md",
        "progress.md",
        "findings.md",
    }
)


def _as_posix(relative: str) -> str:
    return relative.replace("\\", "/").strip("/")


def classify_path(relative: str) -> str:
    """按项目口径给相对路径分级。"""
    posix = _as_posix(relative)
    lowered = posix.lower()
    for name, prefixes in _CLASS_RULES:
        for prefix in prefixes:
            if lowered == prefix or lowered.startswith(prefix + "/"):
                return name
    if lowered.endswith(".md") or lowered in _DOC_ROOT_FILES:
        return "documentation"
    return "unknown"


def is_network_or_device_path(raw: str) -> bool:
    if raw.startswith(_DEVICE_PREFIXES):
        return True
    return raw.startswith(_NETWORK_PREFIXES)


def _is_link(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
    except OSError:
        return False
    try:
        is_junction = getattr(path, "is_junction", None)
        if is_junction is not None and is_junction():
            return True
    except OSError:
        return False
    return False


def detect_link_component(
    candidate: Path,
    *,
    stop: Path,
    link_detector: Callable[[Path], bool] | None = None,
) -> Path | None:
    """返回 stop 之下第一个链接组件；link_detector 可注入以便确定性地测拒绝路径。"""
    detector = link_detector or _is_link
    try:
        parts = candidate.relative_to(stop).parts
    except ValueError:
        return None
    current = Path(stop)
    for part in parts:
        current = current / part
        if detector(current):
            return current
    return None


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child_norm = os.path.normcase(os.path.abspath(str(child)))
        parent_norm = os.path.normcase(os.path.abspath(str(parent)))
    except (OSError, ValueError):
        return False
    return child_norm == parent_norm or child_norm.startswith(parent_norm + os.sep)


def _relative_posix(child: Path, parent: Path) -> str:
    try:
        return _as_posix(os.path.relpath(str(child), str(parent)))
    except ValueError:
        return _as_posix(str(child))


def resolve_within_root(
    raw: str | os.PathLike[str],
    *,
    root: str | os.PathLike[str] | None = None,
    must_exist: bool = False,
    allow_absolute: bool = False,
    allow_secret: bool = False,
    link_detector: Callable[[Path], bool] | None = None,
    classify: bool = False,
) -> Path | tuple[Path, str]:
    """把输入解析成项目根内的真实路径，任一条不满足即失败。"""
    if isinstance(raw, str):
        text = raw
    else:
        try:
            text = os.fspath(raw)
        except TypeError:
            raise PathPolicyError(
                f"PATH_INPUT_TYPE: 需要 str，实际 {type(raw).__name__}"
            ) from None
    if not isinstance(text, str):
        raise PathPolicyError(f"PATH_INPUT_TYPE: 需要 str，实际 {type(raw).__name__}")
    if "\x00" in text:
        raise PathPolicyError("PATH_NUL_BYTE")
    candidate_raw = text.strip()
    if not candidate_raw:
        raise PathPolicyError("PATH_EMPTY")

    root_path = Path(os.path.abspath(str(root))) if root is not None else DEFAULT_ROOT

    if is_network_or_device_path(candidate_raw):
        raise PathEscapeError(f"PATH_NETWORK_DENIED: {candidate_raw}")

    if _DRIVE_RE.match(candidate_raw):
        if len(candidate_raw) > 2 and candidate_raw[2] not in "\\/":
            raise PathEscapeError(f"PATH_DRIVE_RELATIVE_DENIED: {candidate_raw}")
        if not allow_absolute:
            raise PathEscapeError(f"PATH_ABSOLUTE_NOT_ALLOWED: {candidate_raw}")
        candidate = Path(candidate_raw)
        if os.path.normcase(candidate.drive) != os.path.normcase(root_path.drive):
            raise PathEscapeError(
                f"PATH_DRIVE_MISMATCH: {candidate.drive or '<none>'} != {root_path.drive or '<none>'}"
            )
    elif candidate_raw.startswith(("/", "\\")):
        if not allow_absolute:
            raise PathEscapeError(f"PATH_ABSOLUTE_NOT_ALLOWED: {candidate_raw}")
        candidate = Path(candidate_raw)
    else:
        parts = [part for part in re.split(r"[\\/]+", candidate_raw) if part not in ("", ".")]
        if not parts:
            raise PathPolicyError("PATH_EMPTY")
        for part in parts:
            if part == "..":
                raise PathTraversalError(f"PATH_TRAVERSAL_DENIED: {candidate_raw}")
            if part.split(".")[0].lower() in _RESERVED_NAMES:
                raise PathPolicyError(f"PATH_RESERVED_NAME: {part}")
        candidate = root_path.joinpath(*parts)

    if not _is_within(candidate, root_path):
        raise PathEscapeError(f"PATH_ESCAPE_DENIED: {candidate_raw}")

    relative = _relative_posix(candidate, root_path)
    path_class = classify_path(relative)
    if path_class == "secret" and not allow_secret:
        raise PathEscapeError(f"PATH_SECRET_DENIED: {relative}")

    link = detect_link_component(candidate, stop=root_path, link_detector=link_detector)
    if link is not None:
        raise PathLinkDeniedError(f"PATH_SYMLINK_DENIED: {_relative_posix(link, root_path)}")

    real_root = Path(os.path.realpath(root_path))
    real = Path(os.path.realpath(candidate))
    if not _is_within(real, real_root):
        raise PathEscapeError(f"PATH_ESCAPE_DENIED: {candidate_raw}")
    if must_exist and not real.exists():
        raise PathNotFoundError(f"PATH_NOT_FOUND: {_relative_posix(real, real_root)}")

    if classify:
        return real, classify_path(_relative_posix(real, real_root))
    return real


def _same_file(before: os.stat_result, after: os.stat_result) -> bool:
    if before.st_ino and after.st_ino:
        return (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
    return (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)


def read_within_root(
    raw: str | os.PathLike[str],
    *,
    root: str | os.PathLike[str] | None = None,
    max_bytes: int = 1_000_000,
    link_detector: Callable[[Path], bool] | None = None,
    allow_secret: bool = False,
    _between_stat_and_open: Callable[[Path], None] | None = None,
) -> tuple[Path, bytes]:
    """在项目根内读取文件；先 stat 再 open，标识不一致即判为被替换。

    _between_stat_and_open 仅供测试注入，用来确定性地复现 TOCTOU。
    """
    if max_bytes <= 0:
        raise PathPolicyError(f"PATH_MAX_BYTES_INVALID: {max_bytes}")
    resolved = resolve_within_root(
        raw,
        root=root,
        must_exist=True,
        allow_secret=allow_secret,
        link_detector=link_detector,
    )
    assert isinstance(resolved, Path)
    before = os.stat(resolved)
    if _between_stat_and_open is not None:
        _between_stat_and_open(resolved)
    descriptor = os.open(resolved, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        after = os.fstat(descriptor)
        if not _same_file(before, after):
            raise PathChangedError("PATH_CHANGED_DURING_OPEN: 文件在 stat 与 open 之间被替换")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(descriptor)

    data = b"".join(chunks)
    if len(data) > max_bytes:
        raise PathPolicyError(f"PATH_TOO_LARGE: 超过 {max_bytes} 字节上限")
    return resolved, data
