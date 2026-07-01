"""C-CP-26 PauseResumeProtocol type carriers — 2 enums + 2 envelope models.

U-CP-62 — first unit of cluster 10-CP-B. Declares the type carriers that the
C-CP-26 PauseResumeProtocol class body (U-CP-63 capture_pause_snapshot + U-CP-64
attempt_resume) and the pause/resume span emitter (U-CP-65) consume at runtime:

- `WorkflowPauseReason` — 6-class workflow-layer pause taxonomy (CP spec §26.2;
  renamed from `PauseReason` at v1.11 per path γ disambiguation; EFFECT_FENCE_AMBIGUOUS
  added for B-EFFECT-FENCE-HITL-ROUTE)
- `MaterialDiffPolicy` — 3-class material-diff resumption policy (STRICT default
  per Decision 2.D7)
- `PauseSnapshot` — 8-field pause-snapshot envelope with state-ledger-anchored
  snapshot-hash
- `ResumeResult` — 5-field resume-attempt outcome envelope

Member string values are cited verbatim from CP spec v1.11 §26.2. `PauseSnapshot`
+ `ResumeResult` use frozen Pydantic v2 models (matching the U-CP-58/U-CP-59
precedent at cluster 10-CP-A; the spec's `@dataclass(frozen=True)` declaration
maps to `BaseModel` + `ConfigDict(frozen=True, extra="forbid")` per repo
discipline).

**Naming note (path γ disambiguation, 2026-05-21).** `WorkflowPauseReason`
(workflow-layer) is distinct from the C-CP-22 §22.1 `PauseReason` (engine-layer
replay-pause taxonomy) homed at `harness_cp.pause_resume_protocol`. The two
enums occupy different architectural layers: C-CP-22 = engine-native pause +
replay-resumption mechanics (U-CP-49 surface); C-CP-26 = workflow-driver
explicit-pause + material-diff resumption mechanics. Per workspace
`.harness/class_1_fork_u_cp_63_pause_reason_collision.md` operator-ratified
path γ + CP spec v1.11 §26 NEW NOTE coexistence.

Authority: CP spec v1.11 §26.2 (NEW C-CP-26 PauseResumeProtocol; path γ
identifier rename absorbed); plan unit U-CP-62 (CP plan v2.17 §1).
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from harness_cp.handoff_context import StateSummary

if TYPE_CHECKING:
    from harness_cp.hitl_placement import HITLResult


class WorkflowPauseReason(StrEnum):
    """The 6-class workflow-layer pause reason (CP spec §26.2; the 5 v1.11 members
    + EFFECT_FENCE_AMBIGUOUS added for B-EFFECT-FENCE-HITL-ROUTE).

    Distinct from the engine-layer `PauseReason` at C-CP-22 §22.1 / U-CP-49.
    Per CP spec v1.11 §26 NEW NOTE: C-CP-22 anchors at engine-native pause +
    replay-resumption; C-CP-26 anchors at workflow-driver explicit-pause +
    material-diff resumption. The two protocols coexist as distinct
    architectural primitives at distinct layers.
    """

    EXPLICIT_OPERATOR = "explicit_operator"
    """Operator-initiated pause from outside the workflow loop."""

    HITL_PENDING = "hitl_pending"
    """HITL gate opened; workflow paused awaiting operator response."""

    VALIDATOR_ESCALATION = "validator_escalation"
    """Validator framework escalated to HITL; workflow paused for arbitration."""

    TIMEOUT_BOUNDARY = "timeout_boundary"
    """Step or workflow-layer timeout boundary crossed; system-triggered pause."""

    EXTERNAL_DEPENDENCY = "external_dependency"
    """External dependency unavailable (e.g., MCP server, LLM provider);
    system-triggered pause pending dependency recovery."""

    EFFECT_FENCE_AMBIGUOUS = "effect_fence_ambiguous"
    """Effect fence (runtime spec §14.22 C-RT-31) lost a reserve to a prior
    uncommitted attempt of a non-idempotent effect AND found no captured output
    proving completion (the crash fell in the fire→capture window). Whether the
    effect fired is ambiguous, so the runtime fails to the operator rather than
    auto-re-fire (at-most-once). System-triggered, driver-routed pause
    (B-EFFECT-FENCE-HITL-ROUTE; the runtime ``EffectFenceAmbiguousUncommittedError``
    name-matched at the §26-driver step-dispatch boundary)."""


class MaterialDiffPolicy(StrEnum):
    """The 3-class material-diff resumption policy (CP spec v1.11 §26.2).

    `STRICT` is the default per Decision 2.D7 RATIFIED — any diff aborts
    resumption. `LENIENT` permits resumption when only non-behavior-changing
    diffs are detected. `OPERATOR_ARBITRATE` escalates any diff to HITL.
    """

    STRICT = "strict"
    """Any diff aborts resumption (DEFAULT per Decision 2.D7)."""

    LENIENT = "lenient"
    """Only behavior-changing diffs abort resumption."""

    OPERATOR_ARBITRATE = "operator_arbitrate"
    """Any diff escalates to HITL for operator arbitration."""


class EffectFenceResolution(StrEnum):
    """The operator's resume-side resolution of a §26.2 `EFFECT_FENCE_AMBIGUOUS` pause.

    B-EFFECT-FENCE-PAUSE-RESOLUTION (R-FS-1) — the §14.22 C-RT-31 effect fence
    pauses (via `WorkflowPauseReason.EFFECT_FENCE_AMBIGUOUS`) when a re-dispatch
    lost the per-(run, step, tool) reserve AND no captured output proves the
    non-idempotent effect completed: whether the effect fired is genuinely
    ambiguous and the harness CANNOT compute the answer (the crash fell in the
    fire→capture window). The fence pauses *to ask the operator one question — did
    the effect fire?* These three resolutions are the operator ANSWERING with
    ground-truth the harness lacks (e.g. checking whether the email was sent / the
    git push landed). Delivered one-shot via `ResumeContext.effect_fence_resolution`
    on `api.resume`; key-bound to the paused effect via
    `PauseSnapshot.effect_fence_resume.idempotency_key`.

    Answering the fence's question is IN-DOMAIN — it COMPLETES the at-most-once
    decision the harness couldn't compute, it does NOT override the guarantee. A
    mis-assertion is operator-error responsibility (the C-AS-03 `idempotent` /
    `blast_radius_tier` mis-declaration posture).
    """

    SKIP_AS_FIRED = "skip_as_fired"
    """Operator asserts the effect FIRED (the prior attempt fired, then crashed
    before capturing its output). Proceed treating the step as complete — but the
    lost output is genuinely unrecoverable, so the step yields EMPTY output. NEVER
    re-fires the effect. Downstream consumers that needed the lost output fail
    honestly (the data is gone)."""

    RE_FIRE = "re_fire"
    """Operator asserts the effect did NOT fire (the prior attempt claimed the
    reserve, then crashed before firing). Clear the held claim and re-dispatch the
    step fresh — a FIRST-and-only execution, still at-most-once from the true state
    of the world. The operator supplies the ground-truth the fence couldn't compute."""

    ABORT = "abort"
    """Operator cannot determine whether the effect fired (or chooses not to
    proceed). Fail the run terminally (the conservative default — never re-fire,
    never proceed-with-empty)."""

    ABORT_BRANCH = "abort_branch"
    """B-FANOUT-EFFECT-FENCE-PER-BRANCH-SCOPED-ABORT (R-FS-1) — per-branch-SCOPED
    abort: fail just THIS fan-out branch (record it terminal, never re-fire) while
    the SIBLING branches the operator CAN vouch for (SKIP_AS_FIRED / RE_FIRE) resolve
    and FIRE, and the run folds the survivors per `cascade_policy`. Distinct from
    `ABORT`, which is RUN-level terminal (the whole run FAILS, all continue-resolutions
    suppressed — v1.65 §1(b), preserved byte-for-byte). `ABORT_BRANCH` is meaningful
    ONLY for a fan-out fence pause: it is consumed CP-side at the two fan-out resume sites
    (`_execute_parallelization` / `_execute_orchestrator_workers`) and NEVER threaded to the
    runtime fence (the at-most-once guarantee — the scoped-abort branch is never re-dispatched,
    so its ambiguous effect is never re-fired). A LINEAR fence pause has exactly one branch
    (scoping is vacuous); the runtime fence gate recognizes only SKIP_AS_FIRED / RE_FIRE /
    ABORT, so an `ABORT_BRANCH` supplied for a LINEAR pause is unrecognized → it falls through
    to the default no-resolution fence behavior (suppress-if-captured / else INERT re-pause via
    the decline-mirror, NEVER an auto-action) — use `ABORT` for a linear run-terminal abort. On
    a fan-out pause the scoped-abort branch's output is discarded (a degraded non-contributor)
    → the run folds to PARTIAL with the surviving branches (FAILED if NO survivor)."""


class FanOutBranchResumeState(BaseModel):
    """Per-branch terminal disposition + recovered output for a paused fan-out.

    B-FANOUT-PAUSE (R-FS-1) — one row per fan-out worker branch that reached a
    terminal disposition before the `cascade_policy=pause` halt. A branch absent
    from `FanOutResumeState.branches` is **left re-dispatchable** (the §25.15.1
    pause semantic: "in-flight finish; not-yet-dispatched left re-dispatchable")
    — `api.resume` re-dispatches it. A branch present here MUST NOT be
    re-dispatched (§25.15.2 obligation 7: a `completed`/`timed_out`/`cancelled`
    branch is terminal); its `output` is recovered into the resumed aggregate.

    The terminal_status mirrors the persisted Route-Y `branch_metadata.terminal_status`
    (§25.13): `completed` = the branch's dispatch ran (effect may have landed —
    incl. a ran-and-errored worker, dispatch-boundary semantic per obligation 4);
    `timed_out` = the barrier deadline cut an in-flight branch.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    branch_index: int
    """The fan-out branch ordinal (0-based). For `ORCHESTRATOR_WORKERS` this is the
    worker `steps[1:]` position; for `PARALLELIZATION` (B-FANOUT-PAUSE-PARALLELIZATION,
    a peer fan-out with NO orchestrator `steps[0]`) it is the `steps` position
    directly."""

    step_id: str
    """The branch's `WorkflowStep.step_id` AT CAPTURE TIME. Resume validates the
    re-supplied branch step's `step_id` against this (identity, not just the
    declared branch count) so a same-count body change (a branch rename / reorder)
    fails closed rather than silently attributing recovered output to the wrong
    step. (Full anchor-reachability material-diff is the deferred U-CP-22 arc;
    this is the cheap positional-identity guard.) Strategy-neutral: the orchestrator
    fan-out re-derives it from `worker_steps[branch_index]`, the peer fan-out from
    `steps[branch_index]`."""

    terminal_status: str
    """The persisted branch disposition: `completed` | `timed_out` (the
    discriminating Route-Y `terminal_status`, §25.15.2 obligation 4)."""

    output: Mapping[str, Any] | None = None
    """The completed branch's dispatch output, recovered into the resumed
    aggregate. `None` for a branch that ran-and-errored or timed out (it
    contributed nothing to the original aggregate — preserved as terminal so
    obligation 7 does not re-dispatch its possibly-landed effect)."""


class FanOutResumeState(BaseModel):
    """Fan-out resume reconstruction state carried by a paused-fan-out PauseSnapshot.

    B-FANOUT-PAUSE (R-FS-1) — the self-contained, hash-integrity-checked resume
    source for a `cascade_policy=pause` fan-out halt. Materializes the §25.15.1
    `pause → PAUSED` row's "composes with C-CP-26 PauseResumeProtocol + C-RT-35
    `api.resume`" promise for the fan-out case, which position-only resume cannot
    represent (a fan-out paused at the worker barrier has no single `step_index`
    capturing which branches completed vs. need re-dispatch — `adversarial-review-
    r-fs-1-arc-14` F1-01).

    This is the materialization of the R-CC-1 design §1.1 re-open trigger ("a
    future execution model … would need a state-restoration story + a durable
    store carrying more than the [position-only] PauseSnapshot"): the completed
    branches' OUTPUTS do not survive in the ledger (it carries causality +
    `terminal_status`, not the dispatch output mapping), so they are carried here
    and `_compute_snapshot_hash` COVERS this field — a resumed aggregate trusts
    these recovered outputs, so they are integrity-checked, not a silent-tamper gap.

    Satisfies §25.15.2 obligation 7 ("`api.resume` reads each branch's persisted
    `terminal_status` and MUST NOT re-dispatch a `cancelled`/`completed`/`timed_out`
    branch"): `branches` IS that persisted per-branch terminal disposition.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    orchestrator_output: Mapping[str, Any]
    """The orchestrator step's (`steps[0]`) output, recovered on resume so the
    already-run orchestrator is NOT re-dispatched (it is a completed step; effect
    may have landed)."""

    orchestrator_step_id: str
    """The orchestrator step's (`steps[0]`) `step_id` AT CAPTURE TIME. Resume
    validates the re-supplied `steps[0].step_id` against this — the orchestrator
    output is recovered + its dispatch skipped, so a same-count body change that
    renames/reorders `steps[0]` would otherwise apply stale orchestrator output to
    a different body (Codex [P2]). Fail-closed on mismatch."""

    branches: tuple[FanOutBranchResumeState, ...]
    """The terminal branches (completed / timed_out) at pause time. A worker
    branch ordinal absent from this tuple is left re-dispatchable."""

    worker_count: int
    """The total declared worker count (`len(steps[1:])`) at pause time — bounds
    the re-dispatchable set (any ordinal in `range(worker_count)` not present in
    `branches` is re-dispatched). A material-diff guard at resume: a different
    `worker_count` means the workflow body changed."""

    paused_child_branches: tuple[PausedChildBranchResumeState, ...] = ()
    """B-HIERARCHICAL-PAUSE (R-FS-1) — worker branches whose recursive child
    sub-workflow itself returned `RunStatus.PAUSED` (a grandchild paused under
    `cascade_policy=pause`; HIERARCHICAL_DELEGATION reuses ORCHESTRATOR_WORKERS at
    each level, so a `SUB_AGENT_DISPATCH` worker can recurse + pause). DISTINCT
    from `branches` (terminal — MUST NOT re-dispatch) and from absent ordinals
    (re-dispatch FRESH): a paused-child branch is the THIRD disposition — re-entered
    on resume via the child's OWN `api.resume(child_snapshot)` so the grandchild's
    already-completed steps are NOT re-executed (re-dispatching it fresh would lose
    that work — `[[full-chain-witness-not-half-proofs]]`). Each row's
    `child_snapshot` (a full nested `PauseSnapshot`) is COVERED by
    `_compute_snapshot_hash` transitively: it lives inside `fan_out_resume`, whose
    `model_dump(mode="json")` the hash already serializes recursively, so a tampered
    grandchild cursor fails the parent resume recompute. Additive, default-empty:
    `_compute_snapshot_hash` DROPS this field from the canonical serialization when
    empty, so every pre-existing ORCHESTRATOR_WORKERS / pre-B-HIERARCHICAL-PAUSE
    snapshot hashes byte-identically (an old durable snapshot's dict, lacking this
    key, deserializes via the default + re-hashes unchanged). A worker ordinal here
    MUST NOT also appear in `branches` (the resume material-diff guard enforces no
    overlap — terminal vs paused-child are disjoint dispositions)."""

    synthesis_step_id: str | None = None
    """B-FANOUT-PAUSE-SYNTHESIS (R-FS-1) — the terminal `POST_JOIN_SYNTHESIS` step's
    `step_id` AT PAUSE-CAPTURE TIME, or `None` when the fan-out carried no opt-in
    synthesis. The captured synthesis IDENTITY (presence + step_id). On a pause the
    synthesis NEVER ran (the pause halts at the worker barrier, BEFORE the post-join
    synthesis), so there is nothing to replay — but resume MUST material-diff the
    re-supplied terminal synthesis step against this identity (synthesis added /
    removed / changed `step_id` → fail closed) BEFORE fresh-dispatching it on the
    recovered + re-dispatched branches (effect-free, first-and-only per B-POSTJOIN).
    Additive, default-None: `_compute_snapshot_hash` DROPS this field from the
    canonical serialization when None, so every pre-existing / non-synthesis
    ORCHESTRATOR_WORKERS snapshot hashes byte-identically (an old durable snapshot's
    dict, lacking this key, deserializes via the default + re-hashes unchanged —
    the same `paused_child_branches` drop-when-empty discipline)."""

    effect_fence_paused_branches: tuple[EffectFencePausedBranchResumeState, ...] = ()
    """B-FANOUT-EFFECT-FENCE-BRANCH-PAUSE (R-FS-1) — branches whose own dispatch raised the
    runtime effect fence's `EffectFenceAmbiguousUncommittedError` (C-RT-31 §14.22). DISTINCT from
    `branches` (terminal — MUST NOT re-dispatch), from absent ordinals (re-dispatch FRESH), and
    from `paused_child_branches` (a SUB_AGENT child sub-workflow paused): an effect-fence-paused
    branch is re-entered on resume via the fence-keyed `EffectFenceResolution` (SKIP_AS_FIRED /
    RE_FIRE / ABORT), NOT a fresh dispatch — the fan-out analogue of the LINEAR-path
    B-EFFECT-FENCE-HITL-ROUTE. Additive, default-empty: `_compute_snapshot_hash` DROPS this field
    from the canonical serialization when empty, so every pre-existing snapshot hashes
    byte-identically (the `paused_child_branches` drop-when-empty discipline)."""


class PeerFanOutResumeState(BaseModel):
    """Peer fan-out (PARALLELIZATION) resume reconstruction state.

    B-FANOUT-PAUSE-PARALLELIZATION (R-FS-1) — the `PARALLELIZATION`-shaped sibling
    of `FanOutResumeState`. PARALLELIZATION is a PEER fan-out: every declared
    `WorkflowStep` is a branch (`branch_index = steps` ordinal), with NO orchestrator
    `steps[0]`. So this carrier has NO `orchestrator_output` / `orchestrator_step_id`
    (the illegal-state-unrepresentable choice: a peer fan-out has no orchestrator,
    so the orchestrator-bearing `FanOutResumeState` is NOT reused — its required
    orchestrator fields would be vacuous here, and loosening them to optional would
    make `orchestrator_output=None` representable for an ORCHESTRATOR_WORKERS snapshot,
    an illegal state for that strategy). `branches` + `branch_count` are the peer
    analogues of `FanOutResumeState.branches` + `worker_count`.

    Carried by `PauseSnapshot.peer_fan_out_resume` (the second additive, defaulted
    field — never co-set with `fan_out_resume`; the strategy that captured the pause
    selects which is populated). Materializes the §25.15.1 `pause → PAUSED` row for
    PARALLELIZATION + the §25.15.2 obligation-7 ledger-based resume reconstruction,
    exactly as `FanOutResumeState` does for ORCHESTRATOR_WORKERS (CP spec v1.44 §1).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    branches: tuple[FanOutBranchResumeState, ...]
    """The terminal branches (completed / timed_out) at pause time. A branch ordinal
    absent from this tuple is left re-dispatchable (the §25.15.1 pause semantic)."""

    branch_count: int
    """The total declared branch count (`len(steps)`) at pause time — bounds the
    re-dispatchable set (any ordinal in `range(branch_count)` not present in
    `branches` is re-dispatched). A material-diff guard at resume: a different
    `branch_count` means the workflow body changed."""

    synthesis_step_id: str | None = None
    """B-FANOUT-PAUSE-SYNTHESIS (R-FS-1) — the PARALLELIZATION analogue of
    `FanOutResumeState.synthesis_step_id`: the terminal `POST_JOIN_SYNTHESIS` step's
    `step_id` at pause-capture time, `None` when no synthesis was opted in. Same
    material-diff-on-resume + fresh-dispatch contract; same additive, default-None,
    drop-from-hash-when-None byte-compat discipline (the `_compute_snapshot_hash` peer
    drop mirrors the FanOut drop — `PeerFanOutResumeState` had no drop before this
    field, so the drop is ADDED at the same site)."""

    effect_fence_paused_branches: tuple[EffectFencePausedBranchResumeState, ...] = ()
    """B-FANOUT-EFFECT-FENCE-BRANCH-PAUSE (R-FS-1) — the PARALLELIZATION analogue of
    `FanOutResumeState.effect_fence_paused_branches`: peer branches whose own dispatch raised the
    runtime effect fence's `EffectFenceAmbiguousUncommittedError` (C-RT-31 §14.22). Re-entered on
    resume via the fence-keyed `EffectFenceResolution`, NOT a fresh dispatch. Additive,
    default-empty, dropped-from-hash-when-empty (same discipline as `synthesis_step_id`)."""


class HandoffStageResumeState(BaseModel):
    """One completed stage of a `DECENTRALIZED_HANDOFF` paused at a later stage.

    B-HANDOFF-PAUSE (R-FS-1) — `DECENTRALIZED_HANDOFF` is single-owner SEQUENTIAL
    (one stage-expert owns the workflow at a time, then hands off to the next via a
    `HandoffContext` record). When a stage fails under `cascade_policy=pause`, the
    completed-stage PREFIX is captured so resume recovers their outputs (the ledger
    carries causality, not the dispatch output mapping) WITHOUT re-executing them.

    Distinct from the fan-out carriers' `FanOutBranchResumeState`: handoff stages are
    a CONTIGUOUS sequential prefix (`stage_index` 0..k-1 for a pause at stage k), not a
    set of terminal branches with re-dispatchable gaps. There is no `terminal_status`
    (a handoff stage either completed — recovered here — or is the failed/not-yet-run
    stage at/after the cursor, re-dispatched on resume)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage_index: int
    """The declared stage ordinal (0-based `steps` position) that completed. The
    captured prefix is contiguous `0..k-1` for a pause at stage `k`."""

    step_id: str
    """The stage `WorkflowStep.step_id` AT CAPTURE TIME. Resume validates the
    re-supplied `steps[stage_index].step_id` against this (positional-identity guard,
    fail-closed on a same-count rename/reorder — the recovered output is replayed into
    this stage's slot + its dispatch skipped, so a renamed body would mis-attribute)."""

    output: Mapping[str, Any]
    """The completed stage's dispatch output, recovered on resume so the stage is NOT
    re-dispatched (it is a completed step; effect may have landed) — replayed into the
    aggregate `stages` map + re-seeded into the inter-step output channel so the next
    stage reads its upstream context (B-INTERSTEP-HANDOFF, runtime §14.21). COVERED by
    `_compute_snapshot_hash` (a resumed aggregate trusts it → integrity-checked)."""


class HandoffResumeState(BaseModel):
    """Single-owner sequential handoff resume reconstruction state (the stage cursor).

    B-HANDOFF-PAUSE (R-FS-1) — the `DECENTRALIZED_HANDOFF` analogue of
    `FanOutResumeState` / `PeerFanOutResumeState`, but a STAGE CURSOR rather than a
    branch set: handoff is single-owner sequential, so a pause at stage `k` has a
    contiguous completed prefix `0..k-1` and re-dispatches from stage `k` onward.

    Materializes the §25.15.1 `pause → PAUSED` row EXTENDED to the single-owner
    sequential case (the §25.15.1 row text is fan-out-barrier-scoped; this extension
    is the §25.18-named `DECENTRALIZED_HANDOFF` impl-discretion materialization — the
    last/hardest strategy in the §25.18 simplest→hardest order). No new orchestrator
    fields (no `steps[0]` orchestrator; no peer-branch set): just the completed-stage
    prefix + the declared stage count.

    Carried by `PauseSnapshot.handoff_resume` (the THIRD additive, defaulted resume
    field — never co-set with `fan_out_resume` / `peer_fan_out_resume`; the capturing
    strategy populates exactly one). On resume, `_execute_decentralized_handoff`
    re-walks the body: the recovered prefix's outputs are replayed (NOT re-dispatched),
    the handoff-chain `parent_action_id` is recomputed deterministically through the
    prefix (so the resumed stage chains off the last completed stage's `action_id`, NOT
    re-anchored to the workflow origin — the load-bearing handoff causality), and stage
    `k` onward is dispatched fresh."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    completed_stages: tuple[HandoffStageResumeState, ...]
    """The contiguous completed-stage prefix (`stage_index` 0..k-1) at pause time. A
    stage ordinal at/after `len(completed_stages)` is re-dispatched on resume. A RE-pause
    (pause→resume→pause) unions the recovered prefix + the newly-completed-on-resume
    stages, so this stays a contiguous prefix across repeated resumes."""

    stage_count: int
    """The total declared stage count (`len(steps)`) at pause time. A material-diff
    guard at resume: a different `stage_count` means the workflow body changed →
    fail-closed rather than recover stale outputs into a changed body."""


class EvaluatorOptimizerStepResumeState(BaseModel):
    """One completed generate-or-evaluate step of an `EVALUATOR_OPTIMIZER` loop paused later.

    B-FANOUT-PAUSE-EVALUATOR-OPTIMIZER (R-FS-1) — `EVALUATOR_OPTIMIZER` is a SEQUENTIAL
    generate→evaluate→regenerate loop (NO fan-out, NO branches, NO `branch_metadata`),
    bounded by a max-iteration cap. When a generate/evaluate dispatch fails under
    `cascade_policy=pause`, the contiguous completed-STEP prefix is captured so resume
    recovers each step's output WITHOUT re-dispatching it (a completed step's effect may
    have landed).

    The #681 `HandoffStageResumeState` analogue, but the cursor unit is a LOOP STEP
    (generate or evaluate) keyed by the MONOTONIC `entry_index` (the ledger row index),
    not a declared stage ordinal: the EO loop re-dispatches the SAME two declared steps
    across iterations, so the resume cursor is entry-granular. The iteration semantics
    (which iteration, the cap) DERIVE from `entry_index` parity (even ⟹ generate, odd ⟹
    evaluate; iteration = entry_index // 2) — no separate iteration field is stored."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entry_index: int
    """The MONOTONIC ledger-row index (0,1,2,3,…) of this completed loop step. The
    captured prefix is contiguous `0..m-1` for a pause at the failed step `m`. Even ⟹ a
    generate dispatch, odd ⟹ an evaluate dispatch (the loop alternates strictly)."""

    declared_step_index: int
    """The DECLARED step ordinal (0=generate, 1=evaluate) this entry dispatched. Resume
    validates `declared_step_index == entry_index % 2` (loop-alternation coherence) and
    `steps[declared_step_index].step_id == step_id` (positional-identity guard)."""

    step_id: str
    """The `WorkflowStep.step_id` AT CAPTURE TIME (`steps[0]`=generate / `steps[1]`=evaluate).
    Resume validates the re-supplied `steps[declared_step_index].step_id` against this —
    fail-closed on a body rename so a recovered output is never replayed into a renamed
    step's slot (the recovered step's dispatch is skipped on resume)."""

    output: Mapping[str, Any]
    """This completed step's dispatch output, recovered on resume so the step is NOT
    re-dispatched. Replayed into the inter-step output channel (so the next live step
    reads its upstream draft/feedback — B-INTERSTEP, runtime §14.21) and into
    `last_generate_output` / `last_evaluation` for the SUCCESS final_state. COVERED by
    `_compute_snapshot_hash` (a resumed loop trusts it → integrity-checked)."""


class EvaluatorOptimizerResumeState(BaseModel):
    """Sequential generate→evaluate loop resume reconstruction state (the iteration cursor).

    B-FANOUT-PAUSE-EVALUATOR-OPTIMIZER (R-FS-1) — the `EVALUATOR_OPTIMIZER` analogue of
    `HandoffResumeState`: a single-owner SEQUENTIAL cursor (no peer-branch set), but over
    the loop's completed STEPS rather than a stage list. A pause at the failed step `m`
    has a contiguous completed-step prefix `0..m-1`; resume recovers their outputs and
    re-dispatches from step `m` onward, honoring the original max-iteration cap across the
    resume boundary (the cap is reconstructed from the recovered generate count — every
    iteration has exactly one generate).

    Materializes the §25.15.1 `pause → PAUSED` row EXTENDED to the sequential
    `EVALUATOR_OPTIMIZER` case (the §25.15.1 row text is fan-out-barrier-scoped; this
    extension is the §25.18-named `EVALUATOR_OPTIMIZER` impl-discretion materialization,
    mirroring the #681 `DECENTRALIZED_HANDOFF` extension). Only `cascade_policy=pause`
    (TEAM tier, with a bound `pause_resume_protocol`) is materialized; `proceed` /
    `cascade-cancel` retain EO's existing terminal-FAILED disposition.

    Carried by `PauseSnapshot.evaluator_optimizer_resume` (the FOURTH additive, defaulted
    resume field — never co-set with `fan_out_resume` / `peer_fan_out_resume` /
    `handoff_resume`; the capturing strategy populates exactly one)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    completed_steps: tuple[EvaluatorOptimizerStepResumeState, ...]
    """The contiguous completed-step prefix (`entry_index` 0..m-1) at pause time. A step
    at/after `len(completed_steps)` is re-dispatched on resume. A RE-pause unions the
    recovered prefix + the newly-completed-on-resume steps, so this stays a contiguous
    prefix across repeated resumes. The recovered-generate count reconstructs the
    iteration cap across the resume boundary; recovered evaluations are all non-accepts by
    construction (an accept would have terminated the loop SUCCESS, not paused)."""


class EffectFenceResumeState(BaseModel):
    """Effect-fence ambiguous-pause resume reconstruction state (the held claim key).

    B-EFFECT-FENCE-PAUSE-RESOLUTION (R-FS-1) — the linear/TOOL_STEP analogue of the
    four fan-out resume carriers, present ONLY when this snapshot captures a §14.22
    C-RT-31 effect-fence `WorkflowPauseReason.EFFECT_FENCE_AMBIGUOUS` pause. Unlike
    the fan-out carriers (which recover completed-branch/step OUTPUTS), the effect
    fence's pause has NO recoverable output by definition (the ambiguity is precisely
    that no output was captured); the only state to carry is the per-(run, step, tool)
    `idempotency_key` of the held reserve, so the resumed dispatch can KEY-BIND the
    operator's resolution to the exact paused effect (apply it only when the recomputed
    dispatch key matches). NEVER co-set with `fan_out_resume` / `peer_fan_out_resume` /
    `handoff_resume` / `evaluator_optimizer_resume` (a fence pause is linear/TOOL_STEP)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    idempotency_key: str
    """The per-(run, step, tool) `idempotency_key` of the reserve the paused dispatch
    lost (the §14.22 fence claim key). Carried so `api.resume` key-binds the operator's
    `EffectFenceResolution` to THIS effect: the resumed dispatch applies the resolution
    ONLY when its recomputed key matches this value, then consumes it. COVERED by
    `_compute_snapshot_hash` (a resumed resolution trusts it → integrity-checked)."""


class OrchestratorEffectFencePausedResumeState(BaseModel):
    """A fan-out ORCHESTRATOR (`steps[0]`) whose OWN sequential dispatch raised the runtime
    effect fence's `EffectFenceAmbiguousUncommittedError` (C-RT-31 §14.22) — the fence lost a
    reserve to a prior uncommitted attempt of a non-idempotent effect AND found no captured
    output proving completion, so whether the orchestrator's effect fired is genuinely ambiguous.

    B-FANOUT-CRASH-RESUME-ORCHESTRATOR-MAYBE-RAN-EFFECT-BEARING (R-FS-1) — the ORCHESTRATOR
    analogue of the WORKER `EffectFencePausedBranchResumeState`. The orchestrator runs FIRST +
    sequentially, BEFORE any worker and BEFORE its own output capture, so when ITS dispatch
    fence-pauses there is no `FanOutResumeState` to carry (no branch ran, no orchestrator output
    exists — that absence IS the ambiguity); this is the FIRST-step analogue of the LINEAR-path
    `EffectFenceResumeState`, not a partial fan-out. Carried on `PauseSnapshot
    .orchestrator_effect_fence_resume` (a 6th top-level resume carrier, NEVER co-set with the
    five others), populated by ORCHESTRATOR_WORKERS / HIERARCHICAL_DELEGATION. On resume the
    orchestrator is RE-DISPATCHED with the operator's `EffectFenceResolution` key-bound to its
    reserve (NOT skipped like a recovered orchestrator, NOT fresh like a pre-arc run) and the
    workers then fan out fresh (none ran). The resolution palette is RE_FIRE / ABORT:
    SKIP_AS_FIRED is REJECTED at the resume site (an orchestrator's empty output would
    silently structure a degenerate fan-out aggregate — fail loud, never silently
    under-execute; the no-silent-failure discipline).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    idempotency_key: str
    """The held effect-fence reserve's `idempotency_key` (read by name off the runtime
    `EffectFenceAmbiguousUncommittedError`, since harness-cp cannot import harness-runtime), so
    `api.resume` key-binds the operator's `EffectFenceResolution` to THIS orchestrator's effect —
    the same keying discipline the LINEAR path's `EffectFenceResumeState` + the worker
    `EffectFencePausedBranchResumeState` use. On resume the orchestrator is re-dispatched with an
    `EffectFenceResolutionDirective(resolution=..., idempotency_key=...)` threaded on its
    `StepExecutionContext.effect_fence_resolution`; the runtime tool / managed-agents dispatcher
    consumes the matching directive (RE_FIRE → clear reserve + re-dispatch, ABORT → fail). Absent
    the key (defensive) → resume re-pauses (INERT, never an auto-re-fire)."""

    step_id: str
    """The orchestrator `WorkflowStep.step_id` AT CAPTURE TIME. Resume validates the re-supplied
    `steps[0].step_id` against this (positional-identity guard, fail-closed on a same-count
    rename/reorder — the same cheap guard `FanOutResumeState.orchestrator_step_id` /
    `EffectFencePausedBranchResumeState.step_id` apply)."""

    step_kind: str
    """The orchestrator `WorkflowStep.step_kind` value AT CAPTURE TIME (`tool-step` or
    `managed-agents` in production — the two fence-recoverable orchestrator kinds). Resume
    validates the re-supplied `steps[0].step_kind` against this (the changed-kind guard): if the
    operator kept the `step_id` but changed the kind away from the captured one, threading the
    `EffectFenceResolution` would reach NO fence (or a DIFFERENT sink) → the original ambiguous
    effect would be silently abandoned. Fail closed — the orchestrator analogue of the worker
    `EffectFencePausedBranchResumeState.step_kind` changed-kind guard (out-of-family Codex [P1])."""


class EffectFenceResolutionDirective(BaseModel):
    """The key-bound resolution the driver threads to the resumed dispatch.

    B-EFFECT-FENCE-PAUSE-RESOLUTION (R-FS-1) — pairs the operator's
    `EffectFenceResolution` (from `ResumeContext.effect_fence_resolution`) with the
    `idempotency_key` it is bound to (from `PauseSnapshot.effect_fence_resume`), so the
    resolution and its target travel together (illegal-state-unrepresentable: a
    resolution without its key cannot exist). Set by the CP driver on the resumed
    linear step's `StepExecutionContext.effect_fence_resolution` (hash-inert); read by
    the runtime tool dispatcher at the §14.22 fence gate, which applies it ONLY when the
    recomputed dispatch key equals `idempotency_key` (the key-bind), then it is naturally
    one-shot (set on the resumed step's context only)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    resolution: EffectFenceResolution
    """The operator's resume-side resolution (skip-as-fired / re-fire / abort)."""

    idempotency_key: str
    """The per-(run, step, tool) key this resolution is bound to. The dispatcher applies
    the resolution only when its recomputed key matches this (key-bind)."""


class PauseSnapshot(BaseModel):
    """8-field pause-snapshot envelope (CP spec v1.11 §26.2).

    Captures the pause-point state digest plus the state-ledger anchor and
    a canonical-serialization sha256 snapshot hash. Frozen after capture per
    §26.6 invariant 1; resume must validate `snapshot_hash` per invariant 2.

    The `state_ledger_anchor` carries the C-IS-05 §5 `entry_hash` at the
    pause point; material-diff detection at U-CP-64 checks whether this
    anchor remains reachable from the current entry chain (§26.6 invariant 3).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_id: str
    """Workflow identifier owning this pause."""

    run_id: str
    """Run identifier owning this pause."""

    step_index: int
    """Step index at which pause was captured."""

    pause_reason: WorkflowPauseReason
    """Why the workflow paused (5-class enum per §26.2)."""

    state_summary: StateSummary
    """Across-turn state digest at pause point (Pattern-D inherited from CP
    plan v2.9 + C-CP-13 §13.4)."""

    snapshot_hash: str
    """sha256 hex string (64 chars) over canonical serialization of
    (workflow_id + run_id + step_index + state_summary)."""

    created_at: int
    """Epoch ms at snapshot capture."""

    state_ledger_anchor: str
    """C-IS-05 §5 `entry_hash` at pause point. Material-diff detection at
    U-CP-64 checks reachability from current entry chain."""

    fan_out_resume: FanOutResumeState | None = None
    """B-FANOUT-PAUSE (R-FS-1) — fan-out resume reconstruction state, present
    ONLY when this snapshot captures a `cascade_policy=pause` fan-out halt
    (`None` for every linear / single-step pause — additive, default-None, so
    existing 8-field snapshots are byte-unchanged and still validate).

    When present, `_compute_snapshot_hash` COVERS it (the resumed aggregate
    trusts the recovered completed-branch outputs → integrity-checked, no
    silent-tamper gap), and `api.resume` re-enters the fan-out strategy with it:
    terminal branches are skipped (outputs recovered), absent branch ordinals
    re-dispatched (§25.15.2 obligation 7)."""

    peer_fan_out_resume: PeerFanOutResumeState | None = None
    """B-FANOUT-PAUSE-PARALLELIZATION (R-FS-1) — the `PARALLELIZATION` (peer fan-out)
    analogue of `fan_out_resume`, present ONLY when this snapshot captures a
    `PARALLELIZATION` `cascade_policy=pause` halt (`None` otherwise — additive,
    default-None, so every existing snapshot is byte-unchanged). NEVER co-set with
    `fan_out_resume`: the strategy that captured the pause populates exactly one (an
    ORCHESTRATOR_WORKERS pause sets `fan_out_resume`; a PARALLELIZATION pause sets
    this). COVERED by `_compute_snapshot_hash` when present (same integrity contract
    as `fan_out_resume`); `api.resume` re-enters `_execute_parallelization` with it
    (terminal branches skipped, outputs recovered; absent ordinals re-dispatched)."""

    handoff_resume: HandoffResumeState | None = None
    """B-HANDOFF-PAUSE (R-FS-1) — the `DECENTRALIZED_HANDOFF` (single-owner sequential)
    analogue of `fan_out_resume` / `peer_fan_out_resume`, present ONLY when this snapshot
    captures a `DECENTRALIZED_HANDOFF` `cascade_policy=pause` halt (`None` otherwise —
    additive, default-None, so every existing snapshot is byte-unchanged). NEVER co-set
    with `fan_out_resume` / `peer_fan_out_resume`: the strategy that captured the pause
    populates exactly one (a handoff pause sets this). COVERED by `_compute_snapshot_hash`
    when present (same integrity contract); `api.resume` re-enters
    `_execute_decentralized_handoff` with it (the completed-stage prefix's outputs
    recovered + their dispatch skipped; stage `k` onward re-dispatched, the handoff chain
    recomputed through the prefix)."""

    evaluator_optimizer_resume: EvaluatorOptimizerResumeState | None = None
    """B-FANOUT-PAUSE-EVALUATOR-OPTIMIZER (R-FS-1) — the `EVALUATOR_OPTIMIZER`
    (single-owner sequential generate→evaluate loop) analogue of `fan_out_resume` /
    `peer_fan_out_resume` / `handoff_resume`, present ONLY when this snapshot captures an
    `EVALUATOR_OPTIMIZER` `cascade_policy=pause` halt (`None` otherwise — additive,
    default-None, so every existing snapshot is byte-unchanged). NEVER co-set with the
    other three resume carriers: the strategy that captured the pause populates exactly
    one (an EO pause sets this). COVERED by `_compute_snapshot_hash` when present (same
    integrity contract); `api.resume` re-enters `_execute_evaluator_optimizer` with it
    (the completed-step prefix's outputs recovered + their dispatch skipped; the loop
    re-dispatches from the failed step onward, honoring the original iteration cap)."""

    effect_fence_resume: EffectFenceResumeState | None = None
    """B-EFFECT-FENCE-PAUSE-RESOLUTION (R-FS-1) — the §14.22 C-RT-31 effect-fence
    analogue of the four fan-out resume carriers, present ONLY when this snapshot
    captures a `WorkflowPauseReason.EFFECT_FENCE_AMBIGUOUS` pause (`None` otherwise —
    additive, default-None, so every existing snapshot is byte-unchanged). NEVER co-set
    with the four fan-out carriers: a fence pause is linear/TOOL_STEP, the fan-out
    strategies populate exactly one of the others. Carries the held reserve's
    `idempotency_key` (no recoverable output — that absence IS the ambiguity); COVERED
    by `_compute_snapshot_hash` when present (same integrity contract). `api.resume`
    key-binds the operator's `ResumeContext.effect_fence_resolution` to it (skip-as-fired
    → empty-output proceed / re-fire → clear the claim + fresh dispatch / abort →
    FAILED)."""

    orchestrator_effect_fence_resume: OrchestratorEffectFencePausedResumeState | None = None
    """B-FANOUT-CRASH-RESUME-ORCHESTRATOR-MAYBE-RAN-EFFECT-BEARING (R-FS-1) — the 6th top-level
    resume carrier, present ONLY when an ORCHESTRATOR_WORKERS / HIERARCHICAL_DELEGATION fan-out's
    OWN orchestrator (`steps[0]`) of a fence-recoverable kind (TOOL_STEP / MANAGED_AGENTS)
    raised the §14.22 effect fence at its sequential dispatch — BEFORE any worker, BEFORE its
    output capture (`None` otherwise — additive, default-None, so every existing snapshot is
    byte-unchanged). NEVER co-set with the four fan-out carriers OR `effect_fence_resume`: the
    orchestrator fence pause is the FIRST-step analogue of the linear `effect_fence_resume`, but
    captured + resumed by the orchestrator-workers strategy (so it carries `step_id`/`step_kind`
    for the changed-orchestrator guard, which the key-only linear carrier does not). COVERED by
    `_compute_snapshot_hash` when present (same integrity contract); when nested inside a
    HIERARCHICAL `paused_child_branches[].child_snapshot` it is dropped-when-None by
    `_strip_default_fanout_resume_fields` (byte-compat with pre-arc nested snapshots). `api.resume`
    key-binds the operator's `ResumeContext.effect_fence_resolution` to it and re-dispatches the
    orchestrator (RE_FIRE → clear + fresh dispatch / ABORT → FAILED; SKIP_AS_FIRED rejected)."""


class PausedChildBranchResumeState(BaseModel):
    """A worker branch whose recursive child sub-workflow returned `RunStatus.PAUSED`.

    B-HIERARCHICAL-PAUSE (R-FS-1) — HIERARCHICAL_DELEGATION reuses ORCHESTRATOR_WORKERS
    at each recursion level (`workflow_driver._execute_hierarchical_delegation`), so a
    `SUB_AGENT_DISPATCH` worker can re-enter the driver for a child sub-workflow that
    itself pauses (a grandchild branch failing under `cascade_policy=pause`). That
    child PAUSE — previously swallowed as success-equivalent at the sub-agent dispatch
    boundary — is now surfaced + captured here so the parent fan-out pauses honestly
    and `api.resume` re-enters the child at its own cursor.

    Carried by `FanOutResumeState.paused_child_branches` (NOT `branches`: a terminal
    branch MUST NOT be re-dispatched, but a paused-child branch MUST be — via the
    child's own resume, not a fresh dispatch — the illegal-states-unrepresentable
    split that keeps the two dispositions type-distinct, mirroring the
    `FanOutResumeState` vs `PeerFanOutResumeState` choice at #679).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    branch_index: int
    """The fan-out worker ordinal (0-based, the `steps[1:]` position) whose child
    sub-workflow paused. On resume this ordinal is re-dispatched THROUGH the child's
    own resume (NOT skipped like a terminal branch, NOT fresh like an absent one)."""

    step_id: str
    """The worker `WorkflowStep.step_id` AT CAPTURE TIME. Resume validates the
    re-supplied worker step's `step_id` against this (positional-identity guard,
    fail-closed on a same-count rename/reorder — the same cheap guard
    `FanOutBranchResumeState` applies to terminal branches)."""

    child_snapshot: PauseSnapshot
    """The child sub-workflow's OWN terminal `PauseSnapshot` (`RunResult.pause_snapshot`
    at the child's PAUSED return). On resume, the worker re-dispatch threads this as
    the child's `execute_workflow(pause_snapshot_input=...)` so the child re-enters at
    its cursor — the grandchild's already-completed steps are recovered, NOT
    re-executed. Nested recursively: this child snapshot may itself carry a
    `fan_out_resume` with its own `paused_child_branches` (a grandchild that paused on
    a great-grandchild). COVERED by `_compute_snapshot_hash` transitively via the
    enclosing `fan_out_resume.model_dump(mode="json")`."""


class EffectFencePausedBranchResumeState(BaseModel):
    """A fan-out branch whose own dispatch raised the runtime effect fence's
    `EffectFenceAmbiguousUncommittedError` (C-RT-31 §14.22) — the fence lost a reserve to a
    prior uncommitted attempt of a non-idempotent effect AND found no captured output proving
    completion, so whether the branch's effect fired is genuinely ambiguous.

    B-FANOUT-EFFECT-FENCE-BRANCH-PAUSE (R-FS-1) — the fan-out analogue of the LINEAR-path
    B-EFFECT-FENCE-HITL-ROUTE / B-EFFECT-FENCE-PAUSE-RESOLUTION (`workflow_driver` §26.2 route).
    A branch raising it was previously caught by the generic branch `except Exception` →
    recorded `completed` (ran-and-errored) → cascade; this carrier COMPOSES that ambiguous-pause
    THROUGH the fan-out barrier instead, so the run PAUSES honestly and `api.resume` re-enters
    the branch with the operator's `EffectFenceResolution` (SKIP_AS_FIRED / RE_FIRE / ABORT).

    Carried by `FanOutResumeState.effect_fence_paused_branches` /
    `PeerFanOutResumeState.effect_fence_paused_branches` (NOT `branches`: a terminal branch MUST
    NOT be re-dispatched, but an effect-fence-paused branch MUST be re-entered — via the
    fence-keyed resolution, NOT a fresh dispatch — the illegal-states-unrepresentable split,
    mirroring `PausedChildBranchResumeState` for the child-pause disposition).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    branch_index: int
    """The fan-out branch/worker ordinal (0-based) whose dispatch raised the fence-ambiguous
    error. On resume this ordinal is re-dispatched THROUGH the fence-keyed resolution directive
    (NOT skipped like a terminal branch, NOT fresh like an absent one)."""

    step_id: str
    """The branch `WorkflowStep.step_id` AT CAPTURE TIME. Resume validates the re-supplied
    branch step's `step_id` against this (positional-identity guard, fail-closed on a same-count
    rename/reorder — the same cheap guard `FanOutBranchResumeState` / `PausedChildBranchResumeState`
    apply)."""

    step_kind: str
    """The branch `WorkflowStep.step_kind` value AT CAPTURE TIME (always `tool-step` in production —
    only a TOOL_STEP's dispatch reaches the runtime tool fence, the source of the ambiguous-pause).
    Resume validates the re-supplied branch's `step_kind` against this (the changed-kind guard): if
    the operator kept the `step_id` but changed the kind away from the captured one, threading the
    `EffectFenceResolution` would reach NO fence → the original ambiguous tool effect would be
    silently abandoned. Fail closed — the live-pause analogue of the §2 crash-resume changed-kind
    guard (out-of-family Codex [P1])."""

    idempotency_key: str
    """The held effect-fence reserve's `idempotency_key` (read by name off the runtime
    `EffectFenceAmbiguousUncommittedError`, since harness-cp cannot import harness-runtime), so
    `api.resume` key-binds the operator's `EffectFenceResolution` to THIS branch's effect — the
    same keying discipline the LINEAR path's `EffectFenceResumeState` uses. On resume the branch
    is re-dispatched with an `EffectFenceResolutionDirective(resolution=..., idempotency_key=...)`
    threaded on its `StepExecutionContext.effect_fence_resolution`; the runtime tool dispatcher
    consumes the matching directive (SKIP_AS_FIRED → empty output, RE_FIRE → clear reserve +
    re-dispatch, ABORT → fail). Absent the key (defensive) → resume re-pauses (INERT, never an
    auto-re-fire)."""


class ResumeResult(BaseModel):
    """5-field resume-attempt outcome envelope (CP spec v1.11 §26.2).

    Reports whether the resumption succeeded, whether a material diff was
    detected, and the optional new `run_id` if resumption required a fresh
    run identifier. `diff_summary_hash` is sha256 of the diff serialization
    (format owed to U-CP-22 implementation arc per §26.7).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    resumed: bool
    """True iff workflow resumed successfully; False on diff-abort, snapshot
    corruption, or arbitration-owed escalation."""

    diff_detected: bool
    """True iff U-CP-64 material-diff detection found a diff."""

    diff_summary_hash: str | None = None
    """sha256 hex of diff-set canonical serialization; None when no diff
    detected. Format owed to U-CP-22 implementation per §26.7."""

    new_run_id: str | None = None
    """Fresh run_id if resumption required one; None when same run_id reused."""

    fail_class: str | None = None
    """CP-FAIL-* class identifier on resume failure; None on clean resume.
    One of CP-FAIL-PAUSE-SNAPSHOT-CORRUPTION, CP-FAIL-RESUME-MATERIAL-DIFF-DETECTED,
    CP-FAIL-RESUME-OPERATOR-ARBITRATION-OWED per §26.5."""


class ResumeContext(BaseModel):
    """Operator-supplied resume-time context envelope (CP spec v1.16 §26.8.1).

    Authored at CP spec v1.16 to enable HITL-gate-as-pause-trigger composition
    per runtime spec v1.21 §14.14.7 deferred-discretion residual (i) resolution.
    The envelope carries operator-supplied data the resumed step must consume
    during the resume cycle. v1.16 authors a single field for the durable-async
    HITL response delivery surface; future arcs may extend per v1.16 §26.8.1
    change-note adjacent defect (i).

    Consumed by runtime spec v1.24 §14.8.2 step 4-bis (the HITL gate composer
    body durable-async branch on resumed-step re-entry). The CP-side
    `attempt_resume(...)` method ingests but does NOT consume `ResumeContext`
    per CP spec v1.16 §26.8.5 method-body-posture-at-v1.16 framing.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    hitl_response: HITLResult | None = None
    """Operator HITL response delivered during durable-async pause.

    None when the pause was not correlated with a HITL gate (e.g.,
    EXPLICIT_OPERATOR, TIMEOUT_BOUNDARY, EXTERNAL_DEPENDENCY pause reasons).
    Populated HITLResult when the pause was triggered by a HITL gate composer
    body firing on durable-async cell synchrony per C-CP-18 §18.1 and the
    operator has delivered a response via the inbound webhook endpoint.
    HITLResult shape canonical at C-CP-17 §17.1.1 (`harness_cp.hitl_placement`).
    """

    effect_fence_resolution: EffectFenceResolution | None = None
    """Operator resolution of a §26.2 `WorkflowPauseReason.EFFECT_FENCE_AMBIGUOUS`
    pause (B-EFFECT-FENCE-PAUSE-RESOLUTION). `None` when the pause was not an
    effect-fence pause (e.g. a HITL / EXPLICIT_OPERATOR pause — the `hitl_response`
    field carries those). When set on a resume of an effect-fence pause, the driver
    key-binds it (via `PauseSnapshot.effect_fence_resume.idempotency_key`) and threads
    it to the resumed linear step's dispatch: SKIP_AS_FIRED → proceed with empty output
    (never re-fire); RE_FIRE → clear the held claim + re-dispatch fresh; ABORT → FAILED.
    Mutually exclusive in practice with `hitl_response` (a pause has one reason).

    For a fan-out pause where MULTIPLE branches fence-paused at once (the
    PARALLELIZATION / ORCHESTRATOR_WORKERS / HIERARCHICAL_DELEGATION barrier can hold
    several `effect_fence_paused_branches`), this single field is the UNIFORM default:
    the driver applies it to EVERY fence-paused branch (key-bound per branch to that
    branch's reserve). To resolve two fence-paused branches DIFFERENTLY in one resume,
    supply `effect_fence_resolutions` (below); a per-key entry there OVERRIDES this
    default for its branch (B-FANOUT-EFFECT-FENCE-PER-BRANCH-RESOLUTION)."""

    effect_fence_resolutions: dict[str, EffectFenceResolution] | None = None
    """Per-branch-DISTINCT effect-fence resolutions, keyed by held-reserve
    `idempotency_key` (B-FANOUT-EFFECT-FENCE-PER-BRANCH-RESOLUTION, R-FS-1). `None`
    (the default) → every fence-paused branch resolves to the uniform
    `effect_fence_resolution` above (the v1.65 byte-identical behavior). When supplied,
    a fan-out branch whose held reserve `idempotency_key` (from
    `FanOutResumeState.effect_fence_paused_branches[*].idempotency_key`, surfaced in the
    pause `PauseSnapshot`) appears as a key here is resolved with THIS map's value;
    branches whose key is absent fall back to the uniform `effect_fence_resolution`
    (and re-pause INERT if that too is `None` — the decline-mirror, never an
    auto-re-fire). This is a `default + per-key override` composition (NOT a replacement
    of the single field): the single field is the uniform answer, the map overrides
    specific branches. Read via `effect_fence_resolution_for(key)`.

    Consumed ONLY at the two fan-out consume sites (`_execute_parallelization` /
    `_execute_orchestrator_workers`). The LINEAR effect-fence pause has exactly one held
    reserve key, so per-branch-DISTINCT resolution is structurally inapplicable there —
    the linear resume consumes the single `effect_fence_resolution` field verbatim (a
    map supplied for a linear pause is inert). Map entries whose key matches no
    fence-paused branch this round are harmlessly ignored. Keyed by `idempotency_key`
    (not `branch_index`) so the map is uniform with the dispatcher's per-`(run, step,
    tool)` key-bind. ABORT in a map entry retains its shipped RUN-level-terminal
    semantic (v1.65 §1(b), preserved byte-for-byte) — the whole run FAILS and all
    continue-resolutions are suppressed. ABORT_BRANCH in a map entry is per-branch-SCOPED
    (B-FANOUT-EFFECT-FENCE-PER-BRANCH-SCOPED-ABORT, CP spec v1.73 §1): fail JUST that
    branch (record it terminal, never re-dispatched) while the SIBLINGS the operator
    vouched for (SKIP_AS_FIRED / RE_FIRE) fire and the run folds survivors per
    `cascade_policy`. So all four resolutions compose freely across branches in one map."""

    def effect_fence_resolution_for(self, idempotency_key: str) -> EffectFenceResolution | None:
        """The operator's effect-fence resolution for one held-reserve `idempotency_key`.

        B-FANOUT-EFFECT-FENCE-PER-BRANCH-RESOLUTION (R-FS-1) — the single source of
        truth for "what did the operator answer for THIS branch's fence?": the
        `effect_fence_resolutions` map entry for `idempotency_key` if present, else the
        uniform `effect_fence_resolution` default. `None` when neither is supplied → the
        branch re-pauses INERT (the #701 decline-mirror; never an auto-re-fire). Pure
        lookup-with-fallback (no control-flow branch on "which mode"): a `None` map and a
        map-without-this-key both fall through to the single default, so the v1.65
        single-field behavior is preserved byte-for-byte when no map is supplied."""
        if self.effect_fence_resolutions is not None:
            mapped = self.effect_fence_resolutions.get(idempotency_key)
            if mapped is not None:
                return mapped
        return self.effect_fence_resolution


# B-HIERARCHICAL-PAUSE (R-FS-1) — `FanOutResumeState.paused_child_branches` forward-refs
# `PausedChildBranchResumeState` (defined after `PauseSnapshot`, which it nests), so the
# annotation cannot resolve at `FanOutResumeState` class-build time (it is the FIRST
# forward reference in this module — `PauseSnapshot.fan_out_resume` resolves backward).
# Rebuild once now that every referenced model exists. `PausedChildBranchResumeState`
# itself needs no rebuild (its `PauseSnapshot` ref is backward).
FanOutResumeState.model_rebuild()
