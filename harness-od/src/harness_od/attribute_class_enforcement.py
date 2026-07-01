"""Cardinality-safe + cardinality-prohibited attribute classes — U-OD-14.

Implements C-OD-11 §11.2 (cardinality-safe attribute set — attributes
admissible as metric / dashboard query dimensions) and §11.3
(cardinality-prohibited attribute set — span-only attributes that MUST NOT
appear as dashboard query dimensions; high-cardinality dashboard queries cause
cardinality blowup).

`CARDINALITY_SAFE_ATTRIBUTES` is the §11.2 table transcribed verbatim — the
§11.2 table has 12 rows; the `harness.breaker.from_state` / `to_state` row
covers two attribute names, so the set holds 13 attribute names total.
`CARDINALITY_PROHIBITED_ATTRIBUTES` is the §11.3 table — 6 rows. The two sets
are disjoint. `assert_cardinality_safe_for_dashboard_dimension` and
`assert_cardinality_prohibited_not_in_dashboard_dimension` are the
dashboard-query-construction-time enforcement gates.

Authority: Implementation_Plan_Operational_Discipline_v2_5.md §3.4.4 U-OD-14
(v2.5 conformance revision — `CARDINALITY_SAFE_ATTRIBUTES` +
`CARDINALITY_PROHIBITED_ATTRIBUTES` member sets conformed to OD spec v1.2
§11.2 / §11.3; preserved verbatim through v2.6 / v2.7 per v2.6 §3 pointer
table); Depends on: [U-OD-05, U-OD-13]; Spec_Operational_Discipline_v1_2.md
§11 C-OD-11 §11.2 + §11.3 (preserved verbatim into v1.3 per v1.3 §0.1);
ADR-D6 v1.1 §1.3 cardinality-discipline paragraph.
"""

from __future__ import annotations

__all__ = [
    "CARDINALITY_PROHIBITED_ATTRIBUTES",
    "CARDINALITY_SAFE_ATTRIBUTES",
    "CardinalityViolation",
    "assert_cardinality_prohibited_not_in_dashboard_dimension",
    "assert_cardinality_safe_for_dashboard_dimension",
]


#: §11.2 verbatim — the cardinality-safe attribute set. Attributes admissible
#: as metric / dashboard query dimensions: bounded-cardinality enum strings and
#: low-cardinality identifiers. The §11.2 table has 12 rows; the
#: `harness.breaker.from_state` / `to_state` row covers two attribute names —
#: 13 attribute names total (acceptance #1).
CARDINALITY_SAFE_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "gen_ai.operation.name",
        "gen_ai.provider.name",
        "gen_ai.request.model",
        "gen_ai.response.finish_reasons",
        "sandbox.tier",
        "sandbox.tech",
        "sandbox.provider",
        "hitl.gate.level",
        "hitl.response.class",
        "harness.breaker.scope",
        "harness.breaker.from_state",
        "harness.breaker.to_state",
        "validator.fail.class",
    }
)

#: §11.3 verbatim — the cardinality-prohibited attribute set. Span-only
#: attributes: high-cardinality identifiers and hashes that MAY appear as span
#: attributes for trace-level join keys but MUST NEVER appear as dashboard
#: query dimensions. The §11.3 table has 6 rows (acceptance #2). Two §11.3
#: rows each name a compound surface: "Session IDs, user IDs, tenant IDs" is
#: carried as `session_user_tenant_ids`; "audit.signature.sha256 /
#: audit.signature.prior_hash" is carried as
#: `audit.signature.sha256_or_prior_hash`.
CARDINALITY_PROHIBITED_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "gen_ai.conversation.id",
        "session_user_tenant_ids",
        "idempotency_key",
        "mcp.primitive.signature.sha256",
        "skill.version_sha",
        "audit.signature.sha256_or_prior_hash",
    }
)


class CardinalityViolation(Exception):  # noqa: N818 — name is the U-OD-14 plan signature verbatim
    """Raised when an attribute is mis-used as a dashboard query dimension.

    The Python materialization of the plan's `Result<(), CardinalityViolation>`
    error branch: per the §0.8 error-type discipline the stack is Pydantic v2 +
    stdlib (no `Result` monad), so a `Result<(), E>` materializes as
    `-> None` on success and `raise CardinalityViolation` on the error branch
    (the landed-unit convention — see U-OD-23 `EmissionContractViolation`).
    """


def assert_cardinality_safe_for_dashboard_dimension(attr: str) -> None:
    """Gate `attr` for use as a dashboard query dimension (C-OD-11 §11.2).

    Returns (`None`) iff `attr` is in `CARDINALITY_SAFE_ATTRIBUTES`. Raises
    `CardinalityViolation` for any attribute not in the cardinality-safe set —
    including cardinality-prohibited attributes and unknown attributes
    (acceptance #5). Enforcement is at dashboard-query-construction time per
    the cell's committed backend (acceptance #7).
    """
    if attr not in CARDINALITY_SAFE_ATTRIBUTES:
        raise CardinalityViolation(
            f"attribute {attr!r} is not cardinality-safe (C-OD-11 §11.2); it "
            f"MUST NOT be used as a dashboard query dimension"
        )


def assert_cardinality_prohibited_not_in_dashboard_dimension(attr: str) -> None:
    """Gate `attr` against cardinality-prohibited dashboard use (C-OD-11 §11.3).

    Returns (`None`) iff `attr` is NOT in `CARDINALITY_PROHIBITED_ATTRIBUTES`.
    Raises `CardinalityViolation` for any attribute in the
    cardinality-prohibited set used as a dashboard dimension (acceptance #6) —
    those attributes MAY appear as span attributes for trace-level join keys
    but high-cardinality dashboard queries over them cause cardinality blowup
    (acceptance #4). Enforcement is at dashboard-query-construction time per
    the cell's committed backend (acceptance #7).
    """
    if attr in CARDINALITY_PROHIBITED_ATTRIBUTES:
        raise CardinalityViolation(
            f"attribute {attr!r} is cardinality-prohibited (C-OD-11 §11.3); it "
            f"MAY be a span attribute for trace-level joins but MUST NOT be a "
            f"dashboard query dimension"
        )
