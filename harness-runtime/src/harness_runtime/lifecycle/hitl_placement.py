"""HITL placement runtime registry — stage 3b CP_ROUTING (U-RT-25).

Per `Spec_Harness_Runtime_v1.md` v1.1 §5 (C-RT-02 stage 3b invariants) and the
Phase 2 Session 3 Track A atomic decomposition §L5 (U-RT-25). The runtime
binds CP's HITL primitives into a single reference-time registry surface:

- `harness_cp.hitl_response_palette` — `HITLResponse` 4-value enum,
  `HITL_RESPONSE_SEMANTICS` 4-row table, `PALETTE_INVARIANTS` enforcement-point
  table, `PER_RESPONSE_AUDIT_ENTRY_SHAPES` (C-CP-16 §16.1).
- `harness_cp.hitl_placement` — `HITLPlacementKind` 3-value closed enum,
  `HITL_PLACEMENT_TRIGGERS` 3-row trigger table (C-CP-17 §17.1).
- `harness_cp.hitl_timeout_degradation.on_hitl_timeout` — the C-CP-21 §21.8
  per-persona-tier timeout-degradation decision; `TIMEOUT_DEGRADATION_TABLE`
  3-row policy table.
- `harness_cp.hitl_as_tool_call_rewriting` — `select_variant(synchrony)` +
  `rewrite_tool_call_to_hitl(...)` (C-CP-17 §17.2).
- `harness_cp.pause_resume_protocol.classify_resume` — the pure decision core
  of `attempt_resume` (C-CP-22 §22.1).

**Pure decision surfaces only.** This registry composes the C-CP-17 / §21 /
§22.1 *decision* surfaces — the ones whose CP-side bodies are deterministic
functions over typed inputs. The NotImplementedError CP stubs (`hitl_gate`,
`capture_pause_snapshot`, `attempt_resume`, `deliver_webhook`) are deferred to
L8 LOOP_INIT / integration-time composition with the IS substrate; they are
NOT wired here. L5 boundary held.

**Configured-wait reading.** The U-RT-25 AC "timeout degradation emits typed
event after configured wait" reads at L5 as a *decision* over the configured
wait: a `HITLInvocation` carries `timeout: int | None` (the configured wait
in ms); calling `registry.on_timeout(invocation, persona_tier)` after that
wait elapses returns the typed `TimeoutDegradationKind` (the "typed event").
The actual wall-clock wait lives at L8 LOOP_INIT — this registry is clock-free
and deterministic, mirroring the U-RT-24 breaker pattern.

Per-component landing posture:

- `RuntimeHITLPlacementRegistry` — concrete `HITLPlacementRegistry` Protocol
  implementation. Frozen dataclass; exposes the 5 CP surfaces as typed
  methods + properties. No mutable state.
- `RuntimeHITLPlacementRegistry.on_timeout(invocation, persona_tier)` —
  composes `on_hitl_timeout`; returns `TimeoutDegradationKind`.
- `RuntimeHITLPlacementRegistry.rewrite_tool_call(...)` — composes
  `rewrite_tool_call_to_hitl`; returns `RewrittenToolCall`.
- `RuntimeHITLPlacementRegistry.classify_resume(...)` — composes
  `classify_resume`; returns `ResumeOutcomeKind`.
- `RuntimeHITLPlacementRegistry.select_variant(synchrony_class)` — composes
  `select_variant`; returns `HITLSemanticVariant`.
- `HITLPlacementStage` — frozen materialization stage carrying the registry.
- `materialize_hitl_placement_stage(config)` — composer.

Scope discipline (U-RT-25 boundary held): NO handoff registry (U-RT-26), NO
gate-delivery mechanism (deferred to L8 LOOP_INIT), NO webhook delivery loop
(integration-time), NO F2 audit-ledger write (U-RT-32), NO actual wall-clock
wait (L8 orchestrator drives the clock).
"""

from __future__ import annotations

from dataclasses import dataclass

from harness_core import PersonaTier
from harness_cp.handoff_context import ProposedAction
from harness_cp.hitl_as_tool_call_rewriting import (
    HITLSemanticVariant,
    MCPServerID,
    RewrittenToolCall,
    ToolName,
    rewrite_tool_call_to_hitl,
    select_variant,
)
from harness_cp.hitl_placement import (
    HITL_PLACEMENT_TRIGGERS,
    HITLPlacementTrigger,
)
from harness_cp.hitl_response_palette import (
    HITL_RESPONSE_SEMANTICS,
    PALETTE_INVARIANTS,
    PER_RESPONSE_AUDIT_ENTRY_SHAPES,
    HITLResponseSemantic,
    PaletteCompletenessInvariant,
    PerResponseAuditEntryShape,
)
from harness_cp.hitl_timeout_degradation import (
    TIMEOUT_DEGRADATION_TABLE,
    TimeoutDegradationKind,
    TimeoutDegradationPolicy,
    on_hitl_timeout,
)

# `MaterialDiff` is consumed via the `classify_resume` signature; import for
# the method signature only (the type lives at the U-CP-50 material-diff unit).
from harness_cp.material_diff_detection import MaterialDiff
from harness_cp.pause_resume_protocol import (
    ResumeOutcomeKind,
    classify_resume,
)
from harness_cp.persona_engine_hitl_matrix import SynchronyClass
from harness_cp.validator_fail_transient_staircase import CrossTrustBoundaryState
from harness_cp.workload_binding_engine_class_selection import HITLInvocation

from harness_runtime.types import RuntimeConfig

__all__ = [
    "HITLPlacementStage",
    "RuntimeHITLPlacementRegistry",
    "materialize_hitl_placement_stage",
]


@dataclass(frozen=True, slots=True)
class RuntimeHITLPlacementRegistry:
    """Concrete `HITLPlacementRegistry` Protocol implementation (U-RT-25).

    Composes the 5 CP HITL primitives into a single reference-time surface.
    The registry holds no mutable state — every method is a pure composition
    of the underlying CP decision function or a property carrying a CP static
    table. The L8 LOOP_INIT orchestrator threads the actual wall-clock wait,
    F2-ledger writes, and webhook delivery against this registry's outputs.

    Per-table properties expose the canonical CP data verbatim:

    - `palette_semantics` → `HITL_RESPONSE_SEMANTICS` (4 rows, C-CP-16 §16.1).
    - `placement_triggers` → `HITL_PLACEMENT_TRIGGERS` (3 rows, C-CP-17 §17.1).
    - `palette_invariants` → `PALETTE_INVARIANTS` (palette-completeness
      enforcement points).
    - `per_response_audit_shapes` → `PER_RESPONSE_AUDIT_ENTRY_SHAPES`
      (per-response audit-entry shapes).
    - `timeout_policies` → `TIMEOUT_DEGRADATION_TABLE` (3 per-persona-tier
      rows, C-CP-21 §21.8).
    """

    @property
    def palette_semantics(self) -> tuple[HITLResponseSemantic, ...]:
        """The 4-row HITL response palette semantics table (C-CP-16 §16.1)."""
        return HITL_RESPONSE_SEMANTICS

    @property
    def placement_triggers(self) -> tuple[HITLPlacementTrigger, ...]:
        """The 3-row HITL placement trigger table (C-CP-17 §17.1)."""
        return HITL_PLACEMENT_TRIGGERS

    @property
    def palette_invariants(self) -> tuple[PaletteCompletenessInvariant, ...]:
        """The palette-completeness enforcement-point table (C-CP-16 §16.1)."""
        return PALETTE_INVARIANTS

    @property
    def per_response_audit_shapes(self) -> tuple[PerResponseAuditEntryShape, ...]:
        """The per-response audit-entry shape table (C-CP-16 §16.1)."""
        return PER_RESPONSE_AUDIT_ENTRY_SHAPES

    @property
    def timeout_policies(self) -> tuple[TimeoutDegradationPolicy, ...]:
        """The 3-row per-persona-tier timeout-degradation table (C-CP-21 §21.8)."""
        return TIMEOUT_DEGRADATION_TABLE

    def on_timeout(
        self,
        invocation: HITLInvocation,
        persona_tier: PersonaTier,
    ) -> TimeoutDegradationKind:
        """Resolve the typed timeout-degradation event for a timed-out invocation.

        AC #2 surface (timeout degradation emits typed event after configured
        wait). The L8 LOOP_INIT orchestrator waits for `invocation.timeout`
        milliseconds; when the wait elapses, it invokes this method and gets
        the typed `TimeoutDegradationKind` per the C-CP-21 §21.8 per-persona-tier
        table. Pure composition of `harness_cp.hitl_timeout_degradation.on_hitl_timeout`.

        SOLO_DEVELOPER → FAIL_CLOSED; TEAM_BINDING → ESCALATE_SECONDARY_CHANNEL;
        MULTI_TENANT_COMPLIANCE → FAIL_CLOSED (override prohibited; `fail-open`
        structurally prohibited per Persona §10.4). Vocab-A per U-CP-92.
        """
        return on_hitl_timeout(invocation, persona_tier)

    def select_variant(
        self,
        cell_synchrony_class: SynchronyClass,
    ) -> HITLSemanticVariant:
        """Select the §17.2 HITL semantic variant for a cell synchrony class.

        SYNC_BLOCKING → REQUEST_HUMAN_INPUT; DURABLE_ASYNC → AWAIT_HUMAN_APPROVAL;
        BOTH_BY_TIER / EXCLUDED → ESCALATE_TO_HUMAN. Pure composition of
        `harness_cp.hitl_as_tool_call_rewriting.select_variant`.
        """
        return select_variant(cell_synchrony_class)

    def rewrite_tool_call(
        self,
        tool: ToolName,
        server: MCPServerID,
        persona_tier: PersonaTier,
        proposed_action: ProposedAction,
        cell_synchrony_class: SynchronyClass,
        cross_trust_boundary_state: CrossTrustBoundaryState,
        hitl_required: bool,
    ) -> RewrittenToolCall:
        """Rewrite a tool call into a HITL semantic variant per C-CP-17 §17.2.

        AC #3 surface (tool-call rewriting wires). Pure composition of
        `harness_cp.hitl_as_tool_call_rewriting.rewrite_tool_call_to_hitl`.
        When `hitl_required` is false, returns the call unchanged; when true,
        selects the §17.2 variant by `cell_synchrony_class` and the response
        palette (full or U-CP-48-restricted by `cross_trust_boundary_state`).
        """
        return rewrite_tool_call_to_hitl(
            tool=tool,
            server=server,
            persona_tier=persona_tier,
            proposed_action=proposed_action,
            cell_synchrony_class=cell_synchrony_class,
            cross_trust_boundary_state=cross_trust_boundary_state,
            hitl_required=hitl_required,
        )

    def classify_resume(
        self,
        diff: tuple[MaterialDiff, ...],
        revalidation_succeeded: bool,
    ) -> ResumeOutcomeKind:
        """Classify a resume outcome from the material-diff set (C-CP-22 §22.1).

        Pure decision core of `attempt_resume`. An empty (or all-non-material)
        diff-set is `RESUME_CLEAN`; a material diff-set resumes after
        revalidation when revalidation succeeds, else aborts. Pure composition
        of `harness_cp.pause_resume_protocol.classify_resume`.

        The full `attempt_resume` execution (snapshot bounded-read + chain
        verification + audit-entry write) composes at U-RT-32 + integration-time
        per the CP plan's deferred-composition discipline.
        """
        return classify_resume(diff, revalidation_succeeded)


@dataclass(frozen=True, slots=True)
class HITLPlacementStage:
    """Frozen result of stage 3b CP_ROUTING HITL placement registry materialization.

    Mirrors the U-RT-21 / U-RT-22 / U-RT-23 / U-RT-24 stage shape. The
    bootstrap orchestrator (U-RT-43) binds `registry` to
    `HarnessContext.hitl_registry`.
    """

    registry: RuntimeHITLPlacementRegistry


def materialize_hitl_placement_stage(config: RuntimeConfig) -> HITLPlacementStage:
    """Build the HITL placement registry stage at stage 3b CP_ROUTING.

    Stage 3b composer. The registry is stateless — every CP surface is a
    pure decision function or a static table — so the composer constructs
    a single `RuntimeHITLPlacementRegistry` and wraps it. `config` is read
    for API consistency with the U-RT-21..U-RT-24 composers; no field is
    consumed at HEAD (the manifest does not carry per-persona-tier HITL
    overrides; C-CP-21 §21.8's `override_permitted` is enforced at L8 when
    operator-supplied policies surface).
    """
    _ = config
    return HITLPlacementStage(registry=RuntimeHITLPlacementRegistry())
