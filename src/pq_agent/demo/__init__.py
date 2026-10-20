"""A5-03 演示层：离线、可复现的 9 步演示与它的文字记录。"""

from __future__ import annotations

from pq_agent.demo.harness import (
    CONTROL_FILE_NAMES,
    DemoHarness,
    DemoNotReady,
    DemoRun,
    DemoStepResult,
    demo_status,
    render_run,
)
from pq_agent.demo.paths import (
    PROTECTED_NAMES,
    ResetReport,
    demo_root,
    demo_stage_root,
    is_inside_demo,
    reset_demo,
)
from pq_agent.demo.steps import (
    DEMO_SCHEMA_VERSION,
    DEMO_STEPS,
    STEP_IDS,
    DemoStep,
    step_by_id,
    total_narration_seconds,
)

__all__ = [
    "CONTROL_FILE_NAMES",
    "DEMO_SCHEMA_VERSION",
    "DEMO_STEPS",
    "PROTECTED_NAMES",
    "STEP_IDS",
    "DemoHarness",
    "DemoNotReady",
    "DemoRun",
    "DemoStep",
    "DemoStepResult",
    "ResetReport",
    "demo_root",
    "demo_stage_root",
    "demo_status",
    "is_inside_demo",
    "render_run",
    "reset_demo",
    "step_by_id",
    "total_narration_seconds",
]
