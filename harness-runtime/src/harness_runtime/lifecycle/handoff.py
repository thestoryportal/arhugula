"""Sub-agent handoff + brief runtime registry — stage 3b CP_ROUTING (U-RT-26).

Per `Spec_Harness_Runtime_v1.md` v1.1 §5 (C-RT-02 stage 3b invariants) and the
Phase 2 Session 3 Track A atomic decomposition §L5 (U-RT-26). The runtime
binds CP's sub-agent dispatch primitives into a single reference-time registry
surface:

- `harness_cp.handoff_context` — `HandoffContext` 7-field schema (C-CP-13
  §13.1); supporting types (`ProposedAction`, `FailedAttempt`, `Alternative`,
  `StateSummary`, `LedgerEntryRef`, `ExternalReference`, `RetryHistory`).
- `harness_cp.sub_agent_brief` — `SubAgentBrief` 5-field schema (C-CP-13 §13.2)
  + `canonicalize_brief` + `compute_brief_summary_hash`.
- `harness_cp.sub_agent_gate_level_descent` — C-CP-12 §12 dispatch protocol:
  `dispatch_sub_agent`, `assert_monotonic_descent` (§12.2), `assert_monotonic_ascent`
  (C-AS-11), `emit_sub_agent_dispatch_audit` (§12.5),
  `sub_agent_dispatch_response_hash` (§12.5).
- `harness_cp.brief_authoring_inheritance` — `BRIEF_AUTHORING_INHERITANCE`
  4-row table (C-CP-13 §13.3) + `inheritance_for(workload_class)`.

**Pure decision surfaces + schema enforcement.** The registry composes the
C-CP-12 / §13 / §13.2 / §13.3 *decision* surfaces and re-exports the brief
canonicalization + summary-hash primitives. The schema enforcement (AC #2)
is delegated to Pydantic v2 — `SubAgentBrief` + `HandoffContext` are frozen,
`extra="forbid"` BaseModels; constructing one with extra or missing fields
raises `ValidationError` at registry-call time, and the canonicalize/hash
round-trip is deterministic.

**Audit-emission scope.** `emit_sub_agent_dispatch_audit` returns a
`CPAuditLedgerEntry` carrying placeholder `timestamp=""` + `prior_event_hash`
("0" * 64) — the integration-time write to the F2 state-ledger fills these
fields from the live clock + the prior-entry hash chain (U-IS-09 / U-IS-11 /
U-RT-32). At L5, the registry surfaces the audit-entry *composition* — the
*write* is U-RT-32.

Per-component landing posture:

- `RuntimeHandoffRegistry` — concrete `HandoffRegistry` Protocol
  implementation. Frozen dataclass; exposes the 4 CP surfaces as typed
  methods + properties. No mutable state.
- `RuntimeHandoffRegistry.dispatch(...)` — composes `dispatch_sub_agent`;
  returns `SubAgentGateLevelDescent`.
- `RuntimeHandoffRegistry.assert_descent(...)` /
  `RuntimeHandoffRegistry.assert_ascent(...)` — composes the C-CP-12 §12.2
  monotonic-descent and C-AS-11 monotonic-ascent invariant enforcers.
- `RuntimeHandoffRegistry.inheritance_for(workload_class)` — composes
  `inheritance_for`; returns `BriefAuthoringInheritance`.
- `RuntimeHandoffRegistry.compute_brief_summary_hash(brief)` — composes the
  C-CP-13 §13.2 brief summary hash.
- `RuntimeHandoffRegistry.canonicalize_brief(brief)` — composes the
  C-CP-13 §13.2 brief canonicalization.
- `RuntimeHandoffRegistry.compose_dispatch_audit(...)` — composes
  `emit_sub_agent_dispatch_audit`; returns `CPAuditLedgerEntry` with the
  placeholder fields the U-RT-32 audit-writer fills at write-time.
- `RuntimeHandoffRegistry.dispatch_response_hash(brief)` — composes
  `sub_agent_dispatch_response_hash` (§12.5 join-key surface).
- `HandoffStage` — frozen materialization stage carrying the registry.
- `materialize_handoff_stage(config)` — composer.

Scope discipline (U-RT-26 boundary held): NO actual sub-agent process
dispatch (deferred to L8 LOOP_INIT / topology dispatcher U-RT-40), NO F2
audit-ledger write (U-RT-32), NO operator-override authoring (deferred per
the CP plan's `GateOverride` opaque type). This registry is a binding-time +
reference-time surface only; the LOOP_INIT orchestrator drives the dispatch
loop and the audit-writer applies the audit-entry composition.
"""

from __future__ import annotations

from dataclasses import dataclass

from harness_as.sandbox_tier import SandboxTier
from harness_core import ActionID, WorkloadClass
from harness_cp.brief_authoring_inheritance import (
    BRIEF_AUTHORING_INHERITANCE,
    BriefAuthoringInheritance,
    inheritance_for,
)
from harness_cp.gate_level_rule import GateLevel
from harness_cp.per_step_override_evaluator import CPAuditLedgerEntry
from harness_cp.sub_agent_brief import (
    SubAgentBrief,
    canonicalize_brief,
    compute_brief_summary_hash,
)
from harness_cp.sub_agent_gate_level_descent import (
    GateOverride,
    SubAgentGateLevelDescent,
    assert_monotonic_ascent,
    assert_monotonic_descent,
    dispatch_sub_agent,
    emit_sub_agent_dispatch_audit,
    sub_agent_dispatch_response_hash,
)

from harness_runtime.types import RuntimeConfig

__all__ = [
    "HandoffStage",
    "RuntimeHandoffRegistry",
    "materialize_handoff_stage",
]


@dataclass(frozen=True, slots=True)
class RuntimeHandoffRegistry:
    """Concrete `HandoffRegistry` Protocol implementation (U-RT-26).

    Composes the 4 CP sub-agent-dispatch primitives into a single
    reference-time surface. The registry holds no mutable state — every
    method is a pure composition of the underlying CP decision function or
    a property carrying a CP static table. The L8 LOOP_INIT orchestrator
    drives the actual process-dispatch loop against this registry's outputs;
    the U-RT-32 audit-writer applies the §12.5 audit-entry composition.

    Per-table properties expose the canonical CP data verbatim:

    - `brief_inheritance_table` → `BRIEF_AUTHORING_INHERITANCE` (4 rows by
      `WorkloadClass`, C-CP-13 §13.3).
    """

    @property
    def brief_inheritance_table(self) -> tuple[BriefAuthoringInheritance, ...]:
        """The 4-row C-CP-13 §13.3 brief-authoring inheritance table verbatim."""
        return BRIEF_AUTHORING_INHERITANCE

    def dispatch(
        self,
        parent_action_id: ActionID,
        parent_gate_level: GateLevel,
        parent_sandbox_tier: SandboxTier,
        sub_agent_brief: SubAgentBrief,
        operator_override: GateOverride | None = None,
    ) -> SubAgentGateLevelDescent:
        """Resolve the sub-agent gate-level descent at a dispatch site.

        Per C-CP-12 §12.2-§12.4: child blast-radius ceiling from the
        default-downgrade rule; child gate level descends monotonically
        (≤ parent); child sandbox tier ascends monotonically (≥ parent).
        Pure composition of `harness_cp.sub_agent_gate_level_descent.dispatch_sub_agent`.

        AC #1 surface (handoff registry queryable). The L8 orchestrator
        invokes this method at each sub-agent-boundary placement to compute
        the descent record; the C-AS-11 / §12.2 invariants are independently
        enforced via `assert_descent` + `assert_ascent`.
        """
        return dispatch_sub_agent(
            parent_action_id=parent_action_id,
            parent_gate_level=parent_gate_level,
            parent_sandbox_tier=parent_sandbox_tier,
            sub_agent_brief=sub_agent_brief,
            operator_override=operator_override,
        )

    def assert_descent(
        self,
        parent_gate_level: GateLevel,
        child_gate_level: GateLevel,
    ) -> None:
        """Enforce the C-CP-12 §12.2 monotonic-descent invariant.

        `child_gate_level <= parent_gate_level`; ascent raises `ValueError`.
        Pure composition of `assert_monotonic_descent`."""
        assert_monotonic_descent(parent_gate_level, child_gate_level)

    def assert_ascent(
        self,
        parent_sandbox_tier: SandboxTier,
        child_sandbox_tier: SandboxTier,
    ) -> None:
        """Enforce the C-AS-11 sandbox-tier monotonic-ascension invariant.

        `child_sandbox_tier >= parent_sandbox_tier`; descent raises `ValueError`.
        Pure composition of `assert_monotonic_ascent`."""
        assert_monotonic_ascent(parent_sandbox_tier, child_sandbox_tier)

    def inheritance_for(
        self,
        workload_class: WorkloadClass,
    ) -> BriefAuthoringInheritance:
        """Return the brief-authoring inheritance rule for a workload class.

        Total over `WorkloadClass`; deterministic. Pure composition of
        `harness_cp.brief_authoring_inheritance.inheritance_for`."""
        return inheritance_for(workload_class)

    def compute_brief_summary_hash(self, brief: SubAgentBrief) -> str:
        """Return `sha256(canonicalize_brief(brief))` as a hex digest (§13.2).

        The U-CP-27 sub-agent-dispatch audit-entry join key. Pure composition
        of `harness_cp.sub_agent_brief.compute_brief_summary_hash`."""
        return compute_brief_summary_hash(brief)

    def canonicalize_brief(self, brief: SubAgentBrief) -> bytes:
        """Deterministically serialize a brief for hashing (§13.2).

        Sorted-key JSON over the 4 §13.2 content fields; the `summary_hash`
        field is excluded (self-referential). Pure composition of
        `harness_cp.sub_agent_brief.canonicalize_brief`."""
        return canonicalize_brief(brief)

    def dispatch_response_hash(self, brief: SubAgentBrief) -> str:
        """`response_hash = sha256(canonicalize(SubAgentBrief))` per §12.5.

        The §12.5 audit-entry `response_hash` field. Pure composition of
        `harness_cp.sub_agent_gate_level_descent.sub_agent_dispatch_response_hash`."""
        return sub_agent_dispatch_response_hash(brief)

    def compose_dispatch_audit(
        self,
        parent_action_id: ActionID,
        descent: SubAgentGateLevelDescent,
        brief_hash: str,
    ) -> CPAuditLedgerEntry:
        """Compose the §12.5 sub-agent-dispatch audit-ledger entry (CP-distinct).

        The returned `CPAuditLedgerEntry` carries placeholder `timestamp` and
        `prior_event_hash` fields; the U-RT-32 audit writer fills them from
        the live clock and the prior-entry hash chain (U-IS-09 / U-IS-11) at
        write-time. Pure composition of
        `harness_cp.sub_agent_gate_level_descent.emit_sub_agent_dispatch_audit`."""
        return emit_sub_agent_dispatch_audit(parent_action_id, descent, brief_hash)


@dataclass(frozen=True, slots=True)
class HandoffStage:
    """Frozen result of stage 3b CP_ROUTING handoff-registry materialization.

    Mirrors the U-RT-21 / U-RT-22 / U-RT-23 / U-RT-24 / U-RT-25 stage shape.
    The bootstrap orchestrator (U-RT-43) binds `registry` to
    `HarnessContext.handoff_registry`.
    """

    registry: RuntimeHandoffRegistry


def materialize_handoff_stage(config: RuntimeConfig) -> HandoffStage:
    """Build the handoff registry stage at stage 3b CP_ROUTING.

    Stage 3b composer. The registry is stateless — every CP surface is a
    pure decision function, a static table, or a hash primitive — so the
    composer constructs a single `RuntimeHandoffRegistry` and wraps it.
    `config` is read for API consistency with the U-RT-21..U-RT-25 composers;
    no field is consumed at HEAD (the manifest does not carry per-workload-class
    handoff overrides; C-CP-13 §13.3 inheritance is spec-pinned)."""
    _ = config
    return HandoffStage(registry=RuntimeHandoffRegistry())
