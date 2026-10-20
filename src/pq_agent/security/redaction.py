"""密钥与敏感信息脱敏（fail-closed）。

核心约定：
- 本模块只负责「减少泄露面」，不承担权限控制；权限由路径策略和人工确认承担。
- 任何无法确认已完整脱敏的输入都不得原样返回：一律抛 RedactionError。
- 不保存异常原文、HTTP 响应体、请求头；异常统一映射为「错误码 + 脱敏摘要」。
- 数据分级与 prompt injection 防护不是同一个控制：数据分级回答"这条数据能不能出门"，
  injection 防护回答"外部文本能不能改变控制流"，两者必须分别实现、分别验证。
"""

from __future__ import annotations

import datetime as dt
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "MARKER_PREFIX",
    "MARKER_SUFFIX",
    "RedactionError",
    "SecretLeakError",
    "SENSITIVE_KEY_NAMES",
    "SECRET_ENV_NAMES",
    "ScrubPolicy",
    "normalize_key",
    "is_sensitive_key",
    "scrub_text",
    "scrub_value",
    "find_secrets",
    "guard_payload",
    "safe_error_summary",
    "collect_secrets_from_environ",
    "secret_env_presence",
]


class RedactionError(RuntimeError):
    """脱敏无法完成（fail-closed）：调用方必须放弃该数据，不能退回原文。"""


class SecretLeakError(RedactionError):
    """脱敏后仍检出密钥或敏感模式，数据被拦下。"""


MARKER_PREFIX = "[REDACTED:"
MARKER_SUFFIX = "]"
REDACTED_VALUE = f"{MARKER_PREFIX}value{MARKER_SUFFIX}"
_MARKER_PATTERN = re.compile(r"\[REDACTED:[^\]]*\]")

DEFAULT_MAX_TEXT_LEN = 2000
DEFAULT_MAX_SUMMARY_LEN = 300
MAX_SCAN_DEPTH = 12

# 敏感字段名（规范化后比较：小写 + 只保留字母数字）
SENSITIVE_KEY_NAMES = frozenset(
    {
        "apikey",
        "apisecret",
        "accesstoken",
        "refreshtoken",
        "authtoken",
        "idtoken",
        "token",
        "authorization",
        "auth",
        "bearer",
        "cookie",
        "setcookie",
        "secret",
        "clientsecret",
        "password",
        "passwd",
        "pwd",
        "privatekey",
        "credential",
        "credentials",
        "sessionid",
        "sessionkey",
        "signingkey",
        "encryptionkey",
        "webhook",
        "webhookurl",
        "webhooksecret",
        "xapikey",
        "proxyauthorization",
    }
)

# 需要登记为「已知密钥」的环境变量名（只读取值，绝不打印）
SECRET_ENV_NAMES = (
    "FEISHU_WEBHOOK_URL",
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "LLM_API_KEY",
    "ANTHROPIC_API_KEY",
)


def normalize_key(key: str) -> str:
    """把字段名规范化，用于敏感字段比对。"""
    return re.sub(r"[^a-z0-9]", "", key.lower())


def is_sensitive_key(key: str) -> bool:
    return normalize_key(key) in SENSITIVE_KEY_NAMES


# ---------------------------------------------------------------- 文本模式
_FEISHU_WEBHOOK = re.compile(
    r"https?://[^\s\"'<>]*open\.feishu\.cn/open-apis/bot/v2/hook/[^\s\"'<>]+",
    re.IGNORECASE,
)
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)
_BEARER = re.compile(r"(?i)\b(bearer|token)\s+([A-Za-z0-9._\-]{6,})")
_ASSIGNMENT = re.compile(
    r"(?i)\b("
    r"api[_-]?key|apikey|access[_-]?token|auth[_-]?token|refresh[_-]?token|"
    r"client[_-]?secret|secret|password|passwd|authorization|signing[_-]?key"
    r")\b\s*[:=]\s*[\"']?([^\s\"',;)\]}]+)"
)
_OPENAI_KEY = re.compile(r"\bsk-[A-Za-z0-9_\-]{10,}")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{2,}")
_WIN_USER_PATH = re.compile(r"(?i)([A-Za-z]:[\\/]+Users[\\/]+)([^\\/:*?\"<>|\r\n]+)")
_UNIX_HOME = re.compile(r"(/home/|/Users/)([^/\s<>]+)")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")

# 顺序有意义：先处理跨行的私钥块，再处理具体模式
_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (_PRIVATE_KEY_BLOCK, f"{MARKER_PREFIX}private_key{MARKER_SUFFIX}"),
    (_FEISHU_WEBHOOK, f"{MARKER_PREFIX}feishu_webhook{MARKER_SUFFIX}"),
    (_BEARER, r"\1 " + f"{MARKER_PREFIX}bearer{MARKER_SUFFIX}"),
    (_JWT, f"{MARKER_PREFIX}jwt{MARKER_SUFFIX}"),
    (_OPENAI_KEY, f"{MARKER_PREFIX}api_key{MARKER_SUFFIX}"),
    (_ASSIGNMENT, r"\1=" + f"{MARKER_PREFIX}sensitive_field{MARKER_SUFFIX}"),
    (_WIN_USER_PATH, r"\1<user>"),
    (_UNIX_HOME, r"\1<user>"),
    (_EMAIL, f"{MARKER_PREFIX}email{MARKER_SUFFIX}"),
)

# 用于「残留检测」的模式（与上面的替换一一对应）
PATTERN_DETECTORS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", _PRIVATE_KEY_BLOCK),
    ("feishu_webhook", _FEISHU_WEBHOOK),
    ("bearer", _BEARER),
    ("jwt", _JWT),
    ("api_key", _OPENAI_KEY),
    ("assignment", _ASSIGNMENT),
    ("windows_user_path", _WIN_USER_PATH),
    ("unix_home_path", _UNIX_HOME),
    ("email", _EMAIL),
)


def _known_secrets(secrets: Iterable[str] | None) -> tuple[str, ...]:
    """只接受非空字符串，长串优先，避免短串先替换破坏长串。"""
    if secrets is None:
        return ()
    values = {s for s in secrets if isinstance(s, str) and s.strip()}
    return tuple(sorted(values, key=len, reverse=True))


# ---------------------------------------------------------------- 文本脱敏
def scrub_text(
    text: str,
    *,
    secrets: Iterable[str] | None = None,
    max_len: int = DEFAULT_MAX_TEXT_LEN,
    truncate_marker: str = "…[truncated]",
) -> str:
    """对单段文本做脱敏；任何异常输入都直接失败，不返回原文。"""
    if not isinstance(text, str):
        raise RedactionError(f"REDACTION_INPUT_TYPE: 需要 str，实际 {type(text).__name__}")
    if max_len <= 0:
        raise RedactionError(f"REDACTION_MAX_LEN_INVALID: {max_len}")

    cleaned = text
    for secret in _known_secrets(secrets):
        cleaned = cleaned.replace(secret, f"{MARKER_PREFIX}known_secret{MARKER_SUFFIX}")
    for pattern, replacement in _REPLACEMENTS:
        cleaned = pattern.sub(replacement, cleaned)
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len] + truncate_marker

    residual = find_secrets(cleaned, secrets=secrets)
    if residual:
        raise SecretLeakError(f"REDACTION_INCOMPLETE: {sorted(set(residual))}")
    return cleaned


# ---------------------------------------------------------------- 结构脱敏
@dataclass(frozen=True)
class ScrubPolicy:
    """脱敏规模上限；超出即失败，避免把整块原始数据带进 telemetry。"""

    max_depth: int = 8
    max_items: int = 200
    max_nodes: int = 5000
    max_text_len: int = DEFAULT_MAX_TEXT_LEN


def scrub_value(
    value: Any,
    *,
    secrets: Iterable[str] | None = None,
    policy: ScrubPolicy | None = None,
) -> Any:
    """递归脱敏任意 JSON 形状的数据；遇到未知类型直接失败（绝不 repr）。"""
    active = policy or ScrubPolicy()
    return _scrub(
        value,
        known=_known_secrets(secrets),
        policy=active,
        depth=0,
        key=None,
        counter=[0],
    )


def _bump(counter: list[int], policy: ScrubPolicy) -> None:
    counter[0] += 1
    if counter[0] > policy.max_nodes:
        raise RedactionError(f"REDACTION_TOO_MANY_NODES: {counter[0]} > {policy.max_nodes}")


def _scrub(
    value: Any,
    *,
    known: tuple[str, ...],
    policy: ScrubPolicy,
    depth: int,
    key: str | None,
    counter: list[int],
) -> Any:
    if depth > policy.max_depth:
        raise RedactionError(f"REDACTION_DEPTH_EXCEEDED: {depth} > {policy.max_depth}")
    _bump(counter, policy)

    if isinstance(value, str):
        return scrub_text(value, secrets=known, max_len=policy.max_text_len)
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        # 二进制一律不落地内容，只留长度
        return f"<bytes len={len(value)}>"
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, Path):
        return scrub_text(str(value), secrets=known, max_len=policy.max_text_len)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise RedactionError(f"REDACTION_NON_STRING_KEY: {type(raw_key).__name__}")
            if is_sensitive_key(raw_key):
                # 敏感字段整体替换，绝不递归（避免子结构里再藏一份）
                result[raw_key] = f"{MARKER_PREFIX}{raw_key}{MARKER_SUFFIX}"
                _bump(counter, policy)
                continue
            result[raw_key] = _scrub(
                item, known=known, policy=policy, depth=depth + 1, key=raw_key, counter=counter
            )
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        if len(value) > policy.max_items:
            raise RedactionError(f"REDACTION_TOO_MANY_ITEMS: {len(value)} > {policy.max_items}")
        return [
            _scrub(item, known=known, policy=policy, depth=depth + 1, key=key, counter=counter)
            for item in value
        ]
    raise RedactionError(f"REDACTION_UNSUPPORTED_TYPE: {type(value).__name__}")


# ---------------------------------------------------------------- 残留检测
def find_secrets(value: Any, *, secrets: Iterable[str] | None = None) -> list[str]:
    """扫描已脱敏数据里是否还有密钥残留；只返回标签，不返回值本身。"""
    known = _known_secrets(secrets)
    found: list[str] = []
    _scan(value, key=None, known=known, found=found, depth=0)
    return found


def _scan(
    value: Any,
    *,
    key: str | None,
    known: tuple[str, ...],
    found: list[str],
    depth: int,
) -> None:
    if depth > MAX_SCAN_DEPTH:
        raise RedactionError(f"REDACTION_DEPTH_EXCEEDED: {depth} > {MAX_SCAN_DEPTH}")
    if isinstance(value, str):
        # 先把脱敏标记去掉再扫描，避免标记本身触发模式误报
        stripped = _MARKER_PATTERN.sub("", value)
        for secret in known:
            if secret in stripped:
                found.append("known_secret")
                break
        for label, pattern in PATTERN_DETECTORS:
            if pattern.search(stripped):
                found.append(f"pattern:{label}")
        if key is not None and is_sensitive_key(key) and stripped.strip():
            found.append(f"sensitive_key:{normalize_key(key)}")
        return
    if value is None:
        return
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        if key is not None and is_sensitive_key(key):
            found.append(f"sensitive_key:{normalize_key(key)}")
        return
    if isinstance(value, (bytes, bytearray, memoryview)):
        return
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return
    if isinstance(value, Path):
        _scan(str(value), key=key, known=known, found=found, depth=depth + 1)
        return
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            # 字段名只做模式扫描（key=None），避免"字段名本身是敏感词"被误判为残留
            _scan(str(raw_key), key=None, known=known, found=found, depth=depth + 1)
            _scan(item, key=str(raw_key), known=known, found=found, depth=depth + 1)
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _scan(item, key=key, known=known, found=found, depth=depth + 1)
        return
    raise RedactionError(f"REDACTION_UNSUPPORTED_TYPE: {type(value).__name__}")


def guard_payload(
    payload: Any,
    *,
    secrets: Iterable[str] | None = None,
    where: str = "telemetry",
    policy: ScrubPolicy | None = None,
) -> Any:
    """进入 telemetry / checkpoint / RAG 之前的统一闸门：先脱敏，再自检。

    自检失败即抛 SecretLeakError —— 宁可丢数据，也不让未脱敏内容出门。
    """
    try:
        scrubbed = scrub_value(payload, secrets=secrets, policy=policy)
    except SecretLeakError:
        raise
    except RedactionError as exc:
        # 脱敏器自身失败同样 fail-closed，并带上目标位置便于定位
        raise RedactionError(f"REDACTION_FAILED: {where}: {exc}") from exc
    leaked = find_secrets(scrubbed, secrets=secrets)
    if leaked:
        raise SecretLeakError(f"SECRET_LEAK_BLOCKED: {where}: {sorted(set(leaked))}")
    return scrubbed


# ---------------------------------------------------------------- 异常摘要
def safe_error_summary(
    exc: BaseException,
    *,
    secrets: Iterable[str] | None = None,
    max_len: int = DEFAULT_MAX_SUMMARY_LEN,
) -> dict[str, str]:
    """把异常压成「错误码 + 脱敏摘要」，绝不包含响应体、请求头或原始堆栈。

    自身出错时返回带 ERROR_REDACTION_FAILED 的空摘要（fail-closed）。
    """
    try:
        raw = str(exc)
        if not isinstance(raw, str):
            raise RedactionError("REDACTION_INPUT_TYPE")
        code, _separator, rest = raw.partition(":")
        code = code.strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,59}", code):
            code = type(exc).__name__
            rest = raw
        summary = scrub_text(rest.strip(), secrets=secrets, max_len=max_len)
        return {
            "error_type": type(exc).__name__,
            "error_code": code,
            "message": summary,
        }
    except Exception:
        # 这里刻意吞掉异常：任何不确定的情况都不能把原文带出去
        return {
            "error_type": "RedactionFailed",
            "error_code": "ERROR_REDACTION_FAILED",
            "message": "",
        }


# ---------------------------------------------------------------- 环境密钥登记
def collect_secrets_from_environ(
    environ: Mapping[str, str] | None = None,
    names: Iterable[str] = SECRET_ENV_NAMES,
) -> tuple[str, ...]:
    """收集需要登记进脱敏器的密钥值；调用方不得打印返回值。"""
    env = os.environ if environ is None else environ
    values: list[str] = []
    for name in names:
        raw = env.get(name)
        if isinstance(raw, str) and raw.strip():
            values.append(raw.strip())
    return tuple(values)


def secret_env_presence(
    environ: Mapping[str, str] | None = None,
    names: Iterable[str] = SECRET_ENV_NAMES,
) -> dict[str, str]:
    """只报告 present/absent，供环境报告使用。"""
    env = os.environ if environ is None else environ
    return {
        name: ("present" if isinstance(env.get(name), str) and env.get(name, "").strip() else "absent")
        for name in names
    }
