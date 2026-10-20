"""A5-03 演示工作目录的安全边界。

演示会写文件（checkpoint、审批记录、trace、收据），所以它必须有一个自己的目录，
并且 **只能** 动这个目录。这一层存在的理由很简单：一个能随手删库的演示脚本，
比没有演示脚本更危险。

三条硬规矩：
1. 演示根固定为 <repo>/.tmp/pq_agent_demo，调用方无法传入别的路径。
2. 清理只删这个根目录下的内容，删不到就如实报告，不吞错误。
3. 任何看起来像用户真实资产的位置（data/ reports/ logs/ planning/ docs/ src/）
   只要出现在删除目标链路上就直接拒绝。
"""

from __future__ import annotations

import os
import pathlib
import shutil
import stat
from dataclasses import dataclass, field
from typing import Any

DEMO_DIR_NAME = "pq_agent_demo"
DEMO_STAGE_DIR_NAME = "stage"

DEMO_PATH_DENIED = "DEMO_PATH_DENIED"
DEMO_RESET_REFUSED = "DEMO_RESET_REFUSED"
DEMO_ROOT_MISSING = "DEMO_ROOT_MISSING"
DEMO_NOT_ISOLATED = "DEMO_NOT_ISOLATED"

PROTECTED_NAMES: tuple[str, ...] = (
    "data",
    "reports",
    "logs",
    "planning",
    "docs",
    "src",
    "tests",
    "deploy",
    "agent_eval",
    ".git",
    ".venv",
)


class DemoPathError(RuntimeError):
    """路径安全检查失败。带着错误码，方便在验收脚本里断言。"""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code + ": " + detail)
        self.code = str(code)
        self.detail = str(detail)


def repo_root_of(start: pathlib.Path | None = None) -> pathlib.Path:
    """从本文件位置反推工程根：src/pq_agent/demo/paths.py → 上四级。"""

    if start is not None:
        return pathlib.Path(start).resolve()
    return pathlib.Path(__file__).resolve().parents[3]


def demo_root(repo_root: pathlib.Path | None = None) -> pathlib.Path:
    """演示根目录。固定推导，不接受调用方给任意路径。"""

    return repo_root_of(repo_root) / ".tmp" / DEMO_DIR_NAME


def demo_stage_root(repo_root: pathlib.Path | None = None) -> pathlib.Path:
    """演示运行期目录：checkpoint、审批记录、trace 都落在这里。"""

    return demo_root(repo_root) / DEMO_STAGE_DIR_NAME


def assert_not_protected(target: pathlib.Path, *, repo_root: pathlib.Path) -> pathlib.Path:
    """拒绝把用户真实资产当成演示目录。返回解析后的绝对路径。"""

    resolved = pathlib.Path(target).resolve()
    repo = pathlib.Path(repo_root).resolve()
    if resolved == repo:
        raise DemoPathError(DEMO_PATH_DENIED, "演示目录不能是工程根： " + str(resolved))
    if resolved == resolved.parent:
        raise DemoPathError(DEMO_PATH_DENIED, "演示目录不能是盘符根： " + str(resolved))
    if repo not in resolved.parents:
        raise DemoPathError(DEMO_PATH_DENIED, "演示目录必须在工程根之内： " + str(resolved))
    relative = resolved.relative_to(repo)
    parts = relative.parts
    if parts and parts[0] in PROTECTED_NAMES:
        raise DemoPathError(
            DEMO_PATH_DENIED,
            "演示目录不得落在受保护目录内： " + str(relative),
        )
    if parts and parts[0] != ".tmp":
        raise DemoPathError(
            DEMO_PATH_DENIED,
            "演示目录必须位于 <repo>/.tmp 下，收到： " + str(relative),
        )
    return resolved


def assert_reset_target(target: pathlib.Path, *, repo_root: pathlib.Path) -> pathlib.Path:
    """清理目标必须 **正好** 是演示根，不接受它的父目录或子目录。"""

    expected = demo_root(repo_root)
    resolved = assert_not_protected(target, repo_root=repo_root)
    if resolved != expected:
        raise DemoPathError(
            DEMO_RESET_REFUSED,
            "只允许清理演示根 " + str(expected) + "，收到： " + str(resolved),
        )
    # .tmp 本身不能被换成软链接指到别处，否则清理会打到别的盘上。
    tmp_parent = expected.parent
    if tmp_parent.is_symlink() or _is_junction(tmp_parent):
        raise DemoPathError(DEMO_RESET_REFUSED, ".tmp 是软链接或联接，拒绝清理： " + str(tmp_parent))
    return resolved


def _is_junction(path: pathlib.Path) -> bool:
    """Windows 联接点不是软链接，但同样会把路径指到别处。"""

    checker = getattr(path, "is_junction", None)
    if checker is None:
        return False
    try:
        return bool(checker())
    except OSError:
        return False


def _remove_tree(path: pathlib.Path) -> None:
    """删目录。只读文件也给一次机会；仍然删不掉就让它抛出去，不假装成功。"""

    def _on_error(func: Any, target: str, _exc: Any) -> None:
        try:
            os.chmod(target, stat.S_IWRITE)
        except OSError:
            pass
        func(target)

    shutil.rmtree(str(path), onerror=_on_error)


@dataclass
class ResetReport:
    """清理结果。removed 是真正删掉的顶层条目，failed 是删不掉的。"""

    root: str = ""
    removed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: bool = False
    reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.failed

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "removed": list(self.removed),
            "failed": list(self.failed),
            "skipped": self.skipped,
            "reason": self.reason,
            "ok": self.ok,
        }


def reset_demo(*, repo_root: pathlib.Path | None = None) -> ResetReport:
    """清理演示根。根不存在时也算成功（幂等），但会说明是 skipped。"""

    repo = repo_root_of(repo_root)
    target = assert_reset_target(demo_root(repo), repo_root=repo)
    report = ResetReport(root=str(target))
    if not target.exists():
        report.skipped = True
        report.reason = "演示根不存在，无需清理"
        return report
    # 双重确认：解析后的路径必须仍然等于预期，防止清理途中被替换。
    if pathlib.Path(target).resolve() != target:
        raise DemoPathError(DEMO_RESET_REFUSED, "演示根在解析后发生变化： " + str(target))
    for child in sorted(target.iterdir()):
        try:
            if child.is_dir() and not child.is_symlink():
                _remove_tree(child)
            else:
                child.unlink()
            report.removed.append(child.name)
        except OSError as exc:
            report.failed.append(child.name + ": " + type(exc).__name__ + " " + str(exc))
    return report


def ensure_demo_root(*, repo_root: pathlib.Path | None = None) -> pathlib.Path:
    """建好演示根与运行期目录，并复核它确实落在 .tmp 下。"""

    repo = repo_root_of(repo_root)
    root = assert_not_protected(demo_root(repo), repo_root=repo)
    stage = assert_not_protected(demo_stage_root(repo), repo_root=repo)
    stage.mkdir(parents=True, exist_ok=True)
    return root


def is_inside_demo(path: pathlib.Path, *, repo_root: pathlib.Path | None = None) -> bool:
    """判断某个路径是否在演示根内。用来给「没写到用户目录」做证据。"""

    resolved = pathlib.Path(path).resolve()
    root = demo_root(repo_root).resolve()
    return resolved == root or root in resolved.parents


__all__ = [
    "DEMO_DIR_NAME",
    "DEMO_PATH_DENIED",
    "DEMO_RESET_REFUSED",
    "DEMO_ROOT_MISSING",
    "DEMO_NOT_ISOLATED",
    "DEMO_STAGE_DIR_NAME",
    "PROTECTED_NAMES",
    "DemoPathError",
    "ResetReport",
    "assert_not_protected",
    "assert_reset_target",
    "demo_root",
    "demo_stage_root",
    "ensure_demo_root",
    "is_inside_demo",
    "repo_root_of",
    "reset_demo",
]