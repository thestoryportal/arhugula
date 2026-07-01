"""Step-kind dispatcher registry — stage 5 LOOP_INIT (U-RT-59 AC #1).

Per `Spec_Harness_Runtime_v1.md` v1.6 §14.7.1 + §14.7.7 (C-RT-17 contract).
The registry is the routing-layer surface the CP driver consumes at every
per-step dispatch site: `step_dispatchers.lookup(step.kind).dispatch(...)`.

**Architectural posture (§14.7.7).** v1.5 driver took a single
`step_dispatcher: StepDispatcher` parameter and routed every step through it.
v1.6 amends to take a frozen `step_dispatchers: StepKindDispatcherRegistry`
and routes per `step.kind`. The driver remains step-kind-agnostic in the
C-CP-25 §25.3.3.4 "step body opaque to driver" sense — it routes on the
declared `step.kind` enum field (not opaque body content).

**v1.6 binding (§14.7.1).** Two of the 5 `StepKind` values bind at bootstrap
stage 5: `INFERENCE_STEP → ctx.llm_dispatcher` (U-RT-58 C-RT-16 wrapper) and
`SUB_AGENT_DISPATCH → ctx.sub_agent_dispatcher` (U-RT-59 new composer). The
other 3 (`DECLARATIVE_STEP`, `TOOL_STEP`, `HITL_STEP`) are unbound at v1.6;
`lookup` raises `StepKindDispatcherNotBoundError`, which the driver maps to
`RT-FAIL-STEP-KIND-DISPATCHER-NOT-BOUND` per spec §14.7 failure-mode taxonomy.
Follow-on composer arcs (tool-invocation / HITL / validator) bind the
remaining entries.

**Frozen post-construction.** `StepKindDispatcherRegistry` is a frozen
Pydantic v2 model (`extra="forbid"`, `frozen=True`); runtime mutation is
structurally foreclosed per §14.7.7 invariant "frozen post-construction;
runtime mutation is foreclosed by Pydantic v2 frozen=True". No silent
fallback to a default dispatcher per §14.7.7 invariant
"StepKindDispatcherRegistry.lookup(unbound_kind) raises
StepKindDispatcherNotBoundError".
"""

from __future__ import annotations

from collections.abc import Mapping

from harness_cp.workflow_driver import StepDispatcher, StepKindDispatcherNotBoundError
from harness_cp.workflow_driver_types import StepKind
from pydantic import BaseModel, ConfigDict

__all__ = [
    "StepKindDispatcherNotBoundError",
    "StepKindDispatcherRegistry",
]


class StepKindDispatcherRegistry(BaseModel):
    """Frozen mapping `{StepKind → StepDispatcher}` consumed by the CP driver.

    Per `Spec_Harness_Runtime_v1.md` v1.6 §14.7.1 + §14.7.7. Constructed at
    bootstrap stage 5 (LOOP_INIT); assigned to `HarnessContext.step_dispatchers`.
    The CP driver invokes `self.lookup(step.kind).dispatch(binding, step,
    step_context=step_context)` at every per-step dispatch site.

    The `dispatchers` mapping carries `StepDispatcher` Protocol values
    (`harness_cp.workflow_driver.StepDispatcher`, declared at
    `harness-cp/src/harness_cp/workflow_driver.py:151`; `@runtime_checkable`).
    Pydantic v2 cannot type-check Protocol satisfaction at construction; the
    structural check holds at runtime via the `StepDispatcher` Protocol's
    `dispatch(binding, step, *, step_context)` signature.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        arbitrary_types_allowed=True,
    )

    dispatchers: Mapping[StepKind, StepDispatcher]
    """Frozen mapping from step kind to its bound dispatcher.

    v1.6 MVP carries 2 entries (`INFERENCE_STEP`, `SUB_AGENT_DISPATCH`).
    Follow-on composer arcs extend with the remaining 3 entries.
    """

    def lookup(self, step_kind: StepKind) -> StepDispatcher:
        """Return the bound dispatcher for `step_kind`.

        Raises
        ------
        StepKindDispatcherNotBoundError
            `step_kind` is not bound in this registry. The CP driver maps
            this to `RT-FAIL-STEP-KIND-DISPATCHER-NOT-BOUND` per spec
            §14.7 failure-mode taxonomy.
        """
        try:
            return self.dispatchers[step_kind]
        except KeyError as exc:
            raise StepKindDispatcherNotBoundError(step_kind) from exc
