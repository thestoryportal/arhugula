"""Per-D1-engine-class composition overlay + model-binding matrix — U-AS-29.

Implements C-AS-13 §13.3 (per-engine-class composition site overlay), §13.4
(per-sub-agent-role x model-binding contract + pre-HITL escalation order).
Declares `D1EngineClass`, `EngineClassComposition`, the
`ENGINE_CLASS_COMPOSITION_OVERLAY`, `SubAgentRole`, `AnthropicModel`,
`ModelBinding`, the 20-cell `MODEL_BINDING_MATRIX`, and the pre-HITL
escalation metadata.

Authority: Implementation_Plan_Action_Surface_v1.md §2 U-AS-29 (R3-preserved —
v1 body verbatim per Implementation_Plan_Action_Surface_v1_1.md §5.1; v1.1
adds a graph-only `[U-CP-00 (cross-axis: core)]` edge for `WorkloadClass`, no
body revision); Spec_Action_Surface_v1.md §13.3-§13.4 C-AS-13; ADR-D3 v1.2
§1.3-§1.4.

Depends on: U-AS-04 (`DeploymentSurface`); U-AS-28 (the §13 primitive surface);
U-CP-00 (cross-axis: core — `WorkloadClass`); U-IS-01 / U-IS-02 (cross-axis:
IS — the §13.3 column-5 SKILL.md filesystem residence per C-IS-01; consumed
read-only, expressed as the per-engine-class `skills_filesystem_residence`
descriptor string).
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType

from harness_core import WorkloadClass
from pydantic import BaseModel, ConfigDict


class D1EngineClass(StrEnum):
    """The 5 D1 engine classes (C-AS-13 §13.3 column 1)."""

    EVENT_SOURCED_REPLAY = "event_sourced_replay"
    SAVE_POINT_CHECKPOINT = "save_point_checkpoint"
    PURE_PATTERN_NO_ENGINE = "pure_pattern_no_engine"
    RECONCILER_LOOP = "reconciler_loop"
    WAL_SEGMENT = "wal_segment"


class SubAgentRole(StrEnum):
    """The 5 sub-agent roles (C-AS-13 §13.4 column headers)."""

    LEAD_ORCHESTRATOR = "lead_orchestrator"
    GENERATOR = "generator"
    EVALUATOR = "evaluator"
    REVIEWER = "reviewer"
    SUB_AGENT = "sub_agent"


class AnthropicModel(StrEnum):
    """The 4 per-role-bindable Anthropic models (C-AS-13 §13.4)."""

    HAIKU_4_5 = "haiku-4-5"
    SONNET_4_6 = "sonnet-4-6"
    OPUS_4_6 = "opus-4-6"
    OPUS_4_7 = "opus-4-7"


class EngineClassComposition(BaseModel):
    """Per-D1-engine-class composition site overlay (C-AS-13 §13.3)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    engine_class: D1EngineClass
    prompt_cache_scope: str
    batch_api_integration: str
    extended_thinking_placement: str
    skills_filesystem_residence: str


class ModelBinding(BaseModel):
    """A per-role model binding (C-AS-13 §13.4)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    primary_model: AnthropicModel
    qualifier: str | None = None
    cap: int | None = None


class PreHITLEscalationStep(StrEnum):
    """A step of the pre-HITL escalation staircase (C-AS-13 §13.4)."""

    STEP_1_C9_BACKOFF = "STEP_1_C9_BACKOFF"
    STEP_2_C6_MODEL_TIER_ESCALATION = "STEP_2_C6_MODEL_TIER_ESCALATION"
    STEP_3_C11_HITL = "STEP_3_C11_HITL"


# --- §13.3 per-engine-class composition overlay ----------------------------

ENGINE_CLASS_COMPOSITION_OVERLAY: Mapping[D1EngineClass, EngineClassComposition] = MappingProxyType(
    {
        D1EngineClass.EVENT_SOURCED_REPLAY: EngineClassComposition(
            engine_class=D1EngineClass.EVENT_SOURCED_REPLAY,
            prompt_cache_scope="Activity-internal",
            batch_api_integration="Submission as Activity; engine-native idempotency",
            extended_thinking_placement="Activity-internal",
            skills_filesystem_residence=(
                "Activity reads SKILL.md from project-root filesystem at call time"
            ),
        ),
        D1EngineClass.SAVE_POINT_CHECKPOINT: EngineClassComposition(
            engine_class=D1EngineClass.SAVE_POINT_CHECKPOINT,
            prompt_cache_scope="Node-internal",
            batch_api_integration="Submission at node boundary; interrupt() + Command resume",
            extended_thinking_placement="Node-internal",
            skills_filesystem_residence=(
                "Node reads SKILL.md; checkpoint state includes loaded-Skill "
                "manifest for replay determinism"
            ),
        ),
        D1EngineClass.PURE_PATTERN_NO_ENGINE: EngineClassComposition(
            engine_class=D1EngineClass.PURE_PATTERN_NO_ENGINE,
            prompt_cache_scope="Harness-managed",
            batch_api_integration=(
                "Submission idempotency-keyed via F2 state-ledger entry per C-IS-05"
            ),
            extended_thinking_placement="Harness-managed",
            skills_filesystem_residence="Harness reads SKILL.md from F2 filesystem",
        ),
        D1EngineClass.RECONCILER_LOOP: EngineClassComposition(
            engine_class=D1EngineClass.RECONCILER_LOOP,
            prompt_cache_scope="CR-cycle-scoped",
            batch_api_integration=("Submission represented as CR; idempotency on CR metadata.uid"),
            extended_thinking_placement="CR-cycle-scoped",
            skills_filesystem_residence="SKILL.md mounted as ConfigMap / PVC",
        ),
        D1EngineClass.WAL_SEGMENT: EngineClassComposition(
            engine_class=D1EngineClass.WAL_SEGMENT,
            prompt_cache_scope="Per-segment",
            batch_api_integration="Submission as WAL entry; per-segment fail-fast",
            extended_thinking_placement="Per-segment",
            skills_filesystem_residence="SKILL.md per-segment metadata",
        ),
    }
)


# --- §13.4 per-sub-agent-role model-binding matrix -------------------------

_SE = WorkloadClass.SOFTWARE_ENGINEERING
_CC = WorkloadClass.CONTENT_CREATION
_PA = WorkloadClass.PIPELINE_AUTOMATION
_RE = WorkloadClass.RESEARCH

_LEAD = SubAgentRole.LEAD_ORCHESTRATOR
_GEN = SubAgentRole.GENERATOR
_EVAL = SubAgentRole.EVALUATOR
_REV = SubAgentRole.REVIEWER
_SUB = SubAgentRole.SUB_AGENT

_HAIKU = AnthropicModel.HAIKU_4_5
_SONNET = AnthropicModel.SONNET_4_6

#: The 20-cell per-(workload-class, sub-agent-role) model-binding matrix
#: (C-AS-13 §13.4). `n/a` cells resolve to `None`.
MODEL_BINDING_MATRIX: Mapping[tuple[WorkloadClass, SubAgentRole], ModelBinding | None] = (
    MappingProxyType(
        {
            # software-engineering
            (_SE, _LEAD): ModelBinding(
                primary_model=_SONNET,
                qualifier="Sonnet 4.6 default; Opus 4.6 at multi-tenant-compliance",
            ),
            (_SE, _GEN): ModelBinding(primary_model=_SONNET),
            (_SE, _EVAL): ModelBinding(primary_model=_SONNET, qualifier="x1-3", cap=3),
            (_SE, _REV): ModelBinding(primary_model=_HAIKU, cap=3),
            (_SE, _SUB): ModelBinding(primary_model=_HAIKU, qualifier="review/eval reads"),
            # content-creation
            (_CC, _LEAD): ModelBinding(primary_model=_SONNET),
            (_CC, _GEN): ModelBinding(primary_model=_SONNET),
            (_CC, _EVAL): ModelBinding(
                primary_model=_HAIKU, qualifier="operator-as-reviewer dominant"
            ),
            (_CC, _REV): None,
            (_CC, _SUB): None,
            # pipeline-automation
            (_PA, _LEAD): ModelBinding(
                primary_model=_SONNET,
                qualifier="Sonnet 4.6 per-stage default; Haiku 4.5 high-volume idempotent",
            ),
            (_PA, _GEN): ModelBinding(
                primary_model=_SONNET,
                qualifier="Sonnet 4.6 synthesis; Haiku 4.5 idempotent",
            ),
            (_PA, _EVAL): None,
            (_PA, _REV): None,
            (_PA, _SUB): ModelBinding(primary_model=_HAIKU, qualifier="idempotent parallel", cap=3),
            # research
            (_RE, _LEAD): ModelBinding(
                primary_model=_SONNET,
                qualifier="Sonnet 4.6 default; Opus 4.6 multi-tenant-compliance high-fidelity",
            ),
            (_RE, _GEN): None,
            (_RE, _EVAL): None,
            (_RE, _REV): ModelBinding(primary_model=_HAIKU, qualifier="synthesis pre-pass"),
            (_RE, _SUB): ModelBinding(
                primary_model=_HAIKU, qualifier="breadth-search x 3-5", cap=5
            ),
        }
    )
)


def model_binding(workload: WorkloadClass, role: SubAgentRole) -> ModelBinding | None:
    """Return the model binding for a (workload-class, sub-agent-role) cell.

    Total over `(WorkloadClass, SubAgentRole)` — all 20 cells populated; an
    `n/a` cell resolves to `None` (C-AS-13 §13.4).
    """
    return MODEL_BINDING_MATRIX[(workload, role)]


#: The pre-HITL escalation staircase (C-AS-13 §13.4): C9 backoff → C6 model-tier
#: escalation → C11 HITL.
PRE_HITL_ESCALATION_ORDER: tuple[PreHITLEscalationStep, ...] = (
    PreHITLEscalationStep.STEP_1_C9_BACKOFF,
    PreHITLEscalationStep.STEP_2_C6_MODEL_TIER_ESCALATION,
    PreHITLEscalationStep.STEP_3_C11_HITL,
)

#: The C6 model-tier escalation chain, ascending (C-AS-13 §13.4).
MODEL_TIER_ESCALATION_CHAIN: tuple[AnthropicModel, ...] = (
    AnthropicModel.HAIKU_4_5,
    AnthropicModel.SONNET_4_6,
    AnthropicModel.OPUS_4_6,
    AnthropicModel.OPUS_4_7,
)
