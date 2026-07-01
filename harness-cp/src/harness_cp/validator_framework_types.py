"""C-CP-25 ValidatorFramework type carriers — 3 enums per CP spec v1.10 §25.2.

U-CP-58 — first unit of cluster 10-CP-A. Declares the three enums that the
C-CP-25 ValidatorFramework composer body (U-CP-60) and the validator.* span
emitter (U-CP-61) consume at runtime:

- `ValidatorOutcome` — the 5-class outcome a Validator returns from .validate()
- `ValidatorFailClass` — the 5-class pre-emit fail categorization (NEW at CP spec v1.10)
- `ValidatorNextAction` — the 4-class framework-derived next-action

Member string values for `ValidatorOutcome` and `ValidatorFailClass` are cited
verbatim from CP spec v1.10 §25.2. `ValidatorNextAction` value names are per
§25.8 deferred-to-implementation discretion; this impl arc selects lowercase
SCREAMING_SNAKE_CASE-rendered enum-value strings matching the §25.2 mapping
table's value-name column.

**Naming note (path β disambiguation, 2026-05-21).** This module's
`ValidatorFailClass` is distinct from the C-CP-21 §21.1 retry-exit taxonomy
homed at `harness_cp.validator_fail_taxonomy.ValidatorRetryExitClass`. The
two enums occupy different semantic domains: C-CP-21 = post-fail retry-exit
classification (which staircase to run); C-CP-25 = pre-emit fail categorization
(why validation failed). Per workspace
`.harness/class_1_fork_u_cp_58_validator_fail_class_collision.md`
operator-ratified path β.

Authority: CP spec v1.10 §25.2 (NEW C-CP-25 ValidatorFramework); plan unit
U-CP-58 (CP plan v2.16 §1).
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from harness_cp.hitl_response_palette import HITLResponse
from harness_cp.workflow_driver_types import StepExecutionContext, WorkflowStep


class ValidatorOutcome(StrEnum):
    """The 5-class outcome a Validator returns from .validate() (CP spec v1.10 §25.2).

    Each outcome maps to exactly one `ValidatorNextAction` per the §25.2
    mapping table (bijective on outcomes; NOT on next_actions —
    ESCALATE_HITL is the next_action for both ESCALATE and
    OPERATOR_BURDEN_EXCEEDED, with disambiguation via `validator.outcome`
    span attribute per §C-OD-29).
    """

    PASS = "pass"
    """Validation succeeded; step result accepted."""

    REVALIDATE = "revalidate"
    """Mutate payload + retry via C-RT-16 retry wrapper."""

    ESCALATE = "escalate"
    """Validator-fail escalation arc per §25.7 invariant 4;
    opens HITL gate composition via C-RT-18 §14.8."""

    PERMANENT_FAIL = "permanent_fail"
    """Workflow aborts with `fail_class` propagation per §25.6."""

    OPERATOR_BURDEN_EXCEEDED = "operator_burden_exceeded"
    """Operator-burden threshold breach; degrade per persona-tier
    (runtime spec v1.13 §14.10 OperatorBurdenEvaluator)."""


class ValidatorFailClass(StrEnum):
    """The 5-class pre-emit fail categorization (CP spec v1.10 §25.2; NEW at C-CP-25).

    Distinct from C-CP-21 §21.1 ValidatorRetryExitClass (post-fail retry-exit
    classification) homed at `harness_cp.validator_fail_taxonomy`.
    """

    SCHEMA_VIOLATION = "schema_violation"
    """Output doesn't match input_schema."""

    SEMANTIC_INCONSISTENCY = "semantic_inconsistency"
    """Contradicts prior step state."""

    SAFETY_POLICY = "safety_policy"
    """Operator-defined policy hit."""

    RESOURCE_CONSTRAINT = "resource_constraint"
    """Cost/latency budget exceeded."""

    EXTERNAL_REJECTION = "external_rejection"
    """Downstream service rejected."""


class ValidatorNextAction(StrEnum):
    """The 4-class framework-derived next-action (CP spec v1.10 §25.2 mapping table).

    Per §25.8 deferred-to-implementation discretion: enum value names
    selected as SCREAMING_SNAKE_CASE-rendered lowercase strings matching
    the §25.2 mapping table's next_action column. Bijective on outcomes
    (each ValidatorOutcome maps to exactly one ValidatorNextAction);
    NOT bijective on next_actions (ESCALATE_HITL receives both ESCALATE
    and OPERATOR_BURDEN_EXCEEDED — disambiguation via `validator.outcome`
    per §C-OD-29).
    """

    PROCEED = "proceed"
    """ValidatorOutcome=PASS → step result accepted."""

    RETRY = "retry"
    """ValidatorOutcome=REVALIDATE → mutate + retry via C-RT-16."""

    ESCALATE_HITL = "escalate_hitl"
    """ValidatorOutcome ∈ {ESCALATE, OPERATOR_BURDEN_EXCEEDED}
    → open HITL gate via C-RT-18 §14.8."""

    ABORT = "abort"
    """ValidatorOutcome=PERMANENT_FAIL → workflow aborts."""


# ============================================================================
# U-CP-59 — Validator Protocol + ValidatorResult + ValidatorEvaluation +
#            HITLEscalationBrief schemas (CP spec v1.10 §25.1 + §25.2)
# ============================================================================


_DEFAULT_HITL_PALETTE: frozenset[HITLResponse] = frozenset(HITLResponse)
"""C-CP-16 §16.1 full 4-response palette: APPROVE / EDIT / REJECT / RESPOND.

Default `HITLEscalationBrief.proposed_response_palette` per CP spec v1.10
§25.2. Validator-escalation arcs MAY narrow the palette per cross-trust-
boundary discipline (C-CP-21 §21.3); the default is the full palette.
"""


class HITLEscalationBrief(BaseModel):
    """Typed payload passed to a HITL gate when a Validator escalates
    (CP spec v1.10 §25.2).

    Constructed by `ValidatorFramework.evaluate()` when the outcome is
    `ESCALATE` (and bridged to `ValidatorNextAction.ESCALATE_HITL`).
    The HITL gate composer (C-RT-18 §14.8) consumes this brief at
    proposal-window open.
    """

    model_config = ConfigDict(frozen=True)

    parent_step_id: str
    parent_action_id: str
    fail_class: ValidatorFailClass | None = None
    fail_detail_hash: str | None = None
    escalation_reason: str
    proposed_response_palette: frozenset[HITLResponse] = _DEFAULT_HITL_PALETTE


class ValidatorResult(BaseModel):
    """Operator-supplied Validator return shape (CP spec v1.10 §25.2).

    Field cardinality per §25.2:
      - `outcome` REQUIRED
      - `fail_class` None iff outcome=PASS
      - `revalidation_payload` populated on REVALIDATE
      - `escalation_brief` populated on ESCALATE
      - `fail_detail_hash` sha256 of fail-reason text (§25.8 deferred-to-discretion shape)
    """

    model_config = ConfigDict(frozen=True)

    outcome: ValidatorOutcome
    fail_class: ValidatorFailClass | None = None
    revalidation_payload: Mapping[str, Any] | None = None
    escalation_brief: HITLEscalationBrief | None = None
    fail_detail_hash: str | None = None


class ValidatorEvaluation(BaseModel):
    """ValidatorFramework output (CP spec v1.10 §25.2).

    Wraps the operator-supplied `ValidatorResult` with framework-derived
    fields: span attributes for `validator.*` namespace emission (per §25.5),
    the next_action derived per the §25.2 mapping table, and a cumulative
    burden_count tracked on `ctx.operator_burden_counter` per §25.7
    invariant 5.
    """

    model_config = ConfigDict(frozen=True)

    result: ValidatorResult
    span_attributes: Mapping[str, Any]
    next_action: ValidatorNextAction
    burden_count: int


@runtime_checkable
class Validator(Protocol):
    """Operator-supplied Validator Protocol (CP spec v1.10 §25.1).

    Validators run post-dispatch + pre-ledger-append per §25.7 invariant 2.
    `step_result` is the dispatch-time output Mapping (matches the
    `StepDispatcher.dispatch()` return type at
    `harness_runtime.types.StepDispatcher`).
    """

    async def validate(
        self,
        step: WorkflowStep,
        step_result: Mapping[str, Any],
        *,
        step_context: StepExecutionContext,
    ) -> ValidatorResult: ...


@runtime_checkable
class ValidatorFramework(Protocol):
    """Runtime-side Validator composer Protocol (CP spec v1.10 §25.1).

    Materialized at stage 5 LOOP_INIT per §25.3; bound to
    `ctx.validator_framework`. The composer body (U-CP-60) consumes a
    per-step Validator from `ctx.validator_registry`, runs `.validate()`,
    maps the outcome to `next_action`, populates span attributes for
    §25.5 emission, and increments `burden_count` per §25.7 invariant 5.
    """

    async def evaluate(
        self,
        step: WorkflowStep,
        step_result: Mapping[str, Any],
        *,
        step_context: StepExecutionContext,
    ) -> ValidatorEvaluation: ...


@runtime_checkable
class ValidatorPostEvaluateHook(Protocol):
    """Operator-supplied post-evaluate observability hook (CP spec v1.24 §28.10.1).

    Fires once per `ConcreteValidatorFramework.evaluate()` invocation,
    AFTER `ValidatorEvaluation` construction, BEFORE return. Receives
    elapsed wall-clock execution time + step + step_context + evaluation.

    Hook is observability-only; MUST NOT modify the evaluation; MUST NOT
    influence dispatch outcome. Implementation lives at harness-runtime
    (which can import `harness_od` types for cost-attribution); the
    Protocol surface declared here at harness-cp is independent of
    OD-axis vocabulary per X-AL-3 spec extension at
    `.harness/class_1_fork_u_od_40_validator_post_evaluate_hook.md`.

    Best-effort firing discipline: hook exceptions swallowed at the
    firing site per §28.10.4 invariant 2 (mirror
    `_attribute_tool_cost_best_effort` at
    `harness_runtime/lifecycle/runtime_tool_dispatcher.py:285`).
    """

    async def on_post_evaluate(
        self,
        *,
        step: WorkflowStep,
        step_context: StepExecutionContext,
        evaluation: ValidatorEvaluation,
        execution_time_ms: float,
    ) -> None: ...
