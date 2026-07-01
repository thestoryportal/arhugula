"""R-CXA-2 engine recovery loop producer tests."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest
from harness_core import EntryID
from harness_core.deployment_surface import DeploymentSurface
from harness_core.workload_class import WorkloadClass
from harness_cp.cp_shared_types import ActorIdentity
from harness_cp.engine_class import EngineClass
from harness_cp.handoff_context import ExternalReference, ReferenceClass, StateSummary
from harness_cp.pause_resume_protocol import (
    DeterministicEnginePauseResumeSubstrate,
    PauseReason,
    ResumeOutcomeKind,
)
from harness_cp.topology_pattern import TopologyPattern
from harness_is.chain_verification import VerificationStatus, verify_chain
from harness_is.path_class_registry import PathClass
from harness_is.path_resolver import PathResolver
from harness_is.state_ledger_entry_schema import Actor, ActorClass, Identifier
from harness_is.state_ledger_write import WriteResult, read_ledger
from harness_runtime.config.path_bindings import build_path_binding
from harness_runtime.lifecycle.cp_is_wiring import (
    RuntimeCpIsWiring,
    materialize_cp_is_wiring_stage,
)
from harness_runtime.lifecycle.engine_recovery_loop import (
    EngineRecoverySubstrateNotBoundError,
    RuntimeEngineRecoveryLoop,
)
from harness_runtime.lifecycle.state_ledger import LedgerWriter, materialize_state_ledger
from harness_runtime.types import (
    CollectorConfig,
    OTelConfig,
    PathBindingConfig,
    ProviderSecretsConfig,
    RuntimeConfig,
)

_ACTOR = ActorIdentity("engine-loop")
_PROCEDURAL_TIER_SNAPSHOT_FIXTURE = Identifier("a" * 64)


def _pt_resolver() -> Identifier:
    return _PROCEDURAL_TIER_SNAPSHOT_FIXTURE


def _state_summary(version: str = "v1") -> StateSummary:
    return StateSummary(
        relevant_entries=(),
        summary_text=version,
        summary_hash=hashlib.sha256(version.encode()).hexdigest(),
        idempotency_key=Identifier("idem-" + version),
        external_references=(
            ExternalReference(
                reference_class=ReferenceClass.FILESYSTEM_STATE,
                reference_id="state-" + version,
                snapshot_capture_at_pause=b"snapshot-" + version.encode("utf-8"),
            ),
        ),
    )


def _resolver_for(tmp_path: Path) -> PathResolver:
    config = PathBindingConfig(
        raw_entries=(
            {
                "path_class": PathClass.STATE_LEDGER,
                "workflow_class": WorkloadClass.SOFTWARE_ENGINEERING,
                "deployment_surface": DeploymentSurface.LOCAL_DEVELOPMENT,
                "path": str(tmp_path / "state.jsonl"),
            },
        ),
    )
    return PathResolver(build_path_binding(config))


def _ledger_writer(tmp_path: Path) -> LedgerWriter:
    return materialize_state_ledger(
        _resolver_for(tmp_path),
        workflow_class=WorkloadClass.SOFTWARE_ENGINEERING,
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        actor=Actor(actor_class=ActorClass.AGENT, actor_id="test-runtime"),
    )


def _config(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        repository_root=tmp_path,
        path_bindings=PathBindingConfig(),
        provider_secrets=ProviderSecretsConfig(),
        otel=OTelConfig(otlp_endpoint="http://localhost:4317"),
        collector=CollectorConfig(),
        default_topology=TopologyPattern.SINGLE_THREADED_LINEAR,
    )


def _wiring(tmp_path: Path) -> RuntimeCpIsWiring:
    stage = materialize_cp_is_wiring_stage(
        _config(tmp_path),
        _ledger_writer(tmp_path),
        _pt_resolver,
    )
    return stage.wiring


def _loop(tmp_path: Path) -> RuntimeEngineRecoveryLoop:
    substrate = DeterministicEnginePauseResumeSubstrate(
        state_summary_provider=_state_summary,
        pause_audit_entry_id_provider=lambda _workflow_id, _reason: EntryID("pause-entry-1"),
    )
    return RuntimeEngineRecoveryLoop(
        wiring=_wiring(tmp_path),
        substrate_by_engine_class={EngineClass.WAL_SEGMENT: substrate},
        actor=_ACTOR,
    )


def test_engine_recovery_loop_emits_pause_captured(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    result = asyncio.run(
        loop.capture_pause(
            engine_class=EngineClass.WAL_SEGMENT,
            workflow_id="wf-1",
            run_id="run-1",
            step_id="step-1",
            pause_reason=PauseReason.OPERATOR_INITIATED_PAUSE,
        )
    )

    assert result.write_result is WriteResult.APPENDED
    assert result.pause_event.pause_audit_entry_id == "pause-entry-1"
    entries = read_ledger(loop.wiring.ledger_writer.handle)
    assert [entry.action_id for entry in entries] == ["cp.pause-captured"]
    assert verify_chain(entries).status is VerificationStatus.VALID


def test_has_pause_record_is_a_nonemitting_presence_peek(tmp_path: Path) -> None:
    """[P1-r3-a] (Codex) `has_pause_record` reports PRESENCE — not validity —
    WITHOUT emitting a `cp.resume-attempted` entry, so a driver can gate the
    resume firing and avoid a spurious ABORT entry for an ordinary step-prefix
    crash recovery (no engine pause captured). Presence-not-validity is the fix
    for the prior `has_captured_pause`, which used the resume outcome (validity)
    as a presence proxy and so misread a present-but-corrupt snapshot as absent —
    silently skipping the resume + losing the abort record."""
    loop = _loop(tmp_path)
    asyncio.run(
        loop.capture_pause(
            engine_class=EngineClass.WAL_SEGMENT,
            workflow_id="wf-1",
            run_id="run-1",
            step_id="step-1",
            pause_reason=PauseReason.OPERATOR_INITIATED_PAUSE,
        )
    )
    baseline = [entry.action_id for entry in read_ledger(loop.wiring.ledger_writer.handle)]
    assert baseline == ["cp.pause-captured"]

    # The captured pause is reported present (True) for its OWN run; a workflow
    # without one is absent (False); and — Codex [P2] run-scoping — the SAME
    # workflow_id under a DIFFERENT run_id is also absent (a fresh execution must
    # not see an earlier run's record). NEITHER peek writes a ledger entry.
    assert (
        loop.has_pause_record(
            engine_class=EngineClass.WAL_SEGMENT, workflow_id="wf-1", run_id="run-1"
        )
        is True
    )
    assert (
        loop.has_pause_record(
            engine_class=EngineClass.WAL_SEGMENT, workflow_id="wf-1", run_id="run-2"
        )
        is False
    )
    assert (
        loop.has_pause_record(
            engine_class=EngineClass.WAL_SEGMENT, workflow_id="wf-absent", run_id="run-1"
        )
        is False
    )
    after = [entry.action_id for entry in read_ledger(loop.wiring.ledger_writer.handle)]
    assert after == baseline


def test_engine_recovery_loop_emits_resume_attempted_on_abort(tmp_path: Path) -> None:
    loop = _loop(tmp_path)
    result = asyncio.run(
        loop.attempt_resume(
            engine_class=EngineClass.WAL_SEGMENT,
            workflow_id="missing-workflow",
            run_id="run-1",
            step_id="step-1",
            resume_event_id="resume-evt-1",
            resume_attempt_count=1,
            resume_at="2026-06-08T12:00:00Z",
        )
    )

    assert result.write_result is WriteResult.APPENDED
    assert result.resume_outcome.outcome_kind is ResumeOutcomeKind.ABORT_SNAPSHOT_CORRUPTED
    entries = read_ledger(loop.wiring.ledger_writer.handle)
    assert [entry.action_id for entry in entries] == ["cp.resume-attempted"]
    assert verify_chain(entries).status is VerificationStatus.VALID


def test_unbound_engine_class_fails_loud(tmp_path: Path) -> None:
    """The engine-class registry (U-RT-124) is the single source of routing truth: a
    firing call for an engine class with NO bound substrate is a materialization
    defect, so it FAILS LOUD (`EngineRecoverySubstrateNotBoundError`, detect-then-
    refuse) rather than a silent no-op or a raw `KeyError`. `_loop` binds WAL_SEGMENT
    only; a RECONCILER_LOOP firing has no substrate. The driver only fires for a
    gated, in-scope engine class, so this raise is preserved-but-unreachable in a
    correct bind."""
    loop = _loop(tmp_path)
    with pytest.raises(EngineRecoverySubstrateNotBoundError) as excinfo:
        loop.has_pause_record(
            engine_class=EngineClass.RECONCILER_LOOP, workflow_id="wf-1", run_id="run-1"
        )
    assert excinfo.value.engine_class is EngineClass.RECONCILER_LOOP
