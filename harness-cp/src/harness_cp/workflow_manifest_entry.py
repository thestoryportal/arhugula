"""`WorkflowManifestEntry` schema — U-CP-13.

Implements C-CP-06 §6.1 (the workflow-manifest-entry shape). Declares the
`WorkflowManifestEntry` record (13 top-level fields at CP spec v1.63;
`entry_version` at v2.12, `default_gate_level` at v1.20,
`fanout_timeout_disposition` at v1.63), the `FanoutTimeoutDisposition` enum,
and the constituent `StepOverride` record.

**v2.12 re-open (2026-05-20).** `entry_version: int = 1` field added per
`Implementation_Plan_Control_Plane_v2_12.md` §0.1 + §2.2 amendment.
Resolves `[[fork-u-cp-56-resumption-underspec]]` by satisfying the
`run_idempotency_key = sha256(run_id, workflow_id, entry_version)`
composition required at CP spec v1.4 §25.6 line 270. CP spec v1.4 §6.1
(preserved verbatim from v1.2) authorizes the carrier growth via the
explicit "// ... additional per-workload fields" extension clause; no
spec bump required at v2.12. Default value 1 means pre-versioning
workflows compose deterministically without explicit caller-side
annotation.

`WorkflowManifestEntry` is the canonical per-workflow customization-persistence
shape: it binds a workflow to its workload class, persona tier, engine class,
topology pattern, per-layer routing budgets, cross-family fallback chain, HITL
placements, optional sub-agent briefs, and per-step overrides.

`workload_class` and `persona_tier` are mandatory (no default) per ADR-F1 v1.2
workload-class commitment — validation rejects missing values. `topology_pattern`
admissibility is verified against the U-CP-22 `is_admissible` predicate at
validation time; `engine_class` against the U-CP-16 candidate mapping.

Authority: Implementation_Plan_Control_Plane_v2_1.md §2 U-CP-13 (preserved
verbatim into v2.2/v2.3; v2.5 §0.5 + v2.6 §0.11 dependency-edge deltas —
`[U-CP-00]` for `WorkloadClass`, `[U-CORE-01]` for `StepID`, `[U-CP-00c]` for
`ModelBinding`, `[U-CP-30]` for `HandoffContext`-family substrate);
Spec_Control_Plane_v1_2.md §6 C-CP-06 §6.1 (preserved verbatim into v1.3);
ADR-F1 v1.2 §Decision workload-class commitment; ADR-F3 v1.1.
"""

from __future__ import annotations

from enum import StrEnum

from harness_core import PersonaTier, StepID, WorkloadClass
from pydantic import BaseModel, ConfigDict

from harness_cp.cp_shared_types import AgentRole, ModelBinding
from harness_cp.cross_family_fallback_chain import FallbackChain
from harness_cp.engine_class import EngineClass
from harness_cp.gate_level_rule import GateLevel
from harness_cp.hitl_placement import HITLPlacement, LoosenablePlacementKind
from harness_cp.layer_budget import LayerBudget
from harness_cp.sub_agent_brief import SubAgentBrief
from harness_cp.topology_pattern import TopologyPattern


class FanoutTimeoutDisposition(StrEnum):
    """The operator-set resolution of a deadline-cut (`timed_out`) fan-out branch
    on crash-resume — R-FS-1 `B-FANOUT-CRASH-RESUME-TIMEOUT-REPLAY` (CP spec v1.63 §1).

    A `timed_out` branch is a deadline-cut in-flight dispatch (the §25.15 barrier
    cut-off) whose effect MAY or MAY NOT have landed. v1.55 §1 recorded it but
    failed the crash-resume CLOSED unconditionally, naming the operator-resolvable
    disposition as a registered follow-on. This enum is that follow-on's domain.

    At-most-once is the GATE, not the policy: `RE_DISPATCH` re-runs ONLY a
    re-fire-safe deadline-cut branch (no external effect to double-fire, keyed on
    the v1.62 dispatch-time-kind marker) and fails closed on any effect-bearing
    one — there is no fail-open path the operator can select.
    """

    FAIL_CLOSED = "fail-closed"
    """Default — v1.55 §1 byte-identical: any `timed_out` branch fails the
    crash-resume closed (`_FanOutStoreTimeoutAmbiguousError`). The conservative
    reading: a deadline-cut branch's effect may have landed, so refuse recovery."""

    RECOVER_AS_TERMINAL = "recover-as-terminal"
    """Recover the `timed_out` branch as a `completed`-no-output degraded
    non-contributor — never folded into the aggregate, never re-dispatched. The
    run's outcome is then governed by `cascade_policy` (the existing degraded
    reconciliation): `proceed` recovers PARTIAL folding the survivors; the strict
    tiers fire their degraded semantics. Safe for ALL kinds (no re-dispatch)."""

    RE_DISPATCH = "re-dispatch"
    """Re-run a re-fire-safe (`{DECLARATIVE_STEP, INFERENCE_STEP}`) `timed_out`
    branch fresh (excluded from the recovered set → the existing crash-resume
    re-dispatch path re-runs it). An effect-bearing (or un-kinded-marker)
    `timed_out` branch fails closed — its effect may have landed; re-dispatch
    would double-fire. Keyed on the v1.62 dispatch-time-kind marker (the
    changed-manifest at-most-once guard)."""


class StepOverride(BaseModel):
    """A per-step override of manifest-entry defaults (C-CP-06 §6.1).

    Populated for pipeline-automation per-stage customization. Each field is
    optional — an absent field inherits the manifest-entry default. The
    override is applied field-by-field by the U-CP-14 per-step override
    evaluator (`resolve_step_binding`).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: StepID
    model_binding: ModelBinding | None = None
    engine_class: EngineClass | None = None
    hitl_placement: HITLPlacement | None = None

    prompt_version_sha: str | None = None
    """v1.37 addition (CP spec v1.37 §6.1 NEW field per the v1.27 §2(d)
    X-AL-3 explicit-extension discipline; R-FS-1 arc B4 Slice 3 per-step
    PROMPT override).

    When not ``None``, a content-addressed reference into the IS
    ``PromptManifest.versions`` store (IS spec v1.7 §5.3): the ``version_sha``
    of the prompt version whose ``content`` is injected as the provider system
    prompt for *this step*, overriding the per-role (B4 Slice 1) and run-level
    default prompts. ``None`` (the default) preserves v1.6 MVP behavior verbatim
    — the step inherits the per-role (if its branch role binds one) or
    run-level-default prompt.

    Symmetric with the per-step ``model_binding`` (a per-step selection of a
    resource the run otherwise resolves at a coarser scope). The store-membership
    resolution + content injection are the runtime consumer's responsibility (the
    §14.5.2 dispatch seam, precedence per-step > per-role > default), keeping CP
    IS-pure — ``resolve_step_binding`` passes the sha through. Per-step *role*
    override is OUT of scope (the §14.5.3 single-role-source invariant forecloses
    a second per-step role carrier; that is the distinct Slice-4 gate).

    Per-step prompt-override provenance is the per-step override state-ledger
    entry (CP spec v1.37 §6.6): a flip changes ``StepEffectiveBinding.model_dump``
    → the override entry's idempotency_key → a distinct hash-chained entry. NOT
    the run-level C-IS-05 §5.2 procedural-tier hash (which stays run-level — the
    per-step MODEL override precedent).
    """

    agent_role: AgentRole | None = None
    """v1.38 addition (CP spec v1.38 §6.1 NEW field per the v1.27 §2(d) X-AL-3
    explicit-extension discipline; R-FS-1 arc B4 Slice 4 per-step ROLE override).

    When not ``None``, the operator-assigned ``AgentRole`` for *this step*,
    overriding the role the runtime would otherwise carry — the fan-out-derived
    role (``derive_agent_role(step_id)`` per B4 Slice 2) on a non-linear branch,
    or *no role at all* on the ``SINGLE_THREADED_LINEAR`` path (where the step
    would otherwise carry the ``_MVP_DEFAULT_AGENT_ROLE``). ``None`` (the default)
    preserves v1.6 MVP behavior verbatim — the step inherits the fan-out-derived
    role or the linear-path default.

    **Committed-invariant relaxation (runtime spec v1.52 §14.5.3 — operator-
    ratified).** Adding a per-step role *carrier* relaxes two B1-era §14.5.3
    invariants: invariant 2 ("single role source — never a second per-step role
    carrier") and invariant 3 ("linear path untouched"). The relaxation is
    **composition-time, not dispatch-time**: ``resolve_step_binding`` carries the
    override onto ``StepEffectiveBinding.agent_role``; the CP driver *folds* it
    into the single ``StepExecutionContext.agent_role`` source at branch/step
    composition (precedence **per-step > fan-out-derived > default**), so the
    runtime dispatch still reads ONE role source (``step_context.agent_role``) —
    no MODEL-style two-authority-at-dispatch (C-RT-15 §14.5.3). Invariant 1
    (non-breaking default) is preserved: an absent override leaves the composed
    role unchanged, byte-identical to v1.37.

    Symmetric with ``prompt_version_sha``/``model_binding`` (a per-step selection
    of a resource the run otherwise resolves at a coarser scope). Provenance is
    the per-step override state-ledger entry (CP spec v1.38 §6.6, extended): the
    role rides ``StepEffectiveBinding.model_dump`` into the override entry's
    outcome-hash — NOT the run-level C-IS-05 §5.2 procedural-tier hash (which
    hashes the run-level ``PromptSelectionManifest``/``RoutingManifest`` per-role
    catalogs, not per-workflow per-step overrides — the per-step MODEL/PROMPT
    override precedent).
    """

    removed_placements: frozenset[LoosenablePlacementKind] = frozenset()
    """v1.53 addition (CP spec v1.53 §6.1 NEW field per the v1.27 §2(d) X-AL-3
    explicit-extension discipline; R-FS-1 ``B-HITL-PLACEMENT-PER-STEP-LOOSEN`` —
    the operator-ratified committed-invariant relaxation of the §17.1
    monotone-HITL "all cells" floor).

    The opt-in set of HITL placements *this step* REMOVES. ``LoosenablePlacementKind``
    is a closed one-member enum (``SUB_AGENT_BOUNDARY`` only) so ``PRE_ACTION`` /
    ``VALIDATOR_ESCALATION`` are STRUCTURALLY unrepresentable (the §19.1
    floor-evaluation bypass-seam + the §14.15-path wrong-layer, respectively —
    foreclosed at the type, not a runtime guard). Empty (the default) preserves
    the v1.49 ADD-only fold verbatim — byte-identical + monotone.

    This is the FIRST per-step override that can REDUCE gating (every prior
    override only TIGHTENS). It is NOT unconditional: ``resolve_step_binding``
    carries it onto ``StepEffectiveBinding.removed_placements``; the
    SUB_AGENT_BOUNDARY composer (``hitl_gate_composer.py`` step 4c) applies it
    solo-scoped + FLOOR-CLAMPED (overrides only the §19.1 PERSONA human-oversight
    floor + the LOCAL_MUTATION blast cell; the HARD ``per_tool``/``mcp_trust``
    floors + ``blast_radius`` above local-mutation are NEVER override-able → a
    removal on a high-blast / deny-tier / untrusted-MCP dispatch is REFUSED) +
    auto-audited fail-closed. Provenance is the per-step override state-ledger
    entry (CP spec v1.53 §6.6, like ``agent_role``) — NOT the run-level C-IS-05
    §5.2 hash.
    """


class WorkflowManifestEntry(BaseModel):
    """The workflow-manifest-entry shape — canonical per-workflow customization.

    Thirteen top-level fields at v1.63 (`entry_version` appended at v2.12,
    `default_gate_level` at CP spec v1.20, `fanout_timeout_disposition` at CP
    spec v1.63 §1 — R-FS-1 `B-FANOUT-CRASH-RESUME-TIMEOUT-REPLAY`). CP spec
    v1.4 §6.1 (verbatim from v1.2) authorizes the carrier growth via the
    "// ... additional per-workload fields" extension clause; each addition is
    Pydantic-Optional with a behavior-preserving default.

    `workload_class` and `persona_tier` are mandatory (no default) per
    ADR-F1 v1.2; the absence of a default means Pydantic validation rejects
    a missing value. `topology_pattern` admissibility is verified against
    the U-CP-22 `is_admissible` predicate at validation time; `engine_class`
    against the U-CP-16 candidate mapping. `hitl_placements` is ordered by
    placement-kind precedence per the U-CP-38 `HITLPlacement` schema.
    `entry_version` defaults to 1 so existing constructor sites continue to
    validate without modification.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_id: str
    workload_class: WorkloadClass
    """Mandatory — no default (ADR-F1 v1.2 workload-class commitment)."""

    persona_tier: PersonaTier
    """Mandatory — no default (ADR-F1 v1.2 workload-class commitment)."""

    engine_class: EngineClass
    topology_pattern: TopologyPattern
    layer_budgets: tuple[LayerBudget, ...]
    """Per-layer routing-budget overrides."""

    fallback_chain: FallbackChain
    """Overrides for the cross-family fallback chain."""

    hitl_placements: tuple[HITLPlacement, ...]
    """Declared per workflow per C-CP-17 §17.3."""

    sub_agent_briefs: tuple[SubAgentBrief, ...] | None = None
    """For fan-out patterns."""

    per_step_overrides: dict[StepID, StepOverride]
    """Populated for pipeline-automation per-stage customization."""

    entry_version: int = 1
    """v2.12 addition. Integer carried into the U-CP-56 §25.6
    `run_idempotency_key = sha256(run_id, workflow_id, entry_version)`
    hash composition for selective replay-resumption discrimination
    (`[[fork-u-cp-56-resumption-underspec]]` resolution).

    Default value 1 means pre-versioning workflows compose deterministically
    without explicit caller-side annotation. Operators bump the value when
    the workflow's contract changes in a way that should invalidate cached
    step-resumption substrate — i.e., when a re-entry under the same
    `run_id` + `workflow_id` should be treated as a fresh run rather than
    a resumption. Orthogonal to the workflow's body steps' content
    (semantic-version-of-the-workflow-declaration semantics).
    """

    default_gate_level: GateLevel | None = None
    """v1.20 addition (CP spec v1.20 §6.1.Y NEW field per ratified Reading A
    of `.harness/class_1_fork_h_t_cp_19_default_gate_level_spec_extension.md`).
    Operator-surfaced seed for the C-CP-12 §12.2 sub-agent gate-level
    composition formula at workflow_driver per-step composition site.

    Default value `None` preserves the v1.6 MVP behavior: when the field is
    None at runtime, workflow_driver composes `StepExecutionContext` with
    `parent_gate_level=GateLevel.AUTO` (the v1.6 hardcoded MVP default).
    When operator-surfaced (not None), workflow_driver reads from this
    field directly. Pydantic v2 Optional discipline preserves construction-
    time omission across the existing 100+ test fixtures + manifest
    construction sites (zero downstream-consumer disruption per the fork
    doc §2.1 Reading A scope analysis).

    Closes H_T-CP-19 spec-extension layer per
    `.harness/class_1_fork_h_t_cp_19_default_gate_level_spec_extension.md`
    Reading A ratification 2026-05-27 (operator Q1=A + Q2=apply-now +
    Q3=defer-layer-3-e2e). RETIRE-READY promotion enabled at layer-2 close
    (this field + workflow_driver.py:738 read site); RETIRE-READY → RETIRED
    waits on multi-deployment e2e fixture per Q3 deferral.
    """

    fanout_timeout_disposition: FanoutTimeoutDisposition = FanoutTimeoutDisposition.FAIL_CLOSED
    """v1.63 addition (R-FS-1 `B-FANOUT-CRASH-RESUME-TIMEOUT-REPLAY`, CP spec
    v1.63 §1). Operator-set resolution of a deadline-cut (`timed_out`) fan-out
    branch on crash-resume: `FAIL_CLOSED` (default) / `RECOVER_AS_TERMINAL` /
    `RE_DISPATCH` (see `FanoutTimeoutDisposition`). Default `FAIL_CLOSED`
    reproduces the v1.55 §1 unconditional fail-closed byte-for-byte, so existing
    constructor sites + fixtures validate without modification. Consumed at the
    `_determine_fanout_resume` crash-resume timed_out classification site. The
    §6.1 'additional per-workload fields' extension-clause growth (mirrors the
    v1.20 `default_gate_level` + v2.12 `entry_version` additive-optional
    precedents). At-most-once is PRESERVED — `RE_DISPATCH` re-runs only a
    re-fire-safe deadline-cut branch (no external effect to double-fire), keyed
    on the v1.62 dispatch-time-kind marker; effect-bearing fails closed.
    """
