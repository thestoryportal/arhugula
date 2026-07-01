"""Negative-observation invariant enforcement for secrets — U-AS-21.

Implements C-AS-05 §5.3 (the negative-observation invariants — secrets absent
from stored prompts, log surfaces, and the audit ledger; `fetch_secret` the
sole resolution path). Declares `NegativeObservationSurface`,
`NegativeObservationViolation`, and the four validators.

Authority: Implementation_Plan_Action_Surface_v1.md §2 U-AS-21 (R3-preserved —
v1 body verbatim per Implementation_Plan_Action_Surface_v1_1.md §5.1);
Spec_Action_Surface_v1.md §5.3 C-AS-05; ADR-F5 v1.1.

Depends on: U-AS-17 (`SENSITIVE_DATA_EXCLUSIONS`); U-AS-20 (`fetch_secret` is the
sole resolution path).

Detection mechanism (documented discretion): spec §5.3 defers the specific
secret-detection mechanism (regex / fingerprint comparison / cryptographic
taint-tracking). The validators take an explicit `secret_markers` set of
known secret values — a marker-substring detector is the unit-grade placeholder
mechanism (implementation-grade parameter threading); the production detector
is a deployment-binding-time choice.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from harness_as.sandbox_span_schema import (
    SENSITIVE_DATA_EXCLUSIONS,
    AttributeValue,
)


class NegativeObservationSurface(StrEnum):
    """A surface a secret value must never appear on (C-AS-05 §5.3)."""

    STATIC_PROMPT_CACHE_PREFIX = "STATIC_PROMPT_CACHE_PREFIX"
    SPAN_ATTRIBUTES = "SPAN_ATTRIBUTES"
    LOG_RECORDS = "LOG_RECORDS"
    AUDIT_LEDGER_ENTRY = "AUDIT_LEDGER_ENTRY"


class NegativeObservationViolation(BaseModel):
    """A detected negative-observation invariant violation (C-AS-05 §5.3)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    surface: NegativeObservationSurface
    detected_at: str
    invariant: str


def _contains_marker(text: str, secret_markers: frozenset[str]) -> bool:
    return any(marker and marker in text for marker in secret_markers)


def validate_no_secret_in_static_prefix(
    prefix_content: str,
    secret_markers: frozenset[str] = frozenset(),
) -> NegativeObservationViolation | None:
    """Validate that no secret value appears in the static prompt-cache prefix.

    §5.3: secret values MUST NOT enter the static prompt cache prefix. Returns
    a violation when a known secret marker is a substring of `prefix_content`.
    """
    if _contains_marker(prefix_content, secret_markers):
        return NegativeObservationViolation(
            surface=NegativeObservationSurface.STATIC_PROMPT_CACHE_PREFIX,
            detected_at="prompt_cache_prefix",
            invariant="secret value absent from the static prompt cache prefix",
        )
    return None


def validate_no_secret_in_span_attributes(
    attributes: Mapping[str, AttributeValue],
    secret_markers: frozenset[str] = frozenset(),
) -> NegativeObservationViolation | None:
    """Validate that no secret value appears in span attributes (C-AS-05 §5.3).

    Composes with the U-AS-17 `SENSITIVE_DATA_EXCLUSIONS` (acceptance #3): an
    exclusion-set attribute name, or a known secret marker in a string value,
    is a violation.
    """
    excluded = set(attributes) & SENSITIVE_DATA_EXCLUSIONS
    leaky_value = any(
        isinstance(v, str) and _contains_marker(v, secret_markers) for v in attributes.values()
    )
    if excluded or leaky_value:
        return NegativeObservationViolation(
            surface=NegativeObservationSurface.SPAN_ATTRIBUTES,
            detected_at="span_attributes",
            invariant="secret value absent from span attributes / log surfaces",
        )
    return None


def validate_no_secret_in_audit_ledger_entry(
    entry: Mapping[str, object],
    secret_markers: frozenset[str] = frozenset(),
) -> NegativeObservationViolation | None:
    """Validate that no secret value appears in an audit-ledger entry (§5.3).

    The audit ledger carries the structure-not-content fingerprint per C-AS-08;
    a known secret marker in any entry field value is a violation.
    """
    leaky = any(isinstance(v, str) and _contains_marker(v, secret_markers) for v in entry.values())
    if leaky:
        return NegativeObservationViolation(
            surface=NegativeObservationSurface.AUDIT_LEDGER_ENTRY,
            detected_at="audit_ledger_entry",
            invariant="secret value absent from audit-ledger entries",
        )
    return None


def verify_sole_resolution_path(
    secret_arrival_site: str,
) -> NegativeObservationViolation | None:
    """Verify a secret reached the sandbox only via `fetch_secret` (§5.3).

    `fetch_secret` is the **sole** resolution path; a secret arriving by any
    other path (manifest, prompt, log, ledger) is a contract violation.
    """
    if secret_arrival_site == "fetch_secret":
        return None
    return NegativeObservationViolation(
        surface=NegativeObservationSurface.STATIC_PROMPT_CACHE_PREFIX,
        detected_at=secret_arrival_site,
        invariant="fetch_secret is the sole secret-resolution path",
    )
