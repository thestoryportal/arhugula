"""Per-fetch secret-audit emission discipline + span emission — U-AS-27.

Implements C-AS-08 §8.4 (per-fetch emission discipline), §8.5 (cross-axis
composition reference). Declares `FetchOutcome`, `SecretFetchSpanAttributes`,
and the `emit_secret_fetch_audit` / `emit_secret_fetch_span` functions.

Authority: Implementation_Plan_Action_Surface_v1.md §2 U-AS-27 (R3-preserved —
v1 body verbatim per Implementation_Plan_Action_Surface_v1_1.md §5.1);
Spec_Action_Surface_v1.md §8.4-§8.5 C-AS-08; ADR-F5 v1.1 §Consequences (c).

Depends on: U-AS-17 (`EmissionResult`, span validation); U-AS-22 (allowlist —
emission only for PERMITTED fetches); U-AS-24 (`SecretFailClass`); U-AS-26
(`SecretFetchEvent`, `compose_secret_fetch_audit_entry`); U-IS-11 (cross-axis:
IS — the append-only write contract). The actual `.harness/state.jsonl` append
via U-IS-11 + the hash-chain prior are runtime concerns; `emit_secret_fetch_audit`
composes the audit entry and returns the structured result.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from harness_as.sandbox_span_schema import EmissionResult
from harness_as.sandbox_tier_composition import CallSiteContext
from harness_as.secret_fail_class import SecretFailClass
from harness_as.secret_fetch import SecretRef, SecretScope
from harness_as.secret_fetch_audit import (
    SecretFetchEvent,
    compose_secret_fetch_audit_entry,
)


class FetchOutcomeKind(StrEnum):
    """The discriminant of a secret-fetch outcome (C-AS-08 §8.4)."""

    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"


class FetchOutcome(BaseModel):
    """The outcome of a `fetch_secret` call (C-AS-08 §8.4).

    `SUCCESS` carries the resolved `SecretRef`; `FAILURE` carries the
    `SecretFailClass` (U-AS-24).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: FetchOutcomeKind
    secret_ref: SecretRef | None = None
    fail_class: SecretFailClass | None = None


class SecretFetchSpanAttributes(BaseModel):
    """The six-attribute secret-fetch span schema (C-AS-08 §8.4 / ADR-F5 D-derivative).

    Structure-not-content — the schema carries no secret value field
    (acceptance #4).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    scope: SecretScope
    backend: str
    fail_class: SecretFailClass | None
    cache_tier_overhead_ms: int
    policy_access_decision_reason: str


def _outcome_well_formed(outcome: FetchOutcome) -> bool:
    """True when a `FetchOutcome` carries the payload its kind requires."""
    if outcome.kind is FetchOutcomeKind.SUCCESS:
        return outcome.secret_ref is not None and outcome.fail_class is None
    return outcome.fail_class is not None and outcome.secret_ref is None


def emit_secret_fetch_audit(
    outcome: FetchOutcome,
    event_metadata: SecretFetchEvent,
    call_site_context: CallSiteContext,
) -> EmissionResult:
    """Emit the per-fetch audit-ledger entry (C-AS-08 §8.4).

    Per §8.4: a SUCCESS yields exactly one ledger entry; a FAILURE yields
    exactly one entry carrying the `secret.fail.class` (U-AS-24). A malformed
    `outcome` (a SUCCESS without a `SecretRef`, or a FAILURE without a
    `SecretFailClass`) is **not** emitted. The entry is the U-AS-26 six-field
    `StateLedgerEntry` — no secret value (acceptance #4) — composed from
    `event_metadata` (whose actor / thread / step the entry inherits);
    `call_site_context` carries the deployment context for the sibling span.
    The actual `.harness/state.jsonl` append delegates to U-IS-11's append-only
    contract, idempotent on the entry's `(thread_id, step_id)` key
    (acceptance #5) — that file append is a runtime concern. Emission ordering
    (acceptance #6): the ledger entry is composed before any span emission.
    """
    if not _outcome_well_formed(outcome):
        return EmissionResult(emitted=False, rejected_attributes=("malformed_outcome",))
    _ = call_site_context  # deployment context carried for the sibling span
    compose_secret_fetch_audit_entry(event_metadata, None)
    return EmissionResult(emitted=True, rejected_attributes=())


def emit_secret_fetch_span(
    outcome: FetchOutcome,
    span_attrs: SecretFetchSpanAttributes,
    parent_span_id: str,
) -> EmissionResult:
    """Emit the secret-fetch span alongside the ledger entry (C-AS-08 §8.4 row 3).

    The six-attribute D-derivative span schema carries structure only — no
    secret value (acceptance #4 / negative-observation per U-AS-21). The span
    is **not** emitted when it is inconsistent with `outcome`: a FAILURE span
    must carry the `secret.fail.class` and a SUCCESS span must not, and the
    span must link to a non-empty `parent_span_id`.
    """
    rejected: list[str] = []
    if not parent_span_id:
        rejected.append("missing_parent_span_id")
    if outcome.kind is FetchOutcomeKind.FAILURE and span_attrs.fail_class is None:
        rejected.append("missing_fail_class")
    if outcome.kind is FetchOutcomeKind.SUCCESS and span_attrs.fail_class is not None:
        rejected.append("unexpected_fail_class")
    return EmissionResult(emitted=not rejected, rejected_attributes=tuple(rejected))
