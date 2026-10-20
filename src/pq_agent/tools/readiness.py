"""数据就绪度工具：把核心包 check_inputs 包成只读工具。

只读性的关键：check_inputs 本身不写盘（写盘发生在 cli 层的 generate_readiness_report），
所以这里只调用纯函数，工具自身不产生任何文件。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from personal_quant.data_center.readiness import check_inputs, render_readiness_report
from pq_agent.tools.contracts import ArgSpec, ToolError, ToolSpec, build_result


def _parse_as_of(value: Any) -> dt.date:
    if value is None:
        raise ToolError("INVALID_AS_OF", "缺少 as_of 参数")
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError as exc:
        raise ToolError("INVALID_AS_OF", f"as_of 不是合法日期：{value!r}") from exc


def make_readiness_check_spec(positions_path: Path | None, *, focus_positions: bool = True) -> ToolSpec:
    """构造 readiness_check 工具。positions_path 由调用方注入，模型无法指定路径。"""

    def handler(arguments: dict[str, Any], *, dry_run: bool = True) -> dict[str, Any]:
        focus = bool(arguments.get("focus_positions", focus_positions))
        if positions_path is None:
            return build_result(
                spec,
                data={"ready": False, "errors": ["POSITIONS_PATH_NOT_CONFIGURED"], "report": ""},
                summary="未配置持仓文件路径，无法检查数据就绪度",
                data_source="local_check",
                degraded=["INPUTS_NOT_READY"],
            )
        result = check_inputs(positions_path, focus)
        return build_result(
            spec,
            data={
                "ready": bool(result.ready),
                "position_count": int(result.position_count),
                "focus_positions": bool(result.focus_positions),
                "errors": [str(item) for item in result.errors],
                "report": render_readiness_report(result),
            },
            summary="数据齐备" if result.ready else "数据不齐备：" + "; ".join(result.errors),
            data_source="local_check",
            degraded=[] if result.ready else ["INPUTS_NOT_READY"],
        )

    spec = ToolSpec(
        name="readiness_check",
        capability="read_local",
        side_effects=(),
        args=(ArgSpec(name="focus_positions", type="boolean", required=False, description="是否检查逐股内容"),),
        returns={"type": "object", "properties": {"ready": {"type": "boolean"}}},
        timeout_s=30.0,
        idempotency="safe",
        risk="none",
        summary="检查数据是否齐备（只读，不写盘）",
        cli_command="check-inputs",
        handler=handler,
    )
    return spec
