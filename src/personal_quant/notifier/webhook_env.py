from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass


FEISHU_WEBHOOK_ENV = "FEISHU_WEBHOOK_URL"


@dataclass(frozen=True)
class WebhookResolution:
    url: str | None
    source: str


def resolve_feishu_webhook(
    explicit_url: str | None = None,
    environ: Mapping[str, str] | None = None,
    user_env_reader: Callable[[str], str | None] | None = None,
) -> WebhookResolution:
    explicit = _clean(explicit_url)
    if explicit is not None:
        return WebhookResolution(explicit, "命令参数")

    env = os.environ if environ is None else environ
    process_url = _clean(env.get(FEISHU_WEBHOOK_ENV))
    if process_url is not None:
        return WebhookResolution(process_url, "当前进程环境变量")

    reader = _read_windows_user_env if user_env_reader is None else user_env_reader
    user_url = _clean(reader(FEISHU_WEBHOOK_ENV))
    if user_url is not None:
        return WebhookResolution(user_url, "Windows 用户环境变量")

    return WebhookResolution(None, "未设置")


def _read_windows_user_env(name: str) -> str | None:
    try:
        import winreg
    except ImportError:
        return None

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _value_type = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    return str(value) if value is not None else None


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned if cleaned else None
