"""Workflow execution driver — U-CP-56.

Implements C-CP-25 §25.1 (scope) + §25.2 (signatures) + §25.3 (iteration
discipline, happy-path) + §25.5 (lifecycle event emission boundaries —
SINGLE_THREADED_LINEAR filter over §5.1 closed-at-8 taxonomy) + §25.6
(replay-resumption composition with §8.2 idempotency-key join) + §25.7
(failure modes 1-4).

Drain composition (§25.4 + failure mode 5) is U-CP-57.

**Architectural shape (substrate composition).** The driver does not import
`harness-runtime` (which would invert the CP→runtime dependency direction).
Instead it consumes substrate via two locally-declared Protocols:

- `LedgerWriterLike` — write-side substrate (C-IS-07 §7.1 idempotent append
  composition with C-IS-05 entry shape). Concretized by runtime's
  `LedgerWriter` (`harness_runtime.lifecycle.state_ledger.LedgerWriter`)
  which structurally satisfies the protocol.
- `LifecycleEventEmitterLike` — lifecycle-event emission surface (§5.1 8-class
  taxonomy via `harness_core.WorkflowEventClass`). Concretized by runtime's
  `RuntimeLifecycleEventEmitter`.

Step dispatch is delegated through a `StepDispatcher` Protocol — the driver
itself is opaque to step body kind (LLM call / tool call / sub-routine);
binding lookup + provider/model dispatch is the dispatcher's responsibility.
Per C-CP-25 §25.3.3.4: "Step body is opaque to the driver; the router owns
provider / model / engine dispatch."

Authority:
- `Spec_Control_Plane_v1_4.md` §25
- `Implementation_Plan_Control_Plane_v2_11.md` U-CP-56
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import inspect
import json
from collections.abc import Awaitable, Collection, Coroutine, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from datetime import UTC, datetime
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast, runtime_checkable

from harness_as.sandbox_tier import SandboxTier
from harness_core.identity import ActionID
from harness_core.workflow_event_class import WorkflowEventClass
from harness_is.state_ledger_entry_schema import Actor
from opentelemetry.trace import Status, StatusCode, TracerProvider

if TYPE_CHECKING:
    from harness_is.state_ledger_entry_schema import Identifier

    from harness_cp.validator_framework import SyncValidatorFrameworkFacade
    from harness_cp.validator_framework_types import ValidatorEvaluation

from harness_cp.cp_shared_types import ActorIdentity, AgentRole, ModelBinding
from harness_cp.engine_class import EngineClass
from harness_cp.gate_level_rule import GateLevel
from harness_cp.handoff_context import (
    ActionKind,
    HandoffContext,
    LedgerEntryRef,
    ProposedAction,
    RetryHistory,
    StateSummary,
)
from harness_cp.hitl_placement import HITLPlacement
from harness_cp.pause_resume_protocol import (
    CP_FAIL_PAUSE_SNAPSHOT_CORRUPTION,
    CP_FAIL_RESUME_MATERIAL_DIFF_DETECTED,
    PauseReason,
    PauseResumeProtocol,
    PauseResumeProtocolEventKind,
    ResumeOutcomeKind,
)
from harness_cp.pause_resume_protocol_types import (
    EffectFencePausedBranchResumeState,
    EffectFenceResolution,
    EffectFenceResolutionDirective,
    EffectFenceResumeState,
    EvaluatorOptimizerResumeState,
    EvaluatorOptimizerStepResumeState,
    FanOutBranchResumeState,
    FanOutResumeState,
    HandoffResumeState,
    HandoffStageResumeState,
    MaterialDiffPolicy,
    OrchestratorEffectFencePausedResumeState,
    PausedChildBranchResumeState,
    PauseSnapshot,
    PeerFanOutResumeState,
    ResumeContext,
    WorkflowPauseReason,
)
from harness_cp.per_role_catalog import derive_agent_role
from harness_cp.per_step_override_evaluator import (
    StepEffectiveBinding,
    compose_override_entry_payload,
    resolve_step_binding,
)
from harness_cp.topology_pattern import CascadePolicy, TopologyPattern
from harness_cp.workflow_driver_errors import (
    BranchBarrierDeadlineExceededError,
    EngineClassNotYetMaterializedError,
    TopologyPatternNotYetMaterializedError,
)
from harness_cp.workflow_driver_types import (
    RunResult,
    RunStatus,
    StepExecutionContext,
    StepKind,
    SubAgentChildPausedError,
    WorkflowStep,
    compose_branch_child_context,
    compose_branch_metadata,
    compose_branch_path,
    compose_branch_step_action_id,
    compose_branch_terminal_action_id,
    compose_branch_terminal_path,
    fold_step_hitl_placements,
)
from harness_cp.workflow_manifest_entry import (
    FanoutTimeoutDisposition,
    WorkflowManifestEntry,
)
from harness_cp.workload_engine_class_matrix import d4_tunable, lookup_cell

# ---------------------------------------------------------------------------
# v1.4 in-scope sets (per C-CP-25 §25.1 + Implementation Plan §0.2)
# ---------------------------------------------------------------------------


class _DriverStrategyStatus(Enum):
    """Materialization status of a topology's driver strategy (C-CP-25 §25.10).

    The §25.10 driver-strategy dispatch table replaces the §25.1
    `_IN_SCOPE_TOPOLOGY` materialization gate. It enumerates ALL SIX
    `TopologyPattern` values; a pattern lands by flipping its table entry, so
    the dispatch site never needs re-plumbing. At B1-impl-2 only
    `SINGLE_THREADED_LINEAR` is materialized (`LINEAR_INLINE` — the existing
    §25.3 iteration loop); the remaining non-linear patterns are
    `NOT_YET_MATERIALIZED` and raise `TopologyPatternNotYetMaterializedError`
    until each strategy unit (U-CP-86..U-CP-90) lands. `PARALLELIZATION`
    (the fan-out-barrier-aggregate strategy) landed first at U-CP-86;
    `EVALUATOR_OPTIMIZER` (the sequential generate→evaluate→regenerate loop)
    landed second at U-CP-87; `ORCHESTRATOR_WORKERS` (orchestrator-dispatch-
    collect fan-out) landed third at U-CP-88; `HIERARCHICAL_DELEGATION`
    (recursive bounded-fan-out — cap-3 per parent reusing `ORCHESTRATOR_WORKERS`
    at each level) landed fourth at U-CP-89; `DECENTRALIZED_HANDOFF` (single-owner
    sequential handoff via `HandoffContext`) landed fifth (last) at U-CP-90. **All
    six `TopologyPattern` values are now materialized** — no member is
    `NOT_YET_MATERIALIZED` (the status is retained as the dispatch-table sentinel
    type for any future pattern).

    **`DriverStrategy` shape (O-CP-1(d) resolution, decided at U-CP-86 — the
    first strategy unit).** The dispatch is a flat enum-keyed branch in
    `_execute_workflow_body`, NOT a callable/class registry. The enum value
    discriminates which materialized strategy runs; the body routes
    `LINEAR_INLINE` → the existing §25.3 inline loop and each non-linear value
    → its dedicated `_execute_<strategy>(...)` function returning
    `(RunResult, steps_executed)`. A heavier callable/class `DriverStrategy`
    abstraction is intentionally NOT introduced (simplicity-first — five
    strategies routed by a closed enum need no indirection layer; the dispatch
    table already enumerates the closed-at-6 `TopologyPattern`).
    """

    LINEAR_INLINE = "linear-inline"
    PARALLELIZATION = "parallelization"
    EVALUATOR_OPTIMIZER = "evaluator-optimizer"
    ORCHESTRATOR_WORKERS = "orchestrator-workers"
    HIERARCHICAL_DELEGATION = "hierarchical-delegation"
    DECENTRALIZED_HANDOFF = "decentralized-handoff"
    NOT_YET_MATERIALIZED = "not-yet-materialized"


# § 25.10.1 — driver-strategy dispatch table (lifts the §25.1
# `_IN_SCOPE_TOPOLOGY` gate). Keyed on the C-CP-10 §10.1 `TopologyPattern`
# enum; enumerates all six members (the closed-at-6 enum — an exhaustiveness
# test asserts no member is missing, so resolution never falls through to a
# KeyError).
_DRIVER_STRATEGY_DISPATCH: Mapping[TopologyPattern, _DriverStrategyStatus] = {
    TopologyPattern.SINGLE_THREADED_LINEAR: _DriverStrategyStatus.LINEAR_INLINE,
    TopologyPattern.PARALLELIZATION: _DriverStrategyStatus.PARALLELIZATION,
    TopologyPattern.ORCHESTRATOR_WORKERS: _DriverStrategyStatus.ORCHESTRATOR_WORKERS,
    TopologyPattern.HIERARCHICAL_DELEGATION: _DriverStrategyStatus.HIERARCHICAL_DELEGATION,
    TopologyPattern.DECENTRALIZED_HANDOFF: _DriverStrategyStatus.DECENTRALIZED_HANDOFF,
    TopologyPattern.EVALUATOR_OPTIMIZER: _DriverStrategyStatus.EVALUATOR_OPTIMIZER,
}


def resolve_driver_strategy(topology_pattern: TopologyPattern) -> _DriverStrategyStatus:
    """Resolve a topology pattern to its driver strategy (C-CP-25 §25.10).

    Replaces the §25.3.1 `_IN_SCOPE_TOPOLOGY` materialization gate. A pattern
    whose strategy has not yet landed (the four non-linear patterns still
    `NOT_YET_MATERIALIZED` after U-CP-86 lands `PARALLELIZATION`) raises
    `TopologyPatternNotYetMaterializedError`. Admissibility
    (C-CP-10 §10.3 / C-CP-11 §11.1) is unchanged — it is rejected at
    workflow-binding time; §25.10 lifts only the *materialization* gate, not
    admissibility (Invariant 2). The typed error is preserved for any future
    non-enumerated topology.
    """
    status = _DRIVER_STRATEGY_DISPATCH[topology_pattern]
    if status is _DriverStrategyStatus.NOT_YET_MATERIALIZED:
        raise TopologyPatternNotYetMaterializedError(topology_pattern)
    return status


_IN_SCOPE_ENGINE_CLASSES: frozenset[EngineClass] = frozenset(
    {
        EngineClass.PURE_PATTERN_NO_ENGINE,
        EngineClass.SAVE_POINT_CHECKPOINT,
        # U-CP-93 (R-FS-1 E-impl-1) — EVENT_SOURCED_REPLAY materialized as
        # resumption-routing impl against cleared C-CP-07/08, following the
        # U-CP-56 SAVE_POINT_CHECKPOINT precedent (added to _IN_SCOPE as impl,
        # "no spec bump"). See the `:1445`-region dispatch branch below.
        # Resumption (resume_at/RESUMPTION) is computed only on the
        # SINGLE_THREADED_LINEAR path; the 5 non-linear strategies are
        # resume-blind for EVERY in-scope engine class (incl. save-point) — they
        # return before the resume-path block. So EVENT_SOURCED_REPLAY + a
        # non-linear topology inherits the same resume-blind behavior save-point
        # already has; non-linear/fan-out resume is the registered B-FANOUT-PAUSE
        # arc (`.harness/beyond-mvp-capability-boundary-ledger.md`), not this unit.
        EngineClass.EVENT_SOURCED_REPLAY,
        # U-CP-94 (R-FS-1 E-impl-2) — WAL_SEGMENT materialized as segment-replay
        # resumption impl against cleared C-CP-07/08, following the U-CP-56 /
        # U-CP-93 precedent. The `:1469`-region dispatch branch computes resume_at
        # via the F2 per-segment prefix join (`_determine_segment_replay_resume_at`,
        # C-CP-08 §8.2 row 5) — a CP→IS read, no CP→runtime import. The durable
        # WAL segment-log substrate (U-RT-121) + the engine-layer recovery-loop
        # firing (U-CP-95 capture_pause/attempt_resume → C-CP-49/50, R-CXA-2
        # go-live) are the genuine distinguishing capability over save-point.
        # Resume-blind on the 5 non-linear strategies, exactly as save-point /
        # EVENT_SOURCED_REPLAY (B-FANOUT-PAUSE arc, not this unit).
        EngineClass.WAL_SEGMENT,
        # U-CP-96 (R-FS-1 E-impl-3a) — RECONCILER_LOOP materialized as
        # level-triggered read/diff/converge resumption (C-CP-08 §8.1
        # `reconciler_converge`) impl against cleared C-CP-07/08 + the v1_33 §7.4
        # substrate-deferral (hand-rolled etcd-style per I-6), following the
        # U-CP-93/94 precedent (added to _IN_SCOPE as impl, "no spec bump"). The
        # `reconciler-converge` dispatch branch (below, after EVENT_SOURCED_REPLAY)
        # computes resume_at via the F2 prefix join
        # (`_determine_reconciler_converge_resume_at`, a CP→IS read; the engine-owned
        # CRD_RECONCILER_LEDGER substrate is U-RT-123, not read here — no CP→runtime
        # import). This is the CP/IS-only resumption-semantics half; the engine-layer
        # recovery-loop firing (U-CP-97) + the durable etcd-style substrate (U-RT-123)
        # + activation (U-RT-124) are E-impl-3b. **RECONCILER_LOOP is the LAST engine
        # class — with it in _IN_SCOPE, _IN_SCOPE == the full closed EngineClass set
        # and the EngineClassNotYetMaterializedError gate (the `not in
        # _IN_SCOPE_ENGINE_CLASSES` raise above) becomes preserved-but-unreachable
        # (the E sub-program closes at the gate level).**
        # Non-linear pause/resume remains owned by the fan-out pause arcs. The fan-out
        # crash-resume branch now gates RECONCILER replay through the same engine-layer
        # CAS/abort check before replaying branch-store output.
        EngineClass.RECONCILER_LOOP,
    }
)


# U-CP-95 (R-FS-1 E-impl-2) — engine-layer resume-abort → fail-class mapping.
# The C-CP-22 §22.1 `ResumeOutcomeKind` ABORT_* members that must FAIL the run
# CLOSED on engine-layer recovery, each mapped to its semantically-matching
# existing CP fail-class marker (no new fail-class invented — X-AL-3-clean;
# reuses the C-CP-26 §26.5 constants). The two RESUME_* members are absent by
# construction: a present pause whose resume succeeds proceeds normally. A
# corrupt snapshot → ABORT_SNAPSHOT_CORRUPTED; a revalidation failure →
# ABORT_REVALIDATION_FAILED (unreachable under the default WAL substrate wiring,
# which injects an empty diff-provider + always-succeeds revalidation, but
# handled for correctness if a deployment binds a real diff-provider).
_ENGINE_RESUME_ABORT_FAIL_CLASS: dict[ResumeOutcomeKind, str] = {
    ResumeOutcomeKind.ABORT_SNAPSHOT_CORRUPTED: CP_FAIL_PAUSE_SNAPSHOT_CORRUPTION,
    ResumeOutcomeKind.ABORT_REVALIDATION_FAILED: CP_FAIL_RESUME_MATERIAL_DIFF_DETECTED,
}


# ---------------------------------------------------------------------------
# Substrate Protocols (avoid harness-runtime backward dep)
# ---------------------------------------------------------------------------


@runtime_checkable
class LedgerWriterLike(Protocol):
    """Write-side state-ledger substrate (C-IS-07 §7.1 idempotent append).

    Structurally satisfied by
    `harness_runtime.lifecycle.state_ledger.LedgerWriter`.
    """

    actor: Actor

    def append(self, payload: Any, write_key: Any) -> Any:
        """Append a hash-chain-preserving entry (delegates to IS U-IS-11)."""
        ...

    @property
    def is_genesis(self) -> bool:
        """`True` when no entries exist yet."""
        ...

    @property
    def entry_count(self) -> int:
        """Current ledger entry count (snapshot at construction)."""
        ...


@runtime_checkable
class LedgerReaderLike(Protocol):
    """Read-side state-ledger substrate (C-IS-07 §7.4 implementation-discretion
    primitive; `read_by_idempotency_key(key)` enumerated as authorized).

    Introduced at CP plan v2.12 to materialize U-CP-56 AC #6 (full
    selective replay-resumption per `[[fork-u-cp-56-resumption-underspec]]`
    Path A-modified resolution). Mirrors the LedgerWriterLike read/write
    separation pattern; concretized by a runtime adapter wrapping
    `harness_is.state_ledger_read.LedgerNavigationPrimitive` over a
    `harness_is.state_ledger_write.read_ledger` snapshot.

    Method shape mirrors the IS NavigationPrimitive contract verbatim.
    """

    def read_by_idempotency_key(
        self,
        idempotency_key: Any,
        bounded_window: Any,
    ) -> Any:
        """Read entries by `idempotency_key`.

        The `Any` typing on `idempotency_key`, `bounded_window`, and the
        return shape avoids a CP→IS Protocol-level type dependency. Runtime
        concretization uses `harness_is.types.Identifier` (idempotency_key),
        `harness_is.state_ledger_read.BoundedWindow` (bounded_window),
        `harness_is.state_ledger_read.ReadResult` (return) — callers narrow
        at concrete sites if they need the typed shape.
        """
        ...


@runtime_checkable
class LifecycleEventEmitterLike(Protocol):
    """Lifecycle-event emission surface (§5.1 8-class taxonomy via
    `harness_core.WorkflowEventClass`).

    Structurally satisfied by
    `harness_runtime.lifecycle.lifecycle_emitter.RuntimeLifecycleEventEmitter`.
    """

    def emit(self, event_class: WorkflowEventClass) -> None:
        """Emit one lifecycle event of the given canonical class."""
        ...


@runtime_checkable
class StepDispatcher(Protocol):
    """Step body dispatch surface (per C-CP-25 §25.3.3.4 + U-CP-01 router seam).

    The driver delegates step body invocation through this Protocol. Concrete
    implementations live above the driver (typically in the runtime composition
    layer, which knows about the cap-aware router U-CP-01, the sandbox
    dispatch, the HITL gate, etc.).

    **v1.6 Path A amendment.** `step_context: StepExecutionContext` is a
    keyword-only parameter carrying per-step parent context composed by the
    driver from run-level state. Required for sub-agent dispatch composer
    (C-RT-17) per C-CP-12 §12.2 gate-level composition + C-CP-13 §13.5
    audit-trail-link composition. Existing dispatchers (C-RT-15 inner LLM
    dispatch, C-RT-16 retry/breaker/fallback wrapper) accept the parameter
    but do not consume it at v1.6; the parameter is reserved for v1.7+
    surfaces that may bind step context to the LLM inference span attributes or
    similar. See:
    `.harness/class_1_tension_c_rt_17_step_dispatcher_parent_context_gap.md`
    for the resolution rationale.
    """

    def dispatch(
        self,
        binding: StepEffectiveBinding,
        step: WorkflowStep,
        *,
        step_context: StepExecutionContext,
    ) -> Mapping[str, Any]:
        """Invoke the step body under the effective binding; return step output.

        Step output is a mapping; the driver accumulates these into the
        terminal `partial_state` / `final_state` of the returned `RunResult`.

        `step_context` carries per-step parent context composed by the driver
        per `StepExecutionContext` per-field semantics (8 fields; 4 composed
        deterministically + 4 MVP-default-bounded). Dispatchers may ignore
        the parameter at v1.6 if they do not need parent context (the C-RT-15
        LLM dispatcher does); dispatchers that need parent context (the
        C-RT-17 sub-agent dispatcher) consume it.
        """
        ...


class StepKindDispatcherNotBoundError(Exception):
    """No dispatcher bound for a `StepKind` at registry lookup (U-RT-59 §14.7).

    Raised by a `StepDispatcherRegistry.lookup(step_kind)` implementation when
    `step_kind` is not bound. The driver's try/except per C-CP-25 §25.3.3.4
    maps this to a `step-failure: RT-FAIL-STEP-KIND-DISPATCHER-NOT-BOUND: ...`
    per `Spec_Harness_Runtime_v1.md` v1.6 §14.7 failure-mode taxonomy.

    Declared CP-side (vs runtime-side) so the driver's typed `try/except` can
    catch a CP-owned error without inverting the CP→runtime dependency
    direction. Runtime's `StepKindDispatcherRegistry.lookup` raises this same
    type (imports from here).
    """

    def __init__(self, step_kind: StepKind) -> None:
        super().__init__(f"no StepDispatcher bound for step_kind {step_kind.value!r}")
        self.step_kind = step_kind


@runtime_checkable
class StepDispatcherRegistry(Protocol):
    """Routing-layer surface — frozen `{StepKind → StepDispatcher}` mapping.

    Per `Spec_Harness_Runtime_v1.md` v1.6 §14.7.1 + §14.7.7 (C-RT-17). The
    driver invokes `step_dispatchers.lookup(step.kind)` at every per-step
    dispatch site; the returned `StepDispatcher` then dispatches the step
    body via its sync `dispatch(binding, step, *, step_context)` method.

    Structurally satisfied by
    `harness_runtime.lifecycle.step_dispatchers.StepKindDispatcherRegistry`
    (the production composition; bound at bootstrap stage 5 to
    `HarnessContext.step_dispatchers`). The CP driver does not import the
    runtime composition (which would invert the CP→runtime dependency
    direction); it consumes via this Protocol.

    **v1.6 amendment.** Replaces the v1.5 single `step_dispatcher:
    StepDispatcher` parameter at `execute_workflow`. Per spec §14.7.7
    "Driver routing-layer refactor": "Parameter changes from `step_dispatcher:
    StepDispatcher` to `step_dispatchers: StepKindDispatcherRegistry`."
    """

    def lookup(self, step_kind: StepKind) -> StepDispatcher:
        """Return the bound dispatcher for `step_kind`.

        Raises
        ------
        StepKindDispatcherNotBoundError
            `step_kind` is not bound in this registry; driver maps to
            `RT-FAIL-STEP-KIND-DISPATCHER-NOT-BOUND`.
        """
        ...


@runtime_checkable
class DriverContext(Protocol):
    """Minimal substrate the driver consumes (subset of HarnessContext).

    Structurally satisfied by
    `harness_runtime.types.HarnessContext`. The CP driver does not import
    `HarnessContext` (which would invert the CP→runtime dependency direction);
    it consumes the substrate via this Protocol.

    `drained_flag` is consumed at U-CP-57 drain composition at the 3 driver
    boundary sites per `Spec_Control_Plane_v1_4.md` §25.4. U-CP-56 happy-path
    iteration never sets this flag itself (per U-CP-57 AC #6 — "Driver never
    calls `ctx.drained_flag.set()` itself"; flag ownership at U-RT-44 signal
    handler per `Spec_Harness_Runtime_v1.md` §11 C-RT-11).
    """

    ledger_writer: LedgerWriterLike
    ledger_reader: LedgerReaderLike
    lifecycle_emitter: LifecycleEventEmitterLike
    drained_flag: asyncio.Event
    # OTel `TracerProvider` substrate per C-OD-25 §25.2 (OD spec v1.8).
    # Driver opens the `workflow.envelope` outer span via
    # `tracer_provider.get_tracer("harness.cp.workflow_driver")`. Typed as
    # `object` to avoid pulling the OTel SDK into the CP protocol surface
    # (HarnessContext exposes the materialized provider with the same
    # `object`-typed field per harness-runtime/types.py).
    tracer_provider: object

    # OPTIONAL ValidatorFramework — operator-opt-in per C-CP-25 §25.3 +
    # Decision 2.D3 (Phase A.2 RATIFIED). When None, the U-CP-61 post-dispatch
    # validation hook is skipped (driver-level opt-out). When bound, the
    # operator-populated validator_registry must cover every step.step_id
    # (Decision 2.D3 in-band opt-out is via no-op validator at registry, not
    # at framework binding). Typed as `object | None` so the CP Protocol does
    # not import the sync facade type — concrete binding at runtime stage 5
    # uses `harness_cp.validator_framework.SyncValidatorFrameworkFacade`
    # (structural match via the `.evaluate(...)` sync method per
    # `SyncValidatorFrameworkLike` Protocol).
    validator_framework: object | None

    # OPTIONAL PauseResumeProtocol — U-RT-87 (v2.20) operator-opt-in per
    # runtime spec v1.21 §14.14.3 workflow_driver per-step pre-entry
    # pause-trigger detection point. When None (default), the per-step
    # pre-entry pause-trigger detection branch sibling to
    # `drained_flag.is_set()` evaluates False (driver-level opt-out;
    # production-default state preserved per spec §14.14.5 invariant 2).
    # When bound, the driver invokes `protocol.capture_pause_snapshot(...)`
    # on `pause_requested_flag.is_set()` + returns `RunStatus.PAUSED`.
    # Typed as `object | None` to avoid pulling
    # `harness_cp.pause_resume_protocol.PauseResumeProtocol` into the
    # Protocol surface (HarnessContext exposes the typed narrowed field
    # per runtime spec v1.21 §4).
    pause_resume_protocol: object | None

    # U-RT-87 (v2.20) caller-side pause-signaling primitive sibling-pattern
    # to `drained_flag` per runtime spec v1.21 §14.14.3. Set by external
    # caller to request driver pause at next per-step pre-entry; polled by
    # the driver as a sibling check to `drained_flag.is_set()`.
    pause_requested_flag: asyncio.Event

    # Multi-tenant scoping key sourced from `RuntimeConfig.tenant_id`. None =
    # single-tenant (the v1.6 MVP default; preserved unchanged at audit-writer
    # via the `_SINGLE_TENANT_TAG` sentinel). Non-None values flow through the
    # 4-substep audit composition (sub_agent_dispatch.py / hitl_gate_composer.py
    # / llm_dispatch.py / audit_writer.py) via `StepExecutionContext.tenant_id`
    # propagation. HarnessContext exposes this as a computed property reading
    # `self.config.tenant_id` so DriverContext is structurally satisfied
    # without duplicating storage. Per workflow_driver_types.py:189-192
    # deferral comment, this is the v1.7+ extension that lifts the v1.6 MVP
    # hardcode at the workflow_driver composition site (binding fix; not a
    # WorkflowManifestEntry schema extension).
    tenant_id: str | None

    # U-RT-101 (C-RT-27 §14.17.2 hook-1 per-workflow-init) — Skill activation
    # emitter + loaded skills. Both default-None-safe at the binding-site
    # arm; structurally satisfied by HarnessContext.skill_activation_emitter
    # + ctx.skills per runtime spec v1.32 §4. Typed as `object | None` to
    # avoid pulling harness_runtime.lifecycle.skill_activation into the CP
    # Protocol surface. When None, the per-workflow-init hook silent-skips
    # per §14.17.5 invariant 3 (operator opt-out path preserved).
    skill_activation_emitter: object | None
    skills: object

    # U-RT-111 (v2.36) — `RuntimeCpIsWiring` carrier per runtime plan v2.36 §1.2
    # ACs #3 + #11. Operator-opt-in MVP; default `None` preserves pre-v2.36
    # production behavior (workflow_driver pause/resume sites silent-skip
    # emission). Typed `object | None` to avoid pulling
    # `harness_runtime.lifecycle.cp_is_wiring.RuntimeCpIsWiring` into the CP
    # Protocol surface (workspace dep-graph discipline — harness-cp does NOT
    # depend on harness-runtime per `harness-cp/pyproject.toml`). When bound,
    # the 3 pause/resume firing sites at workflow_driver.py:546 + :756 + :881
    # invoke `ctx.cp_is_wiring.emit_pause_resume_state_ledger_entry(...)` via
    # `_run_protocol_method_sync(...)` per the same sync-bridging discipline as
    # `protocol.attempt_resume(...)` + `protocol.capture_pause_snapshot(...)`.
    cp_is_wiring: object | None

    # R-003 producer-site lift — zero-arg resolver returning the
    # `procedural_tier_snapshot_ref` Identifier D-derivative sidecar per IS
    # spec v1.3 §C-IS-05 §5.1. Consumed at `_append_step_ledger_entry`
    # (§25.3.3.7 per-step state-ledger write — a workflow-context emission).
    # Typed `object | None` to avoid pulling
    # `harness_runtime.lifecycle.procedural_tier_snapshot` into the CP Protocol
    # surface (workspace dep-graph discipline — harness-cp does NOT depend on
    # harness-runtime). When bound (production, via the frozen `HarnessContext`
    # field set at bootstrap stage 6), `_append_step_ledger_entry` invokes it
    # and populates the sidecar; when `None` (operator opt-out / outside-
    # workflow / test ctx), the sidecar stays `None`.
    procedural_tier_snapshot_resolver: object | None

    # B-INTERSTEP (runtime spec §14.21 C-RT-34) — run-scoped inter-step output
    # channel (the shared run-context a dispatcher reads). The driver records each
    # completed step's output here; the runtime LLM dispatcher reads
    # `most_recent_output()` and injects the prior step's output into the
    # dispatched payload (making EVALUATOR_OPTIMIZER's draft→evaluate /
    # feedback→regenerate data flow real). Typed `object | None` to avoid pulling
    # `harness_runtime.lifecycle.inter_step_output_channel` into the CP Protocol
    # surface (harness-cp does NOT depend on harness-runtime). When `None`
    # (operator opt-out / test ctx; `RuntimeConfig.inter_step_data_flow=False`
    # default), the driver records nothing (byte-identical to pre-v1.59). When
    # bound (frozen `HarnessContext` field set at stage 5 LOOP_INIT), the driver
    # calls `.record(step_id, step_output)` after each completed step (consumed
    # via `getattr` dynamic dispatch, the `cp_is_wiring` idiom).
    inter_step_output_channel: object | None

    # B-EFFECT-FENCE-PAUSE-RESOLUTION (§14.22.9) — the runtime `ResumeContextHolder`
    # (the one-shot operator-resume-context sidecar). On an effect-fence-ambiguous-pause
    # resume the driver PEEKS it (does NOT consume — the runtime HITL composer owns the
    # one-shot `consume_and_clear`) to extract `effect_fence_resolution`. Typed
    # `object | None` to avoid pulling
    # `harness_runtime.lifecycle.resume_context_holder` into the CP Protocol surface
    # (harness-cp does NOT depend on harness-runtime). When `None` (test ctx / no holder),
    # the driver threads no resolution → the dispatcher's INERT re-pause is preserved.
    resume_context_holder: object | None


# ---------------------------------------------------------------------------
# Driver core
# ---------------------------------------------------------------------------


@runtime_checkable
class _ResumeContextHolderLike(Protocol):
    """The minimal `ResumeContextHolder` surface the driver peeks (NOT consumes).

    B-EFFECT-FENCE-PAUSE-RESOLUTION — `peek()` returns the current `ResumeContext`
    without clearing it, so the runtime HITL composer's one-shot `consume_and_clear`
    stays intact (a step with both a HITL gate and a fenced tool dispatch still
    delivers its HITL response). Typed against the CP-side `ResumeContext` (harness-cp
    owns it); the runtime `ResumeContextHolder` structurally satisfies this."""

    def peek(self) -> ResumeContext | None: ...


def _compute_run_idempotency_key(
    run_id: str,
    workflow_id: str,
    *,
    extras: Sequence[str] = (),
) -> str:
    """Compose the run-scope idempotency key per C-CP-25 §25.6.

    `run_idempotency_key = sha256(run_id, workflow_id, *extras)`. The manifest
    entry does not carry an `entry_version` field at v1.4 — the extras
    parameter is the extension hook for a future workflow-versioning field.
    """
    h = hashlib.sha256()
    h.update(run_id.encode("utf-8"))
    h.update(b"\x00")
    h.update(workflow_id.encode("utf-8"))
    for extra in extras:
        h.update(b"\x00")
        h.update(extra.encode("utf-8"))
    return h.hexdigest()


def _compute_step_idempotency_key(
    run_idempotency_key: str,
    step_index: int,
    branch_path: str | None = None,
) -> str:
    """Per-step `idempotency_key = sha256(run_idempotency_key, step_index[, branch_path])`
    per C-CP-25 §25.3.3.7 + §25.6 + §25.16 (branch-scoped extension).

    `branch_path` (U-CP-83 / §25.16) enters the composition under fan-out so N
    parallel branches at the *same declared `step_index`* do not collapse to one
    ledger entry under the IS writer's `idempotency_key`-only dedup
    (C-IS-07 §7.5). It derives from the branch identity via
    `workflow_driver_types.compose_branch_path`. The `SINGLE_THREADED_LINEAR`
    path passes `branch_path=None` and composes the existing
    `sha256(run_idempotency_key, step_index)` key **byte-identically**
    (regression-safe — no extra separator is hashed when `branch_path is None`).
    This is a CP-side driver write-key composition change only — no six-field /
    hash-chain / ADR change (§25.16).
    """
    h = hashlib.sha256()
    h.update(run_idempotency_key.encode("utf-8"))
    h.update(b"\x00")
    h.update(str(step_index).encode("utf-8"))
    if branch_path is not None:
        h.update(b"\x00")
        h.update(branch_path.encode("utf-8"))
    return h.hexdigest()


def _record_inter_step_output(
    ctx: DriverContext, step_id: str, step_output: Mapping[str, Any]
) -> None:
    """B-INTERSTEP (runtime spec §14.21 C-RT-34) — record a completed step's output
    to the run-scoped inter-step channel so a subsequent dispatch can read it.

    Operator-opt-in: `ctx.inter_step_output_channel` is `None` by default
    (`RuntimeConfig.inter_step_data_flow=False`) → no-op, byte-identical to
    pre-v1.59. Consumed via `getattr` dynamic dispatch (the `cp_is_wiring` idiom —
    harness-cp does not import the runtime `InterStepOutputChannel` holder). The
    driver does NOT introspect the step body — it records the dispatcher's already-
    produced opaque output Mapping (`workflow_driver` §25.3.3.4 preserved)."""
    _channel = getattr(ctx, "inter_step_output_channel", None)
    if _channel is not None:
        _channel.record(step_id, step_output)


def _record_durable_step_output(
    ctx: DriverContext,
    run_idempotency_key: str,
    step_index: int,
    step_id: str,
    step_output: Mapping[str, Any],
) -> None:
    """B-ENGINE-OUTPUT-REPLAY (runtime spec C-RT-32) — durably persist a completed
    step's output to the output-carrying event-history store.

    Called BEFORE `_append_step_ledger_entry` (the F2 materialization point
    `resume_at` counts) — the RESERVE-before-COMMIT skew discipline: the store
    always holds ≥ the ledger's materialized prefix, so an EVENT_SOURCED_REPLAY
    resume never finds a materialized step with a missing output (a crash AFTER
    the store-write but BEFORE the ledger-append leaves an extra uncommitted store
    record, which `resume_at`-driven rehydration ignores). Keyed by
    `run_idempotency_key` (stable across the EVENT_SOURCED_REPLAY restart — the
    same id the resume join uses). Operator-opt-in: `ctx.engine_output_store` is
    `None` by default → no-op. Consumed via `getattr` (the `cp_is_wiring` idiom —
    harness-cp does not import the runtime store)."""
    _store = getattr(ctx, "engine_output_store", None)
    if _store is not None:
        _store.record(run_idempotency_key, step_index, step_id, step_output)


# B-FANOUT-OUTPUT-REPLAY (R-FS-1) — the engine classes whose fan-out run captures its per-branch
# terminals to the durable branch store AND reconstructs its AGGREGATE on crash-resume. The store
# (`EngineOutputStore`, the SAME class-agnostic `record_branch`/`read_branch_records` substrate the
# LINEAR `_record_durable_step_output` uses) is mechanically engine-class-AGNOSTIC, and the fan-out
# aggregate reconstruction (`_determine_fanout_resume`) consumes ONLY that store — NOT the §8.1
# cached-output-replay inter-step channel (which is ESR/WAL-only). SAVE_POINT_CHECKPOINT JOINS
# ESR/WAL here (the `…-FANOUT-CHILD-SAVE-POINT` slice, R-FS-1): SAVE_POINT is the §11.2 ABOVE_ENGINE
# reading (harness composes lease + dedup + resumption → the harness branch store is the SOLE
# aggregate authority, no competing engine-owned substrate), its §14.22 effect fence is auto-active
# (SAVE_POINT ∈ the runtime `_DURABLE_AUTO_FENCE_ENGINE_CLASSES` → in-flight branch re-dispatch is
# at-most-once-safe), and it fires NO recovery loop / CAS-claim (no F-1 window). RECONCILER_LOOP
# JOINS here (the `…-FANOUT-CHILD-RECONCILER` close): the reconciler substrate owns convergence/CAS
# state, not the per-branch output map, so the class-agnostic branch store remains the SOLE fan-out
# AGGREGATE authority; each branch's own RECONCILER crash-resume keeps CAS/F-1 fail-closed.
# Widening this SHARED constant moves the capture gate
# (`_fanout_replay_store`) AND the recoverability predicate (`_fanout_recoverable`) in lockstep —
# the two halves of one mechanism share their scope key
# (`[[durable-recovery-presence-validity-scope]]`). The CP↔runtime mirror is the runtime
# `_SUBAGENT_RECOVERABLE_FANOUT_CHILD_ENGINE_CLASSES` (the agreement witness enforces parity).
_FANOUT_REPLAY_ENGINE_CLASSES: frozenset[EngineClass] = frozenset(
    {
        EngineClass.EVENT_SOURCED_REPLAY,
        EngineClass.WAL_SEGMENT,
        EngineClass.SAVE_POINT_CHECKPOINT,
        EngineClass.RECONCILER_LOOP,
    }
)

# B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT-RECONCILER-CHILD (R-FS-1) — the child engine classes
# whose maybe-ran SUB_AGENT_DISPATCH re-dispatch is RECOVERABLE: re-dispatching the child under
# the deterministic child run_id auto-resumes it from its durable store (`resume_at>0` via the
# engine-class-agnostic F2-prefix join, `_determine_resume_at`) AND reconstructs a result-faithful
# `final_state` (no parent-fold corruption). ALL FOUR durable resumable engine classes are now
# members (PURE_PATTERN_NO_ENGINE, the lone non-durable class with no resume, is the sole
# non-member). SAVE_POINT_CHECKPOINT joined ESR/WAL at the `…-SAVE-POINT-CHILD` close;
# RECONCILER_LOOP JOINS here. The RECONCILER admission is at-most-once-safe WITHOUT first the F-1
# engine-lock arc: a re-dispatched maybe-ran RECONCILER child runs its OWN crash-resume, which
# fires the U-CP-97 engine-layer reconverge (`attempt_resume`) gated AT THE CAS CLAIM, upstream of
# the step loop (`reconciler_pause_resume_substrate.py` F-1). Three exhaustive re-dispatch cases,
# all safe: (1) child never won a claim → cleanly RE-CLAIMs the revision → auto-resumes the
# committed prefix (F2-skipped, not re-fired); (2) clean RESUME_CLEAN → same; (3) the F-1 window —
# the child WON the claim then crashed mid-re-execution → the retry of the already-claimed revision
# ABORTs (`ABORT_REVALIDATION_FAILED`) → the child returns RunStatus.FAILED *before any step
# re-executes* (at-most-once preserved, NEVER a double-fire) → the parent fold raises
# `SubAgentChildFailedError` (fail-closed; never a SUCCESS aggregate from a failed child). So
# admitting RECONCILER strictly IMPROVES the not-won-claim cases (recover vs fail-the-parent-closed)
# and routes the F-1 window through the SAME already-on-main RECONCILER-resume ABORT→§22.1-HITL
# disposition (#779/#781) — the registered F-1 engine-lock auto-recovery arc improves ALL RECONCILER
# resumes and is NOT a prerequisite for this child-recoverability slice. This set is DEDICATED to
# the LINEAR recoverability conjunct; `_FANOUT_REPLAY_ENGINE_CLASSES` is the separate fan-out
# aggregate gate. They currently contain the same four durable classes, but they remain separate
# authorities because the LINEAR path seeds `reconstruct_final_state` while the fan-out path
# consumes the branch-output store. The CP-side MIRROR of the runtime
# `_SUBAGENT_RECOVERABLE_CHILD_ENGINE_CLASSES` (agreement witness enforces parity).
_SUBAGENT_RECOVERABLE_CHILD_ENGINE_CLASSES: frozenset[EngineClass] = frozenset(
    {
        EngineClass.EVENT_SOURCED_REPLAY,
        EngineClass.WAL_SEGMENT,
        EngineClass.SAVE_POINT_CHECKPOINT,
        EngineClass.RECONCILER_LOOP,
    }
)
# B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT-FANOUT-CHILD (R-FS-1) — the FAN-OUT child topologies a
# maybe-ran SUB_AGENT_DISPATCH re-dispatch can recover. A fan-out child does NOT reach the LINEAR
# `reconstruct_final_state` seed (the concurrent strategies return before it); instead its AGGREGATE
# `final_state` reconstructs through the SEPARATE B-FANOUT-OUTPUT-REPLAY branch store at the fan-out
# crash-resume site (`_crash_fan_out_resume`, this module): on the parent's re-dispatch the child
# re-runs under its deterministic `child_run_id` → its captured branches replay (fire-once), its
# in-flight branches re-dispatch through the child's OWN maybe-ran machinery (re-fire-safe fresh,
# effect-bearing fenced) → the aggregate folds result-faithfully into the parent. EXACTLY the three
# concurrent strategies `_crash_fan_out_resume` reconstructs; EVALUATOR_OPTIMIZER and
# DECENTRALIZED_HANDOFF have no fan-out replay store → stay fail-closed. This recovery is gated to
# `_FANOUT_REPLAY_ENGINE_CLASSES` ({ESR,WAL,SAVE_POINT,RECONCILER}) — the classes with a fan-out
# replay store. SAVE_POINT joined at the `…-FANOUT-CHILD-SAVE-POINT` slice; RECONCILER joins at the
# `…-FANOUT-CHILD-RECONCILER` close because the branch-output store, not the reconciler substrate,
# owns the aggregate output map.
_SUBAGENT_RECOVERABLE_FANOUT_CHILD_TOPOLOGIES: frozenset[TopologyPattern] = frozenset(
    {
        TopologyPattern.PARALLELIZATION,
        TopologyPattern.ORCHESTRATOR_WORKERS,
        TopologyPattern.HIERARCHICAL_DELEGATION,
    }
)

# B-CHILD-CRASH-RESUME-FINAL-STATE-RECONSTRUCT-RECONCILER (R-FS-1) — the engine classes
# whose per-step output is durably recorded to the `EngineOutputStore` AND seeded back into
# `accumulated` on resume so the resumed run's `final_state` reconstructs the COMPLETE
# terminal state (CP v1.76 §25.2/§25.6 resume-transparency invariant, extended to
# SAVE_POINT_CHECKPOINT at v1.79 and RECONCILER_LOOP at v1.80 — ALL FOUR durable resumable
# engine classes now reconstruct; PURE_PATTERN_NO_ENGINE is the lone non-member, a non-durable
# class with no resume). This single constant gates BOTH the `_record_durable_step_output`
# producer AND the final_state seed site so the two can NEVER drift apart — the documented
# "never record-only / never seed-only" invariant made structural (a never-rehydrated journal
# is the exact defect the move-together discipline prevents). EVENT_SOURCED_REPLAY / WAL_SEGMENT
# additionally feed the §8.1 cached-output-replay inter-step channel rehydrate (its own
# ESR/WAL-gated branch); SAVE_POINT_CHECKPOINT and RECONCILER_LOOP have no cached-output-replay
# semantic — their journal is consumed ONLY by the final_state seed, the durable-output sink the
# EngineOutputStore is mechanically class-agnostic about. The EngineOutputStore binds whenever
# `RuntimeConfig.engine_output_replay` is on, independent of engine class — so RECONCILER
# reconstruction is a CP-side gate extension, no runtime edit (the same class-agnostic store).
#
# RECONCILER_LOOP joining resolved the registered "two output authorities" probe (the advisor
# two-authorities flag): RECONCILER is an ENGINE-OWNS-SUBSTRATE class whose authoritative durable
# state lives in the U-RT-123 reconciler substrate — but that substrate persists a `StateSummary`
# DIGEST (`summary_text` + `summary_hash` + ledger-entry refs) in its `PauseEvent`, for the
# CAS-lease + revalidation, NOT the per-step `accumulated` output map. The reconciler substrate
# is the authority for engine-layer CONVERGENCE state (revision-stamped, claim-the-revision);
# the EngineOutputStore is the authority for the CP per-step OUTPUT map that builds final_state.
# They measure different things → no one-source-of-truth violation, even though both populate
# during the same run. (`derive-from-the-reconciler-substrate` is non-viable: the per-step
# outputs are not in the digest to derive.) A RECONCILER run flows through the SAME linear
# dispatch loop building `accumulated` per-step, so reconstructing it on resume is identical to
# ESR/WAL/SAVE_POINT and consistent with a non-resumed RECONCILER run's own final_state shape.
# (Corrects the prior comment's stale "`partial_state` in its PauseEvent" mischaracterization,
# the exact text that had misframed the registration as a competing authority.)
_FINAL_STATE_RECONSTRUCT_ENGINE_CLASSES: frozenset[EngineClass] = frozenset(
    {
        EngineClass.EVENT_SOURCED_REPLAY,
        EngineClass.WAL_SEGMENT,
        EngineClass.SAVE_POINT_CHECKPOINT,
        EngineClass.RECONCILER_LOOP,
    }
)


# B-FANOUT-CRASH-RESUME-MAYBE-RAN-RESOLUTION (R-FS-1) — the fan-out branch step kinds whose
# RE-DISPATCH cannot double-fire a non-idempotent EXTERNAL effect, so a MAYBE-RAN branch
# (dispatched-but-uncaptured — its effect MAY have fired) of this kind is SAFE to re-dispatch
# fresh instead of failing the whole strict-tier incomplete recovery closed.
#
# Anchored to the runtime step-blast-radius one-source-of-truth (`step_blast_radius.py:13-22`
# `_READ_ONLY_KINDS`: "INFERENCE_STEP / DECLARATIVE_STEP — no external effect"): an LLM
# inference has no external side-effect (cost + output non-determinism only, which crash-resume
# ALREADY tolerates — the linear resume path re-dispatches an INFERENCE_STEP unfenced, the
# §14.8.8.7 invariant-3 re-ask-per-retry posture), and a DECLARATIVE_STEP is a pure transform.
#
# DELIBERATELY a STRICT SUBSET of the blast-radius READ_ONLY set (re-fire-safety ≠ READ_ONLY
# blast radius): HITL_STEP is READ_ONLY-blast but operator-facing (re-dispatch re-prompts);
# SUB_AGENT_DISPATCH is READ_ONLY at the PARENT gate but its child TOOL_STEPs are fenced at the
# tool sink, so re-dispatch can hit the inner-fence-ambiguous window → the registered
# B-FANOUT-CRASH-RESUME-MAYBE-RAN-FENCED-COMPOSE follow-on. TOOL_STEP is likewise fenced →
# same follow-on. MANAGED_AGENTS performs an effect-bearing vendor-session dispatch (create +
# send) now FENCED at its own §14.22 sink (the B-FANOUT-CRASH-RESUME-MAYBE-RAN-UNFENCED-EXTERNAL
# build) → it is fence-RECOVERABLE, not re-fire-safe (see
# `_FANOUT_MAYBE_RAN_FENCE_RECOVERABLE_KIND_VALUES`). The allowlist is CONSERVATIVE: any unlisted
# (or out-of-bounds) kind stays fail-closed.
_FANOUT_MAYBE_RAN_REFIRE_SAFE_KINDS: frozenset[StepKind] = frozenset(
    {StepKind.DECLARATIVE_STEP, StepKind.INFERENCE_STEP}
)
# The same set as `.value` strings — the dispatch marker records the step kind as a string
# (`StepKind.value`), so the classifier compares the marker's recorded kind against these.
_FANOUT_MAYBE_RAN_REFIRE_SAFE_KIND_VALUES: frozenset[str] = frozenset(
    k.value for k in _FANOUT_MAYBE_RAN_REFIRE_SAFE_KINDS
)


def _refire_unsafe_branch_indices(
    branch_indices: set[int],
    dispatched_kinds: Mapping[int, str | None],
    branch_count: int,
) -> set[int]:
    """B-FANOUT-CRASH-RESUME-MAYBE-RAN-RESOLUTION (R-FS-1) — the subset of `branch_indices`
    (maybe-ran / dispatched fan-out branch ordinals on a strict-tier crash-resume) that are NOT
    safe to re-dispatch, so falling through would risk double-firing a non-idempotent effect.

    An ordinal is SAFE iff it is BOTH (a) within the valid fan-out range `[0, branch_count)`
    AND (b) recorded in the DISPATCH MARKER with a re-fire-safe kind
    (`_FANOUT_MAYBE_RAN_REFIRE_SAFE_KIND_VALUES`). Everything else is unsafe → fail closed:
    - keys on the kind recorded IN THE DISPATCH MARKER (`dispatched_kinds`, from
      `EngineOutputStore.dispatched_branch_kinds`), NOT the resumed manifest's current kind —
      the at-most-once changed-manifest guard (out-of-family Codex [P1]): a branch dispatched as
      an effect-bearing kind that crashed before terminal capture, then re-supplied at the same
      ordinal as a re-fire-safe kind on a same-cardinality resume, STAYS classified by its
      original effect-bearing kind. A missing / unknown / `None` recorded kind (a pre-arc
      v1.60/v1.61 marker, or a torn write) is unsafe.
    - an ordinal OUTSIDE `[0, branch_count)` is an out-of-range / stale-store marker (a corrupt
      extra `branch-N.dispatched` for a smaller fan-out) — the store no longer matches the
      declared fan-out → unsafe, never silently ignored (out-of-family Codex [P2]).

    Shared by the incomplete-recovery + cardinality-only classification sites so the
    re-fire-safety classification is one source of truth."""
    return {
        bi
        for bi in branch_indices
        if not (
            0 <= bi < branch_count
            and dispatched_kinds.get(bi) in _FANOUT_MAYBE_RAN_REFIRE_SAFE_KIND_VALUES
        )
    }


# B-FANOUT-EFFECT-FENCE-BRANCH-PAUSE (R-FS-1) — the maybe-ran fan-out branch kinds whose
# RE-DISPATCH is at-most-once-safe NOT because they have no effect (those are the re-fire-safe
# set above) but because their effect is FENCED at the runtime tool sink (C-RT-31 §14.22): a
# re-dispatch re-reaches the fence, whose `try_reserve` LOSES on the prior attempt's held claim
# and SPLITS — suppress-and-continue (output captured ⟹ effect completed), ambiguous-PAUSE (the
# B-FANOUT-EFFECT-FENCE-BRANCH-PAUSE fan-out analogue of the linear B-EFFECT-FENCE-HITL-ROUTE),
# or fresh-fire (claim ABSENT ⟹ the prior attempt crashed BEFORE the fence reserve ⟹ the effect
# did not fire). TWO kinds reach a fence DIRECTLY at their own sink: TOOL_STEP (its dispatch hits
# the runtime tool-fence) and MANAGED_AGENTS (its vendor-session dispatch — create + send — is now
# wrapped in the SAME §14.22 fence keyed on (parent_idempotency_key, step_id), the
# B-FANOUT-CRASH-RESUME-MAYBE-RAN-UNFENCED-EXTERNAL build; a LEAF effect — no harness-side
# reconstruction — so its suppress folds the captured outcome verbatim, full result-fidelity).
# SUB_AGENT_DISPATCH is fenced only at its CHILD's tool sinks (recursive child crash-resume — a
# larger, separately-verified mechanism → the registered B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT
# follow-on). The fence is AUTO-ACTIVE here: a fan-out crash-resume is reachable ONLY for
# `_FANOUT_REPLAY_ENGINE_CLASSES`, a subset of the runtime's durable-auto-fence set, and a worker
# child context inherits `run_engine_class` (so the fence gate is open for the re-dispatch).
_FANOUT_MAYBE_RAN_FENCE_RECOVERABLE_KIND_VALUES: frozenset[str] = frozenset(
    {StepKind.TOOL_STEP.value, StepKind.MANAGED_AGENTS.value}
)


def _resumed_branch_kinds_by_ordinal(
    branch_steps: Sequence[WorkflowStep], *, branch_count: int, orchestrated: bool
) -> dict[int, str]:
    """B-FANOUT-EFFECT-FENCE-BRANCH-PAUSE (R-FS-1) — map each WORKER/PEER branch ordinal
    (`[0, branch_count)`, the marker's keying scheme) to the RESUMED manifest's `step_kind`
    at that ordinal. ORCHESTRATOR_WORKERS / HIERARCHICAL_DELEGATION carry the orchestrator at
    `branch_steps[0]`, so worker ordinal `bi` is `branch_steps[bi + 1]` (offset 1); a PEER fan-out
    has no orchestrator (offset 0). An ordinal whose mapped step is out of range is OMITTED (left
    to the helper's fail-closed default — never silently treated as TOOL)."""
    offset = 1 if orchestrated else 0
    return {
        bi: branch_steps[bi + offset].step_kind.value
        for bi in range(branch_count)
        if 0 <= bi + offset < len(branch_steps)
    }


def _resumed_branch_step_ids_by_ordinal(
    branch_steps: Sequence[WorkflowStep], *, branch_count: int, orchestrated: bool
) -> dict[int, str]:
    """B-FANOUT-CRASH-RESUME-PAUSE-RECONSTRUCT-MAYBE-RAN-FENCE-STEP-ID (R-FS-1) — the step_id
    sibling of `_resumed_branch_kinds_by_ordinal`: map each WORKER/PEER branch ordinal to the
    RESUMED manifest's `step_id` at that ordinal, so `_fence_unrecoverable_maybe_ran_indices` can
    require the dispatch-marker step_id to equal the resumed step_id (the changed-step_id guard).
    Same orchestrator-offset convention (ORCHESTRATOR_WORKERS / HIERARCHICAL carry the orchestrator
    at `branch_steps[0]`, so worker ordinal `bi` is `branch_steps[bi + 1]`; a PEER fan-out has no
    orchestrator → offset 0). An ordinal whose mapped step is out of range is OMITTED (the helper's
    fail-closed default — a missing resumed step_id never equals the marker's, so the ordinal stays
    unrecoverable)."""
    offset = 1 if orchestrated else 0
    return {
        bi: str(branch_steps[bi + offset].step_id)
        for bi in range(branch_count)
        if 0 <= bi + offset < len(branch_steps)
    }


def _opaque_field(obj: Any, key: str) -> Any:
    """Read `key` from an opaque step-like / manifest-like that may be a serialized mapping
    (`obj[key]`) or a typed carrier (`obj.key`). Raises KeyError/AttributeError/TypeError on miss
    (caught by `_subagent_child_recoverable`'s outer fail-closed guard). Keeps the CP↔runtime read
    pyright-clean on the layering boundary (the CP driver does not own the runtime payload type)."""
    try:
        return obj[key]
    except (TypeError, KeyError):
        return getattr(obj, key)


def payload_child_recoverable(cme: Any, child_steps: Any) -> bool:
    """The SHARED recursive recoverability predicate (the NONLEAF-CHILD arc, R-FS-1) over an opaque
    child manifest entry + child step sequence (serialized mapping OR typed carrier —
    `_opaque_field` reads both). The three conjuncts — engine ∈
    `_SUBAGENT_RECOVERABLE_CHILD_ENGINE_CLASSES`;
    topology == SINGLE_THREADED_LINEAR; every child step non-MANAGED_AGENTS and every nested
    SUB_AGENT_DISPATCH child step ITSELF recoverable (recursive descent, bottoming out at a LINEAR
    leaf). Any read/parse failure at ANY depth propagates to the caller's fail-closed guard.

    ONE SOURCE OF TRUTH (out-of-family Codex [P1], NONLEAF-CHILD): the CP entry
    `_subagent_child_recoverable` AND the runtime composer's nested recursion in
    `subagent_child_recoverable` (`sub_agent_dispatch.py`) BOTH delegate the nested-payload decision
    here, so a partially-valid nested grandchild payload (valid `child_manifest_entry`/`child_steps`
    but missing `child_workflow_id`/`brief`) is classified IDENTICALLY on both sides. A runtime-only
    `SubAgentDispatchPayload.model_validate` of the nested payload would reject what this defensive
    read admits (CP cannot model_validate — a forbidden `harness_cp`→`harness_runtime` import) →
    CP-True/runtime-False → the outer child gets NO seed → double-fire on re-dispatch.
    Recoverability
    depends ONLY on engine+topology+child_steps; `child_workflow_id`/`brief` affect DISPATCHABILITY
    (fail closed at the dispatcher's own model_validate), not recoverability."""
    ec_raw: Any = _opaque_field(cme, "engine_class")
    ec = ec_raw if isinstance(ec_raw, EngineClass) else EngineClass(ec_raw)
    if ec not in _SUBAGENT_RECOVERABLE_CHILD_ENGINE_CLASSES:
        return False
    tp_raw: Any = _opaque_field(cme, "topology_pattern")
    tp = tp_raw if isinstance(tp_raw, TopologyPattern) else TopologyPattern(tp_raw)
    # Conjunct 2 — the recoverable topology∩engine intersection (the FANOUT-CHILD relaxation,
    # R-FS-1):
    #   • SINGLE_THREADED_LINEAR → all four durable engine classes (conjunct 1) reconstruct via the
    #     auto-resume + the LINEAR `reconstruct_final_state` seed;
    #   • a FAN-OUT child reconstructs its AGGREGATE via the SEPARATE B-FANOUT-OUTPUT-REPLAY branch
    #     store (`_crash_fan_out_resume`), gated to `_FANOUT_REPLAY_ENGINE_CLASSES`
    #     ({ESR,WAL,SAVE_POINT,RECONCILER}); RECONCILER's reconciler substrate owns convergence/CAS
    #     state, not the per-branch output map;
    #   • any other topology (EVALUATOR_OPTIMIZER / DECENTRALIZED_HANDOFF) has no reconstruction
    #     substrate → fail closed.
    _fanout_recoverable = (
        tp in _SUBAGENT_RECOVERABLE_FANOUT_CHILD_TOPOLOGIES and ec in _FANOUT_REPLAY_ENGINE_CLASSES
    )
    if not (tp is TopologyPattern.SINGLE_THREADED_LINEAR or _fanout_recoverable):
        return False
    for child_step in child_steps:
        sk_raw: Any = _opaque_field(child_step, "step_kind")
        sk = sk_raw if isinstance(sk_raw, StepKind) else StepKind(sk_raw)
        if sk is StepKind.MANAGED_AGENTS:
            return False  # unfenced vendor sink — no recursively-classifiable child manifest
        if sk is StepKind.SUB_AGENT_DISPATCH:
            # Recursive descent (the NONLEAF-CHILD relaxation): a nested SUB_AGENT_DISPATCH child is
            # admitted IFF it is itself recoverable. A mis-shaped grandchild payload → the read
            # raises → fail closed at the entry guard.
            g_payload: Any = _opaque_field(child_step, "step_payload")
            g_cme: Any = _opaque_field(g_payload, "child_manifest_entry")
            g_steps: Any = _opaque_field(g_payload, "child_steps")
            if not payload_child_recoverable(g_cme, g_steps):
                return False
    return True


def _subagent_child_recoverable(step: WorkflowStep) -> bool | None:
    """B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT (R-FS-1) — whether a SUB_AGENT_DISPATCH worker's
    CHILD is RE-DISPATCH-RECOVERABLE: re-dispatching it under the deterministic child run_id
    auto-resumes the child from its durable store AND reconstructs a result-faithful `final_state`.

    The CP-side MIRROR of the runtime composer's typed `subagent_child_recoverable(payload)` — the
    two must agree (the by-execution witness enforces it). The CP driver does NOT own the runtime
    `SubAgentDispatchPayload` type (layering), so it reads the opaque `step_payload` DEFENSIVELY via
    the recursive `payload_child_recoverable` helper. The THREE conjuncts (the corrected predicate
    over the #746 reverted branch, which keyed on the engine class ALONE → reverted on the [P1-a]
    result-fidelity gap):

    1. child engine ∈ `_SUBAGENT_RECOVERABLE_CHILD_ENGINE_CLASSES`
       ({ESR,WAL,SAVE_POINT,RECONCILER}) — durable output store → the resumed child auto-resumes
       (`resume_at>0` via the engine-class-agnostic F2-prefix join `_determine_resume_at`) AND its
       `final_state` reconstructs. ALL FOUR durable resumable classes are members
       (PURE_PATTERN_NO_ENGINE, the lone non-durable class, is the sole non-member). SAVE_POINT
       joined at the `…-SAVE-POINT-CHILD` close (no CAS-claim, no F-1 window); RECONCILER_LOOP joins
       at the `…-RECONCILER-CHILD` close (R-FS-1): a maybe-ran RECONCILER child re-dispatch runs its
       OWN crash-resume, which fires the U-CP-97 reconverge (`attempt_resume`) gated AT THE CLAIM,
       upstream of the step loop — so the F-1 won-CAS-claim-retry window manifests as
       `ABORT_REVALIDATION_FAILED` → child RunStatus.FAILED *before any step re-executes*
       (at-most-once preserved) → the parent fold raises `SubAgentChildFailedError` (fail-closed;
       never a SUCCESS aggregate). Admitting RECONCILER strictly improves the not-won-claim cases
       and routes the F-1 window through the SAME on-main RECONCILER-resume ABORT→§22.1-HITL
       disposition; the F-1 engine-lock auto-recovery arc improves ALL RECONCILER resumes and is NOT
       a prerequisite here. (DEDICATED set, distinct from `_FANOUT_REPLAY_ENGINE_CLASSES` which the
       separate `_fanout_replay_store` branch-capture gate consumes — carrier segregation.)
    2. child topology is SINGLE_THREADED_LINEAR OR a fan-out topology backed by
       `_FANOUT_REPLAY_ENGINE_CLASSES` ({ESR,WAL,SAVE_POINT,RECONCILER}) — applied at EVERY
       recursion level. LINEAR children reconstruct through the per-step output store; fan-out
       children reconstruct their aggregate through the branch replay store. Unsupported
       topologies or non-durable engines still fail closed.
    3. every child step is non-MANAGED_AGENTS, and every nested SUB_AGENT_DISPATCH child step is
       ITSELF recoverable — the RECURSIVE leaf/non-leaf condition (the NONLEAF-CHILD arc). A
       MANAGED_AGENTS child step is an unfenced vendor sink (no recursive manifest) → fail closed;
       a nested SUB_AGENT_DISPATCH grandchild is admitted IFF the same predicate holds on its own
       payload (correct at ALL depths by construction; the grandchild auto-resumes under its own
       deterministic `child_run_id_seed`, composed by the same code at each re-dispatch level).

    `None` for any non-SUB_AGENT_DISPATCH step (no child → the marker omits the field). Any
    read/parse failure (at any recursion depth) → `False` (the decline-mirror: cannot prove
    recoverable → not recoverable → fail closed), NEVER an exception that would break dispatch."""
    if step.step_kind is not StepKind.SUB_AGENT_DISPATCH:
        return None
    try:
        cme: Any = step.step_payload["child_manifest_entry"]
        child_steps: Any = step.step_payload["child_steps"]
        return payload_child_recoverable(cme, child_steps)
    except (TypeError, KeyError, ValueError, AttributeError):
        # opaque-payload shape mismatch / unknown enum value (at ANY recursion depth) → cannot prove
        # recoverable → fail closed (NEVER break dispatch).
        return False


def _payload_engine_signature(cme: Any, child_steps: Any) -> str:
    """RECURSIVE engine-class signature of a SUB_AGENT child (the NONLEAF-CHILD analogue of the
    cross-engine-class swap guard, R-FS-1). The child's engine value, plus — for each NESTED
    SUB_AGENT_DISPATCH grandchild step — that grandchild's OWN recursive signature.

    Why recursive (out-of-family Codex [P1], NONLEAF-CHILD arc): the recursive `_subagent_child_
    recoverable` now ADMITS a child whose grandchild is itself a recoverable SUB_AGENT. A grandchild
    engine swap (e.g. RECONCILER→SAVE_POINT under the SAME `child_workflow_id` + nested `step_id`)
    keeps the recursive recoverability verdict True AND keeps the OUTER child engine unchanged — so
    the leaf-only `ec.value` guard would pass both legs, and the grandchild's `child_run_id_seed`
    (engine-class-AGNOSTIC) re-derives identically → the swapped grandchild replays the old durable
    store through a DIFFERENT recovery mechanism, bypassing the CAS at-most-once protection ONE
    LEVEL DOWN. Folding the grandchild engines into the signature makes the swap CHANGE the marker
    → the existing dual-gate marker==resumed comparison fails closed.

    Each nested entry records the FULL identity tuple the grandchild's `child_run_id_seed` keys on
    (out-of-family Codex [P1] rounds 2-3) — `[ordinal, step_id, child_workflow_id, recursive_sig]`:
    the ORDINAL (the seed's `parent_idempotency_key` IS the child-step-index key, so a REORDER —
    inserting a step before the grandchild — shifts the seed), the `step_id` + engine (the
    linear-sequential disambiguator catches a RENAME / engine SWAP), and the `child_workflow_id`
    (`compose_child_run_id_seed` mixes it in, catching a RE-POINT). Without ALL four, an edit that
    changes the seed but NOT the marker would pass the dual gate while the grandchild gets a fresh
    run_id → re-fires effects committed under the old identity (the at-most-once bypass the marker
    dual gate prevents at the worker/orchestrator level, ONE LEVEL DOWN). The recursive
    `recursive_sig` carries the grandchild's OWN engine + deeper tuples → holds at all depths.

    TOPOLOGY is part of the signature for a FAN-OUT child (the FANOUT-CHILD arc, R-FS-1, out-of-
    family Codex [P1]): conjunct 2 now admits the SAME durable replay engine set under BOTH
    SINGLE_THREADED and a fan-out topology, so a maybe-ran child dispatched LINEAR-ESR
    (or LINEAR-SAVE_POINT) whose resumed manifest swaps ONLY its topology to fan-out (same
    engine/step_id) would pass an engine-only signature AND reuse the same `child_run_id` against a
    DIFFERENT recovery substrate (the LINEAR `reconstruct_final_state` seed vs the fan-out
    `_crash_fan_out_resume` branch store) → the child finds no matching records → runs FRESH →
    double-fires committed effects. A fan-out↔fan-out swap (PARALLELIZATION↔ORCHESTRATOR_WORKERS) is
    the same hole (a different store shape). The fix folds topology into the signature so ANY
    topology swap CHANGES the marker → the dual-gate marker==resumed comparison fails closed.

    A LINEAR child's base value is the bare engine value, BYTE-IDENTICAL to the pre-FANOUT-CHILD
    marker (the #774..#786 LINEAR closed scope is unaffected); a FAN-OUT child's base is
    `f"{topology}:{engine}"` (the topology prefix never collides with a bare engine value). A LEAF
    child → just the base value. A NESTED child → an UNAMBIGUOUS `json.dumps([base, [[ordinal,
    step_id, workflow_id, grandchild_sig], ...]])` (Codex [P2]): JSON escaping prevents a crafted
    nested `step_id` with delimiters from colliding two distinct child trees onto one marker string.
    A nested signature always starts with `[`, a leaf never does → no leaf↔nested collision. Same
    defensive opaque read as `payload_child_recoverable` (raises on miss → caught by the entry's
    fail-closed guard)."""
    ec_raw: Any = _opaque_field(cme, "engine_class")
    ec = ec_raw if isinstance(ec_raw, EngineClass) else EngineClass(ec_raw)
    tp_raw: Any = _opaque_field(cme, "topology_pattern")
    tp = tp_raw if isinstance(tp_raw, TopologyPattern) else TopologyPattern(tp_raw)
    # LINEAR → bare engine value (byte-identical pre-FANOUT-CHILD marker); fan-out → topology:engine
    # so a LINEAR/fan-out or fan-out/fan-out swap (same engine, a new hole) fails the gate.
    base = ec.value if tp is TopologyPattern.SINGLE_THREADED_LINEAR else f"{tp.value}:{ec.value}"
    nested: list[list[Any]] = []
    for ordinal, child_step in enumerate(child_steps):
        sk_raw: Any = _opaque_field(child_step, "step_kind")
        sk = sk_raw if isinstance(sk_raw, StepKind) else StepKind(sk_raw)
        if sk is StepKind.SUB_AGENT_DISPATCH:
            g_step_id = str(_opaque_field(child_step, "step_id"))
            g_payload: Any = _opaque_field(child_step, "step_payload")
            g_workflow_id = str(_opaque_field(g_payload, "child_workflow_id"))
            g_sig = _payload_engine_signature(
                _opaque_field(g_payload, "child_manifest_entry"),
                _opaque_field(g_payload, "child_steps"),
            )
            # The FULL nested identity tuple the grandchild's `child_run_id_seed` keys on —
            # ORDINAL (the seed's `parent_idempotency_key` is the child-step-index key), step_id +
            # engine (the linear-sequential disambiguator), workflow_id (`compose_child_run_id_seed`
            # mixes it in). ANY edit (reorder / rename / engine swap / re-point) changes BOTH the
            # seed AND this marker → the dual gate fails closed before the outer child is recovered,
            # so a re-dispatch can never replay the old grandchild store under a changed identity.
            nested.append([ordinal, g_step_id, g_workflow_id, g_sig])
    return base if not nested else json.dumps([base, nested], separators=(",", ":"))


def _subagent_child_engine_class(step: WorkflowStep) -> str | None:
    """B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT-RECONCILER-CHILD + -NONLEAF-CHILD (R-FS-1) — the
    DISPATCH-TIME RECURSIVE engine-class SIGNATURE for a SUB_AGENT_DISPATCH worker (else `None`).

    The cross-engine-class swap guard. `_subagent_child_recoverable` admits ALL FOUR durable engine
    classes ({ESR,WAL,SAVE_POINT,RECONCILER}), so once >1 is recoverable a maybe-ran child whose
    marker recorded one recoverable engine (e.g. RECONCILER) but whose RESUMED manifest supplies a
    DIFFERENT recoverable engine (e.g. SAVE_POINT) under the SAME `step_id` passes both boolean
    recoverability gates. `compose_child_run_id_seed` is engine-class-AGNOSTIC, so the swap
    re-dispatches the child against the SAME durable store through a DIFFERENT recovery mechanism →
    the RECONCILER CAS at-most-once protection is bypassed (UNLIKE the documented child-swap /
    tool-swap parities, which CHANGE the key). Persisting (marker) + comparing (gate) this value
    lets the maybe-ran SUB_AGENT gate require the marker signature == the resumed signature (fail
    closed on mismatch / `None`) — the engine-class leg of the same-identity guard.

    Since the NONLEAF-CHILD arc admits NESTED recoverable SUB_AGENT children, the signature is
    RECURSIVE (`_payload_engine_signature`): a grandchild engine swap that keeps the recursive
    verdict True (and the outer engine unchanged) still changes the signature → fail closed. A LEAF
    child → just the engine value (byte-identical to the pre-NONLEAF-CHILD marker).

    Reads the opaque `step_payload` DEFENSIVELY (the CP driver does not own the runtime payload
    type). Any read/parse failure → `None` (fail closed at the gate), NEVER an exception that breaks
    dispatch."""
    if step.step_kind is not StepKind.SUB_AGENT_DISPATCH:
        return None
    try:
        return _payload_engine_signature(
            step.step_payload["child_manifest_entry"], step.step_payload["child_steps"]
        )
    except (TypeError, KeyError, ValueError, AttributeError):
        return None


def _resumed_subagent_child_engine_by_ordinal(
    branch_steps: Sequence[WorkflowStep], *, branch_count: int, orchestrated: bool
) -> dict[int, str | None]:
    """B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT-RECONCILER-CHILD (R-FS-1) — `{ordinal: RESUMED
    manifest child EngineClass value}` for the WORKER/PEER ordinals (the resumed-side half of the
    cross-engine-class swap guard). Compared against the dispatch-time marker engine (the store's
    `dispatched_branch_child_engine_classes`) in `_fence_unrecoverable_maybe_ran_indices`; a
    mismatch / `None` fails closed. Same offset scheme as
    `_resumed_subagent_recoverable_by_ordinal`; out-of-range ordinals omitted (→ `None` at the gate
    → fail closed)."""
    offset = 1 if orchestrated else 0
    return {
        bi: _subagent_child_engine_class(branch_steps[bi + offset])
        for bi in range(branch_count)
        if 0 <= bi + offset < len(branch_steps)
    }


def _resumed_subagent_recoverable_by_ordinal(
    branch_steps: Sequence[WorkflowStep], *, branch_count: int, orchestrated: bool
) -> set[int]:
    """B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT (R-FS-1) — the WORKER/PEER ordinals whose RESUMED
    manifest step is a SUB_AGENT_DISPATCH with a RECOVERABLE child (`_subagent_child_recoverable`
    on the resumed step). The resumed-side half of the maybe-ran-subagent changed-manifest guard
    ([P1-b], the #746 `6930e7ef` Codex [P1]): recovery requires the child be recoverable BOTH at
    dispatch (the marker — durable records exist) AND in the RESUMED manifest (the re-dispatch goes
    through the replay store/fence path); a child edited recoverable→non-recoverable between
    dispatch + resume is in the dispatch-time set but NOT here → fail closed (else the re-dispatch
    runs the now-non-recoverable child fresh → double-fire / suffix-only corruption). Same offset
    scheme as `_resumed_branch_kinds_by_ordinal`; out-of-range ordinals omitted (→ not
    recoverable)."""
    offset = 1 if orchestrated else 0
    return {
        bi
        for bi in range(branch_count)
        if 0 <= bi + offset < len(branch_steps)
        and _subagent_child_recoverable(branch_steps[bi + offset]) is True
    }


def _fence_unrecoverable_maybe_ran_indices(
    unsafe_indices: set[int],
    dispatched_kinds: Mapping[int, str | None],
    resumed_kinds: Mapping[int, str],
    branch_count: int,
    *,
    dispatched_step_ids: Mapping[int, str | None],
    resumed_step_ids: Mapping[int, str],
    subagent_recoverable_indexes: Collection[int] = frozenset(),
    resumed_subagent_recoverable_indexes: Collection[int] = frozenset(),
    dispatched_child_engines: Mapping[int, str | None] = MappingProxyType({}),
    resumed_child_engines: Mapping[int, str | None] = MappingProxyType({}),
) -> set[int]:
    """B-FANOUT-EFFECT-FENCE-BRANCH-PAUSE (R-FS-1) — the subset of `unsafe_indices` (the
    re-fire-UNSAFE maybe-ran ordinals from `_refire_unsafe_branch_indices`) that ALSO cannot be
    recovered by RE-DISPATCH-INTO-THE-EFFECT-FENCE, so they STILL fail closed.

    Narrows the v1.62 effect-bearing blanket fail-closed: a maybe-ran TOOL_STEP branch is now
    FENCE-RECOVERABLE — re-dispatching it re-reaches the runtime effect fence (auto-active on the
    fan-out crash-resume engine classes), which makes the re-dispatch at-most-once at the tool
    sink (suppress / ambiguous-PAUSE / fresh-fire-if-claim-absent).

    An ordinal is fence-recoverable iff ALL of: (a) within `[0, branch_count)`; (b) the DISPATCH
    MARKER kind is in `_FANOUT_MAYBE_RAN_FENCE_RECOVERABLE_KIND_VALUES` (TOOL_STEP or
    MANAGED_AGENTS — the ORIGINAL effect reached a fence at its own sink: the runtime tool-fence,
    or the §14.22 vendor-session fence the B-FANOUT-CRASH-RESUME-MAYBE-RAN-UNFENCED-EXTERNAL build
    added; the at-most-once changed-manifest guard inherited from `_refire_unsafe_branch_indices`);
    (c) the RESUMED manifest's kind at that ordinal is ALSO in the set; AND (d) the marker kind ==
    the resumed kind (the same-kind guard). Conjuncts (c)+(d) are the changed-kind guard: a
    maybe-ran fenced branch re-supplied at the same ordinal as a NON-recoverable kind (e.g.
    DECLARATIVE) — or as a DIFFERENT recoverable kind (TOOL_STEP ⇄ MANAGED_AGENTS) — would
    re-dispatch a step that reaches a DIFFERENT sink (or no sink) → the original effect's fence
    ambiguity would be silently abandoned + a fresh effect fired. Keying fence-recovery on the
    marker kind ALONE (an earlier draft) re-opened the exact v1.62 changed-manifest hole; requiring
    the resumed kind to ALSO be in the set AND equal to the marker kind closes it (the CP driver
    knows `step_kind` — a typed `WorkflowStep` field — without reading the opaque `step_payload`).

    ACCEPTED PARITY (NOT a registered arc — CP spec v1.65 §3 documents it): a tool-SWAP under the
    same `step_id` (marker TOOL_STEP, resumed TOOL_STEP, but a DIFFERENT `tool_id`) composes a
    DIFFERENT fence key → fires the new tool fresh (per-key at-most-once is PRESERVED — each tool
    key fires ≤once), the SAME structural consequence as the cleared LINEAR effect-fence path
    (which likewise re-dispatches the resumed manifest's step into the fence keyed on `tool_id`).
    The CP driver cannot detect it because `tool_id` lives in the opaque `step_payload`
    (`WorkflowStep` §25.2) — this is CORRECT parity with linear, not a missing capability. A
    stricter-than-linear fail-closed-on-tool-swap is *possible* (record `tool_id` runtime-side)
    but is a policy stricter than the cleared linear path, not a deferred capability — so not
    registered.

    An ordinal stays UNRECOVERABLE (fail closed) iff it is NOT a same-kind fence-recoverable branch
    in range: an out-of-range / stale-store ordinal, an un-recorded / `None` marker kind (presence
    ≠ validity), a changed-to-non-recoverable resumed kind, a CROSS-KIND swap between two
    recoverable kinds (TOOL_STEP ⇄ MANAGED_AGENTS), a SUB_AGENT_DISPATCH whose child was
    NON-recoverable at dispatch OR in the resumed manifest (no result-faithful child auto-resume →
    the `…-NONREPLAY-CHILD` / `…-SAVE-POINT-RECONCILER` / fan-out-child residuals), or any other
    effect-bearing kind. Shared by the incomplete-recovery + cardinality-only sites so the
    recoverability classification is one source of truth.

    B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT (R-FS-1) — a SUB_AGENT_DISPATCH worker is READ_ONLY at
    the parent gate; its only external effects live at the CHILD's tool sinks. A maybe-ran
    SUB_AGENT_DISPATCH worker is RECOVERABLE iff its child was RECOVERABLE
    (durable LINEAR or supported fan-out topology, recursively recoverable) BOTH at dispatch
    (`subagent_recoverable_indexes`, the marker — proves the child wrote durable records to
    auto-resume from) AND in the RESUMED manifest (`resumed_subagent_recoverable_indexes` — proves
    the re-dispatch goes through the replay store/fence path, not a fresh non-recoverable run) AND
    with the SAME child engine class at dispatch + resume (`dispatched_child_engines` ==
    `resumed_child_engines` — the cross-engine-class swap guard, out-of-family Codex [P1]: once >1
    durable engine class is recoverable, a same-step_id RECONCILER→SAVE_POINT swap would otherwise
    re-dispatch the child against the engine-class-agnostic-seeded SAME durable store through a
    DIFFERENT recovery mechanism, bypassing the RECONCILER CAS at-most-once protection; marker
    engine missing → None → fail closed). Requiring all three closes the changed-manifest hole
    ([P1-b], #746 `6930e7ef`, + the engine-class leg this arc): a child edited
    recoverable→non-recoverable between dispatch + resume has durable records (dispatch True) but
    the re-dispatch runs the non-recoverable child FRESH (resumed False) → double-fire / suffix-only
    corruption. Re-dispatching a recoverable
    child auto-resumes it under the deterministic child run_id (composer-derived from the worker's
    stable per-branch key) → at-most-once is COMPOSITIONAL (the child recursively re-applies this
    same classifier). ACCEPTED PARITY (documented, mirrors the TOOL_STEP tool-swap): a
    child-workflow-id SWAP under the same step_id composes a DIFFERENT deterministic child run_id →
    the new child runs fresh under its own key (per-child at-most-once PRESERVED) — UNLIKE an
    engine-class swap, which keeps the same seed and is fail-closed above."""
    _tool = _FANOUT_MAYBE_RAN_FENCE_RECOVERABLE_KIND_VALUES
    _subagent = StepKind.SUB_AGENT_DISPATCH.value
    return {
        bi
        for bi in unsafe_indices
        if not (
            0 <= bi < branch_count
            # B-FANOUT-CRASH-RESUME-PAUSE-RECONSTRUCT-MAYBE-RAN-FENCE-STEP-ID (R-FS-1) — the
            # changed-STEP_ID guard (out-of-family Codex [P1], #756), COMMON to both recovery
            # paths. An operator-edited crash-resume manifest that kept the kind but
            # RENAMED/REORDERED the step at this ordinal would re-dispatch a DIFFERENT branch.
            # Require the DISPATCH-MARKER step_id (best-effort; `None` on a torn / pre-arc marker
            # → fail closed, cannot prove the original key) to equal the resumed manifest's step_id.
            and dispatched_step_ids.get(bi) is not None
            and dispatched_step_ids.get(bi) == resumed_step_ids.get(bi)
            and (
                # 1. TOOL_STEP / MANAGED_AGENTS fence-recovery — re-dispatch re-reaches the
                #    runtime effect fence at the tool/vendor sink (at-most-once: suppress /
                #    ambiguous-PAUSE / fresh-fire-if-claim-absent). Same-kind guard (load-bearing
                #    once the recoverable set has >1 kind): a CROSS-KIND swap (marker TOOL_STEP,
                #    resumed MANAGED_AGENTS) re-dispatches into a DIFFERENT fence sink → the
                #    original kind's ambiguous effect abandoned + a fresh effect fired.
                (
                    dispatched_kinds.get(bi) in _tool
                    and resumed_kinds.get(bi) in _tool
                    and dispatched_kinds.get(bi) == resumed_kinds.get(bi)
                )
                # 2. SUB_AGENT_DISPATCH child-recursive-recovery — same-kind (marker AND resumed
                #    both SUB_AGENT_DISPATCH) AND child recoverable BOTH at dispatch (the marker)
                #    AND in the resumed manifest (the [P1-b] dual gate, #746 `6930e7ef`) AND the
                #    SAME-ENGINE guard (out-of-family Codex [P1], …-RECONCILER-CHILD arc): the
                #    marker's dispatch-time child engine class == the resumed manifest's child
                #    engine class. Load-bearing once >1 durable engine class is recoverable
                #    ({ESR,WAL,SAVE_POINT,RECONCILER}): a CROSS-ENGINE swap (marker RECONCILER,
                #    resumed SAVE_POINT) under the same step_id passes BOTH booleans above, and
                #    `compose_child_run_id_seed` is engine-class-agnostic → the swap re-dispatches
                #    the child against the SAME durable store through a DIFFERENT recovery
                #    mechanism, bypassing the RECONCILER CAS at-most-once protection (UNLIKE the
                #    child-swap / tool-swap parities, which CHANGE the key). Marker engine missing
                #    (torn / pre-arc) → None → mismatch → fail closed.
                or (
                    dispatched_kinds.get(bi) == _subagent
                    and resumed_kinds.get(bi) == _subagent
                    and bi in subagent_recoverable_indexes
                    and bi in resumed_subagent_recoverable_indexes
                    and dispatched_child_engines.get(bi) is not None
                    and dispatched_child_engines.get(bi) == resumed_child_engines.get(bi)
                )
            )
        )
    }


def _subagent_recoverable_marker_indexes(store: Any, run_key: str) -> set[int]:
    """B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT (R-FS-1) — defensive read of the DISPATCH-TIME
    SUB_AGENT child-recoverable marker set from the bound store.

    A store that predates / does not implement the marker reader (a pre-arc store, a partial test
    fake) → empty set → every maybe-ran SUB_AGENT_DISPATCH branch fails closed (presence ≠
    validity; the conservative reading — never auto-recover on an un-answerable store). Mirrors the
    `getattr(ctx, "engine_output_store", None)` wiring idiom — additive, fail-closed, byte-identical
    for any run without a SUB_AGENT_DISPATCH worker."""
    reader = getattr(store, "subagent_child_recoverable_indexes", None)
    if reader is None:
        return set()
    return set(reader(run_key))


def _dispatched_child_engines_from_marker(store: Any, run_key: str) -> dict[int, str | None]:
    """B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT-RECONCILER-CHILD (R-FS-1) — defensive read of the
    DISPATCH-TIME `{ordinal: child EngineClass value}` marker map from the bound store (the
    cross-engine-class swap guard's dispatch-side half).

    A store that predates / does not implement the reader (a pre-arc store, a partial test fake) →
    empty map → the gate's `dispatched_child_engines.get(bi)` is `None` → every maybe-ran SUB_AGENT
    branch fails closed at the same-engine conjunct (presence ≠ validity; never auto-recover on an
    un-answerable store). Mirrors the `_subagent_recoverable_marker_indexes` getattr idiom."""
    reader = getattr(store, "dispatched_branch_child_engine_classes", None)
    if reader is None:
        return {}
    return dict(reader(run_key))


def _orchestrator_subagent_recoverable(store: Any, run_key: str) -> bool:
    """B-FANOUT-CRASH-RESUME-ORCHESTRATOR-MAYBE-RAN-SUBAGENT (R-FS-1) — defensive read of the
    DISPATCH-TIME orchestrator child-recoverable marker (the single-orchestrator analogue of
    `_subagent_recoverable_marker_indexes`).

    A store that predates / does not implement the reader (a pre-arc store, a partial test fake) →
    `False` → a maybe-ran SUB_AGENT_DISPATCH orchestrator fails closed (presence ≠ validity; never
    auto-recover on an un-answerable store). Mirrors the worker `getattr` idiom — additive,
    fail-closed, byte-identical for any run without a SUB_AGENT_DISPATCH orchestrator."""
    reader = getattr(store, "orchestrator_subagent_child_recoverable", None)
    if reader is None:
        return False
    return bool(reader(run_key))


def _orchestrator_dispatched_child_engine(store: Any, run_key: str) -> str | None:
    """B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT-RECONCILER-CHILD (R-FS-1) — defensive read of the
    DISPATCH-TIME orchestrator child `EngineClass` value marker (the single-orchestrator analogue
    of `_dispatched_child_engines_from_marker`; the cross-engine-class swap guard's dispatch half).

    A store that predates / does not implement the reader (a pre-arc store, a partial test fake) →
    `None` → the orchestrator gate's same-engine conjunct fails closed (presence ≠ validity; never
    auto-recover on an un-answerable store). Mirrors the `_orchestrator_subagent_recoverable`
    getattr idiom — additive, fail-closed."""
    reader = getattr(store, "orchestrator_dispatched_child_engine_class", None)
    if reader is None:
        return None
    result = reader(run_key)
    return result if isinstance(result, str) else None


def _orchestrator_dispatched_proceed_unstamped(store: Any, run_key: str) -> bool:
    """Defensively read the PROCEED-origin unstamped orchestrator marker bit.

    A store without this additive reader cannot prove that an unstamped effect-free marker came
    from the new PROCEED writer rather than corruption / partial loss, so it fails closed by
    returning ``False``."""
    reader = getattr(store, "orchestrator_dispatched_proceed_unstamped", None)
    if reader is None:
        return False
    return bool(reader(run_key))


def _callable_accepts_keyword(fn: Any, keyword: str) -> bool:
    """Return whether a duck-typed writer can accept an additive keyword argument."""
    try:
        parameters = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return keyword in parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
    )


def _fanout_replay_store(ctx: DriverContext, manifest_entry: WorkflowManifestEntry) -> Any:
    """B-FANOUT-OUTPUT-REPLAY (R-FS-1) — the bound `EngineOutputStore` IFF this run is
    fan-out-crash-recoverable, else `None`.

    The SINGLE gate predicate shared by the branch-output CAPTURE sites
    (`record_branch` / `record_orchestrator` at branch completion) and the crash-resume
    CONSUME site (`_determine_fanout_resume`), so the two halves of the recovery
    mechanism can NEVER skew on which runs are recoverable
    (`[[durable-recovery-presence-validity-scope]]`: two halves of one mechanism share
    their scope key). Gated to `_FANOUT_REPLAY_ENGINE_CLASSES` AND an operator-bound store
    (`ctx.engine_output_store` is `None` by default → returns `None` → no-op,
    byte-identical to pre-arc). Read via `getattr` (the `cp_is_wiring` idiom — harness-cp
    does not import the runtime store type)."""
    if manifest_entry.engine_class not in _FANOUT_REPLAY_ENGINE_CLASSES:
        return None
    return getattr(ctx, "engine_output_store", None)


def _capture_branch_terminal(
    ctx: DriverContext,
    manifest_entry: WorkflowManifestEntry,
    *,
    run_idempotency_key: str,
    branch_index: int,
    step_id: str,
    terminal_status: str,
    output: Mapping[str, Any] | None,
) -> None:
    """B-FANOUT-OUTPUT-REPLAY (R-FS-1) — capture a branch's terminal DISPOSITION to the
    durable store (gated on `_fanout_replay_store`).

    The at-most-once class closer: EVERY branch that reaches a terminal boundary records
    its disposition — `completed` with output (recover + fold), `completed` with `output is
    None` (ran-and-errored, effect LANDED → recover as terminal, never re-dispatch, never
    fold), or `timed_out` (deadline-cut, ambiguous → crash-resume fails closed). An
    output-only store made every non-clean-success disposition invisible, so a crashed
    landed effect was silently re-dispatched. No-op unless replay-capable ∧ store-bound."""
    _store = _fanout_replay_store(ctx, manifest_entry)
    if _store is not None:
        _store.record_branch(
            run_idempotency_key, branch_index, str(step_id), terminal_status, output
        )


def _mark_branch_dispatched(
    ctx: DriverContext,
    manifest_entry: WorkflowManifestEntry,
    *,
    run_idempotency_key: str,
    branch_index: int,
    step_id: str,
    step_kind: StepKind,
    step: WorkflowStep | None = None,
) -> None:
    """B-FANOUT-CRASH-RESUME-STRICT-TIER-INCOMPLETE (R-FS-1) — write the reserve-before-
    DISPATCH marker for a fan-out branch (gated on `_fanout_replay_store`).

    THE NAMED INVARIANT: the caller invokes this — durably (the store fsyncs) — strictly
    BEFORE the branch body dispatches, so within an instrumented run marker-ABSENT ⟺ the
    branch's effect did NOT fire. The fan-out analogue of the §14.22 effect-fence reserve at
    branch granularity. Consumed ONLY by the strict-tier (PAUSE / CASCADE_CANCEL) crash-resume
    classifier; written only on those tiers (PROCEED's recovery is unchanged — it accepts the
    dispatch-before-capture window per the PR1/PR2 precedent). The marker records the branch's
    DISPATCH-TIME `step_kind` so the maybe-ran re-fire-safety classifier
    (B-FANOUT-CRASH-RESUME-MAYBE-RAN-RESOLUTION) keys on the ORIGINAL kind, never the
    (possibly changed) resumed manifest's kind.

    B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT (R-FS-1) — for a SUB_AGENT_DISPATCH branch the marker
    ALSO records the DISPATCH-TIME child recoverability (`_subagent_child_recoverable(step)` — the
    durable LINEAR or supported fan-out, recursively recoverable predicate) so the maybe-ran
    classifier re-dispatches a SUB_AGENT_DISPATCH worker ONLY when its child can auto-resume
    result-faithfully under the deterministic run_id, AND the DISPATCH-TIME child engine class
    (`_subagent_child_engine_class(step)`) so the maybe-ran gate fails closed on a same-step_id
    cross-engine-class swap (out-of-family Codex [P1], …-RECONCILER-CHILD arc). `None` (`step` not
    threaded, or a non-SUB_AGENT branch) → both fields are omitted (marker byte-identical). No-op
    unless replay-capable ∧ store-bound."""
    _store = _fanout_replay_store(ctx, manifest_entry)
    if _store is not None:
        _child_recoverable = _subagent_child_recoverable(step) if step is not None else None
        _child_engine_class = _subagent_child_engine_class(step) if step is not None else None
        if _child_recoverable is None:
            # Non-SUB_AGENT branch (or `step` not threaded) → the recoverability + engine-class
            # fields are omitted; call the store with the pre-arc signature (byte-identical marker;
            # a store / fake that predates the additive kwargs is unaffected).
            _store.record_branch_dispatched(
                run_idempotency_key, branch_index, str(step_id), str(step_kind.value)
            )
        else:
            _store.record_branch_dispatched(
                run_idempotency_key,
                branch_index,
                str(step_id),
                str(step_kind.value),
                child_recoverable=_child_recoverable,
                child_engine_class=_child_engine_class,
            )


def _rematerialize_recovered_branch_writer(
    ctx: DriverContext,
    *,
    branch_index: int,
    branch_context: StepExecutionContext,
    run_idempotency_key: str,
    timestamp: datetime,
    procedural_tier_snapshot_ref: Identifier | None,
    workflow_id: str,
    step: WorkflowStep,
    binding: StepEffectiveBinding,
) -> BufferingLedgerWriter:
    """B-FANOUT-OUTPUT-REPLAY (R-FS-1) — re-materialize a crash-recovered branch's LOST
    ledger entries (out-of-family Codex [P1]).

    Unlike a PAUSE-resume (where the recovered branches' step/terminal entries were already
    DRAINED durably before the pause halt), a CRASH before the barrier drain lost the
    in-memory `BufferingLedgerWriter` contents — the §25.12 D1.b ledger drained NOTHING — so
    the resumed run's ledger would OMIT every recovered branch and undercount
    `workflow.step_count`. This appends the recovered branch's step + `completed` terminal
    entries to a fresh writer (identity + causality only — the fan-out branch entries carry
    NO `response_hash`, so the lost output is not part of the entry). The returned writer is
    added to the barrier-drain set; `drain_branch_buffers` → `append_ledger_entry` DEDUPS by
    idempotency key, so a mid-drain crash that DID persist some branch entries yields
    `IDEMPOTENT_NOOP` for those — re-materialization is correct in EVERY crash window (the
    binary-ledger premise need not hold mid-drain). Crash-resume ONLY; the per-branch
    idempotency key is deterministic, so the resumed entry matches the lost one."""
    writer = BufferingLedgerWriter(actor=ctx.ledger_writer.actor, branch_index=branch_index)
    # B-FANOUT-OUTPUT-REPLAY — re-materialize the per-step override-application entry too
    # (out-of-family Codex [P2]): a recovered branch that used a per-step model / role /
    # prompt / HITL override had its `cp.per-step-override-application` entry buffered BEFORE
    # the step entry, and a crash lost it with the in-memory writer — so a crash-resumed
    # ledger would omit override provenance a no-crash run carries. Buffered FIRST (the
    # resolution-time order); no-op when no override applied; dedup-safe (the override
    # entry's `(step, outcome)` idempotency key is deterministic).
    _buffer_branch_override_if_applied(
        branch_writer=writer,
        workflow_id=workflow_id,
        step=step,
        binding=binding,
        timestamp=timestamp,
        snapshot_ref=procedural_tier_snapshot_ref,
    )
    append_branch_step_ledger_entry(
        branch_writer=writer,
        branch_context=branch_context,
        run_idempotency_key=run_idempotency_key,
        local_step_index=0,
        timestamp=timestamp,
        procedural_tier_snapshot_ref=procedural_tier_snapshot_ref,
    )
    append_branch_terminal_ledger_entry(
        branch_writer=writer,
        branch_context=branch_context,
        run_idempotency_key=run_idempotency_key,
        terminal_status="completed",
        timestamp=timestamp,
        procedural_tier_snapshot_ref=procedural_tier_snapshot_ref,
    )
    return writer


def _step_fail_class(prefix: str, exc: BaseException) -> str:
    """Compose a step-dispatch `fail_class` string, surfacing a canonical
    `RT-FAIL-*` code when the raised exception self-describes one.

    B-HITL-WRAP-FAIL-CLASS-SURFACING (Codex [P2] from B-EDIT-CARRIER): the wrap-
    time HITL gate's terminal exceptions (REJECT / EDIT-decode / timeout / audit-
    compose) carry an `rt_fail_class` attribute naming their §14.8 taxonomy code.
    `harness-cp` cannot import the `harness-runtime` exception TYPES (the axis
    dependency graph), so the driver read the generic `type(exc).__name__` — which
    surfaced e.g. `HITLGateRejectedError` instead of the canonical
    `RT-FAIL-HITL-GATE-REJECTED` the exception docstrings + §14.8 already promise.
    Reading the marker via `getattr` keeps CP import-free AND surfaces the precise
    code for ANY runtime exception that opts in by carrying it; a non-marker
    exception falls back to the class name (byte-identical to before). This is the
    robust generalization of the pre-existing per-name canonicalization (e.g. the
    `ManagedAgentsSessionError` name-match)."""
    code = getattr(exc, "rt_fail_class", None) or type(exc).__name__
    return f"{prefix}: {code}: {exc}"


def _read_durable_replay_prefix(
    ctx: DriverContext,
    *,
    run_idempotency_key: str,
    resume_at: int,
    steps: Sequence[WorkflowStep],
    workflow_id: str,
    run_id: str,
) -> tuple[list[tuple[str, dict[str, Any]]], RunResult | None]:
    """Read + validate the durably-stored committed prefix outputs (`0..resume_at-1`).

    The shared read+validate half of the B-ENGINE-OUTPUT-REPLAY family, consumed by
    BOTH the inter-step CHANNEL rehydrate (`_rehydrate_inter_step_channel_on_replay`,
    the INPUT side — a downstream step reads its recovered predecessor) AND the
    child-scoped final_state RECONSTRUCT (`B-CHILD-CRASH-RESUME-FINAL-STATE-RECONSTRUCT`,
    the OUTPUT side — the resumed run's `final_state` reconstructs the full terminal
    state). Returns `(prefix, None)` — `prefix` an ordered `[(step_id, output)]` for
    `i in [0, resume_at)` — on success; `([], None)` for the no-store / config-flip
    degrade (nothing to replay → the pre-arc empty-prefix behavior preserved); and
    `([], FAILED)` on store↔ledger skew / identity-mismatch / unreadable-store
    corruption (the fail-closed gate, the symmetric of B-FANOUT-PAUSE's identity
    fail-close).

    Read is driven by `resume_at` (the ledger authority), NOT "load whatever's in the
    store" — the store may hold one extra uncommitted step (a crash AFTER the
    store-write but BEFORE the ledger-append), which is ignored."""
    _store = getattr(ctx, "engine_output_store", None)
    if _store is None or resume_at <= 0:
        return [], None
    stored = _store.read_outputs(run_idempotency_key)
    if not stored:
        # `read_outputs` returns empty for BOTH "no journal file" AND "file present
        # but unreadable / undecodable" — these are NOT the same (decorrelated review:
        # advisor caught the config-flip false-failure; Codex caught that a read
        # failure must not be silently degraded). Discriminate on FILE EXISTENCE:
        if getattr(_store, "journal_exists", None) is not None and _store.journal_exists(
            run_idempotency_key
        ):
            # A journal EXISTS but yields no readable records → unreadable / corrupt
            # store → FAIL CLOSED (never silently drop cached outputs → wrong upstream).
            return [], RunResult(
                workflow_id=workflow_id,
                run_id=run_id,
                status=RunStatus.FAILED,
                terminal_step_index=0,
                partial_state=None,
                final_state=None,
                fail_class=(
                    f"engine-output-replay-unreadable-store: a journal exists for the "
                    f"run but yields no readable records (resume_at={resume_at}) — "
                    f"unreadable / corrupt output store"
                ),
            )
        # No journal file at all → a config flip (the original run had
        # `engine_output_replay=False`, so nothing was recorded) or a fresh store →
        # NOT corruption: degrade to the documented empty-prefix path (the resumed
        # step reads None upstream / the reconstruct leaves the suffix-only state)
        # rather than fail-closing a previously-working resume (advisor pre-merge
        # catch). RESERVE-before-COMMIT makes the partial case below EXACT: a
        # committed step's store-write is fsync'd BEFORE its ledger-append, so "store
        # has SOME records but is missing a committed prefix step" IS genuine
        # store↔ledger skew → fail-closed.
        return [], None
    prefix: list[tuple[str, dict[str, Any]]] = []
    for i in range(resume_at):
        if i not in stored:
            return [], RunResult(
                workflow_id=workflow_id,
                run_id=run_id,
                status=RunStatus.FAILED,
                terminal_step_index=i,
                partial_state=None,
                final_state=None,
                fail_class=(
                    f"engine-output-replay-missing-output: step_index={i} is "
                    f"materialized in the ledger (resume_at={resume_at}) but absent "
                    f"from the output store — store↔ledger skew corruption"
                ),
            )
        stored_step_id, stored_output = stored[i]
        if stored_step_id != str(steps[i].step_id):
            return [], RunResult(
                workflow_id=workflow_id,
                run_id=run_id,
                status=RunStatus.FAILED,
                terminal_step_index=i,
                partial_state=None,
                final_state=None,
                fail_class=(
                    f"engine-output-replay-identity-mismatch: step_index={i} stored "
                    f"step_id={stored_step_id!r} != resumed body step_id="
                    f"{str(steps[i].step_id)!r} — the workflow body changed"
                ),
            )
        prefix.append((stored_step_id, stored_output))
    return prefix, None


def _rehydrate_inter_step_channel_on_replay(
    ctx: DriverContext,
    *,
    run_idempotency_key: str,
    resume_at: int,
    steps: Sequence[WorkflowStep],
    workflow_id: str,
    run_id: str,
) -> RunResult | None:
    """B-ENGINE-OUTPUT-REPLAY (runtime spec C-RT-32) — on an EVENT_SOURCED_REPLAY
    resume, replay the durably-stored prefix outputs (steps `0..resume_at-1`) into
    the run-scoped inter-step output channel so the FIRST re-dispatched step
    (`resume_at`) reads its recovered predecessor's output — materializing the
    C-CP-08 §8.1 "activity outputs cached and replayed" clause (degenerate without
    this: a skip-prefix resume leaves the fresh channel empty, so a downstream
    consumer reads `None` where a fresh run would read the upstream output).

    No-op unless BOTH the output store + the inter-step channel are bound (the
    genuine replay path needs the consumer; the store alone records but is not
    observed). Delegates the store read + skew/identity validation to the shared
    `_read_durable_replay_prefix` (which fails closed on store↔ledger skew /
    body-change / unreadable-store corruption) and records the validated prefix into
    the channel. Returns the fail-closed `RunResult` on corruption, else `None`."""
    _channel = getattr(ctx, "inter_step_output_channel", None)
    if _channel is None:
        return None
    prefix, fail_result = _read_durable_replay_prefix(
        ctx,
        run_idempotency_key=run_idempotency_key,
        resume_at=resume_at,
        steps=steps,
        workflow_id=workflow_id,
        run_id=run_id,
    )
    if fail_result is not None:
        return fail_result
    for stored_step_id, stored_output in prefix:
        _channel.record(stored_step_id, stored_output)
    return None


# ---------------------------------------------------------------------------
# Branch buffered/deferred-append substrate (C-CP-25 §25.11/§25.12 — U-CP-82)
# ---------------------------------------------------------------------------
#
# The shared substrate every non-linear topology strategy (U-CP-86..U-CP-90)
# reuses: the buffered-append discipline (D1.b), the deterministic
# branch-index-ordered drain (D1), and the bounded barrier (§25.11). Strategies
# differ in *control flow over steps*; they all defer the ledger *write* through
# this substrate. The `SINGLE_THREADED_LINEAR` strategy is unaffected — it keeps
# the inline per-step append of `_execute_workflow_body` verbatim (§25.12).


class BufferingLedgerWriter:
    """A `LedgerWriterLike` that BUFFERS appends instead of writing through.

    C-CP-25 §25.12 D1.b (the load-bearing buffered/deferred-append mechanism): a
    branch executes its step bodies + emits telemetry but **buffers its pending
    ledger entries** here; the orchestrator **drains the buffers through the
    single real `LedgerWriterLike` in branch-index order at the barrier**
    (`drain_branch_buffers`). Only the ledger WRITE is deferred — step dispatch
    and telemetry still fire inline (so the pre-dispatch gate is never deferred,
    §25.15.2 obligation 2). The inline per-step append of `_execute_workflow_body`
    (which persists in *completion* order under `gather`/`TaskGroup`) is the
    foreclosed anti-pattern for the non-linear strategies.

    Structurally satisfies the same `LedgerWriterLike` Protocol the driver
    consumes, so a branch's `ctx.ledger_writer` is swapped to this instance with
    no change to the per-step entry-*payload* shape. The swap is **necessary but
    not sufficient**: a strategy (U-CP-86+) executing branch steps must ALSO
    compose branch-unique `action_id`s via `compose_branch_step_action_id` and
    branch-scoped idempotency keys via `compose_branch_path` (§25.16) — reusing
    the linear `_append_step_ledger_entry`'s flat `workflow:{wf}:step:{N}`
    `action_id` inside a branch would collide across siblings (the U-CP-81
    forward obligation). `branch_index` is carried so `drain_branch_buffers` can
    order the drain deterministically by branch_index (NOT completion order —
    the §25.12 determinism boundary).
    """

    def __init__(self, *, actor: Actor, branch_index: int) -> None:
        self.actor = actor
        self.branch_index = branch_index
        self._buffer: list[tuple[Any, Any]] = []

    def append(self, payload: Any, write_key: Any) -> None:
        """Buffer the `(payload, write_key)` instead of writing through (§25.12 D1.b)."""
        self._buffer.append((payload, write_key))

    @property
    def is_genesis(self) -> bool:
        """Protocol-completeness only — NOT consulted on the branch-append path.

        A branch only appends; chain position / genesis detection is the single
        real writer's concern (a branch never reads `prior_event_hash`). Reports
        the buffer's own emptiness so the field is well-defined if read.
        """
        return len(self._buffer) == 0

    @property
    def entry_count(self) -> int:
        """Count of buffered (not-yet-drained) entries."""
        return len(self._buffer)

    @property
    def buffered_entries(self) -> list[tuple[Any, Any]]:
        """The ordered pending-entry list (step order within this branch)."""
        return list(self._buffer)


def drain_branch_buffers(
    real_writer: LedgerWriterLike,
    branch_buffers: Iterable[BufferingLedgerWriter],
) -> int:
    """Drain buffered branch entries through the single real writer in
    **branch-index order** at the barrier (C-CP-25 §25.12 D1 / D1.b).

    Realizes ADR-F2 v1.2 §Consequences's single-threaded-write boundary: branch
    *execution* is concurrent, but the resulting ledger *appends* are serialized
    through the one real `LedgerWriterLike` in deterministic branch-index order.
    The hash chain stays **single-parent linear** — no second `prior_event_hash`,
    no DAG entry; this helper only feeds the real writer's existing serialized
    append deterministically.

    `branch_buffers` MAY be collected in branch completion order (whichever
    branch's barrier task finished first); the drain **sorts by `branch_index`**
    so the persisted order is a pure function of `branch_index`, independent of
    which branch's model call returned first (the §25.12 determinism boundary;
    "lowest branch-index on tie"). Within a branch, entries drain in their
    buffered step order. Returns the count of entries drained.

    **Drain-time timestamp — the IS-monotonicity realization of the module's own
    "timestamp records the ledger-*append* event" semantic.** Every buffered
    payload is re-stamped to a single drain-moment timestamp at this — its actual
    append — point, NOT the buffer-time value the strategy supplied. A fan-out is
    one barrier-drain persist event, so one drain = one timestamp. This keeps the
    shared ZERO-tolerance IS ledger (`_CLOCK_SKEW_TOLERANCE = timedelta(0)`)
    strictly non-decreasing for **CAUSALLY-ORDERED** drains: the within-level
    scrambled-completion drain (buffer-time wall-clocks can invert branch-index
    order, but the single barrier drain runs on one thread, so `now()` here is
    `>=` whatever preceded the fan-out), AND the single-path cross-level recursion
    inversion (one `SUB_AGENT_DISPATCH` child drains its entries DURING the
    parent's barrier — causally *before* this post-barrier parent drain — so the
    child's `now()` `<=` the parent's). The buffer-time `timestamp=` the append
    helpers carry is a placeholder this drain overrides; the zero-tolerance writer
    remains the live safety net for the DIRECT (linear / runtime) append paths.

    **NOT covered (a known gap; the runtime concurrency fork).** `drain_timestamp`
    is captured here, OUTSIDE the IS writer's serialization point (the module-level
    `_WRITE_LOCK` inside `append_ledger_entry`). So this is monotonic-by-
    construction ONLY for causally-ordered drains, NOT for **concurrent** appends
    to the shared writer that this drain cannot order: (a) two `SUB_AGENT_DISPATCH`
    SIBLING children draining on separate fan-out threads (each captures its own
    `now()` outside `_WRITE_LOCK`; the lock can serialize their physical appends in
    the opposite order → `NonMonotonicTimestampError`), and (b) a runtime audit /
    cost write interleaving between this drain's capture and its appends. Both are
    unreachable today — the runtime sync/async-bridge deadlock blocks concurrent
    sub-agent recursion end-to-end — and were equally broken under the prior
    fan-out-start-timestamp policy (NOT a regression). The clean fix is
    timestamp-authority INSIDE `_WRITE_LOCK` (an IS write-path change, contract-
    touching) and belongs to the same arc as the deadlock; see
    `.harness/runtime_defect_sub_agent_inference_child_loop_bridge_deadlock.md`
    §8 + `test_concurrent_sibling_drains_invert_timestamp` (xfail, strict). The
    §25.12 determinism boundary is untouched regardless (it constrains append
    *order* — still a pure function of branch_index — never timestamp *values*;
    the chain is not byte-stable across replay).
    """
    drain_timestamp = datetime.now(UTC)
    drained = 0
    for buffer in sorted(branch_buffers, key=lambda b: b.branch_index):
        for payload, write_key in buffer.buffered_entries:
            real_writer.append(payload.model_copy(update={"timestamp": drain_timestamp}), write_key)
            drained += 1
    return drained


async def bounded_barrier[T](
    branch_tasks: Iterable[Awaitable[T]],
    *,
    deadline_seconds: float,
) -> list[T]:
    """Await all branch tasks at a barrier, bounded by a wall-clock deadline.

    C-CP-25 §25.11 (bounded barriers): every barrier (`TaskGroup` / `gather`
    join over branches) is wrapped in a wall-clock deadline so a stuck branch
    cannot strand its parent indefinitely. On deadline-exceeded, raises
    `BranchBarrierDeadlineExceededError`.

    **Leak-freedom (a property of the bound, not of cascade-policy).** No branch
    task ever outlives the barrier: on ANY non-clean exit — deadline-exceeded OR
    a branch raising before the deadline — the still-pending sibling tasks are
    cancelled and awaited before control returns, so no orphaned branch keeps
    dispatching effects in the background (the foreclosed `gather`-leaks-orphans
    anti-pattern, §25.15.2 obligation 8). This bounds the primitive's own tasks;
    it does NOT decide the run-level cascade-policy *reaction* (`FAILED` /
    `PARTIAL` / `PAUSED`) — a branch exception is re-raised UNCHANGED for the
    strategy / U-CP-85 to map (§25.15).

    `gather` (not `TaskGroup`) is used deliberately: it is policy-neutral
    (re-raises the original branch exception verbatim), and §25.11 permits it
    "where no cascade-cancel semantic is needed" — U-CP-82's scope. The
    cascade_policy-AWARE structured-cancellation form (TaskGroup, which bakes in
    cancel-siblings-on-failure and would foreclose the `proceed` policy that
    lets siblings run to completion) lands at U-CP-85 (§25.15.2 obligation 8).

    Results are returned in the input (branch) order of `branch_tasks`; the
    deterministic *persisted* order is enforced separately at
    `drain_branch_buffers`.
    """
    tasks = [asyncio.ensure_future(task) for task in branch_tasks]
    timeout_cm = asyncio.timeout(deadline_seconds)
    try:
        async with timeout_cm:
            return await asyncio.gather(*tasks)
    except TimeoutError as exc:
        # Disambiguate the BARRIER deadline from a branch-LOCAL TimeoutError
        # (e.g. a provider client timeout raised INSIDE a branch). Only the
        # former — for which the timeout context actually expired — is the
        # barrier deadline; a branch's own TimeoutError is re-raised UNCHANGED
        # per the policy-neutral contract above (`gather` propagates it verbatim).
        if timeout_cm.expired():
            raise BranchBarrierDeadlineExceededError(deadline_seconds) from exc
        raise
    finally:
        # Leak-freedom: cancel + await any branch task still pending after a
        # non-clean exit (deadline OR a sibling raising) so none outlives the
        # barrier. On the clean path every task is already done → no-op.
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


# ---------------------------------------------------------------------------
# U-CP-85 — `cascade_policy` consumption + cascade-cancel reach (C-CP-25 §25.15)
# ---------------------------------------------------------------------------
#
# `CascadePolicy` (`pause` / `proceed` / `cascade-cancel`, C-CP-10 §10.2) is
# declared-but-unconsumed at HEAD; under fan-out it becomes load-bearing. This
# block consumes it per the §25.15.2 eight cascade-cancel obligations (the
# council-resolved Fork A — `.harness/council/r-fs-1-b1-cascade-cancel/`):
#
#   - `cascade_policy_run_status`     — the §25.15.1 on-branch-failure run-level
#                                       status mapping (obl. 6).
#   - `resume_should_redispatch`      — resume-idempotency-terminality (obl. 7).
#   - `cascade_cancel_barrier`        — `asyncio.TaskGroup` structured
#                                       cancellation of not-yet-dispatched
#                                       siblings (obl. 1 + 8); the cascade-cancel
#                                       counterpart to the policy-neutral
#                                       `bounded_barrier`.
#   - `dispatch_branch_step_shielded` — an in-flight effectful dispatch runs to
#                                       completion / deadline-timeout under a
#                                       cascade-cancel (obl. 1 + 3 + 4).
#
# Obligations 2 (no-gate-bypass-by-buffering) and 5 (high-blast-radius
# pre-dispatch gating via the committed C-AS-02 → C-CP-19 → C-CP-16 chain) are
# discharged by a branch dispatching its pre-dispatch gate BEFORE the shielded
# effectful dispatch — the gate is a not-yet-dispatched boundary where a
# cascade-cancel is clean. This block provides the cancellation machinery; the
# concrete branch control flow (gate → shielded dispatch → classify → record) is
# the consuming strategy's (U-CP-88, the first cascade-policy consumer). The
# machinery is unit-proven here against SYNTHETIC branch coroutines; the
# real-strategy + `RunResult.status` e2e lands at U-CP-88.


#: The C-CP-25 §25.15.1 run-level `RunStatus` a fan-out reaches when ≥1 branch
#: FAILS under the given policy (obl. 6). Existing `RunStatus` members only — no
#: new value. `PARTIAL` belongs to `proceed`, NEVER `cascade-cancel`
#: (advisor-caught at the council). The §25.15.1 "`degraded=true`" is SRE
#: graceful-degradation PROSE, not a contracted field — `RunStatus.PARTIAL` is
#: the sole degradation signal (the 8-field `RunResult` carries no `degraded`).
_CASCADE_POLICY_RUN_STATUS: dict[CascadePolicy, RunStatus] = {
    CascadePolicy.CASCADE_CANCEL: RunStatus.FAILED,
    CascadePolicy.PROCEED: RunStatus.PARTIAL,
    CascadePolicy.PAUSE: RunStatus.PAUSED,
}


def cascade_policy_run_status(policy: CascadePolicy) -> RunStatus:
    """Map a `cascade_policy` to its run-level `RunStatus` on a branch failure
    (C-CP-25 §25.15.1 table + §25.15.2 obligation 6).

    The **on-branch-failure** reaction mapping — the run-level status a fan-out
    reaches when ≥1 branch fails under the given policy:

    - ``CASCADE_CANCEL`` → ``RunStatus.FAILED``: the fan-out fails;
      not-yet-dispatched siblings were cancelled (`cascade_cancel_barrier`),
      in-flight steps ran to completion / deadline-timeout.
    - ``PROCEED`` → ``RunStatus.PARTIAL``: siblings ran to completion; the
      aggregator (the strategy's deterministic fold, U-CP-86) sees a partial
      result set carried in the existing ``RunResult.partial_state``. **No
      ``degraded`` boolean is minted** — ``RunStatus.PARTIAL`` is the sole
      degradation signal (the §25.15.1 "``degraded=true``" is SRE prose, not a
      contracted ``RunResult`` field).
    - ``PAUSE`` → ``RunStatus.PAUSED``: the fan-out halts at the HITL/pause
      boundary; composes with the existing PauseResumeProtocol + ``api.resume``
      (C-RT-35) — this mapping does NOT re-build pause-snapshot capture.

    A clean fan-out (no branch failure) is the strategy's normal ``SUCCESS`` path
    and does not consult this function.
    """
    return _CASCADE_POLICY_RUN_STATUS[policy]


def resume_should_redispatch(
    terminal_status: Literal["cancelled", "completed", "timed_out"] | None,
) -> bool:
    """Decide whether `api.resume` may re-dispatch a branch given its persisted
    `branch_metadata.terminal_status` (C-CP-25 §25.15.2 obligation 7 —
    resume-idempotency-terminality).

    A branch that reached ANY dispatch-boundary terminal disposition
    (``cancelled`` / ``completed`` / ``timed_out``) MUST NOT be re-dispatched on
    resume — its terminal entry is persisted (U-CP-84), so re-running it would
    double-dispatch its effects. Only a branch with **no** persisted terminal
    entry (``None`` — it never reached a dispatch boundary, e.g. a fan-out
    interrupted before this branch ran) is re-dispatch-eligible.

    ``api.resume`` (C-RT-35) reads each branch's persisted ``terminal_status``
    via the branch-scoped idempotency key (U-CP-83) and consults this predicate
    before re-dispatching: ``True`` ⟹ eligible; ``False`` ⟹ already-terminal,
    skip.
    """
    return terminal_status is None


#: The barrier↔shield coordination channel for the deadline cut-off (obl. 1).
#: Each enclosing `cascade_cancel_barrier` contributes ITS OWN registry set to a
#: **CHAIN** (outermost-first); a branch's in-flight effectful dispatch
#: (`dispatch_branch_step_shielded`) registers itself in **EVERY** set in the
#: chain, so the deadline watchdog of ANY enclosing barrier can cancel it DIRECTLY
#: — `asyncio.shield` protects an in-flight dispatch from the branch's own
#: cancellation (a sibling failure → the effect runs to completion, obl. 1) but
#: NOT from a direct cancel, so a watchdog's direct `inflight.cancel()` is exactly
#: the "...or barrier-deadline timeout" cut-off. **The chain (not a single set) is
#: load-bearing for NESTED fan-out (e.g. HIERARCHICAL_DELEGATION, U-CP-89): a
#: nested barrier's in-flight dispatch must remain visible to the OUTER deadline
#: watchdog, or the outer deadline would only cancel the outer branch task while
#: the shielded inner dispatch outlives it — the outer deadline would stop being a
#: hard cap.** A nested barrier `.set`s `(*parent_chain, my_set)`; the tightest
#: enclosing deadline that fires first cuts the dispatch. A ContextVar (not an
#: argument) so a deeply-nested branch dispatch reaches the chain without threading
#: it through the strategy's control flow; `None` when the helper is used outside
#: any `cascade_cancel_barrier` (then there is no deadline cut-off — only the
#: shield-drive).
_BRANCH_INFLIGHT_DISPATCHES: ContextVar[tuple[set[asyncio.Future[Any]], ...] | None] = ContextVar(
    "branch_inflight_dispatches", default=None
)


async def cascade_cancel_barrier[T](
    branch_coros: Iterable[Coroutine[Any, Any, T]],
    *,
    deadline_seconds: float,
) -> list[T]:
    """Await all branches under `asyncio.TaskGroup` structured cancellation,
    bounded by a wall-clock deadline (C-CP-25 §25.15.2 obligations 1 + 8 — the
    `cascade-cancel` counterpart to the policy-NEUTRAL `bounded_barrier`).

    The `cascade-cancel` policy form: on the FIRST branch raising, the
    ``TaskGroup`` deterministically cancels every not-yet-finished sibling
    (obligation 8: structured cancellation, no orphan leak — the foreclosed
    ``gather``-leaks-orphans anti-pattern). A sibling whose effectful step is in
    flight runs that step to completion (`dispatch_branch_step_shielded`,
    obligation 1) before the cancellation lands at its next dispatch boundary; a
    sibling at a not-yet-dispatched boundary unwinds cleanly. This barrier is
    used ONLY by `cascade-cancel` (the policy that needs
    cancel-siblings-on-first-failure).

    **`proceed` and `pause` use a DIFFERENT barrier — NOT this one and NOT
    `bounded_barrier` as-is.** `bounded_barrier` (gather, policy-NEUTRAL)
    re-raises a branch exception UNCHANGED and is the bounded-wait used where no
    cascade-cancel semantic is needed — but on a branch failure its ``finally``
    CANCELS the still-pending siblings, so it does NOT implement `proceed`
    either. `proceed` requires siblings to **run to completion** (a
    ``return_exceptions``-collecting barrier → a partial result set →
    `RunStatus.PARTIAL`); `pause` halts the fan-out at the HITL/pause boundary →
    `RunStatus.PAUSED`. Those two FLOWS — and the real high-blast-radius
    pre-dispatch gate (obligation 5: C-AS-02 → C-CP-19 → C-CP-16) — are owed at
    the consuming strategy (U-CP-88), composing with the pure
    `cascade_policy_run_status` mapping. U-CP-85 supplies the cascade-cancel
    barrier + that mapping; it does NOT itself wire the `proceed`/`pause` flows
    or the real gate.

    **The wall-clock deadline is a HARD cap on a stuck branch (§25.11).** Two
    composed mechanisms enforce it so it bounds a branch stuck ANYWHERE:

    - A **deadline watchdog** fires at ``deadline_seconds`` and cancels every
      registered in-flight effectful dispatch (`_BRANCH_INFLIGHT_DISPATCHES`)
      DIRECTLY — ``asyncio.shield`` keeps an in-flight dispatch alive against the
      branch's own cancellation (obligation 1 "...runs to its own completion"),
      so without this direct cut-off a stuck in-flight step would defeat the
      deadline ("...OR barrier-deadline timeout"). A branch whose in-flight step
      is cut this way records ``timed_out``.
    - ``asyncio.timeout(deadline_seconds)`` around the ``TaskGroup`` cancels the
      branch TASKS, bounding a branch stuck at a not-yet-dispatched boundary
      (e.g. a blocking HITL gate) that has no in-flight dispatch for the watchdog
      to cut. Such a branch records ``cancelled`` (no effect dispatched).

    On a branch failure the ``TaskGroup`` raises a ``BaseExceptionGroup``; it
    propagates UNCHANGED for the calling strategy (U-CP-88) to map to
    ``cascade_policy_run_status(CASCADE_CANCEL) == RunStatus.FAILED`` (obligation
    6). On the wall-clock deadline (no branch failure), raises
    ``BranchBarrierDeadlineExceededError`` (§25.11 parity with `bounded_barrier`).
    Results are returned in the input (branch) order of ``branch_coros``; the
    deterministic PERSISTED order is enforced separately at `drain_branch_buffers`
    (the §25.12 boundary).

    `branch_coros` are coroutines (the ``TaskGroup`` owns task creation — unlike
    `bounded_barrier`, which accepts already-scheduled awaitables); each is
    created exactly once, so a non-clean exit cannot leave an un-awaited coroutine.
    """
    inflight_dispatches: set[asyncio.Future[Any]] = set()
    # Compose with any enclosing barrier's chain (nested fan-out): this barrier's
    # set is appended so an inner dispatch registers in BOTH this set and every
    # ancestor set — the outer deadline watchdog stays a hard cap over inner work.
    parent_chain = _BRANCH_INFLIGHT_DISPATCHES.get() or ()
    registry_token = _BRANCH_INFLIGHT_DISPATCHES.set((*parent_chain, inflight_dispatches))

    async def _deadline_cutoff() -> None:
        # At the deadline, cancel each in-flight effectful dispatch DIRECTLY.
        # This — NOT timer ordering — is what makes the deadline a hard cap:
        # `asyncio.shield` keeps an in-flight dispatch alive against the BRANCH's
        # own cancellation (obl. 1 "...runs to its own completion") but NOT
        # against a direct `inflight.cancel()`, so cancelling `inflight` here
        # unblocks the branch's shielded drive REGARDLESS of whether this watchdog
        # or the `asyncio.timeout` below fires first (both orderings converge —
        # empirically verified). Do not "optimize away" this watchdog believing
        # the `asyncio.timeout` alone bounds the in-flight drive — it does not.
        # The `asyncio.timeout` below then unwinds any gate-stuck (no-in-flight)
        # branch the watchdog has nothing to cut.
        await asyncio.sleep(deadline_seconds)
        for inflight in list(inflight_dispatches):
            if not inflight.done():
                inflight.cancel()

    cutoff_task = asyncio.ensure_future(_deadline_cutoff())
    tasks: list[asyncio.Task[T]] = []
    try:
        async with asyncio.timeout(deadline_seconds):
            async with asyncio.TaskGroup() as task_group:
                tasks = [task_group.create_task(coro) for coro in branch_coros]
        return [task.result() for task in tasks]
    except TimeoutError as exc:
        # The barrier deadline fired with no branch failure: `asyncio.timeout`
        # cancelled the TaskGroup body and converted the resulting CancelledError
        # to TimeoutError. (A branch failure instead surfaces as a
        # BaseExceptionGroup, which is NOT a TimeoutError → it propagates
        # unchanged for the strategy to map to FAILED.)
        raise BranchBarrierDeadlineExceededError(deadline_seconds) from exc
    finally:
        # Reap the watchdog (a no-op if it already fired) so it never outlives
        # the barrier, then restore the registry ContextVar.
        cutoff_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cutoff_task
        _BRANCH_INFLIGHT_DISPATCHES.reset(registry_token)


async def dispatch_branch_step_shielded[T](inflight: asyncio.Future[T]) -> T:
    """Await an in-flight effectful step dispatch with `asyncio.shield` so a
    cascade-cancel of this branch does NOT abandon the in-flight effect
    (C-CP-25 §25.15.2 obligation 1).

    The caller schedules the dispatch (``inflight = asyncio.ensure_future(...)``)
    and passes the resulting future so it can classify the branch's terminal
    disposition afterwards (obligation 4). The dispatch is registered in the
    `cascade_cancel_barrier` deadline-cut-off channel (`_BRANCH_INFLIGHT_DISPATCHES`)
    for its lifetime, so the barrier's watchdog can cut it off at the deadline.
    Behavior:

    - **Clean path** (no cancellation): returns the dispatch result.
    - **Cancelled while in flight** (a sibling's cascade-cancel cancels THIS
      branch): the shielded ``inflight`` is driven to completion so the effect
      lands (obligations 1 + 3), then ``CancelledError`` is **re-raised** so the
      cancellation is honored (swallowing it would desync the ``TaskGroup`` and
      keep this branch running work that should stop). The caller classifies
      ``completed`` (the in-flight step ran) and records its step + terminal entry.
    - **The in-flight dispatch ERRORS during the drive** (the model/tool call
      raised, not a cancellation): the dispatch RAN, so its error does NOT
      override the cancellation — it is swallowed here and ``CancelledError`` is
      re-raised; the branch's terminal disposition is ``completed`` (a
      ran-and-errored branch is ``completed``, dispatch-boundary not step-outcome,
      per `append_branch_terminal_ledger_entry`'s closed-set contract; the step's
      failure lives at the step's own entry per obligation 3). Letting the error
      escape would spuriously mark a cancelled branch FAILED and drop its terminal
      record (the silent-audit-gap obligations 3/4 foreclose).
    - **Barrier deadline cuts off the in-flight dispatch** (a watchdog of THIS or
      any enclosing barrier cancels ``inflight`` DIRECTLY): ``asyncio.shield``
      surfaces ``inflight``'s cancellation; ``inflight`` is done+cancelled → the
      caller classifies ``timed_out`` (obligation 1 "...or barrier-deadline
      timeout"). The dispatch registers in EVERY enclosing barrier's registry
      (the `_BRANCH_INFLIGHT_DISPATCHES` chain), so an OUTER deadline is a hard
      cap over inner in-flight work (nested fan-out, U-CP-89).

    A cascade-cancel landing at a not-yet-dispatched boundary (the pre-dispatch
    gate, BEFORE this helper is called) never reaches here — the branch's gate
    ``await`` raises ``CancelledError`` cleanly → ``cancelled`` (obligation 4).

    The caller's classify-then-record-then-reraise idiom (the shape U-CP-88
    follows; `cascade_cancel_barrier` cancels a stuck branch's task at the
    deadline, so a branch reaching this ``except`` was itself cancelled → always
    re-raise to honor it)::

        inflight = asyncio.ensure_future(dispatcher.dispatch(step))
        try:
            await dispatch_branch_step_shielded(inflight)
        except asyncio.CancelledError:
            # The step was DISPATCHED → record its step entry (obligation 3:
            # every dispatched effectful step gets its own step ledger entry,
            # REGARDLESS of terminal disposition — the effect may have landed) on
            # BOTH terminal paths. `completed` = the in-flight step ran to
            # completion; `timed_out` = the barrier deadline cut it off (it ran
            # but did not return). A `cancelled` branch (not-yet-dispatched, no
            # effect) records NO step entry — it is handled at the gate boundary,
            # not here.
            record_step(local)  # obligation 3 — keyed by step index, not result
            terminal = "timed_out" if (inflight.cancelled() or not inflight.done()) else "completed"
            record_terminal(terminal)  # U-CP-84 fresh terminal entry
            raise  # honor the cancellation (the barrier cancelled this branch)
        record_step(local)  # clean: record + continue
    """
    # Register in EVERY enclosing barrier's registry (the chain) so an OUTER
    # deadline watchdog stays a hard cap over this (possibly nested) dispatch.
    chain = _BRANCH_INFLIGHT_DISPATCHES.get()
    if chain:
        for registry in chain:
            registry.add(inflight)
    try:
        return await asyncio.shield(inflight)
    except asyncio.CancelledError:
        if not inflight.done():
            try:
                # Drive the shielded in-flight dispatch to completion (obl. 1) so
                # the landed effect is recordable — do NOT abandon it mid-send.
                # (A deadline watchdog cancels `inflight` DIRECTLY, which surfaces
                # above as `inflight` already done+cancelled, so this drive is
                # bounded by the tightest enclosing deadline, never unbounded.)
                await asyncio.shield(inflight)
            except asyncio.CancelledError:
                # A SECOND cancellation while draining → cut it off (no leak).
                inflight.cancel()
            except Exception:
                # The dispatch ERRORED during the drive. The step RAN (errored)
                # → its disposition is `completed`
                # (dispatch-boundary, not step-outcome); honor the cancellation by
                # re-raising below. Swallowing the dispatch error here (not letting
                # it escape) is what keeps a cancelled-and-errored branch from
                # being spuriously marked FAILED with no terminal record (F2-01).
                pass
        raise
    finally:
        if chain:
            for registry in chain:
                registry.discard(inflight)


def resolve_parent_gate_level(manifest_entry: WorkflowManifestEntry) -> GateLevel:
    """Resolve `step_context.parent_gate_level` from manifest per CP spec v1.20 §6.1.Y.

    Reading A composition: operator-supplied `default_gate_level` flows through
    unchanged; `None` falls back to the v1.6 MVP hardcoded `GateLevel.AUTO`.
    This is the single source of truth for the workflow_driver:738 composition
    site — exposed as a module-level helper so H_T-CP-19 Layer 3 e2e tests
    can exercise the chain without re-implementing the conditional.
    """
    if manifest_entry.default_gate_level is not None:
        return manifest_entry.default_gate_level
    return GateLevel.AUTO


def _run_protocol_method_sync[TProtocolResult](
    coro: Coroutine[Any, Any, TProtocolResult],
) -> TProtocolResult:
    """Run a PauseResumeProtocol async-method coroutine to completion from sync
    driver context.

    The PauseResumeProtocol class declares its methods `async def` per CP spec
    v1.13 §26.1 but the body of `capture_pause_snapshot` + `attempt_resume`
    contains no actual `await` expressions at the v1.21 narrow-scope MVP
    (state-summary serialization + hash composition + reader invocation are
    all synchronous primitives). The workflow_driver runs in a worker thread
    spawned by `asyncio.to_thread` from `harness_runtime.api.run` — no current
    event loop is bound on the worker thread. `asyncio.run` constructs a new
    loop for this single coroutine.

    Per spec v1.21 §14.14.7 deferred-discretion: the sync-bridging mechanism
    is impl-discretion. The MVP uses `asyncio.run`; future arcs may substitute
    a `SyncDispatcherFacade`-style captured-loop bridge if the protocol body
    ever introduces real async I/O (e.g., async snapshot persistence).
    """
    return asyncio.run(coro)


def _attempt_reconciler_engine_resume_gate(
    *,
    ctx: DriverContext,
    manifest_entry: WorkflowManifestEntry,
    run_id: str,
    step_id: str,
) -> tuple[RunResult | None, bool]:
    """Run the RECONCILER engine-layer resume gate when a pause record is present.

    Used by both RECONCILER paths conceptually: the linear path has an inline call with a
    resume_at-derived step id; fan-out crash-resume uses this helper before replaying branch-store
    output. A present pause record must fire `attempt_resume` so CAS/ABORT outcomes fail closed
    instead of letting branch replay bypass the reconciler substrate.
    """
    if manifest_entry.engine_class is not EngineClass.RECONCILER_LOOP:
        return None, False
    _engine_recovery_loop = getattr(ctx, "engine_recovery_loop", None)
    if _engine_recovery_loop is None:
        return None, False
    if not _engine_recovery_loop.has_pause_record(
        engine_class=manifest_entry.engine_class,
        workflow_id=manifest_entry.workflow_id,
        run_id=run_id,
    ):
        return None, False
    _engine_resume = _run_protocol_method_sync(
        _engine_recovery_loop.attempt_resume(
            engine_class=manifest_entry.engine_class,
            workflow_id=manifest_entry.workflow_id,
            run_id=run_id,
            step_id=step_id,
            resume_event_id=f"resume:{run_id}:{step_id}",
            resume_attempt_count=1,
            resume_at=datetime.now(UTC).isoformat(),
        )
    )
    _abort_fail_class = _ENGINE_RESUME_ABORT_FAIL_CLASS.get(
        _engine_resume.resume_outcome.outcome_kind
    )
    if _abort_fail_class is None:
        return None, True
    ctx.lifecycle_emitter.emit(WorkflowEventClass.RESUMPTION)
    return (
        RunResult(
            workflow_id=manifest_entry.workflow_id,
            run_id=run_id,
            status=RunStatus.FAILED,
            terminal_step_index=None,
            partial_state=None,
            final_state=None,
            fail_class=_abort_fail_class,
        ),
        True,
    )


def _reconciler_fanout_resume_finalized(store: Any, run_idempotency_key: str) -> bool:
    """Whether a prior RECONCILER fan-out resume crossed the strategy commit boundary."""
    finalized = getattr(store, "reconciler_fanout_resume_finalized", None)
    return bool(finalized(run_idempotency_key)) if callable(finalized) else False


def _record_reconciler_fanout_resume_finalized(
    ctx: DriverContext,
    manifest_entry: WorkflowManifestEntry,
    run_idempotency_key: str,
) -> None:
    """Durably mark a clean RECONCILER fan-out resume after the fan-out result commits."""
    if manifest_entry.engine_class is not EngineClass.RECONCILER_LOOP:
        return
    store = _fanout_replay_store(ctx, manifest_entry)
    if store is None:
        return
    record = getattr(store, "record_reconciler_fanout_resume_finalized", None)
    if callable(record):
        record(run_idempotency_key)


def execute_workflow(
    manifest_entry: WorkflowManifestEntry,
    steps: Sequence[WorkflowStep],
    run_id: str,
    ctx: DriverContext,
    *,
    default_model_binding: ModelBinding,
    step_dispatchers: StepDispatcherRegistry,
    pause_snapshot_input: PauseSnapshot | None = None,
    reconstruct_final_state: bool = True,
) -> RunResult:
    """Execute the workflow per C-CP-25 §25.3 happy-path discipline.

    Drain semantics are NOT applied at U-CP-56 — drain composition is U-CP-57.
    A `ctx.drained_flag.is_set()` value is not consulted here. To exercise the
    happy-path discipline under U-CP-56 alone, supply a context whose
    `drained_flag` is never set OR omit drain-aware composition by calling
    this function directly.

    Parameters
    ----------
    manifest_entry
        The workflow's manifest entry per §6.1; carries `engine_class`,
        `topology_pattern`, per-step overrides, fallback chain, etc.
    steps
        The step sequence in declaration order (in-session amendment §E to
        spec v1.4 — step sequence is decoupled from manifest_entry).
    run_id
        Harness-unique run identifier; root `idempotency_key` derives from
        this.
    ctx
        Driver context (ledger writer + lifecycle event emitter substrate).
        Structurally satisfied by `HarnessContext` at runtime composition.
    default_model_binding
        Default `(provider, model)` binding for steps without per-step
        override; per C-CP-06 §6.2's caller-supplied default discipline.
    step_dispatchers
        Frozen `{StepKind → StepDispatcher}` routing registry. v1.6
        amendment per C-RT-17 §14.7.7 — replaces the v1.5 single
        `step_dispatcher: StepDispatcher` parameter. Driver routes via
        `step_dispatchers.lookup(step.kind).dispatch(...)` (§25.3.3.4
        opaque-step-body discipline preserved — driver routes on the
        declared enum field, not on opaque payload content).

    Returns
    -------
    RunResult
        Terminal status + accumulated state. `status==SUCCESS` on happy-path
        completion; `status==FAILED` on step body or ledger append failure.

    Raises
    ------
    TopologyPatternNotYetMaterializedError
        `manifest_entry.topology_pattern` is outside the v1.4 in-scope set.
    EngineClassNotYetMaterializedError
        `manifest_entry.engine_class` is outside the v1.4 in-scope set.
    """
    # § 25.4 row "Driver entry" — drain check at entry (U-CP-57 AC #1).
    # If drained at entry, return DRAINED before any state mutation (no
    # workflow.start emit; no ledger append; no validation). Per spec §25.4
    # row 1 + plan v2.11 U-CP-57 AC #1: drain check precedes topology +
    # engine-class validation. Per C-OD-25 §25.1 AC #1 (U-OD-35): the
    # workflow.envelope span opens AFTER this check — drain-at-entry returns
    # before any envelope opens (no observable workflow execution occurred).
    if ctx.drained_flag.is_set():
        return RunResult(
            workflow_id=manifest_entry.workflow_id,
            run_id=run_id,
            status=RunStatus.DRAINED,
            terminal_step_index=None,
            partial_state=None,
            final_state=None,
            fail_class=None,
        )

    # U-RT-101 (C-RT-27 §14.17.2 hook-1 per-workflow-init activation hook).
    # Pre-condition: emitter bound + hook bound + skills available. When any
    # is missing/None, silent-skip per §14.17.5 invariant 3 (operator opt-out
    # path preserves pre-v1.32 production behavior). Emit one
    # `skill.activation` span per skill returned by the operator-supplied
    # `SkillActivationHook.select_for_workflow_init(...)` policy, with
    # `activation_mode = FRONTMATTER_ONLY` per Q2=(d) hybrid hook-to-enum
    # mapping. Fires AFTER drain check + BEFORE resume detection / first
    # step dispatch per §14.17.2 hook-1 step 4 ordering.
    _emitter = getattr(ctx, "skill_activation_emitter", None)
    _skills = getattr(ctx, "skills", None)
    if _emitter is not None and _skills is not None:
        _hook = getattr(_emitter, "hook", None)
        if _hook is not None:
            # String literal per AS spec v1.7 §14.4 + runtime spec v1.32
            # §14.17.1 enum. Passed as str (NOT importing SkillActivationMode)
            # to preserve workspace dep-graph discipline — harness-cp does
            # NOT depend on harness-runtime per harness-cp/pyproject.toml;
            # the StrEnum value space is the contract surface across
            # workspace package boundaries.
            for _skill_id in _hook.select_for_workflow_init(
                loaded_skills=_skills.keys(),
                workflow_id=manifest_entry.workflow_id,
            ):
                if _skill_id in _skills:
                    _emitter.emit(
                        skill_id=_skill_id,
                        mode="frontmatter_only",
                        workflow_id=manifest_entry.workflow_id,
                        skill=_skills[_skill_id],
                    )

    # U-RT-89 (C-RT-24 §14.14.3) — entry-point resume detection.
    # When the caller supplies a pause_snapshot_input + the operator has bound
    # PauseResumeProtocol at ctx, invoke `attempt_resume(...)` to validate the
    # snapshot's integrity + check for material diff. The MVP fires the
    # STRICT MaterialDiffPolicy default per spec v1.21 change-note adjacent
    # defect (iii); operator-supplied per-resume policy selection is impl-
    # discretion at follow-on composer arc per spec §14.14.7.
    #
    # The resume detection runs BEFORE the workflow.envelope opens — a failed
    # resume (corruption or diff-aborted) returns FAILED without opening a
    # new envelope. A clean resume sets resume_at_step_index that overrides
    # the prefix-replay path at the body per spec §14.14.5 invariant 5
    # mutual-exclusivity (the two paths are non-overlapping).
    resume_at_step_index: int | None = None
    if pause_snapshot_input is not None and ctx.pause_resume_protocol is not None:
        protocol = cast(PauseResumeProtocol, ctx.pause_resume_protocol)
        resume_result = _run_protocol_method_sync(
            protocol.attempt_resume(
                pause_snapshot_input,
                material_diff_policy=MaterialDiffPolicy.STRICT,
            )
        )
        # U-RT-111 v2.38 AC #3 — RESUME_ATTEMPTED CP→IS state-ledger emission.
        # Defensive operator-opt-in: when cp_is_wiring is None, silent-skip.
        # Per-composer kwarg derivation per plan v2.38 §1.2 AC #3.
        _cp_is_wiring = getattr(ctx, "cp_is_wiring", None)
        if _cp_is_wiring is not None:
            # RESUME audit sequence (the §16.5 idempotency-key discriminator). For most
            # topologies `step_index` is unique per pause point. For EVALUATOR_OPTIMIZER it
            # is the failed step's DECLARED ordinal (0/1 — required < len(steps) for the
            # runtime guard), which REPEATS across same-parity re-pauses (two generate
            # failures both → 0); the iteration cursor's completed-step COUNT is the unique
            # per-pause-point discriminator, so the audit sequence derives from it (else the
            # idempotency key collides + the second resume audit entry is silently dropped as
            # an idempotent no-op). Non-EO snapshots keep `step_index` (byte-unchanged).
            # Out-of-family Codex [P2].
            _eo_resume_for_audit = pause_snapshot_input.evaluator_optimizer_resume
            _resume_audit_sequence = (
                len(_eo_resume_for_audit.completed_steps)
                if _eo_resume_for_audit is not None
                else pause_snapshot_input.step_index
            )
            _run_protocol_method_sync(
                _cp_is_wiring.emit_pause_resume_state_ledger_entry(
                    workflow_id=manifest_entry.workflow_id,
                    step_id=str(_resume_audit_sequence),
                    protocol_event_kind=(PauseResumeProtocolEventKind.RESUME_ATTEMPTED),
                    event_sequence_id=(_resume_audit_sequence << 2) | 0,
                    protocol_state_snapshot=resume_result.model_dump(mode="json"),
                    # Reading A apply (PR #83 sibling-extension): pass
                    # ActorIdentity str-newtype matching composer signature
                    # `actor: ActorIdentity`. See
                    # `.harness/class_2_fork_u_cp_74_actor_field_malformation.md`.
                    actor=ActorIdentity(ctx.ledger_writer.actor.actor_id),
                )
            )
        if not resume_result.resumed:
            return RunResult(
                workflow_id=manifest_entry.workflow_id,
                run_id=run_id,
                status=RunStatus.FAILED,
                terminal_step_index=pause_snapshot_input.step_index,
                partial_state=None,
                final_state=None,
                fail_class=resume_result.fail_class,
            )
        resume_at_step_index = pause_snapshot_input.step_index

    # § C-OD-25 §25.1 — Open the workflow.envelope outer OTel span via
    # ctx.tracer_provider.get_tracer(...).start_as_current_span(...). Every
    # downstream child span (LLM dispatch / tool dispatch / HITL gate /
    # validator / pause-resume / per-server-trust) nests under this envelope
    # via OTel parent-context propagation per §25.4 invariant 3 (U-OD-37 AC #3).
    #
    # Envelope close discipline (U-OD-37 AC #1 + AC #4):
    # - Normal SUCCESS / DRAINED returns leave status UNSET (§25.5 default
    #   — DRAINED is not a fail).
    # - FAILED returns set StatusCode.ERROR with fail_class as description.
    # - Unhandled exceptions inside the body trigger OTel's default discipline
    #   (record_exception=True + set_status_on_exception=True defaults on
    #   start_as_current_span) — exception event recorded with
    #   "exception.type" + status set to ERROR. No explicit try/except wrap
    #   needed; verified at test_envelope_records_exception_on_validation_failure.
    # - Span.end_time_ns reflects actual workflow termination time (context
    #   manager closes on body return; verified at
    #   test_envelope_end_time_reflects_workflow_termination).
    #
    # Resumption (U-OD-37 AC #2): each call to execute_workflow opens a FRESH
    # envelope per §25.4 invariant 1. The prior envelope was closed at
    # pause-snapshot capture per C-CP-26 §26. State-ledger anchoring across
    # envelopes via workflow.run_id + workflow.idempotency_key attributes.
    tracer = cast(TracerProvider, ctx.tracer_provider).get_tracer("harness.cp.workflow_driver")
    with tracer.start_as_current_span("workflow.envelope") as span:
        # C-OD-25 §25.1 — populate the 8 envelope-open attributes from
        # manifest_entry + run identity (workflow.id / run_id / idempotency_key
        # / entry_version / topology_pattern / engine_class / workload_class /
        # persona_tier). Enum values serialize via .value (AC #4 — string
        # form). idempotency_key matches the run-scope key computed inside
        # the body per §25.6 (kept consistent via _compute_run_idempotency_key).
        run_idempotency_key = _compute_run_idempotency_key(
            run_id,
            manifest_entry.workflow_id,
            extras=(str(manifest_entry.entry_version),),
        )
        span.set_attribute("workflow.id", manifest_entry.workflow_id)
        span.set_attribute("workflow.run_id", run_id)
        span.set_attribute("workflow.idempotency_key", run_idempotency_key)
        span.set_attribute("workflow.entry_version", int(manifest_entry.entry_version))
        span.set_attribute("workflow.topology_pattern", manifest_entry.topology_pattern.value)
        span.set_attribute("workflow.engine_class", manifest_entry.engine_class.value)
        span.set_attribute("workflow.workload_class", manifest_entry.workload_class.value)
        span.set_attribute("workflow.persona_tier", manifest_entry.persona_tier.value)

        result, steps_executed = _execute_workflow_body(
            manifest_entry=manifest_entry,
            steps=steps,
            run_id=run_id,
            ctx=ctx,
            default_model_binding=default_model_binding,
            step_dispatchers=step_dispatchers,
            span=span,
            run_idempotency_key=run_idempotency_key,
            resume_at_step_index_override=resume_at_step_index,
            # B-FANOUT-PAUSE — the validated snapshot threads to the non-linear
            # strategy so a `cascade_policy=pause` fan-out resume can skip terminal
            # branches + recover their outputs (`fan_out_resume`). Linear resume
            # ignores it (it uses resume_at_step_index_override); a non-resume run
            # passes None. Gated on `resume_at_step_index is not None` so only a
            # `attempt_resume`-validated snapshot reaches the strategy.
            resume_snapshot=(pause_snapshot_input if resume_at_step_index is not None else None),
            # B-CHILD-CRASH-RESUME-FINAL-STATE-RECONSTRUCT (R-FS-1) — opt-in (a child
            # sub-workflow run; default-off for top-level runs): on a durable-engine-class
            # resume, seed the suffix-only `accumulated` with the durably-stored committed
            # prefix so the child's `final_state` reconstructs the COMPLETE terminal state
            # the parent fan-out / hierarchical-pause fold consumes.
            reconstruct_final_state=reconstruct_final_state,
        )

        # C-OD-25 §25.1 close-time attributes (4 of 12). Outcome enum serializes
        # via .value. fail_class null on DRAINED per §25.5 default (omit
        # attribute rather than set null). terminal_step_index null on SUCCESS
        # (omit). step_count = steps_executed (single-attribute terminal-only
        # per §25.5 default).
        span.set_attribute("workflow.outcome", result.status.value)
        if result.status is RunStatus.FAILED and result.fail_class is not None:
            span.set_attribute("workflow.fail_class", result.fail_class)
        if result.terminal_step_index is not None:
            span.set_attribute("workflow.terminal_step_index", int(result.terminal_step_index))
        span.set_attribute("workflow.step_count", int(steps_executed))

        # C-OD-25 §25.4 invariant 2 — deterministic close. Set span status
        # from RunResult.status. FAILED → StatusCode.ERROR with fail_class
        # description; SUCCESS / DRAINED leave default UNSET.
        if result.status is RunStatus.FAILED:
            span.set_status(Status(StatusCode.ERROR, result.fail_class or "FAILED"))
        return result


def _execute_workflow_body(
    manifest_entry: WorkflowManifestEntry,
    steps: Sequence[WorkflowStep],
    run_id: str,
    ctx: DriverContext,
    *,
    default_model_binding: ModelBinding,
    step_dispatchers: StepDispatcherRegistry,
    span: Any,
    run_idempotency_key: str,
    resume_at_step_index_override: int | None = None,
    resume_snapshot: PauseSnapshot | None = None,
    reconstruct_final_state: bool = True,
) -> tuple[RunResult, int]:
    """Execute the workflow body within the workflow.envelope OTel span.

    Per C-OD-25 §25.1–§25.5 (OD spec v1.8): this helper executes inside the
    envelope opened by execute_workflow above. Returns the RunResult plus the
    count of steps fully executed (body + step.boundary emit + ledger append
    all succeeded) — consumed by the wrapper to populate the
    workflow.step_count close-time attribute (§25.5 default — single-attribute
    terminal-only).

    The run_idempotency_key parameter is computed by the wrapper (per §25.6
    + §25.1 workflow.idempotency_key attribute) and threaded through to keep
    the run-scope key identical between envelope-attribute set and the
    resumption N-lookup.
    """
    # § 25.10 — driver-strategy dispatch (replaces the §25.3.1
    # `_IN_SCOPE_TOPOLOGY` materialization gate). `SINGLE_THREADED_LINEAR`
    # resolves to the existing §25.3 inline loop below; the five non-linear
    # patterns raise `TopologyPatternNotYetMaterializedError` until their
    # strategy units (U-CP-86..U-CP-90) land. Resolution stays at this site so
    # the drain-at-entry check (§25.4, above) still precedes topology
    # validation (U-CP-57 AC #1 / C-OD-25 §25.1 ordering). The engine-class
    # gate is unchanged.
    strategy = resolve_driver_strategy(manifest_entry.topology_pattern)
    if manifest_entry.engine_class not in _IN_SCOPE_ENGINE_CLASSES:
        raise EngineClassNotYetMaterializedError(manifest_entry.engine_class)

    # B-POSTJOIN placement guard (out-of-family Codex [P2]): a POST_JOIN_SYNTHESIS
    # step is valid ONLY as the SINGLE terminal step of a CONCURRENT fan-out
    # (PARALLELIZATION / ORCHESTRATOR_WORKERS / HIERARCHICAL_DELEGATION) — the only
    # paths that carve it out (`_split_synthesis`) + dispatch it post-barrier with
    # the siblings. Any other placement (non-terminal, multiple, or a non-concurrent
    # topology) would otherwise run the synthesis as an ordinary branch/step with NO
    # siblings → a wasted LLM call on no data folded into the aggregate. Reject
    # fail-closed (no `workflow.start`-equivalent side effect has fired here yet).
    _synth_positions = [
        i for i, _s in enumerate(steps) if _s.step_kind is StepKind.POST_JOIN_SYNTHESIS
    ]
    # B-FANOUT-PAUSE-SYNTHESIS (R-FS-1) — a synthesis-bearing fan-out PAUSE is now
    # RESUMABLE (this arc relaxes the prior blanket `post-join-synthesis-on-resume-
    # unsupported` reject, out-of-family Codex round 9 [P1]). The pause snapshot now
    # carries the terminal synthesis IDENTITY (presence + `step_id`) on its fan-out
    # resume carrier, so resume material-diffs the re-supplied synthesis step against
    # it: match → PROCEED (the strategy recovers branches + fresh-dispatches the
    # synthesis post-barrier — it never ran on a pause, so it is effect-free,
    # first-and-only per B-POSTJOIN); mismatch (synthesis added / removed / changed
    # `step_id`) → fail closed BEFORE any dispatch side-effect (the [P1] fail-closed
    # posture preserved, now as a TYPED material-diff rather than a blanket reject).
    # The helper also covers the synthesis-REMOVED case (snapshot captured a synthesis,
    # resumed body dropped it → would silently fold) that a check nested inside the
    # `if _synth_positions:` placement block would structurally miss. Both-absent →
    # None (a non-synthesis fan-out resume, unchanged). HIERARCHICAL child levels
    # re-enter via `execute_workflow(pause_snapshot_input=...)` so they hit this same
    # guard against the CHILD's own snapshot (the child carves + captures its own
    # synthesis identity via the reused `_execute_orchestrator_workers`).
    if resume_snapshot is not None:
        # B-FANOUT-RESUME-CARRIER-TOPOLOGY-MISMATCH (R-FS-1) — general carrier/topology
        # mismatch guard, FIRST (out-of-family Codex flagged this 3× at
        # B-FANOUT-PAUSE-SYNTHESIS). A pause snapshot populates EXACTLY ONE topology resume
        # carrier (`fan_out_resume` / `peer_fan_out_resume` / `handoff_resume` /
        # `evaluator_optimizer_resume` / `effect_fence_resume`), read by exactly its
        # capturing strategy. A snapshot resumed under a topology whose carrier it did NOT
        # populate (a topology change between pause and resume) must fail closed — the
        # resuming strategy would otherwise read its absent carrier (`_is_resume` False) and
        # run the WHOLE topology FRESH, re-dispatching effect-bearing branches/stages (an
        # at-most-once violation). This GENERALIZES the B-FANOUT-PAUSE-SYNTHESIS
        # synthesis-only carrier-mismatch guard to ALL topology resume carriers,
        # synthesis-bearing or not (the v1.58 §1 "non-synthesis ... unchanged" carve-out is
        # now closed). It runs FIRST so the synthesis material-diff below always sees a
        # carrier-consistent snapshot (and its own former carrier-mismatch leg — the
        # both-dropped synthesis false-pass — is subsumed: a populated synthesis carrier the
        # strategy does not read fails here on carrier-populated alone, independent of the
        # synthesis identity).
        _carrier_diff = _resume_carrier_topology_mismatch(resume_snapshot, strategy)
        _synth_diff = (
            _carrier_diff
            if _carrier_diff is not None
            else _synthesis_resume_material_diff(resume_snapshot, steps, strategy)
        )
        if _synth_diff is not None:
            return (
                RunResult(
                    workflow_id=manifest_entry.workflow_id,
                    run_id=run_id,
                    status=RunStatus.FAILED,
                    terminal_step_index=(
                        _synth_positions[0] if _synth_positions else max(0, len(steps) - 1)
                    ),
                    partial_state=None,
                    final_state=None,
                    fail_class=_synth_diff,
                ),
                0,
            )
    if _synth_positions:
        # B-POSTJOIN placement guard (out-of-family Codex [P2]): a POST_JOIN_SYNTHESIS
        # step is valid ONLY as the single terminal step of a concurrent fan-out. This
        # runs on BOTH fresh runs and resumes (a matching-identity resume falls through
        # the diff above into here); the placement was already valid at pause, so a
        # resume re-validates harmlessly.
        _concurrent_fanout = {
            _DriverStrategyStatus.PARALLELIZATION,
            _DriverStrategyStatus.ORCHESTRATOR_WORKERS,
            _DriverStrategyStatus.HIERARCHICAL_DELEGATION,
        }
        if (
            len(_synth_positions) > 1
            or _synth_positions[0] != len(steps) - 1
            or strategy not in _concurrent_fanout
            # Zero-branch guard (out-of-family Codex [P2]): a lone POST_JOIN_SYNTHESIS
            # (len < 2, no fan-out step to compose) would carve to empty branch_steps
            # → the strategy's empty-steps early return SILENTLY DROPS it. A synthesis
            # needs ≥1 fan-out sibling — reject fail-closed.
            or len(steps) < 2
        ):
            return (
                RunResult(
                    workflow_id=manifest_entry.workflow_id,
                    run_id=run_id,
                    status=RunStatus.FAILED,
                    terminal_step_index=_synth_positions[0],
                    partial_state=None,
                    final_state=None,
                    fail_class=(
                        "post-join-synthesis-misplaced: POST_JOIN_SYNTHESIS is valid only "
                        "as the single terminal step of a concurrent fan-out with ≥1 "
                        "fan-out step (PARALLELIZATION / ORCHESTRATOR_WORKERS / "
                        "HIERARCHICAL_DELEGATION)"
                    ),
                ),
                0,
            )

    # § 25.10/25.11 — non-linear strategy dispatch (U-CP-86+). A materialized
    # non-linear pattern routes to its dedicated `_execute_<strategy>` and
    # returns here; the `SINGLE_THREADED_LINEAR` inline loop below stays
    # BYTE-UNCHANGED (§25.10 Invariant 1 — regression-safety). The linear-only
    # paths the early return skips (prefix-replay/resume detection, mid-loop
    # drain checks, pause-trigger detection, the per-step validator hook)
    # compose at later strategy units (U-CP-85 cascade_policy / U-CP-88
    # ORCHESTRATOR_WORKERS). `PARALLELIZATION` (U-CP-86) is the happy-path
    # fan-out + deterministic aggregation; `EVALUATOR_OPTIMIZER` (U-CP-87) is
    # the sequential generate→evaluate→regenerate loop. `ORCHESTRATOR_WORKERS`
    # (U-CP-88) dispatches a dynamic worker fan-out under one orchestrator;
    # `HIERARCHICAL_DELEGATION` (U-CP-89) is recursive `ORCHESTRATOR_WORKERS` (one
    # re-entrant level per `SUB_AGENT_DISPATCH` worker, fan-out cap 3 per parent).
    # `DECENTRALIZED_HANDOFF` (U-CP-90) is the single-owner sequential handoff (each
    # per-role stage chains ownership to the next via a `HandoffContext` record; no
    # fan-out, no `SUB_AGENT_DISPATCH`). ALL SIX patterns are now materialized.
    # Cross-level / scrambled-completion timestamp
    # monotonicity on the shared zero-tolerance IS ledger is realized at the drain
    # (`drain_branch_buffers` re-stamps every entry to its append moment); no
    # strategy coordinates a shared timestamp, and a `SUB_AGENT_DISPATCH` child
    # reuses the same recursion seam transparently.
    # net-add #3 (B-FANOUT-OUTPUT-REPLAY, R-FS-1) — fan-out CRASH-resume entry. The 3
    # concurrent fan-out strategies early-return BEFORE the linear resume block, so a
    # crashed fan-out otherwise restarts fresh. When this run is replay-capable ∧
    # store-bound (the SAME `_fanout_replay_store` gate the capture sites use) ∧ there is
    # no explicit PAUSE snapshot, reconstruct the synthetic resume state from the durable
    # branch store and thread it as `crash_fan_out_resume` — the strategy then runs the
    # EXISTING pause-resume recovery VERBATIM (skip terminal branches, recover outputs,
    # re-dispatch the rest; only the snapshot SOURCE differs). `_FanOutStoreCorruptError`
    # (a present-but-unreadable branch / orchestrator) → FAILED (fail-closed, never
    # silently re-dispatch a corrupt branch). Default (no store / not replay-capable /
    # zero completed branches) → `None` → fresh run, byte-identical.
    _crash_fan_out_resume: FanOutResumeState | PeerFanOutResumeState | None = None
    # B-FANOUT-CRASH-RESUME-PAUSE-RECONSTRUCT-NOT-YET-DISPATCHED (R-FS-1, CP spec v1.70 §1) —
    # set True when a PAUSE-trigger crash-resume is INCOMPLETE but every absent ordinal is
    # PROVABLY-NOT-RUN (instrumented + no dispatch marker). Threaded to the fan-out strategy so
    # it re-pauses WITHOUT dispatching the absent ordinals (the obl-5-respecting re-pause mode):
    # the strategy skips them → an empty branch_plan → the existing `_crash_pause_reestablish`
    # gate re-establishes the lost PAUSED state OMITTING them; `api.resume` re-dispatches them
    # (the operator's resume decision is the blast-radius gate, NOT crash-resume).
    _crash_pause_reconstruct_no_dispatch = False
    # B-FANOUT-CRASH-RESUME-PAUSE-RECONSTRUCT-MAYBE-RAN-FENCE-STEP-ID (R-FS-1) — the
    # FENCE-RECOVERABLE (TOOL_STEP / MANAGED_AGENTS) maybe-ran ordinals carried INTO the
    # reconstructed snapshot's `effect_fence_paused_branches` (NOT omitted like the re-fire-safe
    # set): api.resume re-dispatches each THROUGH the resume material-diff guard (changed-step_id /
    # changed-kind → fail closed) + the auto-active fence (at-most-once at the tool sink). Empty
    # unless the PAUSE-reconstruct gate below classifies a fence-recoverable maybe-ran ordinal.
    _crash_pause_reconstruct_fence_paused: tuple[EffectFencePausedBranchResumeState, ...] = ()
    _reconciler_fanout_engine_resume_required = False
    if (
        strategy
        in {
            _DriverStrategyStatus.PARALLELIZATION,
            _DriverStrategyStatus.ORCHESTRATOR_WORKERS,
            _DriverStrategyStatus.HIERARCHICAL_DELEGATION,
        }
        and resume_snapshot is None
    ):
        _crash_replay_store = _fanout_replay_store(ctx, manifest_entry)
        if _crash_replay_store is not None:
            _crash_branch_steps, _ = _split_synthesis(steps)
            try:
                _crash_fan_out_resume = _determine_fanout_resume(
                    _crash_replay_store,
                    run_idempotency_key,
                    _crash_branch_steps,
                    manifest_entry.topology_pattern,
                    manifest_entry.fanout_timeout_disposition,
                )
            except _FanOutStoreCorruptError as exc:
                return (
                    RunResult(
                        workflow_id=manifest_entry.workflow_id,
                        run_id=run_id,
                        status=RunStatus.FAILED,
                        terminal_step_index=None,
                        partial_state=None,
                        final_state=None,
                        fail_class=f"fan-out-crash-resume-store-corrupt: {exc}",
                    ),
                    0,
                )
            except _FanOutStoreTimeoutAmbiguousError as exc:
                return (
                    RunResult(
                        workflow_id=manifest_entry.workflow_id,
                        run_id=run_id,
                        status=RunStatus.FAILED,
                        terminal_step_index=None,
                        partial_state=None,
                        final_state=None,
                        fail_class=f"fan-out-crash-resume-timeout-ambiguous: {exc}",
                    ),
                    0,
                )
            except _FanOutStoreOrchestratorMaybeRanError as exc:
                return (
                    RunResult(
                        workflow_id=manifest_entry.workflow_id,
                        run_id=run_id,
                        status=RunStatus.FAILED,
                        terminal_step_index=None,
                        partial_state=None,
                        final_state=None,
                        fail_class=f"fan-out-crash-resume-orchestrator-maybe-ran: {exc}",
                    ),
                    0,
                )
            # RECONCILER fan-out replay returns before the linear engine branch below, so a present
            # engine pause record must fire the engine-layer pause/CAS gate before branch-store
            # replay/finalization. Branch records (and synthesis records) are reserve-before-commit:
            # a crash can leave them complete before the fan-out barrier/result committed. Only the
            # post-finalization marker below proves a prior clean resume already crossed the
            # strategy commit boundary and may skip a duplicate CAS on an idempotent re-drive.
            # The one-shot CAS itself is attempted inside the fan-out strategy AFTER its pure
            # replay/body material-diff checks, so an invalid retry cannot consume the claim.
            if manifest_entry.engine_class is EngineClass.RECONCILER_LOOP and not (
                _reconciler_fanout_resume_finalized(_crash_replay_store, run_idempotency_key)
            ):
                _reconciler_fanout_engine_resume_required = True
            # B-FANOUT-CRASH-RESUME-CASCADE-POLICY — strict-tier CARDINALITY-ONLY fail-closed
            # (out-of-family Codex [P1] round-2). The fan-out cardinality marker is written ONCE
            # on a fresh run BEFORE any branch dispatches (`record_fanout_cardinality`, the
            # `not _is_resume` gate). So a crash AFTER the marker but before any branch is
            # readably captured leaves `_determine_fanout_resume` → None (nothing recoverable)
            # WHILE a cardinality marker IS present: the fan-out STARTED, and a branch may have
            # dispatched its effect but crashed before its capture. For the strict tiers (PAUSE /
            # CASCADE_CANCEL) treating this as a FRESH run would re-dispatch the maybe-run branch
            # → double-fire an effect-BEARING branch. Fail CLOSED — the incomplete-recovery rule
            # extends to the cardinality-only store. NO marker → the fan-out never started →
            # genuinely fresh (continue, all policies). PROCEED with a marker also continues (the
            # SOLO tier accepts the dispatch-before-capture window — PR1, unchanged).
            if _crash_fan_out_resume is None:
                _cardinality_policy = d4_tunable(
                    lookup_cell(manifest_entry.workload_class, manifest_entry.engine_class),
                    manifest_entry.persona_tier,
                ).cascade_policy
                _cardinality = _crash_replay_store.read_fanout_cardinality(run_idempotency_key)
                if (
                    _cardinality_policy is not CascadePolicy.PROCEED
                    and _cardinality is None
                    and _crash_replay_store.fanout_cardinality_present(run_idempotency_key)
                ):
                    # PRESENT-but-TORN cardinality marker (`read_fanout_cardinality` → None
                    # because unreadable, but `fanout_cardinality_present` → True): the run
                    # ADVANCED past the cardinality write, so its maybe-ran branches' effects MAY
                    # have fired. Treating torn-as-absent here would skip the strict-tier maybe-ran
                    # analysis below and FRESH-re-dispatch every branch → double-fire an
                    # effect-bearing maybe-ran branch. Fail closed (mirror the
                    # `_determine_fanout_resume` changed-cardinality guard + the orchestrator
                    # `_downstream_artifact_present` check; `[[durable-recovery-presence-validity-
                    # scope]]`: presence ≠ validity). A genuinely-absent marker (None + not present)
                    # falls through to the unchanged fresh-run (never started).
                    return (
                        RunResult(
                            workflow_id=manifest_entry.workflow_id,
                            run_id=run_id,
                            status=RunStatus.FAILED,
                            terminal_step_index=None,
                            partial_state=None,
                            final_state=None,
                            fail_class=(
                                "fan-out-crash-resume-cardinality-marker-torn: a "
                                f"{_cardinality_policy.value} fan-out's cardinality marker is "
                                "present but unreadable (the run advanced past the cardinality "
                                "write) — fail closed rather than fresh-re-dispatching maybe-ran "
                                "branches whose effects may have fired"
                            ),
                        ),
                        0,
                    )
                if _cardinality_policy is not CascadePolicy.PROCEED and _cardinality is not None:
                    # B-FANOUT-CRASH-RESUME-STRICT-TIER-INCOMPLETE (R-FS-1) — lift the
                    # cardinality-only fail-closed when reserve-before-dispatch PROVES no branch
                    # began. An INSTRUMENTED run (the dispatch stamp) with ZERO dispatch markers
                    # means the fan-out started (cardinality present) but no branch dispatched its
                    # effect → a fresh re-dispatch is at-most-once-safe (fall through; the run
                    # proceeds fresh, re-dispatching every branch first-and-only). An UN-stamped
                    # (pre-arc) journal → fail closed: it carries no markers for ANY branch,
                    # including a maybe-ran one, so "no marker" can't be trusted as "not-run"
                    # (`[[durable-recovery-presence-validity-scope]]`).
                    #
                    # B-FANOUT-CRASH-RESUME-MAYBE-RAN-RESOLUTION (R-FS-1) — a dispatch marker means
                    # at least one branch is MAYBE-RAN (dispatched, no capture). Distinguish by step
                    # KIND (same per-kind split + helper as the incomplete-recovery path): a
                    # maybe-ran branch of a RE-FIRE-SAFE kind (DECLARATIVE_STEP / INFERENCE_STEP) is
                    # safe to re-dispatch fresh → fall through; only a maybe-ran branch of an
                    # EFFECT-BEARING kind (or an out-of-range / stale-store ordinal) forces the
                    # fail-closed (`_cardinality` is the declared fan-out branch count bound).
                    _instrumented = _crash_replay_store.dispatch_instrumented(run_idempotency_key)
                    _dispatched_kinds = _crash_replay_store.dispatched_branch_kinds(
                        run_idempotency_key
                    )
                    # The dispatch markers are keyed by WORKER/PEER ordinal (synthesis-excluded,
                    # orchestrator-excluded). The out-of-range bound is therefore the worker/branch
                    # count — NOT `_cardinality` (the recorded `len(steps)`, which for
                    # ORCHESTRATOR_WORKERS / HIERARCHICAL_DELEGATION INCLUDES the orchestrator
                    # `steps[0]`, so `_cardinality` would over-count by one and let a stale
                    # `branch-{worker_count}.dispatched` marker slip through as in-range; out-of-
                    # family Codex [P2]). Computed from `_crash_branch_steps` (synthesis already
                    # split off) the same way as the incomplete-recovery `_expected_branches`.
                    _card_branch_count = len(_crash_branch_steps) - (
                        1
                        if manifest_entry.topology_pattern
                        in {
                            TopologyPattern.ORCHESTRATOR_WORKERS,
                            TopologyPattern.HIERARCHICAL_DELEGATION,
                        }
                        else 0
                    )
                    _dispatched_unsafe = _refire_unsafe_branch_indices(
                        set(_dispatched_kinds), _dispatched_kinds, _card_branch_count
                    )
                    # B-FANOUT-EFFECT-FENCE-BRANCH-PAUSE (R-FS-1) — narrow the unsafe set: a
                    # maybe-ran TOOL_STEP branch is FENCE-RECOVERABLE (a fresh re-run re-dispatches
                    # it into the auto-active effect fence, whose held prior claim makes the re-fire
                    # at-most-once at the tool sink — suppress / ambiguous-PAUSE / fresh-fire)
                    # PROVIDED the RESUMED branch at that ordinal is ALSO a TOOL_STEP (else the
                    # re-dispatch reaches no fence → the changed-kind guard fails it closed). Only
                    # the genuinely-
                    # unrecoverable remainder (SUB_AGENT / MANAGED / out-of-range / un-kinded /
                    # changed-to-non-TOOL) forces the fail-closed.
                    _dispatched_resumed_kinds = _resumed_branch_kinds_by_ordinal(
                        _crash_branch_steps,
                        branch_count=_card_branch_count,
                        orchestrated=manifest_entry.topology_pattern
                        in {
                            TopologyPattern.ORCHESTRATOR_WORKERS,
                            TopologyPattern.HIERARCHICAL_DELEGATION,
                        },
                    )
                    _dispatched_resumed_step_ids = _resumed_branch_step_ids_by_ordinal(
                        _crash_branch_steps,
                        branch_count=_card_branch_count,
                        orchestrated=manifest_entry.topology_pattern
                        in {
                            TopologyPattern.ORCHESTRATOR_WORKERS,
                            TopologyPattern.HIERARCHICAL_DELEGATION,
                        },
                    )
                    _dispatched_fail_closed = _fence_unrecoverable_maybe_ran_indices(
                        _dispatched_unsafe,
                        _dispatched_kinds,
                        _dispatched_resumed_kinds,
                        _card_branch_count,
                        dispatched_step_ids=_crash_replay_store.dispatched_branch_step_ids(
                            run_idempotency_key
                        ),
                        resumed_step_ids=_dispatched_resumed_step_ids,
                        subagent_recoverable_indexes=(
                            _subagent_recoverable_marker_indexes(
                                _crash_replay_store, run_idempotency_key
                            )
                        ),
                        resumed_subagent_recoverable_indexes=(
                            _resumed_subagent_recoverable_by_ordinal(
                                _crash_branch_steps,
                                branch_count=_card_branch_count,
                                orchestrated=manifest_entry.topology_pattern
                                in {
                                    TopologyPattern.ORCHESTRATOR_WORKERS,
                                    TopologyPattern.HIERARCHICAL_DELEGATION,
                                },
                            )
                        ),
                        dispatched_child_engines=_dispatched_child_engines_from_marker(
                            _crash_replay_store, run_idempotency_key
                        ),
                        resumed_child_engines=_resumed_subagent_child_engine_by_ordinal(
                            _crash_branch_steps,
                            branch_count=_card_branch_count,
                            orchestrated=manifest_entry.topology_pattern
                            in {
                                TopologyPattern.ORCHESTRATOR_WORKERS,
                                TopologyPattern.HIERARCHICAL_DELEGATION,
                            },
                        ),
                    )
                    if not (_instrumented and not _dispatched_fail_closed):
                        return (
                            RunResult(
                                workflow_id=manifest_entry.workflow_id,
                                run_id=run_id,
                                status=RunStatus.FAILED,
                                terminal_step_index=None,
                                partial_state=None,
                                final_state=None,
                                fail_class=(
                                    "fan-out-crash-resume-cascade-policy-incomplete-recovery: a "
                                    f"{_cardinality_policy.value} fan-out STARTED (cardinality "
                                    "marker present) but NOTHING is readably recoverable and the "
                                    "reserve-before-dispatch markers do not prove every branch "
                                    "re-dispatch-safe (a maybe-ran branch that is neither re-fire-"
                                    "safe NOR a fence-recoverable TOOL_STEP — an unfenced "
                                    "MANAGED_AGENTS, a SUB_AGENT_DISPATCH, an out-of-range "
                                    "ordinal, or a pre-arc un-stamped journal) — a fresh re-run "
                                    "would risk double-firing an effect these tiers must not. "
                                    "Fail closed (re-fire-safe + fence-recoverable TOOL_STEP "
                                    "maybe-ran kinds now recover; the cardinality-only case)"
                                ),
                            ),
                            0,
                        )
            # B-FANOUT-CRASH-RESUME-CASCADE-POLICY (R-FS-1) — cascade-policy-AWARE crash-resume,
            # the STRICT-TIER-conservative version (out-of-family Codex [P1] + advisor reconcile).
            # The cascade_policy (§25.15.1) governs the run's ON-A-BRANCH-FAILURE semantics. For
            # the strict tiers — PAUSE (TEAM_BINDING) + CASCADE_CANCEL (MULTI_TENANT_COMPLIANCE) —
            # a crash-resume CONTINUES the fan-out ONLY when the recovery is provably safe: the
            # recovered branch set is COMPLETE (every declared ordinal recovered → NOTHING to
            # re-dispatch) AND no branch errored. Every other state fails closed:
            #   • an ERRORED branch (a failure fired the policy before the crash — captured
            #     `completed`-no-output; in the recovered set `output is None` ⟺ ran-and-errored
            #     OR a `RECOVER_AS_TERMINAL`-recovered deadline-cut branch [B-FANOUT-CRASH-RESUME-
            #     TIMEOUT-REPLAY, CP spec v1.63 §2] — both degraded non-contributors, treated
            #     identically here; under `FAIL_CLOSED`/`RE_DISPATCH` `_determine_fanout_resume`
            #     raises/excludes the timed_out branch so it never reaches the recovered set) →
            #     the policy's failure semantics: CASCADE_CANCEL → reproduce FAILED (§25.15.1),
            #     PAUSE → fail-closed-ambiguous.
            #   • an INCOMPLETE recovery (a branch ordinal absent) → fail CLOSED. An absent
            #     branch is INDISTINGUISHABLE from one that RAN its effect but crashed BEFORE
            #     `_capture_branch_terminal` persisted (the dispatch-before-capture window), so
            #     re-dispatching it would risk DOUBLE-FIRING an effect — branches are effect-
            #     BEARING (unlike the effect-free synthesis), and PR1 deliberately failed closed
            #     here for these tiers. True at-most-once branch dispatch (reserve-before-
            #     dispatch — so an absent branch is provably not-yet-run) is the registered
            #     B-FANOUT-CRASH-RESUME-STRICT-TIER-INCOMPLETE follow-on.
            # The COMPLETE ∧ no-error continue path re-dispatches NOTHING → it finalizes from the
            # recovered set (trivially safe). Worker-branch PROCEED is unchanged (PR1 re-dispatches
            # absent branches — the SOLO tier accepts the dispatch-before-capture window). An
            # errored
            # ORCHESTRATOR is NOT a cascade trigger (it returns FAILED directly before any
            # worker; `cascade_policy governs WORKER failure` — `workflow_driver.py:6548`), so
            # detection over `.branches` (workers/peers) is complete (orchestrator-workers
            # witnesses VERIFY this, advisor BLOCKING catch). `worker_count` (FanOutResumeState)
            # / `branch_count` (PeerFanOutResumeState) is the declared total bounding the
            # recovered set (both carriers; per-topology getattr-fallback).
            if _crash_fan_out_resume is not None:
                _crash_cascade_policy = d4_tunable(
                    lookup_cell(manifest_entry.workload_class, manifest_entry.engine_class),
                    manifest_entry.persona_tier,
                ).cascade_policy
                if _crash_cascade_policy is not CascadePolicy.PROCEED:
                    _crash_degraded = any(b.output is None for b in _crash_fan_out_resume.branches)
                    # The PAUSE TRIGGER is a genuine branch FAILURE (`terminal_status ==
                    # "completed"` + no output = ran-and-errored), NOT a RECOVER_AS_TERMINAL
                    # `timed_out` branch (a degraded non-contributor — a live timeout is
                    # `deadline_struck`→FAILED, never a pause). Distinct from `_crash_degraded`
                    # (output-None, which a recovered timeout also satisfies) so a timeout-only
                    # degraded recovery is NOT a lost-pause trigger (out-of-family Codex [P2]).
                    _crash_pause_trigger = any(
                        b.terminal_status == "completed" and b.output is None
                        for b in _crash_fan_out_resume.branches
                    )
                    _crash_expected = getattr(_crash_fan_out_resume, "worker_count", None)
                    if _crash_expected is None:
                        _crash_expected = getattr(_crash_fan_out_resume, "branch_count", 0)
                    _crash_complete = len(_crash_fan_out_resume.branches) == _crash_expected
                    if _crash_cascade_policy is CascadePolicy.CASCADE_CANCEL and _crash_degraded:
                        # A branch FAILED → cascade-cancel fired → reproduce FAILED (§25.15.1 +
                        # obligation 6). Store-only audit (NOT ledger re-materialization — the
                        # deliberate choice): the §25.12 disposition keystone IS the durable
                        # crash-recovery audit substrate, and a FAILED run attests no aggregate.
                        return (
                            RunResult(
                                workflow_id=manifest_entry.workflow_id,
                                run_id=run_id,
                                status=RunStatus.FAILED,
                                terminal_step_index=None,
                                partial_state=None,
                                final_state=None,
                                fail_class=(
                                    "fan-out-crash-resume-cascade-cancel: a branch failed before "
                                    "the crash under CascadePolicy.CASCADE_CANCEL — reproduce "
                                    "RunStatus.FAILED (§25.15.1); the cancelled siblings stay "
                                    "cancelled (never re-dispatched — they would run effects the "
                                    "compliance tier stopped; obligation 7)"
                                ),
                            ),
                            0,
                        )
                    if (
                        _crash_cascade_policy is CascadePolicy.PAUSE
                        and _crash_pause_trigger
                        and not _crash_complete
                    ):
                        # B-FANOUT-CRASH-RESUME-PAUSE-RECONSTRUCT (R-FS-1, CP spec v1.68 §1/§2 +
                        # v1.70 §1 + v1.71 §1) — a branch FAILED → `pause` was triggered, but the
                        # durable PauseSnapshot write was LOST in the crash. The COMPLETE-recovery
                        # case (every declared ordinal recovered) is LIFTED below (falls through to
                        # the strategy, which re-establishes the PAUSED state — the store provably
                        # holds every finished-in-flight branch, so §25.15.1 does not apply). The
                        # INCOMPLETE case (an absent ordinal) re-establishes PAUSED OMITTING every
                        # absent ordinal whose RESUME re-dispatch is at-most-once-safe, else fails:
                        #   • NOT-YET-DISPATCHED (instrumented + NO dispatch marker, the v1.60
                        #     reserve-before-dispatch proof) → re-dispatchable fresh, first-and-only
                        #     (B-FANOUT-CRASH-RESUME-PAUSE-RECONSTRUCT-NOT-YET-DISPATCHED, v1.70).
                        #   • RE-FIRE-SAFE MAYBE-RAN (a dispatch marker, NO terminal capture) of a
                        #     DECLARATIVE_STEP / INFERENCE_STEP kind — NO external effect to
                        #     double-fire → re-dispatchable ON RESUME regardless of the resume
                        #     manifest (B-FANOUT-CRASH-RESUME-PAUSE-RECONSTRUCT-MAYBE-RAN, v1.71).
                        #     The strategy omits every not-yet-recovered ordinal → an empty/partial
                        #     branch_plan → the v1.68 `_crash_pause_reestablish` / `not branch_plan`
                        #     re-establish block builds the snapshot from recovered terminals
                        #     OMITTING the absent; `api.resume` re-dispatches the omitted
                        #     (snapshot-absent ⟹ re-dispatched, keyed on `branch_index in
                        #     _recovered_terminal` below, NOT a divergent terminal_status read).
                        #   • FENCE-RECOVERABLE MAYBE-RAN (TOOL_STEP / MANAGED_AGENTS) is NOT
                        #     recovered here — it STAYS fail-closed. Unlike the §25.15 incomplete
                        #     leg (re-dispatch at CRASH-TIME with the SAME manifest → the fence key
                        #     (idempotency_key, step_id) matches the held claim), this reconstruct
                        #     OMITS the ordinal and DEFERS the re-dispatch to `api.resume`
                        #     (operator-mediated; the manifest may be edited). The snapshot carries
                        #     NO step_id for the omitted ordinal, so a same-kind CHANGED-step_id
                        #     resume would re-dispatch under a DIFFERENT fence key, miss the held
                        #     claim, and DOUBLE-FIRE the maybe-fired effect (out-of-family Codex
                        #     [P1]; the orchestrator's same-step_id guard at its OWN re-dispatch
                        #     site does not generalize to a deferred-omitted worker). A fix needs
                        #     the snapshot to CARRY the marker (idempotency_key, step_id) per
                        #     omitted ordinal + compare at `api.resume` → the registered
                        #     B-FANOUT-CRASH-RESUME-PAUSE-RECONSTRUCT-MAYBE-RAN-FENCE-STEP-ID
                        #     follow-on (the same hole at LOWER exposure is in #742's crash-time
                        #     predicate; folded into that follow-on's scope).
                        #   • GENUINELY-UNRECOVERABLE MAYBE-RAN (SUB_AGENT_DISPATCH recursive child,
                        #     un-kinded / out-of-range) OR a pre-arc un-instrumented journal → still
                        #     doubly-ambiguous, no at-most-once proof → STAYS fail-closed → HITL.
                        # Fidelity (NOT a byte-match of a live pause): the omitted ordinal fires on
                        # RESUME, so a re-fire-safe branch's disposition CAN differ from the
                        # live-pause baseline (it re-runs) — the §14.8.8.7 invariant-3 re-ask
                        # semantic (already-committed). At-most-once + the operator's pause election
                        # PRESERVED (re-fire-safe has NO external effect to double-fire under ANY
                        # resume manifest — the step_id hazard above is fence-effect-only).
                        _pr_instrumented = _crash_replay_store.dispatch_instrumented(
                            run_idempotency_key
                        )
                        _pr_recovered_indexes = {
                            b.branch_index for b in _crash_fan_out_resume.branches
                        }
                        _pr_maybe_ran = (
                            _crash_replay_store.present_dispatched_indexes(run_idempotency_key)
                            - _pr_recovered_indexes
                        )
                        # The maybe-ran ordinals split THREE ways. (1) RE-FIRE-SAFE
                        # (DECLARATIVE_STEP / INFERENCE_STEP — `_refire_unsafe_branch_indices`
                        # excludes them) → re-pause OMITTING (api.resume re-dispatches fresh; no
                        # external effect to double-fire). (2) FENCE-RECOVERABLE (TOOL_STEP /
                        # MANAGED_AGENTS — re-fire-unsafe but the original effect reached a fence
                        # at its own sink) → carried into the reconstructed
                        # `effect_fence_paused_branches` (B-FANOUT-CRASH-RESUME-PAUSE-RECONSTRUCT-
                        # MAYBE-RAN-FENCE-STEP-ID, this arc): api.resume re-dispatches each THROUGH
                        # the resume material-diff guard (changed-step_id / changed-kind → fail
                        # closed, closing the #756 Codex [P1] deferred-resume hole) + the
                        # auto-active fence (at-most-once at the tool sink). (3) GENUINELY-
                        # UNRECOVERABLE (SUB_AGENT recursive child / un-kinded / out-of-range / a
                        # crash-time CHANGED-step_id) → STILL fail closed. The split reuses
                        # `_fence_unrecoverable_maybe_ran_indices` (the §25.15 SoT classifier): at
                        # reconstruct the resumed manifest IS the crash manifest, so its same-kind
                        # / same-step_id conjuncts pass trivially — the real changed-manifest
                        # protection rides on the resume-side guard on the carried entries.
                        _pr_maybe_ran_kinds = _crash_replay_store.dispatched_branch_kinds(
                            run_idempotency_key
                        )
                        _pr_maybe_ran_unsafe = _refire_unsafe_branch_indices(
                            _pr_maybe_ran, _pr_maybe_ran_kinds, _crash_expected
                        )
                        _pr_orchestrated = manifest_entry.topology_pattern in {
                            TopologyPattern.ORCHESTRATOR_WORKERS,
                            TopologyPattern.HIERARCHICAL_DELEGATION,
                        }
                        _pr_marker_step_ids = _crash_replay_store.dispatched_branch_step_ids(
                            run_idempotency_key
                        )
                        _pr_resumed_kinds = _resumed_branch_kinds_by_ordinal(
                            _crash_branch_steps,
                            branch_count=_crash_expected,
                            orchestrated=_pr_orchestrated,
                        )
                        _pr_resumed_step_ids = _resumed_branch_step_ids_by_ordinal(
                            _crash_branch_steps,
                            branch_count=_crash_expected,
                            orchestrated=_pr_orchestrated,
                        )
                        _pr_fence_unrecoverable = _fence_unrecoverable_maybe_ran_indices(
                            _pr_maybe_ran_unsafe,
                            _pr_maybe_ran_kinds,
                            _pr_resumed_kinds,
                            _crash_expected,
                            dispatched_step_ids=_pr_marker_step_ids,
                            resumed_step_ids=_pr_resumed_step_ids,
                            subagent_recoverable_indexes=(
                                _subagent_recoverable_marker_indexes(
                                    _crash_replay_store, run_idempotency_key
                                )
                            ),
                            resumed_subagent_recoverable_indexes=(
                                _resumed_subagent_recoverable_by_ordinal(
                                    _crash_branch_steps,
                                    branch_count=_crash_expected,
                                    orchestrated=_pr_orchestrated,
                                )
                            ),
                            dispatched_child_engines=_dispatched_child_engines_from_marker(
                                _crash_replay_store, run_idempotency_key
                            ),
                            resumed_child_engines=_resumed_subagent_child_engine_by_ordinal(
                                _crash_branch_steps,
                                branch_count=_crash_expected,
                                orchestrated=_pr_orchestrated,
                            ),
                        )
                        if _pr_instrumented and not _pr_fence_unrecoverable:
                            _crash_pause_reconstruct_no_dispatch = True
                            # FENCE-RECOVERABLE = the re-fire-unsafe ordinals MINUS the
                            # genuinely-unrecoverable. Carry each as an
                            # EffectFencePausedBranchResumeState; the marker step_id + kind are
                            # guaranteed present (the recoverable filter excludes None /
                            # out-of-range). idempotency_key="" is the defensive default: CP cannot
                            # derive the runtime fence key (it composes the opaque `tool_id`, X-AL
                            # axis isolation) — but the operator is NEVER asked at reconstruct time
                            # (this was not a pause they saw), so there is no resolution to key-bind
                            # on the first resume. api.resume re-dispatches FRESH into the
                            # auto-active fence (keyed on the RUNTIME-computed key, not this ""),
                            # which re-pauses with the REAL key (captured off the runtime error) if
                            # still ambiguous → a subsequent resume key-binds the operator
                            # resolution. At-most-once rides entirely on the durable fsynced reserve
                            # + the resume-side step_id/kind guard, never this carried key.
                            _pr_fence_recoverable = _pr_maybe_ran_unsafe - _pr_fence_unrecoverable
                            _crash_pause_reconstruct_fence_paused = tuple(
                                EffectFencePausedBranchResumeState(
                                    branch_index=_bi,
                                    step_id=str(_pr_marker_step_ids[_bi]),
                                    step_kind=str(_pr_maybe_ran_kinds[_bi]),
                                    idempotency_key="",
                                )
                                for _bi in sorted(_pr_fence_recoverable)
                            )
                        else:
                            return (
                                RunResult(
                                    workflow_id=manifest_entry.workflow_id,
                                    run_id=run_id,
                                    status=RunStatus.FAILED,
                                    terminal_step_index=None,
                                    partial_state=None,
                                    final_state=None,
                                    fail_class=(
                                        "fan-out-crash-resume-pause-trigger-ambiguous: a branch "
                                        "failed before the crash under CascadePolicy.PAUSE with an "
                                        "INCOMPLETE recovery whose absent ordinal is a maybe-ran "
                                        "branch that is neither re-fire-safe nor fence-recoverable "
                                        "(a SUB_AGENT_DISPATCH recursive child; an un-kinded / "
                                        "out-of-range ordinal; or a crash-time CHANGED-step_id "
                                        "edited manifest) or a pre-arc un-instrumented journal — a "
                                        "reconstruct cannot honor §25.15.1 'finish in-flight, then "
                                        "pause' at-most-once; fail closed (the complete-recovery + "
                                        "provably-not-yet-dispatched + re-fire-safe-maybe-ran + "
                                        "fence-recoverable-maybe-ran windows reconstruct)"
                                    ),
                                ),
                                0,
                            )
                    if not _crash_complete and not _crash_pause_reconstruct_no_dispatch:
                        # B-FANOUT-CRASH-RESUME-STRICT-TIER-INCOMPLETE (R-FS-1) — lift the
                        # incomplete-recovery fail-closed when reserve-before-dispatch PROVES every
                        # absent branch not-yet-run. A branch is MAYBE-RAN iff it has a dispatch
                        # marker but NO terminal-capture record (the narrow fire→capture window):
                        # `present_dispatched_indexes − recovered`. This is index-scheme-agnostic
                        # (no `range(expected)` reconstruction). A provably-not-run branch (no
                        # marker, INSTRUMENTED) is always re-dispatchable (the marker is the
                        # at-most-once proof). B-FANOUT-CRASH-RESUME-MAYBE-RAN-RESOLUTION extends
                        # the fall-through to maybe-ran branches of a RE-FIRE-SAFE kind
                        # (DECLARATIVE_STEP / INFERENCE_STEP — no non-idempotent external effect);
                        # only a maybe-ran branch of an EFFECT-BEARING kind (or a pre-arc un-stamped
                        # journal — no markers to trust, presence ≠ validity
                        # [[durable-recovery-presence-validity-scope]]) forces the conservative
                        # fail-closed.
                        _instrumented = _crash_replay_store.dispatch_instrumented(
                            run_idempotency_key
                        )
                        _recovered_indexes = {
                            b.branch_index for b in _crash_fan_out_resume.branches
                        }
                        _maybe_ran = (
                            _crash_replay_store.present_dispatched_indexes(run_idempotency_key)
                            - _recovered_indexes
                        )
                        # B-FANOUT-CRASH-RESUME-MAYBE-RAN-RESOLUTION (R-FS-1) — the #732 arc
                        # failed CLOSED on ANY maybe-ran branch (conservative: "branches are
                        # effect-BEARING"). Distinguish by step KIND: a maybe-ran branch of a
                        # RE-FIRE-SAFE kind (DECLARATIVE_STEP / INFERENCE_STEP — no non-idempotent
                        # external effect) is SAFE to re-dispatch fresh (re-dispatch cannot
                        # double-fire — an LLM inference / declarative transform has no external
                        # side-effect; the unfenced linear-resume posture already re-dispatches
                        # inference). A maybe-ran TOOL_STEP is SAFE a DIFFERENT way — its effect is
                        # FENCED (B-FANOUT-EFFECT-FENCE-BRANCH-PAUSE below). Only a maybe-ran branch
                        # that is NEITHER (SUB_AGENT_DISPATCH / MANAGED_AGENTS, or an out-of-bounds
                        # ordinal) forces the conservative fail-closed — its effect MAY have fired
                        # with no fence to make the re-dispatch at-most-once.
                        _maybe_ran_kinds = _crash_replay_store.dispatched_branch_kinds(
                            run_idempotency_key
                        )
                        _maybe_ran_unsafe = _refire_unsafe_branch_indices(
                            _maybe_ran, _maybe_ran_kinds, _crash_expected
                        )
                        # B-FANOUT-EFFECT-FENCE-BRANCH-PAUSE (R-FS-1) — narrow the v1.62 effect-
                        # bearing fail-closed: of the re-fire-UNSAFE maybe-ran branches, a TOOL_STEP
                        # is FENCE-RECOVERABLE (re-dispatch re-reaches the auto-active runtime fence
                        # — at-most-once at the tool sink: suppress-if-captured / ambiguous-PAUSE /
                        # fresh-fire-if-claim-absent) — PROVIDED the RESUMED branch at that ordinal
                        # is ALSO a TOOL_STEP (the changed-kind guard; a TOOL→non-TOOL resume
                        # reaches no fence → fail closed). Only the genuinely-unrecoverable
                        # remainder (SUB_AGENT_DISPATCH recursive child / MANAGED_AGENTS unfenced /
                        # out-of-range / un-kinded / changed-to-non-TOOL) forces the fail-closed.
                        _maybe_ran_resumed_kinds = _resumed_branch_kinds_by_ordinal(
                            _crash_branch_steps,
                            branch_count=_crash_expected,
                            orchestrated=manifest_entry.topology_pattern
                            in {
                                TopologyPattern.ORCHESTRATOR_WORKERS,
                                TopologyPattern.HIERARCHICAL_DELEGATION,
                            },
                        )
                        _maybe_ran_resumed_step_ids = _resumed_branch_step_ids_by_ordinal(
                            _crash_branch_steps,
                            branch_count=_crash_expected,
                            orchestrated=manifest_entry.topology_pattern
                            in {
                                TopologyPattern.ORCHESTRATOR_WORKERS,
                                TopologyPattern.HIERARCHICAL_DELEGATION,
                            },
                        )
                        _maybe_ran_fail_closed = _fence_unrecoverable_maybe_ran_indices(
                            _maybe_ran_unsafe,
                            _maybe_ran_kinds,
                            _maybe_ran_resumed_kinds,
                            _crash_expected,
                            dispatched_step_ids=_crash_replay_store.dispatched_branch_step_ids(
                                run_idempotency_key
                            ),
                            resumed_step_ids=_maybe_ran_resumed_step_ids,
                            subagent_recoverable_indexes=(
                                _subagent_recoverable_marker_indexes(
                                    _crash_replay_store, run_idempotency_key
                                )
                            ),
                            resumed_subagent_recoverable_indexes=(
                                _resumed_subagent_recoverable_by_ordinal(
                                    _crash_branch_steps,
                                    branch_count=_crash_expected,
                                    orchestrated=manifest_entry.topology_pattern
                                    in {
                                        TopologyPattern.ORCHESTRATOR_WORKERS,
                                        TopologyPattern.HIERARCHICAL_DELEGATION,
                                    },
                                )
                            ),
                            dispatched_child_engines=_dispatched_child_engines_from_marker(
                                _crash_replay_store, run_idempotency_key
                            ),
                            resumed_child_engines=_resumed_subagent_child_engine_by_ordinal(
                                _crash_branch_steps,
                                branch_count=_crash_expected,
                                orchestrated=manifest_entry.topology_pattern
                                in {
                                    TopologyPattern.ORCHESTRATOR_WORKERS,
                                    TopologyPattern.HIERARCHICAL_DELEGATION,
                                },
                            ),
                        )
                        if not (_instrumented and not _maybe_ran_fail_closed):
                            return (
                                RunResult(
                                    workflow_id=manifest_entry.workflow_id,
                                    run_id=run_id,
                                    status=RunStatus.FAILED,
                                    terminal_step_index=None,
                                    partial_state=None,
                                    final_state=None,
                                    fail_class=(
                                        "fan-out-crash-resume-cascade-policy-incomplete-"
                                        "recovery: a "
                                        f"{_crash_cascade_policy.value} crash-resume recovered an "
                                        "INCOMPLETE branch set with a maybe-ran branch that is "
                                        "neither re-fire-safe NOR a fence-recoverable TOOL_STEP "
                                        "(an unfenced MANAGED_AGENTS, a SUB_AGENT_DISPATCH, an "
                                        "out-of-range ordinal, or a pre-arc un-stamped journal) — "
                                        "dispatched but uncaptured, its effect MAY have fired with "
                                        "no fence to make the re-dispatch at-most-once. Fail "
                                        "closed (re-fire-safe — DECLARATIVE_STEP / INFERENCE_STEP "
                                        "— and fence-recoverable TOOL_STEP maybe-ran kinds now "
                                        "recover; the SUB_AGENT / unfenced-external residual is a "
                                        "registered follow-on)"
                                    ),
                                ),
                                0,
                            )
                        # INSTRUMENTED ∧ every maybe-ran branch recoverable → fall through to
                        # recovery: re-dispatch ALL absent branches — the provably-not-run ones
                        # (the marker proves not-fired), the re-fire-safe maybe-ran ones (no effect
                        # to double-fire), AND the fence-recoverable TOOL_STEP maybe-ran ones (the
                        # auto-active fence resolves the re-dispatch at-most-once at the tool sink:
                        # B-FANOUT-EFFECT-FENCE-BRANCH-PAUSE — its ambiguous-uncommitted error
                        # composes through the barrier to a §26.2 PAUSE, suppresses-and-continues on
                        # a captured output, or fires fresh when the prior attempt left no claim).
                    # COMPLETE ∧ no-error on a strict tier → fall through; the strategy finalizes
                    # from the recovered set (re-dispatches NOTHING → trivially safe). PROCEED
                    # (the `is not PROCEED` guard skips this whole block) → PR1 recovery unchanged.
            # Material-diff fail-closed (out-of-family Codex [P2]): the store holds recovered
            # branch / orchestrator records but the RESUMED manifest carries NO branch steps
            # (a changed body). The strategy's empty-step fast path would return SUCCESS with
            # an empty aggregate BEFORE its resume material-diff guard runs — silently dropping
            # the recovered outputs. Reject here instead (the guard the fast path bypasses).
            if _crash_fan_out_resume is not None and not _crash_branch_steps:
                return (
                    RunResult(
                        workflow_id=manifest_entry.workflow_id,
                        run_id=run_id,
                        status=RunStatus.FAILED,
                        terminal_step_index=None,
                        partial_state=None,
                        final_state=None,
                        fail_class=(
                            "fan-out-crash-resume-material-diff: the store holds recovered "
                            "fan-out state but the resumed manifest has no branch steps "
                            "(changed body) — fail closed rather than drop the recovered outputs"
                        ),
                    ),
                    0,
                )

            # B-FANOUT-OUTPUT-REPLAY PR2 — synthesis-completeness fail-closed (out-of-family
            # Codex [P2] ×2). A captured synthesis PROVES a COMPLETE, SUCCESSFUL fan-out: the
            # synthesis composes over the full OUTPUT-BEARING sibling set, and runs ONLY on
            # RunStatus.SUCCESS. So whenever a synthesis is captured, the store MUST hold a
            # complete, output-bearing, ZERO-RE-DISPATCH fan-out state — every expected branch
            # present + readable + `completed`-WITH-OUTPUT (+ a recovered orchestrator for the
            # orchestrator topologies, which `_determine_fanout_resume` guarantees when it
            # returns non-None). ANY gap → fail CLOSED here, BEFORE any re-dispatch (a re-fired
            # landed effect + a stale/degraded replayed aggregate). This fires INDEPENDENT of
            # `_crash_fan_out_resume`: (a) ALL branches absent → `_determine_fanout_resume`
            # returns None, yet the run must NOT proceed as fresh then replay the stale synthesis;
            # (b) a `completed`-no-output (degraded, effect-landed-no-output) branch keeps the
            # count but is non-output-bearing. `_crash_branch_steps` excludes the carved
            # synthesis; `steps[0]` is the orchestrator for the orchestrator topologies.
            if _crash_replay_store.synthesis_present(run_idempotency_key):
                # Material-diff: the resumed manifest DROPPED the terminal synthesis step
                # (out-of-family Codex [P2] round-3). A captured synthesis but no synthesis
                # in the resumed body would silently return the deterministic FOLD, DISCARDING
                # the captured synthesized aggregate. Fail closed (the symmetric of the
                # `step_id` material-diff that catches a CHANGED synthesis body).
                if not _synth_positions:
                    return (
                        RunResult(
                            workflow_id=manifest_entry.workflow_id,
                            run_id=run_id,
                            status=RunStatus.FAILED,
                            terminal_step_index=None,
                            partial_state=None,
                            final_state=None,
                            fail_class=(
                                "post-join-synthesis-replay-material-diff: a synthesis was "
                                "captured but the resumed manifest has NO terminal "
                                "POST_JOIN_SYNTHESIS step (a changed body) — proceeding would "
                                "silently discard the captured synthesized aggregate for the "
                                "deterministic fold; fail closed"
                            ),
                        ),
                        0,
                    )
                _is_orchestrated = manifest_entry.topology_pattern in {
                    TopologyPattern.ORCHESTRATOR_WORKERS,
                    TopologyPattern.HIERARCHICAL_DELEGATION,
                }
                _expected_branches = len(_crash_branch_steps) - (1 if _is_orchestrated else 0)
                _complete_output_bearing = (
                    _crash_fan_out_resume is not None
                    and len(_crash_fan_out_resume.branches) == _expected_branches
                    and all(b.output is not None for b in _crash_fan_out_resume.branches)
                )
                if not _complete_output_bearing:
                    return (
                        RunResult(
                            workflow_id=manifest_entry.workflow_id,
                            run_id=run_id,
                            status=RunStatus.FAILED,
                            terminal_step_index=_synth_positions[0],
                            partial_state=None,
                            final_state=None,
                            fail_class=(
                                "post-join-synthesis-replay-incomplete-branches: a synthesis "
                                "was captured (proving a complete, successful, output-bearing "
                                "fan-out) but the recovered state is incomplete (an absent / "
                                "corrupt / non-output-bearing branch, or a missing orchestrator) "
                                "— fail closed; a captured synthesis admits only a pure "
                                "zero-re-dispatch replay over the full output-bearing set"
                            ),
                        ),
                        0,
                    )

    # B-FANOUT-OUTPUT-REPLAY PR2 — the PR1 synthesis-bearing crash-resume fail-closed is now
    # RELAXED (CP spec v1.56 §1/§2). A POST_JOIN_SYNTHESIS fan-out that crash-resumes under
    # `CascadePolicy.PROCEED` (the cascade-policy guard above already fails closed for
    # PAUSE + CASCADE_CANCEL) recovers its branches (PR1 net-add #1-3) then, at the strategy's
    # post-barrier `_maybe_post_join_synthesis`, either REPLAYS a captured synthesis output
    # (the W3 crash window — verified by the record-local self-hash + step_id material-diff,
    # reproducible) or dispatches the synthesis FRESH on the reproduced branches (a crash
    # BEFORE the synthesis ran — effect-free, first-and-only, consistent over the same
    # reproduced siblings). Because every recursive HIERARCHICAL child re-enters
    # `_execute_workflow_body`, the per-level synthesis recovers at each level keyed by its
    # own run-scoped store. The PAUSE-resume synthesis path (`resume_snapshot is not None`)
    # is now ALSO supported (B-FANOUT-PAUSE-SYNTHESIS): the top-of-function
    # `_synthesis_resume_material_diff` guard verifies the re-supplied synthesis identity
    # (presence + step_id) against the pause snapshot's captured identity, then the strategy
    # recovers branches + fresh-dispatches the synthesis at the same post-barrier
    # `_maybe_post_join_synthesis` (the synthesis never ran on a pause, so `synthesis_present`
    # is False → fresh dispatch, effect-free + first-and-only). `_synth_positions` is still
    # consumed by the placement guard below.

    if strategy is _DriverStrategyStatus.PARALLELIZATION:
        # B-POSTJOIN-LLM-SYNTHESIS (CP spec v1.54 §3) — carve an opt-in terminal
        # POST_JOIN_SYNTHESIS step out of the peer branch set; the strategy
        # dispatches it post-barrier. `(steps, None)` absent the opt-in →
        # byte-identical to pre-v1.54.
        _branch_steps, _synthesis_step = _split_synthesis(steps)
        return _execute_parallelization(
            manifest_entry=manifest_entry,
            steps=_branch_steps,
            run_id=run_id,
            ctx=ctx,
            default_model_binding=default_model_binding,
            step_dispatchers=step_dispatchers,
            run_idempotency_key=run_idempotency_key,
            # B-FANOUT-PAUSE-PARALLELIZATION — peer fan-out resume re-entry. The
            # snapshot's `peer_fan_out_resume` drives the skip-terminal + re-dispatch
            # path; None on a normal first run.
            resume_snapshot=resume_snapshot,
            # B-FANOUT-OUTPUT-REPLAY — synthetic CRASH-resume state (None unless this run
            # crashed mid-fan-out with ≥1 completed branch in the store); never co-set
            # with `resume_snapshot`.
            crash_fan_out_resume=_crash_fan_out_resume,
            # B-FANOUT-CRASH-RESUME-PAUSE-RECONSTRUCT-NOT-YET-DISPATCHED — re-pause-without-
            # dispatch mode (the dispatcher proved every absent ordinal not-yet-run).
            crash_pause_reconstruct_no_dispatch=_crash_pause_reconstruct_no_dispatch,
            crash_pause_reconstruct_fence_paused=_crash_pause_reconstruct_fence_paused,
            reconciler_engine_resume_required=_reconciler_fanout_engine_resume_required,
            synthesis_step=_synthesis_step,
        )
    if strategy is _DriverStrategyStatus.EVALUATOR_OPTIMIZER:
        return _execute_evaluator_optimizer(
            manifest_entry=manifest_entry,
            steps=steps,
            run_id=run_id,
            ctx=ctx,
            default_model_binding=default_model_binding,
            step_dispatchers=step_dispatchers,
            run_idempotency_key=run_idempotency_key,
            # B-FANOUT-PAUSE-EVALUATOR-OPTIMIZER — iteration-cursor resume re-entry. The
            # snapshot's `evaluator_optimizer_resume` drives the recover-prefix +
            # re-dispatch-from-failed-step path; None on a normal first run.
            resume_snapshot=resume_snapshot,
        )
    if strategy is _DriverStrategyStatus.ORCHESTRATOR_WORKERS:
        # B-POSTJOIN-LLM-SYNTHESIS (CP spec v1.54 §3) — carve an opt-in terminal
        # POST_JOIN_SYNTHESIS step out of the orchestrator+worker step set
        # (`steps[0]`=orchestrator, `steps[1:]`=workers); the strategy dispatches
        # it post-barrier composing the worker siblings. `(steps, None)` absent the
        # opt-in → byte-identical to pre-v1.54.
        _branch_steps, _synthesis_step = _split_synthesis(steps)
        return _execute_orchestrator_workers(
            manifest_entry=manifest_entry,
            steps=_branch_steps,
            run_id=run_id,
            ctx=ctx,
            default_model_binding=default_model_binding,
            step_dispatchers=step_dispatchers,
            run_idempotency_key=run_idempotency_key,
            # B-FANOUT-PAUSE — fan-out resume re-entry (only ORCHESTRATOR_WORKERS
            # materialized this arc; the other non-linear strategies' fan-out/
            # handoff pause-resume are registered forward arcs). `pause_resumable`
            # gates the PAUSED return to this top-level strategy — HIERARCHICAL,
            # which reuses `_execute_orchestrator_workers`, calls it False so its
            # (not-yet-wired) resume cannot advertise a false-resumable PAUSED.
            resume_snapshot=resume_snapshot,
            # B-FANOUT-OUTPUT-REPLAY — synthetic CRASH-resume state (None unless this run
            # crashed mid-fan-out with ≥1 completed worker in the store); never co-set
            # with `resume_snapshot`.
            crash_fan_out_resume=_crash_fan_out_resume,
            # B-FANOUT-CRASH-RESUME-PAUSE-RECONSTRUCT-NOT-YET-DISPATCHED — re-pause-without-
            # dispatch mode (the dispatcher proved every absent worker ordinal not-yet-run).
            crash_pause_reconstruct_no_dispatch=_crash_pause_reconstruct_no_dispatch,
            crash_pause_reconstruct_fence_paused=_crash_pause_reconstruct_fence_paused,
            reconciler_engine_resume_required=_reconciler_fanout_engine_resume_required,
            pause_resumable=True,
            synthesis_step=_synthesis_step,
        )
    if strategy is _DriverStrategyStatus.HIERARCHICAL_DELEGATION:
        # B-HIERARCHICAL-PAUSE (R-FS-1) — HIERARCHICAL now threads the resume
        # snapshot + opts into resumable pause (it reuses _execute_orchestrator_workers
        # at each level, so a level-local worker pause materializes via FanOutResumeState
        # AND a recursive child PAUSE is captured into paused_child_branches + re-entered
        # at the child's cursor on resume — the cross-bootstrap-boundary resume).
        # B-POSTJOIN-LLM-SYNTHESIS (CP spec v1.54 §3) — carve an opt-in TOP-LEVEL
        # terminal POST_JOIN_SYNTHESIS step out of this level's step set; recursive
        # child levels carve their own. `(steps, None)` absent the opt-in →
        # byte-identical to pre-v1.54.
        _branch_steps, _synthesis_step = _split_synthesis(steps)
        return _execute_hierarchical_delegation(
            manifest_entry=manifest_entry,
            steps=_branch_steps,
            run_id=run_id,
            ctx=ctx,
            default_model_binding=default_model_binding,
            step_dispatchers=step_dispatchers,
            run_idempotency_key=run_idempotency_key,
            resume_snapshot=resume_snapshot,
            # B-FANOUT-OUTPUT-REPLAY — synthetic CRASH-resume state, forwarded into the
            # per-level `_execute_orchestrator_workers` (None unless this run crashed
            # mid-fan-out with ≥1 completed worker); never co-set with `resume_snapshot`.
            crash_fan_out_resume=_crash_fan_out_resume,
            # B-FANOUT-CRASH-RESUME-PAUSE-RECONSTRUCT-NOT-YET-DISPATCHED — re-pause-without-
            # dispatch mode forwarded into the per-level orchestrator-workers execution.
            crash_pause_reconstruct_no_dispatch=_crash_pause_reconstruct_no_dispatch,
            crash_pause_reconstruct_fence_paused=_crash_pause_reconstruct_fence_paused,
            reconciler_engine_resume_required=_reconciler_fanout_engine_resume_required,
            pause_resumable=True,
            synthesis_step=_synthesis_step,
        )
    if strategy is _DriverStrategyStatus.DECENTRALIZED_HANDOFF:
        return _execute_decentralized_handoff(
            manifest_entry=manifest_entry,
            steps=steps,
            run_id=run_id,
            ctx=ctx,
            default_model_binding=default_model_binding,
            step_dispatchers=step_dispatchers,
            run_idempotency_key=run_idempotency_key,
            # B-HANDOFF-PAUSE (R-FS-1) — single-owner sequential handoff resume
            # re-entry. The snapshot's `handoff_resume` stage cursor drives the
            # recover-completed-prefix + re-dispatch-from-the-cursor path; None on a
            # normal first run.
            resume_snapshot=resume_snapshot,
        )

    # Selective per-run replay-resumption via N-lookup over the existing
    # IS `read_by_idempotency_key` primitive (CP plan v2.12 §0.1 +
    # §2.9 U-CP-56 AC #6 re-author; operator-ratified Path A-modified —
    # no new IS prefix-match primitive). For each step index, compute the
    # expected per-step idempotency_key and look it up; advance `resume_at`
    # over the contiguous prefix of materialized steps.
    #
    # U-RT-89 (C-RT-24 §14.14.5 invariant 5): explicit-pause resume override.
    # When the entry-point caller supplied `pause_snapshot_input` + the
    # `attempt_resume(...)` returned `resumed=True`, the resume_at_step_index
    # override REPLACES the prefix-replay path. The two paths are mutually
    # exclusive per spec — explicit-pause resumption handles workflow-layer
    # PauseResumeProtocol resume; prefix-replay handles save-point-checkpoint
    # crash-recovery resumption.
    resume_at = 0
    if resume_at_step_index_override is not None:
        resume_at = resume_at_step_index_override
        if resume_at > 0:
            ctx.lifecycle_emitter.emit(WorkflowEventClass.RESUMPTION)
    elif manifest_entry.engine_class is EngineClass.SAVE_POINT_CHECKPOINT:
        resume_at = _determine_resume_at(
            ctx=ctx,
            run_idempotency_key=run_idempotency_key,
            step_count=len(steps),
            workload_class=manifest_entry.workload_class,
        )
        if resume_at > 0:
            ctx.lifecycle_emitter.emit(WorkflowEventClass.RESUMPTION)
    elif manifest_entry.engine_class is EngineClass.EVENT_SOURCED_REPLAY:
        # U-CP-93 (R-FS-1 E-impl-1) — EVENT_SOURCED_REPLAY resumption-routing.
        # Replay from event history = advance resume_at over the contiguous
        # materialized prefix (C-CP-08 §8.1 `engine_replay`: "no re-execution
        # of activities" — the prefix is not re-dispatched). Under the §8.2
        # row 1 reading the event history joins the F2 state-ledger on
        # `idempotency_key`, so resume_at is computed by the same F2-prefix
        # mechanism as save-point (`_determine_event_replay_resume_at`
        # delegates). The §8.1 *cached-output replay* refinement (replay prior
        # outputs into downstream-visible state) is degenerate at HEAD — the F2
        # ledger carries no activity output and the driver threads no
        # inter-step data flow (B-INTERSTEP) — so it is a registered build arc,
        # not this unit's burden. See `.harness/r-fs-1-e-impl-1-finding.md`.
        resume_at = _determine_event_replay_resume_at(
            ctx=ctx,
            run_idempotency_key=run_idempotency_key,
            step_count=len(steps),
            workload_class=manifest_entry.workload_class,
        )
        if resume_at > 0:
            ctx.lifecycle_emitter.emit(WorkflowEventClass.RESUMPTION)
            # B-ENGINE-OUTPUT-REPLAY (runtime spec C-RT-32) — the §8.1 "cached
            # outputs replayed" half: replay the durably-stored prefix outputs into
            # the inter-step channel so the first re-dispatched step reads its
            # recovered predecessor (no-op unless store + channel are bound;
            # fail-closed on a store↔ledger skew gap / body-change identity mismatch).
            _replay_fail = _rehydrate_inter_step_channel_on_replay(
                ctx,
                run_idempotency_key=run_idempotency_key,
                resume_at=resume_at,
                steps=steps,
                workflow_id=manifest_entry.workflow_id,
                run_id=run_id,
            )
            if _replay_fail is not None:
                return _replay_fail, 0
    elif manifest_entry.engine_class is EngineClass.RECONCILER_LOOP:
        # U-CP-96 (R-FS-1 E-impl-3a) — RECONCILER_LOOP convergence resumption.
        # "Re-derive state from declarative CRDs; reconciler-loop converges through
        # compare-and-swap" (C-CP-08 §8.1 `reconciler_converge`): advance resume_at
        # over the contiguous materialized prefix so already-converged steps are not
        # re-dispatched. Under §8.2 row 4 the reconciler reads the F2 state-ledger
        # (joined on `idempotency_key`) to detect prior actions, so the CP-level
        # resume_at is the same F2-prefix computation save-point / event-replay /
        # segment-replay use (`_determine_reconciler_converge_resume_at` delegates) —
        # a CP→IS read, never a read of the engine-owned CRD_RECONCILER_LEDGER /
        # U-RT-123 substrate (no `harness_cp` → `harness_runtime` import; avoids a
        # CP↔RT cycle). reconciler-loop is an ENGINE-OWNS-SUBSTRATE class
        # (`f2_substrate_join_discipline.py:9-12`, grouped with event-sourced-replay),
        # so the AUTHORITATIVE durable reconciler state lives in U-RT-123 (E-impl-3b)
        # and this CP/IS resume_at is DELIBERATELY degenerate vs save-point (the same
        # accepted bar U-CP-93/94 take). The genuine distinguishing RECONCILER_LOOP
        # capabilities — the hand-rolled etcd-style CAS-lease substrate (U-RT-123) +
        # the engine-layer recovery-loop firing (U-CP-97, appended below) — give this
        # DURABLE_ASYNC class the EVENT_SOURCED_REPLAY resume_at shape PLUS the
        # WAL_SEGMENT-style engine-layer firing; the resume_at here is the (A)
        # resumption-semantics half, the (B) recovery-loop firing follows.
        resume_at = _determine_reconciler_converge_resume_at(
            ctx=ctx,
            run_idempotency_key=run_idempotency_key,
            step_count=len(steps),
            workload_class=manifest_entry.workload_class,
        )
        # U-CP-97 (R-FS-1 E-impl-3c) — RECONCILER_LOOP engine-layer recovery-loop
        # RESUME firing. The engine-native reconverge analogue of the workflow-layer
        # resume, mirroring the U-CP-95 WAL_SEGMENT RESUME firing (below) — duck-typed
        # `ctx.engine_recovery_loop` (no `harness_cp` → `harness_runtime` import), gated
        # by this RECONCILER_LOOP branch so the WAL firing + every non-reconciler path
        # stays behavior-unchanged. Fires `attempt_resume` → `cp.resume-attempted`
        # (C-CP-50) against the U-RT-123 reconciler substrate (U-RT-124 binds it
        # engine-class-aware so the reconverge reads the reconciler store, never the WAL
        # segment-log).
        #
        # GATED on (a) the PRESENCE of a pause record (presence, NOT validity — a
        # present-but-corrupt record still FIRES → ABORT_* → fail closed below; an
        # ordinary step-prefix recovery with no engine pause does not fire; `run_id`
        # run-scopes the record, identical key composition to the capture branch; the
        # WAL precedent's Codex [P1]/[P2] discipline) AND (b) `resume_at < len(steps)` —
        # the run is NOT already complete. (b) is the one RECONCILER-SPECIFIC divergence
        # from the WAL branch and is load-bearing: the reconciler substrate's CAS lease
        # makes a SECOND `attempt_resume` of an already-claimed revision ABORT (the
        # genuine new lease-coordination capability; U-RT-123). So once a run has fully
        # completed (every step committed → `resume_at == len(steps)`), an at-least-once
        # re-drive of the SAME run_id has NOTHING to reconverge, and firing would
        # claim-again → ABORT → spuriously FAIL a finished run. Skipping the fire when
        # complete lets the empty step loop return idempotent SUCCESS — satisfying
        # C-CP-07 §7.4 floor (ii) "idempotency-keyed exactly-once via the F2 ledger". The
        # WAL branch (below) carries NO such guard: its re-resumable substrate returns
        # RESUME_CLEAN (not ABORT) on a completed-run re-drive, so it does NOT fail-close
        # — the fail-closed regression this guard fixes is reconciler-only (a milder
        # PRE-EXISTING WAL exactly-once duplicate-emit on the same path is out of scope
        # here and tracked at `.harness/r-fs-1-e-impl-3c-f1-01-wal-exactly-once.md`). (b)
        # is an UPPER bound ONLY — a step-0 engine pause (resume_at == 0) still fires
        # (Codex [P2.b]); it is the incomplete-vs-complete discriminator, NOT a
        # `resume_at > 0` gate.
        _engine_recovery_loop = getattr(ctx, "engine_recovery_loop", None)
        _resume_engine_pause = (
            _engine_recovery_loop is not None
            and resume_at < len(steps)
            and _engine_recovery_loop.has_pause_record(
                engine_class=manifest_entry.engine_class,
                workflow_id=manifest_entry.workflow_id,
                run_id=run_id,
            )
        )
        if resume_at > 0 or _resume_engine_pause:
            ctx.lifecycle_emitter.emit(WorkflowEventClass.RESUMPTION)
        if _resume_engine_pause and _engine_recovery_loop is not None:
            _engine_resume = _run_protocol_method_sync(
                _engine_recovery_loop.attempt_resume(
                    engine_class=manifest_entry.engine_class,
                    workflow_id=manifest_entry.workflow_id,
                    run_id=run_id,
                    step_id=str(resume_at),
                    resume_event_id=f"resume:{run_id}:{resume_at}",
                    resume_attempt_count=1,
                    resume_at=datetime.now(UTC).isoformat(),
                )
            )
            # FAIL CLOSED on an aborting reconverge outcome (C-CP-22 §22.1 ABORT_*),
            # mirroring the WAL RESUME firing: a present pause whose reconverge aborts
            # must HALT the run — never proceed past unrecoverable engine state. The
            # `cp.resume-attempted` entry the fire emitted is the durable audit record.
            _abort_fail_class = _ENGINE_RESUME_ABORT_FAIL_CLASS.get(
                _engine_resume.resume_outcome.outcome_kind
            )
            if _abort_fail_class is not None:
                return RunResult(
                    workflow_id=manifest_entry.workflow_id,
                    run_id=run_id,
                    status=RunStatus.FAILED,
                    terminal_step_index=None,
                    partial_state=None,
                    final_state=None,
                    fail_class=_abort_fail_class,
                ), 0
    elif manifest_entry.engine_class is EngineClass.WAL_SEGMENT:
        # U-CP-94 (R-FS-1 E-impl-2) — WAL_SEGMENT segment-replay resumption.
        # "Replay from WAL segments; per-segment dedup" (C-CP-08 §8.1
        # `segment_replay`): advance resume_at over the contiguous materialized
        # segment prefix. Under §8.2 row 5 the per-segment ledger entries join
        # the F2 state-ledger on `idempotency_key`, so the segment prefix is the
        # same F2-prefix computation save-point / event-replay use
        # (`_determine_segment_replay_resume_at` delegates) — a CP→IS read, never
        # a read of the U-RT-121 runtime segment-log substrate (no CP→runtime
        # import; resolves the only reading that avoids a CP↔RT cycle, U-CP-94
        # AC). As with EVENT_SOURCED_REPLAY this resume_at semantic is degenerate
        # vs save-point at the CP/IS level (same accepted bar); the genuine
        # distinguishing WAL_SEGMENT capability is the durable segment-log
        # substrate (U-RT-121) + the engine-layer recovery-loop firing below
        # (U-CP-95). The §8.1 cached-output-replay refinement (replay prior outputs
        # into downstream-visible state) is now ALSO wired for WAL_SEGMENT
        # (B-ENGINE-OUTPUT-REPLAY-WAL-SEGMENT — the rehydrate block below; shares the
        # EngineOutputStore with EVENT_SOURCED_REPLAY).
        resume_at = _determine_segment_replay_resume_at(
            ctx=ctx,
            run_idempotency_key=run_idempotency_key,
            step_count=len(steps),
            workload_class=manifest_entry.workload_class,
        )
        # U-CP-95 (R-FS-1 E-impl-2) — engine-layer recovery-loop RESUME firing.
        # Fire `ctx.engine_recovery_loop.attempt_resume` → `cp.resume-attempted`
        # (C-CP-50) through the CP→IS wiring (R-CXA-2 engine-layer seam),
        # consumed duck-typed (`Any` on the runtime ctx, exactly as `cp_is_wiring`
        # / `pause_resume_protocol`) — no `harness_cp` → `harness_runtime` import.
        #
        # GATED on the PRESENCE of a pause record (`has_pause_record`, a pure
        # non-emitting substrate read) — presence, NOT validity, and NOT
        # `resume_at`:
        #   - resume_at > 0 alone is the ORDINARY step-prefix crash recovery, which
        #     can occur with NO engine pause captured — firing there would emit a
        #     spurious `cp.resume-attempted = ABORT_SNAPSHOT_CORRUPTED` for a clean
        #     recovery, polluting the ledger (Codex [P2.a]); and
        #   - a WAL_SEGMENT engine pause can be captured BEFORE step 0 (resume_at
        #     == 0 yet a real pause record exists) — gating the firing on
        #     resume_at > 0 would silently never resume it (Codex [P2.b]); and
        #   - a present-but-CORRUPT record must still FIRE the resume (which then
        #     classifies it ABORT_* and the driver fails closed below) — the prior
        #     `has_captured_pause` conflated presence with validity, so a corrupt
        #     snapshot was misread as "absent" and silently skipped, losing the
        #     abort record AND resuming past unrecoverable state (Codex [P1-r3-a]).
        # So the presence check is the sole gate. NB: the engine `resume_at` arg is
        # the ResumeAttempt ISO-8601 timestamp — NOT the int step-index (distinct).
        # `run_id` run-scopes the engine pause record (matching the F2 prefix's
        # run_idempotency_key scope) so a fresh run of the same workflow_id never
        # picks up an earlier run's lingering record (Codex [P2]). The capture
        # branch below passes the SAME run_id — identical key composition.
        _engine_recovery_loop = getattr(ctx, "engine_recovery_loop", None)
        _resume_engine_pause = _engine_recovery_loop is not None and (
            _engine_recovery_loop.has_pause_record(
                engine_class=manifest_entry.engine_class,
                workflow_id=manifest_entry.workflow_id,
                run_id=run_id,
            )
        )
        # RESUMPTION fires when resuming a committed step prefix OR a present
        # engine pause (a step-0 engine pause is a resumption even at resume_at==0).
        if resume_at > 0 or _resume_engine_pause:
            ctx.lifecycle_emitter.emit(WorkflowEventClass.RESUMPTION)
        # B-ENGINE-OUTPUT-REPLAY-WAL-SEGMENT (runtime spec C-RT-32 §14.23 + the
        # C-CP-08 §8.1 `segment_replay` cached-output-replay refinement) — the §8.1
        # "activity outputs cached and replayed" half, the EVENT_SOURCED_REPLAY shape
        # applied to the SHARED EngineOutputStore: replay the durably-stored prefix
        # outputs into the inter-step channel so the first re-dispatched segment-step
        # reads its recovered predecessor's output (degenerate without this — a
        # skip-prefix resume leaves a fresh channel empty → downstream reads None).
        # No-op unless store + channel are bound; fail-closed on a store↔ledger skew
        # gap / body-change identity mismatch (the shared helper). Keyed by
        # run_idempotency_key (engine-class-independent), so WAL_SEGMENT's resume_at
        # (the same F2-prefix step-index as event-replay) composes. Fires on the
        # committed-prefix condition (resume_at > 0), independent of the engine-pause
        # recovery below (a SEPARATE C-CP-22 engine-layer surface).
        if resume_at > 0:
            _replay_fail = _rehydrate_inter_step_channel_on_replay(
                ctx,
                run_idempotency_key=run_idempotency_key,
                resume_at=resume_at,
                steps=steps,
                workflow_id=manifest_entry.workflow_id,
                run_id=run_id,
            )
            if _replay_fail is not None:
                return _replay_fail, 0
        if _resume_engine_pause and _engine_recovery_loop is not None:
            _engine_resume = _run_protocol_method_sync(
                _engine_recovery_loop.attempt_resume(
                    engine_class=manifest_entry.engine_class,
                    workflow_id=manifest_entry.workflow_id,
                    run_id=run_id,
                    step_id=str(resume_at),
                    resume_event_id=f"resume:{run_id}:{resume_at}",
                    resume_attempt_count=1,
                    resume_at=datetime.now(UTC).isoformat(),
                )
            )
            # FAIL CLOSED on an aborting resume outcome (C-CP-22 §22.1
            # ABORT_SNAPSHOT_CORRUPTED / ABORT_REVALIDATION_FAILED). A present
            # pause whose resume aborts must HALT the run — never proceed past
            # unrecoverable engine state (Codex [P1-r3-b]). Mirrors the
            # workflow-layer precedent (`if not resume_result.resumed: return
            # FAILED` at the C-CP-26 resume branch above). Plain FAILED with the
            # matching CP fail-class marker; operator escalation is a future arc,
            # not silently absorbed here. The `cp.resume-attempted` entry the fire
            # above emitted is the durable audit record of the abort.
            _abort_fail_class = _ENGINE_RESUME_ABORT_FAIL_CLASS.get(
                _engine_resume.resume_outcome.outcome_kind
            )
            if _abort_fail_class is not None:
                return RunResult(
                    workflow_id=manifest_entry.workflow_id,
                    run_id=run_id,
                    status=RunStatus.FAILED,
                    terminal_step_index=None,
                    partial_state=None,
                    final_state=None,
                    fail_class=_abort_fail_class,
                ), 0
    # Under pure-pattern-no-engine: no resumption-specific emission per CP spec
    # §25.5 v1.4 scope carve-out (`workflow.resumption` CONDITIONAL row: "At v1.4
    # scope: emit on re-entry if manifest_entry.engine_class ==
    # save-point-checkpoint"). §8.1 declares the 5-class ResumptionKind enum +
    # universal observable behavior at §8.3 — those are the full contract space;
    # §25.5 carves out the v1.4 implementation scope. §8.2 row 3 governs
    # state-ledger native dedup for the pure-pattern engine class (orthogonal
    # to emission scope; row 3 is JOIN discipline, not emission discipline).

    # § 25.3.2 — Emit workflow.start.
    ctx.lifecycle_emitter.emit(WorkflowEventClass.WORKFLOW_START)

    # § 25.3.3 — Iterate steps in declaration order (SINGLE_THREADED_LINEAR
    # has no parallel/fan-out branching). Begin at `resume_at` to skip
    # already-materialized steps from a prior crashed/drained run.
    # `steps_executed` tracks completed-this-envelope step count for the
    # workflow.step_count close-time attribute per C-OD-25 §25.1 (U-OD-36).
    # Fresh-envelope-on-resumption (§25.4 invariant 1 + §25.5 default) means
    # prior re-materialized steps were observed under the prior envelope;
    # this counter reflects only this envelope's executions.
    accumulated: dict[str, Any] = {}
    steps_executed = 0
    # B-CHILD-CRASH-RESUME-FINAL-STATE-RECONSTRUCT (#766) + B-TOP-LEVEL-CRASH-RESUME-
    # FINAL-STATE-RECONSTRUCT (R-FS-1) — final_state reconstruction on resume. On a
    # durable-engine-class resume the dispatch loop would return a SUFFIX-ONLY final_state
    # (the committed prefix is skipped + never seeded into `accumulated`) — a silent
    # truncation: a SUCCESS run that lies about its output. `reconstruct_final_state`
    # (DEFAULT True — reconstruction is the correct behavior; suffix-only was the bug)
    # seeds `accumulated` with the durably-stored committed prefix `[0, resume_at)` so the
    # SUCCESS `final_state` (and a DRAINED/FAILED `partial_state`) reconstructs the full
    # run — the OUTPUT-side analogue of the inter-step CHANNEL rehydrate (the INPUT side);
    # both consume the shared `_read_durable_replay_prefix`. This is reached by BOTH the
    # top-level run (the `run_workflow` handler / `api.run`+`api.resume` take the default,
    # CP v1.76 §25.2/§25.6 resume-transparency invariant) AND a child sub-workflow run (so
    # the parent fan-out / hierarchical-pause fold sees the COMPLETE child terminal state).
    # Scoped to the durable-output-store engine classes (EVENT_SOURCED_REPLAY / WAL_SEGMENT /
    # SAVE_POINT_CHECKPOINT / RECONCILER_LOOP — the classes whose per-step output is durably
    # recorded, gated by the SAME `_FINAL_STATE_RECONSTRUCT_ENGINE_CLASSES` constant at the
    # `_record_durable_step_output` producer below so the producer + seed can never drift apart).
    # SAVE_POINT joined at v1.79 and RECONCILER at v1.80 (the EngineOutputStore is mechanically
    # class-agnostic; a real RECONCILER forward run flows through this same LINEAR loop, recording
    # its prefix at the producer → reconstructing here on resume — witnessed end-to-end, not
    # pre-seeded). The RECONCILER two-authorities concern resolved at the constant above: the
    # U-RT-123 reconciler substrate persists a CONVERGENCE DIGEST (StateSummary) for the CAS-lease,
    # NOT the per-step output map → the EngineOutputStore is the sole authority for `accumulated`.
    # An aborting CAS reconverge (a lost claim) returns FAILED upstream of this seed (no
    # reconstruction on a lost claim); a succeeding/no-pause resume falls through here.
    # This LINEAR seed site is reached only by SINGLE_THREADED_LINEAR
    # runs (the non-linear strategies return before it) → the slice is LINEAR-scoped, mirroring
    # the v1.75 child-scoped narrowing. Explicit `reconstruct_final_state=False` is the opt-out
    # (no production caller takes it). Fail-closed on a store↔ledger skew / identity-mismatch
    # (the shared read+validate), surfaced as a FAILED run so neither a parent fold nor a
    # top-level caller ever sees a corrupt/partial reconstructed state (a corrupt durable prefix
    # → FAILED, not a silently-truncated SUCCESS).
    if (
        reconstruct_final_state
        and resume_at > 0
        and manifest_entry.engine_class in _FINAL_STATE_RECONSTRUCT_ENGINE_CLASSES
    ):
        _prefix, _reconstruct_fail = _read_durable_replay_prefix(
            ctx,
            run_idempotency_key=run_idempotency_key,
            resume_at=resume_at,
            steps=steps,
            workflow_id=manifest_entry.workflow_id,
            run_id=run_id,
        )
        if _reconstruct_fail is not None:
            return _reconstruct_fail, 0
        for _stored_step_id, _stored_output in _prefix:
            accumulated[_stored_step_id] = dict(_stored_output)
    # B-EFFECT-FENCE-PAUSE-RESOLUTION (§14.22.9) — for an effect-fence-ambiguous-pause
    # resume, PEEK (NOT consume — leave the holder intact for the runtime HITL composer's
    # one-shot consume) the operator's resolution and key-bind it to the held reserve's
    # idempotency_key (from the snapshot carrier). Threaded onto the RESUMED step's
    # context only (`step_index == resume_at`), so the dispatcher applies exactly one
    # key-matched resolution. Gated on `effect_fence_resume is not None` so a HITL-only
    # resume never peeks (and never starves the HITL composer). `None` otherwise → the
    # dispatcher's INERT re-pause is preserved.
    effect_fence_directive: EffectFenceResolutionDirective | None = None
    if resume_snapshot is not None and resume_snapshot.effect_fence_resume is not None:
        _holder = cast(
            "_ResumeContextHolderLike | None",
            getattr(ctx, "resume_context_holder", None),
        )
        _resume_ctx = _holder.peek() if _holder is not None else None
        if _resume_ctx is not None and _resume_ctx.effect_fence_resolution is not None:
            effect_fence_directive = EffectFenceResolutionDirective(
                resolution=_resume_ctx.effect_fence_resolution,
                idempotency_key=resume_snapshot.effect_fence_resume.idempotency_key,
            )
    for step_index, step in enumerate(steps[resume_at:], start=resume_at):
        # § 25.4 row "Per-step pre-entry" — drain check before entering next
        # step (U-CP-57 AC #2; Path B operator-ratified — no `step.boundary`
        # emit at this site to preserve §5.2 step.kind 5-value enum). On
        # drain: return DRAINED with terminal_step_index = step_index - 1
        # (the prior step is the last fully-completed one).
        if ctx.drained_flag.is_set():
            return RunResult(
                workflow_id=manifest_entry.workflow_id,
                run_id=run_id,
                status=RunStatus.DRAINED,
                terminal_step_index=step_index - 1 if step_index > 0 else None,
                partial_state=dict(accumulated),
                final_state=None,
                fail_class=None,
            ), steps_executed

        # U-CP-95 (R-FS-1 E-impl-2) — WAL_SEGMENT engine-layer recovery-loop
        # PAUSE firing. The engine-native pause analogue of the workflow-layer
        # `ctx.pause_resume_protocol` fire below (a SEPARATE architectural
        # surface — C-CP-22 engine-layer vs C-CP-26 workflow-layer). Gated on
        # `engine_class == WAL_SEGMENT` + a bound (duck-typed) recovery loop +
        # the pause flag; checked BEFORE the workflow-layer branch so a
        # WAL_SEGMENT engine pause takes precedence and every non-WAL_SEGMENT
        # path stays BYTE-UNCHANGED (CP §25.10 Invariant 1). Fires
        # `capture_pause` → `cp.pause-captured` (C-CP-49) through the CP→IS
        # wiring, activating the R-CXA-2 engine-layer seam in production (the
        # first production caller of `RuntimeEngineRecoveryLoop` —
        # `[[built-but-vacuous-reground-ledger-asis]]`). Consumed duck-typed
        # (`Any` on the runtime ctx, no `harness_cp` → `harness_runtime` import).
        # The engine layer's durable state lives in the U-RT-121 segment log, so
        # the RunResult carries no workflow-layer PauseSnapshot (default None).
        _engine_recovery_loop = getattr(ctx, "engine_recovery_loop", None)
        if (
            manifest_entry.engine_class is EngineClass.WAL_SEGMENT
            and _engine_recovery_loop is not None
            and ctx.pause_requested_flag.is_set()
        ):
            _run_protocol_method_sync(
                _engine_recovery_loop.capture_pause(
                    engine_class=manifest_entry.engine_class,
                    workflow_id=manifest_entry.workflow_id,
                    run_id=run_id,
                    step_id=str(step_index),
                    pause_reason=PauseReason.ENGINE_NATIVE_PAUSE,
                )
            )
            return RunResult(
                workflow_id=manifest_entry.workflow_id,
                run_id=run_id,
                status=RunStatus.PAUSED,
                terminal_step_index=step_index - 1 if step_index > 0 else None,
                partial_state=dict(accumulated),
                final_state=None,
                fail_class=None,
            ), steps_executed

        # U-CP-97 (R-FS-1 E-impl-3c) — RECONCILER_LOOP engine-layer recovery-loop
        # PAUSE firing. Sibling to the WAL_SEGMENT PAUSE firing above, gated on
        # `engine_class == RECONCILER_LOOP` (mutually exclusive with the WAL gate —
        # a workflow has exactly one engine class), reusing the already-fetched
        # duck-typed `_engine_recovery_loop`. Fires `capture_pause` →
        # `cp.pause-captured` (C-CP-49) against the U-RT-123 reconciler substrate
        # (U-RT-124 binds it engine-class-aware so the convergence state lands in the
        # reconciler store, never the WAL segment-log — the no-cross-contamination
        # invariant). The reconciler's durable state lives in the U-RT-123 store, so
        # the RunResult carries no workflow-layer PauseSnapshot (default None).
        if (
            manifest_entry.engine_class is EngineClass.RECONCILER_LOOP
            and _engine_recovery_loop is not None
            and ctx.pause_requested_flag.is_set()
        ):
            _run_protocol_method_sync(
                _engine_recovery_loop.capture_pause(
                    engine_class=manifest_entry.engine_class,
                    workflow_id=manifest_entry.workflow_id,
                    run_id=run_id,
                    step_id=str(step_index),
                    pause_reason=PauseReason.ENGINE_NATIVE_PAUSE,
                )
            )
            return RunResult(
                workflow_id=manifest_entry.workflow_id,
                run_id=run_id,
                status=RunStatus.PAUSED,
                terminal_step_index=step_index - 1 if step_index > 0 else None,
                partial_state=dict(accumulated),
                final_state=None,
                fail_class=None,
            ), steps_executed

        # U-RT-89 (C-RT-24 §14.14.3) — per-step pre-entry pause-trigger
        # detection. Sibling check to `ctx.drained_flag.is_set()` above.
        # When the operator has bound PauseResumeProtocol + the caller has
        # signaled pause via `ctx.pause_requested_flag.set()`, capture a
        # PauseSnapshot via the protocol + return RunStatus.PAUSED with the
        # snapshot populated for caller-side resume invocation. MVP fires
        # WorkflowPauseReason.EXPLICIT_OPERATOR as the default reason per
        # spec v1.21 change-note adjacent defect (ii); finer-grained reason
        # selection is impl-discretion at follow-on composer arc per spec
        # §14.14.7.
        if ctx.pause_resume_protocol is not None and ctx.pause_requested_flag.is_set():
            protocol = cast(PauseResumeProtocol, ctx.pause_resume_protocol)
            pause_snapshot = _run_protocol_method_sync(
                protocol.capture_pause_snapshot(
                    workflow_id=manifest_entry.workflow_id,
                    run_id=run_id,
                    step_index=step_index,
                    pause_reason=WorkflowPauseReason.EXPLICIT_OPERATOR,
                )
            )
            # U-RT-111 v2.38 AC #3 — PAUSE_CAPTURED drain-flag CP→IS emission.
            # event_kind_index=1 reserves the low bit for drain-flag path.
            _cp_is_wiring = getattr(ctx, "cp_is_wiring", None)
            if _cp_is_wiring is not None:
                _run_protocol_method_sync(
                    _cp_is_wiring.emit_pause_resume_state_ledger_entry(
                        workflow_id=manifest_entry.workflow_id,
                        step_id=str(step_index),
                        protocol_event_kind=(PauseResumeProtocolEventKind.PAUSE_CAPTURED),
                        event_sequence_id=(step_index << 2) | 1,
                        protocol_state_snapshot=pause_snapshot.model_dump(mode="json"),
                        # Reading A apply (PR #83 sibling-extension): see fork
                        # doc U-CP-74 actor field malformation.
                        actor=ActorIdentity(ctx.ledger_writer.actor.actor_id),
                    )
                )
            return RunResult(
                workflow_id=manifest_entry.workflow_id,
                run_id=run_id,
                status=RunStatus.PAUSED,
                terminal_step_index=step_index - 1 if step_index > 0 else None,
                partial_state=dict(accumulated),
                final_state=None,
                fail_class=None,
                pause_snapshot=pause_snapshot,
            ), steps_executed

        # § 25.3.3.2 — Resolve binding via U-CP-14.
        # `persona_tier` sourced from manifest_entry per CP spec v1.17 §6.5.3
        # (canonical upstream — §6.1 WorkflowManifestEntry.persona_tier).
        binding = resolve_step_binding(
            manifest_entry,
            str(step.step_id),
            default_model_binding=default_model_binding,
            persona_tier=manifest_entry.persona_tier,
        )

        # U-CP-74 §16.5 (S) sibling-variant CP→IS state-ledger emission.
        # Per CP spec v1.27 §16.5.6 dual-emission discipline: emit only when
        # the per-step override was applied (binding.override_applied=True);
        # absent-override steps inherit manifest defaults and have no
        # override-specific state-ledger entry to emit. Defensive
        # operator-opt-in: when cp_is_wiring is None, silent-skip.
        # post_override_step_config is the StepEffectiveBinding canonical
        # JSON projection per spec §16.5.5 outcome-bytes semantic.
        if binding.override_applied:
            _cp_is_wiring = getattr(ctx, "cp_is_wiring", None)
            if _cp_is_wiring is not None:
                # Reading A apply (PR #83): pass ActorIdentity str-newtype to
                # match composer signature `actor: ActorIdentity` at
                # `per_step_override_evaluator.py:286`. Pre-Reading-A passed
                # `ctx.ledger_writer.actor` (an `Actor` Pydantic model);
                # composer's `str(actor)` produced the Pydantic field-repr
                # instead of the clean identity string. See
                # `.harness/class_2_fork_u_cp_74_actor_field_malformation.md`.
                _run_protocol_method_sync(
                    _cp_is_wiring.emit_override_state_ledger_entry(
                        workflow_id=manifest_entry.workflow_id,
                        step_id=str(step.step_id),
                        post_override_step_config=binding.model_dump(mode="json"),
                        actor=ActorIdentity(ctx.ledger_writer.actor.actor_id),
                    )
                )

        # § 25.3.3.3 — Acquire lease (per §5.3 lease.mechanism substrate;
        # per-engine-class binding under-specified at CP spec v1.4 §B
        # carry-forward — resolved at implementation per c1-orchestration-
        # control SKILL substrate). At v1.4 minimum-viable scope, lease
        # emission is deferred to a follow-up unit when the first
        # lease-requiring engine-class materializes. For pure-pattern-no-
        # engine: no lease per §8.2 row 3 "F2 state-ledger native"
        # substrate reading. For save-point-checkpoint: lease emission
        # deferred (substrate-anchored to c1-orchestration-control SKILL).
        # No lease.acquired emit at v1.4 minimum-viable scope.

        # § 25.3.3.4 — Dispatch step body through injected dispatcher.
        # v1.6 Path A — compose StepExecutionContext from driver-tracked
        # state per the 8-field schema at workflow_driver_types.py. See
        # the type's docstring for per-field semantics + MVP-default
        # rationale. Resolves the C-RT-17 Class 1 fork on StepDispatcher
        # parent-context gap (Path A ratified 2026-05-20).
        step_idempotency_key_pre = _compute_step_idempotency_key(run_idempotency_key, step_index)
        # MVP defaults per C-CP-12 §12.4 + Spec_Control_Plane_v1_6.md §25.2.1:
        # parent_gate_level: sourced from manifest_entry.default_gate_level
        # per CP spec v1.20 §6.1.Y Reading A absorption (X-AL-3 silent-
        # absorption gap closed at v1.20 per
        # `.harness/class_1_fork_h_t_cp_19_default_gate_level_spec_extension.md`).
        # None → GateLevel.AUTO preserves the v1.6 MVP hardcoded default
        # at construction sites that do not surface the field; operator-
        # supplied (not None) values flow through unchanged.
        # parent_sandbox_tier = TIER_1_PROCESS; parent_entry_hash = ""
        # (child shares parent ledger writer per C-RT-17 §14.7.4); tenant_id
        # sourced from `ctx.tenant_id` (HarnessContext exposes the
        # `RuntimeConfig.tenant_id` value per the v1.7+ deferral comment
        # at workflow_driver_types.py:189-192). None preserves single-tenant
        # default; operator-supplied values flow through the 4-substep audit
        # composition unchanged.
        # R-FS-1 B4 Slice 4 (CP spec v1.38 §6.1) — per-step ROLE override folded
        # into the single `StepExecutionContext.agent_role` source. On the linear
        # path there is no fan-out-derived role, so the per-step override is the
        # sole role source: `binding.agent_role` (None when no override → the
        # field stays None → byte-identical to v1.37, §14.5.3 invariant-1 holds;
        # an override THREADS the role on the linear path, relaxing the §14.5.3
        # invariant-3 "linear path untouched" at composition-time per the runtime
        # spec v1.52 operator-ratified relaxation). The runtime dispatch reads
        # this single source unchanged — no two-authority-at-dispatch.
        step_context = StepExecutionContext(
            workflow_id=manifest_entry.workflow_id,
            parent_action_id=(f"workflow:{manifest_entry.workflow_id}:step:{step_index}"),
            parent_gate_level=resolve_parent_gate_level(manifest_entry),
            # B-HITL-PLACEMENT-PER-STEP-PRODUCER — surface the workflow's declared
            # placements onto the per-step context so the wrap-time HITL composer
            # (runtime §14.8.2 step 1) fires per-step. Default () → no gate.
            # B-HITL-PLACEMENT-PER-STEP-OVERRIDE-FOLD (CP spec v1.49 §6.2) — fold
            # the per-step `binding.hitl_placement` override onto the workflow
            # tuple (union-by-position, tune-not-remove, monotone). None → verbatim.
            hitl_placements=fold_step_hitl_placements(
                manifest_entry.hitl_placements, binding.hitl_placement
            ),
            # B-EFFECT-FENCE-DURABLE-AUTO — the RUN engine class (NOT a per-step
            # StepOverride.engine_class) so the tool dispatcher auto-fences durable runs.
            run_engine_class=manifest_entry.engine_class,
            # B-EFFECT-FENCE-PAUSE-RESOLUTION — the key-bound operator resolution, set
            # ONLY on the RESUMED step (`step_index == resume_at`); the dispatcher applies
            # it at the §14.22 fence gate when the recomputed key matches. None on every
            # other step (and every non-resume run) → fence behaves as pre-v1.73.
            effect_fence_resolution=(effect_fence_directive if step_index == resume_at else None),
            parent_sandbox_tier=SandboxTier.TIER_1_PROCESS,
            parent_actor=ctx.ledger_writer.actor,
            parent_entry_hash="",
            parent_idempotency_key=step_idempotency_key_pre,
            tenant_id=ctx.tenant_id,
            step_index=step_index,
            agent_role=binding.agent_role,
            # B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT-NONLEAF-CHILD (R-FS-1) — this is
            # the SINGLE_THREADED_LINEAR inline step loop; a SUB_AGENT_DISPATCH step here
            # is a once-per-run sequential step whose (run_id, step_index) recurs only as
            # a same-logical-step forward resume. The runtime seed gate reads this to
            # extend the deterministic child_run_id_seed to a recoverable nested-grandchild
            # SUB_AGENT (the NONLEAF-CHILD relaxation) so a maybe-ran parent's re-dispatch
            # auto-resumes the grandchild instead of re-firing its committed effects.
            is_linear_sequential_dispatch=True,
        )
        # v1.6 routing-layer refactor per C-RT-17 §14.7.7: dispatch via
        # registry.lookup(step.kind).dispatch(...) instead of single
        # bound dispatcher. StepKindDispatcherNotBoundError maps to
        # RT-FAIL-STEP-KIND-DISPATCHER-NOT-BOUND per §14.7 failure-mode
        # taxonomy (documented expected behavior at v1.6 for unbound
        # step_kinds: DECLARATIVE_STEP, TOOL_STEP, HITL_STEP, INFERENCE_STEP
        # — the last is a Class 1 carry-forward per the U-RT-59 landing
        # arc; sub-agent dispatch composer arc bound SUB_AGENT_DISPATCH
        # only at v1.6 MVP).
        try:
            step_output = step_dispatchers.lookup(step.step_kind).dispatch(
                binding, step, step_context=step_context
            )
        except StepKindDispatcherNotBoundError as exc:
            return RunResult(
                workflow_id=manifest_entry.workflow_id,
                run_id=run_id,
                status=RunStatus.FAILED,
                terminal_step_index=step_index,
                partial_state=dict(accumulated),
                final_state=None,
                fail_class=(f"step-failure: RT-FAIL-STEP-KIND-DISPATCHER-NOT-BOUND: {exc}"),
            ), steps_executed
        except BaseException as exc:
            # U-RT-95 (runtime spec v1.24 §14.8.8.4) — driver-side handler for
            # the HITLPauseRequestedSignal typed control-flow exception raised
            # by the HITL gate composer's durable-async branch (§14.8.8.1
            # step 6). The signal inherits BaseException (not Exception) so
            # `except Exception` below does NOT consume it; explicit
            # BaseException catch + class-name match honors the layering
            # discipline (harness-cp cannot import from harness-runtime per
            # the workspace dependency graph). On catch: capture the pause
            # snapshot via ctx.pause_resume_protocol (guaranteed non-None by
            # the §14.8.8.1 step 0 OR-form precondition that gated the
            # signal raise) + return RunStatus.PAUSED with
            # terminal_step_index = step_index - 1 (paused at step N's HITL
            # gate; completed through step N-1).
            if type(exc).__name__ == "HITLPauseRequestedSignal":
                if ctx.pause_resume_protocol is not None and ctx.pause_requested_flag.is_set():
                    protocol = cast(PauseResumeProtocol, ctx.pause_resume_protocol)
                    pause_snapshot = _run_protocol_method_sync(
                        protocol.capture_pause_snapshot(
                            workflow_id=manifest_entry.workflow_id,
                            run_id=run_id,
                            step_index=step_index,
                            pause_reason=WorkflowPauseReason.HITL_PENDING,
                        )
                    )
                    # U-RT-111 v2.38 AC #3 — PAUSE_CAPTURED HITL-signal CP→IS
                    # emission. event_kind_index=2 disambiguates HITL-signal
                    # path from drain-flag path (=1) at same step_index.
                    _cp_is_wiring = getattr(ctx, "cp_is_wiring", None)
                    if _cp_is_wiring is not None:
                        _run_protocol_method_sync(
                            _cp_is_wiring.emit_pause_resume_state_ledger_entry(
                                workflow_id=manifest_entry.workflow_id,
                                step_id=str(step_index),
                                protocol_event_kind=(PauseResumeProtocolEventKind.PAUSE_CAPTURED),
                                event_sequence_id=(step_index << 2) | 2,
                                protocol_state_snapshot=(pause_snapshot.model_dump(mode="json")),
                                # Reading A apply (PR #83 sibling-extension):
                                # see fork doc U-CP-74 actor malformation.
                                actor=ActorIdentity(ctx.ledger_writer.actor.actor_id),
                            )
                        )
                    return RunResult(
                        workflow_id=manifest_entry.workflow_id,
                        run_id=run_id,
                        status=RunStatus.PAUSED,
                        terminal_step_index=(step_index - 1 if step_index > 0 else None),
                        partial_state=dict(accumulated),
                        final_state=None,
                        fail_class=None,
                        pause_snapshot=pause_snapshot,
                    ), steps_executed
                # Defensive — signal fired but pause_resume_protocol absent.
                # Per §14.8.8.1 step 0 OR-form precondition this is
                # unreachable; surface as FAILED for visibility.
            # B-EFFECT-FENCE-HITL-ROUTE (runtime spec §14.22 two-case split) — the
            # runtime effect fence lost a reserve to a prior uncommitted attempt of
            # a non-idempotent effect AND found no captured output proving
            # completion → whether the effect fired is genuinely ambiguous. The
            # runtime raises `EffectFenceAmbiguousUncommittedError`; name-matched
            # here (harness-cp cannot import from harness-runtime per the workspace
            # dependency graph, mirroring HITLPauseRequestedSignal above). Route to
            # a §26.2 EFFECT_FENCE_AMBIGUOUS PAUSE when a PauseResumeProtocol is
            # bound (operator opted into resumable pause); else fall through to the
            # generic FAILED mapping below — behaviorally equivalent to the pre-v1.72
            # fail-closed (FAILED, no auto-re-fire; only the fail_class string
            # differs). NEVER an auto-re-fire on either branch.
            if (
                type(exc).__name__ == "EffectFenceAmbiguousUncommittedError"
                and ctx.pause_resume_protocol is not None
            ):
                protocol = cast(PauseResumeProtocol, ctx.pause_resume_protocol)
                # B-EFFECT-FENCE-PAUSE-RESOLUTION — carry the held reserve's
                # idempotency_key (off the runtime error, read by name since harness-cp
                # cannot import harness-runtime) so `api.resume` can key-bind the
                # operator's resolution to THIS effect. Absent the key (defensive) →
                # None carrier → resume cannot resolve, re-pauses (the pre-resolution
                # INERT behavior, never an auto-re-fire).
                _fence_key = getattr(exc, "idempotency_key", None)
                pause_snapshot = _run_protocol_method_sync(
                    protocol.capture_pause_snapshot(
                        workflow_id=manifest_entry.workflow_id,
                        run_id=run_id,
                        step_index=step_index,
                        pause_reason=WorkflowPauseReason.EFFECT_FENCE_AMBIGUOUS,
                        effect_fence_resume=(
                            EffectFenceResumeState(idempotency_key=_fence_key)
                            if isinstance(_fence_key, str)
                            else None
                        ),
                    )
                )
                # PAUSE_CAPTURED effect-fence CP→IS emission. event_kind_index=3
                # disambiguates the effect-fence pause path from the drain-flag
                # path (=1) and the HITL-signal path (=2) at the same step_index.
                _cp_is_wiring = getattr(ctx, "cp_is_wiring", None)
                if _cp_is_wiring is not None:
                    _run_protocol_method_sync(
                        _cp_is_wiring.emit_pause_resume_state_ledger_entry(
                            workflow_id=manifest_entry.workflow_id,
                            step_id=str(step_index),
                            protocol_event_kind=(PauseResumeProtocolEventKind.PAUSE_CAPTURED),
                            event_sequence_id=(step_index << 2) | 3,
                            protocol_state_snapshot=(pause_snapshot.model_dump(mode="json")),
                            actor=ActorIdentity(ctx.ledger_writer.actor.actor_id),
                        )
                    )
                return RunResult(
                    workflow_id=manifest_entry.workflow_id,
                    run_id=run_id,
                    status=RunStatus.PAUSED,
                    terminal_step_index=(step_index - 1 if step_index > 0 else None),
                    partial_state=dict(accumulated),
                    final_state=None,
                    fail_class=None,
                    pause_snapshot=pause_snapshot,
                ), steps_executed
            if not isinstance(exc, Exception):
                # Unknown BaseException (KeyboardInterrupt, SystemExit, etc.) —
                # re-raise per Python convention; do not consume.
                raise
            # Spec v1.31 §11 — per-step worker-thread blocking bound exceeded
            # at SyncDispatcherFacade.dispatch's
            # future.result(timeout=config.step_dispatch_timeout_seconds).
            # Discriminated from generic Exception so the fail-class string
            # canonicalizes to RT-FAIL-STEP-DISPATCH-TIMEOUT. Name-match per
            # the HITLPauseRequestedSignal pattern above (harness-cp cannot
            # import from harness-runtime per workspace dependency graph).
            if type(exc).__name__ == "StepDispatchTimeoutError":
                return RunResult(
                    workflow_id=manifest_entry.workflow_id,
                    run_id=run_id,
                    status=RunStatus.FAILED,
                    terminal_step_index=step_index,
                    partial_state=dict(accumulated),
                    final_state=None,
                    fail_class=(f"step-failure: RT-FAIL-STEP-DISPATCH-TIMEOUT: {exc}"),
                ), steps_executed
            # R-FS-1 arc M (C-RT-28 §14.20.4) — managed-agents session did not
            # reach a success terminal status. Discriminated from generic
            # Exception so the fail-class canonicalizes to
            # RT-FAIL-MANAGED-AGENTS-SESSION. Name-match per the
            # StepDispatchTimeoutError pattern above (harness-cp cannot import
            # from harness-runtime per workspace dependency graph).
            if type(exc).__name__ == "ManagedAgentsSessionError":
                return RunResult(
                    workflow_id=manifest_entry.workflow_id,
                    run_id=run_id,
                    status=RunStatus.FAILED,
                    terminal_step_index=step_index,
                    partial_state=dict(accumulated),
                    final_state=None,
                    fail_class=(f"step-failure: RT-FAIL-MANAGED-AGENTS-SESSION: {exc}"),
                ), steps_executed
            return RunResult(
                workflow_id=manifest_entry.workflow_id,
                run_id=run_id,
                status=RunStatus.FAILED,
                terminal_step_index=step_index,
                partial_state=dict(accumulated),
                final_state=None,
                fail_class=_step_fail_class("step-failure", exc),
            ), steps_executed

        # § 25.3.3.5 — Emit step.boundary.
        ctx.lifecycle_emitter.emit(WorkflowEventClass.STEP_BOUNDARY)

        # § 25.3.3.6 — Release lease (deferred per §25.3.3.3 above).

        # § 25.3.5 (NEW at v1.10) — U-CP-61 post-dispatch validation hook.
        # Per C-CP-25 §25.3 "post-dispatch, pre-ledger-append validation
        # hook". Operator-opt-in: skip when ctx.validator_framework is None
        # (driver-level opt-out). When bound, the framework returns a
        # ValidatorEvaluation; the next_action drives the branch:
        #   PROCEED   → fall through to ledger append (normal flow)
        #   RETRY     → caller's retry wrapper (C-RT-16) handles; pass through
        #               here (the framework body has no retry-state visibility;
        #               the U-CP-60 convert_revalidate_to_permanent_fail() is
        #               invoked externally on budget exhaustion). v1.10 MVP:
        #               proceeds-with-validator.revalidation event emit.
        #   ESCALATE_HITL → emit validator.escalation event; proceeds-with-
        #               escalation-marker. Actual HITL gate dispatch (per spec
        #               §25.7 invariant 4) is a future arc; v1.10 MVP emits the
        #               span linking-to subsequent hitl.gate.evaluated per
        #               §C-OD-29.1 row 10 + F2-02 absorption.
        #   ABORT     → return RunResult(FAILED) with CP-FAIL-VALIDATOR-PERMANENT
        if ctx.validator_framework is not None:
            tracer = cast(TracerProvider, ctx.tracer_provider).get_tracer(
                "harness.cp.workflow_driver"
            )
            with tracer.start_as_current_span("validator.evaluate") as evaluate_span:
                try:
                    evaluation: ValidatorEvaluation = cast(
                        "SyncValidatorFrameworkFacade", ctx.validator_framework
                    ).evaluate(
                        step,
                        step_output,
                        step_context=step_context,
                    )
                except Exception as exc:
                    evaluate_span.record_exception(exc)
                    return RunResult(
                        workflow_id=manifest_entry.workflow_id,
                        run_id=run_id,
                        status=RunStatus.FAILED,
                        terminal_step_index=step_index,
                        partial_state=dict(accumulated),
                        final_state=None,
                        fail_class=(f"validator-framework-failure: {type(exc).__name__}: {exc}"),
                    ), steps_executed

                # §C-OD-29.1 outer envelope (3 attrs).
                for attr_name, attr_value in evaluation.span_attributes.items():
                    evaluate_span.set_attribute(attr_name, attr_value)

                # AC #4 — populate validator.escalation.parent_hitl_span_id
                # when outcome=ESCALATE (links to subsequent hitl.gate.evaluated
                # span per F2-02 absorption). v1.10 MVP: use current span_id
                # as the parent-link marker; future HITL gate composer reads
                # this attribute to anchor its parent context.
                if evaluation.result.outcome.value == "escalate":
                    parent_hitl_span_id = format(evaluate_span.get_span_context().span_id, "016x")
                    evaluate_span.set_attribute(
                        "validator.escalation.parent_hitl_span_id",
                        parent_hitl_span_id,
                    )
                    if evaluation.result.fail_class is not None:
                        evaluate_span.set_attribute(
                            "validator.escalation.fail_class",
                            evaluation.result.fail_class.value,
                        )

                # AC #5 branch on next_action.
                if evaluation.next_action.value == "abort":
                    return RunResult(
                        workflow_id=manifest_entry.workflow_id,
                        run_id=run_id,
                        status=RunStatus.FAILED,
                        terminal_step_index=step_index,
                        partial_state=dict(accumulated),
                        final_state=None,
                        fail_class=(
                            f"CP-FAIL-VALIDATOR-PERMANENT: "
                            f"validator returned PERMANENT_FAIL at step_id="
                            f"{step.step_id!r}"
                        ),
                    ), steps_executed

                # Reading B v1.22 §14.15 — ESCALATE_HITL mid-step re-entry.
                # Per C-CP-28 §25.4 invariant 4: "ESCALATE always emits HITL
                # gate. Escalation cannot be silently dropped." Fires the
                # ValidatorEscalationGateComposer mid-step pre-ledger-append
                # per C-CP-28 §25.3 + §25.4 invariant 2. Operator-opt-in:
                # only when both validator_framework AND ask_user_question_
                # surface are bound at ctx (production paths supply both;
                # test paths may set ask_user_question_surface = None and
                # the escalation outcome will fail-closed).
                if evaluation.next_action.value == "escalate_hitl":
                    ask_user_question_surface = getattr(ctx, "ask_user_question_surface", None)
                    escalation_brief = evaluation.result.escalation_brief
                    if ask_user_question_surface is not None and escalation_brief is not None:
                        # Lazy import to avoid cycle (runtime → cp → runtime).
                        # GateLevel is module-level imported at line 51;
                        # do NOT lazy-import here (would shadow + break
                        # line 735's GateLevel.AUTO reference).
                        from harness_runtime.lifecycle.validator_escalation_composer import (
                            ValidatorEscalationGateAuditComposeError,
                            ValidatorEscalationGateRejectedError,
                            ValidatorEscalationGateTimeoutError,
                            compose_validator_escalation_gate,
                        )

                        from harness_cp.validator_fail_transient_staircase import (
                            CrossTrustBoundaryState,
                        )

                        try:
                            # Async composer bridged to sync driver context
                            # per `_run_protocol_method_sync` pattern (analog
                            # of PauseResumeProtocol bridging at U-CP-62).
                            hitl_response = _run_protocol_method_sync(
                                compose_validator_escalation_gate(
                                    ask_user_question_surface=ask_user_question_surface,
                                    brief=escalation_brief,
                                    step_action_id=str(step_context.parent_action_id),
                                    # v1.22 MVP sentinels per spec §14.15.8
                                    # deferred-discretion; full cross-trust-
                                    # state derivation gated on follow-on arc
                                    # per scoping doc adjacent observation (c).
                                    cross_trust_state=CrossTrustBoundaryState.NONE,
                                    gate_level=GateLevel.ASK,
                                    tracer_provider=ctx.tracer_provider,
                                )
                            )
                        except ValidatorEscalationGateRejectedError as exc:
                            return RunResult(
                                workflow_id=manifest_entry.workflow_id,
                                run_id=run_id,
                                status=RunStatus.FAILED,
                                terminal_step_index=step_index,
                                partial_state=dict(accumulated),
                                final_state=None,
                                fail_class=(
                                    f"RT-FAIL-HITL-GATE-REJECTED: "
                                    f"validator-escalation rejected at "
                                    f"step_id={step.step_id!r}: {exc}"
                                ),
                            ), steps_executed
                        except ValidatorEscalationGateTimeoutError as exc:
                            return RunResult(
                                workflow_id=manifest_entry.workflow_id,
                                run_id=run_id,
                                status=RunStatus.FAILED,
                                terminal_step_index=step_index,
                                partial_state=dict(accumulated),
                                final_state=None,
                                fail_class=(
                                    f"RT-FAIL-HITL-GATE-TIMEOUT: "
                                    f"validator-escalation timed out at "
                                    f"step_id={step.step_id!r}: {exc}"
                                ),
                            ), steps_executed
                        except ValidatorEscalationGateAuditComposeError as exc:
                            return RunResult(
                                workflow_id=manifest_entry.workflow_id,
                                run_id=run_id,
                                status=RunStatus.FAILED,
                                terminal_step_index=step_index,
                                partial_state=dict(accumulated),
                                final_state=None,
                                fail_class=(
                                    f"RT-FAIL-VALIDATOR-ESCALATION-GATE-COMPOSE: "
                                    f"audit-compose failed at step_id="
                                    f"{step.step_id!r}: {exc}"
                                ),
                            ), steps_executed

                        # APPROVE / EDIT / RESPOND — proceed to ledger append.
                        # Per spec §14.15.8 deferred-discretion: EDIT semantics
                        # (whether to mutate step_output) is implementer-
                        # discretion at v1.22 MVP — proceed-with-original-
                        # outcome is the safe default; future arc may apply
                        # operator edits to step_output. RESPOND: operator
                        # response recorded in audit (deferred to follow-on
                        # CP composer arc per scoping doc adjacent obs (d));
                        # workflow proceeds with original validator outcome.
                        _ = hitl_response

        # B-ENGINE-OUTPUT-REPLAY (runtime spec C-RT-32) — durably persist this
        # step's output to the output-carrying event-history store BEFORE the
        # ledger-append below (RESERVE-before-COMMIT: the store always holds ≥ the
        # ledger's materialized prefix). Opt-in; no-op when unbound. The store-write
        # precedes the ledger materialization `resume_at` counts, so a durable-class
        # resume never finds a materialized step with a missing stored output. GATED on
        # the durable-output-store engine classes via the SHARED
        # `_FINAL_STATE_RECONSTRUCT_ENGINE_CLASSES` constant (EVENT_SOURCED_REPLAY +
        # WAL_SEGMENT — the C-CP-08 §8.1 cached-output-replay refinement — PLUS
        # SAVE_POINT_CHECKPOINT at v1.79 and RECONCILER_LOOP at v1.80 for the final_state
        # reconstruction) so a non-durable run with the flag on does not write a
        # never-consumed journal (advisor pre-merge catch). The record half is SAFE only
        # because a consumer fires for the SAME classes: B-ENGINE-OUTPUT-REPLAY-WAL-SEGMENT
        # added WAL to BOTH this producer gate AND the resume-side rehydrate;
        # B-CHILD-...-SAVE-POINT added SAVE_POINT and B-CHILD-...-RECONCILER added RECONCILER
        # to BOTH this producer gate AND the final_state seed (their only consumer — neither
        # has a cached-output-replay channel rehydrate). Using ONE constant at both sites
        # makes "never record-only" structural — never a never-consumed journal.
        if manifest_entry.engine_class in _FINAL_STATE_RECONSTRUCT_ENGINE_CLASSES:
            _record_durable_step_output(
                ctx, run_idempotency_key, step_index, str(step.step_id), step_output
            )

        # § 25.3.3.7 — State-ledger append via U-IS-11 composition.
        # Reuse pre-dispatch step_idempotency_key composed at the
        # StepExecutionContext site above (identical per-step value).
        step_idempotency_key = step_idempotency_key_pre
        try:
            _append_step_ledger_entry(
                ctx=ctx,
                workflow_id=manifest_entry.workflow_id,
                step_index=step_index,
                step_idempotency_key=step_idempotency_key,
                step_output=step_output,
            )
        except Exception as exc:
            return RunResult(
                workflow_id=manifest_entry.workflow_id,
                run_id=run_id,
                status=RunStatus.FAILED,
                terminal_step_index=step_index,
                partial_state=dict(accumulated),
                final_state=None,
                fail_class=f"ledger-append-failed: {type(exc).__name__}: {exc}",
            ), steps_executed

        # Accumulate step output under its step id for terminal state.
        accumulated[str(step.step_id)] = dict(step_output)
        # B-INTERSTEP (runtime spec §14.21 C-RT-34) — also record to the run-scoped
        # inter-step channel (opt-in; no-op when unbound) so a subsequent step's
        # dispatch can read this step's output as upstream context.
        _record_inter_step_output(ctx, str(step.step_id), step_output)
        # Step is fully complete (body + step.boundary + ledger append all
        # succeeded). Increment the workflow.step_count carrier per §C-OD-25
        # (U-OD-36).
        steps_executed += 1

        # § 25.4 row "Per-step post-exit" — drain check after step body
        # completes + step.boundary emitted + ledger append persisted
        # (U-CP-57 AC #3). On drain: return DRAINED with terminal_step_index
        # = this step (it counted; its ledger entry has persisted per
        # U-IS-11 append discipline).
        if ctx.drained_flag.is_set():
            return RunResult(
                workflow_id=manifest_entry.workflow_id,
                run_id=run_id,
                status=RunStatus.DRAINED,
                terminal_step_index=step_index,
                partial_state=dict(accumulated),
                final_state=None,
                fail_class=None,
            ), steps_executed

    # § 25.3.4 + § 25.3.5 — Terminal SUCCESS return. No new event class at
    # terminal exit; the absence of a further step.boundary plus the
    # RunResult.status=SUCCESS return is the terminal observable.
    return RunResult(
        workflow_id=manifest_entry.workflow_id,
        run_id=run_id,
        status=RunStatus.SUCCESS,
        terminal_step_index=None,
        partial_state=None,
        final_state=dict(accumulated),
        fail_class=None,
    ), steps_executed


def _determine_resume_at(
    *,
    ctx: DriverContext,
    run_idempotency_key: str,
    step_count: int,
    workload_class: Any,
) -> int:
    """Determine the resume-at index for selective replay-resumption (§25.6).

    Per CP plan v2.12 §2.9 U-CP-56 AC #6: under save-point-checkpoint binding,
    for each step index `i ∈ [0, step_count)`, compute the expected per-step
    idempotency_key and query the IS state-ledger via
    `ctx.ledger_reader.read_by_idempotency_key`. Advance over the contiguous
    prefix of materialized steps; stop at the first step whose expected key
    returns zero entries.

    Returns the index of the first step that needs to execute (i.e., the
    count of already-materialized contiguous-prefix steps). Returns 0 for a
    genesis run (no prior entries match this run's expected keys).

    Conservative semantic — gap behavior: if the ledger contains a gap
    (e.g., step 0 + step 2 entries exist but step 1 missing), the
    `resume_at` advances only over the contiguous prefix (returns 1 in
    that case). Gap-fill resumption is out of scope at v2.12.
    """
    # Lazy import to keep the module's import surface narrow and to avoid
    # pulling IS-read at module load. The `BoundedWindow` shape is the
    # IS-side bounding contract per C-IS-07 §7.2.
    from harness_is.state_ledger_entry_schema import Identifier
    from harness_is.state_ledger_read import BoundedWindow

    # The bounding window's `max_entries` must be ≥ 1 (positive). Use the
    # ledger's current entry_count as an upper bound, falling back to a
    # nonzero value for a genesis ledger (returns no entries — correct).
    window_size = max(1, ctx.ledger_writer.entry_count)
    window = BoundedWindow(max_entries=window_size, workload_class=workload_class)

    for i in range(step_count):
        expected_step_key = _compute_step_idempotency_key(run_idempotency_key, i)
        result = ctx.ledger_reader.read_by_idempotency_key(
            Identifier(expected_step_key),
            window,
        )
        if not result.entries:
            return i
    return step_count


def _determine_event_replay_resume_at(
    *,
    ctx: DriverContext,
    run_idempotency_key: str,
    step_count: int,
    workload_class: Any,
) -> int:
    """Determine resume-at for EVENT_SOURCED_REPLAY (C-CP-08 §8.1 `engine_replay`).

    Named seam for engine-event-history replay resumption (U-CP-93). At HEAD it
    delegates to `_determine_resume_at`: under C-CP-08 §8.2 row 1 the engine
    event history joins the F2 state-ledger on `idempotency_key`, so the
    resume-at index — the count of contiguous already-materialized steps — is
    the identical F2-prefix computation save-point uses. EVENT_SOURCED_REPLAY's
    §8.1 distinction ("no re-execution of activities") manifests in the driver
    as the materialized prefix not being re-dispatched (the loop begins at
    `resume_at`), exactly as for save-point.

    The §8.1 *cached-output replay* refinement — replaying prior activity
    outputs into downstream-visible state so post-resume steps observe them
    deterministically — is **degenerate at HEAD and out of this unit's scope**:
    the F2 `EntryPayload` carries no activity output (only `response_hash`), and
    the driver threads no inter-step data flow (B-INTERSTEP). It is a registered
    build arc, not a silent defer. See `.harness/r-fs-1-e-impl-1-finding.md`.
    This helper is the extension point where that refinement lands once the
    output-carrying event-history substrate + inter-step data flow exist.
    """
    return _determine_resume_at(
        ctx=ctx,
        run_idempotency_key=run_idempotency_key,
        step_count=step_count,
        workload_class=workload_class,
    )


def _determine_segment_replay_resume_at(
    *,
    ctx: DriverContext,
    run_idempotency_key: str,
    step_count: int,
    workload_class: Any,
) -> int:
    """Determine resume-at for WAL_SEGMENT (C-CP-08 §8.1 `segment_replay`).

    Named seam for WAL-segment replay resumption (U-CP-94). At HEAD it delegates
    to `_determine_resume_at`: under C-CP-08 §8.2 row 5 the per-segment ledger
    entries join the F2 state-ledger on `idempotency_key`, so the resume-at index
    — the count of the contiguous already-materialized segment prefix — is the
    identical F2-prefix computation save-point / event-replay use. WAL_SEGMENT's
    §8.1 distinction ("replay from WAL segments; per-segment dedup") manifests in
    the driver as the materialized segment prefix not being re-dispatched (the
    loop begins at `resume_at`), and the F2 idempotency-key join is the
    per-segment dedup (a re-materialized segment's key already resolves to an
    entry → it is not re-applied).

    This is the CP→IS reading the U-CP-94 AC names as the only one that avoids a
    CP↔RT cycle (the CP driver cannot import `harness_runtime`, so it cannot read
    the U-RT-121 segment-log substrate directly). The durable segment-log
    substrate (U-RT-121) is what `ctx.engine_recovery_loop` fires against for the
    engine-layer pause/resume entries (U-CP-95) — it is NOT the `resume_at`
    source. As with EVENT_SOURCED_REPLAY (`.harness/r-fs-1-e-impl-1-finding.md`)
    this CP/IS-level resume_at is degenerate vs save-point; the genuine
    distinguishing WAL_SEGMENT capability is the durable substrate + recovery
    loop firing, not a richer prefix computation here.
    """
    return _determine_resume_at(
        ctx=ctx,
        run_idempotency_key=run_idempotency_key,
        step_count=step_count,
        workload_class=workload_class,
    )


def _determine_reconciler_converge_resume_at(
    *,
    ctx: DriverContext,
    run_idempotency_key: str,
    step_count: int,
    workload_class: Any,
) -> int:
    """Determine resume-at for RECONCILER_LOOP (C-CP-08 §8.1 `reconciler_converge`).

    Named seam for reconciler-loop convergence resumption (U-CP-96). At HEAD it
    delegates to `_determine_resume_at`: under C-CP-08 §8.2 row 4 the reconciler
    reads the F2 state-ledger (joined on `idempotency_key`) to detect prior
    actions, so the resume-at index — the count of the contiguous already-converged
    prefix — is the identical F2-prefix computation save-point / event-replay /
    segment-replay use. RECONCILER_LOOP's §8.1 distinction ("re-derive state from
    declarative CRDs; reconciler-loop converges through compare-and-swap")
    manifests in the driver as the materialized prefix not being re-dispatched
    (the loop begins at `resume_at`), and the F2 idempotency-key join is the
    convergence dedup (an already-converged step's key resolves to an entry → it
    is not re-applied).

    reconciler-loop is an ENGINE-OWNS-SUBSTRATE class
    (`f2_substrate_join_discipline.py:9-12`, grouped with event-sourced-replay; F2
    join `CRD_RECONCILER_LEDGER`): the AUTHORITATIVE durable reconciler state lives
    in the engine-owned, hand-rolled etcd-style store (U-RT-123, E-impl-3b), NOT
    this CP→IS F2-overlay read. The CP driver cannot import `harness_runtime`, so
    it cannot read that store directly — the only reading that avoids a CP↔RT cycle
    (U-CP-96 AC). As with EVENT_SOURCED_REPLAY (`.harness/r-fs-1-e-impl-1-finding.md`)
    and WAL_SEGMENT (`.harness/r-fs-1-e-impl-2-finding.md`) this CP/IS-level resume_at is
    DELIBERATELY degenerate vs save-point — a sharper engine-owns-vs-overlay split
    than WAL had, which makes the "if a genuinely engine-owned resume_at is needed
    it folds into the runtime layer (U-RT-124), never a new CP→RT edge" contingency
    MORE warranted for reconciler, not less (mirrors the U-CP-94 hedge, a fortiori).
    The genuine distinguishing RECONCILER_LOOP capabilities are the durable
    CAS-lease substrate (U-RT-123) + the engine-layer recovery-loop firing
    (U-CP-97), not a richer prefix computation here.
    """
    return _determine_resume_at(
        ctx=ctx,
        run_idempotency_key=run_idempotency_key,
        step_count=step_count,
        workload_class=workload_class,
    )


def _append_step_ledger_entry(
    *,
    ctx: DriverContext,
    workflow_id: str,
    step_index: int,
    step_idempotency_key: str,
    step_output: Mapping[str, Any],
) -> None:
    """Compose + append the per-step state-ledger entry per § 25.3.3.7.

    Uses the IS-exported `EntryPayload` + `WriteKey` shapes (C-IS-07 §7.1).
    Imported lazily to avoid pulling IS-write at module load.
    """
    # Lazy-import to keep the module's import surface narrow.
    from harness_is.state_ledger_entry_schema import Identifier
    from harness_is.state_ledger_write import EntryPayload, WriteKey

    action_id = ActionID(f"workflow:{workflow_id}:step:{step_index}")
    # R-003 producer-site lift — populate the `procedural_tier_snapshot_ref`
    # D-derivative sidecar (IS spec v1.3 §C-IS-05 §5.1) for this workflow-
    # context per-step emission. The resolver arrives via the DriverContext
    # Protocol (never an import); `getattr` defensive-reads it the same way the
    # `cp_is_wiring` firing sites do (operator-opt-out / test ctx → `None`).
    _resolver = getattr(ctx, "procedural_tier_snapshot_resolver", None)
    _procedural_tier_snapshot_ref = _resolver() if _resolver is not None else None
    # `Identifier` is the IS-typed string newtype for state-ledger string ids;
    # we pass through the action_id verbatim plus the idempotency hex.
    payload = EntryPayload(
        action_id=Identifier(str(action_id)),
        idempotency_key=Identifier(step_idempotency_key),
        actor=ctx.ledger_writer.actor,
        timestamp=datetime.now(UTC),
        procedural_tier_snapshot_ref=_procedural_tier_snapshot_ref,
    )
    write_key = WriteKey(
        thread_id=Identifier(workflow_id),
        step_id=Identifier(str(step_index)),
        idempotency_key=Identifier(step_idempotency_key),
    )
    ctx.ledger_writer.append(payload, write_key)
    # Discard return value — driver does not branch on append vs idempotent-noop;
    # both outcomes leave the ledger correctly composed per C-IS-07 §7.1.


# ---------------------------------------------------------------------------
# Post-join synthesis (C-CP-25 §25.12 v1.54 / B-POSTJOIN-LLM-SYNTHESIS — arc-a)
# ---------------------------------------------------------------------------
#
# An OPT-IN terminal `POST_JOIN_SYNTHESIS` step (CP spec v1.54 §5.2/§25.2/§3)
# replaces a concurrent fan-out's deterministic aggregate (`_aggregate_*`) with
# an LLM-composed synthesis over the branch-index-ordered sibling outputs —
# sacrificing the §25.12 Point-2 aggregator-purity guarantee for that run ONLY
# (Point-1 + branch-index ordering preserved; default fold byte-identical absent
# the opt-in). Effect-free read-only compose. Reproducible cached-replay of the
# synthesized aggregate is the registered follow-on `B-FANOUT-OUTPUT-REPLAY`
# (a separate §25.12-Point-1/D1 reckoning — NOT this arc).


# B-FANOUT-RESUME-CARRIER-TOPOLOGY-MISMATCH (R-FS-1) — the topology resume carrier ↔
# strategy map. A PauseSnapshot populates EXACTLY ONE of these carriers (the NEVER-co-set
# invariant on C-CP-26 §26.2), and each `_execute_<strategy>` reads ONLY its own
# (`resume_snapshot.<carrier> if resume_snapshot is not None else <crash-reconstructed>`;
# `_is_resume = <carrier> is not None`). So a snapshot resumed under a strategy NOT in its
# carrier's set has the strategy read its absent carrier → `_is_resume` False → the whole
# topology runs FRESH (re-dispatching effect-bearing branches/stages). `effect_fence_resume`
# is consumed by the LINEAR_INLINE step loop (a fence pause is only ever captured in the
# linear/TOOL_STEP path — `workflow_driver.py` `_execute_workflow_body`, never a fan-out
# strategy function). A plain linear `step_index`-only resume populates NONE of these → no
# entry matched → never a mismatch (correctly admitted).
_RESUME_CARRIER_STRATEGIES: tuple[tuple[str, frozenset[_DriverStrategyStatus]], ...] = (
    (
        "fan_out_resume",
        frozenset(
            {
                _DriverStrategyStatus.ORCHESTRATOR_WORKERS,
                _DriverStrategyStatus.HIERARCHICAL_DELEGATION,
            }
        ),
    ),
    ("peer_fan_out_resume", frozenset({_DriverStrategyStatus.PARALLELIZATION})),
    ("handoff_resume", frozenset({_DriverStrategyStatus.DECENTRALIZED_HANDOFF})),
    ("evaluator_optimizer_resume", frozenset({_DriverStrategyStatus.EVALUATOR_OPTIMIZER})),
    ("effect_fence_resume", frozenset({_DriverStrategyStatus.LINEAR_INLINE})),
    # B-FANOUT-CRASH-RESUME-ORCHESTRATOR-MAYBE-RAN-EFFECT-BEARING — the orchestrator's OWN
    # effect-fence pause is captured + resumed by the orchestrator-workers strategy (the
    # orchestrator runs at `steps[0]` of ORCHESTRATOR_WORKERS / HIERARCHICAL_DELEGATION), so a
    # snapshot carrying it resumed under any other strategy fails closed (topology change).
    (
        "orchestrator_effect_fence_resume",
        frozenset(
            {
                _DriverStrategyStatus.ORCHESTRATOR_WORKERS,
                _DriverStrategyStatus.HIERARCHICAL_DELEGATION,
            }
        ),
    ),
)


def _resume_carrier_topology_mismatch(
    resume_snapshot: PauseSnapshot,
    strategy: _DriverStrategyStatus,
) -> str | None:
    """Fail closed when a pause snapshot's populated topology resume carrier does NOT match
    the resuming strategy (B-FANOUT-RESUME-CARRIER-TOPOLOGY-MISMATCH, R-FS-1).

    A `PauseSnapshot` records its fan-out / handoff / evaluator-optimizer / effect-fence
    recovery state on EXACTLY ONE topology-specific carrier, captured by the strategy that
    paused (`peer_fan_out_resume` by PARALLELIZATION, `handoff_resume` by
    DECENTRALIZED_HANDOFF, etc. — see ``_RESUME_CARRIER_STRATEGIES``). On a resume
    (``resume_snapshot is not None``) each strategy reads ONLY its own carrier; a topology
    change between pause and resume therefore leaves the resuming strategy reading its
    ABSENT carrier → it runs the WHOLE topology FRESH, re-dispatching effect-bearing
    branches/stages (an at-most-once violation). This guard fails that closed BEFORE any
    dispatch side-effect.

    GENERALIZES the B-FANOUT-PAUSE-SYNTHESIS synthesis-only carrier-mismatch guard to ALL
    topology resume carriers, synthesis-bearing or not — the v1.58 §1 "non-synthesis
    carrier/topology mismatch ... is PRE-EXISTING behavior ... unchanged" carve-out is now
    closed. It runs FIRST at the resume entry so the synthesis material-diff sees a
    carrier-consistent snapshot; the synthesis-specific carrier-mismatch leg (the
    both-dropped false-pass) is subsumed — a populated synthesis carrier the strategy does
    not read fails here on carrier-populated alone, independent of synthesis identity.

    The crash-resume path (``resume_snapshot is None``) never reaches here: each strategy
    RECONSTRUCTS its own carrier keyed to the executing topology (``_determine_fanout_resume``),
    so a foreign carrier cannot be surfaced there.

    Returns a ``fail_class`` string on mismatch, or ``None`` when the populated carrier (if
    any) matches the resuming strategy. The loop fails closed on ANY populated-but-foreign
    carrier, so a corrupt multi-populated snapshot (the never-co-set invariant violated)
    also fails closed rather than relying on one carrier happening to match.
    """
    for name, allowed in _RESUME_CARRIER_STRATEGIES:
        if getattr(resume_snapshot, name) is not None and strategy not in allowed:
            return (
                f"resume-carrier-topology-mismatch: the pause snapshot populated the "
                f"{name!r} resume carrier, which the resumed {strategy.value} topology does "
                "not read (the snapshot's topology changed between pause and resume). "
                "Resuming would run the whole topology FRESH, re-dispatching effect-bearing "
                "branches/stages (an at-most-once violation). Rejected fail-closed "
                "(B-FANOUT-RESUME-CARRIER-TOPOLOGY-MISMATCH)."
            )
    return None


def _synthesis_resume_material_diff(
    resume_snapshot: PauseSnapshot,
    steps: Sequence[WorkflowStep],
    strategy: _DriverStrategyStatus,
) -> str | None:
    """Material-diff the re-supplied terminal synthesis step against a pause
    snapshot's captured synthesis identity (B-FANOUT-PAUSE-SYNTHESIS, R-FS-1).

    On a `cascade_policy=pause` halt the post-join synthesis NEVER ran (the pause
    halts at the worker barrier, before it), so there is nothing to replay — but the
    synthesis IDENTITY (presence + ``step_id``) is captured on the fan-out resume
    carrier (`FanOutResumeState` / `PeerFanOutResumeState`) so a resume that
    re-reaches the terminal synthesis can verify the body did not change before
    FRESH-dispatching it on the recovered + re-dispatched branches (effect-free,
    first-and-only per B-POSTJOIN).

    The captured identity is read from the carrier THE RESUMING STRATEGY uses — NOT
    "whichever carrier the snapshot populated". A carrier/topology mismatch is now FRONT-RUN
    by ``_resume_carrier_topology_mismatch`` (B-FANOUT-RESUME-CARRIER-TOPOLOGY-MISMATCH), so
    this function only ever sees a carrier-consistent snapshot; the strategy-keyed read here
    is retained (a mismatch still incidentally surfaces as a material-diff when a synthesis
    is present in the resumed body — captured None ≠ present — which is harmless given the
    front guard). A non-fan-out strategy has no synthesis carrier → captured None.

    Returns a ``fail_class`` string on mismatch (synthesis added / removed / changed
    ``step_id``, or a carrier/topology mismatch), or ``None`` when the captured and
    re-supplied identities match (incl. both-absent — a non-synthesis resume, unchanged).
    The REMOVED case (snapshot captured a synthesis, resumed body dropped it) fails closed
    rather than silently yielding the deterministic fold — exactly the silent-drop class
    the original entry reject existed to prevent.

    B-FANOUT-CRASH-RESUME-ORCHESTRATOR-MAYBE-RAN-EFFECT-BEARING (out-of-family Codex [P2]) — an
    ORCHESTRATOR effect-fence resume (`orchestrator_effect_fence_resume` populated) ran NOTHING
    (the orchestrator paused at its OWN dispatch, BEFORE any worker and BEFORE the synthesis), so
    there is no captured synthesis identity to diff: the orchestrator + workers + synthesis ALL
    re-dispatch fresh on resume (a fresh-everything resume gated only by the changed-orchestrator
    guard at the dispatch site). Skip the material-diff — else the absent captured identity
    (`None`) would falsely reject an unchanged synthesis-bearing body as a "removed" diff."""
    if resume_snapshot.orchestrator_effect_fence_resume is not None:
        return None
    captured: str | None = None
    if strategy in (
        _DriverStrategyStatus.ORCHESTRATOR_WORKERS,
        _DriverStrategyStatus.HIERARCHICAL_DELEGATION,
    ):
        if resume_snapshot.fan_out_resume is not None:
            captured = resume_snapshot.fan_out_resume.synthesis_step_id
    elif strategy is _DriverStrategyStatus.PARALLELIZATION:
        if resume_snapshot.peer_fan_out_resume is not None:
            captured = resume_snapshot.peer_fan_out_resume.synthesis_step_id
    resumed: str | None = None
    if steps and steps[-1].step_kind is StepKind.POST_JOIN_SYNTHESIS:
        resumed = str(steps[-1].step_id)
    if captured == resumed:
        return None
    return (
        "post-join-synthesis-resume-material-diff: the resumed workflow body's terminal "
        f"synthesis identity ({resumed!r}) differs from the pause snapshot's captured "
        f"synthesis identity ({captured!r}) for the {strategy.value} carrier — the "
        "POST_JOIN_SYNTHESIS step was added, removed, or changed between pause and resume "
        "(or the snapshot's fan-out carrier does not match the resumed topology). A "
        "synthesis pause-resume fresh-dispatches the synthesis on the recovered branches, "
        "so a mismatched identity would dispatch a divergent synthesis (or silently drop "
        "it to the fold). Rejected fail-closed (B-FANOUT-PAUSE-SYNTHESIS)."
    )


def _split_synthesis(
    steps: Sequence[WorkflowStep],
) -> tuple[Sequence[WorkflowStep], WorkflowStep | None]:
    """Carve an opt-in terminal `POST_JOIN_SYNTHESIS` step out of a concurrent
    fan-out's branch set (CP spec v1.54 §3).

    Returns ``(branch_steps, synthesis_step)``: when the LAST step is a
    ``POST_JOIN_SYNTHESIS`` step it is carved out (NOT executed as a branch) and
    returned separately for the post-barrier dispatch; otherwise ``(steps, None)``
    — byte-identical to pre-v1.54 (the deterministic-fold path)."""
    if steps and steps[-1].step_kind is StepKind.POST_JOIN_SYNTHESIS:
        return steps[:-1], steps[-1]
    return steps, None


def _append_synthesis_ledger_entry(
    *,
    ctx: DriverContext,
    workflow_id: str,
    synthesis_index: int,
    synthesis_idempotency_key: str,
) -> None:
    """Append the terminal post-barrier synthesis step's state-ledger entry
    (CP spec v1.54 §3 disclosure), mirroring ``_append_step_ledger_entry`` but
    with a synthesis-DISCLOSING ``action_id``.

    The ``action_id`` ``workflow:{wf}:post-join-synthesis:{N}`` self-discloses
    the step as the non-deterministic LLM-composed aggregate — the §25.12
    Point-2 sacrifice made LOUD at the ledger. The entry rides the single real
    writer on the driver thread POST-barrier (after the branch buffers have
    drained), exactly as the linear terminal entry."""
    from harness_is.state_ledger_entry_schema import Identifier
    from harness_is.state_ledger_write import EntryPayload, WriteKey

    action_id = ActionID(f"workflow:{workflow_id}:post-join-synthesis:{synthesis_index}")
    _resolver = getattr(ctx, "procedural_tier_snapshot_resolver", None)
    _procedural_tier_snapshot_ref = _resolver() if _resolver is not None else None
    payload = EntryPayload(
        action_id=Identifier(str(action_id)),
        idempotency_key=Identifier(synthesis_idempotency_key),
        actor=ctx.ledger_writer.actor,
        timestamp=datetime.now(UTC),
        procedural_tier_snapshot_ref=_procedural_tier_snapshot_ref,
    )
    write_key = WriteKey(
        thread_id=Identifier(workflow_id),
        step_id=Identifier(f"post-join-synthesis:{synthesis_index}"),
        idempotency_key=Identifier(synthesis_idempotency_key),
    )
    ctx.ledger_writer.append(payload, write_key)


def _compute_synthesis_self_hash(step_id: str, output: Mapping[str, Any]) -> str:
    """B-FANOUT-OUTPUT-REPLAY PR2 — the record-local capture-time self-hash for a
    captured POST_JOIN_SYNTHESIS output.

    sha256 over canonical JSON of `(step_id, output)` — the `_compute_snapshot_hash`
    shape (sorted-key, compact separators, UTF-8). The synthesis is the ONE genuine
    integrity residual (#719 C9): non-deterministic, NO ledger `response_hash`, sole
    authority on a W3 crash. This self-hash is its only cross-check — recomputed over the
    read-back record on replay, fail-closed on mismatch (corruption / tamper). It is a
    harness-internal integrity field, NOT a §6 hash-chain link and NOT a second attested
    authority (§25.12 D1 preserved). The output is JSON-round-trip-stable (it came from an
    LLM dispatch → JSON), so capture-time and replay-time canonicalization agree byte-for-
    byte."""
    canonical = {"step_id": str(step_id), "output": dict(output)}
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _replay_captured_synthesis(
    *,
    store: Any,
    run_idempotency_key: str,
    synthesis_step: WorkflowStep,
    synthesis_index: int,
    synthesis_idempotency_key: str,
    ctx: DriverContext,
    manifest_entry: WorkflowManifestEntry,
    run_id: str,
) -> RunResult | dict[str, Any]:
    """B-FANOUT-OUTPUT-REPLAY PR2 — REPLAY a captured synthesis output (the W3 crash
    window: a crash AFTER the synthesis output was captured but BEFORE the run finalized).

    The branches are already reproduced (PR1 net-add #1-3); the captured synthesis output is
    replayed — verified by the record-local self-hash + the `step_id` material-diff — rather
    than re-dispatching a NON-reproducible fresh LLM compose. Three fail-closed gates:
    present-but-unreadable (a corrupt capture — never silently re-dispatch a fresh synthesis
    that would mask it), self-hash mismatch (tamper / corruption), and a changed synthesis
    body (`step_id` material-diff — the resumed manifest redefined the synthesis step). On
    success it RE-APPENDS the synthesis ledger entry (dedup-safe: a W3 crash can land AFTER
    the original append, so the deterministic idempotency key → `IDEMPOTENT_NOOP` for the
    already-persisted entry — PR1 §2 re-materialization discipline) + emits the STEP_BOUNDARY,
    then returns the captured output as the run's `final_state`."""
    captured_output, failure = _read_valid_captured_synthesis(
        store=store,
        run_idempotency_key=run_idempotency_key,
        synthesis_step=synthesis_step,
        synthesis_index=synthesis_index,
        manifest_entry=manifest_entry,
        run_id=run_id,
    )
    if failure is not None:
        return failure
    assert captured_output is not None
    # Audit completeness: re-append the synthesis ledger entry (DEDUP-SAFE — the W3 crash
    # can land after the original append; the deterministic key → IDEMPOTENT_NOOP) + emit the
    # STEP_BOUNDARY. Map a ledger/emitter failure to a FAILED RunResult here, exactly like the
    # fresh-dispatch path's `try`/`except` (out-of-family Codex [P2]): the replay runs
    # POST-barrier, OUTSIDE the inline per-step try/except, so an unwrapped raise would ESCAPE
    # `execute_workflow` instead of returning the expected failed workflow result.
    try:
        _append_synthesis_ledger_entry(
            ctx=ctx,
            workflow_id=manifest_entry.workflow_id,
            synthesis_index=synthesis_index,
            synthesis_idempotency_key=synthesis_idempotency_key,
        )
        ctx.lifecycle_emitter.emit(WorkflowEventClass.STEP_BOUNDARY)
    except Exception as exc:
        return RunResult(
            workflow_id=manifest_entry.workflow_id,
            run_id=run_id,
            status=RunStatus.FAILED,
            terminal_step_index=synthesis_index,
            partial_state=None,
            final_state=None,
            fail_class=_step_fail_class("post-join-synthesis-replay-failure", exc),
        )
    return captured_output


def _read_valid_captured_synthesis(
    *,
    store: Any,
    run_idempotency_key: str,
    synthesis_step: WorkflowStep,
    synthesis_index: int,
    manifest_entry: WorkflowManifestEntry,
    run_id: str,
) -> tuple[dict[str, Any] | None, RunResult | None]:
    """Read and validate a captured synthesis record without committing replay side effects."""
    captured = store.read_synthesis(run_idempotency_key)
    if captured is None:
        # `synthesis_present` is True (the file EXISTS) but `read_synthesis` yields no
        # readable record → a corrupt / torn / un-self-hashed capture. Fail closed.
        return (
            None,
            RunResult(
                workflow_id=manifest_entry.workflow_id,
                run_id=run_id,
                status=RunStatus.FAILED,
                terminal_step_index=synthesis_index,
                partial_state=None,
                final_state=None,
                fail_class=(
                    "post-join-synthesis-replay-corrupt: a captured synthesis file exists but "
                    "holds no readable self-hashed record — fail closed rather than re-dispatch a "
                    "fresh (non-reproducible) synthesis that would mask the corruption"
                ),
            ),
        )
    captured_step_id, captured_output, captured_self_hash = captured
    if _compute_synthesis_self_hash(captured_step_id, captured_output) != captured_self_hash:
        return (
            None,
            RunResult(
                workflow_id=manifest_entry.workflow_id,
                run_id=run_id,
                status=RunStatus.FAILED,
                terminal_step_index=synthesis_index,
                partial_state=None,
                final_state=None,
                fail_class=(
                    "post-join-synthesis-replay-self-hash-mismatch: the captured synthesis "
                    "record fails its record-local capture-time self-hash (corruption / tamper) "
                    "— fail closed (the synthesis carries no ledger response_hash to cross-check)"
                ),
            ),
        )
    if captured_step_id != str(synthesis_step.step_id):
        return (
            None,
            RunResult(
                workflow_id=manifest_entry.workflow_id,
                run_id=run_id,
                status=RunStatus.FAILED,
                terminal_step_index=synthesis_index,
                partial_state=None,
                final_state=None,
                fail_class=(
                    "post-join-synthesis-replay-material-diff: the resumed manifest's synthesis "
                    f"step_id ({synthesis_step.step_id!s}) differs from the captured identity "
                    f"({captured_step_id}) — a changed synthesis body; fail closed"
                ),
            ),
        )
    return dict(captured_output), None


def _captured_synthesis_replay_validation_failure(
    *,
    ctx: DriverContext,
    manifest_entry: WorkflowManifestEntry,
    run_idempotency_key: str,
    synthesis_step: WorkflowStep | None,
    branch_count: int,
    run_id: str,
) -> RunResult | None:
    """Validate pure captured-synthesis replay checks before a RECONCILER CAS claim."""
    if synthesis_step is None:
        return None
    store = _fanout_replay_store(ctx, manifest_entry)
    if store is None or not store.synthesis_present(run_idempotency_key):
        return None
    _captured_output, failure = _read_valid_captured_synthesis(
        store=store,
        run_idempotency_key=run_idempotency_key,
        synthesis_step=synthesis_step,
        synthesis_index=branch_count,
        manifest_entry=manifest_entry,
        run_id=run_id,
    )
    return failure


def _maybe_post_join_synthesis(
    *,
    synthesis_step: WorkflowStep | None,
    status: RunStatus,
    collected: Mapping[int, tuple[str, Mapping[str, Any]]],
    ctx: DriverContext,
    manifest_entry: WorkflowManifestEntry,
    step_dispatchers: StepDispatcherRegistry,
    default_model_binding: ModelBinding,
    fanout_parent: StepExecutionContext,
    run_idempotency_key: str,
    run_id: str,
    branch_count: int,
) -> RunResult | dict[str, Any] | None:
    """Dispatch the opt-in terminal `POST_JOIN_SYNTHESIS` step.

    Returns one of three (the caller discriminates):
    - ``None`` → use the byte-identical default deterministic fold (no synthesis
      opted in, OR the run is NOT ``SUCCESS`` — a salvaged ``PARTIAL`` uses the
      fold over its incomplete survivor set, never a synthesis over an incomplete
      sibling set).
    - a FAILED ``RunResult`` → the synthesis dispatch/append RAISED; the caller
      returns this directly (mirroring the inline per-step `try`/`except` → FAILED
      mapping, so an exception NEVER escapes `execute_workflow` post-drain —
      out-of-family Codex [P2]).
    - the synthesis output ``dict`` → SUCCESS; becomes the run's `final_state`.

    On SUCCESS: dispatch post-barrier (SYNC on the driver thread — the fan-out is
    drained, nothing runs concurrently; mirrors the linear
    ``step_dispatchers.lookup(...).dispatch``) reading the branch-index-ordered
    siblings on ``StepExecutionContext.sibling_outputs``, append the disclosing
    ledger entry, return the output (the §25.12 Point-2 sacrifice for this run;
    Point-1 + branch-index ordering preserved). Read-only / effect-free."""
    if synthesis_step is None or status is not RunStatus.SUCCESS:
        return None
    # The synthesis disclosure ordinal = the post-carve branch count (out-of-family
    # adversarial-reviewer F3-3): this IS the synthesis step's index in the original
    # `steps` for PARALLELIZATION (all prior steps are branches), but for
    # ORCHESTRATOR_WORKERS / HIERARCHICAL_DELEGATION the orchestrator at steps[0] is not a
    # branch, so it is a STABLE UNIQUE terminal ordinal (distinct from every 0..N-1 branch
    # index) for the disclosure action_id + idempotency key — not a literal original-steps
    # position. Uniqueness within the run is what the action_id/key require.
    synthesis_index = branch_count
    sibling_outputs: tuple[tuple[int, Mapping[str, Any]], ...] = tuple(
        (bi, dict(out)) for bi, (_sid, out) in sorted(collected.items())
    )
    # B-POSTJOIN zero-sibling guard (out-of-family Codex round 6 [P2]): the placement
    # guard's static `len(steps) < 2` catches a PARALLELIZATION lone-synthesis, but an
    # ORCHESTRATOR_WORKERS / HIERARCHICAL_DELEGATION `[orchestrator, synthesis]` has
    # len == 2 (the orchestrator is steps[0], NOT a branch) → it passes placement, the
    # orchestrator runs, and the fan-out drains ZERO workers → an empty `collected`. A
    # synthesis over zero siblings spends an LLM call on no branch data + violates the
    # spec §3 "follows a concurrent fan-out with ≥1 sibling" requirement. Reject
    # fail-closed at dispatch time (the only point the post-carve branch count is known
    # for a DYNAMIC worker fan-out; a static topology-specific guard would over-reject a
    # legitimately dynamic `[orchestrator, synthesis]`). Mirrors the placement-guard
    # FAILED shape; the partially-run orchestrator's side effects are already recorded.
    if not sibling_outputs:
        return RunResult(
            workflow_id=manifest_entry.workflow_id,
            run_id=run_id,
            status=RunStatus.FAILED,
            terminal_step_index=synthesis_index,
            partial_state=None,
            final_state=None,
            fail_class=(
                "post-join-synthesis-no-siblings: POST_JOIN_SYNTHESIS requires ≥1 fan-out "
                "sibling to compose; the concurrent fan-out produced zero branches (e.g. an "
                "ORCHESTRATOR_WORKERS / HIERARCHICAL_DELEGATION [orchestrator, synthesis] with "
                "no workers). Rejected fail-closed rather than spend an LLM call on no data."
            ),
        )
    synthesis_idempotency_key = _compute_step_idempotency_key(run_idempotency_key, synthesis_index)
    # B-FANOUT-OUTPUT-REPLAY PR2 — REPLAY a captured synthesis (the W3 crash window). The
    # SHARED `_fanout_replay_store` gate governs BOTH the capture (below) and this consume,
    # so the two halves can never skew. A captured synthesis is present ONLY on a crash-
    # resume where the synthesis ran + was captured before the crash (a fresh run captures
    # AFTER dispatch, never re-entering this terminal step in the same run) → replay it
    # reproducibly. ABSENT a capture (a crash BEFORE the synthesis ran, or a non-replay-
    # capable run) → fall through to a fresh dispatch on the reproduced branches (effect-
    # free, first-and-only; non-byte-reproducible by construction but consistent — it sits on
    # the SAME reproduced siblings, per CP spec v1.56 §1).
    _replay_store = _fanout_replay_store(ctx, manifest_entry)
    if _replay_store is not None and _replay_store.synthesis_present(run_idempotency_key):
        return _replay_captured_synthesis(
            store=_replay_store,
            run_idempotency_key=run_idempotency_key,
            synthesis_step=synthesis_step,
            synthesis_index=synthesis_index,
            synthesis_idempotency_key=synthesis_idempotency_key,
            ctx=ctx,
            manifest_entry=manifest_entry,
            run_id=run_id,
        )
    synth_binding = resolve_step_binding(
        manifest_entry,
        str(synthesis_step.step_id),
        default_model_binding=default_model_binding,
        persona_tier=manifest_entry.persona_tier,
    )
    synthesis_context = fanout_parent.model_copy(
        update={
            # The synthesis context's parent_action_id is set CONSISTENT with the synthesis
            # step's OWN disclosing ledger entry action_id (`_append_synthesis_ledger_entry`:
            # `workflow:{wf}:post-join-synthesis:{N}`), NOT the generic `...:step:{N}`
            # (out-of-family Codex round 8 [P2]): a downstream record that references this
            # context's parent_action_id (cost attribution, an LLM-dispatch span, a HITL
            # audit entry if a gate fires on the synthesis) should resolve to a REAL ledger
            # entry; the prior `...:step:{N}` referenced an action_id with NO matching
            # synthesis entry. This makes the reference RESOLVE — it does not by itself prove
            # any specific consumer join (no `:step:`-suffix parser runs on this context:
            # `compose_branch_terminal_*` only APPENDS + is gated on `branch_index`, which is
            # None here). The idempotency key keys off `synthesis_index` directly, not this
            # string, so it is unaffected.
            "parent_action_id": str(
                ActionID(
                    f"workflow:{manifest_entry.workflow_id}:post-join-synthesis:{synthesis_index}"
                )
            ),
            "parent_idempotency_key": synthesis_idempotency_key,
            "step_index": synthesis_index,
            "sibling_outputs": sibling_outputs,
            # the synthesis is a top-level terminal step, NOT a branch child
            "branch_index": None,
            # B-POSTJOIN per-step overrides on the SYNTHESIS step apply to ITS context,
            # not the parent's (out-of-family Codex round 2 [P2]): the runtime routing
            # reads `agent_role` + the wrap-time HITL composer reads `hitl_placements`, so
            # a per-step role / HITL override on the synthesis step must fold into the
            # synthesis context — else it routes/gates as the fan-out parent. Mirrors the
            # per-branch `compose_branch_child_context` + `fold_step_hitl_placements`.
            #
            # Role fallback = the synthesis step's OWN derived role, NOT the parent's
            # (out-of-family Codex round 7 [P2]): for ORCHESTRATOR_WORKERS /
            # HIERARCHICAL_DELEGATION, `fanout_parent` IS the orchestrator_context, which
            # carries the ORCHESTRATOR's per-step role override — so falling back to
            # `fanout_parent.agent_role` would dispatch the synthesis under the
            # orchestrator's role (wrong per-role model/routing). Workers explicitly avoid
            # this leak (`binding.agent_role or derive_agent_role(step.step_id)` at the
            # worker spawn), so the synthesis mirrors them: its own per-step override wins,
            # else its own step-id-derived role (a distinct, operator-bindable role via
            # `per_role_bindings`, never the orchestrator's). Truthiness-fold consistent
            # with the worker site (an empty `AgentRole("")` is not a usable routing key).
            "agent_role": synth_binding.agent_role or derive_agent_role(synthesis_step.step_id),
            "hitl_placements": fold_step_hitl_placements(
                manifest_entry.hitl_placements, synth_binding.hitl_placement
            ),
        }
    )
    # B-POSTJOIN failed-run mapping (out-of-family Codex [P2]): the synthesis runs
    # POST-barrier, OUTSIDE the inline per-step try/except, so an unbound dispatcher /
    # raising LLM call / failing ledger append would ESCAPE execute_workflow after the
    # branch buffers drained. Map to a FAILED RunResult here (mirrors the inline
    # step-dispatch mapping at the §25.3 loop), so the caller returns it cleanly.
    try:
        # B-POSTJOIN override provenance (out-of-family Codex [P2]): a per-step
        # override on the SYNTHESIS step must emit the override-application ledger
        # entry like every other path (linear `emit_override_state_ledger_entry` /
        # branch `_buffer_branch_override_if_applied`), else the synthesized final
        # state runs under an override with NO provenance (C-CP-06 §6.6 all-paths
        # contract). The synthesis runs on the driver thread post-barrier → the
        # DIRECT emit (mirroring the linear `_execute_workflow_body` guard), gated
        # on `override_applied`.
        if synth_binding.override_applied:
            _cp_is_wiring = getattr(ctx, "cp_is_wiring", None)
            if _cp_is_wiring is not None:
                _run_protocol_method_sync(
                    _cp_is_wiring.emit_override_state_ledger_entry(
                        workflow_id=manifest_entry.workflow_id,
                        step_id=str(synthesis_step.step_id),
                        post_override_step_config=synth_binding.model_dump(mode="json"),
                        actor=ActorIdentity(ctx.ledger_writer.actor.actor_id),
                    )
                )
        synth_output = step_dispatchers.lookup(StepKind.POST_JOIN_SYNTHESIS).dispatch(
            synth_binding, synthesis_step, step_context=synthesis_context
        )
        # B-FANOUT-OUTPUT-REPLAY PR2 — CAPTURE the synthesis output + record-local self-hash,
        # RESERVE-before-COMMIT (BEFORE the synthesis ledger-append below), gated on the SAME
        # `_fanout_replay_store` predicate as the replay above. A W3 crash AFTER this capture
        # replays the output (verified by the self-hash) on resume instead of re-dispatching a
        # NON-reproducible fresh compose. Default (no store / not replay-capable) → no capture,
        # byte-identical to pre-PR2. The synthesis carries no ledger response_hash (#719 C9), so
        # this self-hash is its sole integrity cross-check.
        #
        # PRE-CAPTURE crash window (out-of-family Codex round-4 [P2], DELIBERATELY accepted —
        # advisor-reconciled). The dispatch (above) precedes this capture, so a crash BETWEEN
        # the dispatch returning and this fsync loses the output → on resume `synthesis_present`
        # is False (indistinguishable from "synthesis never ran") → fresh re-dispatch. This is
        # SAFE and consistent: (1) the synthesis is ENFORCED effect-free (the runtime LLM
        # dispatch boundary, B-POSTJOIN) → a re-dispatch fires NO duplicate effect; (2) the lost
        # aggregate was never committed (the ledger-append is AFTER this capture) → no consumer
        # observed it → nothing to be non-reproducible against; (3) PR1 ALREADY accepts the
        # identical dispatch-before-capture window for effect-BEARING branches (absent → re-
        # dispatched), so a started-marker + fail-closed here would hold the effect-free
        # synthesis to a STRICTER standard than the effect-bearing branches — incoherent, and it
        # would degrade availability for no correctness gain. LOAD-BEARING DEPENDENCY: this is
        # safe ONLY while the synthesis stays effect-free; if that is ever relaxed, this window
        # needs a durable "synthesis-started" marker + fail-closed-on-marker-without-capture.
        if _replay_store is not None:
            _synth_dict = dict(synth_output)
            _replay_store.record_synthesis(
                run_idempotency_key,
                str(synthesis_step.step_id),
                _synth_dict,
                _compute_synthesis_self_hash(str(synthesis_step.step_id), _synth_dict),
            )
        _append_synthesis_ledger_entry(
            ctx=ctx,
            workflow_id=manifest_entry.workflow_id,
            synthesis_index=synthesis_index,
            synthesis_idempotency_key=synthesis_idempotency_key,
        )
        # B-POSTJOIN telemetry (out-of-family Codex [P2]): the synthesis IS a step
        # that executed → emit its lifecycle STEP_BOUNDARY (the caller increments
        # `steps_executed` by one on a dict return), so `workflow.step_count` counts it.
        ctx.lifecycle_emitter.emit(WorkflowEventClass.STEP_BOUNDARY)
    except StepKindDispatcherNotBoundError as exc:
        return RunResult(
            workflow_id=manifest_entry.workflow_id,
            run_id=run_id,
            status=RunStatus.FAILED,
            terminal_step_index=synthesis_index,
            partial_state=None,
            final_state=None,
            fail_class=(
                f"post-join-synthesis-failure: RT-FAIL-STEP-KIND-DISPATCHER-NOT-BOUND: {exc}"
            ),
        )
    except Exception as exc:
        # B-POSTJOIN timeout taxonomy (out-of-family Codex [P2]): a synthesis LLM call
        # exceeding `step_dispatch_timeout_seconds` raises `StepDispatchTimeoutError`
        # (no `rt_fail_class` attr → `_step_fail_class` would mislabel it the class
        # name). Name-match it to the canonical RT-FAIL-STEP-DISPATCH-TIMEOUT, like the
        # inline §25.3 loop (harness-cp cannot import the runtime exception type), so
        # alerts keyed on the failure taxonomy catch timed-out synthesis steps.
        _fc = (
            f"post-join-synthesis-failure: RT-FAIL-STEP-DISPATCH-TIMEOUT: {exc}"
            if type(exc).__name__ == "StepDispatchTimeoutError"
            else _step_fail_class("post-join-synthesis-failure", exc)
        )
        return RunResult(
            workflow_id=manifest_entry.workflow_id,
            run_id=run_id,
            status=RunStatus.FAILED,
            terminal_step_index=synthesis_index,
            partial_state=None,
            final_state=None,
            fail_class=_fc,
        )
    except BaseException as exc:
        # B-POSTJOIN durable-HITL-pause (out-of-family Codex [P1]): the synthesis flows
        # through the hitl_inference composer (stage-5 chain), so a PRE_ACTION gate on
        # the synthesis step can raise the durable-async `HITLPauseRequestedSignal` (a
        # BaseException — NOT caught by `except Exception` above). The inline §25.3 loop
        # captures it → PAUSED, but a paused TERMINAL post-barrier synthesis has no
        # resumable re-entry today (fan-out resume is branch-scoped; reproducible
        # synthesis resume is the registered B-FANOUT-OUTPUT-REPLAY follow-on). So map
        # it to FAILED fail-closed (no dead-end PAUSED that cannot resume; no escaping
        # BaseException), naming the SYNC-HITL alternative. Other BaseExceptions
        # (KeyboardInterrupt / SystemExit) are NOT swallowed — re-raised.
        if type(exc).__name__ != "HITLPauseRequestedSignal":
            raise
        return RunResult(
            workflow_id=manifest_entry.workflow_id,
            run_id=run_id,
            status=RunStatus.FAILED,
            terminal_step_index=synthesis_index,
            partial_state=None,
            final_state=None,
            fail_class=(
                "post-join-synthesis-failure: durable-async HITL pause on "
                "POST_JOIN_SYNTHESIS is not resumable (use a synchronous HITL gate; "
                "reproducible synthesis resume is the registered B-FANOUT-OUTPUT-REPLAY "
                "follow-on)"
            ),
        )
    return dict(synth_output)


# ---------------------------------------------------------------------------
# Branch ledger write-cadence (C-CP-25 §25.13 + IS §5.4 + runtime §2.2(c) — U-CP-84)
# ---------------------------------------------------------------------------
#
# The producer-cadence by which the CP WorkflowDriver populates the IS
# `branch_metadata` sidecar under a non-linear topology strategy (U-CP-86+). Two
# helpers, both buffering into the branch's own `BufferingLedgerWriter` (U-CP-82)
# so the single barrier drain (`drain_branch_buffers`) serializes them in
# branch-index order with the existing single-writer discipline — NO change to
# `drain_branch_buffers`, NO second `prior_event_hash` (D1). The linear
# `_append_step_ledger_entry` is unaffected (its `branch_metadata` stays the
# carrier default `None`).
#
# Timestamp discipline (the determinism-boundary ⟂ IS-monotonicity interaction).
# The IS writer enforces a ZERO-tolerance non-decreasing-timestamp invariant
# (`state_ledger_write.append_ledger_entry`, `_CLOCK_SKEW_TOLERANCE = timedelta(0)`),
# while the drain persists in branch-index order independent of which branch's
# model call returned first (§25.12). Wall-clock stamps captured at branch
# *execution* time would therefore trip `NonMonotonicTimestampError` once a
# lower-branch-index entry happens to be stamped later than a higher one — and,
# under recursion, a `SUB_AGENT_DISPATCH` child sharing the writer drains its
# entries DURING the parent's barrier (before the parent's LATE post-barrier
# drain), a cross-level inversion the within-level shared-timestamp policy can't
# reach. This is NOT a spec contradiction (§25.12 constrains append *order*, not
# timestamp *values*; the canonical reading — consistent with the linear path —
# is that the timestamp records the ledger-*append* event, and a fan-out is one
# barrier-drain persist event). The realization: `drain_branch_buffers` re-stamps
# every entry to a single drain-moment timestamp at its actual append point, so
# physical-append-order == timestamp-order for **causally-ordered** drains —
# every level reached through the single-threaded recursion seam, plus the
# interleaved DIRECT linear-inline writer, are serialized in causal order and stay
# non-decreasing. This is NOT "by construction" for **concurrent** appends the
# drain cannot order: `drain_timestamp` is captured OUTSIDE the IS writer's
# `_WRITE_LOCK`, so two sibling `SUB_AGENT_DISPATCH` children draining on separate
# fan-out threads (or a runtime audit / cost write interleaving the lock between
# this capture and its appends) can still invert → `NonMonotonicTimestampError`.
# That is a known gap — unreachable today behind the runtime sync/async-bridge
# deadlock, and equally broken under the prior fan-out-start-timestamp policy (NOT
# a regression); the clean fix is timestamp-authority INSIDE `_WRITE_LOCK` (an IS
# write-path change, contract-touching) belonging to the same arc as the deadlock.
# See `.harness/runtime_defect_sub_agent_inference_child_loop_bridge_deadlock.md`
# §8 + `drain_branch_buffers` + `test_concurrent_sibling_drains_invert_timestamp`
# (xfail, strict). The `timestamp=` these helpers
# carry is a buffer-time placeholder the drain overrides (it never reaches the
# ledger; see `drain_branch_buffers`). The R-003
# active-workflow-context invariant (IS §5.1
# `procedural_tier_snapshot_ref` populated at every producer site) is honored via
# a caller-supplied injection param (defaulting `None`) on both helpers — the
# strategy (U-CP-86), which holds the `DriverContext` resolver the linear
# `_append_step_ledger_entry` reads, resolves + passes it, keeping these helpers
# pure (no `DriverContext` coupling) and the public API forward-complete.


def append_branch_step_ledger_entry(
    *,
    branch_writer: BufferingLedgerWriter,
    branch_context: StepExecutionContext,
    run_idempotency_key: str,
    local_step_index: int,
    timestamp: datetime,
    procedural_tier_snapshot_ref: Identifier | None = None,
) -> None:
    """Buffer a branch's per-step ledger entry carrying causality-only
    `branch_metadata` (`terminal_status=None`) — C-CP-25 §25.13 / runtime §2.2(c).

    Composes the branch-unique `action_id` (`compose_branch_step_action_id`) + the
    branch-scoped idempotency key (`compose_branch_path` → `_compute_step_idempotency_key`,
    U-CP-83) + the `branch_metadata` causality carrier (`compose_branch_metadata`,
    `terminal_status=None`) and buffers it through the branch's
    `BufferingLedgerWriter` (the write is deferred to the barrier drain; dispatch
    + telemetry already fired inline, so the pre-dispatch gate is never deferred,
    §25.15.2 obl. 2). `branch_context` must be a branch child context (the
    composers raise on a linear context). `timestamp` is a buffer-time
    placeholder the barrier drain overrides (`drain_branch_buffers` re-stamps to
    the append moment); see the module-level timestamp discipline.

    `procedural_tier_snapshot_ref` is the **caller-supplied** R-003 sidecar (IS
    spec v1.3 §5.1): a branch step entry is written inside an active-workflow
    context, so the strategy (U-CP-86) — which holds the `DriverContext` resolver
    the linear `_append_step_ledger_entry` reads — resolves and passes it here to
    honor the active-workflow-context population invariant. Defaulting `None` keeps
    this helper pure (no `DriverContext` coupling) and matches the §5.1
    omit-when-`None` canonicalization for the resolver-less paths (tests, an
    operator with no resolver bound).
    """
    from harness_is.state_ledger_entry_schema import Identifier
    from harness_is.state_ledger_write import EntryPayload, WriteKey

    branch_metadata = compose_branch_metadata(branch_context, terminal_status=None)
    action_id = compose_branch_step_action_id(branch_context, local_step_index)
    idempotency_key = _compute_step_idempotency_key(
        run_idempotency_key,
        local_step_index,
        compose_branch_path(branch_context),
    )
    payload = EntryPayload(
        action_id=Identifier(action_id),
        idempotency_key=Identifier(idempotency_key),
        actor=branch_writer.actor,
        timestamp=timestamp,
        procedural_tier_snapshot_ref=procedural_tier_snapshot_ref,
        branch_metadata=branch_metadata,
    )
    write_key = WriteKey(
        thread_id=Identifier(branch_context.workflow_id),
        step_id=Identifier(f"{branch_metadata.branch_index}:{local_step_index}"),
        idempotency_key=Identifier(idempotency_key),
    )
    branch_writer.append(payload, write_key)


def append_branch_terminal_ledger_entry(
    *,
    branch_writer: BufferingLedgerWriter,
    branch_context: StepExecutionContext,
    run_idempotency_key: str,
    terminal_status: Literal["cancelled", "completed", "timed_out"],
    timestamp: datetime,
    procedural_tier_snapshot_ref: Identifier | None = None,
) -> None:
    """Buffer a branch's **fresh terminal entry** carrying the dispatch-boundary
    disposition — C-CP-25 §25.13 / IS §5.4 append-only invariant / runtime §2.2(c).

    A branch's terminal disposition is recorded at a fresh terminal entry (its own
    `compose_branch_terminal_action_id` marker + the distinct
    `compose_branch_terminal_path` idempotency key, so the IS dedup never drops it)
    — **never** by mutating an already-buffered step entry. Buffered as the
    branch's last entry, so the existing `drain_branch_buffers` appends it after
    the branch's step entries in branch-index order; the §6.3 chain re-verifies
    because every entry (including this one) is a fresh append.

    `terminal_status` is the caller-decided disposition (U-CP-85's cascade logic);
    U-CP-84 persists the value it is handed. The carrier's closed set forecloses
    `failed` — a ran-and-errored branch is `completed` (dispatch-boundary, not
    step-outcome). `procedural_tier_snapshot_ref` is the caller-supplied R-003
    sidecar — see `append_branch_step_ledger_entry` (the terminal entry is written
    at the barrier drain, still inside the active-workflow context). `branch_context`
    must be a branch child context.
    """
    from harness_is.state_ledger_entry_schema import Identifier
    from harness_is.state_ledger_write import EntryPayload, WriteKey

    branch_metadata = compose_branch_metadata(branch_context, terminal_status=terminal_status)
    action_id = compose_branch_terminal_action_id(branch_context)
    idempotency_key = _compute_step_idempotency_key(
        run_idempotency_key,
        branch_context.step_index,
        compose_branch_terminal_path(branch_context),
    )
    payload = EntryPayload(
        action_id=Identifier(action_id),
        idempotency_key=Identifier(idempotency_key),
        actor=branch_writer.actor,
        timestamp=timestamp,
        procedural_tier_snapshot_ref=procedural_tier_snapshot_ref,
        branch_metadata=branch_metadata,
    )
    write_key = WriteKey(
        thread_id=Identifier(branch_context.workflow_id),
        step_id=Identifier(f"{branch_metadata.branch_index}:terminal"),
        idempotency_key=Identifier(idempotency_key),
    )
    branch_writer.append(payload, write_key)


def append_branch_override_ledger_entry(
    *,
    branch_writer: BufferingLedgerWriter,
    workflow_id: str,
    step_id: str,
    post_override_step_config: Mapping[str, Any],
    actor: ActorIdentity,
    timestamp: datetime,
    procedural_tier_snapshot_ref: Identifier | None = None,
) -> None:
    """Buffer a branch's per-step **override-application** ledger entry — the
    non-linear counterpart of the `SINGLE_THREADED_LINEAR` site at
    `_execute_workflow_body` (`emit_override_state_ledger_entry`, gated on
    `binding.override_applied`) — R-FS-1 `B-NONLINEAR-OVERRIDE-PROVENANCE`.

    Closes the CP spec v1.40 §6.6 topology-scope gap: at HEAD only the linear
    path emitted the `cp.per-step-override-application` entry, so a per-step
    override on any of the 5 non-linear strategies (model / prompt / role) was
    applied at dispatch but left **no dedicated override-ledger entry**. This
    helper emits it **through the buffered-branch path** (C-CP-25 §25.12 D1.b):
    the entry is buffered into the branch's `BufferingLedgerWriter`, so the
    single barrier drain (`drain_branch_buffers`) serializes it through the one
    real writer on the driver thread in branch-index order — realizing ADR-F2
    v1.2's single-threaded-write boundary without a synchronous per-worker write.

    The entry is composed via `compose_override_entry_payload` (the shared shape
    authority), so the persisted entry is **byte-shape-identical** to the linear
    path — same `action_id` + the same §16.5.4 `(workflow_id, step_id,
    outcome_hash)` idempotency key. The key is per-`(step, outcome)` (NOT
    branch-scoped — unlike the step/terminal entries above, whose
    `compose_branch_path`/`compose_branch_terminal_path` scoping prevents the IS
    dedup from dropping a legitimately-repeated step *execution*): an override is
    a static property of the resolved binding, so a `(step, outcome)` repeated
    across iterations / recursion levels idempotently dedups at the IS writer to
    one entry (the spec's designed §16.5.4 semantic).

    `timestamp` is a buffer-time placeholder the barrier drain overrides
    (`drain_branch_buffers` re-stamps to the append moment; see the module-level
    timestamp discipline). `procedural_tier_snapshot_ref` is the caller-supplied
    R-003 sidecar (IS spec v1.3 §5.1) — the strategy resolves + passes it, the
    same way the step/terminal helpers do; `None` for a resolver-less ctx.
    """
    from harness_is.state_ledger_entry_schema import Identifier
    from harness_is.state_ledger_write import WriteKey

    payload = compose_override_entry_payload(
        workflow_id=workflow_id,
        step_id=step_id,
        post_override_step_config=post_override_step_config,
        actor=actor,
        procedural_tier_snapshot_ref=procedural_tier_snapshot_ref,
        timestamp=timestamp,
    )
    write_key = WriteKey(
        thread_id=Identifier(workflow_id),
        step_id=Identifier(step_id),
        idempotency_key=payload.idempotency_key,
    )
    branch_writer.append(payload, write_key)


def _buffer_branch_override_if_applied(
    *,
    branch_writer: BufferingLedgerWriter,
    workflow_id: str,
    step: WorkflowStep,
    binding: StepEffectiveBinding,
    timestamp: datetime,
    snapshot_ref: Identifier | None,
) -> None:
    """Buffer the per-step override-application entry IFF the override was applied
    (`binding.override_applied`) — the non-linear sibling of the
    `_execute_workflow_body` linear guard (`emit_override_state_ledger_entry`,
    gated on `binding.override_applied`). R-FS-1 `B-NONLINEAR-OVERRIDE-PROVENANCE`.

    The shared guard + plumbing every non-linear strategy calls (at branch-plan
    construction / sequential dispatch) so the firing condition + the
    `binding.model_dump(mode="json")` outcome-bytes projection + the actor
    derivation stay single-source. A no-op when no override applied (byte-
    identical pre-arc behavior).
    """
    if not binding.override_applied:
        return
    append_branch_override_ledger_entry(
        branch_writer=branch_writer,
        workflow_id=workflow_id,
        step_id=str(step.step_id),
        post_override_step_config=binding.model_dump(mode="json"),
        actor=ActorIdentity(branch_writer.actor.actor_id),
        timestamp=timestamp,
        procedural_tier_snapshot_ref=snapshot_ref,
    )


# ---------------------------------------------------------------------------
# U-CP-86 — PARALLELIZATION driver strategy (C-CP-25 §25.11)
# ---------------------------------------------------------------------------
#
# The FIRST non-linear topology strategy — fan-out-barrier-aggregate. Per
# §25.11 "strategies differ only in *control flow over steps*", the SAME
# `steps` sequence the `SINGLE_THREADED_LINEAR` loop runs sequentially is here
# run CONCURRENTLY: each declared `WorkflowStep` is one branch (branch_index =
# its ordinal) over its varied `step_payload` (§25.11 PARALLELIZATION row + the
# B1 design "variation is in *inputs*, not agent specialization"). This reuses
# the existing `execute_workflow(manifest, steps, ...)` input with ZERO schema
# extension (a branch-spec payload schema would be an X-AL-3 spec extension).
#
# Composes the U-CP-80..84 substrate:
#   - branch child contexts            (compose_branch_child_context, U-CP-81)
#   - the buffered/deferred-append path (BufferingLedgerWriter + the
#     branch-index-ordered drain, U-CP-82) — NEVER the linear inline append
#   - branch_metadata causality + a `completed` terminal entry per branch
#     (append_branch_step/terminal_ledger_entry, U-CP-84)
#   - the policy-NEUTRAL bounded barrier (bounded_barrier, U-CP-82).
#
# U-CP-86 does NOT depend on U-CP-85 (cascade), so it uses NO cascade-cancel:
# the barrier is `bounded_barrier` (policy-neutral, leak-free); a branch failure
# maps to `RunStatus.FAILED`. The richer `cascade_policy` proceed/pause/
# cascade-cancel differentiation (→ PARTIAL/PAUSED/FAILED) is U-CP-85's machinery,
# first consumed by U-CP-88 (ORCHESTRATOR_WORKERS).
#
# Determinism (§25.12): both the persisted append order AND the aggregate are
# pure functions of the ORDERED (branch-index) result set — never completion
# order. `drain_branch_buffers` sorts by branch_index; `_aggregate_parallelization`
# votes with a lowest-branch-index tiebreak.

_DEFAULT_PARALLELIZATION_AGENT_ROLE = AgentRole("parallelization-worker")
"""The single per-worker role for PARALLELIZATION branches (C-CP-25 §25.11).

PARALLELIZATION varies *inputs*, NOT agent specialization — one role for all
branches (the B1 design "non-degenerate with one role"). The runtime role-read
(U-RT-114) therefore routes every branch to the same model. Per-role worker
specialization is the ORCHESTRATOR_WORKERS family (U-CP-88+)."""


def _per_step_role_override(
    manifest_entry: WorkflowManifestEntry, step_id: object
) -> AgentRole | None:
    """The per-step `StepOverride.agent_role` for `step_id`, else `None` (B4 Slice 4).

    A lightweight read of the per-step ROLE override (CP spec v1.38 §6.1) for the
    composition sites that do NOT already resolve a full `StepEffectiveBinding`
    (the orchestrator's own context; the decentralized handoff-record `next_role`
    preview of the next stage). Where a `binding` is already in hand, read
    `binding.agent_role` directly — `resolve_step_binding` sets it to the same
    override value, so the two are equivalent. `None` ⟹ no override ⟹ the call
    site's existing role source (derived / default / unset) is unchanged.
    """
    override = manifest_entry.per_step_overrides.get(step_id)  # type: ignore[arg-type]
    return override.agent_role if override is not None else None


def _per_step_hitl_placement_override(
    manifest_entry: WorkflowManifestEntry, step_id: object
) -> HITLPlacement | None:
    """The per-step `StepOverride.hitl_placement` for `step_id`, else `None` (v1.49).

    The `_per_step_role_override` analogue for the per-step HITL placement override
    (CP spec v1.49 §6.2 fold) — a lightweight read for the composition sites that
    do NOT already resolve a full `StepEffectiveBinding` (the orchestrator's own
    context). Where a `binding` is in hand, read `binding.hitl_placement` directly
    (`resolve_step_binding` sets it to the same override value). `None` ⟹ no
    override ⟹ `fold_step_hitl_placements` returns the workflow tuple verbatim.
    """
    override = manifest_entry.per_step_overrides.get(step_id)  # type: ignore[arg-type]
    return override.hitl_placement if override is not None else None


_DEFAULT_FANOUT_BARRIER_DEADLINE_SECONDS = 300.0
"""Wall-clock deadline on the fan-out barrier (C-CP-25 §25.11 bounded barriers;
O-CP-1(c) impl-discretion). Bounds the PARENT's return: a branch stuck past this
cap does not strand the workflow — the barrier raises and the run returns
`RunStatus.FAILED` (the fan-out is driven so a wedged SYNC branch thread cannot
re-defeat the cap at executor shutdown; see `_run_fanout_to_completion`). The
HARD in-flight EFFECT cut-off (cancelling a running dispatch) is §25.15 cascade
scope (U-CP-85), deliberately excluded from U-CP-86 per its dependency set.
Generous default sized for INFERENCE_STEP branches; a manifest-surfaced
per-workflow deadline is a forward field (not surfaced at v1.32)."""

_HIERARCHICAL_DELEGATION_FANOUT_CAP = 3
"""Fan-out cap per parent for `HIERARCHICAL_DELEGATION` (C-CP-25 §25.11 row —
"recursive bounded-fan-out … fan-out cap 3 per parent (C-CP-10 §10.3)";
`topology_pattern.py:76` "scope-bounded recursion; fan-out cap 3 per parent").

Spec-PINNED at 3 for HIERARCHICAL_DELEGATION (distinct from the §25.18
impl-discretion that governs OTHER patterns' per-cell caps). Counts a level's
DIRECT children = the worker steps `steps[1:]` under the level's orchestrator
(`steps[0]`). A level whose worker count exceeds the cap is rejected
`detect-then-refuse` (`RunStatus.FAILED`), NEVER silently truncated. The cap
auto-applies at EVERY recursion level whose child manifest declares
`HIERARCHICAL_DELEGATION` (a child declaring `ORCHESTRATOR_WORKERS` re-enters
the uncapped strategy — by design: the cap is a property of this topology)."""


def _parallelization_fanout_action_id(workflow_id: str) -> str:
    """The shared fan-out parent `action_id` every branch descends from.

    All PARALLELIZATION branches fan out from the workflow root, so they share
    one fan-out point. `(parent_action_id, branch_index)` is the branch
    causality key (IS spec v1.8 §5.4); a single fan-out `action_id` + the
    per-branch index yields a globally-unique key per branch (no `branch_path`
    at the causality key — Route Y).
    """
    return f"workflow:{workflow_id}:fanout"


def _aggregate_parallelization(
    branch_outputs: list[tuple[int, str, Mapping[str, Any]]],
) -> dict[str, Any]:
    """Fold the branch outputs into one result — voting, deterministic tiebreak
    = lowest branch-index (C-CP-25 §25.11 aggregator + §25.12 determinism).

    `branch_outputs` is `[(branch_index, step_id, output), ...]`. The fold is a
    PURE function of the ORDERED (branch-index) set — never completion order
    (the §25.12 determinism boundary; "first to finish wins" is forbidden):

    - **`branch_outputs`** (all preserved, no discard): every branch's output
      keyed by its `step_id` — parity with the linear path, which keys every
      step output by `step_id` (a single-winner fold that *dropped* the other
      N-1 branch results would silently lose them).
    - **`aggregate`** (the single synthesized result): a voting fold — branches
      "vote" with their canonical-JSON-serialized output; the winner is the
      most-voted output; ties break to the LOWEST branch-index. With
      all-distinct outputs every vote is 1 → tie → branch 0 wins (the
      deterministic floor; pinned by an explicit all-distinct test as
      *intended*, not accidental).
    """
    sorted_outputs = sorted(branch_outputs, key=lambda t: t[0])
    # Tally votes by canonical-JSON of each output, inserting in branch-index
    # order so a count tie resolves to the FIRST-inserted (lowest branch-index)
    # key — `max` is stable on first-seen among equal keys, and dicts preserve
    # insertion order.
    vote_counts: dict[str, int] = {}
    representative: dict[str, Mapping[str, Any]] = {}
    for _branch_index, _step_id, output in sorted_outputs:
        canon = json.dumps(output, sort_keys=True, default=str)
        if canon not in vote_counts:
            vote_counts[canon] = 0
            representative[canon] = output
        vote_counts[canon] += 1
    winning_canon = max(vote_counts, key=lambda k: vote_counts[k])
    return {
        "branch_outputs": {step_id: dict(output) for _bi, step_id, output in sorted_outputs},
        "aggregate": dict(representative[winning_canon]),
    }


def _writer_ran_a_step(writer: BufferingLedgerWriter) -> bool:
    """True iff the branch buffered ≥1 **STEP** entry (an `EntryPayload` whose
    `branch_metadata.terminal_status is None`) — i.e. its dispatch actually ran.

    A branch with ONLY a terminal entry (e.g. a CASCADE_CANCEL `cancelled`
    disposition written by the post-barrier empty-buffer scan for a
    not-yet-dispatched worker) did NOT run a step; counting it as a ran-step
    would inflate `workflow.step_count` + emit a spurious `STEP_BOUNDARY`
    ([P2-b]). The step/terminal discriminator is the same
    `branch_metadata.terminal_status` `resume_should_redispatch` reads (§25.15.2
    obl. 7).

    A **per-step override-application** entry (`append_branch_override_ledger_entry`,
    B-NONLINEAR-OVERRIDE-PROVENANCE) carries `branch_metadata=None` — it is NOT a
    step entry. A branch whose dispatch raised/was-cancelled BEFORE buffering a
    step entry can hold ONLY its override entry (buffered at branch-plan time,
    before dispatch); that branch did NOT run a step, so the predicate must
    require a present `branch_metadata` (a step/terminal entry), not merely
    `terminal_status is None` (which a `None` branch_metadata reads as, via the
    old defensive getattr — the bug that would mis-count an override-only writer).
    """
    return any(
        (bm := getattr(payload, "branch_metadata", None)) is not None and bm.terminal_status is None
        for payload, _write_key in writer.buffered_entries
    )


def _writer_has_branch_disposition(writer: BufferingLedgerWriter) -> bool:
    """True iff the branch buffered a **step OR terminal** entry (any entry
    carrying a `branch_metadata`) — i.e. it dispatched, or was classified at a
    dispatch boundary.

    A per-step **override-application** entry (`append_branch_override_ledger_entry`,
    B-NONLINEAR-OVERRIDE-PROVENANCE) carries `branch_metadata=None` and does NOT
    count: a CASCADE_CANCEL worker cancelled BEFORE scheduling its dispatch that
    nonetheless carries its pre-fan-out override entry is still **not-yet-dispatched**
    and MUST receive its `cancelled` terminal (§25.15.2 obl. 4 /
    `resume_should_redispatch`). The pre-arc `entry_count == 0` predicate would now
    miss it (the override makes `entry_count == 1`), so the not-yet-dispatched scan
    keys on the absence of a step/terminal disposition, not on an empty buffer."""
    return any(
        getattr(payload, "branch_metadata", None) is not None
        for payload, _write_key in writer.buffered_entries
    )


def _drain_and_emit_step_boundaries(
    ctx: DriverContext,
    branch_writers: Sequence[BufferingLedgerWriter],
) -> int:
    """Drain all branch buffers (branch-index order) through the single real
    writer, then emit one `STEP_BOUNDARY` per branch that ran a step.

    Single-threaded at the drain BY CONSTRUCTION: all emitter access (this
    drain + the one `WORKFLOW_START`) runs on the driver thread, after the
    barrier (`asyncio.run` over the fan-out has returned) — never concurrently.
    `STEP_BOUNDARY` is therefore NEVER emitted from the `to_thread` branch
    workers (which would also race the non-thread-safe emitter). The per-step
    boundary→append ordering of the linear path
    (§25.3.3.5 then §25.3.3.7) does not apply to a fan-out — the barrier-drain
    is one persist event; emitting the boundaries after the drain is the
    single-threaded analogue (impl-discretion, §25.18).

    Returns the count of branches that ran a step (the `workflow.step_count`
    carrier, C-OD-25) — counted by branches that buffered ≥1 **STEP** entry
    (`branch_metadata.terminal_status is None`), NOT by `entry_count > 0`. A
    fully-run branch buffered a step entry + a terminal entry (so its
    `entry_count` is 2; counted once); a branch whose dispatch raised before
    buffering contributed nothing (`entry_count` 0; not counted). The
    [P2-a/P2-b] distinction: a CASCADE_CANCEL worker cancelled BEFORE dispatch
    buffers ONLY a terminal `cancelled` entry (`entry_count` 1, NO step entry) —
    it did NOT run a step, so it must NOT inflate `workflow.step_count` or emit a
    `STEP_BOUNDARY`. (`entry_count > 0` would mis-count it; the non-cascade
    strategies — PARALLELIZATION / EVALUATOR_OPTIMIZER — never buffer a
    terminal-only branch, so the predicate is a no-op for them.)
    """
    ran = sum(1 for writer in branch_writers if _writer_ran_a_step(writer))
    drain_branch_buffers(ctx.ledger_writer, branch_writers)
    for _ in range(ran):
        ctx.lifecycle_emitter.emit(WorkflowEventClass.STEP_BOUNDARY)
    return ran


def _run_fanout_to_completion[T](fanout: Coroutine[Any, Any, T], *, max_workers: int) -> T:
    """Drive the async fan-out from the sync driver thread — WITHOUT `asyncio.run`.

    `asyncio.run` JOINS its default `ThreadPoolExecutor` at shutdown
    (`loop.shutdown_default_executor()` waits for every worker). A branch's SYNC
    dispatch runs off-loop via `asyncio.to_thread`, and CPython cannot kill a
    running thread — so a genuinely-wedged branch would block the parent's return
    at executor shutdown EVEN AFTER `bounded_barrier` raised the §25.11 wall-clock
    deadline, re-defeating the cap (the parent hangs instead of returning
    `parallelization-barrier-deadline-exceeded`).

    This drives the fan-out on a dedicated loop + a dedicated executor (sized to
    the fan-out so all branches run concurrently). On a CLEAN return every branch
    thread is idle/done → `shutdown(wait=True)` reclaims them (no thread leak). On
    ANY exception — the barrier deadline OR a branch failure — the executor is
    abandoned (`shutdown(wait=False)`) so a wedged branch thread NEVER blocks the
    parent's return; the orphaned thread runs to completion in the background.

    Honest residual (a CPython limit the spec routes to §25.15, NOT a U-CP-86
    defect): the orphaned thread is unkillable, so the HARD in-flight EFFECT
    cut-off (cancelling a running dispatch, classifying it `timed_out`) is §25.15
    cascade scope — U-CP-85's `cascade_cancel_barrier` `_deadline_cutoff`
    watchdog — deliberately excluded from U-CP-86 per its dependency set. In
    practice provider SDK per-call timeouts bound a real dispatch well under the
    fan-out backstop.
    """
    loop = asyncio.new_event_loop()
    executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="cp-fanout")
    loop.set_default_executor(executor)
    try:
        result = loop.run_until_complete(fanout)
    except BaseException:
        # Abandon (never join) on BOTH exit reasons — the barrier deadline AND an
        # ordinary branch failure — because either can leave a wedged sibling
        # thread, and joining one would re-defeat U-CP-86's OWN §25.11 obligation
        # ("a stuck branch cannot strand its parent indefinitely"): a branch that
        # fails fast (e.g. t=1s) has already exited `bounded_barrier`'s
        # `asyncio.timeout` window, so a `wait=True` join of a wedged sibling
        # would block PAST the deadline, forever. Parent-bounding is the
        # obligation U-CP-86 owns — honored by always returning. The orphaned
        # sibling's effect DISPOSITION (cancel / record / discriminate
        # terminal_status) is the §25.15.1 cascade_policy semantic — U-CP-85's
        # `cascade_cancel_barrier` + the proceed/pause/cascade-cancel table —
        # deliberately EXCLUDED from U-CP-86 by its dependency set. No
        # silent-uncompensated-effect results: PARALLELIZATION is §10.3-admissible
        # only for RESEARCH / CONTENT_CREATION (non-effectful breadth-search / A-B
        # cells; `topology_pattern.py:73-86`, and not a §11.1 primary), and an
        # effectful step gates BEFORE dispatch inside the dispatcher
        # (C-AS-02 → C-CP-19 → C-CP-16; only the ledger WRITE is buffered, never
        # the gate), so an orphaned sibling either never dispatched (no effect) or
        # already passed its operator gate.
        executor.shutdown(wait=False)
        raise
    else:
        # Clean fan-out: every branch thread is idle/done → reclaim them.
        executor.shutdown(wait=True)
        return result
    finally:
        loop.close()


class _FanOutStoreCorruptError(Exception):
    """A fan-out branch journal is PRESENT but UNREADABLE on crash-resume.

    The fail-closed signal (`[[durable-recovery-presence-validity-scope]]`): a branch
    file that exists but yields no parseable record is corruption / tamper, NOT a
    genuinely-incomplete branch — re-dispatching it would mask the corruption and
    re-fire its possibly-landed effect. `_execute_workflow_body` turns this into a
    FAILED RunResult rather than a fresh re-dispatch.
    """


class _FanOutStoreTimeoutAmbiguousError(Exception):
    """A recovered fan-out branch reached a TIMED_OUT terminal disposition.

    Irreducibly ambiguous: a deadline-cut in-flight dispatch may or may not have landed
    its effect (the effect-fence-ambiguous case the linear path needed a dedicated arc to
    resolve). Crash-resume cannot guess across a fan-out crash, so it FAILS CLOSED; the
    operator-resolvable timeout-replay is the registered follow-on. `_execute_workflow_body`
    turns this into a FAILED RunResult with a distinct fail_class.
    """


class _FanOutStoreOrchestratorMaybeRanError(Exception):
    """The ORCHESTRATOR_WORKERS `steps[0]` orchestrator MAYBE-RAN on a crash-resume.

    B-FANOUT-CRASH-RESUME-ORCHESTRATOR-DISPATCH (R-FS-1). The orchestrator reserve-before-
    DISPATCH marker is PRESENT (the orchestrator's dispatch BEGAN, its effect may have fired)
    but its terminal-capture record is ABSENT (the crash fell in the orchestrator's
    fire→capture window). Re-dispatching `steps[0]` fresh would risk a double-fire on the
    effect-bearing strict tiers, so the run FAILS CLOSED — the narrow irreducible at-most-once
    ambiguity for the orchestrator, mirroring the per-branch maybe-ran case. Its RESOLUTION (a
    post-dispatch capture distinguishing completed-and-captured from genuinely-ambiguous, then
    suppress-and-continue or a §26.2 pause) is the registered follow-on
    `B-FANOUT-CRASH-RESUME-ORCHESTRATOR-MAYBE-RAN-RESOLUTION`. `_execute_workflow_body` turns
    this into a FAILED RunResult with a distinct fail_class.
    """


def _determine_fanout_resume(
    store: Any,
    run_key: str,
    steps: Sequence[WorkflowStep],
    topology: TopologyPattern,
    timeout_disposition: FanoutTimeoutDisposition = FanoutTimeoutDisposition.FAIL_CLOSED,
) -> FanOutResumeState | PeerFanOutResumeState | None:
    """Reconstruct a fan-out crash-resume state from the durable branch STORE.

    B-FANOUT-OUTPUT-REPLAY (R-FS-1). On a mid-fan-out crash the durable F2 ledger is
    BINARY (branch terminals buffer into per-branch `BufferingLedgerWriter`s and drain
    ATOMICALLY at the barrier per CP §25.12 D1.b), so the STORE is the SOLE
    which-branches-completed authority: `read_branch_outputs` keys = the completed set;
    every other declared ordinal is left re-dispatchable. (The ledger is consulted only
    to know the fan-out is INCOMPLETE — the replay trigger — never for which branches.)

    Returns None (→ fresh run, byte-identical default) when no branch completed. Raises
    `_FanOutStoreCorruptError` (→ FAILED) when a branch file is PRESENT but UNREADABLE
    (fail-closed; never silently re-dispatch a corrupt branch). The recovered `step_id`
    comes from the STORE (CAPTURE-time identity) so the EXISTING strategy resume
    material-diff guard meaningfully fails closed on a changed body.
    """
    records = store.read_branch_records(run_key)
    corrupt = store.present_branch_indexes(run_key) - records.keys()
    if corrupt:
        raise _FanOutStoreCorruptError(
            f"fan-out branch journal(s) present but unreadable: {sorted(corrupt)}"
        )
    # Changed-cardinality fail-closed (out-of-family Codex [P2]): the store records the
    # ORIGINAL fan-out cardinality at capture time, so a manifest redefined with a DIFFERENT
    # branch count between crash + resume (which the per-branch material-diff cannot catch
    # when the surviving prefix still matches) fails closed rather than silently dropping the
    # original in-flight branches. A PRESENT-but-TORN marker (`read_fanout_cardinality` → None
    # because the file is unreadable, but `fanout_cardinality_present` → True) PROVES the run
    # advanced past the cardinality write (fsynced after `record_orchestrator`), so it is
    # CORRUPTION → fail closed, NOT treated as a genuinely-absent (pre-cardinality / pre-arc)
    # marker (`[[durable-recovery-presence-validity-scope]]`: presence ≠ validity — a validity-
    # proxy `read → None` conflates absent + torn, so a torn marker would otherwise silently DROP
    # the original in-flight branches on a changed-cardinality resume). GATED to the NON-
    # orchestrator path: the orchestrator block below ALREADY fails closed on a torn marker via
    # the `_downstream_artifact_present` presence-check (richer "capture was LOST" diagnosis), so
    # this guard targets exactly the PARALLELIZATION/peer gap it lacked — leaving the orchestrator
    # path untouched. A genuinely-absent marker (None + not present) → no check (unchanged).
    _recorded_cardinality = store.read_fanout_cardinality(run_key)
    if _recorded_cardinality is None:
        if store.fanout_cardinality_present(run_key) and not store.orchestrator_dispatched(run_key):
            raise _FanOutStoreCorruptError(
                "fan-out crash-resume cardinality marker present but unreadable (torn) — the "
                "run advanced past the cardinality write; fail closed rather than dropping the "
                "original in-flight branches"
            )
    elif _recorded_cardinality != len(steps):
        raise _FanOutStoreCorruptError(
            f"fan-out crash-resume cardinality mismatch: store captured a {_recorded_cardinality}-"
            f"branch fan-out, resume supplied {len(steps)} (changed body) — fail closed"
        )
    # Disposition recovery (the at-most-once class closer — an output-only store made every
    # non-clean-success disposition invisible). A COMPLETED branch with NO output (ran-and-
    # errored; its effect LANDED) is recovered as TERMINAL — not re-dispatched (the seed loop
    # folds only output-bearing branches). A TIMED_OUT branch is a deadline-cut in-flight
    # dispatch (may or may not have landed) — resolved by the operator-set
    # `fanout_timeout_disposition` per B-FANOUT-CRASH-RESUME-TIMEOUT-REPLAY (CP spec v1.63 §1).
    timed_out = sorted(bi for bi, (_s, status, _o) in records.items() if status == "timed_out")
    # The timed_out branches to EXCLUDE from the recovered tuple so the existing crash-resume
    # re-dispatch path re-runs them (RE_DISPATCH); empty for FAIL_CLOSED (raises) +
    # RECOVER_AS_TERMINAL (they flow in as `completed`-no-output degraded non-contributors).
    _timeout_recover_excluded: set[int] = set()
    if timed_out:
        if timeout_disposition is FanoutTimeoutDisposition.FAIL_CLOSED:
            # Default — v1.55 §1 byte-identical: refuse recovery (the effect may have landed).
            raise _FanOutStoreTimeoutAmbiguousError(
                f"fan-out branch(es) {timed_out} timed out (deadline-cut in-flight dispatch — "
                f"may or may not have landed); crash-resume fails closed "
                f"(fanout_timeout_disposition=FAIL_CLOSED)"
            )
        if timeout_disposition is FanoutTimeoutDisposition.RE_DISPATCH:
            # Re-run the re-fire-safe deadline-cut branches fresh; an effect-bearing (or
            # un-recorded-marker-kind) one fails closed — its effect may have landed, so
            # re-dispatch would double-fire (at-most-once is the GATE, not operator-overridable).
            # Keyed on the v1.62 dispatch-time-kind marker (`dispatched_branch_kinds`) — NOT the
            # resumed manifest (the changed-manifest at-most-once guard). Excluded branches drop
            # from `branches` → the existing re-dispatch path (the provably-not-run / re-fire-safe-
            # maybe-ran machinery) re-runs them. `_branch_count` is the worker/branch count
            # (orchestrator `steps[0]` excluded for the orchestrator topologies — the marker is
            # keyed by branch ordinal, matching the cardinality-only `_card_branch_count`).
            _branch_count = len(steps) - (
                1
                if topology
                in {
                    TopologyPattern.ORCHESTRATOR_WORKERS,
                    TopologyPattern.HIERARCHICAL_DELEGATION,
                }
                else 0
            )
            _timeout_unsafe = _refire_unsafe_branch_indices(
                set(timed_out), store.dispatched_branch_kinds(run_key), _branch_count
            )
            if _timeout_unsafe:
                raise _FanOutStoreTimeoutAmbiguousError(
                    f"fan-out branch(es) {sorted(_timeout_unsafe)} timed out under "
                    f"fanout_timeout_disposition=RE_DISPATCH but are RE-FIRE-UNSAFE (an effect-"
                    f"bearing dispatch-time kind — TOOL_STEP / SUB_AGENT_DISPATCH / "
                    f"MANAGED_AGENTS — an out-of-range ordinal, or an un-recorded marker kind); "
                    f"their effect may have landed, so re-dispatch would double-fire. Fail closed "
                    f"(at-most-once is not operator-overridable)"
                )
            _timeout_recover_excluded = set(timed_out)
        # RECOVER_AS_TERMINAL → fall through: the timed_out branches flow into `branches` below
        # as their TRUE `timed_out`-no-output disposition (never folded into the aggregate,
        # never re-dispatched); `cascade_policy` then governs the degraded reaction. `output is
        # None` ⟺ ran-and-errored OR recovered-deadline-cut — both degraded, treated identically
        # by the cascade reconciliation EXCEPT the PAUSE-reconstruct re-establish gate, which keys
        # on `terminal_status == "completed"` (a genuine branch FAILURE = a pause trigger) so a
        # RECOVER_AS_TERMINAL `timed_out` branch finalizes PARTIAL, never a spurious re-established
        # PAUSE (CP spec v1.68 §1; out-of-family Codex [P2]). Preserving `_status` is otherwise
        # inert — every other consumer keys on `output is None`, not `terminal_status`.
    branches = tuple(
        FanOutBranchResumeState(
            branch_index=branch_index,
            step_id=step_id,
            terminal_status=_status,
            output=output,
        )
        for branch_index, (step_id, _status, output) in sorted(records.items())
        if branch_index not in _timeout_recover_excluded
    )
    if topology is TopologyPattern.PARALLELIZATION:
        # Peer fan-out: NO orchestrator `steps[0]`. A captured orchestrator journal here
        # means the crashed run was an ORCHESTRATOR / HIERARCHICAL run resumed under a
        # CHANGED topology (the `run_idempotency_key` does NOT bind topology) → fail closed
        # rather than reinterpret worker records as peer branches or drop a captured
        # orchestrator effect (out-of-family Codex [P2]). B-FANOUT-CRASH-RESUME-ORCHESTRATOR-
        # DISPATCH widens this to the orchestrator reserve-before-DISPATCH marker: a crashed
        # orchestrator that dispatched but never captured its output leaves no orchestrator
        # RECORD, only the marker — a PARALLELIZATION resume must still fail closed on it
        # (a peer fan-out never writes the marker, so this only fires on a genuine
        # changed-topology resume of a maybe-ran orchestrator).
        if store.orchestrator_present(run_key) or store.orchestrator_dispatched(run_key):
            raise _FanOutStoreCorruptError(
                "fan-out crash-resume topology mismatch: a PARALLELIZATION manifest resumed a "
                "run whose store holds an orchestrator record/marker — fail closed (changed "
                "topology)"
            )
        if not branches:
            return None  # no completed branch → fresh run, byte-identical default
        return PeerFanOutResumeState(branches=branches, branch_count=len(steps))
    # ORCHESTRATOR_WORKERS / HIERARCHICAL_DELEGATION: the orchestrator (`steps[0]`) is
    # dispatched FIRST (sequentially), BEFORE any worker. So a crash after the orchestrator
    # output is captured but BEFORE any worker completes must STILL recover the
    # orchestrator — else re-dispatching `steps[0]` double-fires its effect, which the
    # sidecar already captured (out-of-family Codex [P1]). The orchestrator record is
    # therefore the recovery authority for these topologies, independent of the branch set.
    orchestrator = store.read_orchestrator_output(run_key)
    if orchestrator is None:
        if store.orchestrator_present(run_key):
            # Orchestrator file present-but-unreadable → fail closed (corruption / tamper),
            # whether or not any worker completed.
            raise _FanOutStoreCorruptError(
                "fan-out orchestrator output present but unreadable (corrupt store)"
            )
        if not branches:
            # B-FANOUT-CRASH-RESUME-ORCHESTRATOR-DISPATCH (R-FS-1) — nothing captured
            # (orchestrator output absent + no worker). PRE-arc this ALWAYS returned None
            # (fresh re-run → re-dispatch `steps[0]` → a double-fire on the strict tiers if
            # the orchestrator had already fired). The orchestrator reserve-before-DISPATCH
            # marker is the fix, and its PRESENCE ALONE is the fail-closed signal (presence-only,
            # like `present_dispatched_indexes`): the marker is a NEW file written ONLY by this
            # arc's instrumented code, strictly BEFORE the orchestrator dispatch, so —
            #   • marker PRESENT ⟹ the orchestrator BEGAN dispatch (its effect may have fired)
            #     but its output was never captured → MAYBE-RAN → fail closed. This holds even
            #     for an orphaned marker WITHOUT the dispatch-instrumented stamp: the stamp is
            #     written before the marker, so a marker-without-stamp is an INCONSISTENT store
            #     (corruption / tamper / partial loss), NOT a legitimate pre-arc journal (which
            #     carries no orchestrator marker at all) — failing it closed is the conservative
            #     at-most-once reading, never a fresh re-dispatch (out-of-family Codex [P2]).
            #   • marker ABSENT ⟹ provably-not-run (no dispatch began) OR a pre-arc journal
            #     (no marker was ever written) → the unchanged fresh re-run. Both safe.
            # The cross-version guard is therefore INHERENT in the new-file marker — no stamp
            # gate needed (`[[durable-recovery-presence-validity-scope]]`).
            if store.orchestrator_dispatched(run_key):
                # B-FANOUT-CRASH-RESUME-ORCHESTRATOR-MAYBE-RAN-RESOLUTION (R-FS-1, CP spec v1.64 §1)
                # — DEFAULT-DENY re-fire-safe relaxation (out-of-family Codex R1-R4: four corruption
                # edges proved an enumerate-then-fail-closed DENYLIST inherently incomplete; this is
                # inverted to a default-deny ALLOWLIST tied to the `_execute_orchestrator_workers`
                # writer artifact list). The orchestrator runs FIRST + sequentially: its dispatch
                # marker + the dispatch-instrumented stamp are the ONLY durable artifacts in the
                # true pre-everything maybe-ran window. EVERY artifact the writer emits AFTER the
                # orchestrator marker — the orchestrator capture (already excluded above:
                # `orchestrator is None ∧ not orchestrator_present`), the cardinality marker
                # (fsynced after record_orchestrator; R2 readable + R3 torn — presence-only), a
                # worker dispatch marker (R4 — written only after the orchestrator+cardinality
                # phase), a worker branch capture (readable OR corrupt), a post-join synthesis
                # capture (written last) — PROVES the run advanced past the orchestrator phase, so
                # an absent orchestrator capture here is a LOST capture (corruption), NOT the
                # fire→capture window. Re-running the WHOLE fan-out fresh is safe ONLY in the
                # pristine window (`[[durable-recovery-presence-validity-scope]]`: presence-only
                # checks; the equivalence is "re-fire-safe-maybe-ran ≡ provably-not-run EXCEPT the
                # orchestrator marker" — assert-absent everything the no-marker fresh path assumes).
                _downstream_artifact_present = (
                    store.fanout_cardinality_present(run_key)
                    or bool(store.present_dispatched_indexes(run_key))
                    or bool(store.present_branch_indexes(run_key))
                    or store.synthesis_present(run_key)
                )
                _orch_kind = store.orchestrator_dispatched_kind(run_key)
                if (
                    not _downstream_artifact_present
                    and (
                        store.dispatch_instrumented(run_key)
                        or _orchestrator_dispatched_proceed_unstamped(store, run_key)
                    )
                    and _orch_kind in _FANOUT_MAYBE_RAN_REFIRE_SAFE_KIND_VALUES
                ):
                    # PRISTINE window + a re-fire-safe DISPATCH-TIME kind (DECLARATIVE_STEP /
                    # INFERENCE_STEP — no external effect; the common LLM-orchestrator shape; keyed
                    # on the MARKER, not the resumed manifest — the changed-manifest guard) → re-run
                    # the whole fan-out fresh (first-and-only; nothing downstream to double-fire),
                    # but ONLY with a provenance guard: the strict-tier dispatch-instrumented stamp
                    # OR the PROCEED-origin unstamped marker bit. A random orphaned unstamped marker
                    # remains corruption and fails closed below.
                    return None
                _resumed_orch_kind = steps[0].step_kind.value if steps else None
                _resumed_orch_step_id = str(steps[0].step_id) if steps else None
                _orch_marker_step_id = store.orchestrator_dispatched_step_id(run_key)
                if (
                    not _downstream_artifact_present
                    and store.dispatch_instrumented(run_key)
                    and _orch_kind in _FANOUT_MAYBE_RAN_FENCE_RECOVERABLE_KIND_VALUES
                    and _resumed_orch_kind in _FANOUT_MAYBE_RAN_FENCE_RECOVERABLE_KIND_VALUES
                    and _orch_kind == _resumed_orch_kind
                    # Same-step_id guard (out-of-family Codex [P1]): the runtime effect-fence key
                    # INCLUDES step_id, so a maybe-ran orchestrator re-supplied at steps[0] with the
                    # same kind but a CHANGED step_id (rename / reorder) would re-dispatch into a
                    # DIFFERENT fence key, miss the held claim, and double-fire the original effect.
                    # The re-fire-safe leg above does NOT need this (no external effect to
                    # double-fire); the fence-recoverable leg DOES. Marker step_id missing →
                    # None → mismatch → fail closed.
                    and _orch_marker_step_id is not None
                    and _orch_marker_step_id == _resumed_orch_step_id
                ):
                    # B-FANOUT-CRASH-RESUME-ORCHESTRATOR-MAYBE-RAN-EFFECT-BEARING (R-FS-1) —
                    # PRISTINE window + dispatch-instrumented stamp + a FENCE-RECOVERABLE
                    # DISPATCH-TIME kind (TOOL_STEP / MANAGED_AGENTS — effect-bearing, but its
                    # effect is FENCED at the runtime sink, C-RT-31 §14.22). UNLIKE the
                    # re-fire-safe set above (which has no effect), this orchestrator's effect may
                    # have landed — so re-running fresh is at-most-once ONLY because the re-dispatch
                    # re-reaches the auto-active fence, which SPLITS: suppress-and-continue (the
                    # fence captured the output ⟹ effect completed), ambiguous-PAUSE (the
                    # orchestrator analogue of the linear B-EFFECT-FENCE-HITL-ROUTE — composed to a
                    # §26.2 pause at the dispatch site), or fresh-fire (claim ABSENT ⟹ the prior
                    # attempt crashed before the reserve ⟹ did not fire). The same-kind guard
                    # (marker kind == resumed kind, both recoverable) is the changed-kind guard
                    # inherited from the worker `_fence_unrecoverable_maybe_ran_indices`: a
                    # cross-kind swap or a change to a non-recoverable kind would re-dispatch into a
                    # DIFFERENT sink (or none) and silently abandon the original effect's ambiguity
                    # → fail closed below. The fence is AUTO-ACTIVE here (the fan-out crash-resume
                    # engine classes are a subset of the durable-auto-fence set; the orchestrator
                    # context inherits `run_engine_class`).
                    return None
                if (
                    not _downstream_artifact_present
                    and store.dispatch_instrumented(run_key)
                    and _orch_kind == StepKind.SUB_AGENT_DISPATCH.value
                    and _resumed_orch_kind == StepKind.SUB_AGENT_DISPATCH.value
                    # Same-step_id guard (manifest-stability parity with the worker SUB_AGENT path,
                    # #756): a maybe-ran orchestrator re-supplied at steps[0] as a RENAMED/REORDERED
                    # step is a DIFFERENT logical orchestrator → fail closed. Marker step_id missing
                    # (torn / pre-arc) → None → mismatch → fail closed.
                    and _orch_marker_step_id is not None
                    and _orch_marker_step_id == _resumed_orch_step_id
                    # The [P1-b] dual gate (#746): the orchestrator's child must be recoverable
                    # BOTH at dispatch (the marker — durable child records exist to auto-resume
                    # from) AND in the RESUMED manifest (the re-dispatch goes through the child's
                    # replay store/fence path, not a fresh non-recoverable run). A child edited
                    # recoverable→non-recoverable between dispatch + resume is in the dispatch set
                    # but NOT the resumed set → fail closed (else the re-dispatch runs the
                    # now-non-recoverable child fresh → double-fire / suffix-only corruption).
                    and _orchestrator_subagent_recoverable(store, run_key)
                    and bool(steps)
                    and _subagent_child_recoverable(steps[0]) is True
                    # SAME-ENGINE guard (out-of-family Codex [P1], …-RECONCILER-CHILD arc): the
                    # marker's dispatch-time child engine class == the resumed steps[0] child
                    # engine class. Load-bearing once >1 durable engine class is recoverable: a
                    # same-step_id RECONCILER→SAVE_POINT swap passes BOTH dual-gate booleans above,
                    # and `compose_child_run_id_seed` is engine-class-agnostic → the swap
                    # re-dispatches the child against the SAME durable store through a DIFFERENT
                    # recovery mechanism, bypassing the RECONCILER CAS at-most-once protection.
                    # Marker engine missing → None → mismatch → fail closed.
                    and _orchestrator_dispatched_child_engine(store, run_key) is not None
                    and _orchestrator_dispatched_child_engine(store, run_key)
                    == _subagent_child_engine_class(steps[0])
                ):
                    # B-FANOUT-CRASH-RESUME-ORCHESTRATOR-MAYBE-RAN-SUBAGENT (R-FS-1) — pristine
                    # window + dispatch-instrumented stamp + a SUB_AGENT_DISPATCH orchestrator whose
                    # recoverable child is re-dispatch-recoverable. UNLIKE the
                    # fence-recoverable kinds (the orchestrator's OWN dispatch reaches a fence), a
                    # sub-agent orchestrator's effects live at its CHILD's tool sinks → recovery
                    # is COMPOSITIONAL recursive child crash-resume: re-running the whole fan-out
                    # fresh re-dispatches the orchestrator, whose child auto-resumes from its
                    # durable store under the DETERMINISTIC `child_run_id_seed` (composer-derived
                    # from the orchestrator's stable `orchestrator_idempotency_key`,
                    # `branch_path=None`), with result-faithful `final_state` reconstruction
                    # (B-CHILD-CRASH-RESUME-FINAL-STATE-RECONSTRUCT, v1.75). The whole fan-out
                    # re-runs SAFELY because nothing downstream was dispatched (pristine window: no
                    # worker dispatch marker / branch capture / cardinality marker / synthesis).
                    # The orchestrator analogue of the worker
                    # `_fence_unrecoverable_maybe_ran_indices` SUB_AGENT recovery disjunct. The real
                    # gate is `_subagent_child_recoverable(steps[0]) is True` above — it admits
                    # LINEAR {ESR,WAL,SAVE_POINT,RECONCILER}, fan-out
                    # {ESR,WAL,SAVE_POINT,RECONCILER}, and nested recoverable children; unsupported
                    # topologies or non-durable engines return False → fail closed below.
                    return None
                if _downstream_artifact_present:
                    # A downstream artifact survived but the orchestrator capture is absent → the
                    # run advanced past orchestrator capture → the capture was LOST → corruption.
                    raise _FanOutStoreCorruptError(
                        "fan-out orchestrator dispatch marker present + a downstream artifact "
                        "(cardinality marker / worker dispatch marker / worker branch capture / "
                        "synthesis capture) present but the orchestrator output is absent — the "
                        "run advanced past orchestrator capture, so the capture was LOST (corrupt "
                        "store) — fail closed, never a fresh re-dispatch"
                    )
                # Pristine window but NOT re-fire-safe: an effect-bearing / un-kinded orchestrator
                # (its effect may have landed) OR an un-stamped marker for a stamped-only recovery
                # class. Fail closed maybe-ran.
                raise _FanOutStoreOrchestratorMaybeRanError(
                    "fan-out orchestrator dispatched but its output was never captured (crash "
                    "in the orchestrator fire→capture window, or an orphaned dispatch marker) "
                    "— fail closed (maybe-ran of an effect-bearing / un-kinded orchestrator, or "
                    "a stamped-only recovery class without the dispatch-instrumented stamp; "
                    "re-dispatch would risk a double-fire)"
                )
            return None  # no marker → provably-not-run OR pre-arc journal → fresh run
        # Workers completed but the orchestrator output is ABSENT — an inconsistent store
        # (the orchestrator completes BEFORE any worker dispatches). Fail closed.
        raise _FanOutStoreCorruptError(
            "fan-out orchestrator output absent but workers completed (inconsistent store)"
        )
    # Cardinality-ordering fail-closed (out-of-family Codex [P2]): the orchestrator record is
    # fsynced BEFORE the fan-out cardinality marker, so a crash between them leaves a valid
    # orchestrator record with NO recorded cardinality — and a manifest with a CHANGED worker
    # count would then reuse the old orchestrator output against the new worker set undetected.
    # An orchestrator record without a cardinality marker therefore fails closed.
    if _recorded_cardinality is None:
        raise _FanOutStoreCorruptError(
            "fan-out orchestrator record present but the fan-out cardinality marker is absent "
            "(crash between orchestrator capture and cardinality write) — fail closed (the "
            "worker count cannot be validated against a changed manifest)"
        )
    orchestrator_step_id, orchestrator_output = orchestrator
    return FanOutResumeState(
        orchestrator_output=orchestrator_output,
        orchestrator_step_id=orchestrator_step_id,
        branches=branches,  # possibly empty — orchestrator captured, zero workers completed
        worker_count=len(steps) - 1,
        paused_child_branches=(),
    )


def _execute_parallelization(
    *,
    manifest_entry: WorkflowManifestEntry,
    steps: Sequence[WorkflowStep],
    run_id: str,
    ctx: DriverContext,
    default_model_binding: ModelBinding,
    step_dispatchers: StepDispatcherRegistry,
    run_idempotency_key: str,
    resume_snapshot: PauseSnapshot | None = None,
    crash_fan_out_resume: FanOutResumeState | PeerFanOutResumeState | None = None,
    crash_pause_reconstruct_no_dispatch: bool = False,
    crash_pause_reconstruct_fence_paused: tuple[EffectFencePausedBranchResumeState, ...] = (),
    reconciler_engine_resume_required: bool = False,
    synthesis_step: WorkflowStep | None = None,
) -> tuple[RunResult, int]:
    """Execute the `PARALLELIZATION` fan-out-barrier-aggregate strategy (U-CP-86).

    Each declared `WorkflowStep` is fanned out as one branch (branch_index = its
    ordinal); all branches run concurrently; the barrier holds until every
    branch finishes; the structured outputs fold into one deterministic result.
    Returns `(RunResult, steps_executed)` for the `_execute_workflow_body`
    caller (matching the linear path's tuple).

    `cascade_policy` consumption (§25.15.1, D4 multiplicative tunable) is
    materialized here (R-FS-1 `B-PARALLELIZATION-CASCADE`): a branch failure
    resolves SOLO→proceed (harvest survivors → PARTIAL) / TEAM→pause / MTC→
    cascade-cancel (cancel siblings → FAILED). The U-CP-86 build was happy-path-only
    ("U-CP-85 non-dep"); this is impl-to-cleared-spec (§25.15 + §25.18 anticipate it).

    `pause → PAUSED` resume (R-FS-1 `B-FANOUT-PAUSE-PARALLELIZATION`, CP spec v1.44
    §2): a TEAM-tier branch failure under `pause` (with a bound `pause_resume_protocol`)
    captures a `PeerFanOutResumeState`-bearing `PauseSnapshot` + returns PAUSED;
    `api.resume` re-enters here with `resume_snapshot`, skips the terminal branches
    (recovering their outputs from the snapshot, §25.15.2 obligation 7), and
    re-dispatches the not-yet-dispatched ones. This is the `_execute_orchestrator_
    workers` (U-CP-88 / B-FANOUT-PAUSE) shape applied PARALLELIZATION-shaped (NO
    orchestrator `steps[0]`; every step is a peer branch, indexed over `steps`). When
    no protocol is bound, `pause` fails HONESTLY (`...-pause-resume-protocol-not-bound`)
    — never a false-resumable PAUSED.

    Bypassed linear-only paths (documented scoped-not-forgotten): prefix-replay
    / explicit-pause resume detection, mid-loop drain checks, per-step
    pause-trigger detection, and the per-step validator hook are NOT composed
    here — they compose at later strategy units (U-CP-88 ORCHESTRATOR_WORKERS).
    """
    workflow_id = manifest_entry.workflow_id

    # B-FANOUT-PAUSE-PARALLELIZATION — peer fan-out resume reconstruction state
    # (None on a normal first run). Mirrors `_execute_orchestrator_workers` but
    # PARALLELIZATION-shaped: NO orchestrator `steps[0]`, so the recovered set is
    # keyed over `steps` ordinals directly (every step is a peer branch).
    # B-FANOUT-OUTPUT-REPLAY (R-FS-1) — crash-resume threads the synthetic peer resume
    # state through the SAME `_peer_resume` local the pause path uses (only the snapshot
    # SOURCE differs; skip-terminal + seed-collected below are reused VERBATIM). Never
    # co-set with the pause snapshot — `_execute_workflow_body` computes the crash state
    # only when `resume_snapshot is None`; the assert pins the invariant.
    assert resume_snapshot is None or crash_fan_out_resume is None
    _peer_crash_resume = (
        crash_fan_out_resume if isinstance(crash_fan_out_resume, PeerFanOutResumeState) else None
    )
    _peer_resume = (
        resume_snapshot.peer_fan_out_resume if resume_snapshot is not None else _peer_crash_resume
    )
    _is_resume = _peer_resume is not None
    reconciler_engine_resume_attempted = False
    # branch_index -> recovered terminal disposition (carried forward across
    # repeated resumes so a re-pause snapshot unions prior + this-round terminals).
    _recovered_terminal: dict[int, FanOutBranchResumeState] = (
        {b.branch_index: b for b in _peer_resume.branches} if _peer_resume is not None else {}
    )
    # B-FANOUT-EFFECT-FENCE-BRANCH-PAUSE — branch_index -> the held reserve's idempotency_key
    # for each peer branch whose OWN dispatch raised the effect fence (the PARALLELIZATION
    # analogue of the ORCHESTRATOR_WORKERS recovery dict). NOT skipped on resume: re-dispatched
    # WITH the operator's resolution key-bound to this branch's reserve. "" → no key → re-pause
    # INERT. Rebuilt fresh per round (re-dispatch IS the carry).
    _recovered_effect_fence_paused: dict[int, str] = (
        {b.branch_index: b.idempotency_key for b in _peer_resume.effect_fence_paused_branches}
        if _peer_resume is not None
        else {}
    )
    # B-FANOUT-EFFECT-FENCE-BRANCH-PAUSE / B-FANOUT-EFFECT-FENCE-PER-BRANCH-RESOLUTION — PEEK
    # (NOT consume — the HITL composer one-shot intact) the operator's resume context off the
    # holder. Per paused peer below, `effect_fence_resolution_for(branch_key)` returns that
    # branch's `effect_fence_resolutions` map entry if supplied, else the uniform
    # `effect_fence_resolution` default — so two paused peers can resolve DIFFERENTLY in one
    # resume (SKIP_AS_FIRED vs RE_FIRE; ABORT keeps its run-level-terminal semantic). None holder /
    # no resolution for a branch's key → that paused peer re-pauses INERT (the #701 decline-mirror).
    _ef_resume_ctx: ResumeContext | None = None
    if _recovered_effect_fence_paused:
        _ef_holder = cast(
            "_ResumeContextHolderLike | None",
            getattr(ctx, "resume_context_holder", None),
        )
        _ef_resume_ctx = _ef_holder.peek() if _ef_holder is not None else None

    # B-FANOUT-EFFECT-FENCE-PER-BRANCH-RESOLUTION — run-level ABORT guard (out-of-family Codex
    # [P1]). ABORT stays a RUN-LEVEL terminal decision (v1.65 §1(b)) even per-branch: if the
    # operator ABORTs ANY fence-paused peer, the run WILL fail, so NO sibling continue-resolution
    # (SKIP/RE_FIRE) may FIRE first — a RE_FIRE sibling would otherwise clear+re-fire its effect
    # concurrently in the TaskGroup before the ABORT branch fails the run. When any resolution is
    # ABORT we suppress the non-ABORT siblings' directives below (they re-pause INERT, never fire);
    # the ABORT branch keeps its directive → re-dispatch → EffectFenceAbortedError → terminal
    # FAILED. Per-branch-SCOPED abort (fire survivors anyway) is the registered follow-on
    # B-FANOUT-EFFECT-FENCE-PER-BRANCH-SCOPED-ABORT.
    _any_fence_abort = _ef_resume_ctx is not None and any(
        _ef_resume_ctx.effect_fence_resolution_for(_k) is EffectFenceResolution.ABORT
        for _k in _recovered_effect_fence_paused.values()
    )

    # Empty step sequence → trivially SUCCESS with an empty aggregate (no
    # fan-out; mirrors the linear path's empty-loop SUCCESS).
    if not steps:
        if reconciler_engine_resume_required:
            (
                _reconciler_resume_fail,
                reconciler_engine_resume_attempted,
            ) = _attempt_reconciler_engine_resume_gate(
                ctx=ctx,
                manifest_entry=manifest_entry,
                run_id=run_id,
                step_id="fanout-crash-resume",
            )
            if _reconciler_resume_fail is not None:
                return _reconciler_resume_fail, 0
        result = RunResult(
            workflow_id=workflow_id,
            run_id=run_id,
            status=RunStatus.SUCCESS,
            terminal_step_index=None,
            partial_state=None,
            final_state={"branch_outputs": {}, "aggregate": {}},
            fail_class=None,
        )
        if reconciler_engine_resume_attempted:
            ctx.lifecycle_emitter.emit(WorkflowEventClass.RESUMPTION)
            _record_reconciler_fanout_resume_finalized(ctx, manifest_entry, run_idempotency_key)
        return result, 0

    # B-FANOUT-PAUSE-PARALLELIZATION — material-diff guard on resume: the re-supplied
    # workflow's branch count MUST match the count captured at pause, and each
    # recovered branch ordinal MUST still resolve to the same `step_id` (identity,
    # not just count) — else the recovered outputs no longer map to these steps (a
    # changed body) → fail closed rather than re-dispatch / mis-attribute a recovered
    # output. NO orchestrator-identity check (PARALLELIZATION has no `steps[0]`).
    if _peer_resume is not None:

        def _resume_body_mismatch() -> str | None:
            if len(steps) != _peer_resume.branch_count:
                return (
                    f"branch-count-mismatch: snapshot captured "
                    f"{_peer_resume.branch_count} branches, resume supplied {len(steps)}"
                )
            seen: set[int] = set()
            for b in _peer_resume.branches:
                if not (0 <= b.branch_index < len(steps)):
                    return f"branch-index-out-of-range: {b.branch_index} ∉ [0, {len(steps)})"
                if b.branch_index in seen:
                    return f"duplicate-branch-index: {b.branch_index}"
                seen.add(b.branch_index)
                if str(steps[b.branch_index].step_id) != b.step_id:
                    return (
                        f"branch-identity-mismatch at {b.branch_index}: snapshot "
                        f"step_id={b.step_id!r}, resume step_id="
                        f"{str(steps[b.branch_index].step_id)!r}"
                    )
            # B-FANOUT-EFFECT-FENCE-BRANCH-PAUSE — effect-fence-paused peers: same BOUNDS +
            # IDENTITY guard, PLUS no-overlap with the terminal `branches` (`seen`) — a
            # fence-paused ordinal is the disjoint SECOND disposition (PARALLELIZATION has no
            # paused-child), so any ordinal listed as both terminal AND fence-paused is corrupt.
            for ef in _peer_resume.effect_fence_paused_branches:
                if not (0 <= ef.branch_index < len(steps)):
                    return (
                        f"effect-fence-paused-index-out-of-range: {ef.branch_index} "
                        f"∉ [0, {len(steps)})"
                    )
                if ef.branch_index in seen:
                    return f"effect-fence-paused-overlaps-terminal-or-duplicate: {ef.branch_index}"
                seen.add(ef.branch_index)
                if str(steps[ef.branch_index].step_id) != ef.step_id:
                    return (
                        f"effect-fence-paused-identity-mismatch at {ef.branch_index}: snapshot "
                        f"step_id={ef.step_id!r}, resume step_id="
                        f"{str(steps[ef.branch_index].step_id)!r}"
                    )
                # B-FANOUT-EFFECT-FENCE-BRANCH-PAUSE — the resumed branch's kind MUST match the
                # CAPTURED kind (always `tool-step` in production — only a TOOL_STEP's dispatch
                # reaches the runtime tool fence, the source of the ambiguous-pause). If the
                # operator kept the step_id but changed the kind, threading the
                # `EffectFenceResolution` would reach NO fence → the original ambiguous tool effect
                # silently abandoned. Fail closed — the live-pause analogue of the §2 crash-resume
                # changed-kind guard (out-of-family Codex [P1] R2).
                if str(steps[ef.branch_index].step_kind.value) != ef.step_kind:
                    return (
                        f"effect-fence-paused-kind-changed at {ef.branch_index}: snapshot kind="
                        f"{ef.step_kind!r}, resume kind="
                        f"{str(steps[ef.branch_index].step_kind.value)!r} — the resolution would "
                        "not reach the fence (only the captured-kind dispatch does)"
                    )
            return None

        _mismatch = _resume_body_mismatch()
        if _mismatch is not None:
            return RunResult(
                workflow_id=workflow_id,
                run_id=run_id,
                status=RunStatus.FAILED,
                terminal_step_index=None,
                partial_state=None,
                final_state=None,
                fail_class=f"parallelization-resume-body-mismatch: {_mismatch}",
            ), 0

    # Resolve cascade policy before the RECONCILER CAS gate so invalid effect-fence pause resumes
    # under PROCEED fail without consuming the one-shot engine resume claim.
    cascade_policy = d4_tunable(
        lookup_cell(manifest_entry.workload_class, manifest_entry.engine_class),
        manifest_entry.persona_tier,
    ).cascade_policy
    if _recovered_effect_fence_paused and cascade_policy is CascadePolicy.PROCEED:
        return RunResult(
            workflow_id=workflow_id,
            run_id=run_id,
            status=RunStatus.FAILED,
            terminal_step_index=None,
            partial_state=None,
            final_state=None,
            fail_class="parallelization-effect-fence-resume-requires-strict-tier",
        ), 0

    # One fan-out parent context (the fan-out point); each branch descends a
    # child via compose_branch_child_context (U-CP-81). The MVP-default seed
    # fields mirror the linear per-step composition site.
    fanout_parent = StepExecutionContext(
        workflow_id=workflow_id,
        parent_action_id=_parallelization_fanout_action_id(workflow_id),
        parent_gate_level=resolve_parent_gate_level(manifest_entry),
        # B-HITL-PLACEMENT-PER-STEP-PRODUCER — branch children inherit this via
        # compose_branch_child_context's model_copy (covers fan-out workers).
        hitl_placements=manifest_entry.hitl_placements,
        # B-EFFECT-FENCE-DURABLE-AUTO — the RUN engine class (NOT a per-step
        # StepOverride.engine_class) so the tool dispatcher auto-fences durable runs.
        run_engine_class=manifest_entry.engine_class,
        parent_sandbox_tier=SandboxTier.TIER_1_PROCESS,
        parent_actor=ctx.ledger_writer.actor,
        parent_entry_hash="",
        parent_idempotency_key=_compute_step_idempotency_key(run_idempotency_key, 0),
        tenant_id=ctx.tenant_id,
        step_index=0,
    )

    # R-003 active-workflow-context sidecar (resolved once per fan-out; the same
    # resolver the linear `_append_step_ledger_entry` reads). None when no
    # resolver is bound (operator opt-out / test ctx).
    _resolver = getattr(ctx, "procedural_tier_snapshot_resolver", None)
    snapshot_ref = _resolver() if _resolver is not None else None

    # Buffer-time placeholder timestamp for every branch entry; the authoritative
    # append timestamp is assigned at the drain (`drain_branch_buffers` re-stamps
    # to one drain-moment value — the IS-monotonicity realization, see the module
    # timestamp-discipline note above `append_branch_step_ledger_entry`).
    fanout_timestamp = datetime.now(UTC)

    # Per-branch plan: (branch_index, step, child context, buffering writer,
    # resolved binding). The fan-out cardinality cap (C-CP-10 §10.3 cells) is an
    # ADMISSIBILITY property rejected at workflow-binding (§25.10 Invariant 2) —
    # NOT re-truncated here (silently dropping declared steps beyond a cap would
    # be silent branch loss); the strategy fans out every declared branch.
    branch_plan: list[
        tuple[int, WorkflowStep, StepExecutionContext, BufferingLedgerWriter, StepEffectiveBinding]
    ] = []
    # B-FANOUT-EFFECT-FENCE-PER-BRANCH-SCOPED-ABORT (R-FS-1, CP spec v1.73 §1) — peer ordinals
    # the operator scoped-aborted (`ABORT_BRANCH`) this resume: EXCLUDED from re-dispatch
    # (collected here, processed terminal after the disposition dicts init below). DISJOINT
    # from `branch_plan` (never re-dispatched → at-most-once: the ambiguous effect is never
    # re-fired) and from run-level `ABORT` (which fails the whole run).
    _scoped_abort_ordinals: set[int] = set()
    for branch_index, step in enumerate(steps):
        # B-FANOUT-PAUSE-PARALLELIZATION — on resume, a branch that reached a terminal
        # disposition before the prior `pause` halt is SKIPPED (§25.15.2 obligation 7:
        # a terminal branch MUST NOT be re-dispatched — its effect may have landed).
        # Its recovered output is folded into the aggregate (the seed loop below).
        # Only the not-yet-dispatched (left-re-dispatchable) branches fan out again.
        if branch_index in _recovered_terminal:
            continue
        # B-FANOUT-CRASH-RESUME-PAUSE-RECONSTRUCT-NOT-YET-DISPATCHED (CP spec v1.70 §1) — in the
        # re-pause-without-dispatch mode the dispatcher proved every absent ordinal not-yet-run
        # (instrumented + no dispatch marker). SKIP them all this round → an empty `branch_plan`
        # → the `_crash_pause_reestablish` gate below re-establishes the lost PAUSED state OMITTING
        # them (re-dispatchable on `api.resume`, which is the operator's obl-5 blast-radius gate —
        # NOT crash-resume, which must never auto-fire a not-yet-dispatched effect-bearing branch).
        if crash_pause_reconstruct_no_dispatch:
            continue
        # Resolve the per-step binding FIRST so a per-step ROLE override (CP spec
        # v1.38 §6.1, B4 Slice 4) can take precedence over the parallelization
        # default role when composing the child (precedence per-step > default).
        binding = resolve_step_binding(
            manifest_entry,
            str(step.step_id),
            default_model_binding=default_model_binding,
            persona_tier=manifest_entry.persona_tier,
        )
        # B-FANOUT-EFFECT-FENCE-BRANCH-PAUSE — on resume, a peer whose OWN dispatch raised the
        # effect fence is re-dispatched WITH the operator's resolution key-bound to THIS branch's
        # reserve, threaded on the (hash-inert) `effect_fence_resolution` (the PARALLELIZATION
        # analogue of the ORCHESTRATOR_WORKERS threading). Built ONLY when this ordinal was
        # fence-paused AND the captured key is non-empty AND a resolution was supplied; else None
        # → the fence re-pauses INERT. None for every non-fence peer → byte-identical context.
        # B-FANOUT-EFFECT-FENCE-PER-BRANCH-RESOLUTION — resolve THIS peer's fence key against the
        # operator's resume context: its per-key `effect_fence_resolutions` entry if supplied, else
        # the uniform `effect_fence_resolution` default (so two paused peers can resolve DIFFERENTLY
        # in one resume). None → re-pause INERT (the decline-mirror). Run-level ABORT guard: if ANY
        # paused peer resolves to ABORT, suppress this NON-ABORT peer's continue-resolution so it
        # re-pauses INERT (never fires) before the ABORT fails the run (Codex [P1]).
        _branch_fence_key = _recovered_effect_fence_paused.get(branch_index)
        _branch_resolution = (
            _ef_resume_ctx.effect_fence_resolution_for(_branch_fence_key)
            if (_branch_fence_key and _ef_resume_ctx is not None)
            else None
        )
        # B-FANOUT-EFFECT-FENCE-PER-BRANCH-SCOPED-ABORT (R-FS-1, CP spec v1.73 §1) — the operator
        # scoped the ABORT to THIS peer (`ABORT_BRANCH`): fail just this branch, let the
        # vouched-for siblings (SKIP_AS_FIRED / RE_FIRE) fire. Do NOT re-dispatch (at-most-once:
        # no directive reaches the runtime fence → the ambiguous effect is never re-fired); the
        # post-init block below records it `completed`/no-output terminal + captures it durably +
        # the post-barrier folds survivors per cascade_policy. This interception runs BEFORE the
        # run-level ABORT suppression below so a {ABORT, ABORT_BRANCH} mixed map records the
        # scoped-abort peer DETERMINISTICALLY terminal (the suppression would otherwise null its
        # resolution first → it would re-dispatch into the ABORT race, never recorded scoped-abort,
        # contradicting the never-half-recorded contract). Run-level ABORT still dominates: the
        # ABORT branch keeps its directive → the post-barrier ABORT→FAILED return precedes the
        # scoped-abort fold; an excluded branch never dispatches, so it can never fire ahead of it.
        if _branch_resolution is EffectFenceResolution.ABORT_BRANCH:
            _scoped_abort_ordinals.add(branch_index)
            continue
        # Run-level ABORT guard (Codex [P1]): if ANY paused peer resolves to ABORT, suppress this
        # NON-ABORT (continue: SKIP_AS_FIRED / RE_FIRE) peer's directive so it re-pauses INERT
        # (never fires) before the ABORT fails the run. Keys on `is ABORT` — ABORT_BRANCH (handled
        # above) never reaches here.
        if _any_fence_abort and _branch_resolution is not EffectFenceResolution.ABORT:
            _branch_resolution = None
        _branch_effect_fence_directive = (
            EffectFenceResolutionDirective(
                resolution=_branch_resolution,
                idempotency_key=_branch_fence_key,
            )
            if (_branch_fence_key and _branch_resolution is not None)
            else None
        )
        child = compose_branch_child_context(
            fanout_parent,
            branch_index=branch_index,
            agent_role=binding.agent_role or _DEFAULT_PARALLELIZATION_AGENT_ROLE,
        ).model_copy(
            # B-HITL-PLACEMENT-PER-STEP-OVERRIDE-FOLD (CP spec v1.49 §6.2) — fold this
            # worker's per-step `binding.hitl_placement` override onto the workflow
            # tuple (keyed from manifest_entry, so no sibling/parent leak; the child
            # inherits manifest_entry.hitl_placements from fanout_parent otherwise).
            update={
                "effect_fence_resolution": _branch_effect_fence_directive,
                "hitl_placements": fold_step_hitl_placements(
                    manifest_entry.hitl_placements, binding.hitl_placement
                ),
            }
        )
        writer = BufferingLedgerWriter(actor=ctx.ledger_writer.actor, branch_index=branch_index)
        # B-NONLINEAR-OVERRIDE-PROVENANCE — buffer the per-step override-application
        # entry through the branch's writer (the linear `_execute_workflow_body`
        # site's non-linear counterpart). Buffered HERE on the driver thread (the
        # writer's first op, before the fan-out spawns → no concurrent access; the
        # branch's step entry is buffered later on the loop thread, so the drain
        # serializes override-then-step in branch-index order). Buffering at
        # resolution time mirrors the linear path emitting the override BEFORE
        # dispatch (override is a resolution-time binding fact).
        _buffer_branch_override_if_applied(
            branch_writer=writer,
            workflow_id=workflow_id,
            step=step,
            binding=binding,
            timestamp=fanout_timestamp,
            snapshot_ref=snapshot_ref,
        )
        branch_plan.append((branch_index, step, child, writer, binding))
    branch_writers = [plan[3] for plan in branch_plan]

    # Pre-flight (out-of-family Codex [P2]): resolve every branch's dispatcher up
    # front, single-threaded on the driver thread, so an UNBOUND `StepKind` — a
    # SETUP/config error, NOT a branch dispatch failure — fails the whole run LOUD
    # (FAILED), exactly as the linear path (the `StepKindDispatcherNotBoundError`
    # → FAILED above) + the OLD bounded_barrier did. Without this, the `proceed`
    # harvest (`gather(return_exceptions=True)`) would capture the lookup error as
    # a degradable branch failure → a SILENT `PARTIAL` that drops the branch.
    try:
        branch_dispatchers = {
            bi: step_dispatchers.lookup(step.step_kind)
            for bi, step, _child, _writer, _binding in branch_plan
        }
    except StepKindDispatcherNotBoundError as exc:
        return RunResult(
            workflow_id=workflow_id,
            run_id=run_id,
            status=RunStatus.FAILED,
            terminal_step_index=None,
            partial_state=None,
            final_state=None,
            fail_class=f"parallelization-step-kind-not-bound: {exc}",
        ), 0

    if reconciler_engine_resume_required:
        _synth_replay_validation_fail = _captured_synthesis_replay_validation_failure(
            ctx=ctx,
            manifest_entry=manifest_entry,
            run_idempotency_key=run_idempotency_key,
            synthesis_step=synthesis_step,
            branch_count=len(steps),
            run_id=run_id,
        )
        if _synth_replay_validation_fail is not None:
            return _synth_replay_validation_fail, 0
        (
            _reconciler_resume_fail,
            reconciler_engine_resume_attempted,
        ) = _attempt_reconciler_engine_resume_gate(
            ctx=ctx,
            manifest_entry=manifest_entry,
            run_id=run_id,
            step_id="fanout-crash-resume",
        )
        if _reconciler_resume_fail is not None:
            return _reconciler_resume_fail, 0

    # § 25.3.2 — Emit workflow.start (the fan-out begins). Single-threaded on the
    # driver thread, BEFORE the concurrent branches spawn. B-FANOUT-PAUSE-
    # PARALLELIZATION — on a resume the terminal branches already ran in the original
    # envelope, so this re-entry emits RESUMPTION (mirrors `_execute_orchestrator_
    # workers`), not a second WORKFLOW_START.
    ctx.lifecycle_emitter.emit(
        WorkflowEventClass.RESUMPTION
        if (_is_resume or reconciler_engine_resume_attempted)
        else WorkflowEventClass.WORKFLOW_START
    )

    # B-PARALLELIZATION-CASCADE (R-FS-1) — the on-branch-failure cascade reaction
    # (§25.15.1), resolved above from the manifest's (workload_class, engine_class,
    # persona_tier) via the §11.4 D4 multiplicative tunable (SOLO→proceed /
    # TEAM→pause / MTC→cascade-cancel). U-CP-86 was built happy-path-only
    # ("U-CP-85 non-dep") so a SINGLE branch failure fail-fasted the whole
    # parallel fan-out, even under `proceed` where §25.15.1 mandates PARTIAL with
    # survivors. This materializes the cleared §25.15 consumption for
    # PARALLELIZATION (impl-to-cleared-spec; §25.18 line 169 anticipates it,
    # "implement per strategy ... PARALLELIZATION ... with a cascade-cancel
    # idempotency test"), mirroring `_execute_orchestrator_workers` (U-CP-88)
    # PARALLELIZATION-shaped (every declared step is a PEER branch; NO
    # orchestrator step[0]). The RESUMABLE pause (FanOutResumeState capture +
    # api.resume re-entry) is the registered follow-on `B-FANOUT-PAUSE-
    # PARALLELIZATION`; here `pause` fails HONESTLY (`...-not-yet-materialized`),
    # never a false-resumable PAUSED.
    # branch_index -> (step_id, output) for a cleanly-completed branch — the
    # aggregate source. The cascade-cancel not-yet-dispatched scan reads the writers
    # directly via `_writer_has_branch_disposition`.
    collected: dict[int, tuple[str, Mapping[str, Any]]] = {}
    # B-FANOUT-PAUSE-PARALLELIZATION — branch_index -> terminal disposition
    # ("completed" / "timed_out") for every branch that reached a terminal boundary.
    # Written from the same loop-thread sites as `collected` (single-threaded → safe);
    # read AFTER the barrier to build a `pause` snapshot's `PeerFanOutResumeState.
    # branches` + the resumed-terminal degraded check. Only consumed on the `pause`
    # path (harmlessly populated for proceed / cascade-cancel).
    terminal_dispositions: dict[int, str] = {}
    # B-FANOUT-EFFECT-FENCE-BRANCH-PAUSE — branch_index -> the held reserve's idempotency_key
    # for each peer branch whose OWN dispatch raised the effect fence (the PARALLELIZATION
    # analogue). DISJOINT from `terminal_dispositions` (caught at a different except site); read
    # AFTER the barrier to build `PeerFanOutResumeState.effect_fence_paused_branches`. "" → the
    # captured error carried no key → resume re-pauses INERT.
    effect_fence_paused_dispositions: dict[int, str] = {}
    # B-FANOUT-EFFECT-FENCE-BRANCH-PAUSE — peer ordinals whose re-dispatch on resume raised the
    # runtime fence's `EffectFenceAbortedError` (the operator resolved the pause with ABORT). An
    # ABORT is a TERMINAL decision (NOT a re-pause): the post-barrier forces RunStatus.FAILED,
    # tier-agnostic, BEFORE the pause path — so a fan-out ABORT maps to FAILED exactly as the
    # LINEAR effect-fence ABORT does (out-of-family Codex [P1]: without this an ABORT re-supplied
    # under CascadePolicy.PAUSE fell through the generic branch-failure path → re-pause).
    effect_fence_aborted_dispositions: set[int] = set()

    # B-FANOUT-PAUSE-PARALLELIZATION — seed the recovered terminal branches (from the
    # resume snapshot) into `collected` + `terminal_dispositions` so (a) their outputs
    # fold into the resumed aggregate and (b) a RE-pause snapshot unions the
    # already-terminal set with this round's newly-terminal branches. `step_id` is
    # re-derived from the re-supplied `steps` (it was not carried in the snapshot).
    for _bi, _branch in _recovered_terminal.items():
        terminal_dispositions[_bi] = _branch.terminal_status
        if _branch.output is not None:
            collected[_bi] = (str(steps[_bi].step_id), _branch.output)
        # B-FANOUT-EFFECT-FENCE-SCOPED-ABORT-CRASH-DURABLE (R-FS-1, CP spec v1.74 §1) — a
        # crash-recovered branch the operator scoped-aborted (the durable `scoped_aborted`
        # disposition) reconstructs `_scoped_abort_ordinals` so the post-barrier all-abort
        # guard (`_scoped_abort_ordinals and not collected` → FAILED) reproduces the in-resume
        # scoped-abort fold across a crash, rather than the vacuous PAUSED/PARTIAL the
        # `completed`-keyed reconstruct would yield. On the operator-RESUME (snapshot) path the
        # snapshot carries `completed` and this is inert — that path is already correct (the
        # last resume's resolution ctx repopulates the set + the degraded-terminal fold).
        if _branch.terminal_status == "scoped_aborted":
            _scoped_abort_ordinals.add(_bi)
    # B-FANOUT-OUTPUT-REPLAY — a crash-recovered branch with NO output (ran-and-errored,
    # effect landed) means the ORIGINAL run was DEGRADED; the resumed run must stay PARTIAL
    # rather than upgrade to SUCCESS by omitting the failure (out-of-family Codex [P2]).
    _recovered_degraded = any(b.output is None for b in _recovered_terminal.values())
    # B-FANOUT-OUTPUT-REPLAY — record the capture-time fan-out CARDINALITY once on a fresh
    # run (before any branch dispatches), so a changed-cardinality crash-resume (a manifest
    # redefined with fewer branches) fails closed instead of silently dropping the original
    # in-flight branches (out-of-family Codex [P2]).
    if not _is_resume:
        _cardinality_store = _fanout_replay_store(ctx, manifest_entry)
        if _cardinality_store is not None:
            _cardinality_store.record_fanout_cardinality(run_idempotency_key, len(steps))
            # B-FANOUT-CRASH-RESUME-STRICT-TIER-INCOMPLETE (R-FS-1) — stamp the run
            # dispatch-instrumented (the cross-version guard) on the strict tiers, at fan-out
            # start before any branch dispatches. Worker-branch PROCEED is unchanged here; the
            # PROCEED orchestrator marker is emitted at the orchestrator-dispatch site without
            # stamping the worker-marker trust gate.
            if cascade_policy is not CascadePolicy.PROCEED:
                _cardinality_store.record_dispatch_instrumented(run_idempotency_key)

    # B-FANOUT-OUTPUT-REPLAY — on a CRASH-resume (NOT a pause-resume), re-materialize the
    # recovered branches' LOST ledger entries (out-of-family Codex [P1]): a crash before the
    # barrier drained nothing, so the resumed ledger would omit them + undercount
    # `workflow.step_count`. Dedup-safe; extends the barrier-drain set.
    if _peer_crash_resume is not None:
        for _bi in sorted(_recovered_terminal):
            _r_step = steps[_bi]
            _r_binding = resolve_step_binding(
                manifest_entry,
                str(_r_step.step_id),
                default_model_binding=default_model_binding,
                persona_tier=manifest_entry.persona_tier,
            )
            _r_child = compose_branch_child_context(
                fanout_parent,
                branch_index=_bi,
                agent_role=_r_binding.agent_role or _DEFAULT_PARALLELIZATION_AGENT_ROLE,
            ).model_copy(
                update={
                    "hitl_placements": fold_step_hitl_placements(
                        manifest_entry.hitl_placements, _r_binding.hitl_placement
                    )
                }
            )
            branch_writers.append(
                _rematerialize_recovered_branch_writer(
                    ctx,
                    branch_index=_bi,
                    branch_context=_r_child,
                    run_idempotency_key=run_idempotency_key,
                    timestamp=fanout_timestamp,
                    procedural_tier_snapshot_ref=snapshot_ref,
                    workflow_id=workflow_id,
                    step=_r_step,
                    binding=_r_binding,
                )
            )

    # B-FANOUT-EFFECT-FENCE-PER-BRANCH-SCOPED-ABORT (R-FS-1, CP spec v1.73 §1) — record each
    # scoped-aborted peer as a `completed`/no-output terminal (the IS-hash-bearing
    # `terminal_status` Literal is REUSED, not extended → CP-only, no §5.2 IS-hash change;
    # `completed` is dispatch-boundary-accurate — the ORIGINAL attempt ran, its effect may have
    # landed). Captured durably so a crash mid-resume recovers it terminal-failed (NOT maybe-ran
    # → re-dispatch — `[[durable-recovery-presence-validity-scope]]`). Its writer joins the
    # barrier drain; its ordinal (`_scoped_abort_ordinals`) feeds the post-barrier fold (PARTIAL
    # with survivors; FAILED if NONE) + the run fail_class. NEVER re-dispatched.
    # SKIPPED under PROCEED: an effect-fence resume under PROCEED is rejected fail-closed by the
    # strict-tier guard below (BEFORE any branch dispatch), so recording a durable terminal here
    # would mutate state for a resume that is then rejected → corrupt later strict/crash recovery
    # (fail-closed MUST precede durable writes — `[[durable-recovery-presence-validity-scope]]`;
    # out-of-family Codex [P2]).
    _scoped_abort_to_record = (
        ()
        if cascade_policy is CascadePolicy.PROCEED
        # EXCLUDE already-recovered ordinals (B-FANOUT-EFFECT-FENCE-SCOPED-ABORT-CRASH-DURABLE):
        # on a crash-resume the seed loop reconstructs the recovered scoped-aborts INTO
        # `_scoped_abort_ordinals` (so the fold guards fire), but those branches are ALREADY
        # durable (ledger terminal + store record) — re-recording would append a DUPLICATE
        # ledger terminal. Record only the NEWLY-aborted (this-resume) ordinals; the recovered
        # ones flow into the fold via the combined set. Disjoint on the operator-resume path
        # (snapshot scoped-aborts carry `completed` → never reconstructed here).
        else sorted(_scoped_abort_ordinals - _recovered_terminal.keys())
    )
    for _sa_bi in _scoped_abort_to_record:
        _sa_step = steps[_sa_bi]
        _sa_binding = resolve_step_binding(
            manifest_entry,
            str(_sa_step.step_id),
            default_model_binding=default_model_binding,
            persona_tier=manifest_entry.persona_tier,
        )
        _sa_child = compose_branch_child_context(
            fanout_parent,
            branch_index=_sa_bi,
            agent_role=_sa_binding.agent_role or _DEFAULT_PARALLELIZATION_AGENT_ROLE,
        )
        _sa_writer = BufferingLedgerWriter(actor=ctx.ledger_writer.actor, branch_index=_sa_bi)
        append_branch_terminal_ledger_entry(
            branch_writer=_sa_writer,
            branch_context=_sa_child,
            run_idempotency_key=run_idempotency_key,
            terminal_status="completed",
            timestamp=fanout_timestamp,
            procedural_tier_snapshot_ref=snapshot_ref,
        )
        # B-FANOUT-EFFECT-FENCE-SCOPED-ABORT-CRASH-DURABLE (R-FS-1, CP spec v1.74 §1) — the
        # DURABLE store records this scoped-abort as `scoped_aborted` (distinct from a
        # ran-and-errored `completed`-no-output), so a crash mid-resume reconstructs
        # `_scoped_abort_ordinals` from the recovered terminals (the seed loop above) and
        # reproduces the in-resume all-abort FAILED rather than the vacuous PAUSED/PARTIAL the
        # `completed`-keyed `_crash_pause_reestablish` gate would re-establish. The IS-hash-
        # bearing F2 LEDGER append (above) stays `completed` → no §5.2 IS-hash change; only the
        # runtime store carries the distinguishing value. `terminal_dispositions` (in-resume
        # only; inert for the crash path — `_crash_pause_reestablish` requires a crash-resume
        # state) stays `completed`; on crash-resume it is REBUILT from the store's value.
        _capture_branch_terminal(
            ctx,
            manifest_entry,
            run_idempotency_key=run_idempotency_key,
            branch_index=_sa_bi,
            step_id=str(_sa_step.step_id),
            terminal_status="scoped_aborted",
            output=None,
        )
        terminal_dispositions[_sa_bi] = "completed"
        branch_writers.append(_sa_writer)

    def _record_clean(
        branch_index: int,
        step: WorkflowStep,
        child: StepExecutionContext,
        writer: BufferingLedgerWriter,
        output: Mapping[str, Any],
    ) -> None:
        # A cleanly-completed branch: its per-step entry (causality-only
        # branch_metadata) + a fresh `completed` terminal entry (U-CP-84) +
        # collect the output for the aggregate. Both buffer through the branch's
        # OWN writer on the loop thread; the single barrier drain serializes them
        # in branch-index order (U-CP-82/84).
        append_branch_step_ledger_entry(
            branch_writer=writer,
            branch_context=child,
            run_idempotency_key=run_idempotency_key,
            local_step_index=0,
            timestamp=fanout_timestamp,
            procedural_tier_snapshot_ref=snapshot_ref,
        )
        # B-FANOUT-OUTPUT-REPLAY (R-FS-1) — RESERVE-before-COMMIT: durably capture this
        # branch's output to its own per-branch store file BEFORE the terminal ledger
        # append (the §25.12 D1.b concurrent-fan-out ledger is BINARY — buffered, drained
        # atomically at the barrier — so it holds no mid-fan-out branch set; the store is
        # the SOLE crash-resume which-branches-completed authority). `step_id` captured
        # here is the load-bearing identity the resume material-diff guard fails closed
        # on. No-op unless replay-capable ∧ store-bound (`_fanout_replay_store`).
        _replay_store = _fanout_replay_store(ctx, manifest_entry)
        if _replay_store is not None:
            _replay_store.record_branch(
                run_idempotency_key, branch_index, str(step.step_id), "completed", output
            )
        append_branch_terminal_ledger_entry(
            branch_writer=writer,
            branch_context=child,
            run_idempotency_key=run_idempotency_key,
            terminal_status="completed",
            timestamp=fanout_timestamp,
            procedural_tier_snapshot_ref=snapshot_ref,
        )
        collected[branch_index] = (str(step.step_id), output)
        terminal_dispositions[branch_index] = "completed"  # B-FANOUT-PAUSE-PARALLELIZATION

    def _finalize_reconciler_cas_if_attempted() -> None:
        if reconciler_engine_resume_attempted:
            _record_reconciler_fanout_resume_finalized(ctx, manifest_entry, run_idempotency_key)

    def _finish(
        status: RunStatus,
        *,
        fail_class: str | None,
        salvage: bool,
        pause_snapshot: PauseSnapshot | None = None,
    ) -> tuple[RunResult, int]:
        # B-FANOUT-OUTPUT-REPLAY — a crash-resume that recovered a ran-and-errored branch
        # (terminal, no output) keeps the run DEGRADED: never report SUCCESS while omitting
        # a recovered failure (out-of-family Codex [P2]).
        if status is RunStatus.SUCCESS and _recovered_degraded:
            status, salvage = RunStatus.PARTIAL, True
        # Drain the branch buffers (branch-index order) + emit one STEP_BOUNDARY
        # per persisted-step writer, then fold the collected outputs into one
        # deterministic aggregate (lowest-branch-index tiebreak, §25.12). A
        # salvaged non-SUCCESS run carries the survivors as `partial_state`.
        steps_executed = _drain_and_emit_step_boundaries(ctx, branch_writers)
        # B-POSTJOIN-LLM-SYNTHESIS (CP spec v1.54 §3/§4) — an opt-in terminal
        # POST_JOIN_SYNTHESIS step REPLACES the deterministic fold on SUCCESS,
        # dispatched POST-drain (so its disclosing ledger entry follows the
        # branch entries in terminal order); `None` → the byte-identical fold.
        _synth = _maybe_post_join_synthesis(
            synthesis_step=synthesis_step,
            status=status,
            collected=collected,
            ctx=ctx,
            manifest_entry=manifest_entry,
            step_dispatchers=step_dispatchers,
            default_model_binding=default_model_binding,
            fanout_parent=fanout_parent,
            run_idempotency_key=run_idempotency_key,
            run_id=run_id,
            branch_count=len(steps),
        )
        # A FAILED RunResult ⟺ the synthesis dispatch/append raised → return it
        # directly (no escaping exception post-drain — Codex [P2]).
        if isinstance(_synth, RunResult):
            return _synth, steps_executed
        _finalize_reconciler_cas_if_attempted()
        # A dict ⟺ the synthesis dispatched → count its executed step (Codex [P2];
        # its STEP_BOUNDARY was emitted inside `_maybe_post_join_synthesis`).
        if _synth is not None:
            steps_executed += 1
        # No collected output (e.g. an all-timed-out deadline strike) → the empty
        # aggregate (matching the empty-steps early-return shape); `_aggregate_
        # parallelization([])` would otherwise `max()` an empty vote tally.
        aggregate: dict[str, Any] = (
            _synth
            if _synth is not None
            else (
                _aggregate_parallelization(
                    [(bi, sid, out) for bi, (sid, out) in sorted(collected.items())]
                )
                if collected
                else {"branch_outputs": {}, "aggregate": {}}
            )
        )
        result = RunResult(
            workflow_id=workflow_id,
            run_id=run_id,
            status=status,
            terminal_step_index=None,
            partial_state=aggregate if (salvage and status is not RunStatus.SUCCESS) else None,
            final_state=aggregate if status is RunStatus.SUCCESS else None,
            fail_class=fail_class,
            # B-FANOUT-PAUSE-PARALLELIZATION — PAUSED carries the salvaged aggregate
            # as partial_state (above) + the resumable snapshot.
            pause_snapshot=pause_snapshot,
        )
        return result, steps_executed

    deadline = _DEFAULT_FANOUT_BARRIER_DEADLINE_SECONDS

    # B-FANOUT-EFFECT-FENCE-BRANCH-PAUSE — an effect-fence pause is a STRICT-TIER construct (only
    # PAUSE / CASCADE_CANCEL compose the ambiguous-pause through the barrier). Resuming one under a
    # manifest/persona that now resolves to PROCEED is incoherent: the PROCEED `_proceed_branch`
    # path has no pause/resolution handling, so the re-entered branch's `EffectFenceAbortedError`
    # (or a missing-resolution ambiguous error) would be caught as an ordinary failure → PARTIAL,
    # silently dropping the operator's ABORT / the at-most-once re-pause. Fail closed — the resume
    # requires a strict tier (out-of-family Codex [P2] R3; the tier-change material-diff is allowed
    # otherwise, so this guard is the load-bearing strict-tier requirement).
    if _recovered_effect_fence_paused and cascade_policy is CascadePolicy.PROCEED:
        return _finish(
            RunStatus.FAILED,
            fail_class="parallelization-effect-fence-resume-requires-strict-tier",
            salvage=False,
        )

    # === proceed: branches run to completion → SUCCESS | PARTIAL (degraded) ===
    if cascade_policy is CascadePolicy.PROCEED:

        async def _proceed_branch(
            branch_index: int,
            step: WorkflowStep,
            child: StepExecutionContext,
            writer: BufferingLedgerWriter,
            binding: StepEffectiveBinding,
        ) -> None:
            dispatcher = branch_dispatchers[branch_index]
            try:
                output = await asyncio.to_thread(
                    dispatcher.dispatch, binding, step, step_context=child
                )
            except asyncio.CancelledError:
                # The §25.11 wall-clock deadline cancelled this in-flight branch.
                # Its dispatch was scheduled (the effect may have landed) → record
                # the step entry (obl. 3 — no silent gap) + a `timed_out` terminal,
                # then re-raise to honor the cancellation.
                append_branch_step_ledger_entry(
                    branch_writer=writer,
                    branch_context=child,
                    run_idempotency_key=run_idempotency_key,
                    local_step_index=0,
                    timestamp=fanout_timestamp,
                    procedural_tier_snapshot_ref=snapshot_ref,
                )
                # B-FANOUT-OUTPUT-REPLAY — capture the timed-out DISPOSITION (the effect may
                # have landed; crash-resume FAILS CLOSED on it — never a silent re-dispatch).
                _capture_branch_terminal(
                    ctx,
                    manifest_entry,
                    run_idempotency_key=run_idempotency_key,
                    branch_index=branch_index,
                    step_id=step.step_id,
                    terminal_status="timed_out",
                    output=None,
                )
                append_branch_terminal_ledger_entry(
                    branch_writer=writer,
                    branch_context=child,
                    run_idempotency_key=run_idempotency_key,
                    terminal_status="timed_out",
                    timestamp=fanout_timestamp,
                    procedural_tier_snapshot_ref=snapshot_ref,
                )
                raise
            except Exception:
                # Ran-and-errored → record the step entry (obl. 3) + a `completed`
                # terminal (dispatch-boundary, not step-outcome; the failure lives
                # at the step entry). Contributes nothing to the aggregate;
                # re-raise so the return_exceptions gather marks this branch failed
                # (→ the partial result set → PARTIAL). proceed does NOT cancel
                # siblings.
                append_branch_step_ledger_entry(
                    branch_writer=writer,
                    branch_context=child,
                    run_idempotency_key=run_idempotency_key,
                    local_step_index=0,
                    timestamp=fanout_timestamp,
                    procedural_tier_snapshot_ref=snapshot_ref,
                )
                # B-FANOUT-OUTPUT-REPLAY — a ran-and-errored branch's effect may have LANDED
                # (dispatch-boundary `completed`, no output) → capture the disposition so
                # crash-resume recovers it as TERMINAL (no re-dispatch, no fold), never
                # re-firing a landed effect (the disposition class closer).
                _capture_branch_terminal(
                    ctx,
                    manifest_entry,
                    run_idempotency_key=run_idempotency_key,
                    branch_index=branch_index,
                    step_id=step.step_id,
                    terminal_status="completed",
                    output=None,
                )
                append_branch_terminal_ledger_entry(
                    branch_writer=writer,
                    branch_context=child,
                    run_idempotency_key=run_idempotency_key,
                    terminal_status="completed",
                    timestamp=fanout_timestamp,
                    procedural_tier_snapshot_ref=snapshot_ref,
                )
                raise
            _record_clean(branch_index, step, child, writer, output)

        async def _proceed_fanout() -> list[Any]:
            # `return_exceptions=True`: a failing branch does NOT cancel siblings
            # (the proceed semantic). Bounded by the §25.11 wall-clock deadline.
            async with asyncio.timeout(deadline):
                return await asyncio.gather(
                    *(_proceed_branch(*plan) for plan in branch_plan),
                    return_exceptions=True,
                )

        try:
            results = _run_fanout_to_completion(
                _proceed_fanout(), max_workers=max(1, len(branch_plan))
            )
        except BranchBarrierDeadlineExceededError:
            # A stuck branch hit the deadline; the completed branches buffered
            # their entries → PARTIAL (degraded). proceed does not cancel; the
            # stuck branch is abandoned per `_run_fanout_to_completion`.
            return _finish(RunStatus.PARTIAL, fail_class=None, salvage=True)
        except TimeoutError:
            return _finish(RunStatus.PARTIAL, fail_class=None, salvage=True)
        any_failed = any(isinstance(r, BaseException) for r in results)
        if any_failed:
            return _finish(RunStatus.PARTIAL, fail_class=None, salvage=True)
        return _finish(RunStatus.SUCCESS, fail_class=None, salvage=False)

    # === cascade-cancel | pause: cancel-on-failure (TaskGroup structured cancel) ===
    # Both halt the fan-out on the first branch failure with in-flight effects run
    # to completion (shielded). They differ only in the run-level outcome on a
    # branch failure: cascade-cancel → FAILED (+ the empty-buffer not-yet-dispatched
    # scan records `cancelled`); pause → FAILED + `parallelization-pause-resume-not-
    # yet-materialized` (resumable fan-out pause is the follow-on `B-FANOUT-PAUSE-
    # PARALLELIZATION`; a false-`PAUSED` is foreclosed). The CLEAN (no-failure) path
    # is SUCCESS for both.
    async def _cancel_branch(
        branch_index: int,
        step: WorkflowStep,
        child: StepExecutionContext,
        writer: BufferingLedgerWriter,
        binding: StepEffectiveBinding,
    ) -> None:
        dispatcher = branch_dispatchers[branch_index]
        # B-FANOUT-CRASH-RESUME-STRICT-TIER-INCOMPLETE (R-FS-1) — reserve-before-DISPATCH:
        # durably mark this branch DISPATCHED, fsynced STRICTLY BEFORE the effect can fire
        # (THE named invariant: marker-absent ⟺ effect-not-fired within an instrumented run),
        # so a strict-tier crash-resume re-dispatches only PROVABLY-not-run branches. SYNCHRONOUS
        # (not off-loop) so the marker write + the `ensure_future` dispatch are ATOMIC with no
        # yield between them: a cascade-cancel can NOT land after the marker but before the
        # dispatch (which would leave a false-positive marker — and would shift the cancel from
        # the in-flight `completed` boundary to a not-yet-dispatched `cancelled` one). Matches the
        # existing synchronous `_capture_branch_terminal` store I/O. `_cancel_branch` is the
        # strict-tier (PAUSE / CASCADE_CANCEL) path only — PROCEED's `_proceed_branch` writes none.
        _mark_branch_dispatched(
            ctx,
            manifest_entry,
            run_idempotency_key=run_idempotency_key,
            branch_index=branch_index,
            step_id=step.step_id,
            step_kind=step.step_kind,
            step=step,
        )
        # Schedule the (sync) dispatch off-loop; `dispatch_branch_step_shielded`
        # keeps it alive against THIS branch's cancellation so an in-flight effect
        # runs to its own completion (obl. 1), and registers it for the barrier's
        # deadline watchdog (the hard "...or barrier-deadline timeout" cut-off).
        inflight: asyncio.Future[Mapping[str, Any]] = asyncio.ensure_future(
            asyncio.to_thread(dispatcher.dispatch, binding, step, step_context=child)
        )
        try:
            output = await dispatch_branch_step_shielded(inflight)
        except asyncio.CancelledError:
            # B-FANOUT-EFFECT-FENCE-BRANCH-PAUSE (R-FS-1) — this branch was cancelled because a
            # SIBLING raised first, but its OWN in-flight dispatch may have raised the runtime
            # effect fence (the shield suppresses the in-flight exception + re-raises
            # CancelledError, so the fence error lands in `inflight.exception()`). Name-matched
            # (NOT isinstance — a harness-runtime type harness-cp cannot import; `type(None)`
            # is "NoneType" so a deadline-cut / clean cancel is safely skipped). Capture as
            # effect-fence-paused (NOT a terminal branch — else resume skips it + drops the
            # pause): stash the reserve key + re-raise, no step/terminal entry (the disjoint
            # pause disposition, the peer analogue of the ORCHESTRATOR_WORKERS in-flight catch).
            _inflight_exc = (
                inflight.exception() if (inflight.done() and not inflight.cancelled()) else None
            )
            if type(_inflight_exc).__name__ == "EffectFenceAbortedError":
                # B-FANOUT-EFFECT-FENCE-BRANCH-PAUSE — an in-flight branch whose re-dispatch raised
                # the operator's ABORT (symmetric with the own-dispatch catch) → terminal FAILED.
                effect_fence_aborted_dispositions.add(branch_index)
                raise
            if type(_inflight_exc).__name__ == "EffectFenceAmbiguousUncommittedError":
                _inflight_fence_key = getattr(_inflight_exc, "idempotency_key", None)
                effect_fence_paused_dispositions[branch_index] = (
                    _inflight_fence_key if isinstance(_inflight_fence_key, str) else ""
                )
                raise
            # In-flight at cancel-time: the effect ran (shielded to completion) or
            # the deadline cut it. Record the step entry (obl. 3) + the
            # discriminating terminal (obl. 4): `completed` = ran (ran-and-errored
            # is still completed — dispatch-boundary), `timed_out` = the deadline
            # cut the in-flight step. A not-yet-dispatched branch NEVER reaches here
            # (no inflight) — its `cancelled` disposition is the post-barrier scan.
            append_branch_step_ledger_entry(
                branch_writer=writer,
                branch_context=child,
                run_idempotency_key=run_idempotency_key,
                local_step_index=0,
                timestamp=fanout_timestamp,
                procedural_tier_snapshot_ref=snapshot_ref,
            )
            terminal: Literal["completed", "timed_out"] = (
                "timed_out" if (inflight.cancelled() or not inflight.done()) else "completed"
            )
            append_branch_terminal_ledger_entry(
                branch_writer=writer,
                branch_context=child,
                run_idempotency_key=run_idempotency_key,
                terminal_status=terminal,
                timestamp=fanout_timestamp,
                procedural_tier_snapshot_ref=snapshot_ref,
            )
            terminal_dispositions[branch_index] = terminal  # B-FANOUT-PAUSE-PARALLELIZATION
            # A sibling that was IN-FLIGHT when the barrier cancelled this branch
            # ran to completion under the shield (`terminal == "completed"` ⟹
            # `inflight.done()` and not cancelled). Collect its successful OUTPUT
            # so the salvaged aggregate keeps it (a ran-and-errored in-flight,
            # `exception() is not None`, has no output).
            if terminal == "completed" and inflight.exception() is None:
                # B-FANOUT-OUTPUT-REPLAY — a sibling that LANDED under the shield (its effect
                # fired) is captured WITH output (out-of-family Codex [P1]) — else crash-resume
                # RE-DISPATCHES it (double-fire). The buffered terminal append above is not
                # durable until the drain, so this still satisfies RESERVE-before-COMMIT.
                _capture_branch_terminal(
                    ctx,
                    manifest_entry,
                    run_idempotency_key=run_idempotency_key,
                    branch_index=branch_index,
                    step_id=step.step_id,
                    terminal_status="completed",
                    output=inflight.result(),
                )
                collected[branch_index] = (str(step.step_id), inflight.result())
            else:
                # A timed-out (deadline-cut, ambiguous) OR ran-and-errored (effect LANDED, no
                # output) in-flight sibling — capture the DISPOSITION with no output so
                # crash-resume recovers-as-terminal (`completed`) or FAILS CLOSED (`timed_out`),
                # never silently re-dispatching a landed effect (the disposition class closer).
                _capture_branch_terminal(
                    ctx,
                    manifest_entry,
                    run_idempotency_key=run_idempotency_key,
                    branch_index=branch_index,
                    step_id=step.step_id,
                    terminal_status=terminal,
                    output=None,
                )
            raise  # honor the cancellation (the barrier cancelled this branch)
        except Exception as _exc:
            # B-FANOUT-EFFECT-FENCE-BRANCH-PAUSE (R-FS-1) — this branch's OWN dispatch raised the
            # runtime effect fence's `EffectFenceAmbiguousUncommittedError` (C-RT-31 §14.22; the
            # peer analogue of the ORCHESTRATOR_WORKERS catch). Name-matched (harness-cp cannot
            # import harness-runtime). NOT a terminal branch — record NO step/terminal entry (a
            # `completed` terminal would make resume SKIP it + drop the pause): stash the held
            # reserve's idempotency_key keyed by ordinal ("" when absent → resume re-pauses INERT,
            # never an auto-re-fire) so the post-barrier compose lands it in
            # `effect_fence_paused_branches`, then re-raise so the TaskGroup halts the fan-out at
            # the pause boundary (the post-barrier guard fails honestly if no protocol is bound).
            if type(_exc).__name__ == "EffectFenceAbortedError":
                # B-FANOUT-EFFECT-FENCE-BRANCH-PAUSE — the operator resolved THIS branch's fence
                # pause with ABORT on resume → a TERMINAL decision: record the abort ordinal +
                # re-raise so the post-barrier forces RunStatus.FAILED (tier-agnostic), never a
                # re-pause (the LINEAR effect-fence ABORT → FAILED analogue; Codex [P1]).
                effect_fence_aborted_dispositions.add(branch_index)
                raise
            if type(_exc).__name__ == "EffectFenceAmbiguousUncommittedError":
                _fence_key = getattr(_exc, "idempotency_key", None)
                effect_fence_paused_dispositions[branch_index] = (
                    _fence_key if isinstance(_fence_key, str) else ""
                )
                raise
            # THIS branch's own dispatch ERRORED — the failure that triggers the
            # cascade. The effect ran-and-errored → record the step entry (obl. 3)
            # + a `completed` terminal (dispatch-boundary, not step-outcome — the
            # carrier forecloses `failed`). Re-raise so the TaskGroup cascade-
            # cancels the siblings.
            # B-FANOUT-OUTPUT-REPLAY — capture the `completed` disposition (effect may have
            # LANDED, no output) so crash-resume recovers it as terminal, never re-firing it.
            _capture_branch_terminal(
                ctx,
                manifest_entry,
                run_idempotency_key=run_idempotency_key,
                branch_index=branch_index,
                step_id=step.step_id,
                terminal_status="completed",
                output=None,
            )
            append_branch_step_ledger_entry(
                branch_writer=writer,
                branch_context=child,
                run_idempotency_key=run_idempotency_key,
                local_step_index=0,
                timestamp=fanout_timestamp,
                procedural_tier_snapshot_ref=snapshot_ref,
            )
            append_branch_terminal_ledger_entry(
                branch_writer=writer,
                branch_context=child,
                run_idempotency_key=run_idempotency_key,
                terminal_status="completed",
                timestamp=fanout_timestamp,
                procedural_tier_snapshot_ref=snapshot_ref,
            )
            # B-FANOUT-PAUSE-PARALLELIZATION — this failed branch is terminal
            # (`completed`, dispatch-boundary, NO collected output) so a `pause`
            # snapshot records it + obligation 7 does NOT re-dispatch its landed
            # effect on resume.
            terminal_dispositions[branch_index] = "completed"
            raise
        _record_clean(branch_index, step, child, writer, output)

    async def _cancel_fanout() -> list[None]:
        return await cascade_cancel_barrier(
            (_cancel_branch(*plan) for plan in branch_plan), deadline_seconds=deadline
        )

    branch_failed = False
    deadline_struck = False
    try:
        _run_fanout_to_completion(_cancel_fanout(), max_workers=max(1, len(branch_plan)))
    except BranchBarrierDeadlineExceededError:
        # The wall-clock deadline fired with no branch raising (a stuck fan-out) —
        # the §25.11 hard cap. The in-flight branches recorded `timed_out`.
        deadline_struck = True
    except BaseExceptionGroup:
        # A branch raised → the TaskGroup cancelled not-yet-finished siblings.
        # In-flight siblings ran to completion (shielded) + recorded their terminal;
        # the failing branch's exception group is consumed here (the durable record
        # is the drained ledger). cascade_policy maps the run-level status.
        branch_failed = True

    # B-FANOUT-EFFECT-FENCE-BRANCH-PAUSE — an operator ABORT on resume is a TERMINAL decision:
    # force RunStatus.FAILED tier-agnostically, BEFORE the cascade-policy branching, so a
    # CascadePolicy.PAUSE run does NOT re-pause an aborted fence branch (the LINEAR ABORT → FAILED
    # analogue; Codex [P1]). The aborted branch already re-dispatched (its effect-fence claim is
    # the durable record); the run fails honestly.
    if effect_fence_aborted_dispositions:
        return _finish(
            RunStatus.FAILED, fail_class="parallelization-effect-fence-aborted", salvage=False
        )

    if cascade_policy is CascadePolicy.CASCADE_CANCEL:
        # obl. 4: a not-yet-dispatched branch (no step/terminal disposition — its
        # task was cancelled before scheduling its dispatch) records a `cancelled`
        # terminal so resume does not double-dispatch. Keyed on the ABSENCE of a
        # step/terminal disposition, NOT an empty buffer (an overridden branch
        # carries its pre-fan-out override entry, `branch_metadata=None`).
        for _bi, _step, child, writer, _binding in branch_plan:
            if not _writer_has_branch_disposition(writer):
                # B-FANOUT-EFFECT-FENCE-BRANCH-PAUSE — an effect-fence-paused peer DID dispatch
                # (its own dispatch fired the fence; the effect may have landed), so under
                # cascade-cancel it MUST NOT be mislabeled `cancelled` (= not-yet-dispatched).
                # Record `completed` (dispatch-boundary) so the ledger reflects the dispatch;
                # the run FAILs + the resolution is discarded (not resumed under cascade-cancel).
                _fence_paused = _bi in effect_fence_paused_dispositions
                _disposition = "completed" if _fence_paused else "cancelled"
                if _fence_paused:
                    # ALSO capture the `completed`/no-output terminal to the durable STORE (not
                    # just the buffered ledger) — every other ran-and-errored branch does, via the
                    # branch coroutine. Without this, a crash mid-cascade-cancel leaves only the
                    # dispatch marker → crash-resume mis-classifies the branch as MAYBE-RAN +
                    # re-dispatches instead of reproducing the cascade-cancel FAILED (Codex [P2]).
                    _capture_branch_terminal(
                        ctx,
                        manifest_entry,
                        run_idempotency_key=run_idempotency_key,
                        branch_index=_bi,
                        step_id=str(steps[_bi].step_id),
                        terminal_status="completed",
                        output=None,
                    )
                append_branch_terminal_ledger_entry(
                    branch_writer=writer,
                    branch_context=child,
                    run_idempotency_key=run_idempotency_key,
                    terminal_status=_disposition,
                    timestamp=fanout_timestamp,
                    procedural_tier_snapshot_ref=snapshot_ref,
                )
        # B-FANOUT-EFFECT-FENCE-PER-BRANCH-SCOPED-ABORT (R-FS-1, CP spec v1.73 §1; Codex [P1]) —
        # under CASCADE_CANCEL a scoped-abort is a deliberate branch failure → the run cancels
        # (FAILED), NEVER a SUCCESS hiding the aborted branch. Per-branch isolation is incompatible
        # with cascade-cancel-everything; this fires BEFORE the SUCCESS return below (the
        # cascade-cancel block returns before the §25.15.1 degraded fold, so the fold's scoped-abort
        # guard would otherwise be bypassed on this tier).
        if _scoped_abort_ordinals:
            return _finish(
                RunStatus.FAILED,
                fail_class="parallelization-effect-fence-branch-aborted",
                salvage=False,
            )
        if branch_failed or deadline_struck:
            return _finish(
                RunStatus.FAILED,
                fail_class="parallelization-cascade-cancel",
                salvage=False,
            )
        return _finish(RunStatus.SUCCESS, fail_class=None, salvage=False)

    # pause (§25.15.1 `pause → PAUSED`) — resumable PARALLELIZATION pause
    # (B-FANOUT-PAUSE-PARALLELIZATION, R-FS-1; CP spec v1.44 §2). On a branch failure:
    # in-flight siblings finished (shielded, recorded their terminal); not-yet-
    # dispatched siblings were TaskGroup-cancelled and are LEFT RE-DISPATCHABLE — the
    # cascade-cancel `cancelled`-terminal scan above is DELIBERATELY NOT run here (the
    # §25.15.1 pause semantic: "in-flight finish; not-yet-dispatched left
    # re-dispatchable"). We capture the per-branch terminal dispositions + the
    # completed branches' OUTPUTS (which the ledger does NOT carry) into a
    # `PeerFanOutResumeState`-bearing, hash-integrity-checked `PauseSnapshot`, return
    # PAUSED, and `api.resume` re-enters this strategy to skip terminal branches +
    # re-dispatch the rest (obligation 7). The deadline-strike case (a STUCK fan-out,
    # no branch raised) stays FAILED — there is no clean pause boundary to resume from.
    if deadline_struck:
        return _finish(
            RunStatus.FAILED, fail_class="parallelization-barrier-deadline", salvage=False
        )
    # B-FANOUT-CRASH-RESUME-PAUSE-RECONSTRUCT (R-FS-1, CP spec v1.68 §1) — a COMPLETE-recovery
    # crash-resume of a degraded PAUSE fan-out RE-ESTABLISHES the lost PAUSED state (Reading A:
    # the crash interrupted the run BEFORE it produced PAUSED, so the fresh re-execution restores
    # it; NOT the operator's resume, which would finalize PARTIAL). `not branch_plan` ⟺ every
    # branch is recovered-terminal (complete; nothing re-dispatched), so the snapshot is a pure
    # state-restoration reusing the SAME capture block a live branch-failure pause runs (→ the
    # reconstructed `branches` byte-match a real pause snapshot). The crash-resume entry already
    # fail-closed the INCOMPLETE case (§1; B-…-NOT-YET-DISPATCHED / -MAYBE-RAN residuals).
    # The pause TRIGGER is a genuine branch FAILURE (`terminal_status == "completed"` + no
    # collected output = ran-and-errored), NOT a RECOVER_AS_TERMINAL `timed_out` branch (which is
    # a degraded non-contributor → PARTIAL via the tail, never a pause — a live timeout is
    # `deadline_struck`→FAILED, never a pause). Out-of-family Codex [P2]: keying the re-establish
    # on `output is None` ALONE would re-pause a recovered-timeout that never lost a pause snapshot.
    _crash_pause_reestablish = (
        crash_fan_out_resume is not None
        and resume_snapshot is None
        and cascade_policy is CascadePolicy.PAUSE
        and not branch_plan
        and any(
            terminal_dispositions[_bi] == "completed" and _bi not in collected
            for _bi in terminal_dispositions
        )
    )
    if branch_failed or _crash_pause_reestablish:
        protocol = getattr(ctx, "pause_resume_protocol", None)
        if protocol is None:
            # No pause/resume opt-in bound → the snapshot cannot be captured, so a
            # PAUSED would advertise a resumability the harness cannot honor (the
            # FALSE-`PAUSED` silent-degradation mode). Fail HONESTLY — detect-then-
            # refuse, mirroring `api.resume`'s ResumeProtocolNotBoundError. Completed
            # / in-flight ledger entries + the salvaged partial set still persist.
            return _finish(
                RunStatus.FAILED,
                fail_class="parallelization-pause-resume-protocol-not-bound",
                salvage=True,
            )
        # Build the resume state from the post-barrier terminal dispositions +
        # collected outputs — both already MERGED with any recovered-from-a-prior-
        # resume terminals (the seed loop above), so a RE-pause snapshot unions the
        # prior + this-round terminal sets. Absent branch ordinals (the cancelled
        # not-yet-dispatched ones) are left re-dispatchable by omission. `step_id`
        # is captured per branch so resume validates body identity.
        peer_fan_out_resume = PeerFanOutResumeState(
            branches=tuple(
                FanOutBranchResumeState(
                    branch_index=_bi,
                    step_id=str(steps[_bi].step_id),
                    terminal_status=_status,
                    output=(collected[_bi][1] if _bi in collected else None),
                )
                for _bi, _status in sorted(terminal_dispositions.items())
            ),
            branch_count=len(steps),
            # B-FANOUT-EFFECT-FENCE-BRANCH-PAUSE — peers whose OWN dispatch raised the effect
            # fence this round. DISJOINT from `branches` (a fence-paused ordinal never entered
            # `terminal_dispositions`). Each carries the held reserve's idempotency_key so
            # `api.resume` key-binds the operator's resolution to THIS peer's effect. COVERED by
            # the snapshot hash (dropped-when-empty → a no-fence pause hashes byte-identically).
            # B-FANOUT-CRASH-RESUME-PAUSE-RECONSTRUCT-MAYBE-RAN-FENCE-STEP-ID — APPEND the
            # fence-recoverable maybe-ran ordinals the crash-resume gate classified (empty `()` in
            # reconstruct mode the live disposition map is empty; empty `()` in a normal pause the
            # reconstruct tuple is). Disjoint from terminals (maybe-ran ≠ recovered) + the live set
            # (no branch dispatched in reconstruct mode); the resume material-diff guard re-checks
            # disjointness + the changed-step_id / changed-kind identity.
            effect_fence_paused_branches=tuple(
                EffectFencePausedBranchResumeState(
                    branch_index=_bi,
                    step_id=str(steps[_bi].step_id),
                    step_kind=str(steps[_bi].step_kind.value),
                    idempotency_key=_fence_key,
                )
                for _bi, _fence_key in sorted(effect_fence_paused_dispositions.items())
            )
            + crash_pause_reconstruct_fence_paused,
            # B-FANOUT-PAUSE-SYNTHESIS — capture the terminal POST_JOIN_SYNTHESIS
            # step's identity (presence + step_id) so resume can material-diff it
            # before fresh-dispatching. None when no synthesis was opted in
            # (drop-from-hash-when-None keeps those snapshots byte-identical).
            synthesis_step_id=(str(synthesis_step.step_id) if synthesis_step is not None else None),
        )
        # B-FANOUT-EFFECT-FENCE-BRANCH-PAUSE — label the pause EFFECT_FENCE_AMBIGUOUS whenever a
        # branch fence-paused this round, so an operator surface keying off the reason knows to
        # request an `EffectFenceResolution` (mirrors the LINEAR effect-fence pause reason); else
        # the ordinary `cascade_policy=pause` branch-failure pause stays EXPLICIT_OPERATOR (Codex
        # [P2]).
        _pause_reason = (
            WorkflowPauseReason.EFFECT_FENCE_AMBIGUOUS
            if effect_fence_paused_dispositions
            else WorkflowPauseReason.EXPLICIT_OPERATOR
        )
        snapshot = _run_protocol_method_sync(
            cast(PauseResumeProtocol, protocol).capture_pause_snapshot(
                workflow_id=workflow_id,
                run_id=run_id,
                step_index=0,
                pause_reason=_pause_reason,
                peer_fan_out_resume=peer_fan_out_resume,
            )
        )
        return _finish(RunStatus.PAUSED, fail_class=None, salvage=True, pause_snapshot=snapshot)
    # No branch failed THIS round. But a RECOVERED terminal branch may have failed in
    # the original run (a resume tail) — a terminal branch with no collected output is
    # a failed/timed-out branch (`_record_clean` always populates `collected` for a
    # clean branch). Returning a bare SUCCESS there would SILENTLY drop that failure
    # (the silent-degradation class this arc forecloses) — instead mirror `proceed`:
    # degraded → PARTIAL + salvage. Non-resume clean runs have no failed terminal →
    # not degraded → SUCCESS (no regression).
    # B-FANOUT-EFFECT-FENCE-PER-BRANCH-SCOPED-ABORT (R-FS-1, CP spec v1.73 §1) — ALL-ABORT GUARD:
    # if every fence-paused peer was scoped-aborted and NO peer survived (nothing collected), the
    # run has NO result → FAILED (with the operator-abort provenance fail_class — conventional on a
    # FAILED status, the codebase reads fail_class gated on FAILED), not the vacuous PARTIAL the
    # degraded check below would return with zero survivors. A PARTIAL WITH survivors carries
    # `fail_class=None` like every other degraded PARTIAL (the aborted peer is a degraded terminal
    # non-contributor recorded per-branch in the ledger; run-result operator-abort-vs-errored
    # provenance on a PARTIAL is out of scope — the pre-existing limit for any degraded branch).
    if _scoped_abort_ordinals and not collected:
        return _finish(
            RunStatus.FAILED,
            fail_class="parallelization-effect-fence-branch-aborted",
            salvage=True,
        )
    if any(_bi not in collected for _bi in terminal_dispositions):
        return _finish(RunStatus.PARTIAL, fail_class=None, salvage=True)
    return _finish(RunStatus.SUCCESS, fail_class=None, salvage=False)


# ---------------------------------------------------------------------------
# U-CP-87 — EVALUATOR_OPTIMIZER driver strategy (C-CP-25 §25.11)
# ---------------------------------------------------------------------------
#
# The SECOND non-linear topology strategy — a sequential generate→evaluate→
# (accept | regenerate) loop, bounded by a max-iteration cap (§25.11
# EVALUATOR_OPTIMIZER row: "Loop: generate-step → evaluate-step → (accept |
# regenerate-with-feedback), bounded by a max-iteration cap. Sequential;
# terminal on evaluator accept or cap.").
#
# Per the §25.11 common substrate, `steps` is the loop body: `steps[0]` is the
# GENERATE step (the optimizer) and `steps[1]` is the EVALUATE step (the
# evaluator); the two are distinguished by their per-step prompt (R-PM-1 §29,
# already landed) — non-hollow at B1 WITHOUT the B4 per-role binding catalog,
# because generate ≠ evaluate by `step_id` (and hence by selected prompt). The
# CP-unit proof is distinct-step dispatch (a different `step_id` resolves a
# different binding); live R-PM-1 §29 prompt selection is composed at runtime
# stage-0, not in the CP driver (presence-vs-correctness honesty).
#
# Deps are [U-CP-80 (dispatch), U-CP-82 (buffered-append substrate)] — NOT
# U-CP-81 (no branch child-contexts; sequential single-owner, NO fan-out) and
# NOT U-CP-85 (no cascade_policy). So:
#   - NO branch_metadata: entries carry the carrier default (`None`) — the AC's
#     "Sequential; no fan-out branch_metadata required". (Hence a dedicated
#     `_append_buffered_sequential_entry`, NOT the U-CP-84 branch helpers, which
#     always compose branch_metadata + require a branch child-context.)
#   - The buffered/deferred-append path (§25.11/§25.12) is STILL used (the
#     common-substrate mandate for all 5 non-linear strategies + the plan AC):
#     one `BufferingLedgerWriter`; the orchestrator drains it through the single
#     real writer at the end. Sequential execution is already naturally ordered,
#     so the drain is order-preserving by construction (the determinism subtlety
#     is the fan-out concern, not this one).
#
# Idempotency (the load-bearing decision). The loop re-dispatches the SAME two
# declared steps each iteration, so TWO distinct indices are kept apart:
#   - the MONOTONIC `entry_index` (0,1,2,3,… across the whole loop) scopes the
#     unique ledger action_id + idempotency key. Re-using the declared step
#     ordinal (0/1) as the ledger key would collide iteration-2's generate with
#     iteration-1's on the IS writer's `idempotency_key`-only dedup (C-IS-07
#     §7.5) → a silently-dropped entry. The monotonic key makes every dispatched
#     step persist a distinct entry (the live e2e asserts the full
#     `iterations × 2` persisted count + chain VALID).
#   - the DECLARED step ordinal (0=generate, 1=evaluate) is what
#     `StepExecutionContext.step_index` carries (Codex [P2]): downstream
#     dispatchers read that field for per-step policy / override selection +
#     audit context, so it MUST keep matching the declared step across iterations
#     rather than drifting to the ledger row number. The ledger `action_id`s
#     (incl. `parent_action_id`) keep the unique `entry_index`.
#
# Scope (scoped-not-forgotten): the linear-only prefix-replay / explicit-pause
# resume detection, mid-loop drain checks, per-step pause-trigger detection, and
# the per-step validator hook are NOT composed here (their units are not EO
# deps) — they compose at later strategy units. "regenerate-with-feedback": the
# loop re-dispatching the generate step IS the regenerate. Inter-step DATA flow
# (the generate draft → the evaluator's input; the evaluator's feedback → the
# next generate's input) is realized by **B-INTERSTEP** (runtime spec §14.21
# C-RT-34, new at v1.59) — the runtime/dispatcher concern this comment originally
# named ("a shared run context the dispatcher reads"). The driver records each
# step's opaque output to `ctx.inter_step_output_channel` (the
# `_record_inter_step_output` call in `_dispatch_and_buffer` above; opt-in via
# `RuntimeConfig.inter_step_data_flow`, no-op when unbound) and the runtime LLM
# dispatcher injects `most_recent_output()` into the next dispatch's payload. The
# dispatcher Protocol signature is UNCHANGED (`binding, step, step_context`, never
# a prior-output parameter) and the driver still never introspects or mutates the
# frozen `step_payload` (§25.3.3.4 preserved) — it records the dispatcher's
# already-produced output. The channel is the SAME shared run-context for every
# topology; the `SINGLE_THREADED_LINEAR` path records at its `accumulated` site.
# EO at the driver level remains the loop CONTROL FLOW (generate→evaluate→accept/
# regenerate, bounded cap, accept-signal read from the evaluator's structured
# output). Scope at v1.59: SINGLE_THREADED_LINEAR + EVALUATOR_OPTIMIZER record;
# the 4 remaining non-linear strategies' recording (concurrent-sibling writes via
# the #648 buffered-branch drain) + cross-step resume rehydration (the
# B-ENGINE-OUTPUT-REPLAY output-carrying substrate) are registered follow-ons.

_EVALUATOR_OPTIMIZER_ACCEPT_KEY = "accepted"
"""The reserved key the EVALUATE step's output sets truthy to signal acceptance
(C-CP-25 §25.11 EVALUATOR_OPTIMIZER terminal-on-accept; §25.18 impl-discretion).

The evaluator/optimizer roles are distinguished by per-step prompt (R-PM-1 §29);
the accept SIGNAL the driver reads from the evaluator's structured output is this
boolean key. A missing/false key ⟹ regenerate (continue the loop). The signal
SHAPE is impl-discretion (§25.18 — the contract specifies observable behavior,
not the signal encoding); no other accept/terminal convention exists in step
outputs (grep-clean at authoring)."""

_DEFAULT_EVALUATOR_OPTIMIZER_MAX_ITERATIONS = 3
"""The max-iteration cap on the generate→evaluate loop (C-CP-25 §25.11 "bounded
by a max-iteration cap"; §25.18 impl-discretion). The loop terminates on the
first evaluator-accept OR when this many iterations have run without accept (a
best-effort SUCCESS, `accepted=False`; §25.17 lists no cap-failure mode — cap is
a normal bounded termination, NOT a failure). A manifest-surfaced per-workflow
cap is a forward field (not surfaced at v1.32)."""


def _evaluator_optimizer_accepted(evaluation: Mapping[str, Any]) -> bool:
    """`True` when the evaluator's output signals acceptance (terminal-on-accept).

    Reads the `_EVALUATOR_OPTIMIZER_ACCEPT_KEY` reserved key (truthy ⟹ accept;
    absent/false ⟹ regenerate). Pure; no side effects.
    """
    return bool(evaluation.get(_EVALUATOR_OPTIMIZER_ACCEPT_KEY, False))


def _append_buffered_sequential_entry(
    *,
    writer: BufferingLedgerWriter,
    workflow_id: str,
    entry_index: int,
    idempotency_key: str,
    timestamp: datetime,
    procedural_tier_snapshot_ref: Identifier | None = None,
) -> None:
    """Buffer a sequential strategy's per-step ledger entry — NO branch_metadata
    (C-CP-25 §25.11/§25.12 buffered path; the EVALUATOR_OPTIMIZER sequential
    analogue of the U-CP-84 branch helpers).

    Composes the flat `workflow:{wf}:step:{entry_index}` action_id (the
    caller-supplied `idempotency_key` is the matching
    `_compute_step_idempotency_key(run_idempotency_key, entry_index)` value,
    reused from the `StepExecutionContext` composition to avoid recomputing it)
    and buffers it through the strategy's single `BufferingLedgerWriter` (the
    write is deferred to the drain; dispatch + telemetry already fired inline, so
    the pre-dispatch gate is never deferred — §25.15.2 obl. 2). The
    `branch_metadata` carrier stays the default `None` — this strategy is
    sequential single-owner with NO fan-out causality (the AC's "no fan-out
    branch_metadata required"); it is therefore a dedicated helper, NOT the
    U-CP-84 branch-cadence helpers (which always compose branch_metadata).

    `entry_index` is MONOTONIC across the whole loop (NOT the declared step
    ordinal) so iteration-N's re-dispatch of the same declared step never collides
    with iteration-(N-1)'s on the IS writer's idempotency_key-only dedup (C-IS-07
    §7.5). The linear `_append_step_ledger_entry` is left byte-unchanged (§25.10
    Invariant 1).
    """
    from harness_is.state_ledger_entry_schema import Identifier as _Identifier
    from harness_is.state_ledger_write import EntryPayload, WriteKey

    action_id = ActionID(f"workflow:{workflow_id}:step:{entry_index}")
    payload = EntryPayload(
        action_id=_Identifier(str(action_id)),
        idempotency_key=_Identifier(idempotency_key),
        actor=writer.actor,
        timestamp=timestamp,
        procedural_tier_snapshot_ref=procedural_tier_snapshot_ref,
    )
    write_key = WriteKey(
        thread_id=_Identifier(workflow_id),
        step_id=_Identifier(str(entry_index)),
        idempotency_key=_Identifier(idempotency_key),
    )
    writer.append(payload, write_key)


class _EvaluatorOptimizerStepDispatchError(Exception):
    """Marks a failure from the EO step DISPATCH itself (pause-eligible).

    Distinguished from a SETUP failure (dispatcher lookup / binding resolution) or a
    post-dispatch BOOKKEEPING failure (ledger buffer / inter-step record / cursor append):
    only a genuine `dispatch()` failure may take the `cascade_policy=pause` → resumable
    PAUSED path. A setup error would loop forever on resume (the cause is still present);
    a bookkeeping error after a successful dispatch means the step's effect already landed,
    so a resume would double-fire it — both must FAIL, never PAUSE (out-of-family Codex [P2])."""

    def __init__(self, original: BaseException) -> None:
        super().__init__(str(original))
        self.original = original


def _execute_evaluator_optimizer(
    *,
    manifest_entry: WorkflowManifestEntry,
    steps: Sequence[WorkflowStep],
    run_id: str,
    ctx: DriverContext,
    default_model_binding: ModelBinding,
    step_dispatchers: StepDispatcherRegistry,
    run_idempotency_key: str,
    resume_snapshot: PauseSnapshot | None = None,
) -> tuple[RunResult, int]:
    """Execute the `EVALUATOR_OPTIMIZER` generate→evaluate→regenerate loop (U-CP-87).

    `steps[0]` is the GENERATE step, `steps[1]` is the EVALUATE step. The loop
    dispatches generate then evaluate, terminating on the first evaluator-accept
    (`_evaluator_optimizer_accepted`) or when
    `_DEFAULT_EVALUATOR_OPTIMIZER_MAX_ITERATIONS` iterations have run. Each
    dispatched step buffers a plain (no-branch_metadata) ledger entry keyed by a
    MONOTONIC `entry_index`; the buffer drains through the single real writer at
    the end (§25.11/§25.12 buffered path). Returns `(RunResult, steps_executed)`
    for the `_execute_workflow_body` caller (matching the linear path's tuple;
    `steps_executed` counts only steps dispatched in THIS envelope — a resume
    excludes its recovered prefix, mirroring `_execute_decentralized_handoff`).

    Terminal: evaluator-accept OR cap → SUCCESS (`final_state.accepted`
    discriminates accept-terminal from cap-terminal; §25.17 lists no cap-failure
    mode); a step dispatch raising → FAILED (the prior buffered entries STILL
    drain — no silent loss). Sequential single-owner; NO fan-out, NO branch_metadata (AC).

    `pause → PAUSED` resume (R-FS-1 `B-FANOUT-PAUSE-EVALUATOR-OPTIMIZER`): a
    TEAM-tier step failure under `cascade_policy=pause` (with a bound
    `pause_resume_protocol`) captures an `EvaluatorOptimizerResumeState`-bearing
    `PauseSnapshot` (the completed-step prefix + their recovered outputs) + returns
    PAUSED; `api.resume` re-enters here with `resume_snapshot`, replays the
    completed-step prefix recovering its outputs (NOT re-dispatched — effect may
    have landed), and re-dispatches from the failed step onward, honoring the
    original max-iteration cap across the resume boundary (the recovered generate
    count reconstructs the cap — every iteration has exactly one generate). When no
    protocol is bound, `pause` fails HONESTLY
    (`evaluator-optimizer-pause-resume-protocol-not-bound`) — never a
    false-resumable PAUSED. `proceed` / `cascade-cancel` retain EO's existing
    terminal-FAILED disposition (only `pause` is materialized for EO — surgical
    additive scope). This materializes the §25.15.1 `pause → PAUSED` row EXTENDED
    to the sequential single-owner EO loop per §25.18's named impl-order (the
    §25.15.1 row text is fan-out-barrier-scoped; the extension is impl-discretion,
    mirroring the #681 `DECENTRALIZED_HANDOFF` extension — not a re-reading of the row).
    """
    workflow_id = manifest_entry.workflow_id

    # B-FANOUT-PAUSE-EVALUATOR-OPTIMIZER (R-FS-1) — iteration-cursor resume reconstruction
    # state (None on a normal first run). The cursor: a CONTIGUOUS completed-step prefix
    # (entry_index 0..m-1) recovered on resume + the re-dispatchable failed step (m onward).
    # Unlike the fan-out carriers (a branch set with re-dispatchable gaps), an EO resume is
    # a strictly sequential prefix over the loop's generate/evaluate steps.
    _eo_resume = resume_snapshot.evaluator_optimizer_resume if resume_snapshot is not None else None
    _resume_completed_count = len(_eo_resume.completed_steps) if _eo_resume is not None else 0
    if _eo_resume is not None:
        # Material-diff guard on resume: the re-supplied body MUST be the same 2 declared
        # steps, the cursor prefix MUST be contiguous 0..m-1 with the right parity
        # (even⟹generate / odd⟹evaluate), each recovered step's identity MUST still match
        # the body slot, and no recovered evaluate may signal accept (an accept terminates
        # the loop SUCCESS, never pauses) — else the recovered output no longer maps to its
        # step → fail closed. Mirrors the DECENTRALIZED_HANDOFF resume-body-mismatch guard.
        def _resume_body_mismatch() -> str | None:
            assert _eo_resume is not None  # narrowed by the enclosing guard
            assert resume_snapshot is not None  # _eo_resume non-None ⇒ snapshot non-None
            completed = _eo_resume.completed_steps
            # Cross-field coherence: `snapshot.step_index` is the failed step's DECLARED
            # ordinal (0=generate / 1=evaluate) — a valid `steps` position (the runtime
            # `api.resume` guard requires `0 <= step_index < len(steps)`, which the
            # entry_index cannot satisfy since the loop re-dispatches the same 2 steps).
            # The failed step's entry_index == the prefix length, so its declared ordinal
            # is `len(completed) % 2`; a hash-valid but INCOHERENT snapshot whose
            # step_index disagrees with the cursor's next-step parity → fail closed.
            _expected_failed_declared = len(completed) % 2
            if resume_snapshot.step_index != _expected_failed_declared:
                return (
                    f"cursor-step-index-mismatch: snapshot.step_index="
                    f"{resume_snapshot.step_index} but the {len(completed)}-step prefix "
                    f"implies the failed step's declared ordinal is {_expected_failed_declared} "
                    f"(even-length prefix ⟹ a failed generate=0, odd ⟹ a failed evaluate=1)"
                )
            # EO is exactly generate→evaluate (2 declared steps). A resumed body of any
            # other size is a changed body → fail closed (the malformed-count check below
            # only runs for a non-resume run).
            if len(steps) != 2:
                return (
                    f"step-count-mismatch: EVALUATOR_OPTIMIZER requires exactly 2 declared "
                    f"steps (generate, evaluate); resume supplied {len(steps)}"
                )
            for expected_index, cs in enumerate(completed):
                if cs.entry_index != expected_index:
                    return (
                        f"non-contiguous-prefix at position {expected_index}: "
                        f"entry_index={cs.entry_index} (a loop prefix must be 0..m-1)"
                    )
                # Loop-alternation coherence: even entry ⟹ generate (declared 0), odd ⟹
                # evaluate (declared 1). Also bounds the `steps[...]` index to {0,1}.
                if cs.declared_step_index != cs.entry_index % 2:
                    return (
                        f"step-parity-mismatch at entry {cs.entry_index}: declared_step_index"
                        f"={cs.declared_step_index} (even⟹0 generate / odd⟹1 evaluate)"
                    )
                if str(steps[cs.declared_step_index].step_id) != cs.step_id:
                    return (
                        f"step-identity-mismatch at entry {cs.entry_index}: snapshot "
                        f"step_id={cs.step_id!r}, resume step_id="
                        f"{str(steps[cs.declared_step_index].step_id)!r}"
                    )
                # An accept in the recovered prefix is incoherent — an accepting evaluate
                # would have terminated the loop SUCCESS, not paused. Fail closed on a
                # tampered cursor smuggling an accept into the prefix.
                if cs.declared_step_index == 1 and _evaluator_optimizer_accepted(cs.output):
                    return (
                        f"accepted-step-in-prefix at entry {cs.entry_index}: a recovered "
                        "evaluation signals accept, but an accept terminates the loop "
                        "SUCCESS (a paused prefix is all non-accepts)"
                    )
            # Resumable-tail + cap-coherence check (mirrors the handoff no-resumable-stage
            # guard). A legitimate pause leaves a failed step to re-dispatch AND never
            # exceeds the max-iteration cap. The cap bound differs by cursor parity:
            # - EVEN cursor (pending generate / a generate-failure pause): the failed
            #   generate is NOT in the prefix, and the original loop only dispatched it when
            #   `generates_so_far < MAX`, so a legitimate even cursor has
            #   `generates_recovered < MAX` (and needs a re-dispatchable generate under cap).
            # - ODD cursor (pending evaluate / an evaluate-failure pause): the iteration's
            #   generate IS in the prefix, and the loop only started that iteration when
            #   `generates_so_far <= MAX`, so a legitimate odd cursor has
            #   `generates_recovered <= MAX`.
            # A cursor exceeding its parity's bound is semantically impossible (no real run
            # could produce it) → fail closed on a tampered/corrupt-but-hash-valid durable
            # cursor (out-of-family Codex [P2] — the odd over-cap case the even-only check
            # missed: e.g. 7 records / 4 generates at MAX=3 would otherwise replay past cap).
            generates_recovered = sum(1 for cs in completed if cs.declared_step_index == 0)
            pending_evaluate = len(completed) % 2 == 1
            _cap = _DEFAULT_EVALUATOR_OPTIMIZER_MAX_ITERATIONS
            if (not pending_evaluate and generates_recovered >= _cap) or (
                pending_evaluate and generates_recovered > _cap
            ):
                return (
                    f"no-resumable-iteration: {generates_recovered} generates recovered "
                    f"exceeds the cap {_cap} for a "
                    f"{'pending-evaluate' if pending_evaluate else 'pending-generate'} cursor "
                    f"(a real run cannot produce this cursor; pause requires a "
                    f"re-dispatchable step within the iteration cap)"
                )
            return None

        _mismatch = _resume_body_mismatch()
        if _mismatch is not None:
            return RunResult(
                workflow_id=workflow_id,
                run_id=run_id,
                status=RunStatus.FAILED,
                terminal_step_index=None,
                partial_state=None,
                final_state=None,
                fail_class=f"evaluator-optimizer-resume-body-mismatch: {_mismatch}",
            ), 0

    # Empty step sequence → trivially SUCCESS (mirrors the linear empty-loop + the
    # PARALLELIZATION empty-steps SUCCESS). Only reachable on a non-resume run — a resume
    # always carries a non-empty cursor (a pause is captured on a step FAILURE), so an
    # empty body + a non-None cursor is caught by the step-count guard above → FAILED.
    if not steps:
        return RunResult(
            workflow_id=workflow_id,
            run_id=run_id,
            status=RunStatus.SUCCESS,
            terminal_step_index=None,
            partial_state=None,
            final_state={"accepted": False, "iterations": 0, "output": {}, "evaluation": {}},
            fail_class=None,
        ), 0

    # EVALUATOR_OPTIMIZER is exactly generate→evaluate (§25.11) — 2 declared
    # steps. A non-empty manifest declaring any other count is malformed for this
    # pattern (a multi-step generate phase would be a spec extension, not a driver
    # generalization — X-AL-3). FAILED with a clear fail_class (no silent reshape).
    if len(steps) != 2:
        return RunResult(
            workflow_id=workflow_id,
            run_id=run_id,
            status=RunStatus.FAILED,
            terminal_step_index=None,
            partial_state=None,
            final_state=None,
            fail_class=(
                "evaluator-optimizer-malformed: expected exactly 2 steps "
                f"(generate, evaluate); got {len(steps)}"
            ),
        ), 0

    generate_step, evaluate_step = steps[0], steps[1]

    # R-003 active-workflow-context sidecar (resolved once; the same resolver the
    # linear `_append_step_ledger_entry` reads). None when no resolver is bound.
    _resolver = getattr(ctx, "procedural_tier_snapshot_resolver", None)
    snapshot_ref = _resolver() if _resolver is not None else None

    # The on-step-failure cascade reaction (§25.15.1 EXTENDED to the sequential EO loop per
    # §25.18's named impl-order) — resolved from the manifest's (workload, engine, persona)
    # via the §11.4 D4 tunable, the same source ORCHESTRATOR_WORKERS / DECENTRALIZED_HANDOFF
    # read (SOLO→proceed / TEAM→pause / MTC→cascade-cancel). Only `pause` is materialized
    # for EO; `proceed` / `cascade-cancel` retain EO's existing terminal-FAILED disposition.
    cascade_policy = d4_tunable(
        lookup_cell(manifest_entry.workload_class, manifest_entry.engine_class),
        manifest_entry.persona_tier,
    ).cascade_policy

    # Buffer-time placeholder timestamp for every buffered entry; the
    # authoritative append timestamp is assigned at the drain
    # (`drain_branch_buffers` re-stamps to one drain-moment value — the
    # IS-monotonicity realization, see the module timestamp-discipline note).
    loop_timestamp = datetime.now(UTC)

    writer = BufferingLedgerWriter(actor=ctx.ledger_writer.actor, branch_index=0)

    # § 25.3.2 — Emit workflow.start (single-threaded on the driver thread). An EO RESUME
    # re-enters the SAME envelope (the completed-step prefix already ran in the original
    # run), so it emits RESUMPTION — not a second WORKFLOW_START — mirroring the fan-out /
    # handoff resume emit.
    ctx.lifecycle_emitter.emit(
        WorkflowEventClass.RESUMPTION
        if _eo_resume is not None
        else WorkflowEventClass.WORKFLOW_START
    )

    # B-FANOUT-PAUSE-EVALUATOR-OPTIMIZER — the completed-step records (recovered prefix +
    # this-run completions), captured into an EvaluatorOptimizerResumeState cursor on a
    # `pause` halt. Pre-populated below on resume with the recovered prefix so a RE-pause
    # unions it with the newly-completed steps (stays a contiguous prefix across resumes).
    completed_step_records: list[EvaluatorOptimizerStepResumeState] = []

    def _dispatch_and_buffer(
        step: WorkflowStep, *, declared_step_index: int, entry_index: int
    ) -> Mapping[str, Any]:
        # Dispatch one declared step on the driver thread (sequential — no
        # to_thread / barrier), buffer its plain ledger entry under the monotonic
        # entry_index, and emit one STEP_BOUNDARY. The pre-dispatch gate fires
        # inline inside the dispatcher; only the ledger WRITE is buffered
        # (§25.15.2 obl. 2). A dispatch exception propagates to the caller's
        # FAILED handler (the entry is NOT buffered + no boundary emitted for the
        # failed step, so entry_index stays the completed-step count).
        #
        # TWO distinct indices (Codex [P2]). `declared_step_index` is the step's
        # DECLARED ordinal (0=generate, 1=evaluate) — it is what
        # `StepExecutionContext.step_index` carries, because downstream dispatchers
        # read that field for per-step policy / override selection + audit context
        # (it must keep matching the declared step across loop iterations, NOT
        # drift to the ledger row number). `entry_index` is the MONOTONIC ledger
        # row index (0,1,2,3,…) — it scopes the unique ledger action_id +
        # idempotency key so re-dispatching the same declared step across
        # iterations never collapses on the IS writer's idempotency_key-only dedup
        # (C-IS-07 §7.5). The two coincide only on iteration 0.
        binding = resolve_step_binding(
            manifest_entry,
            str(step.step_id),
            default_model_binding=default_model_binding,
            persona_tier=manifest_entry.persona_tier,
        )
        entry_idempotency_key = _compute_step_idempotency_key(run_idempotency_key, entry_index)
        step_context = StepExecutionContext(
            workflow_id=workflow_id,
            parent_action_id=f"workflow:{workflow_id}:step:{entry_index}",
            parent_gate_level=resolve_parent_gate_level(manifest_entry),
            # B-HITL-PLACEMENT-PER-STEP-PRODUCER — EVALUATOR_OPTIMIZER per-step.
            # B-HITL-PLACEMENT-PER-STEP-OVERRIDE-FOLD (CP spec v1.49 §6.2) — fold
            # the per-step `binding.hitl_placement` override onto the workflow
            # tuple (union-by-position, tune-not-remove, monotone). None → verbatim.
            hitl_placements=fold_step_hitl_placements(
                manifest_entry.hitl_placements, binding.hitl_placement
            ),
            # B-EFFECT-FENCE-DURABLE-AUTO — the RUN engine class (NOT a per-step
            # StepOverride.engine_class) so the tool dispatcher auto-fences durable runs.
            run_engine_class=manifest_entry.engine_class,
            parent_sandbox_tier=SandboxTier.TIER_1_PROCESS,
            parent_actor=ctx.ledger_writer.actor,
            parent_entry_hash="",
            parent_idempotency_key=entry_idempotency_key,
            tenant_id=ctx.tenant_id,
            step_index=declared_step_index,
            # B4 Slice 4 (CP spec v1.38 §6.1) — per-step ROLE override folded into
            # the single role source (evaluator-optimizer generate/evaluate steps
            # carry no derived role, so the override is the sole source — linear
            # precedent). None → byte-identical to v1.37 (§14.5.3 invariant-1).
            agent_role=binding.agent_role,
        )
        # B-NONLINEAR-OVERRIDE-PROVENANCE — buffer the per-step override entry
        # BEFORE dispatch (mirrors the linear path's pre-dispatch emission). A
        # re-dispatched step (generate across iterations) buffers the same
        # (step, outcome) key each time → the IS writer idempotently dedups to
        # one persisted override entry (the §16.5.4 designed semantic).
        _buffer_branch_override_if_applied(
            branch_writer=writer,
            workflow_id=workflow_id,
            step=step,
            binding=binding,
            timestamp=loop_timestamp,
            snapshot_ref=snapshot_ref,
        )
        # Pre-flight the dispatcher lookup OUTSIDE the pause-eligible boundary (mirrors
        # `_execute_decentralized_handoff`): an UNBOUND StepKind is a SETUP/config error,
        # NOT a resumable step-dispatch failure — a `pause`→PAUSED here would loop forever
        # on resume (the dispatcher is still unbound). It raises natively (caught by the
        # outer `except Exception` → FAILED, never pause). Out-of-family Codex [P2].
        dispatcher = step_dispatchers.lookup(step.step_kind)
        # ONLY the `dispatch()` call is pause-eligible — wrap it so the outer cascade
        # handler distinguishes a genuine step-dispatch failure (resumable) from a setup
        # (lookup/binding) or post-dispatch BOOKKEEPING failure (the ledger buffer / record
        # below). A bookkeeping failure AFTER a successful dispatch means the effect already
        # landed, so a resume would DOUBLE-FIRE the step — those must FAIL, never PAUSE
        # (out-of-family Codex [P2]).
        try:
            step_output = dispatcher.dispatch(binding, step, step_context=step_context)
        except SubAgentChildPausedError:
            # An EO step that is a SUB_AGENT_DISPATCH whose recursive child sub-workflow
            # PAUSED (#680) raises the typed SubAgentChildPausedError. Propagate it TYPED so
            # the outer handler fails CLOSED — EO does NOT materialize the cross-recursion
            # child-pause disposition (that is the ORCHESTRATOR_WORKERS / HIERARCHICAL
            # `paused_child_branches` machinery). Wrapping it as a generic EO dispatch
            # failure would route it to the EO-level pause path, dropping the child's OWN
            # cursor (the #680 swallow-bug one level over). Out-of-family Codex [P2],
            # mirroring `_execute_decentralized_handoff`.
            raise
        except Exception as dispatch_exc:
            raise _EvaluatorOptimizerStepDispatchError(dispatch_exc) from dispatch_exc
        # B-INTERSTEP (runtime spec §14.21 C-RT-34) — record this step's output to
        # the run-scoped inter-step channel BEFORE the next dispatch so the EO data
        # flow is real: the evaluate dispatch reads the generate draft, and the
        # next iteration's regenerate reads the evaluator feedback (append-ordered
        # `most_recent_output()`). Opt-in; no-op when unbound. The EO loop runs
        # within one driver invocation (no resume boundary crossed), so this
        # within-loop data flow is resume-safe.
        _record_inter_step_output(ctx, str(step.step_id), step_output)
        _append_buffered_sequential_entry(
            writer=writer,
            workflow_id=workflow_id,
            entry_index=entry_index,
            idempotency_key=entry_idempotency_key,
            timestamp=loop_timestamp,
            procedural_tier_snapshot_ref=snapshot_ref,
        )
        ctx.lifecycle_emitter.emit(WorkflowEventClass.STEP_BOUNDARY)
        # B-FANOUT-PAUSE-EVALUATOR-OPTIMIZER — record this completed step for the resume
        # cursor (a later step's `pause` halt captures this prefix into an
        # EvaluatorOptimizerResumeState). Appended ONLY on a successful dispatch+buffer; a
        # failed dispatch raises before this, so the cursor never includes the failed step.
        completed_step_records.append(
            EvaluatorOptimizerStepResumeState(
                entry_index=entry_index,
                declared_step_index=declared_step_index,
                step_id=str(step.step_id),
                output=step_output,
            )
        )
        return step_output

    entry_index = 0
    accepted = False
    iterations = 0
    last_generate_output: Mapping[str, Any] = {}
    last_evaluation: Mapping[str, Any] = {}

    # B-FANOUT-PAUSE-EVALUATOR-OPTIMIZER (R-FS-1) — RESUME recovery of the completed-step
    # prefix. On resume, steps 0..m-1 already dispatched + persisted their ledger entries
    # in the ORIGINAL run; replay them here WITHOUT re-dispatching: recover each output,
    # re-seed the inter-step channel (so the first re-dispatched step reads its upstream
    # draft/feedback — the run-scoped channel starts empty on this fresh resume
    # invocation), reconstruct the loop counters (entry_index / iterations / last_*), and
    # carry the record forward for a possible RE-pause. DO NOT create a ledger entry (no
    # double-write; no step-count). `[[full-chain-witness-not-half-proofs]]`: a recovered
    # step's dispatcher is NEVER invoked across the resume (the discriminating witness).
    if _eo_resume is not None:
        for cs in _eo_resume.completed_steps:
            if cs.declared_step_index == 0:
                last_generate_output = cs.output
                iterations += 1  # every generate starts an iteration (the cap counter)
            else:
                last_evaluation = cs.output  # non-accept by construction (guarded above)
            _record_inter_step_output(ctx, cs.step_id, cs.output)
            completed_step_records.append(cs)
            entry_index += 1
    # An ODD cursor ⟹ the original paused on an evaluate (the iteration's generate is
    # recovered, the evaluate is the failed step to re-dispatch first to finish the
    # partial iteration); its iteration is ALREADY counted via the replayed generate.
    resume_pending_evaluate = entry_index % 2 == 1

    try:
        if resume_pending_evaluate:
            last_evaluation = _dispatch_and_buffer(
                evaluate_step, declared_step_index=1, entry_index=entry_index
            )
            entry_index += 1
            if _evaluator_optimizer_accepted(last_evaluation):
                accepted = True
        # The bounded generate→evaluate loop. `iterations` (generates started) spans the
        # resume boundary so the original max-iteration cap is honored across pause/resume
        # (recovered generates already counted toward it). A non-resume run enters with
        # iterations=0 + resume_pending_evaluate=False → byte-identical to the pre-arc
        # `for _iteration in range(MAX)` loop.
        while not accepted and iterations < _DEFAULT_EVALUATOR_OPTIMIZER_MAX_ITERATIONS:
            iterations += 1
            last_generate_output = _dispatch_and_buffer(
                generate_step, declared_step_index=0, entry_index=entry_index
            )
            entry_index += 1
            last_evaluation = _dispatch_and_buffer(
                evaluate_step, declared_step_index=1, entry_index=entry_index
            )
            entry_index += 1
            if _evaluator_optimizer_accepted(last_evaluation):
                accepted = True
                break
    except SubAgentChildPausedError as child_paused:
        # An EO SUB_AGENT_DISPATCH step's recursive child sub-workflow PAUSED. EO does NOT
        # materialize the cross-recursion child-pause disposition (the ORCHESTRATOR_WORKERS
        # / HIERARCHICAL `paused_child_branches` machinery) — converting it to an EO-level
        # PAUSE would DROP the child's cursor (the #680 swallow-bug one level over). Fail
        # CLOSED honestly (never a false-resumable EO PAUSE that loses the child state); the
        # completed prefix still drains (no silent loss). Out-of-family Codex [P2], mirroring
        # `_execute_decentralized_handoff`.
        drain_branch_buffers(ctx.ledger_writer, [writer])
        return RunResult(
            workflow_id=workflow_id,
            run_id=run_id,
            status=RunStatus.FAILED,
            terminal_step_index=None,
            partial_state=None,
            final_state=None,
            fail_class=(
                "evaluator-optimizer-child-pause-unsupported: an EO sub-agent step's child "
                f"PAUSED ({child_paused}); the cross-recursion child-pause disposition is "
                "not materialized for EVALUATOR_OPTIMIZER"
            ),
        ), entry_index - _resume_completed_count
    except _EvaluatorOptimizerStepDispatchError as _dispatch_failure:
        # A generate/evaluate DISPATCH raised (the ONLY pause-eligible failure — setup +
        # post-dispatch bookkeeping failures take the `except Exception` path below, NEVER
        # pause). §25.15.1 (EXTENDED to the sequential EO loop per §25.18's named impl-order)
        # governs the disposition. Only `pause` (TEAM) is materialized; `proceed` /
        # `cascade-cancel` retain EO's existing terminal-FAILED behavior. In all cases the
        # buffered completed-step entries STILL drain (no silent loss).
        exc = _dispatch_failure.original
        if cascade_policy is CascadePolicy.PAUSE:
            # B-FANOUT-PAUSE-EVALUATOR-OPTIMIZER (R-FS-1) — materialize the §25.15.1
            # `pause → PAUSED` row EXTENDED to the sequential EO loop. The completed-step
            # prefix is captured into a hash-integrity-checked iteration cursor;
            # `api.resume` re-enters here, recovers the prefix (NOT re-dispatched), and
            # re-dispatches from the failed step. The failed step buffered nothing.
            protocol = getattr(ctx, "pause_resume_protocol", None)
            if protocol is None:
                # No pause/resume opt-in bound → the snapshot cannot be captured, so a
                # PAUSED would advertise a resumability the harness cannot honor (the
                # FALSE-`PAUSED` silent-degradation mode). Fail HONESTLY
                # (detect-then-refuse), draining the completed-step buffer.
                drain_branch_buffers(ctx.ledger_writer, [writer])
                return RunResult(
                    workflow_id=workflow_id,
                    run_id=run_id,
                    status=RunStatus.FAILED,
                    terminal_step_index=None,
                    partial_state=None,
                    final_state=None,
                    fail_class=(
                        "evaluator-optimizer-pause-resume-protocol-not-bound: a step failed "
                        "under cascade_policy=pause but no pause_resume_protocol is bound "
                        "(cannot capture a resumable snapshot) — underlying: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                ), entry_index - _resume_completed_count
            # Drain the completed-step buffer BEFORE returning PAUSED so this-run
            # completions persist; the cursor captures their outputs for resume recovery.
            drain_branch_buffers(ctx.ledger_writer, [writer])
            eo_resume = EvaluatorOptimizerResumeState(
                completed_steps=tuple(completed_step_records),
            )
            snapshot = _run_protocol_method_sync(
                cast(PauseResumeProtocol, protocol).capture_pause_snapshot(
                    workflow_id=workflow_id,
                    run_id=run_id,
                    # The failed step's DECLARED ordinal (0=generate / 1=evaluate), NOT its
                    # entry_index: `step_index` must be a valid `steps` position so the
                    # runtime `api.resume` guard (`0 <= step_index < len(steps)`) admits the
                    # resume (the loop re-dispatches the same 2 steps, so entry_index grows
                    # past len(steps)). At the raise `entry_index` is the failed step's
                    # entry_index (not yet incremented), so `entry_index % 2` is its ordinal.
                    # The cursor itself (completed_steps) carries the full resume position.
                    step_index=entry_index % 2,
                    pause_reason=WorkflowPauseReason.EXPLICIT_OPERATOR,
                    evaluator_optimizer_resume=eo_resume,
                )
            )
            return RunResult(
                workflow_id=workflow_id,
                run_id=run_id,
                status=RunStatus.PAUSED,
                terminal_step_index=None,
                # PAUSED carries the salvaged loop state as partial_state + the resumable
                # iteration cursor.
                partial_state={
                    "accepted": False,
                    "iterations": iterations,
                    "output": dict(last_generate_output),
                    "evaluation": dict(last_evaluation),
                },
                final_state=None,
                fail_class=None,
                pause_snapshot=snapshot,
            ), entry_index - _resume_completed_count
        # `proceed` / `cascade-cancel` — preserve EO's existing terminal-FAILED behavior
        # (drain whatever was buffered so the completed steps' entries persist).
        drain_branch_buffers(ctx.ledger_writer, [writer])
        return RunResult(
            workflow_id=workflow_id,
            run_id=run_id,
            status=RunStatus.FAILED,
            terminal_step_index=None,
            partial_state=None,
            final_state=None,
            fail_class=_step_fail_class("evaluator-optimizer-step-failure", exc),
        ), entry_index - _resume_completed_count
    except Exception as exc:
        # A SETUP failure (dispatcher lookup / binding resolution) or a post-dispatch
        # BOOKKEEPING failure (ledger buffer / inter-step record / cursor append) — NOT a
        # resumable step-dispatch failure. This is FAILED for ALL cascade policies (never
        # PAUSED, even under TEAM/`pause`): a setup error would loop forever on resume, and
        # a bookkeeping error after a successful dispatch means the effect already landed →
        # a resume would double-fire the step (out-of-family Codex [P2]). Drain whatever was
        # buffered so the completed steps' entries persist (no silent loss).
        drain_branch_buffers(ctx.ledger_writer, [writer])
        return RunResult(
            workflow_id=workflow_id,
            run_id=run_id,
            status=RunStatus.FAILED,
            terminal_step_index=None,
            partial_state=None,
            final_state=None,
            fail_class=_step_fail_class("evaluator-optimizer-setup-or-bookkeeping-failure", exc),
        ), entry_index - _resume_completed_count

    # Clean termination (accept or cap) — drain the buffer through the single real
    # writer in execution order, then return SUCCESS. `accepted` discriminates
    # accept-terminal from cap-terminal (§25.17 lists no cap-failure mode).
    drain_branch_buffers(ctx.ledger_writer, [writer])
    return RunResult(
        workflow_id=workflow_id,
        run_id=run_id,
        status=RunStatus.SUCCESS,
        terminal_step_index=None,
        partial_state=None,
        final_state={
            "accepted": accepted,
            "iterations": iterations,
            "output": dict(last_generate_output),
            "evaluation": dict(last_evaluation),
        },
        fail_class=None,
    ), entry_index - _resume_completed_count


# ---------------------------------------------------------------------------
# U-CP-88 — ORCHESTRATOR_WORKERS driver strategy (C-CP-25 §25.11/§25.14/§25.15)
# ---------------------------------------------------------------------------
#
# The THIRD non-linear topology strategy — orchestrator-dispatch-collect fan-out
# with per-role workers (§25.11 ORCHESTRATOR_WORKERS row: "An orchestrator step
# computes a dynamic worker set, dispatches workers concurrently (per-role
# specialization via §25.14), collects. Barrier at collection; orchestrator
# composes the final result."). It is the FIRST `cascade_policy` consumer (the
# U-CP-85 machinery) AND the FIRST role-seam consumer (the U-CP-81 `agent_role`
# field + the runtime read U-RT-114).
#
# Structure (the B1 design §6 reading — `.harness/r-fs-1-b1-topology-...md`):
#   - `steps[0]` is the ORCHESTRATOR step. It is dispatched FIRST, sequentially
#     on the driver thread; its `action_id` (`workflow:{wf}:step:0`) is the
#     fan-out parent every worker branch descends from ("worker steps serialize
#     under the orchestrator's parent_action_id" — design §6). At B1 the
#     orchestrator's OUTPUT does NOT drive worker selection — there is NO
#     inter-step DATA flow (B-INTERSTEP), exactly as the linear path /
#     EVALUATOR_OPTIMIZER never thread a step's output into the next step's
#     input. The "dynamic worker set" is the declared `steps[1:]` (structural,
#     not data-driven); richer orchestrator-output-driven worker spawning is the
#     same runtime/dispatcher concern deferred at B-INTERSTEP for every topology.
#   - `steps[1:]` are the WORKERS, fanned out CONCURRENTLY, each under a per-role
#     child `StepExecutionContext` (U-CP-81 `agent_role`). The role is derived
#     from the worker's `step_id` (`AgentRole(str(step.step_id))`) — distinct per
#     worker, so with a `RoutingManifest.per_role_bindings` catalog the runtime
#     read (U-RT-114) routes each worker to its role's model (non-hollow by
#     per-role model specialization). The per-role binding CATALOG + per-step
#     override surface remains R-FS-1 child-arc B4; B1 pins the seam MECHANISM.
#   - The barrier COLLECTS; the orchestrator "composes the final result" = a
#     deterministic fold over the orchestrator output + the branch-index-ordered
#     worker outputs (§25.12 determinism — a pure function of the ORDERED set,
#     never completion order). NO second "compose" dispatch (that would need the
#     deferred inter-step data).
#
# Composes the U-CP-80..85 substrate: the dispatch table (U-CP-80); branch child
# contexts (U-CP-81); the buffered/deferred-append path + branch-index drain
# (U-CP-82); the branch_metadata causality + fresh terminal entry (U-CP-84); and
# — the new consumer — the `cascade_policy` machinery (U-CP-85): the per-policy
# run-status mapping (`cascade_policy_run_status`), the cascade-cancel
# TaskGroup barrier (`cascade_cancel_barrier`), and the in-flight-effect shield
# (`dispatch_branch_step_shielded`).
#
# `cascade_policy` (resolved from the manifest's (workload_class, engine_class,
# persona_tier) via the §11.4 D4 multiplicative tunable — NOT a manifest field)
# governs the on-WORKER-FAILURE reaction at the barrier (§25.15.1):
#   - `proceed`        → siblings RUN TO COMPLETION; the aggregator sees a
#                        partial result set → `RunStatus.PARTIAL` (degraded). A
#                        `return_exceptions`-collecting barrier (NOT
#                        `bounded_barrier`, whose finally cancels pending
#                        siblings on a failure).
#   - `cascade-cancel` → `cascade_cancel_barrier` (TaskGroup structured
#                        cancellation) cancels not-yet-dispatched siblings;
#                        in-flight effects run to completion (shielded);
#                        → `RunStatus.FAILED`.
#   - `pause`          → resumable FAN-OUT pause is NOT YET MATERIALIZED at B1
#                        (see the pause branch below): a `RunStatus.PAUSED` would
#                        advertise a resumability the position-only C-CP-26
#                        `PauseSnapshot` + the resume-blind strategy cannot honor
#                        (§25.15.2 obl. 7 resume reconstructs N-branch state from
#                        the LEDGER, and completed-branch OUTPUTS are not persisted
#                        for the aggregate merge). A worker failure under `pause`
#                        therefore fails HONESTLY → `RunStatus.FAILED` +
#                        `not-yet-materialized` fail_class (no false-`PAUSED`); the
#                        resumable-fan-out-pause build is a focused follow-on arc.
#
# The eight §25.15.2 cascade-cancel obligations are discharged: (1) dispatch-
# boundary-bounded + (8) structured cancellation by `cascade_cancel_barrier`;
# (2) no-gate-bypass + (5) high-blast-radius pre-dispatch gating by the gate
# living INSIDE `dispatcher.dispatch` (the buffered path defers only the ledger
# WRITE, never the gate — the SAME committed C-AS-02→C-CP-19→C-CP-16 gate the
# linear path uses; cascade-cancel COMPOSES it, never re-invents — §25.15.2 obl.
# 5 + §25.18 (d) "no dry_run/preview primitive"); (3) audit-completeness — every
# dispatched (in-flight) worker records its OWN step ledger entry regardless of
# terminal disposition; (4) discriminating `terminal_status` — `cancelled` =
# not-yet-dispatched (empty buffer, no effect), `completed` = the in-flight step
# ran (ran-and-errored is still `completed` — dispatch-boundary, not step-
# outcome), `timed_out` = the barrier deadline cut an in-flight step; (6) the
# run-level status mapping (`cascade_policy_run_status`); (7) resume-idempotency-
# terminality via the branch-scoped idempotency keys (U-CP-83) + the persisted
# discriminating `terminal_status` (`resume_should_redispatch`).
#
# Scope (scoped-not-forgotten): the linear-only prefix-replay / explicit-pause
# resume detection, mid-loop drain checks, and the per-step validator hook are
# NOT composed here (their units are not U-CP-88 deps). HIERARCHICAL_DELEGATION
# (U-CP-89) reuses THIS strategy recursively; DECENTRALIZED_HANDOFF (U-CP-90) is
# the single-owner sequential sibling.


def _aggregate_orchestrator_workers(
    orchestrator_output: Mapping[str, Any],
    collected: Mapping[int, tuple[str, Mapping[str, Any]]],
) -> dict[str, Any]:
    """Compose the ORCHESTRATOR_WORKERS final result — the orchestrator output +
    the branch-index-ordered worker outputs (C-CP-25 §25.11 "orchestrator
    composes the final result" + §25.12 determinism).

    The fold is a PURE function of the ORDERED (branch-index) collected set —
    never completion order ("first to finish wins" is forbidden, §25.12). At B1
    there is NO second "compose" dispatch (that would need the deferred
    inter-step DATA flow, B-INTERSTEP); the orchestrator's composition is this
    deterministic fold:

    - **`orchestrator`** — the orchestrator step's output (the fan-out parent).
    - **`worker_outputs`** — every COMPLETED worker's output keyed by its
      `step_id`, in branch-index order (cancelled / timed-out / stuck workers
      contribute nothing — their disposition lives in the persisted ledger
      `terminal_status`, not the in-memory aggregate).
    """
    sorted_items = sorted(collected.items(), key=lambda kv: kv[0])
    return {
        "orchestrator": dict(orchestrator_output),
        "worker_outputs": {step_id: dict(output) for _bi, (step_id, output) in sorted_items},
    }


def _execute_orchestrator_workers(
    *,
    manifest_entry: WorkflowManifestEntry,
    steps: Sequence[WorkflowStep],
    run_id: str,
    ctx: DriverContext,
    default_model_binding: ModelBinding,
    step_dispatchers: StepDispatcherRegistry,
    run_idempotency_key: str,
    resume_snapshot: PauseSnapshot | None = None,
    crash_fan_out_resume: FanOutResumeState | PeerFanOutResumeState | None = None,
    crash_pause_reconstruct_no_dispatch: bool = False,
    crash_pause_reconstruct_fence_paused: tuple[EffectFencePausedBranchResumeState, ...] = (),
    pause_resumable: bool = False,
    reconciler_engine_resume_required: bool = False,
    synthesis_step: WorkflowStep | None = None,
) -> tuple[RunResult, int]:
    """Execute the `ORCHESTRATOR_WORKERS` orchestrator-dispatch-collect strategy (U-CP-88).

    `steps[0]` is the orchestrator (dispatched first, sequentially; its
    `action_id` parents the worker fan-out); `steps[1:]` are workers fanned out
    concurrently under per-role child contexts. The barrier collects per the
    resolved `cascade_policy` (proceed → PARTIAL / cascade-cancel → FAILED /
    pause → PAUSED on a worker failure; SUCCESS when every worker completes);
    the orchestrator composes a deterministic fold. Returns
    `(RunResult, steps_executed)` for the `_execute_workflow_body` caller
    (matching the linear path's tuple). See the module block above for the full
    §25.11/§25.14/§25.15 obligation discharge.

    **B-FANOUT-PAUSE (R-FS-1) — resumable `cascade_policy=pause` fan-out.** When
    `resume_snapshot` carries a `fan_out_resume` (an `api.resume` of a prior
    `pause` halt), this re-enters in RESUME mode: the orchestrator is NOT
    re-dispatched (its output is recovered from the snapshot — it ran originally,
    effect may have landed), terminal worker branches are SKIPPED (§25.15.2
    obligation 7 — their outputs recovered into the aggregate), and only the
    not-yet-dispatched (left-re-dispatchable) workers fan out again under the
    same `cascade_policy`. A worker failing AGAIN under `pause` re-pauses with a
    fresh snapshot whose `branches` union the recovered + newly-terminal set.

    `pause_resumable` (default False) gates the resumable `pause → PAUSED` return.
    Both the TOP-LEVEL `ORCHESTRATOR_WORKERS` strategy AND `HIERARCHICAL_DELEGATION`
    (which REUSES this helper at each recursion level) now thread `pause_resumable=True`
    + the `resume_snapshot` (B-HIERARCHICAL-PAUSE, R-FS-1), so a level-local worker
    pause materializes via `FanOutResumeState`. The default `False` is retained for any
    UNWIRED caller — it fails HONESTLY (`...-not-yet-materialized`) rather than advertise
    a false-resumable PAUSED.

    **B-HIERARCHICAL-PAUSE — recursive child PAUSE.** A `SUB_AGENT_DISPATCH` worker
    whose child sub-workflow itself PAUSED (a grandchild failing under `cascade_policy=
    pause`) raises `SubAgentChildPausedError`; that worker is captured into the snapshot's
    `paused_child_branches` (NOT `branches` — it is re-dispatchable, not terminal),
    carrying the child's nested `PauseSnapshot`. On resume the worker is re-dispatched
    WITH the child snapshot threaded on its `StepExecutionContext.child_resume_snapshot`,
    so the child re-enters at its own cursor (the grandchild's completed steps are
    recovered, NOT re-executed) — the THIRD branch disposition (skip-terminal /
    re-dispatch-fresh / re-enter-child-at-cursor). A child PAUSE under `proceed` /
    `cascade-cancel` (no resumable boundary) FAILS honestly, never silently dropping the
    suspended child.
    """
    workflow_id = manifest_entry.workflow_id

    # B-FANOUT-PAUSE — resume reconstruction state (None on a normal first run).
    # B-FANOUT-OUTPUT-REPLAY (R-FS-1) — crash-resume threads the synthetic resume state
    # through the SAME `_fan_out_resume` local the pause path uses (only the snapshot
    # SOURCE differs; the `_is_resume` orchestrator-recover + skip-terminal-worker +
    # re-dispatch-incomplete path below is reused VERBATIM). Never co-set with the pause
    # snapshot (computed only when `resume_snapshot is None`); the assert pins it.
    assert resume_snapshot is None or crash_fan_out_resume is None
    _fan_out_crash_resume = (
        crash_fan_out_resume if isinstance(crash_fan_out_resume, FanOutResumeState) else None
    )
    _fan_out_resume = (
        resume_snapshot.fan_out_resume if resume_snapshot is not None else _fan_out_crash_resume
    )
    _is_resume = _fan_out_resume is not None
    reconciler_engine_resume_attempted = False
    # branch_index -> recovered terminal disposition (carried forward across
    # repeated resumes so a re-pause snapshot unions prior + this-round terminals).
    _recovered_terminal: dict[int, FanOutBranchResumeState] = (
        {b.branch_index: b for b in _fan_out_resume.branches} if _fan_out_resume is not None else {}
    )
    # B-HIERARCHICAL-PAUSE — branch_index -> the child sub-workflow's PauseSnapshot for
    # each worker whose recursive child PAUSED. These are NOT skipped on resume (unlike
    # terminal branches): the worker is RE-DISPATCHED with its child's snapshot threaded
    # so the child re-enters at its cursor (the THIRD branch disposition). Each is
    # re-dispatched THIS round → it either completes (→ terminal), fails, or pauses again
    # (→ recaptured into this round's paused_child set), so the set is rebuilt fresh per
    # round (no carry-seed — re-dispatch IS the carry).
    _recovered_paused_child: dict[int, PauseSnapshot] = (
        {b.branch_index: b.child_snapshot for b in _fan_out_resume.paused_child_branches}
        if _fan_out_resume is not None
        else {}
    )
    # B-FANOUT-EFFECT-FENCE-BRANCH-PAUSE — branch_index -> the held reserve's idempotency_key
    # for each TOOL_STEP worker whose OWN dispatch raised the effect fence. Like a paused child,
    # these are NOT skipped on resume: the worker is RE-DISPATCHED with the operator's
    # `EffectFenceResolution` threaded (key-bound to this branch's reserve) so the runtime
    # fence applies exactly one key-matched resolution (SKIP_AS_FIRED / RE_FIRE / ABORT) — NOT
    # a fresh dispatch. Rebuilt fresh per round (re-dispatch IS the carry). An empty key ("")
    # means the captured error had none → no directive → the fence re-pauses INERT.
    _recovered_effect_fence_paused: dict[int, str] = (
        {b.branch_index: b.idempotency_key for b in _fan_out_resume.effect_fence_paused_branches}
        if _fan_out_resume is not None
        else {}
    )
    # B-FANOUT-EFFECT-FENCE-BRANCH-PAUSE / B-FANOUT-EFFECT-FENCE-PER-BRANCH-RESOLUTION — PEEK
    # (NOT consume — the HITL composer's one-shot consume stays intact, mirroring the linear path)
    # the operator's resume context off the holder. Per paused worker below,
    # `effect_fence_resolution_for(branch_key)` returns that branch's `effect_fence_resolutions`
    # map entry if supplied, else the uniform `effect_fence_resolution` default — so two paused
    # workers can resolve DIFFERENTLY in one resume (SKIP_AS_FIRED vs RE_FIRE; ABORT keeps its
    # run-level-terminal semantic). None when no holder / no resolution for a branch's key → that
    # paused worker re-pauses INERT (never an auto-re-fire — the #701 decline-mirror).
    _ef_resume_ctx: ResumeContext | None = None
    # B-FANOUT-CRASH-RESUME-ORCHESTRATOR-MAYBE-RAN-EFFECT-BEARING — ALSO peek the holder when the
    # ORCHESTRATOR itself fence-paused (its pause carries no worker fence branches, so
    # `_recovered_effect_fence_paused` is empty; without this the orchestrator resolution would
    # never be read → the orchestrator would re-pause INERT forever).
    _orch_fence_resume_present = (
        resume_snapshot is not None and resume_snapshot.orchestrator_effect_fence_resume is not None
    )
    if _recovered_effect_fence_paused or _orch_fence_resume_present:
        _ef_holder = cast(
            "_ResumeContextHolderLike | None",
            getattr(ctx, "resume_context_holder", None),
        )
        _ef_resume_ctx = _ef_holder.peek() if _ef_holder is not None else None

    # B-FANOUT-EFFECT-FENCE-PER-BRANCH-RESOLUTION — run-level ABORT guard (out-of-family Codex
    # [P1]). If the operator ABORTs ANY fence-paused worker, the run WILL fail, so NO sibling
    # continue-resolution (SKIP/RE_FIRE) may FIRE first (a RE_FIRE sibling would otherwise
    # clear+re-fire concurrently before the ABORT branch fails the run). Suppress the non-ABORT
    # siblings' directives below; the ABORT worker keeps its directive → EffectFenceAbortedError
    # → terminal FAILED. The PARALLELIZATION analogue.
    _any_fence_abort = _ef_resume_ctx is not None and any(
        _ef_resume_ctx.effect_fence_resolution_for(_k) is EffectFenceResolution.ABORT
        for _k in _recovered_effect_fence_paused.values()
    )

    # B-FANOUT-CRASH-RESUME-ORCHESTRATOR-MAYBE-RAN-EFFECT-BEARING (out-of-family Codex [P2]) — an
    # orchestrator effect-fence resume whose body was CHANGED to empty (`steps == []`) must FAIL
    # CLOSED, not hit the empty-steps SUCCESS fast path below: the snapshot carries an unresolved
    # ambiguous orchestrator effect (`orchestrator_effect_fence_resume`), and a removed `steps[0]`
    # would silently abandon BOTH that effect and the operator's resolution. The
    # changed-orchestrator guard at the dispatch site (which reads `steps[0]`) cannot fire when
    # there is no `steps[0]`, so this is its empty-body analogue.
    if not steps and _orch_fence_resume_present:
        return RunResult(
            workflow_id=workflow_id,
            run_id=run_id,
            status=RunStatus.FAILED,
            terminal_step_index=None,
            partial_state=None,
            final_state=None,
            fail_class="fan-out-orchestrator-effect-fence-resume-changed-orchestrator",
        ), 0
    # Empty step sequence → trivially SUCCESS (mirrors the linear empty-loop +
    # the PARALLELIZATION / EVALUATOR_OPTIMIZER empty-steps SUCCESS).
    if not steps:
        if reconciler_engine_resume_required:
            (
                _reconciler_resume_fail,
                reconciler_engine_resume_attempted,
            ) = _attempt_reconciler_engine_resume_gate(
                ctx=ctx,
                manifest_entry=manifest_entry,
                run_id=run_id,
                step_id="fanout-crash-resume",
            )
            if _reconciler_resume_fail is not None:
                return _reconciler_resume_fail, 0
        result = RunResult(
            workflow_id=workflow_id,
            run_id=run_id,
            status=RunStatus.SUCCESS,
            terminal_step_index=None,
            partial_state=None,
            final_state={"orchestrator": {}, "worker_outputs": {}},
            fail_class=None,
        )
        if reconciler_engine_resume_attempted:
            ctx.lifecycle_emitter.emit(WorkflowEventClass.RESUMPTION)
            _record_reconciler_fanout_resume_finalized(ctx, manifest_entry, run_idempotency_key)
        return result, 0

    orchestrator_step = steps[0]
    worker_steps = list(steps[1:])

    # B-FANOUT-PAUSE — material-diff guard on resume: the re-supplied workflow's
    # worker count MUST match the count captured at pause, else the recovered
    # branch ordinals no longer map to these steps (a changed body) → fail closed
    # rather than re-dispatch a mismatched set.
    if _fan_out_resume is not None:

        def _resume_body_mismatch() -> str | None:
            # orchestrator identity (its output is recovered + dispatch skipped —
            # Codex [P2]: a renamed/reordered steps[0] would apply stale output) ...
            if str(orchestrator_step.step_id) != _fan_out_resume.orchestrator_step_id:
                return (
                    f"orchestrator-identity-mismatch: snapshot step_id="
                    f"{_fan_out_resume.orchestrator_step_id!r}, resume step_id="
                    f"{str(orchestrator_step.step_id)!r}"
                )
            # worker_count (the gross shape) ...
            if len(worker_steps) != _fan_out_resume.worker_count:
                return (
                    f"worker-count-mismatch: snapshot captured "
                    f"{_fan_out_resume.worker_count} workers, resume supplied "
                    f"{len(worker_steps)}"
                )
            # ... then per-recovered-branch BOUNDS + IDENTITY + no-duplicates
            # (Codex [P1]: a same-count reorder / rename would otherwise attribute
            # a recovered output to the wrong step → silent stale output). Full
            # anchor-reachability is the deferred U-CP-22 arc; this is the cheap
            # positional-identity guard, fail-closed on any drift.
            seen: set[int] = set()
            for b in _fan_out_resume.branches:
                if not (0 <= b.branch_index < len(worker_steps)):
                    return f"branch-index-out-of-range: {b.branch_index} ∉ [0, {len(worker_steps)})"
                if b.branch_index in seen:
                    return f"duplicate-branch-index: {b.branch_index}"
                seen.add(b.branch_index)
                if str(worker_steps[b.branch_index].step_id) != b.step_id:
                    return (
                        f"branch-identity-mismatch at {b.branch_index}: snapshot "
                        f"step_id={b.step_id!r}, resume step_id="
                        f"{str(worker_steps[b.branch_index].step_id)!r}"
                    )
            # B-HIERARCHICAL-PAUSE — paused-child branches: same BOUNDS + IDENTITY guard,
            # PLUS no-overlap with the terminal `branches` (`seen`) — a paused-child
            # ordinal is the disjoint THIRD disposition, so a snapshot listing the same
            # ordinal as both terminal AND paused-child is corrupt (fail closed).
            for pc in _fan_out_resume.paused_child_branches:
                if not (0 <= pc.branch_index < len(worker_steps)):
                    return (
                        f"paused-child-index-out-of-range: {pc.branch_index} "
                        f"∉ [0, {len(worker_steps)})"
                    )
                if pc.branch_index in seen:
                    return f"paused-child-overlaps-terminal-or-duplicate: {pc.branch_index}"
                seen.add(pc.branch_index)
                if str(worker_steps[pc.branch_index].step_id) != pc.step_id:
                    return (
                        f"paused-child-identity-mismatch at {pc.branch_index}: snapshot "
                        f"step_id={pc.step_id!r}, resume step_id="
                        f"{str(worker_steps[pc.branch_index].step_id)!r}"
                    )
            # B-FANOUT-EFFECT-FENCE-BRANCH-PAUSE — effect-fence-paused branches: same
            # BOUNDS + IDENTITY guard, PLUS no-overlap with BOTH the terminal `branches`
            # AND the paused-child set (`seen` accumulates both) — a fence-paused ordinal
            # is the disjoint FOURTH disposition, so any ordinal listed as more than one of
            # {terminal, paused-child, fence-paused} is a corrupt snapshot (fail closed).
            for ef in _fan_out_resume.effect_fence_paused_branches:
                if not (0 <= ef.branch_index < len(worker_steps)):
                    return (
                        f"effect-fence-paused-index-out-of-range: {ef.branch_index} "
                        f"∉ [0, {len(worker_steps)})"
                    )
                if ef.branch_index in seen:
                    return (
                        f"effect-fence-paused-overlaps-other-disposition-or-duplicate: "
                        f"{ef.branch_index}"
                    )
                seen.add(ef.branch_index)
                if str(worker_steps[ef.branch_index].step_id) != ef.step_id:
                    return (
                        f"effect-fence-paused-identity-mismatch at {ef.branch_index}: snapshot "
                        f"step_id={ef.step_id!r}, resume step_id="
                        f"{str(worker_steps[ef.branch_index].step_id)!r}"
                    )
                # B-FANOUT-EFFECT-FENCE-BRANCH-PAUSE — the resumed worker's kind MUST match the
                # CAPTURED kind (always `tool-step` in production — the fence source); a
                # kept-step_id-but-changed-kind resume would thread the resolution into a dispatcher
                # that never reaches the fence → fail closed (the live-pause analogue of the §2
                # changed-kind guard; Codex [P1] R2).
                if str(worker_steps[ef.branch_index].step_kind.value) != ef.step_kind:
                    return (
                        f"effect-fence-paused-kind-changed at {ef.branch_index}: snapshot kind="
                        f"{ef.step_kind!r}, resume kind="
                        f"{str(worker_steps[ef.branch_index].step_kind.value)!r} — the resolution "
                        "would not reach the fence (only the captured-kind dispatch does)"
                    )
            return None

        _mismatch = _resume_body_mismatch()
        if _mismatch is not None:
            return RunResult(
                workflow_id=workflow_id,
                run_id=run_id,
                status=RunStatus.FAILED,
                terminal_step_index=None,
                partial_state=None,
                final_state=None,
                fail_class=f"orchestrator-workers-resume-{_mismatch}",
            ), 0

    # Resolve cascade policy before the RECONCILER CAS gate so invalid effect-fence pause resumes
    # under PROCEED fail without consuming the one-shot engine resume claim.
    cascade_policy = d4_tunable(
        lookup_cell(manifest_entry.workload_class, manifest_entry.engine_class),
        manifest_entry.persona_tier,
    ).cascade_policy
    if _recovered_effect_fence_paused and cascade_policy is CascadePolicy.PROCEED:
        return RunResult(
            workflow_id=workflow_id,
            run_id=run_id,
            status=RunStatus.FAILED,
            terminal_step_index=None,
            partial_state=None,
            final_state=None,
            fail_class="orchestrator-workers-effect-fence-resume-requires-strict-tier",
        ), 0

    if reconciler_engine_resume_required:
        _synth_replay_validation_fail = _captured_synthesis_replay_validation_failure(
            ctx=ctx,
            manifest_entry=manifest_entry,
            run_idempotency_key=run_idempotency_key,
            synthesis_step=synthesis_step,
            branch_count=len(steps),
            run_id=run_id,
        )
        if _synth_replay_validation_fail is not None:
            return _synth_replay_validation_fail, 0
        (
            _reconciler_resume_fail,
            reconciler_engine_resume_attempted,
        ) = _attempt_reconciler_engine_resume_gate(
            ctx=ctx,
            manifest_entry=manifest_entry,
            run_id=run_id,
            step_id="fanout-crash-resume",
        )
        if _reconciler_resume_fail is not None:
            return _reconciler_resume_fail, 0

    def _finalize_reconciler_cas_if_attempted() -> None:
        if reconciler_engine_resume_attempted:
            _record_reconciler_fanout_resume_finalized(ctx, manifest_entry, run_idempotency_key)

    # The on-worker-failure cascade reaction (§25.15.1) — resolved above from the
    # manifest's (workload_class, engine_class, persona_tier) via the §11.4 D4
    # multiplicative tunable (`cascade_policy` is NOT a WorkflowManifestEntry
    # field; it is the D4-layer tunable default — SOLO→proceed / TEAM→pause /
    # MTC→cascade-cancel).
    # R-003 active-workflow-context sidecar (resolved once; the same resolver the
    # linear `_append_step_ledger_entry` reads). None when no resolver is bound.
    _resolver = getattr(ctx, "procedural_tier_snapshot_resolver", None)
    snapshot_ref = _resolver() if _resolver is not None else None

    # Buffer-time placeholder timestamp for every entry (orchestrator + workers);
    # the authoritative append timestamp is assigned at the drain
    # (`drain_branch_buffers` re-stamps to one drain-moment value — the
    # IS-monotonicity realization, see the module timestamp-discipline note above
    # `append_branch_step_ledger_entry`).
    fanout_timestamp = datetime.now(UTC)

    # § 25.3.2 — Emit workflow.start (single-threaded on the driver thread, BEFORE
    # any dispatch). B-FANOUT-PAUSE: a fan-out RESUME emits RESUMPTION instead
    # (mirrors the linear resume-path RESUMPTION emit) — the orchestrator + the
    # terminal workers already ran in the original envelope.
    ctx.lifecycle_emitter.emit(
        WorkflowEventClass.RESUMPTION
        if (_is_resume or reconciler_engine_resume_attempted)
        else WorkflowEventClass.WORKFLOW_START
    )

    # --- 1) the orchestrator step (sequential; its action_id parents the fan-out) ---
    # `workflow:{wf}:step:0` is the orchestrator's action_id AND the fan-out
    # parent every worker descends from (compose_branch_child_context carries it
    # verbatim). A plain sequential entry (NO branch_metadata — the orchestrator
    # is the parent, not itself a branch).
    orchestrator_action_id = f"workflow:{workflow_id}:step:0"
    orchestrator_idempotency_key = _compute_step_idempotency_key(run_idempotency_key, 0)
    # B-FANOUT-PAUSE — None on resume (the orchestrator already ran; no writer /
    # no re-appended entry). The real writer is created in the first-run branch.
    orchestrator_writer: BufferingLedgerWriter | None = None
    # B-FANOUT-CRASH-RESUME-ORCHESTRATOR-MAYBE-RAN-EFFECT-BEARING (R-FS-1) — the NEW
    # resume→orchestrator application site. A snapshot carrying
    # `orchestrator_effect_fence_resume` resumes an ORCHESTRATOR whose OWN dispatch
    # fence-paused (effect-bearing maybe-ran). UNLIKE a normal fan-out resume
    # (`fan_out_resume` present → `_is_resume` True → the orchestrator is SKIPPED, its
    # output recovered), here NOTHING ran: `_fan_out_resume` is None → `_is_resume` False
    # → the orchestrator is RE-DISPATCHED through the `else` branch below, WITH the
    # operator's resolution key-bound to its reserve so the runtime fence applies exactly
    # one key-matched resolution (the linear/worker `effect_fence_resolution` threading,
    # but on the orchestrator context). Guards run FIRST (fail-closed BEFORE any dispatch).
    _orch_fence_resume = (
        resume_snapshot.orchestrator_effect_fence_resume if resume_snapshot is not None else None
    )
    _orch_effect_fence_directive: EffectFenceResolutionDirective | None = None
    if _orch_fence_resume is not None:
        if (
            str(orchestrator_step.step_id) != _orch_fence_resume.step_id
            or orchestrator_step.step_kind.value != _orch_fence_resume.step_kind
        ):
            # Changed-orchestrator guard (positional-identity + changed-kind): the operator
            # renamed/reordered or changed the kind of `steps[0]` between pause and resume →
            # threading the resolution would reach the WRONG (or no) fence and silently
            # abandon the original ambiguous effect. Fail closed (the worker
            # `EffectFencePausedBranchResumeState` step_id/step_kind guard analogue).
            _finalize_reconciler_cas_if_attempted()
            return RunResult(
                workflow_id=workflow_id,
                run_id=run_id,
                status=RunStatus.FAILED,
                terminal_step_index=None,
                partial_state=None,
                final_state=None,
                fail_class="fan-out-orchestrator-effect-fence-resume-changed-orchestrator",
            ), 0
        _orch_resolution = (
            _ef_resume_ctx.effect_fence_resolution_for(_orch_fence_resume.idempotency_key)
            if (_orch_fence_resume.idempotency_key and _ef_resume_ctx is not None)
            else None
        )
        if _orch_resolution is EffectFenceResolution.SKIP_AS_FIRED:
            # SKIP_AS_FIRED REJECTED for an orchestrator: it would yield EMPTY orchestrator
            # output, which `_aggregate_orchestrator_workers` would silently fold into a
            # DEGENERATE aggregate (a no-silent-failure violation — the workers run, but the
            # orchestrator's structuring contribution is silently gone). The palette for an
            # orchestrator fence pause is RE_FIRE / ABORT; fail loud, never under-execute.
            _finalize_reconciler_cas_if_attempted()
            return RunResult(
                workflow_id=workflow_id,
                run_id=run_id,
                status=RunStatus.FAILED,
                terminal_step_index=None,
                partial_state=None,
                final_state=None,
                fail_class="fan-out-orchestrator-effect-fence-resume-skip-as-fired-unsupported",
            ), 0
        # RE_FIRE / ABORT → thread the key-bound directive onto the orchestrator dispatch
        # (the runtime fence consumes it: RE_FIRE clears the claim + re-fires fresh; ABORT
        # raises EffectFenceAbortedError → FAILED via the generic dispatch except). None →
        # the fence re-pauses INERT (never an auto-re-fire), the decline-mirror.
        if _orch_resolution is not None:
            _orch_effect_fence_directive = EffectFenceResolutionDirective(
                resolution=_orch_resolution,
                idempotency_key=_orch_fence_resume.idempotency_key,
            )
    orchestrator_context = StepExecutionContext(
        workflow_id=workflow_id,
        parent_action_id=orchestrator_action_id,
        parent_gate_level=resolve_parent_gate_level(manifest_entry),
        # B-HITL-PLACEMENT-PER-STEP-PRODUCER — orchestrator step + workers
        # (workers inherit via compose_branch_child_context's model_copy).
        # B-HITL-PLACEMENT-PER-STEP-OVERRIDE-FOLD (CP spec v1.49 §6.2) — fold the
        # orchestrator step's OWN per-step placement override (binding not yet
        # resolved here; use the read helper, mirroring agent_role at line below).
        # Workers re-fold from manifest_entry.hitl_placements, so this orchestrator
        # override does NOT leak to the fan-out children.
        hitl_placements=fold_step_hitl_placements(
            manifest_entry.hitl_placements,
            _per_step_hitl_placement_override(manifest_entry, orchestrator_step.step_id),
        ),
        # B-EFFECT-FENCE-DURABLE-AUTO — the RUN engine class (NOT a per-step
        # StepOverride.engine_class) so the tool dispatcher auto-fences durable runs.
        run_engine_class=manifest_entry.engine_class,
        parent_sandbox_tier=SandboxTier.TIER_1_PROCESS,
        parent_actor=ctx.ledger_writer.actor,
        parent_entry_hash="",
        parent_idempotency_key=orchestrator_idempotency_key,
        tenant_id=ctx.tenant_id,
        step_index=0,
        # B4 Slice 4 — a per-step ROLE override on the orchestrator step itself
        # (it carries no derived role — coordinator default). Folded here so the
        # override applies to the orchestrator's OWN dispatch; workers re-set
        # their own role via compose_branch_child_context, so this does not leak
        # to the fan-out children (CP spec v1.38 §6.1).
        agent_role=_per_step_role_override(manifest_entry, orchestrator_step.step_id),
        # B-FANOUT-CRASH-RESUME-ORCHESTRATOR-MAYBE-RAN-EFFECT-BEARING — the key-bound
        # operator resolution for an orchestrator effect-fence resume (None on every
        # non-fence-resume run → hash-inert, byte-identical to the pre-arc context).
        effect_fence_resolution=_orch_effect_fence_directive,
        # B-FANOUT-CRASH-RESUME-ORCHESTRATOR-MAYBE-RAN-SUBAGENT — mark THIS context as
        # the orchestrator's own dispatch so the runtime extends the deterministic
        # `child_run_id_seed` to a SUB_AGENT_DISPATCH orchestrator (recoverable child
        # auto-resumes on a crash re-dispatch). Workers RESET it (compose_branch_child_
        # context) so they keep their per-branch seed; hash-inert (byte-identical when
        # the orchestrator is not a recoverable SUB_AGENT_DISPATCH).
        is_orchestrator_dispatch=True,
    )
    if _is_resume:
        # B-FANOUT-PAUSE — the orchestrator already ran in the ORIGINAL envelope
        # (effect may have landed); recover its output, do NOT re-dispatch and do
        # NOT re-append its ledger entry (this fresh-bootstrap resume ledger is a
        # continuation — the linear position-only resume model, R-CC-1 design §1.1,
        # now extended to carry the fan-out's already-run outputs in the snapshot).
        assert _fan_out_resume is not None  # guarded by `_is_resume`
        orchestrator_output: Mapping[str, Any] = dict(_fan_out_resume.orchestrator_output)
        # B-FANOUT-OUTPUT-REPLAY — on a CRASH-resume the orchestrator's OWN sequential
        # ledger entry was LOST (the crash drained nothing), so re-materialize it
        # (dedup-safe; out-of-family Codex [P1]). A PAUSE-resume's entry was already
        # durable → skip (the comment above). Sets `orchestrator_writer` so it drains.
        if _fan_out_crash_resume is not None:
            orchestrator_writer = BufferingLedgerWriter(
                actor=ctx.ledger_writer.actor, branch_index=0
            )
            # B-FANOUT-OUTPUT-REPLAY — re-materialize the orchestrator's per-step override
            # provenance too (out-of-family Codex [P2]; the fresh `else` branch buffers it
            # before the sequential entry — match it so a crash-resumed override-bearing
            # orchestrator carries the same provenance a no-crash run does). Dedup-safe.
            _orch_resume_binding = resolve_step_binding(
                manifest_entry,
                str(orchestrator_step.step_id),
                default_model_binding=default_model_binding,
                persona_tier=manifest_entry.persona_tier,
            )
            _buffer_branch_override_if_applied(
                branch_writer=orchestrator_writer,
                workflow_id=workflow_id,
                step=orchestrator_step,
                binding=_orch_resume_binding,
                timestamp=fanout_timestamp,
                snapshot_ref=snapshot_ref,
            )
            _append_buffered_sequential_entry(
                writer=orchestrator_writer,
                workflow_id=workflow_id,
                entry_index=0,
                idempotency_key=orchestrator_idempotency_key,
                timestamp=fanout_timestamp,
                procedural_tier_snapshot_ref=snapshot_ref,
            )
    else:
        orchestrator_writer = BufferingLedgerWriter(actor=ctx.ledger_writer.actor, branch_index=0)
        # Resolve the binding (pure) + buffer the orchestrator step's own per-step
        # override entry BEFORE dispatch — uniform with the parallelization / worker /
        # evaluator-optimizer sites + the linear path (the override is a
        # resolution-time binding fact, recorded before dispatch, gated on
        # binding.override_applied). B-NONLINEAR-OVERRIDE-PROVENANCE.
        orchestrator_binding = resolve_step_binding(
            manifest_entry,
            str(orchestrator_step.step_id),
            default_model_binding=default_model_binding,
            persona_tier=manifest_entry.persona_tier,
        )
        _buffer_branch_override_if_applied(
            branch_writer=orchestrator_writer,
            workflow_id=workflow_id,
            step=orchestrator_step,
            binding=orchestrator_binding,
            timestamp=fanout_timestamp,
            snapshot_ref=snapshot_ref,
        )
        # B-FANOUT-CRASH-RESUME-ORCHESTRATOR-DISPATCH (R-FS-1) — reserve-before-DISPATCH for
        # the orchestrator's OWN sequential dispatch. The orchestrator (`steps[0]`) fires FIRST;
        # a crash in its fire→capture window (after the `dispatch()` below, before the
        # `record_orchestrator` reserve-before-COMMIT capture) leaves no orchestrator record +
        # no cardinality marker, so a fresh re-run would re-dispatch `steps[0]` → a double-fire
        # on the strict tiers. Resolve the dispatcher FIRST — a PURE registry lookup, no effect —
        # then write the orchestrator dispatched marker (fsynced) STRICTLY BETWEEN the lookup and
        # the `dispatch()`. Writing the marker AFTER the lookup is load-bearing: a lookup failure
        # (unknown `step_kind`) raises BEFORE any effect could fire, so it must leave NO marker —
        # else a false marker would poison a later same-run-key recovery into a spurious fail-
        # closed (out-of-family Codex [P2]). marker-absent ⟺ the orchestrator's effect did NOT
        # fire. The marker is a NEW file written ONLY here, so its PRESENCE alone is the resume-
        # side fail-closed signal (the cross-version guard is INHERENT — a pre-arc journal carries
        # no orchestrator marker; the resume classifier needs no stamp gate for presence). Strict
        # tiers still write the existing dispatch-instrumented stamp here because their pristine-
        # window allowlist already uses it to validate re-fire-safe / fence-recoverable marker
        # contents before any worker fan-out starts. PROCEED deliberately does NOT write that
        # worker-marker trust stamp: PROCEED worker paths still emit no per-branch dispatch markers,
        # so stamping a PROCEED orchestrator-only run would make a later strict-tier resume trust
        # absent worker markers that were never instrumented. lookup → marker → dispatch is
        # SYNCHRONOUS (no `ensure_future` / yield), so the marker still strictly precedes the effect
        # with no interleave — no false-positive marker without the worker path's atomicity dance. A
        # store-write failure during the reserve is caught below → FAILED (conservative —
        # at-most-once can't be guaranteed without the marker). PROCEED writes the same marker now:
        # worker failures still harvest survivors as PARTIAL, but an effect-bearing orchestrator
        # pre-capture crash must flow through the maybe-ran classifier instead of accepting a
        # double-fire window.
        # `_fanout_replay_store` handle reused.
        _orch_replay_store = _fanout_replay_store(ctx, manifest_entry)
        try:
            _orch_dispatcher = step_dispatchers.lookup(orchestrator_step.step_kind)
            if _orch_replay_store is not None:
                if cascade_policy is not CascadePolicy.PROCEED:
                    _orch_replay_store.record_dispatch_instrumented(run_idempotency_key)
                # B-FANOUT-CRASH-RESUME-ORCHESTRATOR-MAYBE-RAN-RESOLUTION (R-FS-1, CP spec v1.64
                # §1/§2) — record the orchestrator's DISPATCH-TIME step kind in the reserve
                # marker so the resume-side maybe-ran classifier keys on the ORIGINAL kind (the
                # at-most-once changed-manifest guard), mirroring the worker
                # `record_branch_dispatched(... str(step_kind.value))` caller.
                # B-FANOUT-CRASH-RESUME-ORCHESTRATOR-MAYBE-RAN-SUBAGENT — ALSO record whether a
                # SUB_AGENT_DISPATCH orchestrator's child was RE-DISPATCH-RECOVERABLE at dispatch
                # (durable LINEAR or supported fan-out topology, recursively recoverable; the
                # SAME `_subagent_child_recoverable` predicate the worker uses) AND the
                # DISPATCH-TIME child engine class (the cross-engine-class swap guard,
                # out-of-family Codex [P1], …-RECONCILER-CHILD arc). `None` (non-SUB_AGENT
                # orchestrator) → both additive
                # kwargs are OMITTED entirely (out-of-family Codex [P2]: a store implementing the
                # pre-v1.86 3-arg signature must not see a `child_recoverable=None` kwarg →
                # TypeError), mirroring the worker `_mark_branch_dispatched`
                # only-pass-when-not-None compatibility. The DISPATCH-TIME value (the at-most-once
                # changed-manifest guard; the resumed-side half is
                # `_subagent_child_recoverable(steps[0])` +
                # `_subagent_child_engine_class(steps[0])`). PROCEED adds `proceed_unstamped=True`
                # because it intentionally withholds the worker dispatch-instrumented stamp; that
                # narrow provenance lets an effect-free maybe-ran orchestrator recover without
                # trusting arbitrary orphaned unstamped markers. Each additive kwarg is filtered
                # by the bound writer signature: older duck-typed stores still write the historical
                # marker and then fail closed on any later ambiguous resume instead of TypeErroring
                # before dispatch.
                _orch_child_recoverable = _subagent_child_recoverable(orchestrator_step)
                _orch_child_engine_class = _subagent_child_engine_class(orchestrator_step)
                _orch_marker_writer = _orch_replay_store.record_orchestrator_dispatched
                _orch_marker_kwargs: dict[str, object] = {}
                if _orch_child_recoverable is not None and _callable_accepts_keyword(
                    _orch_marker_writer, "child_recoverable"
                ):
                    _orch_marker_kwargs["child_recoverable"] = _orch_child_recoverable
                if _orch_child_engine_class is not None and _callable_accepts_keyword(
                    _orch_marker_writer, "child_engine_class"
                ):
                    _orch_marker_kwargs["child_engine_class"] = _orch_child_engine_class
                if cascade_policy is CascadePolicy.PROCEED and _callable_accepts_keyword(
                    _orch_marker_writer, "proceed_unstamped"
                ):
                    _orch_marker_kwargs["proceed_unstamped"] = True
                if _orch_marker_kwargs:
                    _orch_marker_writer(
                        run_idempotency_key,
                        str(orchestrator_step.step_id),
                        str(orchestrator_step.step_kind.value),
                        **_orch_marker_kwargs,
                    )
                else:
                    _orch_marker_writer(
                        run_idempotency_key,
                        str(orchestrator_step.step_id),
                        str(orchestrator_step.step_kind.value),
                    )
            orchestrator_output = _orch_dispatcher.dispatch(
                orchestrator_binding, orchestrator_step, step_context=orchestrator_context
            )
        except Exception as exc:
            # B-FANOUT-CRASH-RESUME-ORCHESTRATOR-MAYBE-RAN-EFFECT-BEARING (R-FS-1) — the
            # orchestrator's OWN dispatch raised the runtime effect fence's
            # `EffectFenceAmbiguousUncommittedError` (its effect-bearing maybe-ran re-dispatch
            # re-reached the auto-active fence, which lost the reserve + found no captured
            # output, so whether the orchestrator's effect fired is genuinely ambiguous).
            # Name-matched (harness-cp cannot import harness-runtime, mirroring the linear
            # B-EFFECT-FENCE-HITL-ROUTE). Compose a §26.2 EFFECT_FENCE_AMBIGUOUS pause — the
            # orchestrator analogue of the linear route — when a PauseResumeProtocol is bound +
            # the run is pause-resumable + a STRICT tier (PROCEED has no resolution handling →
            # its resume would degrade the operator's decision). Else fall through to the FAILED
            # return below (behaviorally the pre-arc fail-closed; only the disposition differs).
            # An `EffectFenceAbortedError` (an operator ABORT applied on resume) is NOT
            # name-matched here → it falls through to FAILED (terminal, never a re-pause).
            _orch_pause_protocol = getattr(ctx, "pause_resume_protocol", None)
            _orch_fence_key = getattr(exc, "idempotency_key", None)
            if (
                type(exc).__name__ == "EffectFenceAmbiguousUncommittedError"
                and _orch_pause_protocol is not None
                and pause_resumable
                and cascade_policy is not CascadePolicy.PROCEED
                and isinstance(_orch_fence_key, str)
                and _orch_fence_key
            ):
                # Do NOT drain `orchestrator_writer` here (unlike the terminal FAILED path): on
                # resume the orchestrator re-dispatches + re-buffers its per-step override entry,
                # so persisting it now would DOUBLE it. The reserve marker is already fsynced.
                _orch_pause_snapshot = _run_protocol_method_sync(
                    cast(PauseResumeProtocol, _orch_pause_protocol).capture_pause_snapshot(
                        workflow_id=workflow_id,
                        run_id=run_id,
                        step_index=0,
                        pause_reason=WorkflowPauseReason.EFFECT_FENCE_AMBIGUOUS,
                        orchestrator_effect_fence_resume=OrchestratorEffectFencePausedResumeState(
                            idempotency_key=_orch_fence_key,
                            step_id=str(orchestrator_step.step_id),
                            step_kind=str(orchestrator_step.step_kind.value),
                        ),
                    )
                )
                _finalize_reconciler_cas_if_attempted()
                return RunResult(
                    workflow_id=workflow_id,
                    run_id=run_id,
                    status=RunStatus.PAUSED,
                    terminal_step_index=None,
                    partial_state=None,
                    final_state=None,
                    fail_class=None,
                    pause_snapshot=_orch_pause_snapshot,
                ), 0
            # The orchestrator failed before any worker fan-out → FAILED. Drain the
            # orchestrator_writer so a buffered per-step override entry persists (the
            # override WAS applied; recorded on a failed dispatch, as the linear path
            # does) — its `_writer_ran_a_step` is False (override-only, no step entry),
            # so no spurious STEP_BOUNDARY + step_count stays 0. cascade_policy governs
            # WORKER failure, not the orchestrator's own dispatch.
            drain_branch_buffers(ctx.ledger_writer, [orchestrator_writer])
            _finalize_reconciler_cas_if_attempted()
            return RunResult(
                workflow_id=workflow_id,
                run_id=run_id,
                status=RunStatus.FAILED,
                terminal_step_index=None,
                partial_state=None,
                final_state=None,
                fail_class=_step_fail_class("orchestrator-workers-orchestrator-failure", exc),
            ), 0
        # B-FANOUT-OUTPUT-REPLAY (R-FS-1) — RESERVE-before-COMMIT: capture the
        # orchestrator (steps[0]) output BEFORE its sequential ledger entry, so a crash
        # after ≥1 worker completes can recover `FanOutResumeState.orchestrator_output`.
        # Without it `_determine_fanout_resume` fails CLOSED (workers-completed-but-
        # orchestrator-missing is an inconsistent store), so this capture must be wired
        # in the SAME pass as the worker capture above. No-op unless replay-capable ∧
        # store-bound (the identical `_fanout_replay_store` gate; `_orch_replay_store` was
        # computed above for the reserve-before-DISPATCH marker — reuse it).
        if _orch_replay_store is not None:
            _orch_replay_store.record_orchestrator(
                run_idempotency_key, str(orchestrator_step.step_id), orchestrator_output
            )
        _append_buffered_sequential_entry(
            writer=orchestrator_writer,
            workflow_id=workflow_id,
            entry_index=0,
            idempotency_key=orchestrator_idempotency_key,
            timestamp=fanout_timestamp,
            procedural_tier_snapshot_ref=snapshot_ref,
        )

    # --- 2) the worker fan-out plan (per-role child contexts under the orchestrator) ---
    # B-FANOUT-CRASH-RESUME-ORCHESTRATOR-MAYBE-RAN-SUBAGENT — reset `is_orchestrator_dispatch`
    # on the worker-template parent so no worker inherits the orchestrator's seed discriminator
    # (compose_branch_child_context also resets it; defense-in-depth + intent-documenting).
    fanout_parent = orchestrator_context.model_copy(
        update={
            "parent_idempotency_key": orchestrator_idempotency_key,
            "is_orchestrator_dispatch": False,
        }
    )
    branch_plan: list[
        tuple[int, WorkflowStep, StepExecutionContext, BufferingLedgerWriter, StepEffectiveBinding]
    ] = []
    # B-FANOUT-EFFECT-FENCE-PER-BRANCH-SCOPED-ABORT (R-FS-1, CP spec v1.73 §1) — worker ordinals
    # the operator scoped-aborted (`ABORT_BRANCH`) this resume: EXCLUDED from re-dispatch
    # (processed terminal after the disposition dicts init below). DISJOINT from `branch_plan`
    # (never re-dispatched → at-most-once: the ambiguous effect is never re-fired) and from
    # run-level `ABORT` (which fails the whole run). The ORCHESTRATOR analogue of the
    # parallelization peer scoped-abort.
    _scoped_abort_ordinals: set[int] = set()
    for branch_index, step in enumerate(worker_steps):
        # B-FANOUT-PAUSE — on resume, a branch that reached a terminal disposition
        # before the prior `pause` halt is SKIPPED (§25.15.2 obligation 7: a
        # `completed`/`timed_out`/`cancelled` branch MUST NOT be re-dispatched —
        # its effect may have landed). Its recovered output is folded into the
        # aggregate below. Only the not-yet-dispatched (left-re-dispatchable)
        # workers fan out again.
        if branch_index in _recovered_terminal:
            continue
        # B-FANOUT-CRASH-RESUME-PAUSE-RECONSTRUCT-NOT-YET-DISPATCHED (CP spec v1.70 §1) — in the
        # re-pause-without-dispatch mode the dispatcher proved every absent worker ordinal
        # not-yet-run (instrumented + no dispatch marker). SKIP them all → an empty `branch_plan`
        # → the `not branch_plan` block below re-establishes the lost PAUSED state OMITTING them
        # (re-dispatchable on `api.resume`, the operator's obl-5 blast-radius gate — NOT crash-
        # resume, which must never auto-fire a not-yet-dispatched effect-bearing worker).
        if crash_pause_reconstruct_no_dispatch:
            continue
        # Resolve the per-step binding FIRST so a per-step ROLE override (CP spec
        # v1.38 §6.1, B4 Slice 4) can take precedence over the fan-out-derived
        # role when composing the child context (precedence per-step > derived).
        binding = resolve_step_binding(
            manifest_entry,
            str(step.step_id),
            default_model_binding=default_model_binding,
            persona_tier=manifest_entry.persona_tier,
        )
        # Per-worker role: a per-step `StepOverride.agent_role` override (B4
        # Slice 4) wins; else the B1 step_id-derived role (distinct per worker,
        # bindable via RoutingManifest.per_role_bindings; the catalog is B4
        # Slice 2). `derive_agent_role` is the single shared B1↔B4 contract an
        # operator keys their catalog on (per_role_catalog.py). Folding the
        # override here keeps `StepExecutionContext.agent_role` the SINGLE role
        # source the runtime dispatch reads (§14.5.3 composition-time relaxation,
        # runtime spec v1.52 — no two-authority-at-dispatch).
        # Truthiness (not `is not None`) is DELIBERATE + dispatch-consistent: an
        # empty `AgentRole("")` is not a usable routing key (the dispatch read
        # `_role = step_context.agent_role or _MVP_DEFAULT_AGENT_ROLE` drops it;
        # no `per_role_bindings` catalog keys on ""), so an accidental empty
        # override falls through to the worker's derived role rather than
        # suppressing it to the bare default. Applies to every `... or derive/
        # default` fold site below. (Out-of-family review [P3].)
        role = binding.agent_role or derive_agent_role(step.step_id)
        # The worker's DECLARED step ordinal is its position in the original
        # `steps` (orchestrator=0, workers=1,2,…), i.e. `branch_index + 1`.
        # `compose_branch_child_context` inherits `step_index` from the fan-out
        # parent (the orchestrator, step 0), so set it to the declared ordinal
        # here — downstream consumers key per-step policy / audit / the runtime
        # skill-activation hook on `step_context.step_index`, so every worker must
        # carry its own ordinal (the declared-ordinal discipline U-CP-87 set), not
        # all report as step 0. (Branch identity stays `(parent_action_id,
        # branch_index)`; the ledger keys stay branch-scoped — this only fixes the
        # transient driver-side `step_index` the dispatcher reads.)
        # B-HIERARCHICAL-PAUSE — on resume, a worker whose recursive child PAUSED is
        # re-dispatched WITH the child's snapshot threaded on its (hash-inert)
        # StepExecutionContext, so the runtime sub-agent dispatcher re-enters the child
        # via `execute_workflow(pause_snapshot_input=...)` at the child's cursor (the
        # grandchild's completed steps are recovered, NOT re-executed). `None` for every
        # non-paused-child worker → byte-identical to the pre-arc context.
        _child_resume = _recovered_paused_child.get(branch_index)
        # B-FANOUT-EFFECT-FENCE-BRANCH-PAUSE / B-FANOUT-EFFECT-FENCE-PER-BRANCH-RESOLUTION — on
        # resume, a worker whose OWN dispatch raised the effect fence is re-dispatched WITH the
        # operator's resolution key-bound to THIS branch's reserve, threaded on the (hash-inert)
        # `effect_fence_resolution`. The resolution is `effect_fence_resolution_for(branch_key)` —
        # this worker's per-key `effect_fence_resolutions` entry if supplied, else the uniform
        # default — so two paused workers can resolve DIFFERENTLY in one resume (the worker analogue
        # of the linear B-EFFECT-FENCE-PAUSE-RESOLUTION threading). Built ONLY when this ordinal was
        # fence-paused AND the captured key is non-empty AND a resolution exists for it; else None →
        # the fence re-pauses INERT (never an auto-re-fire). None for every non-fence worker →
        # byte-identical to the pre-arc context.
        _branch_fence_key = _recovered_effect_fence_paused.get(branch_index)
        _branch_resolution = (
            _ef_resume_ctx.effect_fence_resolution_for(_branch_fence_key)
            if (_branch_fence_key and _ef_resume_ctx is not None)
            else None
        )
        # B-FANOUT-EFFECT-FENCE-PER-BRANCH-SCOPED-ABORT (R-FS-1, CP spec v1.73 §1) — the operator
        # scoped the ABORT to THIS worker (`ABORT_BRANCH`): fail just this worker, let the
        # vouched-for siblings (SKIP_AS_FIRED / RE_FIRE) fire. Do NOT re-dispatch (at-most-once:
        # no directive reaches the runtime fence → the ambiguous effect is never re-fired); the
        # post-init block below records it `completed`/no-output terminal + captures it durably +
        # the post-barrier folds survivors per cascade_policy. This interception runs BEFORE the
        # run-level ABORT suppression below so a {ABORT, ABORT_BRANCH} mixed map records the
        # scoped-abort worker DETERMINISTICALLY terminal (the suppression would otherwise null its
        # resolution first → it would re-dispatch into the ABORT race, never recorded scoped-abort,
        # contradicting the never-half-recorded contract). Run-level ABORT still dominates: the
        # ABORT worker keeps its directive → the post-barrier ABORT→FAILED return precedes the
        # scoped-abort fold; an excluded worker never dispatches, so it can never fire ahead of it.
        if _branch_resolution is EffectFenceResolution.ABORT_BRANCH:
            _scoped_abort_ordinals.add(branch_index)
            continue
        # Run-level ABORT guard (Codex [P1]): if ANY paused worker resolves to ABORT, suppress this
        # NON-ABORT (continue: SKIP_AS_FIRED / RE_FIRE) worker's directive so it re-pauses INERT
        # (never fires) before the ABORT fails the run. Keys on `is ABORT` — ABORT_BRANCH (handled
        # above) never reaches here.
        if _any_fence_abort and _branch_resolution is not EffectFenceResolution.ABORT:
            _branch_resolution = None
        _branch_effect_fence_directive = (
            EffectFenceResolutionDirective(
                resolution=_branch_resolution,
                idempotency_key=_branch_fence_key,
            )
            if (_branch_fence_key and _branch_resolution is not None)
            else None
        )
        child = compose_branch_child_context(
            fanout_parent, branch_index=branch_index, agent_role=role
        ).model_copy(
            # B-HITL-PLACEMENT-PER-STEP-OVERRIDE-FOLD (CP spec v1.49 §6.2) — fold this
            # worker's per-step `binding.hitl_placement` override onto the workflow
            # tuple (keyed from manifest_entry, NOT the inherited fanout_parent — so
            # the orchestrator's own placement override never leaks to a worker).
            update={
                "step_index": branch_index + 1,
                "child_resume_snapshot": _child_resume,
                "effect_fence_resolution": _branch_effect_fence_directive,
                "hitl_placements": fold_step_hitl_placements(
                    manifest_entry.hitl_placements, binding.hitl_placement
                ),
            }
        )
        writer = BufferingLedgerWriter(actor=ctx.ledger_writer.actor, branch_index=branch_index)
        # B-NONLINEAR-OVERRIDE-PROVENANCE — buffer the worker's per-step override
        # entry through its writer (driver thread, before the fan-out spawns; see
        # the parallelization site). Drains in branch-index order with the
        # worker's step entry. HIERARCHICAL_DELEGATION inherits this — its
        # recursion re-enters `_execute_orchestrator_workers`.
        _buffer_branch_override_if_applied(
            branch_writer=writer,
            workflow_id=workflow_id,
            step=step,
            binding=binding,
            timestamp=fanout_timestamp,
            snapshot_ref=snapshot_ref,
        )
        branch_plan.append((branch_index, step, child, writer, binding))
    branch_writers = [plan[3] for plan in branch_plan]

    # Worker outputs collected as each branch CLEANLY completes (branch-index
    # keyed). Populated on the fan-out loop thread after the awaited dispatch
    # returns; read on the driver thread AFTER the barrier (single-threaded) for
    # the deterministic fold.
    collected: dict[int, tuple[str, Mapping[str, Any]]] = {}
    # B-FANOUT-PAUSE — branch_index -> terminal disposition ("completed" / "timed_out")
    # for every branch that reached a terminal boundary. Written from the same
    # post-await loop-thread sites as `collected` (single-threaded → safe). Read
    # AFTER the barrier to build a `pause` snapshot's `FanOutResumeState.branches`.
    terminal_dispositions: dict[int, str] = {}
    # B-HIERARCHICAL-PAUSE — branch_index -> the child sub-workflow's PauseSnapshot for
    # each worker whose recursive child PAUSED THIS round (raised SubAgentChildPausedError).
    # Written from the same single-threaded post-await loop sites as `terminal_dispositions`;
    # read AFTER the barrier to build `FanOutResumeState.paused_child_branches`. A paused
    # child is NOT a terminal disposition (it is re-dispatched-via-child-resume on resume),
    # so its ordinal NEVER enters `terminal_dispositions` (the two sets are disjoint).
    paused_child_dispositions: dict[int, PauseSnapshot] = {}
    # B-FANOUT-EFFECT-FENCE-BRANCH-PAUSE (R-FS-1) — branch_index -> the held effect-fence
    # reserve's idempotency_key for each TOOL_STEP worker whose OWN dispatch raised the
    # runtime fence's `EffectFenceAmbiguousUncommittedError` (C-RT-31 §14.22). Like a paused
    # child (NOT a terminal disposition — re-dispatched-under-resolution on resume), so its
    # ordinal NEVER enters `terminal_dispositions` (disjoint). Read AFTER the barrier to build
    # `FanOutResumeState.effect_fence_paused_branches`. The value is the reserve key (read by
    # name off the runtime error); "" when the error carried no key → resume re-pauses INERT
    # (never an auto-re-fire — the linear B-EFFECT-FENCE-HITL-ROUTE defensive shape).
    effect_fence_paused_dispositions: dict[int, str] = {}
    # B-FANOUT-EFFECT-FENCE-BRANCH-PAUSE — worker ordinals whose re-dispatch on resume raised the
    # runtime fence's `EffectFenceAbortedError` (operator ABORT). A TERMINAL decision → the
    # post-barrier forces RunStatus.FAILED tier-agnostic, BEFORE the pause path (the LINEAR ABORT
    # → FAILED analogue; Codex [P1]).
    effect_fence_aborted_dispositions: set[int] = set()

    # B-FANOUT-PAUSE — seed the recovered terminal branches (from the resume
    # snapshot) into `collected` + `terminal_dispositions` so (a) their outputs
    # fold into the resumed aggregate and (b) a RE-pause snapshot unions the
    # already-terminal set with this round's newly-terminal branches. `step_id`
    # is re-derived from the re-supplied `worker_steps` (it was not carried in the
    # snapshot — the workflow object carries the steps).
    for _bi, _branch in _recovered_terminal.items():
        terminal_dispositions[_bi] = _branch.terminal_status
        if _branch.output is not None:
            collected[_bi] = (str(worker_steps[_bi].step_id), _branch.output)
        # B-FANOUT-EFFECT-FENCE-SCOPED-ABORT-CRASH-DURABLE (R-FS-1, CP spec v1.74 §1) — a
        # crash-recovered worker the operator scoped-aborted (the durable `scoped_aborted`
        # disposition) reconstructs `_scoped_abort_ordinals` so the post-barrier all-abort guard
        # reproduces the in-resume scoped-abort fold across a crash. Mirrors the PARALLELIZATION
        # peer seed; inert on the operator-RESUME (snapshot `completed`) path (already correct).
        if _branch.terminal_status == "scoped_aborted":
            _scoped_abort_ordinals.add(_bi)
    # B-FANOUT-OUTPUT-REPLAY — a crash-recovered worker with NO output (ran-and-errored)
    # keeps the run DEGRADED (never SUCCESS while omitting the failure — Codex [P2]).
    _recovered_degraded = any(b.output is None for b in _recovered_terminal.values())
    # B-FANOUT-OUTPUT-REPLAY — record the capture-time fan-out CARDINALITY once on a fresh
    # run so a changed-cardinality crash-resume fails closed (Codex [P2]). `steps` here is
    # the [orchestrator, workers] set the resume's `_determine_fanout_resume` also sees.
    if not _is_resume:
        _cardinality_store = _fanout_replay_store(ctx, manifest_entry)
        if _cardinality_store is not None:
            _cardinality_store.record_fanout_cardinality(run_idempotency_key, len(steps))
            # B-FANOUT-CRASH-RESUME-STRICT-TIER-INCOMPLETE (R-FS-1) — stamp the run
            # dispatch-instrumented (the cross-version guard) on the strict tiers, before any
            # worker dispatches. Worker-branch PROCEED remains unchanged here; the PROCEED
            # orchestrator marker is emitted at the orchestrator-dispatch site without stamping the
            # worker-marker trust gate.
            if cascade_policy is not CascadePolicy.PROCEED:
                _cardinality_store.record_dispatch_instrumented(run_idempotency_key)

    # B-FANOUT-OUTPUT-REPLAY — on a CRASH-resume (NOT pause), re-materialize the recovered
    # WORKER branches' LOST ledger entries (out-of-family Codex [P1]): a crash before the
    # barrier drained nothing, so the resumed ledger would omit them + undercount
    # `workflow.step_count`. Dedup-safe; extends the barrier-drain set. (The worker context
    # mirrors the dispatch-plan build: `derive_agent_role` + `step_index = branch_index + 1`;
    # `child_resume_snapshot` is None — a crash has no paused children.)
    if _fan_out_crash_resume is not None:
        for _bi in sorted(_recovered_terminal):
            _r_step = worker_steps[_bi]
            _r_binding = resolve_step_binding(
                manifest_entry,
                str(_r_step.step_id),
                default_model_binding=default_model_binding,
                persona_tier=manifest_entry.persona_tier,
            )
            _r_child = compose_branch_child_context(
                fanout_parent,
                branch_index=_bi,
                agent_role=_r_binding.agent_role or derive_agent_role(_r_step.step_id),
            ).model_copy(
                update={
                    "step_index": _bi + 1,
                    "hitl_placements": fold_step_hitl_placements(
                        manifest_entry.hitl_placements, _r_binding.hitl_placement
                    ),
                }
            )
            branch_writers.append(
                _rematerialize_recovered_branch_writer(
                    ctx,
                    branch_index=_bi,
                    branch_context=_r_child,
                    run_idempotency_key=run_idempotency_key,
                    timestamp=fanout_timestamp,
                    procedural_tier_snapshot_ref=snapshot_ref,
                    workflow_id=workflow_id,
                    step=_r_step,
                    binding=_r_binding,
                )
            )

    # B-FANOUT-EFFECT-FENCE-PER-BRANCH-SCOPED-ABORT (R-FS-1, CP spec v1.73 §1) — record each
    # scoped-aborted worker as a `completed`/no-output terminal (REUSE the IS-hash-bearing
    # `terminal_status` Literal, not extend → CP-only, no §5.2 IS-hash change; `completed` is
    # dispatch-boundary-accurate — the ORIGINAL attempt ran, its effect may have landed).
    # Captured durably so a crash mid-resume recovers it terminal-failed (NOT maybe-ran →
    # re-dispatch — `[[durable-recovery-presence-validity-scope]]`). Its writer joins the barrier
    # drain; its ordinal (`_scoped_abort_ordinals`) feeds the post-barrier fold (PARTIAL with
    # survivors; FAILED if NONE) + the run fail_class. NEVER re-dispatched. (Worker context
    # mirrors the dispatch-plan / re-materialize build: derive role + `step_index = _bi + 1`.)
    # SKIPPED under PROCEED: an effect-fence resume under PROCEED is rejected fail-closed (the
    # strict-tier guard below, + the `not branch_plan` early gate), so recording a durable terminal
    # here would corrupt state for a rejected resume (fail-closed precedes durable writes —
    # `[[durable-recovery-presence-validity-scope]]`; out-of-family Codex [P2]).
    _scoped_abort_to_record = (
        ()
        if cascade_policy is CascadePolicy.PROCEED
        # EXCLUDE already-recovered ordinals (B-FANOUT-EFFECT-FENCE-SCOPED-ABORT-CRASH-DURABLE):
        # on a crash-resume the seed loop reconstructs the recovered scoped-aborts INTO
        # `_scoped_abort_ordinals` (so the fold guards fire), but those branches are ALREADY
        # durable (ledger terminal + store record) — re-recording would append a DUPLICATE
        # ledger terminal. Record only the NEWLY-aborted (this-resume) ordinals; the recovered
        # ones flow into the fold via the combined set. Disjoint on the operator-resume path
        # (snapshot scoped-aborts carry `completed` → never reconstructed here).
        else sorted(_scoped_abort_ordinals - _recovered_terminal.keys())
    )
    for _sa_bi in _scoped_abort_to_record:
        _sa_step = worker_steps[_sa_bi]
        _sa_binding = resolve_step_binding(
            manifest_entry,
            str(_sa_step.step_id),
            default_model_binding=default_model_binding,
            persona_tier=manifest_entry.persona_tier,
        )
        _sa_child = compose_branch_child_context(
            fanout_parent,
            branch_index=_sa_bi,
            agent_role=_sa_binding.agent_role or derive_agent_role(_sa_step.step_id),
        ).model_copy(
            update={
                "step_index": _sa_bi + 1,
                "hitl_placements": fold_step_hitl_placements(
                    manifest_entry.hitl_placements, _sa_binding.hitl_placement
                ),
            }
        )
        _sa_writer = BufferingLedgerWriter(actor=ctx.ledger_writer.actor, branch_index=_sa_bi)
        append_branch_terminal_ledger_entry(
            branch_writer=_sa_writer,
            branch_context=_sa_child,
            run_idempotency_key=run_idempotency_key,
            terminal_status="completed",
            timestamp=fanout_timestamp,
            procedural_tier_snapshot_ref=snapshot_ref,
        )
        # B-FANOUT-EFFECT-FENCE-SCOPED-ABORT-CRASH-DURABLE (R-FS-1, CP spec v1.74 §1) — the
        # DURABLE store records this scoped-abort worker as `scoped_aborted` (distinct from a
        # ran-and-errored `completed`-no-output) so a crash mid-resume reconstructs
        # `_scoped_abort_ordinals` from the recovered terminals (the seed loop above) and
        # reproduces the in-resume all-abort FAILED rather than the vacuous PAUSED/PARTIAL.
        # IS-hash-bearing LEDGER append (above) stays `completed` (no §5.2 IS-hash change);
        # `terminal_dispositions` (in-resume only) stays `completed` (rebuilt from the store on
        # crash-resume). Mirrors the PARALLELIZATION peer site.
        _capture_branch_terminal(
            ctx,
            manifest_entry,
            run_idempotency_key=run_idempotency_key,
            branch_index=_sa_bi,
            step_id=str(_sa_step.step_id),
            terminal_status="scoped_aborted",
            output=None,
        )
        terminal_dispositions[_sa_bi] = "completed"
        branch_writers.append(_sa_writer)

    def _record_clean(
        branch_index: int,
        step: WorkflowStep,
        child: StepExecutionContext,
        writer: BufferingLedgerWriter,
        output: Mapping[str, Any],
    ) -> None:
        # A cleanly-completed worker: its per-step entry (causality-only
        # branch_metadata) + a fresh `completed` terminal entry (U-CP-84) +
        # collect the output for the aggregate.
        append_branch_step_ledger_entry(
            branch_writer=writer,
            branch_context=child,
            run_idempotency_key=run_idempotency_key,
            local_step_index=0,
            timestamp=fanout_timestamp,
            procedural_tier_snapshot_ref=snapshot_ref,
        )
        # B-FANOUT-OUTPUT-REPLAY (R-FS-1) — RESERVE-before-COMMIT branch-output capture
        # (the worker analogue of the parallelization site; same store, same gate). The
        # store is the SOLE crash-resume authority (the §25.12 D1.b binary ledger holds
        # no mid-fan-out worker set). No-op unless replay-capable ∧ store-bound.
        _replay_store = _fanout_replay_store(ctx, manifest_entry)
        if _replay_store is not None:
            _replay_store.record_branch(
                run_idempotency_key, branch_index, str(step.step_id), "completed", output
            )
        append_branch_terminal_ledger_entry(
            branch_writer=writer,
            branch_context=child,
            run_idempotency_key=run_idempotency_key,
            terminal_status="completed",
            timestamp=fanout_timestamp,
            procedural_tier_snapshot_ref=snapshot_ref,
        )
        collected[branch_index] = (str(step.step_id), output)
        terminal_dispositions[branch_index] = "completed"  # B-FANOUT-PAUSE

    # No worker fan-out to run → SUCCESS with the recovered/empty worker set.
    # Non-resume: orchestrator-only (no workers) — `not branch_plan` ⟺ `not
    # worker_steps` (one plan entry per worker, no skips). Resume: every worker
    # already reached a terminal disposition before the pause (nothing left to
    # re-dispatch) → the recovered outputs ARE the aggregate. B-FANOUT-PAUSE.
    if not branch_plan:
        _orch_writers = [orchestrator_writer] if orchestrator_writer is not None else []
        drain_branch_buffers(ctx.ledger_writer, _orch_writers)
        if orchestrator_writer is not None:
            ctx.lifecycle_emitter.emit(WorkflowEventClass.STEP_BOUNDARY)
        # B-FANOUT-OUTPUT-REPLAY — drain + count the CRASH-resume re-materialized worker
        # writers (empty unless a crash-resume recovered EVERY worker → an empty branch_plan;
        # else the recovered workers' ledger entries would be silently dropped here, the
        # finding-1 fail-open audit gap in the full-recovery path). Dedup-safe.
        _rematerialized_steps = _drain_and_emit_step_boundaries(ctx, branch_writers)
        # B-FANOUT-EFFECT-FENCE-PER-BRANCH-SCOPED-ABORT (R-FS-1, CP spec v1.73 §1; Codex [P2]) — a
        # PROCEED resume of an effect-fence pause is rejected fail-closed. The strict-tier guard
        # below the worker fold (~8905) NEVER runs when branch_plan is empty (all workers were
        # scoped-aborted/terminal → this `not branch_plan` short-circuit), so reject HERE — after
        # the drain, before the no-worker fold (incl. the scoped-abort all-abort guard) — so an
        # all-scoped-abort PROCEED resume reports requires-strict-tier (NOT a SUCCESS/PARTIAL nor
        # the scoped-abort fail_class). No scoped-abort durable write happened (the recording loop
        # is PROCEED-guarded → fail-closed precedes durable writes).
        if _recovered_effect_fence_paused and cascade_policy is CascadePolicy.PROCEED:
            _finalize_reconciler_cas_if_attempted()
            return RunResult(
                workflow_id=workflow_id,
                run_id=run_id,
                status=RunStatus.FAILED,
                terminal_step_index=None,
                partial_state=None,
                final_state=None,
                fail_class="orchestrator-workers-effect-fence-resume-requires-strict-tier",
            ), (1 if orchestrator_writer is not None else 0) + _rematerialized_steps
        # B-FANOUT-PAUSE (advisor [P1]) — a resume where EVERY worker was already
        # terminal: if a recovered branch FAILED (terminal but no collected output)
        # the run is degraded → PARTIAL, not a bare silent SUCCESS dropping the
        # failure. Non-resume orchestrator-only → empty terminal set → SUCCESS.
        _degraded = any(_bi not in collected for _bi in terminal_dispositions)
        # B-FANOUT-CRASH-RESUME-PAUSE-RECONSTRUCT (R-FS-1, CP spec v1.68 §1) — a COMPLETE-recovery
        # crash-resume of a degraded PAUSE fan-out RE-ESTABLISHES the lost PAUSED state HERE (the
        # ORCHESTRATOR_WORKERS analogue of the parallelization gate; a complete recovery short-
        # circuits at this `not branch_plan` block, never reaching the worker-failed pause site).
        # Reading A: restore the PAUSED the crash interrupted, NOT a silent PARTIAL — the operator's
        # `pause` HITL election is preserved. The recovered set is degraded + complete (branch_plan
        # empty → re-dispatch nothing), so build the SAME `FanOutResumeState` a live worker-failure
        # pause builds (paused-child + effect-fence EMPTY on a complete recovery, synthesis_step_id
        # from the live param) → the reconstructed snapshot's `branches` byte-match a real pause.
        # The trigger is a genuine worker FAILURE (`completed` + no output), NOT a
        # RECOVER_AS_TERMINAL `timed_out` worker (which finalizes PARTIAL via `_degraded` below;
        # out-of-family Codex [P2]).
        _crash_pause_trigger = any(
            terminal_dispositions[_bi] == "completed" and _bi not in collected
            for _bi in terminal_dispositions
        )
        if (
            _crash_pause_trigger
            and crash_fan_out_resume is not None
            and resume_snapshot is None
            and cascade_policy is CascadePolicy.PAUSE
        ):
            _reestablish_protocol = getattr(ctx, "pause_resume_protocol", None)
            _reestablish_steps = (
                1 if orchestrator_writer is not None else 0
            ) + _rematerialized_steps
            if _reestablish_protocol is None:
                # No protocol bound → cannot capture a snapshot → fail HONESTLY (never a
                # false-resumable PAUSED), mirroring the worker-failed pause site.
                _finalize_reconciler_cas_if_attempted()
                return RunResult(
                    workflow_id=workflow_id,
                    run_id=run_id,
                    status=RunStatus.FAILED,
                    terminal_step_index=None,
                    partial_state=_aggregate_orchestrator_workers(orchestrator_output, collected),
                    final_state=None,
                    fail_class="orchestrator-workers-pause-resume-protocol-not-bound",
                ), _reestablish_steps
            _reestablish_fan_out_resume = FanOutResumeState(
                orchestrator_output=dict(orchestrator_output),
                orchestrator_step_id=str(orchestrator_step.step_id),
                branches=tuple(
                    FanOutBranchResumeState(
                        branch_index=_bi,
                        step_id=str(worker_steps[_bi].step_id),
                        terminal_status=_status,
                        output=(collected[_bi][1] if _bi in collected else None),
                    )
                    for _bi, _status in sorted(terminal_dispositions.items())
                ),
                worker_count=len(worker_steps),
                # B-FANOUT-CRASH-RESUME-PAUSE-RECONSTRUCT-MAYBE-RAN-FENCE-STEP-ID — the
                # ORCHESTRATOR_WORKERS / HIERARCHICAL reconstruct-no-dispatch path ALWAYS
                # short-circuits HERE (every worker is skipped → empty branch_plan → this
                # `not branch_plan` block), BEFORE the worker-failed pause site that also appends
                # these carriers. So the fence-recoverable maybe-ran ordinals MUST be carried HERE
                # too, else api.resume would treat them as ordinary absent branches and a
                # changed-step_id edit would bypass the material-diff guard → double-fire
                # (out-of-family Codex [P1]). Empty `()` on a complete recovery + a normal pause.
                effect_fence_paused_branches=crash_pause_reconstruct_fence_paused,
                synthesis_step_id=(
                    str(synthesis_step.step_id) if synthesis_step is not None else None
                ),
            )
            _reestablish_snapshot = _run_protocol_method_sync(
                cast(PauseResumeProtocol, _reestablish_protocol).capture_pause_snapshot(
                    workflow_id=workflow_id,
                    run_id=run_id,
                    step_index=0,
                    pause_reason=WorkflowPauseReason.EXPLICIT_OPERATOR,
                    fan_out_resume=_reestablish_fan_out_resume,
                )
            )
            result = RunResult(
                workflow_id=workflow_id,
                run_id=run_id,
                status=RunStatus.PAUSED,
                terminal_step_index=None,
                partial_state=_aggregate_orchestrator_workers(orchestrator_output, collected),
                final_state=None,
                fail_class=None,
                pause_snapshot=_reestablish_snapshot,
            )
            _finalize_reconciler_cas_if_attempted()
            return result, _reestablish_steps
        # B-FANOUT-EFFECT-FENCE-PER-BRANCH-SCOPED-ABORT (R-FS-1, CP spec v1.73 §1) — all-abort
        # guard at the empty-`branch_plan` short-circuit (every fence-paused worker scoped-aborted →
        # nothing to re-dispatch → this block, BEFORE the worker-failed fold): if NO worker survived
        # (nothing collected), the run has NO result → FAILED, not the vacuous PARTIAL `_degraded`
        # yields. (≥1 survivor → folds to PARTIAL below with the provenance fail_class.)
        if _scoped_abort_ordinals and not collected:
            _finalize_reconciler_cas_if_attempted()
            return RunResult(
                workflow_id=workflow_id,
                run_id=run_id,
                status=RunStatus.FAILED,
                terminal_step_index=None,
                partial_state=_aggregate_orchestrator_workers(orchestrator_output, collected),
                final_state=None,
                fail_class="orchestrator-workers-effect-fence-branch-aborted",
            ), (1 if orchestrator_writer is not None else 0) + _rematerialized_steps
        _no_worker_status = RunStatus.PARTIAL if _degraded else RunStatus.SUCCESS
        # B-POSTJOIN-LLM-SYNTHESIS (CP spec v1.54 §3/§4) — the no-worker-fan-out
        # terminal aggregate also honors an opt-in synthesis on SUCCESS (composing
        # the recovered worker set; never silently skipped); `None` → the fold.
        _synth = _maybe_post_join_synthesis(
            synthesis_step=synthesis_step,
            status=_no_worker_status,
            collected=collected,
            ctx=ctx,
            manifest_entry=manifest_entry,
            step_dispatchers=step_dispatchers,
            default_model_binding=default_model_binding,
            fanout_parent=fanout_parent,
            run_idempotency_key=run_idempotency_key,
            run_id=run_id,
            branch_count=len(steps),
        )
        if isinstance(_synth, RunResult):
            return _synth, (1 if orchestrator_writer is not None else 0) + _rematerialized_steps
        _finalize_reconciler_cas_if_attempted()
        # A dict ⟺ the synthesis dispatched → count its executed step (Codex [P2]).
        _synth_steps = 1 if _synth is not None else 0
        _aggregate = (
            _synth
            if _synth is not None
            else _aggregate_orchestrator_workers(orchestrator_output, collected)
        )
        result = RunResult(
            workflow_id=workflow_id,
            run_id=run_id,
            status=_no_worker_status,
            terminal_step_index=None,
            partial_state=_aggregate if _degraded else None,
            final_state=None if _degraded else _aggregate,
            # B-FANOUT-EFFECT-FENCE-PER-BRANCH-SCOPED-ABORT — a scoped-abort folding into this
            # no-worker PARTIAL (survivors present) carries `fail_class=None` like every other
            # degraded PARTIAL (the all-abort/no-survivor case FAILED earlier with the provenance
            # fail_class; run-result operator-abort provenance on a PARTIAL is out of scope).
            fail_class=None,
        )
        return (
            result,
            (1 if orchestrator_writer is not None else 0) + _synth_steps + _rematerialized_steps,
        )

    def _finish(
        status: RunStatus,
        *,
        fail_class: str | None,
        salvage: bool,
        pause_snapshot: PauseSnapshot | None = None,
    ) -> tuple[RunResult, int]:
        # B-FANOUT-OUTPUT-REPLAY — a crash-resume that recovered a ran-and-errored worker
        # (terminal, no output) keeps the run DEGRADED (Codex [P2]).
        if status is RunStatus.SUCCESS and _recovered_degraded:
            status, salvage = RunStatus.PARTIAL, True
        # Drain the orchestrator entry FIRST (the fan-out parent persists before
        # its workers), then the worker buffers (branch-index order), emitting one
        # STEP_BOUNDARY per persisted-step writer. steps_executed = orchestrator +
        # workers that ran a step. B-FANOUT-PAUSE: on resume `orchestrator_writer`
        # is None (the orchestrator ran in the original envelope, not re-appended)
        # → no orchestrator drain / STEP_BOUNDARY, steps_executed excludes it.
        _orch_writers = [orchestrator_writer] if orchestrator_writer is not None else []
        drain_branch_buffers(ctx.ledger_writer, _orch_writers)
        if orchestrator_writer is not None:
            ctx.lifecycle_emitter.emit(WorkflowEventClass.STEP_BOUNDARY)
        steps_executed = (1 if orchestrator_writer is not None else 0) + (
            _drain_and_emit_step_boundaries(ctx, branch_writers)
        )
        # B-POSTJOIN-LLM-SYNTHESIS (CP spec v1.54 §3/§4) — an opt-in terminal
        # POST_JOIN_SYNTHESIS step composes the branch-index-ordered WORKER
        # siblings post-drain on SUCCESS, REPLACING the deterministic fold;
        # `None` → the byte-identical fold.
        _synth = _maybe_post_join_synthesis(
            synthesis_step=synthesis_step,
            status=status,
            collected=collected,
            ctx=ctx,
            manifest_entry=manifest_entry,
            step_dispatchers=step_dispatchers,
            default_model_binding=default_model_binding,
            fanout_parent=fanout_parent,
            run_idempotency_key=run_idempotency_key,
            run_id=run_id,
            branch_count=len(steps),
        )
        if isinstance(_synth, RunResult):
            return _synth, steps_executed
        _finalize_reconciler_cas_if_attempted()
        # A dict ⟺ the synthesis dispatched → count its executed step (Codex [P2]).
        if _synth is not None:
            steps_executed += 1
        aggregate = (
            _synth
            if _synth is not None
            else _aggregate_orchestrator_workers(orchestrator_output, collected)
        )
        result = RunResult(
            workflow_id=workflow_id,
            run_id=run_id,
            status=status,
            terminal_step_index=None,
            # B-FANOUT-PAUSE — PAUSED carries the salvaged aggregate as
            # partial_state (the completed branches) + the resumable snapshot.
            partial_state=aggregate if (salvage and status is not RunStatus.SUCCESS) else None,
            final_state=aggregate if status is RunStatus.SUCCESS else None,
            fail_class=fail_class,
            pause_snapshot=pause_snapshot,
        )
        return result, steps_executed

    deadline = _DEFAULT_FANOUT_BARRIER_DEADLINE_SECONDS

    # B-FANOUT-EFFECT-FENCE-BRANCH-PAUSE — resuming an effect-fence pause (a STRICT-TIER construct)
    # under a manifest/persona that now resolves to PROCEED is incoherent (the PROCEED path has no
    # pause/resolution handling → a re-entered worker's ABORT / missing-resolution ambiguous error
    # would degrade to PARTIAL, dropping the operator's decision). Fail closed — the resume requires
    # a strict tier (out-of-family Codex [P2] R3; the parallelization analogue).
    if _recovered_effect_fence_paused and cascade_policy is CascadePolicy.PROCEED:
        return _finish(
            RunStatus.FAILED,
            fail_class="orchestrator-workers-effect-fence-resume-requires-strict-tier",
            salvage=False,
        )

    # === proceed: siblings run to completion → SUCCESS | PARTIAL (degraded) ===
    if cascade_policy is CascadePolicy.PROCEED:

        async def _proceed_worker(
            branch_index: int,
            step: WorkflowStep,
            child: StepExecutionContext,
            writer: BufferingLedgerWriter,
            binding: StepEffectiveBinding,
        ) -> None:
            dispatcher = step_dispatchers.lookup(step.step_kind)
            try:
                output = await asyncio.to_thread(
                    dispatcher.dispatch, binding, step, step_context=child
                )
            except asyncio.CancelledError:
                # The §25.11 wall-clock deadline (`_proceed_fanout`'s
                # `asyncio.timeout`) cancelled this in-flight worker. Its dispatch
                # was scheduled (the effect may have landed on the abandoned
                # thread) → record the step entry (obl. 3 — no silent obl-3 gap) +
                # a `timed_out` terminal, then re-raise to honor the cancellation.
                # WITHOUT this branch a deadline-cancelled worker would buffer
                # nothing and its dispatched effect would be an unrecorded silent
                # gap (decorrelated-review [P2]).
                append_branch_step_ledger_entry(
                    branch_writer=writer,
                    branch_context=child,
                    run_idempotency_key=run_idempotency_key,
                    local_step_index=0,
                    timestamp=fanout_timestamp,
                    procedural_tier_snapshot_ref=snapshot_ref,
                )
                # B-FANOUT-OUTPUT-REPLAY — capture the timed-out DISPOSITION (the effect may
                # have landed; crash-resume FAILS CLOSED on it — never a silent re-dispatch).
                _capture_branch_terminal(
                    ctx,
                    manifest_entry,
                    run_idempotency_key=run_idempotency_key,
                    branch_index=branch_index,
                    step_id=step.step_id,
                    terminal_status="timed_out",
                    output=None,
                )
                append_branch_terminal_ledger_entry(
                    branch_writer=writer,
                    branch_context=child,
                    run_idempotency_key=run_idempotency_key,
                    terminal_status="timed_out",
                    timestamp=fanout_timestamp,
                    procedural_tier_snapshot_ref=snapshot_ref,
                )
                raise
            except SubAgentChildPausedError as _paused:
                # B-HIERARCHICAL-PAUSE — this worker's recursive child PAUSED. Under
                # `proceed` (SOLO) there is no resumable-pause boundary to honor it (the
                # fan-out is configured to degrade-on-failure, not pause), and silently
                # treating the suspended child as success/degraded would DROP its state.
                # Stash it (no terminal — not a terminal branch) + re-raise so the
                # gather marks the branch; the post-barrier guard FAILS the run HONESTLY
                # (a paused child cannot be carried under proceed). No silent loss.
                paused_child_dispositions[branch_index] = _paused.child_snapshot
                raise
            except Exception:
                # Ran-and-errored → record the step entry (obl. 3) + a `completed`
                # terminal (dispatch-boundary, not step-outcome; the failure lives
                # at the step entry). Contributes nothing to the aggregate; re-raise
                # so the return_exceptions gather marks this branch failed (→ the
                # partial result set → PARTIAL). proceed does NOT cancel siblings.
                append_branch_step_ledger_entry(
                    branch_writer=writer,
                    branch_context=child,
                    run_idempotency_key=run_idempotency_key,
                    local_step_index=0,
                    timestamp=fanout_timestamp,
                    procedural_tier_snapshot_ref=snapshot_ref,
                )
                # B-FANOUT-OUTPUT-REPLAY — a ran-and-errored branch's effect may have LANDED
                # (dispatch-boundary `completed`, no output) → capture the disposition so
                # crash-resume recovers it as TERMINAL (no re-dispatch, no fold), never
                # re-firing a landed effect (the disposition class closer).
                _capture_branch_terminal(
                    ctx,
                    manifest_entry,
                    run_idempotency_key=run_idempotency_key,
                    branch_index=branch_index,
                    step_id=step.step_id,
                    terminal_status="completed",
                    output=None,
                )
                append_branch_terminal_ledger_entry(
                    branch_writer=writer,
                    branch_context=child,
                    run_idempotency_key=run_idempotency_key,
                    terminal_status="completed",
                    timestamp=fanout_timestamp,
                    procedural_tier_snapshot_ref=snapshot_ref,
                )
                raise
            _record_clean(branch_index, step, child, writer, output)

        async def _proceed_fanout() -> list[Any]:
            # `return_exceptions=True`: a failing worker does NOT cancel siblings
            # (the proceed semantic). Bounded by the §25.11 wall-clock deadline.
            async with asyncio.timeout(deadline):
                return await asyncio.gather(
                    *(_proceed_worker(*plan) for plan in branch_plan),
                    return_exceptions=True,
                )

        try:
            results = _run_fanout_to_completion(
                _proceed_fanout(), max_workers=max(1, len(branch_plan))
            )
        except BranchBarrierDeadlineExceededError:
            # A stuck worker hit the deadline; the completed workers buffered their
            # entries → PARTIAL (degraded). (proceed does not cancel; the stuck
            # worker is abandoned per `_run_fanout_to_completion`.)
            return _finish(RunStatus.PARTIAL, fail_class=None, salvage=True)
        except TimeoutError:
            return _finish(RunStatus.PARTIAL, fail_class=None, salvage=True)
        if paused_child_dispositions:
            # B-HIERARCHICAL-PAUSE — a recursive child PAUSED under `proceed`. There is
            # no resumable-pause boundary here (proceed degrades, it does not pause), so
            # the suspended child cannot be carried for resume — FAIL HONESTLY rather than
            # a SUCCESS/PARTIAL that silently dropped a suspended sub-workflow.
            return _finish(
                RunStatus.FAILED,
                fail_class="orchestrator-workers-child-paused-not-resumable-under-proceed",
                salvage=True,
            )
        any_failed = any(isinstance(r, BaseException) for r in results)
        if any_failed:
            return _finish(RunStatus.PARTIAL, fail_class=None, salvage=True)
        return _finish(RunStatus.SUCCESS, fail_class=None, salvage=False)

    # === cascade-cancel | pause: cancel-on-failure (TaskGroup structured cancel) ===
    # Both halt the fan-out on the first worker failure with in-flight effects run
    # to completion (shielded). They differ only in the run-level outcome on a
    # worker failure: cascade-cancel → FAILED (+ the empty-buffer not-yet-dispatched
    # scan records `cancelled`); pause → FAILED + `not-yet-materialized` fail_class
    # (resumable fan-out pause is a follow-on arc; see the pause branch below — a
    # false-`PAUSED` is foreclosed). The CLEAN (no-failure) path is SUCCESS for both.
    async def _cancel_worker(
        branch_index: int,
        step: WorkflowStep,
        child: StepExecutionContext,
        writer: BufferingLedgerWriter,
        binding: StepEffectiveBinding,
    ) -> None:
        dispatcher = step_dispatchers.lookup(step.step_kind)
        # B-FANOUT-CRASH-RESUME-STRICT-TIER-INCOMPLETE (R-FS-1) — reserve-before-DISPATCH:
        # durably mark this WORKER branch DISPATCHED, fsynced STRICTLY BEFORE the effect can
        # fire (the worker analogue of the parallelization marker; same store, same gate). The
        # worker `branch_index` is the 0-based `worker_steps` ordinal `record_branch` also uses,
        # so the marker/capture sets are partition-consistent. SYNCHRONOUS (atomic with the
        # `ensure_future` dispatch — see `_cancel_branch`). `_cancel_worker` is the strict-tier
        # path only — PROCEED's `_proceed_worker` writes no marker.
        _mark_branch_dispatched(
            ctx,
            manifest_entry,
            run_idempotency_key=run_idempotency_key,
            branch_index=branch_index,
            step_id=step.step_id,
            step_kind=step.step_kind,
            step=step,
        )
        # Schedule the (sync) dispatch off-loop; `dispatch_branch_step_shielded`
        # keeps it alive against THIS branch's cancellation so an in-flight effect
        # runs to its own completion (obl. 1), and registers it for the barrier's
        # deadline watchdog (the hard "...or barrier-deadline timeout" cut-off).
        inflight: asyncio.Future[Mapping[str, Any]] = asyncio.ensure_future(
            asyncio.to_thread(dispatcher.dispatch, binding, step, step_context=child)
        )
        try:
            output = await dispatch_branch_step_shielded(inflight)
        except asyncio.CancelledError:
            # B-HIERARCHICAL-PAUSE (Codex [P1]) — this branch was cancelled because a
            # SIBLING raised first, but its OWN in-flight child sub-workflow may have
            # PAUSED: `dispatch_branch_step_shielded` suppresses the in-flight exception
            # while draining a cancelled dispatch + re-raises `CancelledError`, so a
            # `SubAgentChildPausedError` lands in `inflight.exception()` rather than
            # reaching the typed handler below. Capture it as a paused-child (NOT a
            # terminal `completed` branch — else the snapshot records it terminal,
            # resume skips it, and the child's PauseSnapshot is DROPPED), matching the
            # direct-pause path: stash + re-raise, no step/terminal entry recorded.
            _inflight_exc = (
                inflight.exception() if (inflight.done() and not inflight.cancelled()) else None
            )
            if isinstance(_inflight_exc, SubAgentChildPausedError):
                paused_child_dispositions[branch_index] = _inflight_exc.child_snapshot
                raise
            # B-FANOUT-EFFECT-FENCE-BRANCH-PAUSE (R-FS-1) — this branch was cancelled because
            # a SIBLING raised first, but its OWN in-flight dispatch raised the runtime effect
            # fence (the shield suppresses the in-flight exception + re-raises CancelledError,
            # so the fence error lands in `inflight.exception()`, not the typed handler below).
            # Name-matched (NOT isinstance — it is a harness-runtime type harness-cp cannot
            # import; `type(None).__name__` is "NoneType" so a no-inflight cancel is safely
            # skipped). Capture it as effect-fence-paused (NOT a terminal `completed` branch —
            # else resume skips it + drops the pause), matching the SubAgentChildPausedError
            # in-flight shape: stash the reserve key + re-raise, no step/terminal entry.
            if type(_inflight_exc).__name__ == "EffectFenceAbortedError":
                # an in-flight worker whose re-dispatch raised the operator's ABORT → FAILED.
                effect_fence_aborted_dispositions.add(branch_index)
                raise
            if type(_inflight_exc).__name__ == "EffectFenceAmbiguousUncommittedError":
                _inflight_fence_key = getattr(_inflight_exc, "idempotency_key", None)
                effect_fence_paused_dispositions[branch_index] = (
                    _inflight_fence_key if isinstance(_inflight_fence_key, str) else ""
                )
                raise
            # In-flight at cancel-time: the effect ran (shielded to completion) or
            # the deadline cut it. Record the step entry (obl. 3) + the
            # discriminating terminal (obl. 4): `completed` = ran (ran-and-errored
            # is still completed — dispatch-boundary), `timed_out` = the deadline
            # cut the in-flight step. A not-yet-dispatched worker NEVER reaches here
            # (it has no inflight) — its `cancelled`/re-dispatchable disposition is
            # handled post-barrier by the empty-buffer scan.
            append_branch_step_ledger_entry(
                branch_writer=writer,
                branch_context=child,
                run_idempotency_key=run_idempotency_key,
                local_step_index=0,
                timestamp=fanout_timestamp,
                procedural_tier_snapshot_ref=snapshot_ref,
            )
            terminal: Literal["completed", "timed_out"] = (
                "timed_out" if (inflight.cancelled() or not inflight.done()) else "completed"
            )
            append_branch_terminal_ledger_entry(
                branch_writer=writer,
                branch_context=child,
                run_idempotency_key=run_idempotency_key,
                terminal_status=terminal,
                timestamp=fanout_timestamp,
                procedural_tier_snapshot_ref=snapshot_ref,
            )
            terminal_dispositions[branch_index] = terminal  # B-FANOUT-PAUSE
            # B-FANOUT-PAUSE (Codex [P1]) — a sibling that was IN-FLIGHT when the
            # barrier cancelled this branch ran to completion under the shield
            # (`terminal == "completed"` ⟹ `inflight.done()` and not cancelled). Its
            # successful OUTPUT must be collected so a `pause` snapshot recovers it
            # (else it stores `output=None` → resume skips a successfully-completed
            # branch + drops its output, corrupting the resumed aggregate). A
            # ran-and-errored in-flight (`exception() is not None`) has no output.
            if terminal == "completed" and inflight.exception() is None:
                # B-FANOUT-OUTPUT-REPLAY — a sibling that LANDED under the shield (its effect
                # fired) is captured WITH output (out-of-family Codex [P1]) — else crash-resume
                # RE-DISPATCHES it (double-fire). The buffered terminal append above is not
                # durable until the drain, so this still satisfies RESERVE-before-COMMIT.
                _capture_branch_terminal(
                    ctx,
                    manifest_entry,
                    run_idempotency_key=run_idempotency_key,
                    branch_index=branch_index,
                    step_id=step.step_id,
                    terminal_status="completed",
                    output=inflight.result(),
                )
                collected[branch_index] = (str(step.step_id), inflight.result())
            else:
                # A timed-out (deadline-cut, ambiguous) OR ran-and-errored (effect LANDED, no
                # output) in-flight sibling — capture the DISPOSITION with no output so
                # crash-resume recovers-as-terminal (`completed`) or FAILS CLOSED (`timed_out`),
                # never silently re-dispatching a landed effect (the disposition class closer).
                _capture_branch_terminal(
                    ctx,
                    manifest_entry,
                    run_idempotency_key=run_idempotency_key,
                    branch_index=branch_index,
                    step_id=step.step_id,
                    terminal_status=terminal,
                    output=None,
                )
            raise  # honor the cancellation (the barrier cancelled this branch)
        except SubAgentChildPausedError as _paused:
            # B-HIERARCHICAL-PAUSE — this worker's recursive child sub-workflow PAUSED
            # (a grandchild failed under cascade_policy=pause). NOT a terminal branch
            # (it is re-dispatched-via-child-resume on resume) and NOT a failure: stash
            # the child's snapshot keyed by branch ordinal, record NO terminal entry
            # (its ordinal stays out of `terminal_dispositions`, so it lands in
            # `paused_child_branches`, never `branches`; any pre-dispatch override entry
            # already buffered stays). Re-raise so the TaskGroup halts the fan-out at the
            # pause boundary (siblings: in-flight finish / not-yet-dispatched left
            # re-dispatchable — the §25.15.1 pause semantic), driving the pause branch.
            paused_child_dispositions[branch_index] = _paused.child_snapshot
            raise
        except Exception as _exc:
            # B-FANOUT-EFFECT-FENCE-BRANCH-PAUSE (R-FS-1) — this TOOL_STEP worker's OWN
            # dispatch raised the runtime effect fence's `EffectFenceAmbiguousUncommittedError`
            # (C-RT-31 §14.22): the fence lost a reserve to a prior uncommitted attempt AND
            # found no captured output, so whether THIS branch's effect fired is GENUINELY
            # ambiguous (the worker analogue of the linear B-EFFECT-FENCE-HITL-ROUTE at the
            # per-step loop). Name-matched (harness-cp cannot import harness-runtime). It is
            # NOT a terminal branch — record NO step/terminal entry (a `completed` terminal
            # would make resume SKIP it + drop the pause): stash the held reserve's
            # idempotency_key keyed by ordinal ("" when absent → resume re-pauses INERT,
            # never an auto-re-fire) so the post-barrier compose lands it in
            # `effect_fence_paused_branches`, then re-raise so the TaskGroup halts the fan-out
            # at the pause boundary (the SubAgentChildPausedError disposition shape; the
            # post-barrier guard FAILS HONESTLY if no PauseResumeProtocol is bound).
            if type(_exc).__name__ == "EffectFenceAbortedError":
                # operator ABORT on resume → terminal FAILED (NOT a re-pause); Codex [P1].
                effect_fence_aborted_dispositions.add(branch_index)
                raise
            if type(_exc).__name__ == "EffectFenceAmbiguousUncommittedError":
                _fence_key = getattr(_exc, "idempotency_key", None)
                effect_fence_paused_dispositions[branch_index] = (
                    _fence_key if isinstance(_fence_key, str) else ""
                )
                raise
            # THIS worker's own dispatch ERRORED — the failure that triggers the
            # cascade. The effect ran-and-errored → record the step entry (obl. 3 —
            # every dispatched effectful step gets its own entry REGARDLESS of
            # disposition; the step failure lives at this entry) + a `completed`
            # terminal (dispatch-boundary, not step-outcome — the carrier forecloses
            # `failed`). Re-raise so the TaskGroup cascade-cancels the siblings.
            # B-FANOUT-OUTPUT-REPLAY — capture the `completed` disposition (effect may have
            # LANDED, no output) so crash-resume recovers it as terminal, never re-firing it
            # (out-of-family Codex [P1] — the worker analogue of the parallelization site).
            _capture_branch_terminal(
                ctx,
                manifest_entry,
                run_idempotency_key=run_idempotency_key,
                branch_index=branch_index,
                step_id=step.step_id,
                terminal_status="completed",
                output=None,
            )
            append_branch_step_ledger_entry(
                branch_writer=writer,
                branch_context=child,
                run_idempotency_key=run_idempotency_key,
                local_step_index=0,
                timestamp=fanout_timestamp,
                procedural_tier_snapshot_ref=snapshot_ref,
            )
            append_branch_terminal_ledger_entry(
                branch_writer=writer,
                branch_context=child,
                run_idempotency_key=run_idempotency_key,
                terminal_status="completed",
                timestamp=fanout_timestamp,
                procedural_tier_snapshot_ref=snapshot_ref,
            )
            terminal_dispositions[branch_index] = "completed"  # B-FANOUT-PAUSE
            raise
        _record_clean(branch_index, step, child, writer, output)

    async def _cancel_fanout() -> list[None]:
        return await cascade_cancel_barrier(
            (_cancel_worker(*plan) for plan in branch_plan), deadline_seconds=deadline
        )

    worker_failed = False
    deadline_struck = False
    try:
        _run_fanout_to_completion(_cancel_fanout(), max_workers=max(1, len(branch_plan)))
    except BranchBarrierDeadlineExceededError:
        # The wall-clock deadline fired with no worker raising (a stuck fan-out) —
        # the §25.11 hard cap. A bare strand is FAILED (parity with PARALLELIZATION);
        # the in-flight workers recorded `timed_out` in their except blocks.
        deadline_struck = True
    except BaseExceptionGroup:
        # A worker raised → the TaskGroup cancelled not-yet-finished siblings.
        # In-flight siblings ran to completion (shielded) + recorded their terminal;
        # the failing worker's exception group is consumed here (the durable record
        # is the drained ledger). cascade_policy maps the run-level status.
        worker_failed = True

    # B-FANOUT-EFFECT-FENCE-BRANCH-PAUSE — an operator ABORT on resume is TERMINAL: force FAILED
    # tier-agnostically, BEFORE the cascade-policy branching, so a CascadePolicy.PAUSE run does
    # NOT re-pause an aborted fence worker (the LINEAR ABORT → FAILED analogue; Codex [P1]).
    if effect_fence_aborted_dispositions:
        return _finish(
            RunStatus.FAILED,
            fail_class="orchestrator-workers-effect-fence-aborted",
            salvage=False,
        )

    if cascade_policy is CascadePolicy.CASCADE_CANCEL:
        # obl. 4: a not-yet-dispatched worker (no step/terminal disposition — its
        # task was cancelled before scheduling its dispatch) records a `cancelled`
        # terminal so `resume_should_redispatch` is False (no double-dispatch on
        # resume). Keyed on the ABSENCE of a step/terminal disposition, NOT an empty
        # buffer: an overridden worker carries its pre-fan-out override entry
        # (`branch_metadata=None`, B-NONLINEAR-OVERRIDE-PROVENANCE), so an
        # `entry_count == 0` test would skip its required `cancelled` terminal.
        for _bi, _step, child, writer, _binding in branch_plan:
            if _writer_has_branch_disposition(writer):
                continue
            # B-HIERARCHICAL-PAUSE (Codex [P2]) — a paused-child branch (its recursive
            # child returned PAUSED) DID dispatch but recorded no terminal (it is the
            # third disposition, captured into `paused_child_dispositions`). Under
            # cascade-cancel it is NOT resumed (the run FAILs + the child snapshot is
            # discarded), but it MUST NOT be mislabeled `cancelled` (= not-yet-dispatched)
            # — it dispatched + its child paused. Record `completed` (dispatch-boundary,
            # like a ran-and-errored worker) so the branch ledger reflects the dispatch.
            # B-FANOUT-EFFECT-FENCE-BRANCH-PAUSE — same for an effect-fence-paused ordinal:
            # its OWN dispatch fired the fence (the effect may have landed), so it dispatched
            # and MUST NOT be mislabeled `cancelled`. Under cascade-cancel it is not resumed
            # (the run FAILs + the resolution is discarded); record `completed` so the ledger
            # reflects the dispatch (the ambiguous effect lives at the durable fence claim).
            _disposition = (
                "completed"
                if (_bi in paused_child_dispositions or _bi in effect_fence_paused_dispositions)
                else "cancelled"
            )
            if _bi in effect_fence_paused_dispositions:
                # B-FANOUT-EFFECT-FENCE-BRANCH-PAUSE — ALSO capture the `completed`/no-output
                # terminal to the durable STORE (not just the buffered ledger), so a crash mid-
                # cascade-cancel reproduces the FAILED on resume instead of mis-classifying the
                # worker as MAYBE-RAN + re-dispatching (Codex [P2]; the parallelization analogue).
                _capture_branch_terminal(
                    ctx,
                    manifest_entry,
                    run_idempotency_key=run_idempotency_key,
                    branch_index=_bi,
                    step_id=str(_step.step_id),
                    terminal_status="completed",
                    output=None,
                )
            append_branch_terminal_ledger_entry(
                branch_writer=writer,
                branch_context=child,
                run_idempotency_key=run_idempotency_key,
                terminal_status=_disposition,
                timestamp=fanout_timestamp,
                procedural_tier_snapshot_ref=snapshot_ref,
            )
        # B-FANOUT-EFFECT-FENCE-PER-BRANCH-SCOPED-ABORT (R-FS-1, CP spec v1.73 §1; Codex [P1]) —
        # under CASCADE_CANCEL a scoped-abort is a deliberate worker failure → the run cancels
        # (FAILED), NEVER a SUCCESS hiding the aborted worker. Fires BEFORE the SUCCESS return below
        # (the cascade-cancel block returns before the §25.15.1 degraded fold). The empty-plan
        # all-abort case is covered earlier at the `not branch_plan` short-circuit; this covers a
        # MIXED scoped-abort + surviving-worker resume on the cascade-cancel tier.
        if _scoped_abort_ordinals:
            return _finish(
                RunStatus.FAILED,
                fail_class="orchestrator-workers-effect-fence-branch-aborted",
                salvage=False,
            )
        if worker_failed or deadline_struck:
            return _finish(
                RunStatus.FAILED,
                fail_class="orchestrator-workers-cascade-cancel",
                salvage=False,
            )
        return _finish(RunStatus.SUCCESS, fail_class=None, salvage=False)

    # pause (§25.15.1 `pause → PAUSED`) — resumable FAN-OUT pause (B-FANOUT-PAUSE,
    # R-FS-1). On a worker failure: in-flight siblings finished (shielded, recorded
    # their terminal); not-yet-dispatched siblings were TaskGroup-cancelled and are
    # LEFT RE-DISPATCHABLE — the cascade-cancel obl.4 `cancelled`-terminal scan above
    # is DELIBERATELY NOT run here (the §25.15.1 pause semantic is "in-flight finish;
    # not-yet-dispatched left re-dispatchable", adversarial-review-r-fs-1-arc-14
    # line 55). We capture the per-branch terminal dispositions + the completed
    # branches' OUTPUTS (which the ledger does NOT carry — the R-CC-1 design §1.1
    # data-stateless re-open trigger, materialized for the fan-out case) into a
    # `FanOutResumeState`-bearing, hash-integrity-checked `PauseSnapshot`, return
    # PAUSED, and `api.resume` re-enters this strategy to skip terminal branches +
    # re-dispatch the rest (obl. 7). The deadline-strike case (a STUCK fan-out, no
    # worker raised) stays FAILED — there is no clean pause boundary to resume from.
    if deadline_struck:
        return _finish(
            RunStatus.FAILED, fail_class="orchestrator-workers-barrier-deadline", salvage=False
        )
    if worker_failed:
        protocol = getattr(ctx, "pause_resume_protocol", None)
        if not pause_resumable:
            # An UNWIRED caller (one that does not thread `resume_snapshot`) opts out
            # of resumable pause. Returning PAUSED would advertise a resumability
            # `api.resume` cannot honor (it would re-run the orchestrator + all
            # workers). Fail HONESTLY — `not-yet-materialized`. Post-B-HIERARCHICAL-PAUSE
            # BOTH top-level ORCHESTRATOR_WORKERS and HIERARCHICAL_DELEGATION pass
            # `pause_resumable=True`, so this guard now only protects a future unwired
            # reuse site (defence-in-depth, no live caller hits it).
            return _finish(
                RunStatus.FAILED,
                fail_class="orchestrator-workers-pause-resume-not-yet-materialized",
                salvage=True,
            )
        if protocol is None:
            # No pause/resume opt-in bound → the snapshot cannot be captured, so a
            # PAUSED would advertise a resumability the harness cannot honor (the
            # FALSE-`PAUSED` silent-degradation mode). Fail HONESTLY — detect-then-
            # refuse, mirroring `api.resume`'s ResumeProtocolNotBoundError. Completed
            # / in-flight ledger entries + the salvaged partial set still persist.
            return _finish(
                RunStatus.FAILED,
                fail_class="orchestrator-workers-pause-resume-protocol-not-bound",
                salvage=True,
            )
        # Build the resume state from the post-barrier terminal dispositions +
        # collected outputs — both already MERGED with any recovered-from-a-prior-
        # resume terminals (the seed loop above), so a RE-pause snapshot unions the
        # prior + this-round terminal sets. Absent worker ordinals (the cancelled
        # not-yet-dispatched ones) are left re-dispatchable by omission. `step_id`
        # is captured per branch so resume validates body identity (Codex [P1]).
        fan_out_resume = FanOutResumeState(
            orchestrator_output=dict(orchestrator_output),
            orchestrator_step_id=str(orchestrator_step.step_id),
            branches=tuple(
                FanOutBranchResumeState(
                    branch_index=_bi,
                    step_id=str(worker_steps[_bi].step_id),
                    terminal_status=_status,
                    output=(collected[_bi][1] if _bi in collected else None),
                )
                for _bi, _status in sorted(terminal_dispositions.items())
            ),
            worker_count=len(worker_steps),
            # B-HIERARCHICAL-PAUSE — workers whose recursive child PAUSED this round.
            # DISJOINT from `branches` (a paused-child ordinal never entered
            # `terminal_dispositions`). Each carries the child's nested PauseSnapshot so
            # `api.resume` re-enters the child at its cursor. COVERED by the snapshot hash
            # (transitively via `fan_out_resume.model_dump`).
            paused_child_branches=tuple(
                PausedChildBranchResumeState(
                    branch_index=_bi,
                    step_id=str(worker_steps[_bi].step_id),
                    child_snapshot=_child_snap,
                )
                for _bi, _child_snap in sorted(
                    paused_child_dispositions.items(), key=lambda kv: kv[0]
                )
            ),
            # B-FANOUT-EFFECT-FENCE-BRANCH-PAUSE — TOOL_STEP workers whose OWN dispatch
            # raised the runtime effect fence this round. DISJOINT from both `branches`
            # (a fence-paused ordinal never entered `terminal_dispositions`) and
            # `paused_child_branches` (a fence-paused ordinal never entered
            # `paused_child_dispositions` — they are caught at different except sites).
            # Each carries the held reserve's idempotency_key so `api.resume` key-binds
            # the operator's resolution to THIS branch's effect. COVERED by the snapshot
            # hash (transitively via `fan_out_resume.model_dump`; dropped-when-empty so a
            # no-fence pause hashes byte-identically to pre-arc).
            # B-FANOUT-CRASH-RESUME-PAUSE-RECONSTRUCT-MAYBE-RAN-FENCE-STEP-ID — APPEND the
            # fence-recoverable maybe-ran ordinals the crash-resume gate classified (`()` unless a
            # reconstruct; the live disposition map is empty in reconstruct mode). Disjoint from
            # terminals + paused-child + the live set; the resume material-diff guard re-checks
            # disjointness + the changed-step_id / changed-kind identity.
            effect_fence_paused_branches=tuple(
                EffectFencePausedBranchResumeState(
                    branch_index=_bi,
                    step_id=str(worker_steps[_bi].step_id),
                    step_kind=str(worker_steps[_bi].step_kind.value),
                    idempotency_key=_fence_key,
                )
                for _bi, _fence_key in sorted(effect_fence_paused_dispositions.items())
            )
            + crash_pause_reconstruct_fence_paused,
            # B-FANOUT-PAUSE-SYNTHESIS — capture the terminal POST_JOIN_SYNTHESIS
            # step's identity (presence + step_id) so resume can material-diff it
            # before fresh-dispatching. None when no synthesis was opted in (the
            # common case — drop-from-hash-when-None keeps those snapshots
            # byte-identical). HIERARCHICAL_DELEGATION reuses this function per
            # level, so a child level's synthesis is captured into the child's own
            # snapshot here too.
            synthesis_step_id=(str(synthesis_step.step_id) if synthesis_step is not None else None),
        )
        # B-FANOUT-EFFECT-FENCE-BRANCH-PAUSE — label EFFECT_FENCE_AMBIGUOUS when a worker
        # fence-paused this round so the operator surface knows to supply an EffectFenceResolution
        # (Codex [P2]; the parallelization analogue).
        _pause_reason = (
            WorkflowPauseReason.EFFECT_FENCE_AMBIGUOUS
            if effect_fence_paused_dispositions
            else WorkflowPauseReason.EXPLICIT_OPERATOR
        )
        snapshot = _run_protocol_method_sync(
            cast(PauseResumeProtocol, protocol).capture_pause_snapshot(
                workflow_id=workflow_id,
                run_id=run_id,
                step_index=0,
                pause_reason=_pause_reason,
                fan_out_resume=fan_out_resume,
            )
        )
        return _finish(RunStatus.PAUSED, fail_class=None, salvage=True, pause_snapshot=snapshot)
    # No worker failed THIS round. But a RECOVERED terminal branch may have failed
    # in the original run (a resume tail) — a terminal branch with no collected
    # output is a failed/timed-out branch (`_record_clean` always populates
    # `collected` for a clean worker). Returning a bare SUCCESS there would SILENTLY
    # drop that failure (the silent-degradation class this arc forecloses) — instead
    # mirror `proceed`: degraded → PARTIAL + salvage (advisor [P1]). Non-resume
    # clean runs have no failed terminal → not degraded → SUCCESS (no regression).
    # B-FANOUT-EFFECT-FENCE-PER-BRANCH-SCOPED-ABORT (R-FS-1, CP spec v1.73 §1) — ALL-ABORT GUARD:
    # if every fence-paused worker was scoped-aborted and NO worker survived (nothing collected),
    # the run has NO result → FAILED (with the operator-abort provenance fail_class — conventional
    # on a FAILED status), not the vacuous PARTIAL the degraded check below would return with zero
    # survivors. A PARTIAL WITH survivors carries `fail_class=None` like every degraded PARTIAL
    # (the aborted worker is a degraded terminal non-contributor recorded per-branch in the ledger).
    if _scoped_abort_ordinals and not collected:
        return _finish(
            RunStatus.FAILED,
            fail_class="orchestrator-workers-effect-fence-branch-aborted",
            salvage=True,
        )
    if any(_bi not in collected for _bi in terminal_dispositions):
        return _finish(RunStatus.PARTIAL, fail_class=None, salvage=True)
    return _finish(RunStatus.SUCCESS, fail_class=None, salvage=False)


def _execute_hierarchical_delegation(
    *,
    manifest_entry: WorkflowManifestEntry,
    steps: Sequence[WorkflowStep],
    run_id: str,
    ctx: DriverContext,
    default_model_binding: ModelBinding,
    step_dispatchers: StepDispatcherRegistry,
    run_idempotency_key: str,
    resume_snapshot: PauseSnapshot | None = None,
    crash_fan_out_resume: FanOutResumeState | PeerFanOutResumeState | None = None,
    crash_pause_reconstruct_no_dispatch: bool = False,
    crash_pause_reconstruct_fence_paused: tuple[EffectFencePausedBranchResumeState, ...] = (),
    pause_resumable: bool = False,
    reconciler_engine_resume_required: bool = False,
    synthesis_step: WorkflowStep | None = None,
) -> tuple[RunResult, int]:
    """Execute the `HIERARCHICAL_DELEGATION` recursive bounded-fan-out strategy (U-CP-89).

    HIERARCHICAL_DELEGATION is **recursive `ORCHESTRATOR_WORKERS` with depth**
    (C-CP-25 §25.11 row): at each level `steps[0]` is the orchestrator/parent and
    `steps[1:]` are its direct children (workers); a worker of kind
    `SUB_AGENT_DISPATCH` recurses — its dispatcher re-enters `execute_workflow`
    with the child's own manifest + step sequence (the existing C-RT-17 §14.7.4
    `ChildWorkflowRunner` seam), and when that child manifest declares
    `HIERARCHICAL_DELEGATION` the recursion re-enters HERE, so the cap-3 +
    gate-level descent + bottom-up barrier composition hold at EVERY level.

    This strategy adds exactly two things over `ORCHESTRATOR_WORKERS` (U-CP-88),
    which it **REUSES at each level (NOT a parallel re-implementation — the AC):**

    1. **Materialization** — a manifest may declare `HIERARCHICAL_DELEGATION`, so
       a recursive child re-enters this strategy (vs the uncapped
       `ORCHESTRATOR_WORKERS`).
    2. **The fan-out cap 3 per parent** (`_HIERARCHICAL_DELEGATION_FANOUT_CAP`;
       C-CP-10 §10.3 / §25.11 "recursive *bounded*-fan-out"): a level with more
       than 3 direct children is rejected `detect-then-refuse` (`RunStatus.FAILED`,
       no `workflow.start` emit / no ledger append — parity with the
       topology/engine-class entry gate), NEVER silently truncated.

    Everything else is the `ORCHESTRATOR_WORKERS` strategy verbatim: the
    orchestrator parents the fan-out (`steps[0].action_id`); workers fan out
    concurrently under per-role child contexts whose gate-level descends per
    C-CP-12 §12.2 (`compose_branch_child_context`, monotonic — equality default);
    each parent barriers on its children and composes the deterministic
    branch-index fold (bottom-up); `cascade_policy` governs the on-failure
    reaction (§25.15). The nested barrier deadline composes — `cascade_cancel_barrier`
    extends (not replaces) the `_BRANCH_INFLIGHT_DISPATCHES` chain, so an OUTER
    level's deadline stays a hard cap over an inner-level in-flight dispatch.
    Returns `(RunResult, steps_executed)` for the `_execute_workflow_body` caller.

    **Gate-level descent across the recursion boundary (honest scope).** The
    sub-agent gate-level descent (C-CP-12 §12.2) is COMPUTED + RECORDED at the
    `SUB_AGENT_DISPATCH` dispatch boundary (the runtime
    `RuntimeHandoffRegistry.dispatch` → `dispatch_sub_agent`), but the child's
    EXECUTED gate-level re-seeds from its own manifest — the harness-computed
    descent is recorded-not-applied at the child run (pre-existing v1.6 MVP
    child-context sharing, `child_workflow_runner.py` module docstring). Strict
    cross-level *executed* descent is a v1.7+/B4-adjacent arc
    (`.harness/class_3_hierarchical_delegation_descent_recorded_not_applied.md`);
    §12.2 itself is monotonic-≤ with equality as the valid default, so the
    within-level worker descent (`compose_branch_child_context`) + the recorded
    boundary descent satisfy the monotonic invariant.
    """
    workflow_id = manifest_entry.workflow_id

    # Fan-out cap 3 per parent (C-CP-10 §10.3 / §25.11 "recursive bounded-fan-out").
    # steps[0] = this level's orchestrator (parent); steps[1:] = its direct children.
    # detect-then-refuse: a level exceeding the cap FAILS loud with no side effects
    # (no workflow.start emit, no ledger append) — never a silent truncation. The cap
    # re-checks at every recursion level whose child manifest declares
    # HIERARCHICAL_DELEGATION (the recursion re-enters this function).
    worker_count = max(0, len(steps) - 1)
    if worker_count > _HIERARCHICAL_DELEGATION_FANOUT_CAP:
        return RunResult(
            workflow_id=workflow_id,
            run_id=run_id,
            status=RunStatus.FAILED,
            terminal_step_index=None,
            partial_state=None,
            final_state=None,
            fail_class=(
                f"hierarchical-delegation-fanout-cap-exceeded: {worker_count} children "
                f"> cap {_HIERARCHICAL_DELEGATION_FANOUT_CAP} (C-CP-10 §10.3)"
            ),
        ), 0

    # Reuse ORCHESTRATOR_WORKERS at this level (U-CP-88; the AC: "reuses
    # ORCHESTRATOR_WORKERS at each level — NOT a parallel re-implementation").
    # Recursion-with-depth emerges through SUB_AGENT_DISPATCH workers re-entering
    # the driver per the child manifest's topology.
    #
    # B-HIERARCHICAL-PAUSE (R-FS-1) — thread the resume snapshot + `pause_resumable`
    # so a `cascade_policy=pause` worker failure AT THIS LEVEL materializes a genuine
    # resumable PAUSED (was the honest `...-not-yet-materialized` FAILED), AND a
    # recursive child that PAUSED (a grandchild) is captured + re-entered at its
    # cursor on resume — the orchestrator-workers helper does both; this site just
    # opts HIERARCHICAL in (the same way the top-level ORCHESTRATOR_WORKERS dispatch
    # does), so the materialization is now wired for the recursion-heavy topology.
    return _execute_orchestrator_workers(
        manifest_entry=manifest_entry,
        steps=steps,
        run_id=run_id,
        ctx=ctx,
        default_model_binding=default_model_binding,
        step_dispatchers=step_dispatchers,
        run_idempotency_key=run_idempotency_key,
        resume_snapshot=resume_snapshot,
        # B-FANOUT-OUTPUT-REPLAY (R-FS-1) — forward the synthetic crash-resume state into
        # the per-level orchestrator-workers execution (each recursion level captures +
        # recovers against its own run-keyed store; this top level recovers here).
        crash_fan_out_resume=crash_fan_out_resume,
        # B-FANOUT-CRASH-RESUME-PAUSE-RECONSTRUCT-NOT-YET-DISPATCHED — forward the re-pause-
        # without-dispatch mode into the per-level orchestrator-workers execution.
        crash_pause_reconstruct_no_dispatch=crash_pause_reconstruct_no_dispatch,
        # B-FANOUT-CRASH-RESUME-PAUSE-RECONSTRUCT-MAYBE-RAN-FENCE-STEP-ID — forward the
        # fence-recoverable maybe-ran carriers into the per-level orchestrator-workers execution.
        crash_pause_reconstruct_fence_paused=crash_pause_reconstruct_fence_paused,
        pause_resumable=pause_resumable,
        reconciler_engine_resume_required=reconciler_engine_resume_required,
        # B-POSTJOIN-LLM-SYNTHESIS (CP spec v1.54 §3) — TOP-LEVEL synthesis only:
        # the recursion re-enters via a SUB_AGENT_DISPATCH worker's own
        # execute_workflow on the CHILD manifest, which carves its own synthesis
        # at its own dispatch site, so this top-level `synthesis_step` never leaks
        # into a recursive level (synthesis-per-level is the registered follow-on).
        synthesis_step=synthesis_step,
    )


def _compose_handoff_to_next(
    *,
    completed_action_id: str,
    completed_context: StepExecutionContext,
    next_step: WorkflowStep,
    next_role: AgentRole,
    actor_identity: ActorIdentity,
) -> HandoffContext:
    """Compose the C-CP-13 `HandoffContext` recording the ownership transfer from a
    just-completed stage to the next stage-expert (C-CP-25 §25.11 DECENTRALIZED_HANDOFF).

    A RECORD, not a dispatch (the next stage runs through the ordinary
    `StepDispatcher`, never `SUB_AGENT_DISPATCH`). The MVP composition mirrors the
    runtime `sub_agent_dispatch` precedent exactly: `audit_trail_link` /
    `state_summary.relevant_entries` anchor the handing-off stage's ledger entry
    (`entry_hash` = the stage context's `parent_entry_hash` — `""` on the buffered
    path, the same value the existing composer passes); `summary_hash = sha256(b"")`
    (the v1.6 MVP default); the deliberation fields (`failed_attempts` /
    `alternatives_considered` / `retry_history` / `external_references`) are empty.
    `proposed_action` names the next stage-expert — `SUB_AGENT_DISPATCH` is the
    C-CP-17 §17.1 sub-agent-boundary placement a handoff *is* — and its payload
    carries only the next stage's *identity* (step_id + role): control-flow
    metadata, NOT the prior stage's output (the harness threads NO inter-step data
    for any topology, B-INTERSTEP)."""
    from harness_is.state_ledger_entry_schema import Identifier as _Identifier

    entry_ref = LedgerEntryRef(
        action_id=ActionID(completed_action_id),
        entry_hash=completed_context.parent_entry_hash,
        actor=actor_identity,
    )
    return HandoffContext(
        proposed_action=ProposedAction(
            action_kind=ActionKind.SUB_AGENT_DISPATCH,
            payload={"next_stage": str(next_step.step_id), "next_role": str(next_role)},
            brief=None,
        ),
        agent_confidence=None,
        failed_attempts=(),
        alternatives_considered=(),
        state_summary=StateSummary(
            relevant_entries=(entry_ref,),
            summary_text="",
            summary_hash=hashlib.sha256(b"").hexdigest(),
            idempotency_key=_Identifier(completed_context.parent_idempotency_key),
            external_references=(),
        ),
        audit_trail_link=entry_ref,
        retry_history=RetryHistory(attempts=(), retry_count=0),
    )


def _handoff_record(handoff: HandoffContext) -> dict[str, Any]:
    """Serialize a `HandoffContext` into the deterministic `final_state` surface —
    the observable ownership-transfer chain the AC asserts ("hands ownership
    stage-to-stage via HandoffContext")."""
    return {
        "from_action_id": str(handoff.audit_trail_link.action_id),
        "to_stage": handoff.proposed_action.payload["next_stage"],
        "to_role": handoff.proposed_action.payload["next_role"],
        "action_kind": handoff.proposed_action.action_kind.value,
    }


def _execute_decentralized_handoff(
    *,
    manifest_entry: WorkflowManifestEntry,
    steps: Sequence[WorkflowStep],
    run_id: str,
    ctx: DriverContext,
    default_model_binding: ModelBinding,
    step_dispatchers: StepDispatcherRegistry,
    run_idempotency_key: str,
    resume_snapshot: PauseSnapshot | None = None,
) -> tuple[RunResult, int]:
    """Execute the `DECENTRALIZED_HANDOFF` single-owner sequential handoff strategy (U-CP-90).

    Each declared step is a stage-expert that OWNS the workflow in turn, then hands
    ownership to the next via a `HandoffContext` (C-CP-13). Single-owner-at-a-time:
    stages run strictly sequentially on the driver thread (NO fan-out, NO
    `TaskGroup` — there is never more than one owner). Two consequences fall out of
    "sequential": there are no concurrent drains (it sidesteps the arc-15 F1-01
    sibling-drain timestamp gap), and each stage dispatches through the ordinary
    `StepDispatcher` (NEVER `SUB_AGENT_DISPATCH`), so a real multi-stage e2e
    genuinely SUCCEEDS — no sync/async-bridge recursion (the `HandoffContext` is a
    RECORD, not a dispatch).

    Non-hollow by ledger construction (the persisted distinction):
    - vs `EVALUATOR_OPTIMIZER` (no `branch_metadata` at all) — every stage persists
      `branch_metadata` (it is a per-role branch entry).
    - vs `ORCHESTRATOR_WORKERS` (a STAR — every worker's
      `branch_metadata.parent_action_id` is the ONE orchestrator) — here it CHAINS:
      stage *i*'s `branch_metadata.parent_action_id` is stage *(i-1)*'s `action_id`
      (the durable "who handed to whom" record); `branch_index` stays 0 (single
      owner — no siblings; the ordering rides the chain, not the fan-out ordinal).
      Stage 0 anchors at the workflow origin `workflow:{wf}:step:0`.
    Each stage is a per-role expert (`AgentRole(str(step.step_id))` → distinct binding
    via U-RT-114; the per-role catalog is B4); ownership transfers via a composed
    `HandoffContext` surfaced in `final_state["handoffs"]`.

    Terminal when no further handoff — structural: the declared step list IS the
    handoff sequence, terminal = after the last stage (no continue-signal read from
    step output — terminal CONTROL flow stays structural). Inter-step DATA flow,
    however, IS wired (B-INTERSTEP-NONLINEAR handoff slice, §14.21 C-RT-34): each
    completed stage's output is recorded so the next stage-expert's dispatch reads
    it as upstream context (single-owner sequential → recorded inline like the
    linear/EO sites, no buffered-branch drain). On a stage failure the chain stops (`cascade_policy`
    is degenerate for single-owner — no concurrent in-flight sibling to cancel):
    `cascade-cancel` → FAILED, `proceed` → PARTIAL (completed stages salvaged).

    `pause → PAUSED` resume (R-FS-1 `B-HANDOFF-PAUSE`, CP spec v1.46 §2): a TEAM-tier
    stage failure under `pause` (with a bound `pause_resume_protocol`) captures a
    `HandoffResumeState`-bearing `PauseSnapshot` (the completed-stage prefix + their
    recovered outputs + the declared stage count) + returns PAUSED; `api.resume`
    re-enters here with `resume_snapshot`, RE-WALKS the body recovering the completed
    prefix's outputs (NOT re-dispatched — effect may have landed), and re-dispatches
    from the cursor stage onward. The recompute is deterministic, so the resumed stage's
    `parent_action_id` chains off the last completed stage's `action_id` (the handoff
    causality is INHERENT, not a carried string). When no protocol is bound, `pause`
    fails HONESTLY (`decentralized-handoff-pause-resume-protocol-not-bound`) — never a
    false-resumable PAUSED. This materializes the §25.15.1 `pause → PAUSED` row EXTENDED
    to single-owner sequential per §25.18's named `DECENTRALIZED_HANDOFF` impl-order (the
    §25.15.1 row text is fan-out-barrier-scoped; the extension is impl-discretion, not a
    re-reading of the row). Returns `(RunResult, steps_executed)` for the
    `_execute_workflow_body` caller.
    """
    workflow_id = manifest_entry.workflow_id

    # B-HANDOFF-PAUSE (R-FS-1) — single-owner sequential handoff resume reconstruction
    # state (None on a normal first run). The stage cursor: a CONTIGUOUS completed
    # prefix (stage_index 0..k-1) recovered on resume + the re-dispatchable tail (k..n).
    # Unlike the fan-out carriers (a branch set with re-dispatchable gaps), a handoff
    # resume is a strictly sequential prefix — there is no orchestrator step[0] and no
    # absent-ordinal gap (the pause is a single failed stage k after a contiguous run).
    _handoff_resume = resume_snapshot.handoff_resume if resume_snapshot is not None else None
    if _handoff_resume is not None:
        # Material-diff guard on resume: the re-supplied body's stage count MUST match
        # the count captured at pause, the recovered prefix MUST be contiguous 0..k-1,
        # and each recovered stage ordinal MUST still resolve to the same `step_id`
        # (identity, not just count) — else the recovered output no longer maps to that
        # stage (a changed body) → fail closed rather than replay a stale output into a
        # different stage. Mirrors the PARALLELIZATION resume-body-mismatch guard.
        def _resume_body_mismatch() -> str | None:
            assert _handoff_resume is not None  # narrowed by the enclosing guard
            assert resume_snapshot is not None  # _handoff_resume non-None ⇒ snapshot non-None
            # Cross-field coherence (out-of-family Codex [P2]): the cursor length MUST
            # equal the snapshot's advertised pause `step_index`. At capture both are the
            # failed stage k (`step_index=k`, `len(completed_stages)=k`); a caller-supplied
            # or durably-read snapshot that is internally hash-valid but INCOHERENT
            # (`step_index` ≠ prefix length) would otherwise resume from the cursor-derived
            # position, silently re-dispatching stages the pause step says already
            # completed (an at-most-once violation). Fail closed on disagreement.
            if len(_handoff_resume.completed_stages) != resume_snapshot.step_index:
                return (
                    f"cursor-step-index-mismatch: {len(_handoff_resume.completed_stages)} "
                    f"completed stages but snapshot.step_index={resume_snapshot.step_index} "
                    f"(the prefix length must equal the paused stage index)"
                )
            if len(steps) != _handoff_resume.stage_count:
                return (
                    f"stage-count-mismatch: snapshot captured "
                    f"{_handoff_resume.stage_count} stages, resume supplied {len(steps)}"
                )
            for expected_index, cs in enumerate(_handoff_resume.completed_stages):
                if cs.stage_index != expected_index:
                    return (
                        f"non-contiguous-prefix at position {expected_index}: "
                        f"stage_index={cs.stage_index} (a handoff prefix must be 0..k-1)"
                    )
                # Bounds-check BEFORE indexing `steps` (out-of-family Codex [P2]; mirrors
                # the PARALLELIZATION branch-index-out-of-range guard): a contiguous
                # prefix LONGER than the body (with a matching stage_count, an incoherent
                # snapshot) would otherwise IndexError on `steps[cs.stage_index]` below
                # rather than fail closed cleanly.
                if not (0 <= cs.stage_index < len(steps)):
                    return f"stage-index-out-of-range: {cs.stage_index} ∉ [0, {len(steps)})"
                if str(steps[cs.stage_index].step_id) != cs.step_id:
                    return (
                        f"stage-identity-mismatch at {cs.stage_index}: snapshot "
                        f"step_id={cs.step_id!r}, resume step_id="
                        f"{str(steps[cs.stage_index].step_id)!r}"
                    )
            if len(_handoff_resume.completed_stages) >= len(steps):
                # The whole body completed → there is no failed tail stage to resume
                # from (a pause is captured ONLY on a stage FAILURE, so the cursor is
                # always a STRICT prefix). A full prefix is an incoherent/tampered cursor.
                return (
                    f"no-resumable-stage: {len(_handoff_resume.completed_stages)} completed "
                    f"stages but only {len(steps)} declared (pause requires a failed tail stage)"
                )
            return None

        _mismatch = _resume_body_mismatch()
        if _mismatch is not None:
            return RunResult(
                workflow_id=workflow_id,
                run_id=run_id,
                status=RunStatus.FAILED,
                terminal_step_index=None,
                partial_state=None,
                final_state=None,
                fail_class=f"decentralized-handoff-resume-body-mismatch: {_mismatch}",
            ), 0

    # Empty step sequence → trivially SUCCESS (mirrors the other strategies). Placed
    # AFTER the resume guard (out-of-family Codex [P2]): a resume always carries
    # `stage_count >= 1` (a pause is captured on a stage FAILURE), so an empty/short
    # body + a non-empty cursor is caught by the stage-count guard above → FAILED, never
    # a silent SUCCESS that drops the recovered prefix. A genuine non-resume empty
    # workflow (`_handoff_resume is None`) skips the guard + lands here.
    if not steps:
        return RunResult(
            workflow_id=workflow_id,
            run_id=run_id,
            status=RunStatus.SUCCESS,
            terminal_step_index=None,
            partial_state=None,
            final_state={"stages": {}, "handoffs": []},
            fail_class=None,
        ), 0

    # stage_index -> recovered output for the completed prefix (replayed, NOT
    # re-dispatched); empty on a normal first run.
    _recovered_outputs: dict[int, Mapping[str, Any]] = (
        {cs.stage_index: cs.output for cs in _handoff_resume.completed_stages}
        if _handoff_resume is not None
        else {}
    )
    _resume_completed_count = (
        len(_handoff_resume.completed_stages) if _handoff_resume is not None else 0
    )

    # The on-stage-failure cascade reaction (§25.15.1) — resolved from the manifest's
    # (workload_class, engine_class, persona_tier) via the §11.4 D4 tunable, the same
    # source ORCHESTRATOR_WORKERS reads (SOLO→proceed / TEAM→pause / MTC→cascade-cancel;
    # the §25.11 DECENTRALIZED_HANDOFF row notes cascade-cancel is the typical
    # single-owner case).
    cascade_policy = d4_tunable(
        lookup_cell(manifest_entry.workload_class, manifest_entry.engine_class),
        manifest_entry.persona_tier,
    ).cascade_policy

    _resolver = getattr(ctx, "procedural_tier_snapshot_resolver", None)
    snapshot_ref = _resolver() if _resolver is not None else None

    # Buffer-time placeholder; the drain re-stamps to one drain-moment value. The
    # sequential chain is causally ordered, so this is monotonic by construction (no
    # concurrent drain — the F1-01 gap is structurally unreachable here).
    handoff_timestamp = datetime.now(UTC)
    actor_identity = ActorIdentity(ctx.ledger_writer.actor.actor_id)

    # § 25.3.2 — workflow.start (single-threaded on the driver thread). A handoff
    # RESUME re-enters the SAME envelope (the completed prefix already ran in the
    # original run), so it emits RESUMPTION — not a second WORKFLOW_START — mirroring
    # `_execute_parallelization` / `_execute_orchestrator_workers`.
    ctx.lifecycle_emitter.emit(
        WorkflowEventClass.RESUMPTION
        if _handoff_resume is not None
        else WorkflowEventClass.WORKFLOW_START
    )

    stage_writers: list[BufferingLedgerWriter] = []
    stage_outputs: dict[str, Mapping[str, Any]] = {}
    handoffs: list[HandoffContext] = []
    # B-HANDOFF-PAUSE — the completed-stage records (recovered prefix + this-run
    # completions), captured into a `HandoffResumeState` cursor on a `pause` halt. A
    # RE-pause unions the recovered prefix + the newly-completed stages, so this stays
    # a contiguous prefix across repeated resumes.
    completed_stage_records: list[HandoffStageResumeState] = []
    # Stage 0 anchors at the workflow origin; each later stage chains off its
    # predecessor's action_id (the persisted handoff chain — the non-hollow signal).
    prev_action_id = f"workflow:{workflow_id}:step:0"

    def _finish(
        status: RunStatus,
        *,
        fail_class: str | None,
        salvage: bool,
        pause_snapshot: PauseSnapshot | None = None,
    ) -> tuple[RunResult, int]:
        # Drain the COMPLETED-stage writers in stage order (writer.branch_index =
        # stage ordinal) + emit one STEP_BOUNDARY per stage that ran. On resume, the
        # recovered prefix added NO writers (its ledger entries persisted in the
        # original run), so steps_executed counts only the newly-dispatched stages.
        steps_executed = _drain_and_emit_step_boundaries(ctx, stage_writers)
        aggregate = {
            "stages": {sid: dict(out) for sid, out in stage_outputs.items()},
            "handoffs": [_handoff_record(h) for h in handoffs],
        }
        return RunResult(
            workflow_id=workflow_id,
            run_id=run_id,
            status=status,
            terminal_step_index=None,
            partial_state=aggregate if salvage else None,
            final_state=aggregate if status is RunStatus.SUCCESS else None,
            fail_class=fail_class,
            # B-HANDOFF-PAUSE — PAUSED carries the salvaged aggregate as partial_state
            # (above) + the resumable stage-cursor snapshot.
            pause_snapshot=pause_snapshot,
        ), steps_executed

    def _append_handoff_if_not_terminal(
        stage_index: int,
        completed_action_id: str,
        completed_context: StepExecutionContext,
    ) -> None:
        # Compose the ownership transfer to the next stage-expert (a RECORD; surfaced
        # in final_state). Terminal stage → no further handoff (structural). Shared by
        # the live-dispatch path + the B-HANDOFF-PAUSE resume-recovery path so a
        # resumed run reconstructs the SAME handoff chain (the recovered prefix's
        # handoff records are rebuilt deterministically, not carried in the snapshot).
        if stage_index < len(steps) - 1:
            next_step = steps[stage_index + 1]
            handoffs.append(
                _compose_handoff_to_next(
                    completed_action_id=completed_action_id,
                    completed_context=completed_context,
                    next_step=next_step,
                    # B4 Slice 4 — the handoff-record preview of the next stage's
                    # role must reflect a per-step override so the audit record
                    # matches the role the next stage's loop iteration will fold
                    # in (precedence per-step > derived); CP spec v1.38 §6.1.
                    next_role=(
                        _per_step_role_override(manifest_entry, next_step.step_id)
                        or derive_agent_role(next_step.step_id)
                    ),
                    actor_identity=actor_identity,
                )
            )

    for stage_index, step in enumerate(steps):
        # Resolve the per-step binding FIRST (deterministic, pure — matches the
        # linear/orchestrator sites that call it outside the dispatch try) so a
        # per-step ROLE override (CP spec v1.38 §6.1, B4 Slice 4) can take
        # precedence over the stage's derived role (precedence per-step > derived).
        binding = resolve_step_binding(
            manifest_entry,
            str(step.step_id),
            default_model_binding=default_model_binding,
            persona_tier=manifest_entry.persona_tier,
        )
        role = binding.agent_role or derive_agent_role(step.step_id)
        # The spawning context the next stage descends from: its parent_action_id is
        # the prior stage's action_id (the chain; the workflow origin anchors stage 0).
        spawning = StepExecutionContext(
            workflow_id=workflow_id,
            parent_action_id=prev_action_id,
            parent_gate_level=resolve_parent_gate_level(manifest_entry),
            # B-HITL-PLACEMENT-PER-STEP-PRODUCER — hierarchical/handoff stage ctx
            # (stage_ctx inherits via compose_branch_child_context's model_copy).
            # B-HITL-PLACEMENT-PER-STEP-OVERRIDE-FOLD (CP spec v1.49 §6.2) — fold
            # this stage's per-step `binding.hitl_placement` override onto the
            # workflow tuple (union-by-position, tune-not-remove, monotone).
            hitl_placements=fold_step_hitl_placements(
                manifest_entry.hitl_placements, binding.hitl_placement
            ),
            # B-EFFECT-FENCE-DURABLE-AUTO — the RUN engine class (NOT a per-step
            # StepOverride.engine_class) so the tool dispatcher auto-fences durable runs.
            run_engine_class=manifest_entry.engine_class,
            parent_sandbox_tier=SandboxTier.TIER_1_PROCESS,
            parent_actor=ctx.ledger_writer.actor,
            parent_entry_hash="",
            parent_idempotency_key=_compute_step_idempotency_key(run_idempotency_key, stage_index),
            tenant_id=ctx.tenant_id,
            step_index=stage_index,
        )
        # Single owner → branch_index 0 (no siblings; causality rides the chained
        # parent_action_id, NOT the fan-out ordinal). step_index = the declared stage
        # ordinal (the dispatcher reads it for per-step policy / audit / skill hook).
        stage_ctx = compose_branch_child_context(
            spawning, branch_index=0, agent_role=role
        ).model_copy(update={"step_index": stage_index})
        this_action_id = compose_branch_step_action_id(stage_ctx, 0)
        # B-HANDOFF-PAUSE (R-FS-1) — RESUME recovery of the completed prefix. On resume,
        # stages 0..k-1 already dispatched + persisted their ledger entries in the
        # ORIGINAL run; re-walk them here WITHOUT re-dispatching: recover the output from
        # the cursor, replay it into the aggregate + the inter-step channel, rebuild the
        # handoff record, and DO NOT create a writer / append a ledger entry (no
        # double-write; no step-count). The binding/ctx/action_id were just recomputed
        # deterministically, so `prev_action_id` chains through the prefix EXACTLY as the
        # original run did — the resumed stage k's parent_action_id is the last completed
        # stage's action_id (the handoff causality is INHERENT in the recompute, not a
        # carried string the snapshot could drift on). `[[full-chain-witness-not-half-
        # proofs]]`: a recovered stage's dispatcher is NEVER invoked across the resume.
        if stage_index < _resume_completed_count:
            recovered_output = _recovered_outputs[stage_index]
            stage_outputs[str(step.step_id)] = recovered_output
            # Re-seed the inter-step channel so the FIRST re-dispatched stage (k) reads
            # stage (k-1)'s recovered output as its upstream context (B-INTERSTEP-HANDOFF;
            # the run-scoped channel starts empty on this fresh resume invocation).
            _record_inter_step_output(ctx, str(step.step_id), recovered_output)
            completed_stage_records.append(
                HandoffStageResumeState(
                    stage_index=stage_index,
                    step_id=str(step.step_id),
                    output=recovered_output,
                )
            )
            _append_handoff_if_not_terminal(stage_index, this_action_id, stage_ctx)
            prev_action_id = this_action_id
            continue
        # writer.branch_index = the stage ordinal — the DRAIN-order key (distinct
        # from the entry's branch_metadata.branch_index=0; different layers). One
        # writer per stage → one STEP_BOUNDARY per stage via _drain_and_emit_*.
        writer = BufferingLedgerWriter(actor=ctx.ledger_writer.actor, branch_index=stage_index)
        # B-NONLINEAR-OVERRIDE-PROVENANCE — register the stage writer + buffer its
        # per-step override entry BEFORE dispatch (uniform with the parallelization
        # / worker / orchestrator / evaluator-optimizer sites + the linear path: the
        # override is a resolution-time binding fact, recorded before dispatch). The
        # writer is added to stage_writers HERE so every failure path (`_finish`
        # drains stage_writers) persists the override even on a failed stage; its
        # `_writer_ran_a_step` is False (override-only, no step entry) so a failed
        # stage adds no STEP_BOUNDARY / step-count.
        stage_writers.append(writer)
        _buffer_branch_override_if_applied(
            branch_writer=writer,
            workflow_id=workflow_id,
            step=step,
            binding=binding,
            timestamp=handoff_timestamp,
            snapshot_ref=snapshot_ref,
        )
        # Pre-flight the dispatcher resolution (out-of-family Codex [P2]; mirrors
        # `_execute_parallelization`): an UNBOUND `StepKind` is a SETUP/config error,
        # NOT a stage dispatch failure — it must fail the run LOUD (FAILED), never be
        # swallowed into the cascade `pause`→PAUSED / `proceed`→PARTIAL conversion below
        # (a resumable PAUSED for a config error would just loop on resume, and a PARTIAL
        # would silently degrade a setup bug). Resolved OUTSIDE the stage-failure `try`.
        try:
            dispatcher = step_dispatchers.lookup(step.step_kind)
        except StepKindDispatcherNotBoundError as exc:
            return _finish(
                RunStatus.FAILED,
                fail_class=f"decentralized-handoff-step-kind-not-bound: {exc}",
                salvage=False,
            )
        try:
            output = dispatcher.dispatch(binding, step, step_context=stage_ctx)
        except SubAgentChildPausedError as child_paused:
            # A SUB_AGENT_DISPATCH stage whose recursive child sub-workflow PAUSED raises
            # the typed SubAgentChildPausedError (#680). DECENTRALIZED_HANDOFF does NOT
            # materialize the cross-recursion child-pause disposition (that is the
            # ORCHESTRATOR_WORKERS `paused_child_branches` machinery; handoff is
            # single-owner sequential + the §25.11 row dispatches stages through the
            # ordinary StepDispatcher, NOT SUB_AGENT_DISPATCH). Catching it via the
            # generic `except Exception` below + converting it into a handoff-level PAUSE
            # would DROP the child's OWN cursor (the #680 swallow-bug, one level over) —
            # a false-resumable handoff-PAUSE that silently loses the child's suspended
            # state. Fail CLOSED honestly (out-of-family Codex [P2]); the completed prefix
            # still salvages.
            return _finish(
                RunStatus.FAILED,
                fail_class=(
                    f"decentralized-handoff-child-pause-unsupported: stage {stage_index} "
                    f"sub-agent child PAUSED ({child_paused}); the cross-recursion "
                    "child-pause disposition is not materialized for DECENTRALIZED_HANDOFF"
                ),
                salvage=True,
            )
        except Exception as exc:
            # A stage owner failed → the chain stops (single-owner: no in-flight
            # sibling to cancel; the failed stage buffered nothing). cascade_policy
            # governs the disposition over the COMPLETED-stage prefix.
            if cascade_policy is CascadePolicy.PROCEED:
                return _finish(
                    RunStatus.PARTIAL,
                    fail_class=_step_fail_class("decentralized-handoff-stage-failure", exc),
                    salvage=True,
                )
            if cascade_policy is CascadePolicy.PAUSE:
                # B-HANDOFF-PAUSE (R-FS-1; CP spec v1.46 §2) — materialize the §25.15.1
                # `pause → PAUSED` row EXTENDED to single-owner sequential (per §25.18's
                # named DECENTRALIZED_HANDOFF impl-order). The completed-stage prefix
                # (0..k-1) is captured into a hash-integrity-checked HandoffResumeState
                # stage cursor; `api.resume` re-enters here, recovers the prefix (NOT
                # re-dispatched), and re-dispatches from stage k. The failed stage k
                # buffered nothing (single-owner: no in-flight sibling to cancel).
                protocol = getattr(ctx, "pause_resume_protocol", None)
                if protocol is None:
                    # No pause/resume opt-in bound → the snapshot cannot be captured, so
                    # a PAUSED would advertise a resumability the harness cannot honor
                    # (the FALSE-`PAUSED` silent-degradation mode). Fail HONESTLY —
                    # detect-then-refuse, mirroring _execute_parallelization's
                    # FAILED + salvage (NOT proceed's PARTIAL — a pause that cannot be
                    # honored is a failure, not a graceful degradation). The
                    # completed-stage prefix still salvages as partial_state.
                    return _finish(
                        RunStatus.FAILED,
                        fail_class=(
                            "decentralized-handoff-pause-resume-protocol-not-bound: "
                            f"stage {stage_index} failed under cascade_policy=pause but no "
                            "pause_resume_protocol is bound (cannot capture a resumable "
                            f"snapshot) — underlying: {type(exc).__name__}: {exc}"
                        ),
                        salvage=True,
                    )
                handoff_resume = HandoffResumeState(
                    completed_stages=tuple(completed_stage_records),
                    stage_count=len(steps),
                )
                snapshot = _run_protocol_method_sync(
                    cast(PauseResumeProtocol, protocol).capture_pause_snapshot(
                        workflow_id=workflow_id,
                        run_id=run_id,
                        step_index=stage_index,
                        pause_reason=WorkflowPauseReason.EXPLICIT_OPERATOR,
                        handoff_resume=handoff_resume,
                    )
                )
                return _finish(
                    RunStatus.PAUSED, fail_class=None, salvage=True, pause_snapshot=snapshot
                )
            return _finish(
                RunStatus.FAILED,
                fail_class=_step_fail_class("decentralized-handoff-stage-failure", exc),
                salvage=False,
            )
        # Persist the stage as a per-role branch entry whose branch_metadata chains
        # off the prior stage (causality) + a fresh `completed` terminal entry (U-CP-84).
        append_branch_step_ledger_entry(
            branch_writer=writer,
            branch_context=stage_ctx,
            run_idempotency_key=run_idempotency_key,
            local_step_index=0,
            timestamp=handoff_timestamp,
            procedural_tier_snapshot_ref=snapshot_ref,
        )
        append_branch_terminal_ledger_entry(
            branch_writer=writer,
            branch_context=stage_ctx,
            run_idempotency_key=run_idempotency_key,
            terminal_status="completed",
            timestamp=handoff_timestamp,
            procedural_tier_snapshot_ref=snapshot_ref,
        )
        stage_outputs[str(step.step_id)] = output
        # B-INTERSTEP-NONLINEAR (handoff slice; runtime spec §14.21 C-RT-34) —
        # record this completed stage's output so the NEXT stage's dispatch reads
        # it as upstream context (the runtime LLM dispatcher injects
        # `most_recent_output()`; §14.21.2). Single-owner sequential on the driver
        # thread (NO fan-out — see this function's docstring) → recorded INLINE,
        # exactly like the SINGLE_THREADED_LINEAR / EVALUATOR_OPTIMIZER sites, NOT
        # via the #648 buffered-branch drain (which serializes CONCURRENT sibling
        # writes — structurally absent here). ADR-F2 single-threaded-write holds.
        # (§14.21.7 imprecisely lumps handoff with the 3 concurrent topologies under
        # "buffered-branch drain"; handoff is sequential — a Class-3 spec-imprecision
        # note, spine ledger `.harness/beyond-mvp-capability-boundary-ledger.md`
        # B-INTERSTEP-HANDOFF.)
        # Opt-out (`ctx.inter_step_output_channel is None`) → no-op, byte-identical.
        _record_inter_step_output(ctx, str(step.step_id), output)
        # B-HANDOFF-PAUSE — record the completed stage for the resume cursor (a later
        # stage's `pause` halt captures this prefix into a HandoffResumeState).
        completed_stage_records.append(
            HandoffStageResumeState(
                stage_index=stage_index,
                step_id=str(step.step_id),
                output=output,
            )
        )
        _append_handoff_if_not_terminal(stage_index, this_action_id, stage_ctx)
        prev_action_id = this_action_id

    # Terminal: the last stage completed, no further handoff → SUCCESS.
    return _finish(RunStatus.SUCCESS, fail_class=None, salvage=False)


__all__ = [
    "BufferingLedgerWriter",
    "DriverContext",
    "LedgerWriterLike",
    "LifecycleEventEmitterLike",
    "StepDispatcher",
    "append_branch_override_ledger_entry",
    "append_branch_step_ledger_entry",
    "append_branch_terminal_ledger_entry",
    "bounded_barrier",
    "cascade_cancel_barrier",
    "cascade_policy_run_status",
    "dispatch_branch_step_shielded",
    "drain_branch_buffers",
    "execute_workflow",
    "resume_should_redispatch",
]
