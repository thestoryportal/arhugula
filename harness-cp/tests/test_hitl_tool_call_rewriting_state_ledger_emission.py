"""Tests for U-CP-77 emit_hitl_tool_call_rewriting_state_ledger_entry composer.

CP spec v1.26 §16.5 row U-CP-37.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from harness_cp.cp_shared_types import ActorIdentity
from harness_cp.hitl_as_tool_call_rewriting import (
    HITLSemanticVariant,
    RewrittenToolCall,
    emit_hitl_tool_call_rewriting_state_ledger_entry,
)
from harness_cp.hitl_response_palette import HITLResponse
from harness_cp.state_ledger_canonicalization import _canonicalize_outcome_bytes
from harness_is.state_ledger_entry_schema import ActorClass, Identifier
from harness_is.state_ledger_write import EntryPayload, WriteResult

_PROCEDURAL_TIER_SNAPSHOT_FIXTURE = Identifier("a" * 64)


def _pt_resolver() -> Identifier:
    """CP spec v1.30 §1.4: zero-arg resolver closure returning the fixture."""
    return _PROCEDURAL_TIER_SNAPSHOT_FIXTURE


class _CapturingLedgerWriter:
    """Async ledger_writer stub capturing payloads for verification."""

    def __init__(self, returns: WriteResult = WriteResult.APPENDED) -> None:
        self.captured: list[EntryPayload] = []
        self._returns = returns

    async def __call__(self, payload: EntryPayload) -> WriteResult:
        self.captured.append(payload)
        return self._returns


def _rewritten(
    *,
    tool: str = "send_email",
    server: str = "mcp-mail",
    variant: HITLSemanticVariant = HITLSemanticVariant.AWAIT_HUMAN_APPROVAL,
) -> RewrittenToolCall:
    return RewrittenToolCall(
        tool=tool,
        server=server,
        hitl_required=True,
        variant=variant,
        response_palette=frozenset(HITLResponse),
    )


def _kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "workflow_id": "wf-1",
        "step_id": "step-2",
        "tool_call_id": "call-9001",
        "semantic_variant_binding_id": "row-2-await-human-approval",
        "rewritten_tool_call": _rewritten(),
        "actor": ActorIdentity("control-plane"),
        "procedural_tier_snapshot_resolver": _pt_resolver,
    }
    base.update(overrides)
    return base


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# --- AC #1 ---


def test_emit_hitl_rewriting_action_id() -> None:
    """action_id is the canonical kebab-case identifier per spec v1.26 §16.5.3 row U-CP-37."""
    writer = _CapturingLedgerWriter()
    _run(emit_hitl_tool_call_rewriting_state_ledger_entry(**_kwargs(), ledger_writer=writer))
    assert writer.captured[0].action_id == "cp.hitl-tool-call-rewriting"


# --- AC #2 ---


def test_emit_hitl_rewriting_idempotency_key_per_q_beta_i_1a() -> None:
    """idempotency_key bytes follow §16.5.4 row U-CP-37 5-tuple (v1.26 with outcome-hash suffix)."""
    rewritten = _rewritten()
    outcome_hash = hashlib.sha256(_canonicalize_outcome_bytes(rewritten)).hexdigest()
    expected = hashlib.sha256(
        b"\x1e".join(
            (
                b"wf-1",
                b"step-2",
                b"call-9001",
                b"row-2-await-human-approval",
                outcome_hash.encode("utf-8"),
            )
        )
    ).hexdigest()
    writer = _CapturingLedgerWriter()
    _run(
        emit_hitl_tool_call_rewriting_state_ledger_entry(
            **_kwargs(rewritten_tool_call=rewritten), ledger_writer=writer
        )
    )
    assert writer.captured[0].idempotency_key == expected


def test_emit_hitl_rewriting_idempotency_key_includes_semantic_variant_binding_id() -> None:
    """Different semantic_variant_binding_id at otherwise-identical inputs → different keys."""
    writer_a = _CapturingLedgerWriter()
    writer_b = _CapturingLedgerWriter()
    _run(
        emit_hitl_tool_call_rewriting_state_ledger_entry(
            **_kwargs(semantic_variant_binding_id="row-1-request-human-input"),
            ledger_writer=writer_a,
        )
    )
    _run(
        emit_hitl_tool_call_rewriting_state_ledger_entry(
            **_kwargs(semantic_variant_binding_id="row-2-await-human-approval"),
            ledger_writer=writer_b,
        )
    )
    assert writer_a.captured[0].idempotency_key != writer_b.captured[0].idempotency_key


def test_emit_hitl_rewriting_idempotency_key_includes_tool_call_id() -> None:
    """Different tool_call_id at otherwise-identical inputs → different keys."""
    writer_a = _CapturingLedgerWriter()
    writer_b = _CapturingLedgerWriter()
    _run(
        emit_hitl_tool_call_rewriting_state_ledger_entry(
            **_kwargs(tool_call_id="call-1"), ledger_writer=writer_a
        )
    )
    _run(
        emit_hitl_tool_call_rewriting_state_ledger_entry(
            **_kwargs(tool_call_id="call-2"), ledger_writer=writer_b
        )
    )
    assert writer_a.captured[0].idempotency_key != writer_b.captured[0].idempotency_key


def test_emit_hitl_rewriting_idempotency_key_includes_outcome_hash_suffix() -> None:
    """Q-β.i-1(a): different rewritten_tool_call at same disambiguators → different keys."""
    writer_a = _CapturingLedgerWriter()
    writer_b = _CapturingLedgerWriter()
    _run(
        emit_hitl_tool_call_rewriting_state_ledger_entry(
            **_kwargs(
                rewritten_tool_call=_rewritten(variant=HITLSemanticVariant.REQUEST_HUMAN_INPUT)
            ),
            ledger_writer=writer_a,
        )
    )
    _run(
        emit_hitl_tool_call_rewriting_state_ledger_entry(
            **_kwargs(
                rewritten_tool_call=_rewritten(variant=HITLSemanticVariant.ESCALATE_TO_HUMAN)
            ),
            ledger_writer=writer_b,
        )
    )
    assert writer_a.captured[0].idempotency_key != writer_b.captured[0].idempotency_key


# --- AC #3 ---


def test_emit_hitl_rewriting_fires_post_rewrite_pre_return() -> None:
    """AC #3: composer takes the RewrittenToolCall result and emits a single payload.

    Documents firing-site discipline per §16.5.7: invoked once AFTER
    `rewrite_tool_call_to_hitl(...)` produces RewrittenToolCall, BEFORE
    returning. Single-invocation → single payload.
    """
    writer = _CapturingLedgerWriter()
    rewritten = _rewritten()
    _run(
        emit_hitl_tool_call_rewriting_state_ledger_entry(
            **_kwargs(rewritten_tool_call=rewritten), ledger_writer=writer
        )
    )
    assert len(writer.captured) == 1
    assert writer.captured[0].action_id == "cp.hitl-tool-call-rewriting"


# --- AC #4 (renamed per plan v2.29) ---


def test_emit_hitl_rewriting_response_hash_is_is_computed() -> None:
    """β.i Q-β.i-3(b): composer does NOT supply response_hash; EntryPayload has no such field."""
    assert "response_hash" not in EntryPayload.model_fields
    writer = _CapturingLedgerWriter()
    _run(emit_hitl_tool_call_rewriting_state_ledger_entry(**_kwargs(), ledger_writer=writer))
    payload = writer.captured[0]
    assert set(payload.model_fields_set) <= {
        "action_id",
        "idempotency_key",
        "actor",
        "timestamp",
        "procedural_tier_snapshot_ref",
    }


# --- AC #5 ---


def test_emit_hitl_rewriting_zero_cp_audit_emission() -> None:
    """AC #5: greenfield composer emits NO CPAuditLedgerEntry per §16.5.9 invariant 5."""
    writer = _CapturingLedgerWriter()
    result = _run(
        emit_hitl_tool_call_rewriting_state_ledger_entry(**_kwargs(), ledger_writer=writer)
    )
    assert isinstance(result, WriteResult)
    assert len(writer.captured) == 1


# --- AC #6 (helper reuse) ---


def test_emit_hitl_rewriting_reuses_canonicalize_outcome_bytes_helper() -> None:
    """AC #6: outcome bytes computed via shared `_canonicalize_outcome_bytes` (U-CP-74)."""
    rewritten = _rewritten()
    expected_outcome_hash = hashlib.sha256(_canonicalize_outcome_bytes(rewritten)).hexdigest()
    # Independently re-derive the idempotency_key using the shared helper.
    expected_key = hashlib.sha256(
        b"\x1e".join(
            (
                b"wf-1",
                b"step-2",
                b"call-9001",
                b"row-2-await-human-approval",
                expected_outcome_hash.encode("utf-8"),
            )
        )
    ).hexdigest()
    writer = _CapturingLedgerWriter()
    _run(
        emit_hitl_tool_call_rewriting_state_ledger_entry(
            **_kwargs(rewritten_tool_call=rewritten), ledger_writer=writer
        )
    )
    assert writer.captured[0].idempotency_key == expected_key


# --- composer-await discipline (orthogonal to U-CP-74 AC #9) ---


def test_emit_hitl_rewriting_orthogonal_to_writer_result_variant() -> None:
    """Composer awaits ledger_writer return; does not condition on WriteResult variant."""
    appended_writer = _CapturingLedgerWriter(returns=WriteResult.APPENDED)
    noop_writer = _CapturingLedgerWriter(returns=WriteResult.IDEMPOTENT_NOOP)

    result_a = _run(
        emit_hitl_tool_call_rewriting_state_ledger_entry(**_kwargs(), ledger_writer=appended_writer)
    )
    result_b = _run(
        emit_hitl_tool_call_rewriting_state_ledger_entry(**_kwargs(), ledger_writer=noop_writer)
    )

    assert result_a == WriteResult.APPENDED
    assert result_b == WriteResult.IDEMPOTENT_NOOP
    assert appended_writer.captured[0].idempotency_key == noop_writer.captured[0].idempotency_key


# --- Actor projection ---


def test_emit_hitl_rewriting_actor_projects_to_agent_class() -> None:
    """actor_id is the ActorIdentity stringified; actor_class = AGENT."""
    writer = _CapturingLedgerWriter()
    _run(
        emit_hitl_tool_call_rewriting_state_ledger_entry(
            **_kwargs(actor=ActorIdentity("control-plane")),
            ledger_writer=writer,
        )
    )
    actor = writer.captured[0].actor
    assert actor.actor_class == ActorClass.AGENT
    assert actor.actor_id == "control-plane"
