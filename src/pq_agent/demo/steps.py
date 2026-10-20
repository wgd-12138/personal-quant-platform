"""A5-03 演示步骤合同。

这里的 marker 不是装饰：runbook 里的每一屏都要求这些子串在真实的演示输出里出现，
验收脚本则反过来读这份定义去检查输出。这样「文档说的话」和「程序做的事」
由同一份数据约束，避免文档越写越漂亮、程序越跑越不像。
"""

from __future__ import annotations

from dataclasses import dataclass

DEMO_SCHEMA_VERSION = "pq_agent.demo_run/1"

MIN_NARRATION_SECONDS = 300
MAX_NARRATION_SECONDS = 480


@dataclass(frozen=True)
class DemoStep:
    """一个演示步骤：讲什么、讲多久、屏幕上必须出现什么。"""

    step_id: str
    title: str
    narration_seconds: int
    markers: tuple[str, ...]
    offline_note: str
    pitfall_ids: tuple[str, ...] = ()


DEMO_STEPS: tuple[DemoStep, ...] = (
    DemoStep(
        step_id="doctor",
        title="第 1 步 先体检：端口、checkpoint 驱动、demo fixture、tracing",
        narration_seconds=30,
        markers=("doctor_ok=", "checkpoint_driver", "demo_fixture", "tracing"),
        offline_note="四项检查全部本地完成，不依赖 LLM、Docker 或外网。",
        pitfall_ids=("pit-doctor-green-means-nothing",),
    ),
    DemoStep(
        step_id="replay_qa",
        title="第 2 步 replay 问答：意图识别、工具调用、引用",
        narration_seconds=60,
        markers=("intent=market_check", "market_snapshot", "graph_citations=", "citation=", "rag_mode=bm25"),
        offline_note="没有 LLM 时走规则层，没有向量后端时只开词法通道，两者都如实标注。",
        pitfall_ids=("pit-bm25-called-rag", "pit-model-channel-unmeasured"),
    ),
    DemoStep(
        step_id="missing_data",
        title="第 3 步 缺数据就失败关闭：不给任何行情结论",
        narration_seconds=35,
        markers=("INPUTS_NOT_READY", "AS_OF_NOT_IN_FIXTURE", "不输出任何行情结论"),
        offline_note="fixture 里没有那一天就是没有，不补数、不外推。",
        pitfall_ids=("pit-fabricated-data",),
    ),
    DemoStep(
        step_id="reject_side_effects",
        title="第 4 步 拒绝写与外发：拒绝后零副作用",
        narration_seconds=40,
        markers=("APPROVAL_REJECTED", "external_sends=0", "business_writes=0"),
        offline_note="sender 为 None，演示模式没有任何外发通道。",
        pitfall_ids=("pit-approval-only-id",),
    ),
    DemoStep(
        step_id="approve_two_phase",
        title="第 5 步 双阶段批准：审批绑定参数哈希，且照样 dry-run",
        narration_seconds=45,
        markers=("approved=true", "arguments_hash=", "dry_run=True", "external_sends=0"),
        offline_note="批准不等于真发：演示里 push_preview 仍是 dry-run。",
        pitfall_ids=("pit-approved-means-sent",),
    ),
    DemoStep(
        step_id="sandbox_layer",
        title="第 6 步 沙箱三层：不信任就不跑、静态拒绝、运行时兜底",
        narration_seconds=70,
        markers=("constraint_only", "SANDBOX_NOT_EXECUTABLE", "static_blocked", "SANDBOX_TIMEOUT", "docker="),
        offline_note="没有 Docker 时如实说不可用，绝不回落成本机约束层冒充隔离。",
        pitfall_ids=("pit-constraint-called-isolation",),
    ),
    DemoStep(
        step_id="trace_alerts",
        title="第 7 步 遥测与告警：span、节点序列、APM 采集状态",
        narration_seconds=55,
        markers=("span_count=", "node_sequence=", "collector=", "firing_alerts=", "dropped_spans=0"),
        offline_note="没有 Collector/SkyWalking 时标记降级，本地只留 trace 快照。",
        pitfall_ids=("pit-local-span-as-apm",),
    ),
    DemoStep(
        step_id="resume_checkpoint",
        title="第 8 步 断点恢复：审批前零副作用、恢复后只执行一次",
        narration_seconds=45,
        markers=("pending_from_new_instance=", "resumed=", "side_effect_executions=1"),
        offline_note="pending 落在 sqlite，新实例能读到；执行次数由 call_log 计数证明。",
        pitfall_ids=("pit-resume-double-execute",),
    ),
    DemoStep(
        step_id="role_denied",
        title="第 9 步 角色隔离：analyst 拿不到写与外发工具",
        narration_seconds=30,
        markers=("ROLE_CAPABILITY_DENIED", "analyst_tools=", "call_log_delta_after_deny=0"),
        offline_note="权限差异由代码和测试证明，不靠提示词告诫。",
        pitfall_ids=("pit-role-by-prompt",),
    ),
)

STEP_IDS: tuple[str, ...] = tuple(step.step_id for step in DEMO_STEPS)


def total_narration_seconds() -> int:
    return sum(int(step.narration_seconds) for step in DEMO_STEPS)


def step_by_id(step_id: str) -> DemoStep:
    for step in DEMO_STEPS:
        if step.step_id == step_id:
            return step
    raise KeyError("未知演示步骤： " + str(step_id))


def step_index() -> dict[str, DemoStep]:
    return {step.step_id: step for step in DEMO_STEPS}


__all__ = [
    "DEMO_SCHEMA_VERSION",
    "DEMO_STEPS",
    "MAX_NARRATION_SECONDS",
    "MIN_NARRATION_SECONDS",
    "STEP_IDS",
    "DemoStep",
    "step_by_id",
    "step_index",
    "total_narration_seconds",
]
