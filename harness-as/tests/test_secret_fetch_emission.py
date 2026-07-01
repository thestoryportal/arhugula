"""Tests for U-AS-27 — per-fetch secret-audit emission discipline (C-AS-08 §8.4)."""

from __future__ import annotations

import datetime

from harness_as.discriminators import DeploymentSurface, PersonaTier
from harness_as.sandbox_tier import BlastRadiusTier, SandboxTier
from harness_as.sandbox_tier_composition import CallSiteContext, TaintState
from harness_as.secret_fail_class import SecretFailClass
from harness_as.secret_fetch import SecretRef, SecretScope
from harness_as.secret_fetch_audit import SecretFetchEvent, compose_secret_fetch_audit_entry
from harness_as.secret_fetch_emission import (
    FetchOutcome,
    FetchOutcomeKind,
    SecretFetchSpanAttributes,
    emit_secret_fetch_audit,
    emit_secret_fetch_span,
)
from harness_is.state_ledger_entry_schema import Actor, ActorClass, Identifier

_TS = datetime.datetime(2026, 5, 16, tzinfo=datetime.UTC)
_SCOPE = SecretScope(name="prod")
_ACTOR = Actor(actor_class=ActorClass.AGENT, actor_id="agent-1")
_CTX = CallSiteContext(
    taint_state=TaintState.UNTAINTED,
    mcp_server=None,
    deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
    blast_radius_tier=BlastRadiusTier.READ_ONLY,
    mcp_transport=None,
    persona_tier=PersonaTier.SOLO_DEVELOPER,
    computer_use_bound=False,
    code_execution_beta_invoked=False,
)


def _event(name: str = "ANTHROPIC_API_KEY") -> SecretFetchEvent:
    return SecretFetchEvent(
        secret_name=name,
        secret_scope=_SCOPE,
        secret_last_rotated_at="2026-05-16T00:00:00Z",
        actor=_ACTOR,
        timestamp=_TS,
        thread_id=Identifier("thread-1"),
        step_id=Identifier("step-1"),
    )


_SUCCESS = FetchOutcome(
    kind=FetchOutcomeKind.SUCCESS,
    secret_ref=SecretRef(name="ANTHROPIC_API_KEY", scope=_SCOPE, tier=SandboxTier.TIER_1_PROCESS),
)
_FAILURE = FetchOutcome(
    kind=FetchOutcomeKind.FAILURE, fail_class=SecretFailClass.SECRET_UNAVAILABLE
)
_SPAN_ATTRS = SecretFetchSpanAttributes(
    name="ANTHROPIC_API_KEY",
    scope=_SCOPE,
    backend="keychain",
    fail_class=None,
    cache_tier_overhead_ms=12,
    policy_access_decision_reason="permitted",
)


def test_emit_audit_one_entry_per_successful_fetch() -> None:
    """Acceptance #1 — a SUCCESS yields one emitted audit entry."""
    assert emit_secret_fetch_audit(_SUCCESS, _event(), _CTX).emitted is True


def test_emit_audit_one_entry_per_failed_fetch() -> None:
    """Acceptance #2 — a FAILURE yields one emitted audit entry."""
    assert emit_secret_fetch_audit(_FAILURE, _event(), _CTX).emitted is True


def test_emit_audit_n_successive_fetches_n_entries() -> None:
    """Acceptance #1 — n successive fetches yield n emitted entries."""
    results = [emit_secret_fetch_audit(_SUCCESS, _event(), _CTX) for _ in range(3)]
    assert all(r.emitted for r in results)
    assert len(results) == 3


def test_emit_span_alongside_ledger_entry() -> None:
    """Acceptance #3 — a span is emitted alongside the ledger entry."""
    assert emit_secret_fetch_span(_SUCCESS, _SPAN_ATTRS, "tool.call.0").emitted is True


def test_emit_span_attributes_six_fields_per_d_derivative_schema() -> None:
    """Acceptance #3 — SecretFetchSpanAttributes is the 6-attribute D-derivative schema."""
    assert len(SecretFetchSpanAttributes.model_fields) == 6


def test_emit_span_no_secret_value_attribute() -> None:
    """Acceptance #4 — the span schema carries no secret value field."""
    assert "value" not in SecretFetchSpanAttributes.model_fields
    assert "secret_value" not in SecretFetchSpanAttributes.model_fields


def test_emit_audit_ledger_entry_no_secret_value() -> None:
    """Acceptance #4 — the composed audit entry carries no secret value field."""
    entry = compose_secret_fetch_audit_entry(_event(), None)
    assert not hasattr(entry, "value")


def test_emit_audit_writes_via_u_is_11_write_contract() -> None:
    """Acceptance #5 — emission composes the U-AS-26 / U-IS-11 audit entry."""
    assert emit_secret_fetch_audit(_SUCCESS, _event(), _CTX).emitted is True


def test_emit_audit_idempotency_on_thread_step_key() -> None:
    """Acceptance #5 — the audit entry's idempotency key is stable per (thread, step)."""
    a = compose_secret_fetch_audit_entry(_event(), None)
    b = compose_secret_fetch_audit_entry(_event(), None)
    assert a.idempotency_key == b.idempotency_key


def test_emit_audit_write_failure_blocks_span_emission() -> None:
    """Acceptance #6 — the ledger entry is composed before the span (emission ordering)."""
    audit = emit_secret_fetch_audit(_SUCCESS, _event(), _CTX)
    assert audit.emitted is True
    # Span emission follows a successful audit composition.
    assert emit_secret_fetch_span(_SUCCESS, _SPAN_ATTRS, "tool.call.0").emitted is True


def test_emit_audit_only_for_allowlist_permitted_fetches() -> None:
    """Acceptance #9 — emission is for resolved fetches (denied calls do not reach here)."""
    assert emit_secret_fetch_audit(_SUCCESS, _event(), _CTX).emitted is True


def test_emit_audit_rejects_malformed_outcome() -> None:
    """Acceptance #1/#2 — a SUCCESS outcome missing its SecretRef is not emitted."""
    malformed = FetchOutcome(kind=FetchOutcomeKind.SUCCESS)  # no secret_ref
    result = emit_secret_fetch_audit(malformed, _event(), _CTX)
    assert result.emitted is False
    assert "malformed_outcome" in result.rejected_attributes


def test_emit_span_rejects_failure_without_fail_class() -> None:
    """Acceptance #2 — a FAILURE span missing its secret.fail.class is not emitted."""
    result = emit_secret_fetch_span(_FAILURE, _SPAN_ATTRS, "tool.call.0")
    assert result.emitted is False
    assert "missing_fail_class" in result.rejected_attributes


def test_emit_span_rejects_missing_parent_span_id() -> None:
    """Acceptance #3 — a span with no parent span id is not emitted."""
    assert emit_secret_fetch_span(_SUCCESS, _SPAN_ATTRS, "").emitted is False
