"""
pq_agent 工具层：deny-by-default 的 allowlist registry 与核心包适配器。

设计要点：
1. 权限判定写在代码层，不写在提示词里。工具能不能被调用、参数是否合法、
   是否需要人工确认，全部由 registry 静态判定，模型无法绕过。
2. 不新增第二个允许列表。A0-02 的 docs/agent/CLI_TOOL_MATRIX.md 已经是唯一权威，
   这里只做映射：CLI 子命令 -> 工具名 -> capability / schema / 幂等策略。
3. 直接 import 核心包的纯函数，不经 subprocess。CLI 层把异常转成文本，
   工具层需要的是结构化错误码。
"""

from __future__ import annotations

from .code_exec import CODE_EXEC_TOOL_NAME, make_code_exec_spec
from .contracts import (
    RISK_LEVELS,
    TOOL_ERROR_CODES,
    ToolError,
    ToolSpec,
    build_result,
    truncate_payload,
)
from .quant_tools import TOOL_REGISTRY, build_default_registry
from .registry import ToolRegistry

__all__ = [
    "CODE_EXEC_TOOL_NAME",
    "RISK_LEVELS",
    "TOOL_ERROR_CODES",
    "TOOL_REGISTRY",
    "ToolError",
    "ToolRegistry",
    "ToolSpec",
    "build_default_registry",
    "build_result",
    "make_code_exec_spec",
    "truncate_payload",
]
