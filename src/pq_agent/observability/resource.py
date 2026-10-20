"""A1-06 OpenTelemetry resource：统一 service / version / environment / commit / graph_version。

企业里 trace 第一天能跑通，第二天就因为"资源属性各写各的"变成一堆无法关联的数据：
service.name 一会是 pq_agent 一会是 personal-quant，environment 缺失导致测试数据混进生产，
commit 缺失导致"这个 bug 到底是哪版引入的"无从回答。所以资源属性必须由一处统一产出，
并且**不可用时显式记 unknown**，而不是留空或编一个值。
"""

from __future__ import annotations

import importlib.metadata as md
import os
import pathlib
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

SERVICE_NAME = "pq_agent"

# 无法取得的值统一记这个，绝不编造
UNKNOWN = "unknown"

ENVIRONMENT_ENV_NAMES = ("PQ_AGENT_ENVIRONMENT", "APP_ENV", "ENVIRONMENT")
COMMIT_ENV_NAMES = ("PQ_AGENT_COMMIT", "GIT_COMMIT", "GITHUB_SHA")

VALID_ENVIRONMENTS = ("local", "dev", "staging", "prod")


def _safe_call(fn, fallback: str = UNKNOWN) -> str:
    """git / 元数据这类取值随时可能失败（无 git、无网络、被沙箱拦），失败就记 unknown。"""
    try:
        value = fn()
    except Exception:
        return fallback
    if value is None:
        return fallback
    text = str(value).strip()
    return text or fallback


def detect_version() -> str:
    """包版本；装不上时退回 pq_agent.__version__。"""
    def _from_metadata() -> str:
        return md.version("personal-quant-platform")

    def _from_module() -> str:
        from pq_agent import __version__

        return __version__

    value = _safe_call(_from_metadata)
    if value != UNKNOWN:
        return value
    return _safe_call(_from_module)


def detect_commit(project_root: pathlib.Path | None = None) -> str:
    """当前提交号。刻意不传 shell=True，也不读 .git 目录结构。"""
    root = project_root or _default_root()
    if root is None:
        return UNKNOWN

    def _run() -> str:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
            check=False,
        )
        if completed.returncode != 0:
            return UNKNOWN
        return completed.stdout

    return _safe_call(_run)


def _default_root() -> pathlib.Path | None:
    """从本文件往上找仓库根：src/pq_agent/observability/resource.py -> 仓库根。"""
    try:
        return pathlib.Path(__file__).resolve().parents[3]
    except Exception:
        return None


def detect_environment(environ: Mapping[str, str] | None = None) -> str:
    """运行环境。默认 local；只接受白名单里的值，非法值显式记 unknown。"""
    env = os.environ if environ is None else environ
    for name in ENVIRONMENT_ENV_NAMES:
        raw = env.get(name)
        if isinstance(raw, str) and raw.strip():
            text = raw.strip().lower()
            return text if text in VALID_ENVIRONMENTS else UNKNOWN
    return "local"


def environment_from_names(environ: Mapping[str, str] | None = None) -> str:
    """同 detect_environment 的环境变量入口，保留给测试与调用方显式传值。"""
    return detect_environment(environ)


@dataclass(frozen=True)
class ResourceConfig:
    """资源属性配置。graph_version 由 state 合同提供，这里只负责聚合。"""

    environment: str = "local"
    commit: str = UNKNOWN
    version: str = UNKNOWN
    graph_version: str = UNKNOWN
    service_name: str = SERVICE_NAME
    extra: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "service.name": self.service_name,
            "service.version": self.version,
            "deployment.environment": self.environment,
            "vcs.commit": self.commit,
            "pq_agent.graph_version": self.graph_version,
        }
        for key, value in (self.extra or {}).items():
            text = str(key)
            if text in payload:
                continue
            payload[text] = value
        return payload


def resource_attributes(config: ResourceConfig) -> dict[str, Any]:
    """给断言用的纯字典视图（不依赖 opentelemetry 包）。"""
    payload = config.to_dict()
    for key, value in list(payload.items()):
        if not isinstance(key, str) or not key.strip():
            raise ValueError("RESOURCE_ATTRIBUTE_INVALID: 属性键必须是非空字符串")
        if value is None:
            payload[key] = UNKNOWN
        elif isinstance(value, (str, int, float, bool)):
            payload[key] = value
        else:
            payload[key] = str(value)
    return payload


def build_resource(config: ResourceConfig | None = None, *, graph_version: str | None = None):
    """构造 opentelemetry Resource。缺 opentelemetry 时显式失败，不静默返回 None。"""
    active = config or ResourceConfig()
    if graph_version:
        active = ResourceConfig(
            environment=active.environment,
            commit=active.commit,
            version=active.version,
            graph_version=graph_version,
            service_name=active.service_name,
            extra=active.extra,
        )
    from opentelemetry.sdk.resources import Resource

    return Resource.create(resource_attributes(active))
