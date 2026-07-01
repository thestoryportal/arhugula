"""U-RT-111 v2.38 — AC #3 + AC #10 firing-site integration tests.

Per runtime plan v2.38 §1.2 ACs #3 + #10 (v2.37 sequel-rescope post AC #2
STRIKE at fork doc §11 NEW):

- AC #3: `workflow_driver.execute_workflow` invokes
  `ctx.cp_is_wiring.emit_pause_resume_state_ledger_entry(...)` at the 3
  pause-resume firing sites (`:559` RESUME_ATTEMPTED + `:808`
  PAUSE_CAPTURED drain-flag + `:965` PAUSE_CAPTURED HITL-signal).
  Defensive `getattr(ctx, "cp_is_wiring", None)` access pattern; when
  None, silent-skip (operator-opt-in).
- AC #10: 1-site full chain e2e — pause + resume cycle through
  `execute_workflow` produces state-ledger entries with
  `action_id="cp.pause-resume-protocol"`; `verify_chain` passes.

Per-composer kwarg derivation per plan v2.38 §1.2 AC #3 body:
- `event_sequence_id = (step_index << 2) | event_kind_index` where
  event_kind_index ∈ {0: RESUME_ATTEMPTED, 1: PAUSE_CAPTURED_DRAIN,
  2: PAUSE_CAPTURED_HITL}.
- `protocol_state_snapshot = <snapshot>.model_dump(mode="json")` per
  spec §16.5.5 semantic anchor ("protocol state snapshot after the
  class-level event").

Scaffolding mirrors `tests/integration/test_u_rt_89_pause_resume_full_execution_path.py`
(execute_workflow against real bootstrap via patched_runtime + asyncio.to_thread
for sync-bridging). HITL-signal site exercised via a dispatcher that raises
`HITLPauseRequestedSignal`.
"""

from __future__ import annotations

import asyncio
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from harness_core.identity import StepID
from harness_core.persona_tier import PersonaTier
from harness_cp.cp_shared_types import ModelBinding
from harness_cp.cross_family_fallback_chain import (
    FallbackChain,
    ProviderCandidate,
    ProviderFamily,
)
from harness_cp.engine_class import EngineClass
from harness_cp.hitl_response_palette import HITLResponse
from harness_cp.topology_pattern import TopologyPattern
from harness_cp.validator_framework_types import HITLEscalationBrief
from harness_cp.workflow_driver import execute_workflow
from harness_cp.workflow_driver_types import (
    RunStatus,
    StepKind,
    WorkflowStep,
)
from harness_cp.workflow_manifest_entry import StepOverride, WorkflowManifestEntry
from harness_is.chain_verification import VerificationStatus, verify_chain
from harness_is.state_ledger_write import read_ledger
from harness_runtime.bootstrap import run_bootstrap
from harness_runtime.lifecycle.hitl_gate_composer import HITLPauseRequestedSignal
from harness_runtime.lifecycle.pause_resume_protocol_types import (
    PauseResumeProtocolConfig,
)
from harness_runtime.lifecycle.webhook_delivery_composer import WebhookDeliveryResult
from harness_runtime.types import RuntimeConfig

from .conftest import WORKLOAD, build_config

_PAUSE_RESUME_ACTION_ID = "cp.pause-resume-protocol"
_OVERRIDE_ACTION_ID = "cp.per-step-override-application"


def _config_with_pause_resume_opt_in(tmp_path: Path) -> RuntimeConfig:
    base = build_config(tmp_path)
    return base.model_copy(
        update={
            "pause_resume_protocol_config": PauseResumeProtocolConfig.default(),
        },
    )


_CHAIN = FallbackChain(
    primary=ProviderCandidate(
        provider="anthropic",
        model="claude-haiku-4-5",
        family=ProviderFamily.ANTHROPIC,
    ),
    same_family=(),
    cross_family=(),
    terminal=None,
)


_DEFAULT_BINDING = ModelBinding(provider="anthropic", model="claude-haiku-4-5")


def _attach_get_tracer_to_ctx(ctx: Any) -> None:
    """Mirror test_u_rt_89_pause_resume_full_execution_path:
    FakeTracerProvider lacks get_tracer; attach NoOpTracer.
    """
    from opentelemetry.trace import NoOpTracer

    ctx.tracer_provider.get_tracer = lambda _name, /: NoOpTracer()  # type: ignore[attr-defined,method-assign]


def _minimal_manifest(workflow_id: str) -> WorkflowManifestEntry:
    return WorkflowManifestEntry(
        workflow_id=workflow_id,
        workload_class=WORKLOAD,
        persona_tier=PersonaTier.TEAM_BINDING,
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        topology_pattern=TopologyPattern.SINGLE_THREADED_LINEAR,
        layer_budgets=(),
        fallback_chain=_CHAIN,
        hitl_placements=(),
        per_step_overrides={},
    )


def _single_inference_step() -> tuple[WorkflowStep, ...]:
    return (
        WorkflowStep(
            step_id=StepID("step-0"),
            step_kind=StepKind.INFERENCE_STEP,
            step_payload={"index": 0},
        ),
    )


class _NoopDispatcher:
    def dispatch(
        self,
        binding: Any,
        step: WorkflowStep,
        *,
        step_context: Any = None,
    ) -> dict[str, Any]:
        _ = binding, step_context
        return {"step_id": str(step.step_id), "ok": True}


class _SingleKindRegistry:
    def __init__(self, dispatcher: Any) -> None:
        self._dispatcher = dispatcher

    def lookup(self, step_kind: Any) -> Any:
        _ = step_kind
        return self._dispatcher


class _HitlPauseSignalDispatcher:
    def __init__(self, pause_requested_flag: Any) -> None:
        self._pause_requested_flag = pause_requested_flag

    def dispatch(
        self,
        binding: Any,
        step: WorkflowStep,
        *,
        step_context: Any = None,
    ) -> None:
        _ = binding, step_context
        self._pause_requested_flag.set()
        raise HITLPauseRequestedSignal(
            brief=HITLEscalationBrief(
                parent_step_id=str(step.step_id),
                parent_action_id="workflow:wf-hitl-signal-site:step:0",
                fail_class=None,
                fail_detail_hash=None,
                escalation_reason="durable_async_cell_synchrony",
                proposed_response_palette=frozenset({HITLResponse.APPROVE}),
            ),
            delivery_result=WebhookDeliveryResult(
                delivered=True,
                status_code=200,
                response_idempotency_key="hitl:workflow:wf-hitl-signal-site:step:0:pre_action",
                delivery_attempts=1,
                final_attempt_at=1_700_000_000_000,
            ),
        )


def _read_pause_resume_entries(ctx: Any) -> list[Any]:
    """Return state-ledger entries with action_id == cp.pause-resume-protocol."""
    entries = read_ledger(ctx.ledger_writer.handle)  # type: ignore[arg-type]
    return [e for e in entries if str(e.action_id) == _PAUSE_RESUME_ACTION_ID]


# ---------------------------------------------------------------------------
# AC #3 unit tests — one per firing site.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_caller_site_pause_resume_protocol_emission_resume_attempted(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """Site `:559` post-attempt_resume — RESUME_ATTEMPTED ledger entry persisted."""
    _ = patched_runtime
    config = _config_with_pause_resume_opt_in(tmp_path)
    ctx = await run_bootstrap(config, workload_class=WORKLOAD)
    _attach_get_tracer_to_ctx(ctx)
    assert ctx.cp_is_wiring is not None

    manifest = _minimal_manifest("wf-resume-attempted-site")
    steps = _single_inference_step()
    dispatchers = _SingleKindRegistry(_NoopDispatcher())

    # Phase 1 — pause to obtain a snapshot.
    ctx.pause_requested_flag.set()
    paused = await asyncio.to_thread(
        partial(
            execute_workflow,
            manifest_entry=manifest,
            steps=steps,
            run_id="run-phase1",
            ctx=ctx,  # type: ignore[arg-type]
            default_model_binding=_DEFAULT_BINDING,
            step_dispatchers=dispatchers,  # type: ignore[arg-type]
        )
    )
    assert paused.status == RunStatus.PAUSED
    assert paused.pause_snapshot is not None

    # Phase 2 — clear flag + resume via pause_snapshot_input. Site :559 fires.
    ctx.pause_requested_flag.clear()
    resumed = await asyncio.to_thread(
        partial(
            execute_workflow,
            manifest_entry=manifest,
            steps=steps,
            run_id="run-phase2",
            ctx=ctx,  # type: ignore[arg-type]
            default_model_binding=_DEFAULT_BINDING,
            step_dispatchers=dispatchers,  # type: ignore[arg-type]
            pause_snapshot_input=paused.pause_snapshot,
        )
    )
    assert resumed.status == RunStatus.SUCCESS

    entries = _read_pause_resume_entries(ctx)
    # Phase 1 PAUSE_CAPTURED entry + Phase 2 RESUME_ATTEMPTED entry.
    assert len(entries) >= 2
    # At least one entry exists with the RESUME_ATTEMPTED-shaped
    # event_sequence_id ((step_index << 2) | 0 == step_index * 4). For
    # step_index=0 this yields 0; the idempotency_key encodes this in its
    # hash input. The simplest robust assertion is action_id presence +
    # entry count; the unit-test surface at
    # test_pause_resume_workflow_layer_state_ledger_emission.py covers
    # per-kwarg formula assertions directly against the composer.
    action_ids = [str(e.action_id) for e in entries]
    assert all(aid == _PAUSE_RESUME_ACTION_ID for aid in action_ids)


@pytest.mark.asyncio
async def test_caller_site_pause_resume_protocol_emission_pause_captured_drain_flag(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """Site `:769` PAUSE_CAPTURED drain-flag path — entry persisted via wiring."""
    _ = patched_runtime
    config = _config_with_pause_resume_opt_in(tmp_path)
    ctx = await run_bootstrap(config, workload_class=WORKLOAD)
    _attach_get_tracer_to_ctx(ctx)
    assert ctx.cp_is_wiring is not None

    # Pre-set pause_requested_flag → per-step pre-entry pause check at :766
    # fires capture_pause_snapshot + the new emission at :769.
    ctx.pause_requested_flag.set()

    manifest = _minimal_manifest("wf-pause-captured-drain-site")
    steps = _single_inference_step()
    dispatchers = _SingleKindRegistry(_NoopDispatcher())

    result = await asyncio.to_thread(
        partial(
            execute_workflow,
            manifest_entry=manifest,
            steps=steps,
            run_id="run-drain-pause",
            ctx=ctx,  # type: ignore[arg-type]
            default_model_binding=_DEFAULT_BINDING,
            step_dispatchers=dispatchers,  # type: ignore[arg-type]
        )
    )
    assert result.status == RunStatus.PAUSED
    entries = _read_pause_resume_entries(ctx)
    assert len(entries) == 1
    assert str(entries[0].action_id) == _PAUSE_RESUME_ACTION_ID


@pytest.mark.asyncio
async def test_caller_site_pause_resume_protocol_emission_pause_captured_hitl_signal(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """Site `:965` PAUSE_CAPTURED HITL-signal path — entry persisted via wiring."""
    _ = patched_runtime
    config = _config_with_pause_resume_opt_in(tmp_path)
    ctx = await run_bootstrap(config, workload_class=WORKLOAD)
    _attach_get_tracer_to_ctx(ctx)
    assert ctx.cp_is_wiring is not None

    manifest = _minimal_manifest("wf-hitl-signal-site")
    steps = _single_inference_step()
    dispatchers = _SingleKindRegistry(_HitlPauseSignalDispatcher(ctx.pause_requested_flag))

    result = await asyncio.to_thread(
        partial(
            execute_workflow,
            manifest_entry=manifest,
            steps=steps,
            run_id="run-hitl-signal-pause",
            ctx=ctx,  # type: ignore[arg-type]
            default_model_binding=_DEFAULT_BINDING,
            step_dispatchers=dispatchers,  # type: ignore[arg-type]
        )
    )
    assert result.status == RunStatus.PAUSED
    assert result.pause_snapshot is not None
    assert result.pause_snapshot.pause_reason.value == "hitl_pending"

    entries = _read_pause_resume_entries(ctx)
    assert len(entries) == 1
    assert str(entries[0].action_id) == _PAUSE_RESUME_ACTION_ID


# ---------------------------------------------------------------------------
# AC #10 e2e — chain verification PASS across pause + resume cycle.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_caller_site_full_chain_verification_passes_e2e(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """AC #10 — pause + resume cycle persists ledger entries; verify_chain PASS.

    v2.38 reframed from v2.37's 2-site test name; the single retained
    caller-site invocation surface (pause-resume workflow-layer) exercises
    BOTH event kinds (PAUSE_CAPTURED + RESUME_ATTEMPTED) within a single
    workflow lifecycle.
    """
    _ = patched_runtime
    config = _config_with_pause_resume_opt_in(tmp_path)
    ctx = await run_bootstrap(config, workload_class=WORKLOAD)
    _attach_get_tracer_to_ctx(ctx)

    manifest = _minimal_manifest("wf-e2e-full-chain")
    steps = _single_inference_step()
    dispatchers = _SingleKindRegistry(_NoopDispatcher())

    # Phase 1 — pause.
    ctx.pause_requested_flag.set()
    paused = await asyncio.to_thread(
        partial(
            execute_workflow,
            manifest_entry=manifest,
            steps=steps,
            run_id="run-e2e-phase1",
            ctx=ctx,  # type: ignore[arg-type]
            default_model_binding=_DEFAULT_BINDING,
            step_dispatchers=dispatchers,  # type: ignore[arg-type]
        )
    )
    assert paused.status == RunStatus.PAUSED
    assert paused.pause_snapshot is not None

    # Phase 2 — resume.
    ctx.pause_requested_flag.clear()
    resumed = await asyncio.to_thread(
        partial(
            execute_workflow,
            manifest_entry=manifest,
            steps=steps,
            run_id="run-e2e-phase2",
            ctx=ctx,  # type: ignore[arg-type]
            default_model_binding=_DEFAULT_BINDING,
            step_dispatchers=dispatchers,  # type: ignore[arg-type]
            pause_snapshot_input=paused.pause_snapshot,
        )
    )
    assert resumed.status == RunStatus.SUCCESS

    # Verify ledger contains both pause-resume entries.
    pr_entries = _read_pause_resume_entries(ctx)
    assert len(pr_entries) == 2

    # Verify full chain (every entry, not just pause-resume rows).
    all_entries = read_ledger(ctx.ledger_writer.handle)  # type: ignore[arg-type]
    chain_result = verify_chain(all_entries)
    assert chain_result.status == VerificationStatus.VALID, (
        f"chain verification failed at position "
        f"{chain_result.failure_position}: "
        f"{chain_result.failure_type.value if chain_result.failure_type else 'unknown'}"
    )


# ---------------------------------------------------------------------------
# Negative path — no cp_is_wiring binding → silent-skip at all 3 sites.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_pause_resume_protocol_binding_does_not_emit_state_ledger_entry(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """Operator opt-out: cp_is_wiring=None → silent-skip at all 3 sites.

    Bootstraps without setting cp_is_wiring (the v2.36 Phase 1 plumbing
    defaults the field to None when stage 6 does not bind). Then forces
    None on the bound ctx to model the operator opt-out + exercises the
    drain-flag path. Asserts ZERO pause-resume entries persist.
    """
    _ = patched_runtime
    config = _config_with_pause_resume_opt_in(tmp_path)
    ctx = await run_bootstrap(config, workload_class=WORKLOAD)
    _attach_get_tracer_to_ctx(ctx)

    # Force the operator opt-out shape on the bound ctx — emulates a
    # deployment that has not opted in to CP→IS state-ledger emission.
    # HarnessContext is a frozen Pydantic model per C-RT-04; mutate via
    # object.__setattr__ to bypass the immutability check for this test
    # scaffolding (matches the FakeTracerProvider get_tracer attachment
    # pattern at _attach_get_tracer_to_ctx).
    object.__setattr__(ctx, "cp_is_wiring", None)

    # Drain-flag path: pre-set the flag and execute → site :769 reached.
    ctx.pause_requested_flag.set()

    manifest = _minimal_manifest("wf-opt-out")
    steps = _single_inference_step()
    dispatchers = _SingleKindRegistry(_NoopDispatcher())

    result = await asyncio.to_thread(
        partial(
            execute_workflow,
            manifest_entry=manifest,
            steps=steps,
            run_id="run-opt-out",
            ctx=ctx,  # type: ignore[arg-type]
            default_model_binding=_DEFAULT_BINDING,
            step_dispatchers=dispatchers,  # type: ignore[arg-type]
        )
    )
    assert result.status == RunStatus.PAUSED

    entries = _read_pause_resume_entries(ctx)
    assert entries == []


# ---------------------------------------------------------------------------
# U-CP-74 override-emission caller-site integration.
#
# Mirrors the pause-resume scaffolding. Firing site is at workflow_driver
# immediately after `resolve_step_binding(...)` returns: guard on
# `binding.override_applied`, then invoke
# `ctx.cp_is_wiring.emit_override_state_ledger_entry(...)` with
# `post_override_step_config = binding.model_dump(mode="json")` per
# CP spec v1.27 §16.5.5 outcome-bytes semantic.
#
# `emit_override_audit_entry` (audit-half, sibling at line :187 of
# `per_step_override_evaluator.py`) is the Q2=iii deferred stub per PR
# #66 — orthogonal to the state-ledger sibling exercised here.
# ---------------------------------------------------------------------------


def _read_override_entries(ctx: Any) -> list[Any]:
    """State-ledger entries with action_id == cp.per-step-override-application."""
    entries = read_ledger(ctx.ledger_writer.handle)  # type: ignore[arg-type]
    return [e for e in entries if str(e.action_id) == _OVERRIDE_ACTION_ID]


def _manifest_with_step_override(
    workflow_id: str, step_id: str, override: StepOverride
) -> WorkflowManifestEntry:
    return WorkflowManifestEntry(
        workflow_id=workflow_id,
        workload_class=WORKLOAD,
        persona_tier=PersonaTier.TEAM_BINDING,
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        topology_pattern=TopologyPattern.SINGLE_THREADED_LINEAR,
        layer_budgets=(),
        fallback_chain=_CHAIN,
        hitl_placements=(),
        per_step_overrides={step_id: override},
    )


@pytest.mark.asyncio
async def test_caller_site_override_emission_when_override_applied(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """Override-applied path: state-ledger entry persisted via cp_is_wiring."""
    _ = patched_runtime
    config = _config_with_pause_resume_opt_in(tmp_path)
    ctx = await run_bootstrap(config, workload_class=WORKLOAD)
    _attach_get_tracer_to_ctx(ctx)
    assert ctx.cp_is_wiring is not None

    override_binding = ModelBinding(provider="anthropic", model="claude-opus-4-7")
    override = StepOverride(
        step_id=StepID("step-0"),
        model_binding=override_binding,
    )
    manifest = _manifest_with_step_override("wf-override-applied", "step-0", override)
    steps = _single_inference_step()
    dispatchers = _SingleKindRegistry(_NoopDispatcher())

    result = await asyncio.to_thread(
        partial(
            execute_workflow,
            manifest_entry=manifest,
            steps=steps,
            run_id="run-override",
            ctx=ctx,  # type: ignore[arg-type]
            default_model_binding=_DEFAULT_BINDING,
            step_dispatchers=dispatchers,  # type: ignore[arg-type]
        )
    )
    assert result.status == RunStatus.SUCCESS

    entries = _read_override_entries(ctx)
    assert len(entries) == 1
    assert str(entries[0].action_id) == _OVERRIDE_ACTION_ID


@pytest.mark.asyncio
async def test_caller_site_override_emission_skipped_when_no_override(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """Absent-override path: ZERO override-emission entries persist.

    Per CP spec v1.27 §16.5.6 dual-emission discipline — the emission is
    gated on `binding.override_applied=True`. A manifest with empty
    `per_step_overrides` produces `override_applied=False` at
    `resolve_step_binding` and the firing block silent-skips.
    """
    _ = patched_runtime
    config = _config_with_pause_resume_opt_in(tmp_path)
    ctx = await run_bootstrap(config, workload_class=WORKLOAD)
    _attach_get_tracer_to_ctx(ctx)
    assert ctx.cp_is_wiring is not None

    manifest = _minimal_manifest("wf-no-override")  # per_step_overrides={}
    steps = _single_inference_step()
    dispatchers = _SingleKindRegistry(_NoopDispatcher())

    result = await asyncio.to_thread(
        partial(
            execute_workflow,
            manifest_entry=manifest,
            steps=steps,
            run_id="run-no-override",
            ctx=ctx,  # type: ignore[arg-type]
            default_model_binding=_DEFAULT_BINDING,
            step_dispatchers=dispatchers,  # type: ignore[arg-type]
        )
    )
    assert result.status == RunStatus.SUCCESS

    entries = _read_override_entries(ctx)
    assert entries == []


@pytest.mark.asyncio
async def test_caller_site_override_full_chain_verification_passes_e2e(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """Override emission + full chain verification PASS.

    Exercises the U-CP-74 emission at the workflow_driver firing site and
    verifies the full state-ledger hash chain through `verify_chain`. This
    is the override sibling of the pause-resume AC #10 e2e test above.
    """
    _ = patched_runtime
    config = _config_with_pause_resume_opt_in(tmp_path)
    ctx = await run_bootstrap(config, workload_class=WORKLOAD)
    _attach_get_tracer_to_ctx(ctx)

    override = StepOverride(
        step_id=StepID("step-0"),
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
    )
    manifest = _manifest_with_step_override("wf-override-e2e-full-chain", "step-0", override)
    steps = _single_inference_step()
    dispatchers = _SingleKindRegistry(_NoopDispatcher())

    result = await asyncio.to_thread(
        partial(
            execute_workflow,
            manifest_entry=manifest,
            steps=steps,
            run_id="run-override-e2e",
            ctx=ctx,  # type: ignore[arg-type]
            default_model_binding=_DEFAULT_BINDING,
            step_dispatchers=dispatchers,  # type: ignore[arg-type]
        )
    )
    assert result.status == RunStatus.SUCCESS

    override_entries = _read_override_entries(ctx)
    assert len(override_entries) == 1

    all_entries = read_ledger(ctx.ledger_writer.handle)  # type: ignore[arg-type]
    chain_result = verify_chain(all_entries)
    assert chain_result.status == VerificationStatus.VALID, (
        f"chain verification failed at position "
        f"{chain_result.failure_position}: "
        f"{chain_result.failure_type.value if chain_result.failure_type else 'unknown'}"
    )


@pytest.mark.asyncio
async def test_caller_site_override_no_cp_is_wiring_does_not_emit(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """Operator opt-out: cp_is_wiring=None → silent-skip even when override applied."""
    _ = patched_runtime
    config = _config_with_pause_resume_opt_in(tmp_path)
    ctx = await run_bootstrap(config, workload_class=WORKLOAD)
    _attach_get_tracer_to_ctx(ctx)
    object.__setattr__(ctx, "cp_is_wiring", None)

    override = StepOverride(
        step_id=StepID("step-0"),
        model_binding=ModelBinding(provider="anthropic", model="claude-opus-4-7"),
    )
    manifest = _manifest_with_step_override("wf-override-opt-out", "step-0", override)
    steps = _single_inference_step()
    dispatchers = _SingleKindRegistry(_NoopDispatcher())

    result = await asyncio.to_thread(
        partial(
            execute_workflow,
            manifest_entry=manifest,
            steps=steps,
            run_id="run-override-opt-out",
            ctx=ctx,  # type: ignore[arg-type]
            default_model_binding=_DEFAULT_BINDING,
            step_dispatchers=dispatchers,  # type: ignore[arg-type]
        )
    )
    assert result.status == RunStatus.SUCCESS

    entries = _read_override_entries(ctx)
    assert entries == []


# Reading A apply (PR #83): regression test against the
# `.harness/class_2_fork_u_cp_74_actor_field_malformation.md` finding —
# override emission must persist a clean actor_id string at the
# `actor.actor_id` slot, NOT a Pydantic field-repr of an upstream `Actor`
# model. The 4 pre-existing override-emission tests above all pass on
# both pre-Reading-A and post-Reading-A code because they assert presence
# of the override entry, not the shape of its actor field. This test
# asserts the shape directly.
@pytest.mark.asyncio
async def test_caller_site_override_emission_actor_id_is_clean_identity(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """Override entry's actor.actor_id must be the bare identity string.

    Pre-Reading-A defect: `workflow_driver.py:851` passed
    `ctx.ledger_writer.actor` (an `Actor` Pydantic model) to a composer
    whose signature declared `actor: ActorIdentity` (str-newtype) and
    defensively re-wrapped via `actor=Actor(actor_id=str(actor))`.
    `str(Actor)` returned the Pydantic field-repr, producing
    `actor_id="actor_class=<ActorClass.AGENT: 'agent'> actor_id='...'"`.
    Reading A (PR #83) changes the caller to pass
    `ctx.ledger_writer.actor.actor_id` (the str slot) — composer's
    `str(actor)` is now a no-op and the wire entry carries the clean
    identity string at the nested slot.
    """
    _ = patched_runtime
    config = _config_with_pause_resume_opt_in(tmp_path)
    ctx = await run_bootstrap(config, workload_class=WORKLOAD)
    _attach_get_tracer_to_ctx(ctx)
    assert ctx.cp_is_wiring is not None

    expected_actor_id = ctx.ledger_writer.actor.actor_id

    override = StepOverride(
        step_id=StepID("step-0"),
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
    )
    manifest = _manifest_with_step_override("wf-override-actor-id-shape", "step-0", override)
    steps = _single_inference_step()
    dispatchers = _SingleKindRegistry(_NoopDispatcher())

    result = await asyncio.to_thread(
        partial(
            execute_workflow,
            manifest_entry=manifest,
            steps=steps,
            run_id="run-override-actor-id-shape",
            ctx=ctx,  # type: ignore[arg-type]
            default_model_binding=_DEFAULT_BINDING,
            step_dispatchers=dispatchers,  # type: ignore[arg-type]
        )
    )
    assert result.status == RunStatus.SUCCESS

    entries = _read_override_entries(ctx)
    assert len(entries) == 1
    persisted_actor_id = entries[0].actor.actor_id
    assert persisted_actor_id == expected_actor_id, (
        f"Override entry actor_id is malformed: expected the bare "
        f"identity string {expected_actor_id!r}, got {persisted_actor_id!r}. "
        "If this fails with a string like 'actor_class=<ActorClass...> "
        "actor_id=...' the Reading A caller-side fix at "
        "workflow_driver.py:851 has regressed."
    )
    # Belt-and-braces: the malformed shape contains '=' and '<' from the
    # Pydantic field-repr; the clean identity does not.
    assert "=" not in persisted_actor_id
    assert "<" not in persisted_actor_id
