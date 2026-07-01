"""3-placement HITL enum + `hitl_gate` signature + `HITLPlacement` schema — U-CP-38.

Implements C-CP-17 §17.1 (the closed 3-placement enumeration), §17.1.1 (the
`hitl_gate(...)` topology-primitive interface signature + `HITLResult` return
shape), and §17.3 (the `HITLPlacement` workflow-definition-surface schema).

Declares the closed 3-value `HITLPlacementKind` enum, the per-placement
`HITLPlacementTrigger` table (`HITL_PLACEMENT_TRIGGERS`, 3 entries), the
`HITLResult` 6-field result record, the `hitl_gate` 5-parameter interface
signature, and the `HITLPlacement` 4-field workflow-definition schema.

`hitl_gate` is an **interface signature** — its body is a `NotImplementedError`
stub. C-CP-17 §17.1.1 commits the signature shape; the runtime gate-delivery
mechanism (cell synchrony, durable-async signal-and-wait) is composed by later
HITL units (U-CP-39 rewriting, U-CP-52 timeout-degradation). This unit declares
the contract surface only.

`ToolName` (the `HITLPlacement.tool_filter` element type) is the AS-owned
tool-name concept; no `ToolName` NewType is landed in `harness_as` — the spec
treats tool names as plain strings, so the element type is `str` (a faithful
materialization, consistent with the U-CP-04 `retry_policies` key precedent).
`Duration` (the `timeout` field) is rendered as `int | None` — a millisecond
wall-clock budget; `None` for sync-blocking, bounded for durable-async per
C-CP-21 §21.3. The concrete `Duration` value type is deferred per spec §17.3.

Authority: Implementation_Plan_Control_Plane_v2_1.md §2 U-CP-38 (preserved
verbatim — symbolic-only enum references at v2.2/v2.3/v2.4);
Spec_Control_Plane_v1_2.md §17 C-CP-17 §17.1 + §17.1.1 + §17.3 (preserved
verbatim into v1.3); ADR-D5 v1.3 §1.3 + §1.3.1.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from harness_core.identity import EntryID
from pydantic import BaseModel, ConfigDict

from harness_cp.handoff_context import ProposedAction
from harness_cp.hitl_response_palette import HITLResponse
from harness_cp.topology_pattern import CascadePolicy

if TYPE_CHECKING:
    # Annotation-only (the `hitl_gate` signature). Kept under TYPE_CHECKING so
    # `workflow_driver_types` can import `HITLPlacement` from here at runtime for
    # the `StepExecutionContext.hitl_placements` field (R-FS-1
    # `B-HITL-PLACEMENT-PER-STEP-PRODUCER`) without an import cycle — these names
    # are never evaluated at runtime (`from __future__ import annotations`).
    from harness_cp.workflow_driver_types import StepExecutionContext, WorkflowStep


class HITLPlacementKind(StrEnum):
    """The closed 3-value HITL placement enumeration (C-CP-17 §17.1).

    The placement set is **closed** at D5 per ADR-D5 v1.3 §1.3; extension is a
    Workflow §4.1.2 Class-2 D5 revision. Member string values are the §17.1
    "Placement" column verbatim.
    """

    PRE_ACTION = "pre-action"
    """Before any tool call where `_hitl_required` is true (C-CP-17 §17.1)."""

    SUB_AGENT_BOUNDARY = "sub-agent-boundary"
    """At parent-child handoff (HandoffContext serialization point)."""

    VALIDATOR_ESCALATION = "validator-escalation"
    """After retry-budget exhaustion (3rd validator fail)."""


class LoosenablePlacementKind(StrEnum):
    """The placement kinds a per-step override MAY opt-in to REMOVE (C-CP-06 §6.2).

    `B-HITL-PLACEMENT-PER-STEP-LOOSEN` (R-FS-1 final-closure; the operator-ratified
    committed-invariant relaxation of the §17.1 "all cells" monotone-HITL floor).
    A **closed enum with exactly one member** — `SUB_AGENT_BOUNDARY` — so the two
    other `HITLPlacementKind` values are **structurally unrepresentable** in a
    `removed_placements` set, not merely guarded at runtime:

    - `PRE_ACTION` is EXCLUDED because it is the §19.1 `_hitl_required`
      floor-evaluation call-site (`hitl_gate_composer.py` step 4c) — removing it
      would leave the `mcp_trust`/`per_tool`/blast-radius floors intact but
      UNCONSULTED (the bypass-seam C10 foreclosed). The §19.5 `HITLAutoApprovePolicy`
      already provides the safe, in-`max()` PRE_ACTION loosening (solo persona/blast
      floor cells → AUTO), so per-step PRE_ACTION removal is both unsafe AND
      redundant.
    - `VALIDATOR_ESCALATION` is EXCLUDED because it fires via the §14.15
      validator-outcome re-entry path, NOT a wrap-time placement — a wrap-time
      removal would be a no-op/wrong-layer (foreclosed at the composer per Q5).

    The member string value equals `HITLPlacementKind.SUB_AGENT_BOUNDARY.value`
    verbatim so a `removed_placements` set keys cleanly against the placement's
    `position` at the composer.
    """

    SUB_AGENT_BOUNDARY = "sub-agent-boundary"
    """The only loosenable placement — the parent-child handoff gate. Removal is
    solo-scoped, floor-clamped (hard blast/`per_tool`/`mcp_trust` floors NOT
    override-able), and auto-audited per C-CP-06 §6.2."""


class HITLPlacementTrigger(BaseModel):
    """A per-placement trigger row of the C-CP-17 §17.1 table."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    placement_kind: HITLPlacementKind
    trigger_summary: str
    """The §17.1 "Trigger" column verbatim."""

    cell_applicability_qualifier: str
    """The §17.1 "Cell applicability" column verbatim."""


#: The C-CP-17 §17.1 three-placement trigger table — exactly 3 entries.
#: Closed at cardinality 3; extension is a Workflow §4.1.2 Class-2 D5 revision.
HITL_PLACEMENT_TRIGGERS: tuple[HITLPlacementTrigger, ...] = (
    HITLPlacementTrigger(
        placement_kind=HITLPlacementKind.PRE_ACTION,
        trigger_summary=(
            "Before any tool call where _hitl_required(tool, server, "
            "persona_tier) == true per C-CP-19 §19.1 composition"
        ),
        cell_applicability_qualifier="All cells of C-CP-18 matrix",
    ),
    HITLPlacementTrigger(
        placement_kind=HITLPlacementKind.SUB_AGENT_BOUNDARY,
        trigger_summary=(
            "At parent-child handoff per Cluster 4 §2.4.4 [HIGH] "
            "(HandoffContext serialization point per C-CP-13 §13.1)"
        ),
        cell_applicability_qualifier=(
            "All cells; sub-agent interrupt stranding mitigated via "
            "cascade-timeout per C-CP-21 §21.3"
        ),
    ),
    HITLPlacementTrigger(
        placement_kind=HITLPlacementKind.VALIDATOR_ESCALATION,
        trigger_summary=(
            "After retry-budget exhaustion (3rd validator fail per Cluster 4 §2.2.3 [HIGH])"
        ),
        cell_applicability_qualifier="All cells",
    ),
)


class HITLResult(BaseModel):
    """The result of a `hitl_gate(...)` invocation (C-CP-17 §17.1.1).

    Six fields verbatim. `edited_proposal` is populated only when
    `response == HITLResponse.EDIT`; `response_text` only when
    `response == HITLResponse.RESPOND`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    response: HITLResponse
    edited_proposal: ProposedAction | None = None
    """Populated only when `response == HITLResponse.EDIT`."""

    response_text: str | None = None
    """Populated only when `response == HITLResponse.RESPOND`."""

    timestamp: str
    """ISO-8601 timestamp."""

    audit_ledger_entry_id: EntryID
    """Per C-CP-20 §20.1 entry shape."""

    response_summary_hash: str
    """SHA-256 hex-64 over the canonicalized response payload."""


class HITLPlacement(BaseModel):
    """A workflow-definition HITL placement declaration (C-CP-17 §17.3).

    Four fields verbatim. Multiple placements per workflow are admitted. The
    `tool_filter` glob/regex semantics are deferred to implementation
    discretion per §17.3.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    position: HITLPlacementKind
    tool_filter: tuple[str, ...] | None = None
    """`pre-action` — limits which tools trigger the gate. Element type is
    `str` (AS-owned tool name); no AS `ToolName` NewType is landed."""

    cascade_policy: CascadePolicy | None = None
    """Overrides the workload-class default per C-CP-11 §11.1."""

    timeout: int | None = None
    """Overrides the cell synchrony-class default; millisecond wall-clock
    budget. `Duration` rendered as `int`; concrete type deferred per §17.3."""


@runtime_checkable
class AskUserQuestionSurface(Protocol):
    """Structural surface for HITL question delivery (U-RT-60 binding pin).

    Declared here as a CP-side Protocol so the §17.4 canonical `hitl_gate`
    signature does not introduce a CP → runtime package dependency.
    Concretized by
    `harness_runtime.lifecycle.mcp_backed_ask_user_question_surface.MCPBackedAskUserQuestionSurface`
    (and the broader `AskUserQuestionSurface` Protocol declared at
    `harness_runtime.lifecycle.ask_user_question_surface`); structural
    duck-typing satisfies this CP-side declaration.
    """

    async def ask(self, *args: Any, **kwargs: Any) -> Any:
        """Deliver a HITL question; return the operator response payload."""
        ...


@dataclass(frozen=True)
class HITLGateResult:
    """Typed return envelope for the canonical §17.4 `hitl_gate(...)` signature.

    Reuses the C-CP-16 §16.1 4-response palette (`HITLResponse`). Response-
    conditional fields are populated only when `response` matches their
    discipline per C-CP-16 §16.2:

    - `edited_proposal`: populated iff `response == EDIT`
    - `rejection_reason`: populated iff `response == REJECT`
    - `response_text`: populated iff `response == RESPOND`

    `response_latency_ms` carries the end-to-end gate-delivery wall-clock
    latency; `timed_out` is True on bounded-timeout exhaustion per
    C-CP-21 §21.3.
    """

    response: HITLResponse
    edited_proposal: Mapping[str, Any] | None
    rejection_reason: str | None
    response_text: str | None
    response_latency_ms: int
    timed_out: bool


async def hitl_gate(
    placement: HITLPlacement,
    step: WorkflowStep,
    step_context: StepExecutionContext,
    *,
    surface: AskUserQuestionSurface,
    palette: frozenset[HITLResponse] | None = None,
    timeout: int | None = None,
) -> HITLGateResult:
    """Canonical §17.4 HITL gate signature (U-CP-71; C-CP-17 §17.4 NEW at spec v1.10).

    Pure signature materialization closing the historical
    `NotImplementedError` left at v1.2 + carried through v1.9. The gate
    body composition is owned by the runtime-side composer per
    `Spec_Harness_Runtime_v1.md` v1.13 §14.8 (existing
    `RuntimeHITLGateComposer`). Callers reaching this CP-side signature
    are bootstrap / spec-conformance paths that prove the surface is
    declared; production gate delivery flows through the runtime composer
    bound at `ctx.sub_agent_dispatcher` + `ctx.llm_dispatcher` wraps.

    Per spec §17.4:
    - `palette` defaults to the C-CP-16 §16.1 4-response palette when
      `None` (`frozenset({APPROVE, EDIT, REJECT, RESPOND})`).
    - `surface` is REQUIRED — operator-injected per U-RT-60. Raises
      `ValueError` when `surface is None`.

    Raises
    ------
    NotImplementedError
        Production callers MUST go through the runtime-side
        `RuntimeHITLGateComposer` per C-RT-18 §14.8. This CP-side surface
        is signature-only.
    ValueError
        `surface is None`.
    """
    if surface is None:  # type: ignore[truthy-bool]  # operator-injected per AC #5
        raise ValueError(
            "hitl_gate(...) `surface` is required per CP spec v1.10 §17.4 "
            "(injected per U-RT-60 MCPBackedAskUserQuestionSurface)."
        )
    _ = placement, step, step_context, palette, timeout
    raise NotImplementedError(
        "hitl_gate is a canonical §17.4 signature; the gate body is "
        "composed by the runtime-side RuntimeHITLGateComposer per "
        "Spec_Harness_Runtime_v1.md v1.13 §14.8 (production callers reach "
        "the runtime composer via ctx.sub_agent_dispatcher / ctx.llm_dispatcher)."
    )


# C-CP-16 §16.1 4-response palette default (per spec §17.4 invariant 2).
DEFAULT_HITL_PALETTE: frozenset[HITLResponse] = frozenset(
    {
        HITLResponse.APPROVE,
        HITLResponse.EDIT,
        HITLResponse.REJECT,
        HITLResponse.RESPOND,
    }
)
"""Canonical 4-response palette default per CP spec v1.10 §17.4 invariant 2.

The §17.4 signature defaults `palette` to `None`; callers that pass `None`
indicate "use the canonical 4-response default"; the runtime composer
(C-RT-18 §14.8) applies this default at composition time.
"""
