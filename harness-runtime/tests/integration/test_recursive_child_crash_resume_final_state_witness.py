"""[P1-a] discriminator witness — B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT (R-FS-1).

The #746 design finding feared a re-dispatched recursive SUB_AGENT child returns a
SUFFIX-ONLY ``final_state`` on crash-resume (the parent fold then silently corrupts the
aggregate). Its §8 HEAD-verified re-scope
(``.harness/b-fanout-crash-resume-maybe-ran-subagent-design-finding-v1.md``) hypothesises
that ``B-CHILD-CRASH-RESUME-FINAL-STATE-RECONSTRUCT`` (#766/#768) already closes that
result-fidelity gap for a ``SINGLE_THREADED_LINEAR`` child — but advisor flagged that the
#766 CP witnesses use a ``_FakeOutputStore`` that IGNORES ``run_key``, so they cannot
witness the load-bearing INTEGRATION fact: *does a REAL recursive child get an
``engine_output_store`` bound under its REUSED run_id with the committed prefix, presented
to the seeding on resume?* "Closed by construction" is the exact presence-not-correctness
move that got the #746 full build reverted.

This is that missing discriminator — the workspace's own open coverage residual
(``test_lifecycle_sub_agent_dispatch.py`` ``test_child_workflow_runner_opts_into_final_state_reconstruct``:
"the real-runner-THROUGH-execute_workflow integration ... needs a fully-bootstrapped
HarnessContext"). It is NON-VACUOUS because it:

  * routes through the REAL ``compose_child_workflow_runner`` -> ``execute_workflow``
    (NOT a mocked runner / spied ``execute_workflow`` — the #746 vacuity trap);
  * binds the REAL on-disk ``EngineOutputStore`` (keyed by ``run_idempotency_key`` — a
    ``run_key``-RESPECTING store, unlike the #766 fake), so the run_id threading is real:
    a broken thread -> the read finds nothing -> suffix-only;
  * PINS the child run_id (monkeypatching ``uuid.uuid4``) to simulate the E1
    deterministic-run_id the arc will build, so the crash-resume re-dispatch reuses the
    same ``run_idempotency_key`` and reaches the committed prefix.

The ``_record_engine_output`` producer auto-commits each step's output to
``ctx.engine_output_store`` regardless of dispatcher (``workflow_driver.py:694``), so an
echo dispatcher suffices for the [P1-a] RESULT-fidelity question (at-most-once / [P1-b] is
a separate concern). The WITH-store / WITHOUT-store pair is the discriminator: a real-store
recursive child reconstructs the FULL final_state; without it, suffix-only — the difference
IS the integration fact under test.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import harness_runtime.lifecycle.child_workflow_runner as cwr
import pytest
from harness_as.sandbox_tier import SandboxTier
from harness_core import PersonaTier, StepID, WorkloadClass
from harness_cp.cp_shared_types import ModelBinding
from harness_cp.cross_family_fallback_chain import (
    FallbackChain,
    ProviderCandidate,
    ProviderFamily,
)
from harness_cp.engine_class import EngineClass
from harness_cp.gate_level_rule import GateLevel
from harness_cp.pause_resume_protocol import (
    CP_FAIL_RESUME_MATERIAL_DIFF_DETECTED,
    ResumeOutcomeKind,
)
from harness_cp.per_step_override_evaluator import StepEffectiveBinding
from harness_cp.sub_agent_brief import (
    ClearTaskBoundaries,
    OutputSchema,
    OutputSchemaKind,
    SubAgentBrief,
)
from harness_cp.topology_pattern import TopologyPattern
from harness_cp.workflow_driver import (
    _compute_run_idempotency_key,
    _compute_step_idempotency_key,
)
from harness_cp.workflow_driver_types import RunStatus, StepExecutionContext, StepKind, WorkflowStep
from harness_cp.workflow_manifest_entry import WorkflowManifestEntry
from harness_is.state_ledger_entry_schema import Actor, ActorClass
from harness_is.state_ledger_entry_schema import Identifier as _Identifier
from harness_od.audit_ledger_types import SignatureAlgorithm
from harness_runtime.lifecycle.audit_writer import RuntimeAuditLedgerWriter
from harness_runtime.lifecycle.child_workflow_runner import compose_child_workflow_runner
from harness_runtime.lifecycle.engine_output_store import EngineOutputStore, engine_output_dir_for
from harness_runtime.lifecycle.handoff import RuntimeHandoffRegistry
from harness_runtime.lifecycle.state_ledger import LedgerWriter
from harness_runtime.lifecycle.sub_agent_dispatch import (
    RuntimeSubAgentDispatcher,
    SubAgentChildFailedError,
    SubAgentDispatchPayload,
    compose_child_run_id_seed,
)
from harness_runtime.lifecycle.topology_dispatcher import RuntimeTopologyDispatcher

_ACTOR = Actor(actor_class=ActorClass.AGENT, actor_id="test-recursive-child")
_DEFAULT_BINDING = ModelBinding(provider="anthropic", model="claude-haiku-4-5")
_CHAIN = FallbackChain(
    primary=ProviderCandidate(
        provider="anthropic", model="claude-haiku-4-5", family=ProviderFamily.ANTHROPIC
    ),
    same_family=(),
    cross_family=(),
    terminal=None,
)

#: The pinned child run_id (simulates the E1 deterministic-run_id the arc will build) +
#: the child workflow_id. Both phases reuse them so the run_idempotency_key is stable.
_CHILD_RUN = "child-run-pinned-e1"
_CHILD_WF = "child-wf"
_ENTRY_VERSION = 1  # WorkflowManifestEntry default; the driver hashes str(entry_version).


def _run_key(run_id: str = _CHILD_RUN, workflow_id: str = _CHILD_WF) -> str:
    """Mirror ``_compute_run_idempotency_key`` (workflow_driver.py:604) — the driver
    passes ``extras=(str(manifest_entry.entry_version),)`` (line 1970)."""
    h = hashlib.sha256()
    h.update(run_id.encode("utf-8"))
    h.update(b"\x00")
    h.update(workflow_id.encode("utf-8"))
    h.update(b"\x00")
    h.update(str(_ENTRY_VERSION).encode("utf-8"))
    return h.hexdigest()


def _step_key(step_index: int) -> str:
    """Mirror ``_compute_step_idempotency_key`` (workflow_driver.py:626) for the linear
    (``branch_path=None``) path — the per-step key the ledger_reader is queried with."""
    h = hashlib.sha256()
    h.update(_run_key().encode("utf-8"))
    h.update(b"\x00")
    h.update(str(step_index).encode("utf-8"))
    return h.hexdigest()


def _seeded_step_key(run_id: str, step_index: int) -> str:
    """``_step_key`` for a SEED-derived child run_id (the E1-live path) — same derivation as
    ``_step_key`` but the run_key is computed from the seed (not the pinned ``_CHILD_RUN``)."""
    h = hashlib.sha256()
    h.update(_run_key(run_id=run_id).encode("utf-8"))
    h.update(b"\x00")
    h.update(str(step_index).encode("utf-8"))
    return h.hexdigest()


def _manifest(
    engine_class: EngineClass = EngineClass.EVENT_SOURCED_REPLAY,
) -> WorkflowManifestEntry:
    return WorkflowManifestEntry(
        workflow_id=_CHILD_WF,
        workload_class=WorkloadClass.PIPELINE_AUTOMATION,
        persona_tier=PersonaTier.TEAM_BINDING,
        engine_class=engine_class,
        topology_pattern=TopologyPattern.SINGLE_THREADED_LINEAR,
        layer_budgets=(),
        fallback_chain=_CHAIN,
        hitl_placements=(),
        per_step_overrides={},
    )


def _step(idx: int) -> WorkflowStep:
    return WorkflowStep(
        step_id=StepID(f"step-{idx}"),
        step_kind=StepKind.INFERENCE_STEP,
        step_payload={"index": idx},
    )


class _Ledger:
    """In-memory ``LedgerWriterLike``."""

    def __init__(self) -> None:
        self.actor = _ACTOR
        self.appends: list[tuple[Any, Any]] = []

    def append(self, payload: Any, write_key: Any) -> str:
        self.appends.append((payload, write_key))
        return "appended"

    @property
    def is_genesis(self) -> bool:
        return len(self.appends) == 0

    @property
    def entry_count(self) -> int:
        return len(self.appends)


class _Emitter:
    def __init__(self) -> None:
        self.emits: list[Any] = []

    def emit(self, event_class: Any) -> None:
        self.emits.append(event_class)


class _LedgerReader:
    """In-memory ``LedgerReaderLike`` reporting ``resume_at`` via materialized per-step
    keys (the #766 ``_FakeLedgerReader`` shape)."""

    def __init__(self, materialized: dict[str, int]) -> None:
        self._keys = materialized

    def read_by_idempotency_key(self, idempotency_key: Any, bounded_window: Any) -> Any:
        _ = bounded_window
        count = self._keys.get(str(idempotency_key), 0)

        class _Result:
            def __init__(self, n: int) -> None:
                self.entries = tuple(object() for _ in range(n))
                self.truncated = False
                self.next_position = None

        return _Result(count)


class _Echo:
    """Echo dispatcher; the driver's ``_record_engine_output`` does the durable store
    commit, so the dispatcher need not."""

    def __init__(self) -> None:
        self.dispatched: list[tuple[Any, WorkflowStep]] = []

    def dispatch(
        self, binding: Any, step: WorkflowStep, *, step_context: Any = None
    ) -> dict[str, Any]:
        _ = step_context
        self.dispatched.append((binding, step))
        return {"step_id": str(step.step_id), "echoed": dict(step.step_payload)}


class _Registry:
    def __init__(self, dispatcher: _Echo) -> None:
        self._d = dispatcher

    def lookup(self, step_kind: StepKind) -> _Echo:
        _ = step_kind
        return self._d


class _ReconcilerRecoveryLoop:
    """B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT-RECONCILER-CHILD (R-FS-1) — a duck-typed
    ``engine_recovery_loop`` for the RECONCILER child crash-resume witness. The CP RECONCILER
    resume branch (workflow_driver.py U-CP-97) calls ``has_pause_record`` (presence gate) then the
    async ``attempt_resume`` (run via ``asyncio.run``) and reads ``.resume_outcome.outcome_kind``.

    ``outcome_kind`` is configurable: ``RESUME_CLEAN`` models the not-won-claim / clean-reconverge
    cases (child auto-resumes the committed prefix); ``ABORT_REVALIDATION_FAILED`` models the F-1
    window — the child WON the CAS revision claim on its first attempt, then crashed mid-re-execution,
    so the re-dispatch's retry of the already-claimed revision LOSES → ABORT (the substrate F-1
    at-most-once gate, ``reconciler_pause_resume_substrate.py``). The real reconciler substrate's CAS
    semantics are independently tested; this fake exercises the COMPOSITION (re-dispatch → reconverge
    fires → outcome → child RunResult)."""

    def __init__(self, *, outcome_kind: Any, has_record: bool = True) -> None:
        self._outcome_kind = outcome_kind
        self._has_record = has_record
        self.resume_calls: list[dict[str, Any]] = []

    def has_pause_record(self, *, engine_class: Any, workflow_id: str, run_id: str) -> bool:
        _ = (engine_class, workflow_id, run_id)
        return self._has_record

    async def attempt_resume(
        self,
        *,
        engine_class: Any,
        workflow_id: str,
        run_id: str,
        step_id: str,
        resume_event_id: str,
        resume_attempt_count: int,
        resume_at: str,
        resume_request_actor: Any = None,
    ) -> Any:
        _ = (engine_class, resume_event_id, resume_attempt_count, resume_at, resume_request_actor)
        self.resume_calls.append({"workflow_id": workflow_id, "run_id": run_id, "step_id": step_id})
        return SimpleNamespace(resume_outcome=SimpleNamespace(outcome_kind=self._outcome_kind))


class _Ctx:
    """Complete duck-typed mutable ``DriverContext`` (mirrors the #766 ``_FakeCtx`` field
    set) with a REAL ``engine_output_store`` + ``step_dispatchers`` bound — the runner reads
    ``ctx.step_dispatchers`` (child_workflow_runner.py:185) + ``execute_workflow`` reads
    ``ctx.engine_output_store`` for the reconstruct seed. ``engine_recovery_loop`` is optional
    (None for the SAVE_POINT/ESR witnesses, which fire no recovery loop; a ``_ReconcilerRecoveryLoop``
    for the RECONCILER U-CP-97 reconverge witnesses)."""

    def __init__(
        self,
        *,
        ledger: _Ledger,
        reader: _LedgerReader,
        store: EngineOutputStore | None,
        dispatchers: _Registry,
        engine_recovery_loop: Any = None,
    ) -> None:
        from opentelemetry.trace import NoOpTracerProvider

        self.ledger_writer = ledger
        self.lifecycle_emitter = _Emitter()
        self.ledger_reader = reader
        self.engine_output_store = store
        self.step_dispatchers = dispatchers
        self.tracer_provider = NoOpTracerProvider()
        self.drained_flag = asyncio.Event()
        self.pause_requested_flag = asyncio.Event()
        self.pause_resume_protocol = None
        self.validator_framework = None
        self.tenant_id = None
        self.inter_step_output_channel = None
        self.engine_recovery_loop = engine_recovery_loop


def _pin_child_run_id(monkeypatch: Any) -> None:
    """Pin the child run_id so the crash-resume re-dispatch reuses the
    ``run_idempotency_key`` (simulating the E1 deterministic-run_id the arc will build).
    The runner derives ``child_run_id = uuid.uuid4().hex`` on a crash-resume
    (``pause_snapshot_input is None``; child_workflow_runner.py:153-154)."""
    monkeypatch.setattr(cwr.uuid, "uuid4", lambda: SimpleNamespace(hex=_CHILD_RUN))


def _resume_ctx(store: EngineOutputStore | None) -> _Ctx:
    """A child crash-resume ctx: 2 steps materialized (``resume_at == 2``) so the resume
    seeds the committed prefix ``[0, 2)`` and re-dispatches only step-2."""
    reader = _LedgerReader({_step_key(0): 1, _step_key(1): 1})
    return _Ctx(ledger=_Ledger(), reader=reader, store=store, dispatchers=_Registry(_Echo()))


def test_recursive_child_crash_resume_reconstructs_full_final_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[P1-a] DISCRIMINATOR (the re-scope hypothesis CONFIRMED if GREEN) — a REAL recursive
    child routed through ``compose_child_workflow_runner`` -> ``execute_workflow`` over a REAL
    ``run_key``-respecting ``EngineOutputStore`` holding the committed prefix, crash-resumed
    with the child run_id PINNED, reconstructs its COMPLETE ``final_state``
    {step-0, step-1, step-2} — NOT the suffix-only {step-2} the #746 finding feared. The
    prefix is reconstructed from the store (seeded before the loop), only step-2 is
    re-dispatched."""
    store = EngineOutputStore(journal_dir=engine_output_dir_for(tmp_path))
    run_key = _run_key()
    # The committed prefix the pre-crash child durably recorded (the producer keys by
    # run_idempotency_key; a REUSED run_id -> the SAME key -> this prefix is found).
    store.record(run_key, 0, "step-0", {"out": "v0"})
    store.record(run_key, 1, "step-1", {"out": "v1"})

    _pin_child_run_id(monkeypatch)
    ctx = _resume_ctx(store)
    runner = compose_child_workflow_runner(cast(Any, ctx))
    result = runner(
        workflow_id=_CHILD_WF,
        manifest_entry=_manifest(),
        steps=[_step(0), _step(1), _step(2)],
        handoff_context=cast(Any, None),  # not forwarded to execute_workflow
        descent=cast(Any, None),  # not forwarded to execute_workflow
        default_model_binding=_DEFAULT_BINDING,
        pause_snapshot_input=None,  # CRASH-resume (not a pause-resume)
    )

    assert result.status is RunStatus.SUCCESS
    assert result.final_state is not None
    # [P1-a] is CLOSED for the linear child: FULL terminal state, not suffix-only {step-2}.
    assert set(result.final_state.keys()) == {"step-0", "step-1", "step-2"}
    assert result.final_state["step-0"] == {"out": "v0"}
    assert result.final_state["step-1"] == {"out": "v1"}
    # The committed prefix was reconstructed from the store, NOT re-dispatched.
    dispatcher = ctx.step_dispatchers.lookup(StepKind.INFERENCE_STEP)
    assert len(dispatcher.dispatched) == 1
    assert str(dispatcher.dispatched[0][1].step_id) == "step-2"


def test_recursive_child_crash_resume_save_point_reconstructs_full_final_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT-SAVE-POINT-CHILD (R-FS-1) — the EFFECT-LEVEL pivot
    witness (advisor #1). A REAL recursive child with a SAVE_POINT_CHECKPOINT manifest, routed
    through `compose_child_workflow_runner` -> `execute_workflow` over a `run_key`-respecting
    `EngineOutputStore` holding the committed prefix, crash-resumed with the child run_id PINNED
    (the E1 deterministic re-derive), AUTO-RESUMES: `resume_at` is COMPUTED from the ledger reader
    via the engine-class-agnostic F2-prefix join (SAVE_POINT is its reference impl, NOT a fresh run),
    so ONLY step-2 is re-dispatched — the committed prefix [0,2) is NOT re-fired (at-most-once for
    committed effects, count == 1 per step) — and final_state reconstructs the COMPLETE
    {step-0, step-1, step-2}. Distinct from ESR: a SAVE_POINT resume fires NO inter-step
    cached-output rehydrate and NO engine-layer recovery loop / CAS-claim (no F-1 window). RED on
    HEAD before the predicate flip: the parent classifier excluded SAVE_POINT → the maybe-ran branch
    failed closed (the run never reached this re-dispatch); the child-resume mechanics this asserts
    are what make the now-admitted re-dispatch at-most-once-safe."""
    store = EngineOutputStore(journal_dir=engine_output_dir_for(tmp_path))
    run_key = _run_key()
    store.record(run_key, 0, "step-0", {"out": "v0"})
    store.record(run_key, 1, "step-1", {"out": "v1"})

    _pin_child_run_id(monkeypatch)
    ctx = _resume_ctx(store)
    runner = compose_child_workflow_runner(cast(Any, ctx))
    result = runner(
        workflow_id=_CHILD_WF,
        manifest_entry=_manifest(engine_class=EngineClass.SAVE_POINT_CHECKPOINT),
        steps=[_step(0), _step(1), _step(2)],
        handoff_context=cast(Any, None),
        descent=cast(Any, None),
        default_model_binding=_DEFAULT_BINDING,
        pause_snapshot_input=None,  # CRASH-resume (not a pause-resume)
    )

    assert result.status is RunStatus.SUCCESS
    assert result.final_state is not None
    assert set(result.final_state.keys()) == {"step-0", "step-1", "step-2"}
    assert result.final_state["step-0"] == {"out": "v0"}
    assert result.final_state["step-1"] == {"out": "v1"}
    # At-most-once: only step-2 re-dispatched; the committed prefix [0,2) was AUTO-RESUMED
    # (resume_at computed > 0 from the F2-prefix), NOT re-fired (count == 1 per committed step).
    dispatcher = ctx.step_dispatchers.lookup(StepKind.INFERENCE_STEP)
    assert len(dispatcher.dispatched) == 1
    assert str(dispatcher.dispatched[0][1].step_id) == "step-2"


def _reconciler_resume_ctx(
    store: EngineOutputStore | None, *, outcome_kind: ResumeOutcomeKind
) -> _Ctx:
    """A RECONCILER child crash-resume ctx: 2 steps materialized (``resume_at == 2``) + an
    ``engine_recovery_loop`` whose ``attempt_resume`` returns ``outcome_kind`` — so the U-CP-97
    reconverge fires (``resume_at < len(steps) ∧ has_pause_record``)."""
    reader = _LedgerReader({_step_key(0): 1, _step_key(1): 1})
    loop = _ReconcilerRecoveryLoop(outcome_kind=outcome_kind)
    return _Ctx(
        ledger=_Ledger(),
        reader=reader,
        store=store,
        dispatchers=_Registry(_Echo()),
        engine_recovery_loop=loop,
    )


def test_recursive_child_crash_resume_reconciler_clean_cas_auto_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT-RECONCILER-CHILD (R-FS-1) — the NON-VACUOUS recovery
    witness (advisor #1: confirm the re-dispatched child's ctx actually binds + fires
    ``engine_recovery_loop``, which the #782 SAVE_POINT witness could NOT catch — SAVE_POINT fires no
    recovery loop). A REAL recursive RECONCILER child, re-dispatched through
    ``compose_child_workflow_runner`` -> ``execute_workflow`` over a ``run_key``-respecting
    ``EngineOutputStore`` holding the committed prefix, with an ``engine_recovery_loop`` whose
    reconverge returns RESUME_CLEAN (the not-won-claim / clean-reconverge cases), AUTO-RESUMES: the
    U-CP-97 reconverge FIRES (``resume_calls == 1`` — NOT vacuous), the committed prefix [0,2) is
    reconstructed (final_state {step-0,step-1,step-2}), and ONLY step-2 is re-dispatched (the prefix
    is NOT re-fired — at-most-once)."""
    store = EngineOutputStore(journal_dir=engine_output_dir_for(tmp_path))
    run_key = _run_key()
    store.record(run_key, 0, "step-0", {"out": "v0"})
    store.record(run_key, 1, "step-1", {"out": "v1"})

    _pin_child_run_id(monkeypatch)
    ctx = _reconciler_resume_ctx(store, outcome_kind=ResumeOutcomeKind.RESUME_CLEAN)
    runner = compose_child_workflow_runner(cast(Any, ctx))
    result = runner(
        workflow_id=_CHILD_WF,
        manifest_entry=_manifest(engine_class=EngineClass.RECONCILER_LOOP),
        steps=[_step(0), _step(1), _step(2)],
        handoff_context=cast(Any, None),
        descent=cast(Any, None),
        default_model_binding=_DEFAULT_BINDING,
        pause_snapshot_input=None,  # CRASH-resume
    )

    # The U-CP-97 engine-layer reconverge actually FIRED (the binding is real, not vacuous).
    loop = cast(_ReconcilerRecoveryLoop, ctx.engine_recovery_loop)
    assert len(loop.resume_calls) == 1
    assert result.status is RunStatus.SUCCESS
    assert result.final_state is not None
    assert set(result.final_state.keys()) == {"step-0", "step-1", "step-2"}
    assert result.final_state["step-0"] == {"out": "v0"}
    # At-most-once: the committed prefix [0,2) was AUTO-RESUMED, only step-2 re-dispatched.
    dispatcher = ctx.step_dispatchers.lookup(StepKind.INFERENCE_STEP)
    assert len(dispatcher.dispatched) == 1
    assert str(dispatcher.dispatched[0][1].step_id) == "step-2"


def test_recursive_child_crash_resume_reconciler_f1_abort_fails_closed_at_most_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT-RECONCILER-CHILD (R-FS-1) — the LOAD-BEARING F-1
    case-3 witness (advisor #2 assertions (i)+(ii); the SAVE_POINT close did NOT need this). The F-1
    window: a maybe-ran RECONCILER child WON the CAS revision claim on its first attempt, then
    crashed mid-re-execution. On the parent-crash re-dispatch, the child runs its OWN crash-resume,
    which fires the U-CP-97 reconverge (``attempt_resume``) → the retry of the already-claimed
    revision LOSES → ``ABORT_REVALIDATION_FAILED``. Asserts: (i) the committed prefix is NOT re-fired
    AND no suffix runs — the ABORT short-circuits BEFORE the step loop, so ZERO steps are dispatched
    (at-most-once preserved, never a double-fire); (ii) the child returns RunStatus.FAILED with the
    C-CP-22 §22.1 abort fail_class (final_state None — never a silently-truncated SUCCESS). The PARENT
    fold disposition over THIS FAILED child is the separate full-chain witness
    ``test_maybe_ran_reconciler_child_f1_abort_parent_folds_fail_closed``."""
    store = EngineOutputStore(journal_dir=engine_output_dir_for(tmp_path))
    run_key = _run_key()
    store.record(run_key, 0, "step-0", {"out": "v0"})
    store.record(run_key, 1, "step-1", {"out": "v1"})

    _pin_child_run_id(monkeypatch)
    ctx = _reconciler_resume_ctx(store, outcome_kind=ResumeOutcomeKind.ABORT_REVALIDATION_FAILED)
    runner = compose_child_workflow_runner(cast(Any, ctx))
    result = runner(
        workflow_id=_CHILD_WF,
        manifest_entry=_manifest(engine_class=EngineClass.RECONCILER_LOOP),
        steps=[_step(0), _step(1), _step(2)],
        handoff_context=cast(Any, None),
        descent=cast(Any, None),
        default_model_binding=_DEFAULT_BINDING,
        pause_snapshot_input=None,  # CRASH-resume
    )

    # The reconverge fired and ABORTed → the child fails closed BEFORE any step re-executes.
    loop = cast(_ReconcilerRecoveryLoop, ctx.engine_recovery_loop)
    assert len(loop.resume_calls) == 1
    assert result.status is RunStatus.FAILED
    assert result.final_state is None
    assert result.fail_class == CP_FAIL_RESUME_MATERIAL_DIFF_DETECTED
    # (i) at-most-once: the ABORT short-circuits before the step loop → NOTHING re-dispatched
    # (the committed prefix is not re-fired; no suffix fires either).
    dispatcher = ctx.step_dispatchers.lookup(StepKind.INFERENCE_STEP)
    assert dispatcher.dispatched == []


_PARENT_KEY = "parent-idem-key-worker-branch-0"


def test_recursive_child_crash_resume_e1_live_seed_reconstructs_full_final_state(
    tmp_path: Path,
) -> None:
    """E1-LIVE full-chain witness (B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT) — the discriminator
    advisor #3 flagged: the #770 sibling PINS the child run_id (monkeypatching ``uuid.uuid4``) to
    SIMULATE E1. This one uses the REAL ``compose_child_run_id_seed`` (the deterministic E1 seed the
    arc builds) as ``child_run_id_seed`` — NO uuid pin — and proves the seed-derived run_id drives
    the ``run_idempotency_key`` -> committed-prefix -> reconstruction chain through the REAL runner.

    The seed is a PURE function of (parent_idempotency_key, child_workflow_id), so a parent-crash
    re-dispatch RE-DERIVES the SAME id (asserted directly) -> the child auto-resumes from the durable
    store under the stable key -> the resumed ``final_state`` reconstructs the COMPLETE terminal
    state {step-0, step-1, step-2}, only step-2 re-dispatched. This closes the half-mechanism gap:
    #770 proved reconstruction GIVEN a stable id; this proves the E1 seed PRODUCES that stable id
    through the real runner."""
    seed = compose_child_run_id_seed(_PARENT_KEY, _CHILD_WF)
    # The seed is deterministic — a re-dispatch re-derives it (the recovery prerequisite).
    assert compose_child_run_id_seed(_PARENT_KEY, _CHILD_WF) == seed
    # A different worker / child workflow_id -> a distinct seed (per-branch / per-child isolation).
    assert compose_child_run_id_seed("parent-idem-key-worker-branch-1", _CHILD_WF) != seed
    assert compose_child_run_id_seed(_PARENT_KEY, "other-child-wf") != seed

    store = EngineOutputStore(journal_dir=engine_output_dir_for(tmp_path))
    run_key = _run_key(run_id=seed)
    store.record(run_key, 0, "step-0", {"out": "v0"})
    store.record(run_key, 1, "step-1", {"out": "v1"})

    # resume ctx keyed on the SEED-derived run_key (no uuid pin — the seed IS the id).
    reader = _LedgerReader({_seeded_step_key(seed, 0): 1, _seeded_step_key(seed, 1): 1})
    ctx = _Ctx(ledger=_Ledger(), reader=reader, store=store, dispatchers=_Registry(_Echo()))
    runner = compose_child_workflow_runner(cast(Any, ctx))
    result = runner(
        workflow_id=_CHILD_WF,
        manifest_entry=_manifest(),
        steps=[_step(0), _step(1), _step(2)],
        handoff_context=cast(Any, None),
        descent=cast(Any, None),
        default_model_binding=_DEFAULT_BINDING,
        pause_snapshot_input=None,  # CRASH-resume (not a pause-resume)
        child_run_id_seed=seed,  # E1-LIVE: the deterministic seed, NOT a pinned uuid
    )

    assert result.status is RunStatus.SUCCESS
    assert result.final_state is not None
    assert set(result.final_state.keys()) == {"step-0", "step-1", "step-2"}
    assert result.final_state["step-0"] == {"out": "v0"}
    assert result.final_state["step-1"] == {"out": "v1"}
    dispatcher = ctx.step_dispatchers.lookup(StepKind.INFERENCE_STEP)
    assert len(dispatcher.dispatched) == 1
    assert str(dispatcher.dispatched[0][1].step_id) == "step-2"


def test_recursive_child_crash_resume_without_store_degrades_to_suffix_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NEGATIVE CONTROL (RED-without-the-store) — the SAME recursive child crash-resume with
    NO ``engine_output_store`` bound degrades to the suffix-only ``final_state`` {step-2} (the
    pre-#766 behavior the #746 finding feared). Proves the real-store reconstruction through
    the real child runner is the ONLY source of the full final_state — the integration fact
    the #766 ``run_key``-ignoring fake could not witness."""
    _ = tmp_path
    _pin_child_run_id(monkeypatch)
    ctx = _resume_ctx(store=None)  # NO store bound
    runner = compose_child_workflow_runner(cast(Any, ctx))
    result = runner(
        workflow_id=_CHILD_WF,
        manifest_entry=_manifest(),
        steps=[_step(0), _step(1), _step(2)],
        handoff_context=cast(Any, None),
        descent=cast(Any, None),
        default_model_binding=_DEFAULT_BINDING,
        pause_snapshot_input=None,
    )

    assert result.status is RunStatus.SUCCESS
    assert result.final_state is not None
    assert set(result.final_state.keys()) == {"step-2"}


# ---------------------------------------------------------------------------
# B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT-RECONCILER-CHILD (R-FS-1) — the FULL-CHAIN F-1
# disposition witness (advisor #2 assertion (iii)). Drives the REAL parent
# `RuntimeSubAgentDispatcher.dispatch` over a REAL `compose_child_workflow_runner` whose RECONCILER
# child crash-resume ABORTs (the F-1 window) — proving the parent lands FAIL-CLOSED
# (`SubAgentChildFailedError`), NEVER a SUCCESS aggregate assembled from a failed child.
# ---------------------------------------------------------------------------


def _f1_brief() -> SubAgentBrief:
    return SubAgentBrief(
        objective="reconciler child re-dispatch (F-1 window)",
        output_format=OutputSchema(
            schema_kind=OutputSchemaKind.JSON_SCHEMA, schema_body='{"type":"object"}'
        ),
        guidance="re-dispatch fires the U-CP-97 reconverge",
        task_boundaries=ClearTaskBoundaries(
            in_scope=("reconcile",), out_of_scope=("summarize",), termination_criteria=("done",)
        ),
        summary_hash="0" * 64,
    )


def _reconciler_subagent_step() -> WorkflowStep:
    """A SUB_AGENT_DISPATCH step whose child is a RECONCILER_LOOP LINEAR leaf (the now-recoverable
    shape)."""
    payload = SubAgentDispatchPayload(
        child_workflow_id=_CHILD_WF,
        child_manifest_entry=_manifest(engine_class=EngineClass.RECONCILER_LOOP),
        child_steps=(_step(0), _step(1), _step(2)),
        brief=_f1_brief(),
    )
    return WorkflowStep(
        step_id=StepID("step-0"),
        step_kind=StepKind.SUB_AGENT_DISPATCH,
        step_payload=payload.model_dump(),
    )


def _parent_step_context() -> StepExecutionContext:
    return StepExecutionContext(
        workflow_id="parent-wf",
        parent_action_id="workflow:parent-wf:step:0",
        parent_gate_level=GateLevel.AUTO,
        parent_sandbox_tier=SandboxTier.TIER_1_PROCESS,
        parent_actor=_ACTOR,
        parent_entry_hash="",
        parent_idempotency_key="0" * 64,
        tenant_id=None,
        step_index=0,
    )


def _parent_binding() -> StepEffectiveBinding:
    return StepEffectiveBinding(
        step_id="step-0",
        model_binding=_DEFAULT_BINDING,
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        hitl_placement=None,
        override_applied=False,
        override_audit_ref=None,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
    )


def _parent_ledger_writer(tmp_path: Path) -> LedgerWriter:
    from harness_is.jsonl_event_ledger_lifecycle import JsonlLedgerHandle

    path = tmp_path / "parent-state.jsonl"
    path.touch()
    return LedgerWriter(
        handle=JsonlLedgerHandle(canonical_path=path, exists=True, entry_count=0), actor=_ACTOR
    )


def test_maybe_ran_reconciler_child_f1_abort_parent_folds_fail_closed(tmp_path: Path) -> None:
    """B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT-RECONCILER-CHILD (R-FS-1) — the FULL-CHAIN F-1
    disposition witness (advisor #2 assertion (iii), `[[full-chain-witness-not-half-proofs]]` /
    `[[recovery-effect-fidelity-vs-result-fidelity]]`): a maybe-ran RECONCILER leaf child re-dispatch
    in the F-1 won-CAS-claim-then-crashed window flows through the REAL parent
    `RuntimeSubAgentDispatcher.dispatch` -> REAL `compose_child_workflow_runner` -> `execute_workflow`
    (the child's OWN crash-resume) -> the U-CP-97 reconverge ABORTs (`ABORT_REVALIDATION_FAILED`) ->
    child RunStatus.FAILED -> the PARENT FOLD raises `SubAgentChildFailedError` (fail-closed). This
    PROVES — not assumes — that the parent lands at the safe disposition and NEVER assembles a SUCCESS
    aggregate from the failed child (the fold has no RECONCILER-special branch; FAILED always raises).
    The at-most-once (i)/(ii) halves are witnessed at `..._reconciler_f1_abort_fails_closed_at_most_once`;
    this is the load-bearing parent-disposition half SAVE_POINT never needed."""
    from datetime import UTC, datetime

    from opentelemetry.trace import NoOpTracerProvider

    # The child ctx: an ABORTing reconverge (the F-1 window — the first attempt won the claim).
    store = EngineOutputStore(journal_dir=engine_output_dir_for(tmp_path))
    loop = _ReconcilerRecoveryLoop(outcome_kind=ResumeOutcomeKind.ABORT_REVALIDATION_FAILED)
    child_ctx = _Ctx(
        ledger=_Ledger(),
        reader=_LedgerReader({}),  # resume_at 0 (a step-0 engine pause still fires the reconverge)
        store=store,
        dispatchers=_Registry(_Echo()),
        engine_recovery_loop=loop,
    )
    runner = compose_child_workflow_runner(cast(Any, child_ctx))

    ledger_writer = _parent_ledger_writer(tmp_path)
    dispatcher = RuntimeSubAgentDispatcher(
        handoff_registry=RuntimeHandoffRegistry(),
        topology_dispatcher=RuntimeTopologyDispatcher(),
        tracer_provider=NoOpTracerProvider(),
        child_workflow_runner=cast(Any, runner),
        ledger_writer=ledger_writer,
        audit_writer=RuntimeAuditLedgerWriter(
            ledger_writer=ledger_writer, time_source=lambda: datetime.now(UTC)
        ),
        audit_signing_key_id="test-signing-key",
        audit_signing_algorithm=SignatureAlgorithm.ED25519,
        time_source=lambda: datetime.now(UTC),
        procedural_tier_snapshot_resolver=lambda: _Identifier("b" * 64),
    )

    # The parent FAILS CLOSED on the child's F-1 ABORT — NOT a SUCCESS aggregate.
    with pytest.raises(SubAgentChildFailedError):
        dispatcher.dispatch(
            cast(Any, _parent_binding()),
            _reconciler_subagent_step(),
            step_context=_parent_step_context(),
        )
    # The child REALLY ran its crash-resume (the reconverge fired) and re-dispatched NOTHING
    # (the ABORT short-circuited before the step loop — at-most-once through the full chain).
    assert len(loop.resume_calls) == 1
    assert child_ctx.step_dispatchers.lookup(StepKind.INFERENCE_STEP).dispatched == []


# ---------------------------------------------------------------------------
# B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT-NONLEAF-CHILD (R-FS-1) — the load-bearing
# DEPTH-2 recursive-composition witness (advisor's sharpened target). The recursive
# `subagent_child_recoverable` now ADMITS a LINEAR child whose own child step is a
# recoverable SUB_AGENT (the grandchild). This proves the COMPANION seed-gating extension
# (`is_linear_sequential_dispatch`) is load-bearing: when the maybe-ran parent re-dispatches
# the LINEAR child, the child auto-resumes and re-dispatches its grandchild — and the
# grandchild AUTO-RESUMES from its OWN durable store under its RECURSIVELY-DERIVED run_id
# (the seed composed at the child's LINEAR-loop dispatch of the grandchild) rather than
# re-firing its committed effects. The AT-MOST-ONCE assertion (only the in-flight grandchild
# step re-dispatched) is the load-bearing one — WITHOUT the seed the grandchild re-fires ALL
# its steps (double-fire); WITH it, only the uncommitted tail. Routed end-to-end through the
# REAL `compose_child_workflow_runner` -> `execute_workflow` at BOTH levels + the REAL
# `RuntimeSubAgentDispatcher` at the child->grandchild seam (NOT mocked).
# ---------------------------------------------------------------------------

_GRANDCHILD_WF = "grandchild-wf"


class _KindRegistry:
    """A step-dispatcher registry mapping each `StepKind` to a DISTINCT dispatcher — the
    depth-2 witness binds SUB_AGENT_DISPATCH to a REAL `RuntimeSubAgentDispatcher` (recursing
    into the grandchild) and INFERENCE_STEP to a counting `_Echo` whose `dispatched` length is
    the child's own at-most-once witness."""

    def __init__(self, mapping: dict[StepKind, Any]) -> None:
        self._m = mapping

    def lookup(self, step_kind: StepKind) -> Any:
        return self._m[step_kind]


def _grandchild_payload() -> SubAgentDispatchPayload:
    """A SUB_AGENT_DISPATCH payload whose child (the GRANDCHILD) is an {ESR} ∧ LINEAR leaf with
    3 INFERENCE steps — RECOVERABLE per the recursive predicate, so the child's LINEAR-loop
    dispatch of it gets the deterministic seed."""
    return SubAgentDispatchPayload(
        child_workflow_id=_GRANDCHILD_WF,
        child_manifest_entry=WorkflowManifestEntry(
            workflow_id=_GRANDCHILD_WF,
            workload_class=WorkloadClass.PIPELINE_AUTOMATION,
            persona_tier=PersonaTier.TEAM_BINDING,
            engine_class=EngineClass.EVENT_SOURCED_REPLAY,
            topology_pattern=TopologyPattern.SINGLE_THREADED_LINEAR,
            layer_budgets=(),
            fallback_chain=_CHAIN,
            hitl_placements=(),
            per_step_overrides={},
        ),
        child_steps=(_g_step(0), _g_step(1), _g_step(2)),
        brief=_f1_brief(),
    )


def _g_step(idx: int) -> WorkflowStep:
    """A grandchild INFERENCE step (distinct step_ids from the child's `_step`)."""
    return WorkflowStep(
        step_id=StepID(f"g-step-{idx}"),
        step_kind=StepKind.INFERENCE_STEP,
        step_payload={"g_index": idx},
    )


def _grandchild_run_key() -> str:
    """The grandchild's run_idempotency_key — keyed by the RECURSIVELY-DERIVED run_id (the seed
    the child's LINEAR-loop dispatch of the grandchild composes). Mirrors the production chain
    using the REAL imported key fns (no mirror drift):
      child_run_key   = run_key(_CHILD_RUN, _CHILD_WF)            [the worker's pinned child run_id]
      child_step1_key = step_key(child_run_key, 1)               [the grandchild's dispatch step]
      disambig        = "linear-step:" + json.dumps(["step-1", <gc engine>, <gc topology>])
      gc_seed         = compose_child_run_id_seed(child_step1_key, _GRANDCHILD_WF, disambig)
      gc_run_key      = run_key(gc_seed, _GRANDCHILD_WF)

    The `branch_path` is the SAME-STEP + SAME-ENGINE + SAME-TOPOLOGY IDENTITY binding (Codex [P1]):
    the grandchild's dispatch step is `step-1`, its engine is EVENT_SOURCED_REPLAY and its topology
    SINGLE_THREADED_LINEAR (`_grandchild_payload`), so the seed folds ALL THREE in (a rename / engine
    swap / topology swap → a different seed → no wrong-store / wrong-substrate auto-resume).
    """
    child_run_key = _compute_run_idempotency_key(
        _CHILD_RUN, _CHILD_WF, extras=(str(_ENTRY_VERSION),)
    )
    child_step1_key = _compute_step_idempotency_key(child_run_key, 1)
    disambig = "linear-step:" + json.dumps(
        [
            "step-1",
            EngineClass.EVENT_SOURCED_REPLAY.value,
            TopologyPattern.SINGLE_THREADED_LINEAR.value,
        ]
    )
    gc_seed = compose_child_run_id_seed(child_step1_key, _GRANDCHILD_WF, branch_path=disambig)
    return _compute_run_idempotency_key(gc_seed, _GRANDCHILD_WF, extras=(str(_ENTRY_VERSION),))


def test_maybe_ran_nonleaf_child_grandchild_auto_resumes_at_most_once(tmp_path: Path) -> None:
    """B-FANOUT-CRASH-RESUME-MAYBE-RAN-SUBAGENT-NONLEAF-CHILD (R-FS-1) — the load-bearing depth-2
    witness. A maybe-ran recoverable LINEAR child (re-dispatched by simulating the worker's seed)
    crash-resumes at step-1 and re-dispatches its nested SUB_AGENT grandchild THROUGH the real
    `RuntimeSubAgentDispatcher`. The dispatcher composes the deterministic grandchild seed (because
    the child's LINEAR-loop dispatch sets `is_linear_sequential_dispatch=True` AND the grandchild is
    recoverable) → the grandchild's run_id re-derives → the grandchild AUTO-RESUMES its committed
    prefix [0,2) from its OWN store and re-dispatches ONLY g-step-2.

    AT-MOST-ONCE (load-bearing): the grandchild's INFERENCE dispatcher fired EXACTLY ONCE (only the
    uncommitted tail g-step-2) — the committed g-step-0/1 were reconstructed, NOT re-fired. WITHOUT
    the `is_linear_sequential_dispatch` seed the grandchild would get a fresh uuid → its store lookup
    misses → it re-runs ALL THREE steps (double-fire). Result fidelity (secondary): the grandchild's
    full final_state {g-step-0,1,2} reconstructs and folds into the child's step-1 output."""
    # --- grandchild durable store + reader (committed prefix [0,2) under the recursively-derived
    #     run_key) ---
    gc_run_key = _grandchild_run_key()
    gc_store = EngineOutputStore(journal_dir=engine_output_dir_for(tmp_path / "gc"))
    gc_store.record(gc_run_key, 0, "g-step-0", {"gout": "gv0"})
    gc_store.record(gc_run_key, 1, "g-step-1", {"gout": "gv1"})
    gc_reader = _LedgerReader(
        {
            _compute_step_idempotency_key(gc_run_key, 0): 1,
            _compute_step_idempotency_key(gc_run_key, 1): 1,
        }
    )
    gc_echo = _Echo()
    gc_ctx = _Ctx(
        ledger=_Ledger(), reader=gc_reader, store=gc_store, dispatchers=_Registry(gc_echo)
    )
    gc_runner = compose_child_workflow_runner(cast(Any, gc_ctx))

    # --- the real child->grandchild dispatcher seam ---
    gc_ledger_writer = _parent_ledger_writer(tmp_path)
    from datetime import UTC, datetime

    grandchild_dispatcher = RuntimeSubAgentDispatcher(
        handoff_registry=RuntimeHandoffRegistry(),
        topology_dispatcher=RuntimeTopologyDispatcher(),
        tracer_provider=cast(Any, gc_ctx.tracer_provider),
        child_workflow_runner=cast(Any, gc_runner),
        ledger_writer=gc_ledger_writer,
        audit_writer=RuntimeAuditLedgerWriter(
            ledger_writer=gc_ledger_writer, time_source=lambda: datetime.now(UTC)
        ),
        audit_signing_key_id="test-signing-key",
        audit_signing_algorithm=SignatureAlgorithm.ED25519,
        time_source=lambda: datetime.now(UTC),
        procedural_tier_snapshot_resolver=lambda: _Identifier("c" * 64),
    )

    # --- child durable store + reader (committed prefix [0,1) → child resumes at step-1, the
    #     grandchild dispatch) ---
    child_store = EngineOutputStore(journal_dir=engine_output_dir_for(tmp_path / "child"))
    child_store.record(_run_key(), 0, "step-0", {"out": "cv0"})
    child_reader = _LedgerReader({_step_key(0): 1})  # only step-0 committed → resume_at == 1
    child_echo = _Echo()
    child_ctx = _Ctx(
        ledger=_Ledger(),
        reader=child_reader,
        store=child_store,
        dispatchers=cast(
            Any,
            _KindRegistry(
                {
                    StepKind.INFERENCE_STEP: child_echo,
                    StepKind.SUB_AGENT_DISPATCH: grandchild_dispatcher,
                }
            ),
        ),
    )
    child_runner = compose_child_workflow_runner(cast(Any, child_ctx))

    grandchild_step = WorkflowStep(
        step_id=StepID("step-1"),
        step_kind=StepKind.SUB_AGENT_DISPATCH,
        step_payload=_grandchild_payload().model_dump(),
    )
    # Re-dispatch the maybe-ran child under the worker's deterministic seed (== _CHILD_RUN), the
    # simulation the depth-1 witnesses pin via uuid — here passed directly so the grandchild's
    # OWN run_id comes from the real seed composition, not the monkeypatch.
    result = child_runner(
        workflow_id=_CHILD_WF,
        manifest_entry=_manifest(engine_class=EngineClass.EVENT_SOURCED_REPLAY),
        steps=[_step(0), grandchild_step],
        handoff_context=cast(Any, None),
        descent=cast(Any, None),
        default_model_binding=_DEFAULT_BINDING,
        pause_snapshot_input=None,  # CRASH-resume
        child_run_id_seed=_CHILD_RUN,
    )

    assert result.status is RunStatus.SUCCESS
    # AT-MOST-ONCE (load-bearing): the grandchild re-dispatched ONLY its uncommitted tail g-step-2.
    # Its committed prefix g-step-0/1 was AUTO-RESUMED from the store (recursively-derived run_id),
    # NOT re-fired. Without the `is_linear_sequential_dispatch` seed this list would be all three.
    assert [str(b[1].step_id) for b in gc_echo.dispatched] == ["g-step-2"]
    # The child itself re-dispatched only the maybe-ran grandchild step (step-0 reconstructed).
    assert [str(b[1].step_id) for b in child_echo.dispatched] == []
    # Result fidelity (secondary): the grandchild's FULL final_state folds into the child's step-1.
    assert result.final_state is not None
    grandchild_final = result.final_state["step-1"]
    assert set(grandchild_final.keys()) == {"g-step-0", "g-step-1", "g-step-2"}
