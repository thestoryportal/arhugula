"""U-RT-12 — `materialize_state_ledger` tests.

ACs per Phase 2 Session 3 plan v2.1 §2 L2:
- Fresh-create produces genesis (empty + chain-VALID; first append head).
- Reattach verifies prior chain.
- Tampered chain refuses to open (raises `TamperedChainError`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from harness_core.deployment_surface import DeploymentSurface
from harness_core.workload_class import WorkloadClass
from harness_is.path_class_registry import PathClass
from harness_is.path_resolver import PathResolver
from harness_is.state_ledger_entry_schema import (
    Actor,
    ActorClass,
    Identifier,
)
from harness_is.state_ledger_write import EntryPayload, WriteKey, WriteResult
from harness_runtime.config.path_bindings import build_path_binding
from harness_runtime.lifecycle.state_ledger import (
    LedgerWriter,
    TamperedChainError,
    materialize_state_ledger,
)
from harness_runtime.types import PathBindingConfig


def _resolver_for(tmp_path: Path) -> PathResolver:
    """Build a `PathResolver` with a STATE_LEDGER entry under tmp_path."""
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


def _actor() -> Actor:
    return Actor(actor_class=ActorClass.AGENT, actor_id="test-runtime")


# ---------------------------------------------------------------------------
# Fresh-create (plan AC).
# ---------------------------------------------------------------------------


def test_fresh_create_produces_empty_genesis(tmp_path: Path) -> None:
    """No prior file → ledger created empty; writer is in genesis state."""
    writer = materialize_state_ledger(
        _resolver_for(tmp_path),
        workflow_class=WorkloadClass.SOFTWARE_ENGINEERING,
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        actor=_actor(),
    )
    assert writer.entry_count == 0
    assert writer.is_genesis is True


def test_fresh_create_first_append_is_genesis_head(tmp_path: Path) -> None:
    """The first appended entry is the chain head (prior_event_hash = zeros)."""
    from harness_is.chain_verification import VerificationStatus, verify_chain
    from harness_is.state_ledger_entry_schema import ALL_ZEROS_SENTINEL
    from harness_is.state_ledger_write import read_ledger

    writer = materialize_state_ledger(
        _resolver_for(tmp_path),
        workflow_class=WorkloadClass.SOFTWARE_ENGINEERING,
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        actor=_actor(),
    )
    payload = EntryPayload(
        action_id=Identifier("action-1"),
        idempotency_key=Identifier("key-1"),
        actor=_actor(),
        timestamp=datetime(2026, 5, 19, 0, 0, 0, tzinfo=UTC),
    )
    result = writer.append(
        payload,
        WriteKey(
            thread_id=Identifier("t-1"),
            step_id=Identifier("s-1"),
            idempotency_key=Identifier("key-1"),
        ),
    )
    assert result is WriteResult.APPENDED

    entries = read_ledger(writer.handle)
    assert len(entries) == 1
    assert entries[0].prior_event_hash == ALL_ZEROS_SENTINEL
    # Chain still verifies after the first append.
    assert verify_chain(entries).status is VerificationStatus.VALID


# ---------------------------------------------------------------------------
# Reattach verifies prior chain (plan AC).
# ---------------------------------------------------------------------------


def test_reattach_existing_valid_chain(tmp_path: Path) -> None:
    """Existing valid chain → reattach succeeds; entry_count reflects prior writes."""
    resolver = _resolver_for(tmp_path)
    # First materialize + write two entries.
    writer1 = materialize_state_ledger(
        resolver,
        workflow_class=WorkloadClass.SOFTWARE_ENGINEERING,
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        actor=_actor(),
    )
    for i in range(2):
        writer1.append(
            EntryPayload(
                action_id=Identifier(f"action-{i}"),
                idempotency_key=Identifier(f"key-{i}"),
                actor=_actor(),
                timestamp=datetime(2026, 5, 19, 0, 0, i, tzinfo=UTC),
            ),
            WriteKey(
                thread_id=Identifier(f"t-{i}"),
                step_id=Identifier(f"s-{i}"),
                idempotency_key=Identifier(f"key-{i}"),
            ),
        )

    # Second materialize (a fresh process) reattaches.
    writer2 = materialize_state_ledger(
        resolver,
        workflow_class=WorkloadClass.SOFTWARE_ENGINEERING,
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        actor=_actor(),
    )
    assert writer2.entry_count == 2
    assert writer2.is_genesis is False


# ---------------------------------------------------------------------------
# Tampered chain refuses to open (plan AC).
# ---------------------------------------------------------------------------


def test_tampered_chain_raises(tmp_path: Path) -> None:
    """A ledger with a corrupted chain link is rejected at reattach."""
    resolver = _resolver_for(tmp_path)
    # Build a valid 2-entry chain.
    writer = materialize_state_ledger(
        resolver,
        workflow_class=WorkloadClass.SOFTWARE_ENGINEERING,
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        actor=_actor(),
    )
    for i in range(2):
        writer.append(
            EntryPayload(
                action_id=Identifier(f"action-{i}"),
                idempotency_key=Identifier(f"key-{i}"),
                actor=_actor(),
                timestamp=datetime(2026, 5, 19, 0, 0, i, tzinfo=UTC),
            ),
            WriteKey(
                thread_id=Identifier(f"t-{i}"),
                step_id=Identifier(f"s-{i}"),
                idempotency_key=Identifier(f"key-{i}"),
            ),
        )

    # Tamper: rewrite entry 0's action_id (a chain-identity field).
    # `compute_response_hash(entry 0)` now differs from the stored
    # `entry[1].prior_event_hash` → CHAIN_LINK_MISMATCH at position 2.
    ledger_path = writer.handle.canonical_path
    lines = ledger_path.read_text().splitlines()
    import json

    first = json.loads(lines[0])
    first["action_id"] = "TAMPERED-ACTION-ID"
    lines[0] = json.dumps(first)
    ledger_path.write_text("\n".join(lines) + "\n")

    # Reattach must refuse.
    with pytest.raises(TamperedChainError):
        materialize_state_ledger(
            resolver,
            workflow_class=WorkloadClass.SOFTWARE_ENGINEERING,
            deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
            actor=_actor(),
        )


# ---------------------------------------------------------------------------
# Writer surface.
# ---------------------------------------------------------------------------


def test_writer_is_frozen(tmp_path: Path) -> None:
    """`LedgerWriter` is a frozen dataclass."""
    writer = materialize_state_ledger(
        _resolver_for(tmp_path),
        workflow_class=WorkloadClass.SOFTWARE_ENGINEERING,
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        actor=_actor(),
    )
    with pytest.raises((AttributeError, Exception)):
        writer.actor = None  # type: ignore[misc,assignment]


def test_writer_idempotent_append(tmp_path: Path) -> None:
    """Repeat-key append → IDEMPOTENT_NOOP per C-IS-07 §7.1 (delegated to IS)."""
    writer = materialize_state_ledger(
        _resolver_for(tmp_path),
        workflow_class=WorkloadClass.SOFTWARE_ENGINEERING,
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        actor=_actor(),
    )
    payload = EntryPayload(
        action_id=Identifier("action-1"),
        idempotency_key=Identifier("dup"),
        actor=_actor(),
        timestamp=datetime(2026, 5, 19, 0, 0, 0, tzinfo=UTC),
    )
    write_key = WriteKey(
        thread_id=Identifier("t-1"), step_id=Identifier("s-1"), idempotency_key=Identifier("dup")
    )
    assert writer.append(payload, write_key) is WriteResult.APPENDED
    assert writer.append(payload, write_key) is WriteResult.IDEMPOTENT_NOOP


def test_isinstance_ledger_writer(tmp_path: Path) -> None:
    """Return type is the documented `LedgerWriter`."""
    writer = materialize_state_ledger(
        _resolver_for(tmp_path),
        workflow_class=WorkloadClass.SOFTWARE_ENGINEERING,
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        actor=_actor(),
    )
    assert isinstance(writer, LedgerWriter)
