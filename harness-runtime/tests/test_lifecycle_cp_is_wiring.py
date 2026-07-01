"""U-RT-35 — `materialize_cp_is_wiring_stage` + `RuntimeCpIsWiring` tests.

ACs per Phase 2 Session 7 L7 §12.3 (CP → IS — PARTIAL-LAND, 1 of 17 edges):

AC #1 (LANDED) — U-CP-34 sibling-ledger seam: `emit_sibling_ledger_entry`
composes via CP `construct_sibling_ledger_entry` and appends to IS chain;
chain_verification passes; idempotent on the 5-tuple per C-CP-15.1.

ACs #2 + #3 (STRUCK; routed to Class 1 at
`.harness/class_1_tension_u_rt_35_cp_is_wiring_gaps.md`) — the remaining
8 CP source units lack materialized composers (or have shape-divergent
composers like U-CP-14's CPAuditLedgerEntry); bridging at runtime would
be X-AL-3 silent design extension.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

import pytest
from harness_core.deployment_surface import DeploymentSurface
from harness_core.workload_class import WorkloadClass
from harness_cp.cp_shared_types import ActorIdentity
from harness_cp.topology_pattern import TopologyPattern
from harness_is.chain_verification import VerificationStatus, verify_chain
from harness_is.path_class_registry import PathClass
from harness_is.path_resolver import PathResolver
from harness_is.state_ledger_entry_schema import Actor, ActorClass, Identifier
from harness_is.state_ledger_write import WriteResult, read_ledger

_PROCEDURAL_TIER_SNAPSHOT_FIXTURE = Identifier("a" * 64)


def _pt_resolver() -> Identifier:
    """CP spec v1.30 §1.4: zero-arg resolver closure returning the fixture."""
    return _PROCEDURAL_TIER_SNAPSHOT_FIXTURE


from harness_runtime.config.path_bindings import build_path_binding
from harness_runtime.lifecycle.cp_is_wiring import (
    CpIsWiringBindError,
    CpIsWiringStage,
    RuntimeCpIsWiring,
    materialize_cp_is_wiring_stage,
)
from harness_runtime.lifecycle.state_ledger import (
    LedgerWriter,
    materialize_state_ledger,
)
from harness_runtime.types import (
    CollectorConfig,
    OTelConfig,
    PathBindingConfig,
    ProviderSecretsConfig,
    RuntimeConfig,
)

# ---------------------------------------------------------------------------
# Fixtures + helpers.
# ---------------------------------------------------------------------------


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


class _SiblingKwargs(TypedDict):
    parent_action_id: str
    sibling_thread_id: str
    step_index: int
    tool: str
    canonical_args: str
    sibling_agent_identity: ActorIdentity
    timestamp: datetime


def _sibling_kwargs(
    *,
    parent_action_id: str = "parent-action-0",
    sibling_thread_id: str = "sibling-thread-1",
    step_index: int = 0,
    tool: str = "Bash",
    canonical_args: str = '{"cmd":"echo hi"}',
    timestamp: datetime | None = None,
) -> _SiblingKwargs:
    return {
        "parent_action_id": parent_action_id,
        "sibling_thread_id": sibling_thread_id,
        "step_index": step_index,
        "tool": tool,
        "canonical_args": canonical_args,
        "sibling_agent_identity": ActorIdentity("agent-1"),
        "timestamp": timestamp or datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC),
    }


# ---------------------------------------------------------------------------
# Composer + shape.
# ---------------------------------------------------------------------------


def test_composer_returns_stage(tmp_path: Path) -> None:
    stage = materialize_cp_is_wiring_stage(
        _config(tmp_path),
        _ledger_writer(tmp_path),
        _pt_resolver,
    )
    assert isinstance(stage, CpIsWiringStage)
    assert isinstance(stage.wiring, RuntimeCpIsWiring)


def test_stage_is_frozen(tmp_path: Path) -> None:
    stage = materialize_cp_is_wiring_stage(
        _config(tmp_path),
        _ledger_writer(tmp_path),
        _pt_resolver,
    )
    with pytest.raises(AttributeError):
        stage.wiring = stage.wiring  # type: ignore[misc]


def test_wiring_is_frozen(tmp_path: Path) -> None:
    wiring = _wiring(tmp_path)
    with pytest.raises(AttributeError):
        wiring.ledger_writer = wiring.ledger_writer  # type: ignore[misc]


def test_bind_error_typed() -> None:
    err = CpIsWiringBindError("test")
    assert isinstance(err, Exception)


# ---------------------------------------------------------------------------
# AC #1 (LANDED) — U-CP-34 → U-IS-11 sibling-ledger seam.
# ---------------------------------------------------------------------------


def test_emit_returns_appended_on_fresh_sibling(tmp_path: Path) -> None:
    wiring = _wiring(tmp_path)
    result = wiring.emit_sibling_ledger_entry(**_sibling_kwargs())
    assert result is WriteResult.APPENDED


def test_emit_persists_entry_in_ledger(tmp_path: Path) -> None:
    wiring = _wiring(tmp_path)
    wiring.emit_sibling_ledger_entry(**_sibling_kwargs())
    entries = read_ledger(wiring.ledger_writer.handle)
    assert len(entries) == 1


def test_post_emit_chain_verification_passes(tmp_path: Path) -> None:
    wiring = _wiring(tmp_path)
    wiring.emit_sibling_ledger_entry(**_sibling_kwargs())
    entries = read_ledger(wiring.ledger_writer.handle)
    assert verify_chain(entries).status is VerificationStatus.VALID


def test_emit_actor_class_is_sub_agent(tmp_path: Path) -> None:
    """The sibling composer pins actor_class to SUB_AGENT (per C-CP-15.1)."""
    wiring = _wiring(tmp_path)
    wiring.emit_sibling_ledger_entry(**_sibling_kwargs())
    [persisted] = read_ledger(wiring.ledger_writer.handle)
    assert persisted.actor.actor_class is ActorClass.SUB_AGENT
    assert persisted.actor.actor_id == "agent-1"


def test_emit_sibling_populates_procedural_tier_snapshot_ref(tmp_path: Path) -> None:
    """R-003 — the sibling-ledger seam supplies the D-derivative sidecar from
    the wiring's bound resolver (workflow-context emission per IS spec v1.3
    §C-IS-05 §5.1). `_wiring` binds `_pt_resolver` → Identifier('a' * 64)."""
    wiring = _wiring(tmp_path)
    wiring.emit_sibling_ledger_entry(**_sibling_kwargs())
    [persisted] = read_ledger(wiring.ledger_writer.handle)
    assert persisted.procedural_tier_snapshot_ref == _PROCEDURAL_TIER_SNAPSHOT_FIXTURE


def test_action_id_is_structural_concat(tmp_path: Path) -> None:
    """`action_id = ParentActionID || sibling_thread_id || step_index` (§15.1)."""
    wiring = _wiring(tmp_path)
    wiring.emit_sibling_ledger_entry(
        **_sibling_kwargs(
            parent_action_id="parent-X",
            sibling_thread_id="thread-Y",
            step_index=42,
        )
    )
    [persisted] = read_ledger(wiring.ledger_writer.handle)
    assert persisted.action_id == "parent-Xthread-Y42"


# ---------------------------------------------------------------------------
# Idempotency on the 5-tuple per C-CP-15.1 + C-IS-07 §7.1.
# ---------------------------------------------------------------------------


def test_replay_same_5tuple_is_idempotent_noop(tmp_path: Path) -> None:
    wiring = _wiring(tmp_path)
    kwargs = _sibling_kwargs()
    first = wiring.emit_sibling_ledger_entry(**kwargs)
    second = wiring.emit_sibling_ledger_entry(**kwargs)
    assert first is WriteResult.APPENDED
    assert second is WriteResult.IDEMPOTENT_NOOP
    assert len(read_ledger(wiring.ledger_writer.handle)) == 1


def test_distinct_5tuples_yield_distinct_entries(tmp_path: Path) -> None:
    wiring = _wiring(tmp_path)
    # Differ on one component of the 5-tuple at a time.
    wiring.emit_sibling_ledger_entry(**_sibling_kwargs(parent_action_id="p-1"))
    wiring.emit_sibling_ledger_entry(**_sibling_kwargs(parent_action_id="p-2"))
    wiring.emit_sibling_ledger_entry(**_sibling_kwargs(sibling_thread_id="t-2"))
    wiring.emit_sibling_ledger_entry(**_sibling_kwargs(step_index=99))
    wiring.emit_sibling_ledger_entry(**_sibling_kwargs(tool="Read"))
    wiring.emit_sibling_ledger_entry(**_sibling_kwargs(canonical_args="{}"))
    entries = read_ledger(wiring.ledger_writer.handle)
    assert len(entries) == 6
    keys = {e.idempotency_key for e in entries}
    assert len(keys) == 6


# ---------------------------------------------------------------------------
# Chain integrity across many sequential emissions.
# ---------------------------------------------------------------------------


def test_chain_integrity_across_50_sibling_emissions(tmp_path: Path) -> None:
    wiring = _wiring(tmp_path)
    base = datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)
    for i in range(50):
        result = wiring.emit_sibling_ledger_entry(
            **_sibling_kwargs(
                sibling_thread_id=f"thread-{i}",
                step_index=i,
                timestamp=base.replace(microsecond=i),
            )
        )
        assert result is WriteResult.APPENDED
    entries = read_ledger(wiring.ledger_writer.handle)
    assert len(entries) == 50
    assert verify_chain(entries).status is VerificationStatus.VALID


# ---------------------------------------------------------------------------
# U-RT-110 — 6 NEW §16.5 composer bindings on `RuntimeCpIsWiring`.
#
# Per runtime plan v2.33 §1.2 ACs #6 + #7 + #9. Each per-method test
# constructs a real in-process LedgerWriter, awaits the corresponding
# `emit_*_state_ledger_entry` method, reads the ledger, and asserts the
# entry's `action_id` matches the canonical kebab-case identifier per
# CP spec v1.26 §16.5.3. The integration tests cover full 6-entry
# chain_verification + idempotent-on-replay.
# ---------------------------------------------------------------------------

import asyncio
import hashlib

from harness_cp.engine_class import EngineClass
from harness_cp.handoff_context import ExternalReference, ReferenceClass, StateSummary
from harness_cp.hitl_as_tool_call_rewriting import (
    HITLSemanticVariant,
    RewrittenToolCall,
)
from harness_cp.hitl_response_palette import HITLResponse
from harness_cp.pause_resume_protocol import (
    PauseEvent,
    PauseReason,
    PauseResumeProtocolEventKind,
    ResumeOutcome,
    ResumeOutcomeKind,
)
from harness_cp.workload_binding_engine_class_selection import (
    WorkloadBindingSelectionResult,
)
from harness_is.state_ledger_entry_schema import Identifier


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


def _pause_event(
    *,
    audit_entry_id: str = "pause-audit-1",
) -> PauseEvent:
    state_summary = _state_summary()
    return PauseEvent(
        paused_at="2023-11-14T22:13:20+00:00",
        pause_reason=PauseReason.OPERATOR_INITIATED_PAUSE,
        state_summary_snapshot=state_summary,
        external_refs_captured=state_summary.external_references,
        pause_audit_entry_id=Identifier(audit_entry_id),
    )


def _resume_outcome(
    *,
    kind: ResumeOutcomeKind = ResumeOutcomeKind.RESUME_CLEAN,
) -> ResumeOutcome:
    return ResumeOutcome(
        outcome_kind=kind,
        material_diff=(),
        context_revalidated=True,
        resume_audit_entry_id=None,
    )


def _rewritten_tool_call(
    *,
    tool: str = "send_email",
    server: str = "mcp-mail",
) -> RewrittenToolCall:
    return RewrittenToolCall(
        tool=tool,
        server=server,
        hitl_required=True,
        variant=HITLSemanticVariant.AWAIT_HUMAN_APPROVAL,
        response_palette=frozenset(HITLResponse),
    )


def _selection_result(
    *,
    selected: EngineClass = EngineClass.SAVE_POINT_CHECKPOINT,
) -> WorkloadBindingSelectionResult:
    return WorkloadBindingSelectionResult(
        selected_class=selected,
        candidate_set=frozenset({selected}),
        selection_rationale="single-candidate",
    )


_ACTOR = ActorIdentity("control-plane")


def test_emit_override_state_ledger_entry_writes_canonical_entry(
    tmp_path: Path,
) -> None:
    wiring = _wiring(tmp_path)
    result = asyncio.run(
        wiring.emit_override_state_ledger_entry(
            workflow_id="wf-1",
            step_id="step-1",
            post_override_step_config={"model": "claude-opus-4-7"},
            actor=_ACTOR,
        )
    )
    assert result is WriteResult.APPENDED
    [persisted] = read_ledger(wiring.ledger_writer.handle)
    assert persisted.action_id == "cp.per-step-override-application"


def test_emit_workload_class_selection_state_ledger_entry_writes_canonical_entry(
    tmp_path: Path,
) -> None:
    wiring = _wiring(tmp_path)
    result = asyncio.run(
        wiring.emit_workload_class_selection_state_ledger_entry(
            workflow_id="wf-1",
            step_id="step-1",
            selection_result=_selection_result(),
            actor=_ACTOR,
        )
    )
    assert result is WriteResult.APPENDED
    [persisted] = read_ledger(wiring.ledger_writer.handle)
    assert persisted.action_id == "cp.workload-binding-class-selection"


def test_emit_pause_resume_state_ledger_entry_writes_canonical_entry(
    tmp_path: Path,
) -> None:
    wiring = _wiring(tmp_path)
    result = asyncio.run(
        wiring.emit_pause_resume_state_ledger_entry(
            workflow_id="wf-1",
            step_id="step-1",
            protocol_event_kind=PauseResumeProtocolEventKind.PAUSE_CAPTURED,
            event_sequence_id=1,
            protocol_state_snapshot={"phase": "paused"},
            actor=_ACTOR,
        )
    )
    assert result is WriteResult.APPENDED
    [persisted] = read_ledger(wiring.ledger_writer.handle)
    assert persisted.action_id == "cp.pause-resume-protocol"


def test_emit_hitl_tool_call_rewriting_state_ledger_entry_writes_canonical_entry(
    tmp_path: Path,
) -> None:
    wiring = _wiring(tmp_path)
    result = asyncio.run(
        wiring.emit_hitl_tool_call_rewriting_state_ledger_entry(
            workflow_id="wf-1",
            step_id="step-1",
            tool_call_id="call-1",
            semantic_variant_binding_id="row-2-await-human-approval",
            rewritten_tool_call=_rewritten_tool_call(),
            actor=_ACTOR,
        )
    )
    assert result is WriteResult.APPENDED
    [persisted] = read_ledger(wiring.ledger_writer.handle)
    assert persisted.action_id == "cp.hitl-tool-call-rewriting"


def test_emit_pause_captured_state_ledger_entry_writes_canonical_entry(
    tmp_path: Path,
) -> None:
    wiring = _wiring(tmp_path)
    result = asyncio.run(
        wiring.emit_pause_captured_state_ledger_entry(
            workflow_id="wf-1",
            step_id="step-1",
            pause_event=_pause_event(),
            actor=_ACTOR,
        )
    )
    assert result is WriteResult.APPENDED
    [persisted] = read_ledger(wiring.ledger_writer.handle)
    assert persisted.action_id == "cp.pause-captured"


def test_emit_resume_attempted_state_ledger_entry_writes_canonical_entry(
    tmp_path: Path,
) -> None:
    wiring = _wiring(tmp_path)
    result = asyncio.run(
        wiring.emit_resume_attempted_state_ledger_entry(
            workflow_id="wf-1",
            step_id="step-1",
            resume_event_id="resume-evt-1",
            resume_attempt_count=1,
            resume_outcome=_resume_outcome(),
            actor=_ACTOR,
        )
    )
    assert result is WriteResult.APPENDED
    [persisted] = read_ledger(wiring.ledger_writer.handle)
    assert persisted.action_id == "cp.resume-attempted"


def _invoke_six_methods(
    wiring: RuntimeCpIsWiring, *, workflow_prefix: str = "wf"
) -> list[WriteResult]:
    """Invoke all 6 new emit methods with distinct workflow_ids (distinct thread_id).

    Returns the 6 WriteResults in invocation order.
    """
    return [
        asyncio.run(
            wiring.emit_override_state_ledger_entry(
                workflow_id=f"{workflow_prefix}-override",
                step_id="step-1",
                post_override_step_config={"k": "v"},
                actor=_ACTOR,
            )
        ),
        asyncio.run(
            wiring.emit_workload_class_selection_state_ledger_entry(
                workflow_id=f"{workflow_prefix}-selection",
                step_id="step-1",
                selection_result=_selection_result(),
                actor=_ACTOR,
            )
        ),
        asyncio.run(
            wiring.emit_pause_resume_state_ledger_entry(
                workflow_id=f"{workflow_prefix}-pauseresume",
                step_id="step-1",
                protocol_event_kind=PauseResumeProtocolEventKind.PAUSE_CAPTURED,
                event_sequence_id=1,
                protocol_state_snapshot={"k": "v"},
                actor=_ACTOR,
            )
        ),
        asyncio.run(
            wiring.emit_hitl_tool_call_rewriting_state_ledger_entry(
                workflow_id=f"{workflow_prefix}-hitl",
                step_id="step-1",
                tool_call_id="call-1",
                semantic_variant_binding_id="row-2-await-human-approval",
                rewritten_tool_call=_rewritten_tool_call(),
                actor=_ACTOR,
            )
        ),
        asyncio.run(
            wiring.emit_pause_captured_state_ledger_entry(
                workflow_id=f"{workflow_prefix}-pcapt",
                step_id="step-1",
                pause_event=_pause_event(),
                actor=_ACTOR,
            )
        ),
        asyncio.run(
            wiring.emit_resume_attempted_state_ledger_entry(
                workflow_id=f"{workflow_prefix}-rattempt",
                step_id="step-1",
                resume_event_id="resume-evt-1",
                resume_attempt_count=1,
                resume_outcome=_resume_outcome(),
                actor=_ACTOR,
            )
        ),
    ]


def test_six_emit_methods_full_chain_verification_passes(tmp_path: Path) -> None:
    """All 6 NEW methods sequenced into one ledger; chain_verification passes (C-IS-06 §6)."""
    wiring = _wiring(tmp_path)
    results = _invoke_six_methods(wiring)
    assert all(r is WriteResult.APPENDED for r in results)
    entries = read_ledger(wiring.ledger_writer.handle)
    assert len(entries) == 6
    assert verify_chain(entries).status is VerificationStatus.VALID
    action_ids = [e.action_id for e in entries]
    assert action_ids == [
        "cp.per-step-override-application",
        "cp.workload-binding-class-selection",
        "cp.pause-resume-protocol",
        "cp.hitl-tool-call-rewriting",
        "cp.pause-captured",
        "cp.resume-attempted",
    ]


def test_six_emit_methods_idempotent_on_replay(tmp_path: Path) -> None:
    """Re-invoking each method with identical inputs returns IDEMPOTENT_NOOP (§16.5.4)."""
    wiring = _wiring(tmp_path)
    first = _invoke_six_methods(wiring)
    assert all(r is WriteResult.APPENDED for r in first)
    second = _invoke_six_methods(wiring)
    assert all(r is WriteResult.IDEMPOTENT_NOOP for r in second), second
    # Ledger size unchanged after replay.
    entries = read_ledger(wiring.ledger_writer.handle)
    assert len(entries) == 6
    assert verify_chain(entries).status is VerificationStatus.VALID
