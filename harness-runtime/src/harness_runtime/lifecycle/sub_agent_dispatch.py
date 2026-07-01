"""Sub-agent dispatch composer — stage 5 LOOP_INIT (U-RT-59 ACs #2-#9).

Per `Spec_Harness_Runtime_v1.md` v1.7 §14.7 C-RT-17 (sub-agent dispatch
composer; Path A resolution of the StepDispatcher Protocol parent-context
gap) + v1.7 §14.7.2 step 8 4-substep sequence (Path D + Path B-revised-a
resolution of U-RT-59 Fork 2 CP→OD audit-write gap). Concretes the
`StepDispatcher` Protocol for `StepKind.SUB_AGENT_DISPATCH` steps; composer
body per §14.7.2 ten-step discipline with the v1.7 step 8 4-substep audit
composition: (8a) compose CP audit → (8b) F2-write dispatch action → (8c)
CP→OD convert → (8d) `ctx.audit_writer.append`. AC #9 write half UN-STRUCK
at v1.7 per the now-coherent spec substrate (CXA v2.4 + CP spec v1.7 §13.5.1
converter contract + ADR-D5 v1.4 + OD spec v1.5 C-OD-24 + runtime spec v1.7
§14.7.2 step 8).

**Operator-ratified fork resolutions absorbed at landing.**

1. **Sync end-to-end (Class 3 spec-prose drift, ratified 2026-05-20).** Spec
   §14.7.1 declares async `dispatch`; §14.7.4 declares async
   `ChildWorkflowRunner.__call__`; §14.7.2 step 6 says `await self.child_workflow_runner(...)`.
   Stage 1 plumbing at `harness-cp/src/harness_cp/workflow_driver.py:175`
   froze the `StepDispatcher` Protocol as sync; `execute_workflow` is sync.
   Operator ratified land-sync per the de-facto Stage 1 contract.

2. **Class 1 — CP→OD audit-write gap (RESOLVED at v1.7 spec arc).** Original
   v1.6 prose at §14.7.2 step 8 was incompatible with `audit_writer.append`'s
   OD-shape contract; AC #9 write half STRUCK at landing. Full Fork 2 spec
   arc closed 2026-05-20 across 8 commits (CXA v2.4 + CP spec v1.7 §13.5.1
   converter contract + ADR-D5 v1.4 §1.4 + OD spec v1.5 C-OD-24 + runtime
   spec v1.7 §14.7.2 step 8 4-substep sequence + CP spec v1.8 Form A NOTE
   reconciliation). v1.7 implementation arc materializes the 4-substep
   sequence: (8a) `compose_dispatch_audit` → `CPAuditLedgerEntry` (existing
   surface); (8b) `ledger_writer.append(...)` → F2 entry for the dispatch
   action (Q2(a) — composer writes F2 BEFORE composing OD audit); (8c)
   `cp_audit_to_od_audit(...)` → signed OD `AuditLedgerEntry` (CP spec
   §13.5.1 converter); (8d) `audit_writer.append(tenant_id, od_entry)` →
   IS-anchored persistence via OD wrapper. AC #9 UN-STRUCK at the same
   landing.

3. **Class 1 — async/sync dispatcher defect (filed at landing).**
   `ctx.llm_dispatcher` (U-RT-58 `RetryBreakerFallbackDispatcher` wrapper)
   is `async def dispatch`; the sync driver returns a coroutine if bound
   as `INFERENCE_STEP → ctx.llm_dispatcher`. U-RT-58 wired the wrapper at
   stage 5 without integration-driving through the sync driver — sleeping
   defect surfaced at U-RT-59. Plan AC #11 INFERENCE_STEP binding clause
   STRUCK at v1.6 MVP; registry binds only `SUB_AGENT_DISPATCH`. Resolution
   (sync facade vs async driver vs Protocol revision) owed to follow-on
   arc.

4. **Class 3 prose drift (rolled into landing).** `ctx.audit_writer`
   (not `ctx.audit_ledger_writer`); `harness_cp.topology_subagent_namespace`
   (not `harness_cp.handoff_context`); `ProposedAction` real shape is
   `action_kind / payload / brief` (not `text`); `ChildWorkflowRunner`
   carries additive `default_model_binding` kwarg.

**Composer body shape (v1.7, post-Fork-2-resolution).**

1. Pydantic-validate `step.step_payload → SubAgentDispatchPayload` (AC #3)
2. Compose `HandoffContext` from `step_context` + payload (AC #4)
3. Compute `SubAgentGateLevelDescent` via `ctx.handoff_registry.dispatch` (AC #5a)
4. Verify topology admissibility via `ctx.topology_dispatcher` + `is_topology_permitted` (AC #5b)
5. Open `subagent.span` + set canonical `subagent.*` + narrow `topology.*` attributes (AC #6)
6. Invoke child runner sync (AC #7)
7. Map child `RunResult.status` → `subagent.result_status` (AC #8)
8. Audit composition + persistence — 4-substep sequence (AC #9 — UN-STRUCK at v1.7):
     8a. Compose `CPAuditLedgerEntry` via `compose_dispatch_audit`
     8b. F2-write dispatch action via `ledger_writer.append(...)` → action_id is
         the `StateLedgerEntryRef` for the audit entry's `entry_core`
     8c. Convert CP→OD via `cp_audit_to_od_audit(...)` → signed OD `AuditLedgerEntry`
     8d. Persist via `audit_writer.append(tenant_id, od_entry)` → IS-anchored
9. Return step output (child `final_state` or `partial_state`)
10. Typed error propagation: typed error subclasses bubble; outer driver's
    try/except per C-CP-25 §25.3.3.4 maps to fail class

**Failure-mode taxonomy (per spec §14.7 + v1.7 step 8 follow-on).** Four
typed errors are raised from this module + propagated through the sync driver:

- `SubAgentDispatchPayloadShapeError` → `RT-FAIL-PAYLOAD-SHAPE`
- `SubAgentDispatchTopologyInadmissibleError` → `RT-FAIL-SUB-AGENT-TOPOLOGY-INADMISSIBLE`
- `SubAgentChildFailedError` → `RT-FAIL-SUB-AGENT-CHILD-FAILED`
- `SubAgentDispatchAuditComposeError` → `RT-FAIL-SUB-AGENT-AUDIT-COMPOSE` (new at v1.7;
  raised on 8b/8c/8d failure when the child path is SUCCESS / DRAINED; suppressed
  on FAILED / exception-bubble paths to preserve the primary fault).

**Entry-core source semantic (Q2(a) ratification).** Per CP spec v1.7 §13.5.1
+ OD spec v1.5 C-OD-24.6: the audit's `entry_core: StateLedgerEntryRef` (opaque
str per §24.4) references the F2 state-ledger entry recording the audited
dispatch action. v1.7 implementation uses the F2 entry's `action_id` as the
`StateLedgerEntryRef` value — `StateLedgerEntryRef` is opaque `NewType(str)`
per OD spec §24.4 and the action_id IS-canonically identifies the persisted
F2 entry (consistent with how `RuntimeAuditLedgerWriter._action_id_for(...)`
wraps audit entries into the IS chain). Spec narrative at step 8b mentions
"F2 entry's entry_hash"; this is a Class 3 prose drift — the IS
`LedgerWriter.append` does NOT expose the forward chain hash, and the OD-side
type is opaque str, so the action_id discipline holds without surface
extension. Carry-forward at the runtime spec drift items.

**Audit-signing config posture.** v1.7 MVP binds a deployment-level signing
key_id + algorithm at construction. Operator surfaces are deferred per spec
§14.7 "Deferred to implementation discretion" + ADR-D5 v1.3 §1.4.1 (HSM /
KMS / keystore deferral).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from harness_core.identity import ActionID
from harness_cp.cp_shared_types import ActorIdentity
from harness_cp.engine_class import EngineClass
from harness_cp.handoff_context import (
    ActionKind,
    HandoffContext,
    LedgerEntryRef,
    ProposedAction,
    RetryHistory,
    StateSummary,
)
from harness_cp.sub_agent_brief import SubAgentBrief
from harness_cp.topology_pattern import TopologyPattern
from harness_cp.topology_subagent_namespace import (
    SUBAGENT_NAMESPACE_SCHEMA,
    TOPOLOGY_NAMESPACE_SCHEMA,
)
from harness_cp.workflow_driver_types import (
    RunStatus,
    StepExecutionContext,
    StepKind,
    SubAgentChildPausedError,
    WorkflowStep,
    compose_branch_path,
)
from harness_cp.workflow_manifest_entry import WorkflowManifestEntry
from harness_cxa.cp_audit_conversion import cp_audit_to_od_audit
from harness_is.state_ledger_entry_schema import Identifier, Timestamp
from harness_is.state_ledger_write import EntryPayload, WriteKey, WriteResult
from harness_od.audit_ledger_types import SignatureAlgorithm, StateLedgerEntryRef
from pydantic import BaseModel, ConfigDict, ValidationError

from harness_runtime.lifecycle.audit_writer import RuntimeAuditLedgerWriter
from harness_runtime.lifecycle.child_workflow_runner import ChildWorkflowRunner
from harness_runtime.lifecycle.handoff import RuntimeHandoffRegistry
from harness_runtime.lifecycle.state_ledger import LedgerWriter
from harness_runtime.lifecycle.topology_dispatcher import RuntimeTopologyDispatcher

if TYPE_CHECKING:  # pragma: no cover — type-only import
    from collections.abc import Callable

__all__ = [
    "RuntimeSubAgentDispatcher",
    "SubAgentChildFailedError",
    "SubAgentDispatchAuditComposeError",
    "SubAgentDispatchPayload",
    "SubAgentDispatchPayloadShapeError",
    "SubAgentDispatchTopologyInadmissibleError",
    "compose_child_action_id",
]


# ---------------------------------------------------------------------------
# Payload schema (AC #3)
# ---------------------------------------------------------------------------


class SubAgentDispatchPayload(BaseModel):
    """Typed shape of a `SUB_AGENT_DISPATCH` step's `step_payload` (§14.7.2 step 1).

    `step.step_payload` is opaque to the driver per C-CP-25 §25.3.3.4 but
    typed at the dispatcher: v1.6 pins the convention that
    `SUB_AGENT_DISPATCH` payloads carry the child workflow's manifest + step
    sequence + lead-agent-authored brief. The composer pydantic-validates
    `step.step_payload → SubAgentDispatchPayload`; mis-shaped payloads
    surface as `SubAgentDispatchPayloadShapeError` mapping to
    `RT-FAIL-PAYLOAD-SHAPE` (existing fail class from §14.5).
    """

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    child_workflow_id: str
    """The child sub-workflow's workflow_id (per C-CP-06 §6.1)."""

    child_manifest_entry: WorkflowManifestEntry
    """The child's manifest entry — engine_class, topology_pattern,
    workload_class, persona_tier, per-step overrides, fallback chain."""

    child_steps: Sequence[WorkflowStep]
    """The child's declarative step sequence (in declaration order)."""

    brief: SubAgentBrief
    """Lead-agent-authored sub-agent brief (C-CP-13 §13.2 4-field +
    summary_hash). Drives HandoffContext composition + gate-level descent +
    audit-entry response_hash."""


# ---------------------------------------------------------------------------
# Typed errors (per spec §14.7 failure-mode taxonomy)
# ---------------------------------------------------------------------------


class SubAgentDispatchPayloadShapeError(Exception):
    """`step.step_payload` does not conform to `SubAgentDispatchPayload`.

    Driver's try/except per C-CP-25 §25.3.3.4 maps to `RT-FAIL-PAYLOAD-SHAPE`
    (existing fail class from spec §14.5).
    """


class SubAgentDispatchTopologyInadmissibleError(Exception):
    """Child manifest's topology + workload pair is not admissible at all.

    The pair is neither a C-CP-11 §11.1 primary topology for the workload nor a
    C-CP-10 §10.3 cross-pattern-admissible alternative. Verified at composer
    step 4 via ``ctx.topology_dispatcher.is_topology_permitted(pattern,
    workload)`` (the union predicate per U-RT-59 topology-admissibility Class
    1 fork Path A resolution; see
    ``.harness/class_1_tension_u_rt_59_topology_admissibility_predicate.md``).

    Raised before `subagent.span` opens; no partial spans emitted. Driver's
    try/except per C-CP-25 §25.3.3.4 maps to
    `RT-FAIL-SUB-AGENT-TOPOLOGY-INADMISSIBLE`.
    """


class SubAgentChildFailedError(Exception):
    """Child sub-workflow's terminal `RunResult.status == FAILED`.

    Raised after the composer's child-runner invocation per §14.7.2 step 6.
    Composer sets `subagent.result_status = "failed"` on the `subagent.span`
    before re-raising; the outer driver's try/except per C-CP-25 §25.3.3.4
    maps to `RT-FAIL-SUB-AGENT-CHILD-FAILED`.
    """


class SubAgentDispatchAuditComposeError(Exception):
    """Failure at one of step 8's audit-composition substeps (8b/8c/8d).

    Raised when the child path was SUCCESS / DRAINED but the audit
    composition + persistence sequence at §14.7.2 step 8 failed. Causes
    map as follows:

    - **8b (`LedgerWriteError` family)** — F2-write of the dispatch action
      raised a typed IS write error (`WriteKeyMismatchError`,
      `NonMonotonicTimestampError`, OSError on the underlying JSONL).
    - **8c (`ValueError` from `sign_audit_entry`)** — converter signature
      contract violation per CP spec v1.7 §13.5.1 (empty `key_id`, etc.).
    - **8d (`LedgerWriteError` family on audit_writer.append)** — IS-side
      persistence failure of the OD audit entry.

    The composer annotates `subagent.span` with `subagent.result_status =
    "failed"` before raising. Driver's try/except per C-CP-25 §25.3.3.4
    maps to `RT-FAIL-SUB-AGENT-AUDIT-COMPOSE` (new fail class at runtime
    spec v1.7 §14 follow-on patch).

    Suppressed on FAILED / exception-bubble paths — the primary fault is
    `SubAgentChildFailedError` / the original exception; audit composition
    is best-effort on those paths per spec §14.7.2 step 8 failure-semantics
    paragraph ("the audit-trail-fact record is preserved even when
    downstream substeps fail").
    """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def compose_child_action_id(parent_action_id: str, child_workflow_id: str) -> ActionID:
    """Compose the child sub-workflow's action_id (§14.7.4 deferred-to-discretion).

    Suggested shape per spec §14.7 "Deferred to implementation discretion":
    `f"{parent_action_id}::child::{child_workflow_id}"` for traceability. The
    `::child::` infix is a stable visual anchor in operator-facing ledger
    inspection without invoking a hash (which would lose the parent linkage).
    """
    return ActionID(f"{parent_action_id}::child::{child_workflow_id}")


# B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT-RECONCILER-CHILD (R-FS-1) — the child engine classes
# whose maybe-ran re-dispatch is RECOVERABLE (re-dispatch under the deterministic child run_id
# auto-resumes from the store AND `final_state` reconstructs via `reconstruct_final_state`).
# ALL FOUR durable resumable engine classes are now members; PURE_PATTERN_NO_ENGINE (non-durable,
# no resume) is the sole non-member. SAVE_POINT_CHECKPOINT joined ESR/WAL at the
# `…-SAVE-POINT-CHILD` close; RECONCILER_LOOP JOINS here. The composer (typed) + the CP
# `_subagent_child_recoverable` defensive read are MIRROR implementations of the same predicate —
# kept in sync by the by-execution agreement witness.
#
# RECONCILER admission is at-most-once-safe WITHOUT first building the F-1 engine-lock arc. A
# re-dispatched maybe-ran RECONCILER child runs its OWN crash-resume, which fires the U-CP-97
# engine-layer reconverge (`attempt_resume`) gated AT THE CAS CLAIM, upstream of the step loop
# (`reconciler_pause_resume_substrate.py` F-1). Three exhaustive cases, all safe: (1) child never
# won a claim → cleanly re-claims → auto-resumes the committed prefix (F2-skipped, not re-fired);
# (2) clean RESUME_CLEAN → same; (3) the F-1 window — the child WON the claim then crashed
# mid-re-execution → the retry of the already-claimed revision ABORTs (`ABORT_REVALIDATION_FAILED`)
# → the child returns RunStatus.FAILED *before any step re-executes* (at-most-once preserved, never
# a double-fire) → the parent fold raises `SubAgentChildFailedError` (fail-closed; never a SUCCESS
# aggregate). So admitting RECONCILER strictly IMPROVES the not-won-claim cases (recover vs
# fail-the-parent-closed) and routes the F-1 window through the SAME already-on-main
# RECONCILER-resume ABORT→§22.1-HITL disposition (#779/#781); the registered F-1 engine-lock
# auto-recovery arc improves ALL RECONCILER resumes and is NOT a prerequisite for this slice.
# (SAVE_POINT, unlike RECONCILER, fires NO recovery loop / CAS-claim — no F-1 window at all.)
# This is a DEDICATED set, distinct from the CP `_FANOUT_REPLAY_ENGINE_CLASSES` even though both
# currently contain the four durable engine classes — carrier segregation, not a shared authority.
_SUBAGENT_RECOVERABLE_CHILD_ENGINE_CLASSES: frozenset[EngineClass] = frozenset(
    {
        EngineClass.EVENT_SOURCED_REPLAY,
        EngineClass.WAL_SEGMENT,
        EngineClass.SAVE_POINT_CHECKPOINT,
        EngineClass.RECONCILER_LOOP,
    }
)
# Child step kinds that HARD-disqualify a child regardless of recursion: MANAGED_AGENTS is an
# unfenced vendor sink with no recursively-classifiable child manifest → fail closed. A nested
# SUB_AGENT_DISPATCH is DELIBERATELY NOT here — the NONLEAF-CHILD arc (R-FS-1) admits it IFF its
# own child is recursively recoverable (`subagent_child_recoverable` descends into the grandchild
# payload). The recursion bottoms out at a LINEAR leaf (no SUB_AGENT/MANAGED child steps). TOOL_STEP
# / INFERENCE_STEP / DECLARATIVE_STEP / HITL_STEP children remain in-scope (non-recursive).
_SUBAGENT_RECOVERABLE_HARD_EXCLUDED_CHILD_KINDS: frozenset[StepKind] = frozenset(
    {StepKind.MANAGED_AGENTS}
)
# B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT-FANOUT-CHILD (R-FS-1) — the mirror of the CP
# `_SUBAGENT_RECOVERABLE_FANOUT_CHILD_TOPOLOGIES` + `_FANOUT_REPLAY_ENGINE_CLASSES`. A maybe-ran
# SUB_AGENT_DISPATCH whose child is itself FAN-OUT recovers by re-dispatch: the child re-runs under
# its deterministic `child_run_id` and reconstructs its AGGREGATE through the B-FANOUT-OUTPUT-REPLAY
# branch store (CP `_crash_fan_out_resume`). That store backs {ESR,WAL,SAVE_POINT,RECONCILER} (the
# CP `_fanout_replay_store` gate): SAVE_POINT joined at the `…-FANOUT-CHILD-SAVE-POINT` slice, and
# RECONCILER joins at the `…-FANOUT-CHILD-RECONCILER` close because its reconciler substrate owns
# convergence/CAS state, not the per-branch output map. The CP↔runtime agreement witness enforces
# parity on this mirror.
_SUBAGENT_RECOVERABLE_FANOUT_CHILD_TOPOLOGIES: frozenset[TopologyPattern] = frozenset(
    {
        TopologyPattern.PARALLELIZATION,
        TopologyPattern.ORCHESTRATOR_WORKERS,
        TopologyPattern.HIERARCHICAL_DELEGATION,
    }
)
_SUBAGENT_RECOVERABLE_FANOUT_CHILD_ENGINE_CLASSES: frozenset[EngineClass] = frozenset(
    {
        EngineClass.EVENT_SOURCED_REPLAY,
        EngineClass.WAL_SEGMENT,
        EngineClass.SAVE_POINT_CHECKPOINT,
        EngineClass.RECONCILER_LOOP,
    }
)


def compose_child_run_id_seed(
    parent_idempotency_key: str, child_workflow_id: str, branch_path: str | None = None
) -> str:
    """B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT (R-FS-1) — the DETERMINISTIC
    first-dispatch child run_id seed (§14.7.4).

    `sha256("child-run:" + parent_idempotency_key + [":" + branch_path] + ":" + child_workflow_id)`.

    **`branch_path` is REQUIRED for a fan-out branch (out-of-family Codex [P1]).** The spawning
    worker's `parent_idempotency_key` is `_compute_step_idempotency_key(run_idempotency_key,
    step_index)` — under fan-out, `compose_branch_child_context` inherits it VERBATIM from the
    fan-out parent (the branch-distinct key is the §25.16 `branch_path`, composed DOWNSTREAM, NOT
    folded into `parent_idempotency_key`). So WITHOUT `branch_path` two sibling SUB_AGENT_DISPATCH
    workers that dispatch the SAME `child_workflow_id` would derive the SAME child run_id → ALIASED
    durable output + fence state → cross-branch corruption EVEN WITHOUT A CRASH. `branch_path`
    (`{parent_action_id}:{branch_index}`, C-CP-25 §25.16 — globally unique under nested fan-out per
    IS §5.4) makes the seed per-branch-unique. `None` ⟹ a LINEAR (non-branch) dispatch (no
    sibling-collision surface).

    Both components RE-DERIVE IDENTICALLY when the parent re-dispatches a maybe-ran worker on
    crash-resume (the run key is recovered from the preserved run_id; step_index + branch_index are
    deterministic manifest positions). Mixing in `child_workflow_id` keeps the seed distinct if the
    same worker ever pointed at a different child (the accepted child-swap parity). Replaces the
    legacy fresh `uuid.uuid4().hex` first-dispatch run_id, which — being transient — was lost on a
    parent crash, leaving the child's durable store + fence reserves keyed on an unrecoverable
    identity (the maybe-ran-subagent recovery blocker).

    At-most-once PRESERVED: the child still runs once on the happy path; on a re-dispatch the stable
    per-branch key lets the child's OWN crash-resume auto-resume from the shared durable store."""
    _base = (
        parent_idempotency_key if branch_path is None else f"{parent_idempotency_key}:{branch_path}"
    )
    return hashlib.sha256(f"child-run:{_base}:{child_workflow_id}".encode()).hexdigest()


def subagent_child_recoverable(payload: SubAgentDispatchPayload) -> bool:
    """B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT (R-FS-1) — whether a maybe-ran SUB_AGENT_DISPATCH
    worker's CHILD is RE-DISPATCH-RECOVERABLE: re-dispatching it under the deterministic child
    run_id auto-resumes the child from its durable store AND reconstructs a result-faithful
    `final_state` (no parent-fold corruption).

    THREE conjuncts (all required — the corrected predicate over the #746 reverted branch, which
    keyed on the engine class ALONE and was reverted on the [P1-a] result-fidelity gap):

    1. **engine ∈ {ESR, WAL, SAVE_POINT, RECONCILER}** — the child's per-step output is durably
       recorded, so the resumed child auto-resumes (`resume_at>0` via the engine-class-agnostic
       F2-prefix join) AND `reconstruct_final_state` rebuilds the COMPLETE terminal state. ALL FOUR
       durable resumable classes are members (PURE_PATTERN_NO_ENGINE is the lone non-member).
       SAVE_POINT joined at the `…-SAVE-POINT-CHILD` close (no CAS-claim, no F-1 window);
       RECONCILER_LOOP joins at the `…-RECONCILER-CHILD` close (R-FS-1): a maybe-ran RECONCILER
       child re-dispatch runs its OWN crash-resume, firing the U-CP-97 reconverge (`attempt_resume`)
       gated AT THE CLAIM, upstream of the step loop — so the F-1 won-CAS-claim-retry window
       manifests as `ABORT_REVALIDATION_FAILED` → child RunStatus.FAILED *before any step
       re-executes* (at-most-once preserved) → the parent fold raises `SubAgentChildFailedError`
       (fail-closed; never a SUCCESS aggregate). The F-1 engine-lock auto-recovery arc improves ALL
       RECONCILER resumes and is NOT a prerequisite for this child-recoverability slice.
    2. **topology recoverable-by-substrate** (the FANOUT-CHILD relaxation, R-FS-1): either
       SINGLE_THREADED_LINEAR (any of the four durable engine classes — the auto-resume +
       `reconstruct_final_state` LINEAR seed) OR a FAN-OUT child (PARALLELIZATION /
       ORCHESTRATOR_WORKERS / HIERARCHICAL_DELEGATION) whose engine ∈ {ESR,WAL,SAVE_POINT,
       RECONCILER}. A
       fan-out child reconstructs its AGGREGATE via the SEPARATE B-FANOUT-OUTPUT-REPLAY branch store
       (CP `_crash_fan_out_resume`); that store backs {ESR,WAL,SAVE_POINT,RECONCILER}, so
       EVALUATOR_OPTIMIZER / DECENTRALIZED_HANDOFF (no fan-out replay substrate).
    3. **every child step is non-MANAGED_AGENTS, and every nested SUB_AGENT_DISPATCH child step is
       ITSELF recoverable** — the RECURSIVE leaf/non-leaf condition (the NONLEAF-CHILD arc, R-FS-1,
       relaxing the prior LINEAR-leaf-only slice #770 witnessed). A MANAGED_AGENTS child step is an
       unfenced vendor sink with no recursively-classifiable child manifest → fail closed. A nested
       SUB_AGENT_DISPATCH grandchild is admitted IFF `subagent_child_recoverable` holds on its OWN
       payload (the same {ESR,WAL,SAVE_POINT,RECONCILER} ∧ LINEAR ∧ recursive-leaf test) — correct
       at ALL depths by construction, bottoming out at a LINEAR leaf. The grandchild's own
       crash-resume auto-resumes under its deterministic `child_run_id_seed` (composed at the
       child's re-dispatch by the same composer code at each level) → no parent-fold corruption at
       any depth. A mis-shaped nested payload (cannot validate to `SubAgentDispatchPayload`) → fail
       closed. TOOL/INFERENCE/DECLARATIVE/HITL child steps are in-scope (non-recursive). A FAN-OUT
       grandchild is admitted IFF its engine ∈ {ESR,WAL,SAVE_POINT,RECONCILER} (the FANOUT-CHILD
       conjunct 2).

    The composer gates the DETERMINISTIC `child_run_id_seed` on this (a non-recoverable child gets
    a fresh `uuid` → no auto-resume → pre-existing behavior, no suffix-only corruption), and the CP
    classifier independently mirrors it (`_subagent_child_recoverable`, defensive opaque-payload
    read) to decide whether a maybe-ran SUB_AGENT_DISPATCH branch is re-dispatch-recoverable."""
    cme = payload.child_manifest_entry
    if cme.engine_class not in _SUBAGENT_RECOVERABLE_CHILD_ENGINE_CLASSES:
        return False
    # Conjunct 2 — the FANOUT-CHILD relaxation (mirror of the shared CP predicate).
    # A fan-out child reconstructs its aggregate through the {ESR,WAL,SAVE_POINT,RECONCILER}
    # fan-out replay store; RECONCILER's CAS/reconciler substrate is not the aggregate output-map
    # authority.
    _fanout_recoverable = (
        cme.topology_pattern in _SUBAGENT_RECOVERABLE_FANOUT_CHILD_TOPOLOGIES
        and cme.engine_class in _SUBAGENT_RECOVERABLE_FANOUT_CHILD_ENGINE_CLASSES
    )
    if not (cme.topology_pattern is TopologyPattern.SINGLE_THREADED_LINEAR or _fanout_recoverable):
        return False
    for child_step in payload.child_steps:
        if child_step.step_kind in _SUBAGENT_RECOVERABLE_HARD_EXCLUDED_CHILD_KINDS:
            return False
        if child_step.step_kind is StepKind.SUB_AGENT_DISPATCH:
            # Recursive descent (the NONLEAF-CHILD relaxation): a nested SUB_AGENT_DISPATCH child is
            # admitted IFF it is itself recoverable. DELEGATE the nested decision to the SHARED CP
            # `payload_child_recoverable` (ONE SOURCE OF TRUTH, out-of-family Codex [P1]): a runtime
            # `SubAgentDispatchPayload.model_validate` here would REJECT a partially-valid nested
            # payload (missing child_workflow_id/brief) that the CP mirror — which cannot
            # model_validate (a forbidden harness_cp→harness_runtime import) — ADMITS →
            # CP-True/runtime-False → no seed → double-fire. The shared defensive predicate
            # classifies nested payloads IDENTICALLY on both sides; recoverability depends only on
            # engine+topology+child_steps (child_workflow_id/brief affect DISPATCHABILITY, fail
            # closed at the dispatcher's own model_validate). workflow_driver is already loaded when
            # the runtime dispatches (it calls execute_workflow), so the import is effectively free.
            from harness_cp.workflow_driver import payload_child_recoverable

            try:
                if not payload_child_recoverable(
                    child_step.step_payload["child_manifest_entry"],
                    child_step.step_payload["child_steps"],
                ):
                    return False
            except (TypeError, KeyError, ValueError, AttributeError):
                return False
    return True


def _empty_summary_hash() -> str:
    """`sha256(b"")` hex-64 — the v1.6 MVP `StateSummary.summary_hash` default.

    Per spec §14.7.3 v1.6 MVP composition: `state_summary.summary_hash =
    sha256(b"")`. Deferred to v1.7+ when actual summarization invocation
    lands (C-CP-21 §21.4)."""
    return hashlib.sha256(b"").hexdigest()


def _compose_handoff_context(
    *,
    step_context: StepExecutionContext,
    payload: SubAgentDispatchPayload,
) -> HandoffContext:
    """Build the v1.6 MVP `HandoffContext` per spec §14.7.3 (AC #4).

    Bounded-reduction composition per §14.7.3 table:

    - `proposed_action` — `ProposedAction(action_kind=SUB_AGENT_DISPATCH,
      payload={"objective": brief.objective}, brief=payload.brief)` per
      real `ProposedAction` shape (spec prose `ProposedAction(text=...)`
      was incorrect; rolled into the Class 3 spec-prose-drift note).
    - `agent_confidence` — `None` at v1.6 MVP.
    - `failed_attempts` — empty tuple.
    - `alternatives_considered` — empty tuple.
    - `state_summary` — `StateSummary(relevant_entries=(parent_entry_ref,),
      summary_text="", summary_hash=sha256(b""),
      idempotency_key=step_context.parent_idempotency_key,
      external_references=())`.
    - `audit_trail_link` — `LedgerEntryRef(action_id=step_context.parent_action_id,
      entry_hash=step_context.parent_entry_hash, actor=step_context.parent_actor.actor_id)`
      per `Spec_Control_Plane_v1_6.md` §25.2.1 Path A.
    - `retry_history` — empty `RetryHistory` (cardinality 0, count 0).
    """
    parent_action_id = cast(ActionID, step_context.parent_action_id)
    # `step_context.parent_actor` is the IS-exported `Actor` (BaseModel with
    # `actor_class` + `actor_id`); `LedgerEntryRef.actor` is the CP-owned
    # `ActorIdentity` (NewType[str]). Project Actor → ActorIdentity via the
    # canonical `actor_id` string per the CP-vs-IS actor-identity carrier-map
    # convention at `harness_cp.cp_shared_types` §53.
    actor_identity = ActorIdentity(step_context.parent_actor.actor_id)
    parent_entry_ref = LedgerEntryRef(
        action_id=parent_action_id,
        entry_hash=step_context.parent_entry_hash,
        actor=actor_identity,
    )
    return HandoffContext(
        proposed_action=ProposedAction(
            action_kind=ActionKind.SUB_AGENT_DISPATCH,
            payload={"objective": payload.brief.objective},
            brief=payload.brief,
        ),
        agent_confidence=None,
        failed_attempts=(),
        alternatives_considered=(),
        state_summary=StateSummary(
            relevant_entries=(parent_entry_ref,),
            summary_text="",
            summary_hash=_empty_summary_hash(),
            idempotency_key=Identifier(step_context.parent_idempotency_key),
            external_references=(),
        ),
        audit_trail_link=parent_entry_ref,
        retry_history=RetryHistory(
            attempts=(),
            retry_count=0,
            last_retry_cause=None,
        ),
    )


# ---------------------------------------------------------------------------
# Composer (AC #2 — Protocol satisfaction)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RuntimeSubAgentDispatcher:
    """Sub-agent dispatch composer (U-RT-59; satisfies `StepDispatcher` Protocol).

    Per `Spec_Harness_Runtime_v1.md` v1.6 §14.7 C-RT-17. Sync `dispatch`
    method satisfying the sync `StepDispatcher` Protocol declared at
    `harness-cp/src/harness_cp/workflow_driver.py:175` (`@runtime_checkable`).
    Constructed at bootstrap stage 5 (LOOP_INIT) per spec §14.7.7
    "Integration with C-RT-04"; bound to `HarnessContext.sub_agent_dispatcher`.

    Composition arguments per §14.7.1 + v1.7 §14.7.2 step 8 extension:

    - `handoff_registry` (U-RT-26) — composes `HandoffContext` + computes
      `SubAgentGateLevelDescent` + composes `CPAuditLedgerEntry` at 8a.
    - `topology_dispatcher` (U-RT-40) — dispatches `TopologyPattern` per
      child manifest + verifies admissibility.
    - `tracer_provider` (C-RT-06) — opens the `subagent.span`.
    - `child_workflow_runner` (U-RT-59 AC #7) — invokes the child sub-
      workflow in-process per §14.7.4 recursive primitive.
    - `ledger_writer` (U-RT-12) — F2-write of the dispatch action at 8b.
    - `audit_writer` (U-RT-32) — IS-anchored OD audit-entry persistence
      at 8d via `RuntimeAuditLedgerWriter.append(tenant_id, od_entry)`.
    - `audit_signing_key_id` / `audit_signing_algorithm` — signing config
      passed to the converter at 8c (`cp_audit_to_od_audit`); operator
      surface deferred per ADR-D5 v1.3 §1.4.1.
    - `time_source` — timestamp injection point for the F2 dispatch
      entry at 8b (test determinism; default `datetime.now(UTC)` at the
      construction site).
    """

    handoff_registry: RuntimeHandoffRegistry
    topology_dispatcher: RuntimeTopologyDispatcher
    tracer_provider: Any
    """Typed `Any` per the C-RT-04 pattern (avoids pulling OTel SDK type
    into the schema layer); matches `RuntimeLLMDispatcher` /
    `RetryBreakerFallbackDispatcher` discipline."""

    child_workflow_runner: ChildWorkflowRunner
    ledger_writer: LedgerWriter
    audit_writer: RuntimeAuditLedgerWriter
    audit_signing_key_id: str
    audit_signing_algorithm: SignatureAlgorithm
    time_source: Callable[[], Timestamp]
    procedural_tier_snapshot_resolver: Callable[[], Identifier]
    """R-003 producer-site lift — resolves the `procedural_tier_snapshot_ref`
    D-derivative sidecar for the F2 dispatch entry at 8b. Invoked zero-arg at
    the `EntryPayload(...)` construction. This is a workflow-context emission
    per IS spec v1.3 §C-IS-05 §5.1, so the sidecar MUST be populated (a `None`
    value would be a producer-site bug). Resolver closure built at bootstrap
    stage 5 via `make_procedural_tier_snapshot_resolver(ctx)`; mirrors the
    `RuntimeCpIsWiring.procedural_tier_snapshot_resolver` pattern for the 6
    §16.5 CP composers (`cp_is_wiring.py`)."""

    # Module-bound canonical attribute name constants (per spec §14.7.5
    # "Producer-side attribute carrier reference" — imported from the
    # canonical carrier; not hand-coded as strings). Frozen at construction
    # so a typo in the spec carrier surfaces at dataclass instantiation,
    # not at first dispatch.
    _subagent_attr_names: tuple[str, ...] = field(init=False)
    _topology_attr_names: tuple[str, ...] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "_subagent_attr_names",
            tuple(s.attribute_name for s in SUBAGENT_NAMESPACE_SCHEMA),
        )
        object.__setattr__(
            self,
            "_topology_attr_names",
            tuple(s.attribute_name for s in TOPOLOGY_NAMESPACE_SCHEMA),
        )

    def _compose_and_persist_audit(
        self,
        *,
        parent_action_id: ActionID,
        descent: Any,
        payload: SubAgentDispatchPayload,
        step_context: StepExecutionContext,
        raise_on_failure: bool,
    ) -> tuple[Any, WriteResult | None]:
        """Step 8 4-substep audit sequence (v1.7 §14.7.2 step 8).

        Returns ``(cp_entry, write_result)`` — the CP-shape dispatch fact
        and the WriteResult from 8d (or ``None`` when an intermediate
        substep fails and ``raise_on_failure=False``).

        - **8a** Compose `CPAuditLedgerEntry` via
          `handoff_registry.compose_dispatch_audit(...)`. Never fails
          under normal conditions; an exception here propagates regardless
          of `raise_on_failure` since 8a is the dispatch-fact ground truth.
        - **8b** F2-write the dispatch action via `ledger_writer.append`.
          Action_id pattern: ``dispatch:<parent_action_id>:<child_index>``.
          The action_id IS the `StateLedgerEntryRef` passed to 8c per the
          OD spec v1.5 C-OD-24.4 opaque-str discipline.
        - **8c** Convert CP→OD via `cp_audit_to_od_audit(...)` —
          `audit.cp.*` namespace projection + OD signing per CP spec v1.7
          §13.5.1.
        - **8d** Persist via `audit_writer.append(tenant_id, od_entry)`
          per C-RT-04 + OD spec v1.5 C-OD-24.

        On 8b/8c/8d failure with ``raise_on_failure=True``:
        ``SubAgentDispatchAuditComposeError`` is raised (caller responsible
        for annotating `subagent.span` with ``result_status="failed"``).
        With ``raise_on_failure=False`` (used on FAILED + exception-bubble
        paths), the failure is swallowed and ``(cp_entry, None)`` is
        returned — the dispatch fact at 8a is preserved per spec
        §14.7.2 step 8 failure-semantics paragraph.
        """
        # 8a — compose CP audit (dispatch fact; always produced).
        brief_hash = self.handoff_registry.dispatch_response_hash(payload.brief)
        cp_entry = self.handoff_registry.compose_dispatch_audit(
            parent_action_id=parent_action_id,
            descent=descent,
            brief_hash=brief_hash,
        )

        # Compose the F2 dispatch-action action_id once; reused as both the
        # IS entry's identity AND the StateLedgerEntryRef bound at 8c.
        # OD spec v1.5 C-OD-24.4: `StateLedgerEntryRef = NewType(str)` —
        # opaque marker; action_id IS-canonically identifies the persisted
        # F2 entry. Spec narrative cites "entry_hash"; Class 3 prose drift
        # carry-forward (LedgerWriter.append does not expose the forward
        # chain hash + the OD type accepts opaque str).
        child_index = getattr(descent, "child_index", 0)
        dispatch_action_id = Identifier(f"dispatch:{parent_action_id}:{child_index}")

        try:
            # 8b — F2-write the dispatch action.
            f2_payload = EntryPayload(
                action_id=dispatch_action_id,
                idempotency_key=dispatch_action_id,
                actor=step_context.parent_actor,
                timestamp=self.time_source(),
                procedural_tier_snapshot_ref=(self.procedural_tier_snapshot_resolver()),
            )
            f2_key = WriteKey(
                thread_id=Identifier(f"dispatch:{parent_action_id}"),
                step_id=dispatch_action_id,
                idempotency_key=dispatch_action_id,
            )
            self.ledger_writer.append(f2_payload, f2_key)
            entry_core = StateLedgerEntryRef(str(dispatch_action_id))

            # 8c — convert CP → OD (signing happens inside the converter).
            od_entry = cp_audit_to_od_audit(
                cp_entry,
                key_id=self.audit_signing_key_id,
                algo=self.audit_signing_algorithm,
                entry_core=entry_core,
            )

            # 8d — persist OD audit entry through IS hash chain.
            write_result = self.audit_writer.append(
                tenant_id=step_context.tenant_id,
                audit_entry=od_entry,
            )
        except Exception as exc:
            if raise_on_failure:
                raise SubAgentDispatchAuditComposeError(
                    f"sub-agent dispatch audit composition failed for "
                    f"parent_action_id={parent_action_id!r}: {exc}"
                ) from exc
            return cp_entry, None

        return cp_entry, write_result

    def dispatch(
        self,
        binding: Any,
        step: WorkflowStep,
        *,
        step_context: StepExecutionContext,
    ) -> Mapping[str, Any]:
        """Sync dispatch composer body (§14.7.2; v1.6 MVP per fork resolutions).

        `binding` is typed `Any` here for the same C-RT-04 reason: the
        Protocol declares `StepEffectiveBinding`; the runtime objects
        satisfy the structural shape. Pyright cannot infer Protocol
        satisfaction at this site, so the type relaxation moves to the
        composer. The composer reads `binding.model_binding` as the
        child's `default_model_binding` per spec §14.7.4 +
        `ChildWorkflowRunner` additive Protocol shape.

        Raises
        ------
        SubAgentDispatchPayloadShapeError
            `step.step_payload` failed Pydantic validation against
            `SubAgentDispatchPayload`.
        SubAgentDispatchTopologyInadmissibleError
            Child manifest's `(topology, workload_class)` pair fails
            C-CP-10 §10.3 admissibility.
        SubAgentChildFailedError
            Child sub-workflow's terminal `RunResult.status == FAILED`.
        """
        # --- Step 1: validate payload shape (AC #3) ------------------------
        try:
            payload = SubAgentDispatchPayload.model_validate(dict(step.step_payload))
        except ValidationError as exc:
            raise SubAgentDispatchPayloadShapeError(
                f"SUB_AGENT_DISPATCH step {str(step.step_id)!r} step_payload "
                f"failed SubAgentDispatchPayload validation: {exc}"
            ) from exc

        # --- Step 2: compose HandoffContext (AC #4) ------------------------
        handoff_context = _compose_handoff_context(step_context=step_context, payload=payload)

        # --- Step 3: compute gate-level descent (AC #5a) -------------------
        parent_action_id = cast(ActionID, step_context.parent_action_id)
        descent = self.handoff_registry.dispatch(
            parent_action_id=parent_action_id,
            parent_gate_level=step_context.parent_gate_level,
            parent_sandbox_tier=step_context.parent_sandbox_tier,
            sub_agent_brief=payload.brief,
            operator_override=None,
        )

        # --- Step 4: topology dispatch + strict permission gate (AC #5b) ---
        # Path A resolution of the U-RT-59 topology-admissibility Class 1 fork
        # (.harness/class_1_tension_u_rt_59_topology_admissibility_predicate.md):
        # the spec-named predicate `is_admissible(...)` at §14.7.2 step 4
        # answers C-CP-10 §10.3's CROSS-PATTERN-only admissibility — it returns
        # False for every workload's primary topology because §10.3 annotates
        # non-primary alternatives only. The composer's intent is "admissible
        # at all for this workload" — primary OR cross-pattern. Path A adds
        # `is_topology_permitted(pattern, workload)` at the runtime topology
        # dispatcher, delegating to `harness_cp.per_workload_class_topology
        # .is_topology_permitted_for_workload` (membership in the workload's
        # `permitted_patterns` set, constructed as primary topologies ∪
        # admissibility-closed cross-patterns per the `_permitted` factory).
        # The strict gate is restored with the correct union semantic.
        #
        # Spec §14.7.2 step 4 still names `is_admissible(...)` — this is a
        # documented Class 3 drift (item 8 at
        # `.harness/class_3_tension_u_rt_59_spec_prose_drift.md`); the
        # composer lands against the correct predicate and the spec prose
        # absorbs the rename at the next runtime spec revision pass.
        topology = self.topology_dispatcher.dispatch(payload.child_manifest_entry)
        workload = payload.child_manifest_entry.workload_class
        if not self.topology_dispatcher.is_topology_permitted(topology, workload):
            raise SubAgentDispatchTopologyInadmissibleError(
                f"topology {topology.value!r} is not admissible for workload "
                f"{workload.value!r} — neither a C-CP-11 §11.1 primary topology "
                f"nor a C-CP-10 §10.3 cross-pattern-admissible alternative"
            )

        # --- Step 5: open subagent.span + set attributes (AC #6) -----------
        tracer = self.tracer_provider.get_tracer("harness.runtime.sub_agent_dispatch")
        with tracer.start_as_current_span("subagent.span") as span:
            span_context = span.get_span_context()
            span_id_hex = f"{span_context.span_id:016x}"
            parent_span_context = span.parent if hasattr(span, "parent") and span.parent else None
            parent_span_id_hex = (
                f"{parent_span_context.span_id:016x}"
                if parent_span_context is not None
                else "0" * 16
            )

            # Open-time `subagent.*` attributes (3 of 7 set now; 4 close-time)
            span.set_attribute("subagent.span.id", span_id_hex)
            span.set_attribute("subagent.parent_span_id", parent_span_id_hex)
            # Open-time `topology.*` attributes (2 narrow-subset attributes;
            # fan-out-specific 8 attributes NOT set per §14.7.2 step 5).
            span.set_attribute("topology.pattern", topology.value)
            span.set_attribute(
                "topology.workload_class",
                payload.child_manifest_entry.workload_class.value,
            )

            # --- Step 6: invoke child runner (AC #7) -----------------------
            # B-HIERARCHICAL-PAUSE — when the parent fan-out is RESUMING a
            # previously-paused child, the CP driver set `child_resume_snapshot` on
            # this worker's StepExecutionContext (the hash-inert per-step carrier).
            # Forward it so the child re-enters at its cursor rather than re-running
            # from scratch (the grandchild's completed steps are recovered, NOT
            # re-executed). `None` on a first dispatch → byte-identical to pre-arc.
            # B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT (R-FS-1) — gate the DETERMINISTIC
            # child run_id seed. It is scoped to a FAN-OUT WORKER (`branch_index is not None`)
            # with a RECOVERABLE child — exactly the surface the fan-out maybe-ran crash-resume
            # classifier (`_fence_unrecoverable_maybe_ran_indices`) recovers.
            # A recoverable fan-out worker gets the stable seed so a parent-crash re-dispatch
            # auto-resumes its child (result-faithful reconstruction). Everything else → `None` →
            # a fresh `uuid` → no auto-resume → pre-existing behavior. TWO at-most-once guards:
            #   (1) `branch_path` (§25.16, `{parent_action_id}:{branch_index}`) makes the seed
            #       per-branch-UNIQUE — sibling workers inherit the SAME `parent_idempotency_key`
            #       from the fan-out parent (`compose_branch_child_context` copies it verbatim; the
            #       branch-distinct key is composed downstream), so WITHOUT branch_path two siblings
            #       dispatching the same child_workflow_id would alias one child run_id EVEN WITHOUT
            #       A CRASH (out-of-family Codex [P1]).
            #   (2) `branch_index is not None` EXCLUDES the SEQUENTIAL-LOOP topologies
            #       (EVALUATOR_OPTIMIZER / RECONCILER_LOOP), whose iterated steps reuse the same
            #       declared `step_index` (→ same `parent_idempotency_key`) across iterations
            #       (`workflow_driver.py:2040` — "step_index REPEATS across same-parity re-pauses").
            #       A deterministic seed there would make loop iteration 2 auto-resume iteration 1's
            #       durable store (suppressing a NEW logical run). Those steps carry no
            #       branch_index, so they keep the legacy fresh-`uuid` (no auto-resume) — the
            #       SAVE_POINT/RECONCILER suffix-only corruption + the loop-suppression foreclosed.
            _is_recoverable_fanout_worker = (
                step_context.branch_index is not None and subagent_child_recoverable(payload)
            )
            # B-FANOUT-CRASH-RESUME-ORCHESTRATOR-MAYBE-RAN-SUBAGENT (R-FS-1) — the orchestrator
            # (`steps[0]`) is a SINGLE, once-per-run step (`branch_index is None`, like a
            # sequential-loop iteration) but UNLIKE a loop iteration it dispatches EXACTLY ONCE,
            # so a deterministic seed is SAFE here — there is no iteration-2 to alias iteration-1's
            # durable store (the loop-suppression hazard the `branch_index is not None` gate
            # forecloses for EVALUATOR_OPTIMIZER / RECONCILER_LOOP). The CP driver marks the
            # orchestrator's context with `is_orchestrator_dispatch` (hash-inert) so we distinguish
            # it from those iterated steps. `branch_path=None` — the orchestrator is unique within
            # the run (no fan-out siblings to collide), and its `parent_idempotency_key`
            # (`orchestrator_idempotency_key`, step_index 0) RE-DERIVES IDENTICALLY on a parent
            # crash re-dispatch (the whole fan-out re-runs fresh → the orchestrator re-dispatches
            # → its child auto-resumes from the shared store). A worker's seed INSERTS
            # `branch_path`, so the orchestrator's child run_id stays DISTINCT from any worker's
            # even on the same `child_workflow_id` (no orchestrator↔worker child aliasing).
            _is_recoverable_orchestrator = (
                step_context.is_orchestrator_dispatch and subagent_child_recoverable(payload)
            )
            # B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT-NONLEAF-CHILD (R-FS-1) — the THIRD
            # seed surface: a recoverable nested (grandchild) SUB_AGENT_DISPATCH dispatched
            # by the SINGLE_THREADED_LINEAR inline step loop (`is_linear_sequential_dispatch`).
            # The recursive `subagent_child_recoverable` now ADMITS a LINEAR child whose own
            # child step is a recoverable SUB_AGENT; without a deterministic seed here, a
            # maybe-ran parent's re-dispatch of that LINEAR child would re-dispatch the
            # grandchild with a FRESH uuid → the grandchild re-runs fresh → its committed
            # effects DOUBLE-FIRE. Like the orchestrator, a linear-loop step is once-per-run
            # (`branch_index is None`) but dispatches EXACTLY ONCE — its `(run_id, step_index)`
            # recurs only as a SAME-LOGICAL-STEP forward resume (`resume_at` advances forward
            # over the committed prefix at the linear loop), NEVER as a distinct iteration —
            # so `branch_path=None` is safe (no iteration-2 to alias iteration-1's store; the
            # EVALUATOR_OPTIMIZER step_index-reuse hazard never reaches the linear loop). The
            # seed re-derives identically on the parent's re-dispatch because the parent's
            # step idempotency key is the deterministic monotonic manifest position.
            _is_recoverable_linear_sequential = (
                step_context.is_linear_sequential_dispatch and subagent_child_recoverable(payload)
            )
            # SAME-STEP IDENTITY for the linear-sequential seed (out-of-family Codex [P1]). UNLIKE
            # the fan-out/orchestrator paths, the linear-sequential step carries NO maybe-ran
            # dispatch marker, so the dual gate's same-step_id + same-engine guards do NOT protect
            # it. The seed is keyed on the step INDEX (`parent_idempotency_key`), so RENAMING the
            # SUB_AGENT, SWAPPING its child engine (RECONCILER/SAVE_POINT), or SWAPPING its child
            # TOPOLOGY (LINEAR/fan-out: the FANOUT-CHILD arc, R-FS-1, out-of-family Codex [P1]) at
            # the SAME index (same `child_workflow_id`) between crash + resume would re-derive the
            # SAME seed and auto-resume the OLD child's durable outputs under a DIFFERENT logical
            # step, or through a DIFFERENT recovery substrate (LINEAR `reconstruct_final_state` seed
            # vs the fan-out `_crash_fan_out_resume` branch store: the at-most-once bypass the
            # marker dual gate prevents at the worker/orchestrator level). We fold the `step_id`,
            # the IMMEDIATE child `engine_class` AND its `topology_pattern` into the seed (via the
            # `branch_path` disambiguator slot, JSON-encoded so a `step_id` with delimiters cannot
            # collide; the `linear-step:` prefix is distinct from a worker's
            # `{action_id}:{branch_index}` + orchestrator `None`, no cross-path aliasing). A rename
            # / engine swap / topology swap CHANGES the seed: the edited step gets a FRESH run_id
            # (re-runs fresh, NOT an auto-resume of the old store through the wrong substrate); a
            # legitimate resume keeps all three so the seed re-derives identically and auto-resume
            # works. The IMMEDIATE engine + topology suffice: a DEEPER grandchild-of-grandchild swap
            # is caught by that deeper dispatch's OWN linear-sequential seed (per-level binding).
            _linear_step_disambiguator = "linear-step:" + json.dumps(
                [
                    str(step.step_id),
                    payload.child_manifest_entry.engine_class.value,
                    payload.child_manifest_entry.topology_pattern.value,
                ]
            )
            _child_run_id_seed = (
                compose_child_run_id_seed(
                    step_context.parent_idempotency_key,
                    payload.child_workflow_id,
                    branch_path=compose_branch_path(step_context),
                )
                if _is_recoverable_fanout_worker
                else compose_child_run_id_seed(
                    step_context.parent_idempotency_key,
                    payload.child_workflow_id,
                    branch_path=_linear_step_disambiguator,
                )
                if _is_recoverable_linear_sequential
                else compose_child_run_id_seed(
                    step_context.parent_idempotency_key,
                    payload.child_workflow_id,
                    branch_path=None,
                )
                if _is_recoverable_orchestrator
                else None
            )
            try:
                child_result = self.child_workflow_runner(
                    workflow_id=payload.child_workflow_id,
                    manifest_entry=payload.child_manifest_entry,
                    steps=payload.child_steps,
                    handoff_context=handoff_context,
                    descent=descent,
                    default_model_binding=binding.model_binding,
                    pause_snapshot_input=step_context.child_resume_snapshot,
                    child_run_id_seed=_child_run_id_seed,
                )
            except Exception:
                # Typed errors from child execution: annotate span +
                # propagate. Spec §14.7.2 step 10.
                span.set_attribute("subagent.result_status", "failed")
                span.set_attribute("subagent.request_blocked_by_budget", False)
                span.set_attribute("subagent.tokens_in", 0)
                span.set_attribute("subagent.tokens_out", 0)
                span.set_attribute("subagent.cached_tokens_in", 0)
                # Best-effort audit composition — dispatch fact at 8a is
                # preserved; downstream 8b/8c/8d failures are swallowed so
                # the child's original exception remains the primary fault.
                _ = self._compose_and_persist_audit(
                    parent_action_id=parent_action_id,
                    descent=descent,
                    payload=payload,
                    step_context=step_context,
                    raise_on_failure=False,
                )
                raise

            # --- Step 7: map child result → span (AC #8) -------------------
            if child_result.status == RunStatus.SUCCESS:
                span.set_attribute("subagent.result_status", "completed")
                span.set_attribute("subagent.request_blocked_by_budget", False)
                step_output: Mapping[str, Any] = dict(child_result.final_state or {})
            elif child_result.status == RunStatus.DRAINED:
                # Drain is operator-initiated (not failure) per §14.7.2 step 7
                span.set_attribute("subagent.result_status", "completed")
                span.set_attribute("subagent.request_blocked_by_budget", False)
                step_output = dict(child_result.partial_state or {})
            elif child_result.status == RunStatus.FAILED:
                span.set_attribute("subagent.result_status", "failed")
                span.set_attribute("subagent.request_blocked_by_budget", False)
                # Token counters set to 0 at v1.6 MVP (child does not surface
                # cost rollup through RunResult per C-CP-25 §25.2; deferred
                # to v1.7+ extension).
                span.set_attribute("subagent.tokens_in", 0)
                span.set_attribute("subagent.tokens_out", 0)
                span.set_attribute("subagent.cached_tokens_in", 0)
                # --- Step 8: best-effort audit (FAILED path) ----------------
                # v1.7 4-substep sequence runs best-effort on child FAILED;
                # the primary fault is SubAgentChildFailedError. Audit-write
                # failures are swallowed so the child failure remains the
                # surfaced error per spec §14.7.2 step 8 failure-semantics.
                _ = self._compose_and_persist_audit(
                    parent_action_id=parent_action_id,
                    descent=descent,
                    payload=payload,
                    step_context=step_context,
                    raise_on_failure=False,
                )
                raise SubAgentChildFailedError(
                    f"child sub-workflow {payload.child_workflow_id!r} "
                    f"terminated with RunStatus.FAILED; fail_class="
                    f"{child_result.fail_class!r}"
                )
            elif child_result.status == RunStatus.PAUSED:
                # B-HIERARCHICAL-PAUSE (R-FS-1) — the recursive child sub-workflow
                # itself PAUSED (a grandchild branch failed under cascade_policy=pause).
                # Previously this fell into the `else` below and was swallowed as
                # success-equivalent — silently discarding the child's suspended state.
                # Surface it as a TYPED exception carrying the child's PauseSnapshot so
                # the parent fan-out barrier captures the child's cursor + pauses
                # honestly (resume re-enters the child at that cursor, NOT re-dispatched
                # fresh). A PAUSED RunResult MUST carry its pause_snapshot (the §25.2
                # contract); if it does not, the child cannot be resumed → fail honestly
                # as a child failure (never a false-resumable / silent-success).
                span.set_attribute("subagent.result_status", "paused")
                span.set_attribute("subagent.request_blocked_by_budget", False)
                # The canonical subagent schema requires all 7 attributes; this branch
                # raises before the common close-time token block, so set the 3 token
                # attrs here (0 at v1.6 MVP — child does not surface a cost rollup
                # through RunResult), as the FAILED path does before its raise (Codex [P2]).
                span.set_attribute("subagent.tokens_in", 0)
                span.set_attribute("subagent.tokens_out", 0)
                span.set_attribute("subagent.cached_tokens_in", 0)
                # Best-effort audit BEFORE raising (Codex [P2]) — mirror the FAILED path
                # so a paused sub-agent dispatch leaves a CP/OD audit entry like every
                # other disposition (SUCCESS/DRAINED/FAILED). Downstream 8b/8c/8d failures
                # are swallowed (raise_on_failure=False) so the pause remains the surfaced
                # outcome.
                _ = self._compose_and_persist_audit(
                    parent_action_id=parent_action_id,
                    descent=descent,
                    payload=payload,
                    step_context=step_context,
                    raise_on_failure=False,
                )
                if child_result.pause_snapshot is None:
                    raise SubAgentChildFailedError(
                        f"child sub-workflow {payload.child_workflow_id!r} returned "
                        f"RunStatus.PAUSED with no pause_snapshot (cannot resume; "
                        f"§25.2 contract violation)"
                    )
                raise SubAgentChildPausedError(
                    child_workflow_id=payload.child_workflow_id,
                    child_snapshot=child_result.pause_snapshot,
                )
            else:
                # PARTIAL — reserved per C-CP-25 §25.2. v1.6 MVP treats as
                # success-equivalent (per spec §14.7.2 step 7 enumeration:
                # only SUCCESS / DRAINED / FAILED named).
                span.set_attribute("subagent.result_status", "completed")
                span.set_attribute("subagent.request_blocked_by_budget", False)
                step_output = dict(child_result.partial_state or {})

            # Close-time `subagent.*` token attributes (4 attrs; 0 at v1.6
            # MVP — child cost rollup not surfaced through RunResult per
            # C-CP-25 §25.2 v1.6 shape).
            span.set_attribute("subagent.tokens_in", 0)
            span.set_attribute("subagent.tokens_out", 0)
            span.set_attribute("subagent.cached_tokens_in", 0)

            # --- Step 8: 4-substep audit composition (AC #9, UN-STRUCK) ---
            # v1.7 §14.7.2 step 8: 8a compose CP → 8b F2-write dispatch →
            # 8c CP→OD convert → 8d audit_writer.append. Audit failures on
            # the SUCCESS / DRAINED path raise SubAgentDispatchAuditComposeError
            # → driver maps to RT-FAIL-SUB-AGENT-AUDIT-COMPOSE.
            try:
                _ = self._compose_and_persist_audit(
                    parent_action_id=parent_action_id,
                    descent=descent,
                    payload=payload,
                    step_context=step_context,
                    raise_on_failure=True,
                )
            except SubAgentDispatchAuditComposeError:
                span.set_attribute("subagent.result_status", "failed")
                raise

            # --- Step 9: return step output --------------------------------
            return step_output
