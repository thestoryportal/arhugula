"""U-RT-34 — `materialize_as_is_wiring_stage` + `RuntimeAsIsWiring` tests.

ACs per Phase 2 Session 7 L7 §12.2 (AS → IS — 1 edge per C-RT-12 §12.2):

1. Round-trip: `emit_secret_fetch_audit_entry(event)` composes via AS surface,
   appends to IS chain, returns `APPENDED`; IS `verify_chain` returns VALID;
   the entry appears in `.harness/state.jsonl` per spec post-wiring invariant.
2. Idempotent replay: a re-emission of the same `SecretFetchEvent` yields
   `IDEMPOTENT_NOOP` per U-AS-27 AC #5 (idempotency on the AS
   `_idempotency_key(thread_id, step_id, secret_name, secret_scope.name)`
   formula).
3. Chain integrity preserved across 50 sequential emissions.

Plus shape coverage: composer wiring, stage frozen, bind error typed, the
AS-computed `response_hash` is dropped at the IS boundary (the IS chain owns
its own hash discipline per C-IS-07 §7.1 acceptance #8).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from harness_as.secret_fetch import SecretScope
from harness_as.secret_fetch_audit import (
    SecretFetchEvent,
    compose_secret_fetch_audit_entry,
)
from harness_core.deployment_surface import DeploymentSurface
from harness_core.workload_class import WorkloadClass
from harness_cp.topology_pattern import TopologyPattern
from harness_is.chain_verification import VerificationStatus, verify_chain
from harness_is.path_class_registry import PathClass
from harness_is.path_resolver import PathResolver
from harness_is.state_ledger_entry_schema import (
    Actor,
    ActorClass,
    Identifier,
)
from harness_is.state_ledger_write import WriteResult, read_ledger
from harness_runtime.config.path_bindings import build_path_binding
from harness_runtime.lifecycle.as_is_wiring import (
    AsIsWiringBindError,
    AsIsWiringStage,
    RuntimeAsIsWiring,
    materialize_as_is_wiring_stage,
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


def _wiring(tmp_path: Path) -> RuntimeAsIsWiring:
    stage = materialize_as_is_wiring_stage(_config(tmp_path), _ledger_writer(tmp_path))
    return stage.wiring


def _event(
    *,
    secret_name: str = "OPENAI_API_KEY",
    thread_id: str = "thread-1",
    step_id: str = "step-1",
    timestamp: datetime | None = None,
) -> SecretFetchEvent:
    ts = timestamp if timestamp is not None else datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)
    return SecretFetchEvent(
        secret_name=secret_name,
        secret_scope=SecretScope(name="default"),
        secret_last_rotated_at="2026-05-01T00:00:00+00:00",
        actor=Actor(actor_class=ActorClass.AGENT, actor_id="emission-site"),
        timestamp=ts,
        thread_id=Identifier(thread_id),
        step_id=Identifier(step_id),
    )


# ---------------------------------------------------------------------------
# Composer + shape.
# ---------------------------------------------------------------------------


def test_composer_returns_stage(tmp_path: Path) -> None:
    stage = materialize_as_is_wiring_stage(_config(tmp_path), _ledger_writer(tmp_path))
    assert isinstance(stage, AsIsWiringStage)
    assert isinstance(stage.wiring, RuntimeAsIsWiring)


def test_stage_is_frozen(tmp_path: Path) -> None:
    stage = materialize_as_is_wiring_stage(_config(tmp_path), _ledger_writer(tmp_path))
    with pytest.raises(AttributeError):
        stage.wiring = stage.wiring  # type: ignore[misc]


def test_wiring_is_frozen(tmp_path: Path) -> None:
    wiring = _wiring(tmp_path)
    with pytest.raises(AttributeError):
        wiring.ledger_writer = wiring.ledger_writer  # type: ignore[misc]


def test_bind_error_typed() -> None:
    err = AsIsWiringBindError("test")
    assert isinstance(err, Exception)


# ---------------------------------------------------------------------------
# AC #1 — Round-trip: emit → IS chain → verify_chain VALID.
# ---------------------------------------------------------------------------


def test_emit_returns_appended_on_fresh_event(tmp_path: Path) -> None:
    wiring = _wiring(tmp_path)
    result = wiring.emit_secret_fetch_audit_entry(_event())
    assert result is WriteResult.APPENDED


def test_emit_persists_entry_in_ledger(tmp_path: Path) -> None:
    wiring = _wiring(tmp_path)
    wiring.emit_secret_fetch_audit_entry(_event())
    entries = read_ledger(wiring.ledger_writer.handle)
    assert len(entries) == 1


def test_post_emit_chain_verification_passes(tmp_path: Path) -> None:
    wiring = _wiring(tmp_path)
    wiring.emit_secret_fetch_audit_entry(_event())
    entries = read_ledger(wiring.ledger_writer.handle)
    assert verify_chain(entries).status is VerificationStatus.VALID


def test_emit_preserves_as_composed_idempotency_key(tmp_path: Path) -> None:
    """The persisted entry's idempotency_key matches AS's `_idempotency_key`."""
    wiring = _wiring(tmp_path)
    event = _event()
    wiring.emit_secret_fetch_audit_entry(event)
    [persisted] = read_ledger(wiring.ledger_writer.handle)
    expected = compose_secret_fetch_audit_entry(event, None)
    assert persisted.idempotency_key == expected.idempotency_key


# ---------------------------------------------------------------------------
# AC #2 — Idempotent replay (U-AS-27 AC #5: duplicate writes no-op).
# ---------------------------------------------------------------------------


def test_replay_same_event_is_idempotent_noop(tmp_path: Path) -> None:
    wiring = _wiring(tmp_path)
    event = _event()
    first = wiring.emit_secret_fetch_audit_entry(event)
    second = wiring.emit_secret_fetch_audit_entry(event)
    assert first is WriteResult.APPENDED
    assert second is WriteResult.IDEMPOTENT_NOOP
    assert len(read_ledger(wiring.ledger_writer.handle)) == 1


def test_different_events_yield_distinct_entries(tmp_path: Path) -> None:
    """Distinct identity tuples → distinct idempotency keys → distinct entries."""
    wiring = _wiring(tmp_path)
    wiring.emit_secret_fetch_audit_entry(_event(thread_id="t-1", step_id="s-1"))
    wiring.emit_secret_fetch_audit_entry(_event(thread_id="t-2", step_id="s-2"))
    wiring.emit_secret_fetch_audit_entry(
        _event(thread_id="t-1", step_id="s-1", secret_name="ANTHROPIC_API_KEY")
    )
    entries = read_ledger(wiring.ledger_writer.handle)
    assert len(entries) == 3
    keys = {e.idempotency_key for e in entries}
    assert len(keys) == 3


# ---------------------------------------------------------------------------
# AC #3 — Chain integrity preserved across 50 sequential emissions.
# ---------------------------------------------------------------------------


def test_chain_integrity_across_50_sequential_emissions(tmp_path: Path) -> None:
    wiring = _wiring(tmp_path)
    base = datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)
    for i in range(50):
        event = _event(
            thread_id=f"thread-{i}",
            step_id=f"step-{i}",
            secret_name=f"SECRET_{i}",
            timestamp=base.replace(microsecond=i),
        )
        result = wiring.emit_secret_fetch_audit_entry(event)
        assert result is WriteResult.APPENDED
    entries = read_ledger(wiring.ledger_writer.handle)
    assert len(entries) == 50
    assert verify_chain(entries).status is VerificationStatus.VALID


# ---------------------------------------------------------------------------
# Field-extraction discipline — AS response_hash is informational; IS owns
# the chain's response_hash via internal computation.
# ---------------------------------------------------------------------------


def test_is_chain_response_hash_is_not_as_outputs_hash(tmp_path: Path) -> None:
    """The persisted entry's `response_hash` is the IS-computed value, not AS's."""
    wiring = _wiring(tmp_path)
    event = _event()
    wiring.emit_secret_fetch_audit_entry(event)
    [persisted] = read_ledger(wiring.ledger_writer.handle)
    as_composed = compose_secret_fetch_audit_entry(event, None)
    # AS compose populates a structure-not-content outputs_hash; the IS chain
    # re-computes its own response_hash internally per C-IS-07 §7.1 acceptance #8.
    # These are distinct values by design — the IS-side hash governs chain integrity.
    assert persisted.response_hash != as_composed.response_hash


def test_actor_preserved_from_event(tmp_path: Path) -> None:
    wiring = _wiring(tmp_path)
    event = _event()
    wiring.emit_secret_fetch_audit_entry(event)
    [persisted] = read_ledger(wiring.ledger_writer.handle)
    assert persisted.actor == event.actor


def test_emit_leaves_procedural_tier_snapshot_ref_none_without_resolver(
    tmp_path: Path,
) -> None:
    """Direct/bootstrap AS→IS wiring without a resolver preserves None-canonical sidecar."""
    wiring = _wiring(tmp_path)
    wiring.emit_secret_fetch_audit_entry(_event())
    [persisted] = read_ledger(wiring.ledger_writer.handle)
    assert persisted.procedural_tier_snapshot_ref is None


def test_emit_populates_procedural_tier_snapshot_ref_when_resolver_bound(
    tmp_path: Path,
) -> None:
    """Workflow-context AS→IS secret-fetch writes carry the R-003 sidecar."""
    snapshot = Identifier("b" * 64)
    wiring = RuntimeAsIsWiring(
        ledger_writer=_ledger_writer(tmp_path),
        procedural_tier_snapshot_resolver=lambda: snapshot,
    )
    wiring.emit_secret_fetch_audit_entry(_event())
    [persisted] = read_ledger(wiring.ledger_writer.handle)
    assert persisted.procedural_tier_snapshot_ref == snapshot
