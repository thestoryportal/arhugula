"""Per-step override evaluator binding — stage 5 LOOP_INIT (U-RT-39, opens L8).

Per `Spec_Harness_Runtime_v1.md` v1.1 §C-RT-02 stage 5 invariants +
§C-RT-04 `HarnessContext.override_evaluator` field (`PerStepOverrideEvaluator`
(CP) — stage 5). The runtime binds CP's `resolve_step_binding`
(C-CP-06 §6.2) into a runtime-time evaluator that satisfies the
`PerStepOverrideEvaluator` Protocol (narrowed at `harness_runtime.types`
at this landing).

**Stateless wrapper.** `resolve_step_binding` is a pure function —
takes `WorkflowManifestEntry` + `step_id` + `default_model_binding`,
returns `StepEffectiveBinding`. The runtime evaluator does not hold
state beyond the bound CP function; per-call inputs drive the result
deterministically per the C-CP-06 §6.2 contract.

**Module convention.** One module per unit.
`materialize_override_evaluator_stage` composer returns a frozen
`OverrideEvaluatorStage` dataclass with `slots=True`. Typed
`OverrideEvaluatorBindError` for bootstrap-time failures. Mirrors the
L5 / L6 / L7 stage shape established at U-RT-21..38.
"""

from __future__ import annotations

from dataclasses import dataclass

from harness_core.persona_tier import PersonaTier
from harness_cp.cp_shared_types import ModelBinding
from harness_cp.per_step_override_evaluator import (
    StepEffectiveBinding,
    resolve_step_binding,
)
from harness_cp.workflow_manifest_entry import WorkflowManifestEntry

from harness_runtime.types import RuntimeConfig


class OverrideEvaluatorBindError(Exception):
    """Raised when override-evaluator stage materialization fails."""


@dataclass(frozen=True, slots=True)
class RuntimePerStepOverrideEvaluator:
    """Per-step override evaluator runtime surface (C-CP-06 §6.2 binding).

    Stateless. Each `resolve_step_binding` call delegates directly to CP's
    pure `resolve_step_binding` function; the runtime adds no state
    between calls. Satisfies the `harness_runtime.types.PerStepOverrideEvaluator`
    Protocol (narrowed at U-RT-39 landing).
    """

    def resolve_step_binding(
        self,
        manifest_entry: WorkflowManifestEntry,
        step_id: str,
        *,
        default_model_binding: ModelBinding,
        persona_tier: PersonaTier,
    ) -> StepEffectiveBinding:
        """Resolve the effective per-step binding (delegates to CP C-CP-06 §6.2)."""
        return resolve_step_binding(
            manifest_entry,
            step_id,
            default_model_binding=default_model_binding,
            persona_tier=persona_tier,
        )


@dataclass(frozen=True, slots=True)
class OverrideEvaluatorStage:
    """Frozen result of stage 5 LOOP_INIT override-evaluator binding.

    The bootstrap orchestrator (U-RT-43) binds `evaluator` to
    `HarnessContext.override_evaluator` (C-RT-04 stage 5 invariant).
    Mirrors the L5 / L6 / L7 stage shape.
    """

    evaluator: RuntimePerStepOverrideEvaluator


def materialize_override_evaluator_stage(
    config: RuntimeConfig,
) -> OverrideEvaluatorStage:
    """Build the stage 5 LOOP_INIT per-step override evaluator stage.

    The evaluator is stateless — no construction-time fields consumed.
    `config` is read for API consistency with the L5..L7 composers; no
    field is consumed at HEAD.
    """
    _ = config
    return OverrideEvaluatorStage(evaluator=RuntimePerStepOverrideEvaluator())
