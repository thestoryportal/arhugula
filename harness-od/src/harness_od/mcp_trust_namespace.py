"""C-OD-31 `mcp.trust.*` canonical namespace schema + TrustEvaluationAuditPayload.

U-OD-52 — 4-OD-E canonical schemas cluster. Declares the 5-attribute
`mcp.trust.*` span namespace canonical authority for the
`PerServerTrustEvaluator` emitter homed at CP (per the D6 ingestion pattern:
CP emits, OD ratifies). Also declares the `TrustEvaluationAuditPayload`
field-set used by the `cp_audit_to_od_audit` converter at
`harness-cxa/src/harness_cxa/cp_audit_conversion.py` when the converter
encounters an `mcp_trust:`-prefixed CP action_id (per CXA v2.6 §0.3 +
U-CP-72 §1 AC #1 discriminator-table extension to 8 prefixes).

**5 attributes on the `mcp.trust.evaluate` span site** per OD spec v1.8
§C-OD-31.1:

| Attribute                      | Type   | Cardinality |
|--------------------------------|--------|-------------|
| `mcp.trust.server_name`        | string | medium      |
| `mcp.trust.primitive_kind`     | enum   | bounded (4) |
| `mcp.trust.decision_reason`    | enum   | bounded (6) |
| `mcp.trust.audit_required`     | bool   | binary      |
| `mcp.trust.tier_evaluated`     | enum   | bounded (4) |

**Pattern-P1 alignment** with CP spec v1.10 §27.4 producer-side: attribute
names byte-exact match the §27.4 span emission table; consumers MAY
disambiguate `UNKNOWN_SERVER_*` decisions via `decision_reason` per §31.1 +
the §27.6 invariant 4 audit-required-always rule.

**Audit-ledger projection** per §C-OD-31.2: when an `mcp.trust.evaluate`
result has `audit_required=true`, the converter writes a
`TrustEvaluationAuditPayload` via `mcp_trust:` action_id prefix per CXA v2.6
§0.3 + U-CP-72 expansion. `audit_required` carries True redundantly per AC #4
for query convenience (every row written iff audit_required at emit-time).

**Sampling discipline.** `mcp.trust.evaluate` head=1.0 if `audit_required=true`;
head=0.1 otherwise. UNKNOWN_SERVER decisions ALWAYS audit-required (Decision
3.D1) so always head=1.0 sampled.

Authority: OD spec v1.8 §C-OD-31 (NEW at Closure Arc Phase A.5); plan unit
U-OD-52 (OD plan v2.14 §1 cluster 4-OD-E, preserved at v2.15).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from harness_core import AttributeValueType, Cardinality
from pydantic import BaseModel, ConfigDict

# ----------------------------------------------------------------------------
# Span-site identifier (1 site per §C-OD-31.1)
# ----------------------------------------------------------------------------

SPAN_SITE_MCP_TRUST_EVALUATE: Final[str] = "mcp.trust.evaluate"


# ----------------------------------------------------------------------------
# AttributeSpec carrier (mirrors U-OD-50 validator_namespace.py shape)
# ----------------------------------------------------------------------------


class AttributeSpec(BaseModel):
    """One canonical-namespace span attribute declaration.

    Pattern-P1 alignment carrier — consumers verify byte-exact attribute name
    + value type + cardinality + span site against the OD canonical schema.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    attribute_name: str
    """Byte-exact attribute name per §C-OD-31.1 + Pattern-P1 alignment with
    CP spec v1.10 §27.4 producer site."""

    value_type: AttributeValueType
    """Value-type discriminator per `harness_core.AttributeValueType`."""

    cardinality: Cardinality
    """Cardinality classification per `harness_core.Cardinality`."""

    span_site: str
    """`SPAN_SITE_MCP_TRUST_EVALUATE` — only 1 span site per §C-OD-31.1."""


# ----------------------------------------------------------------------------
# 5-attribute canonical schema (§C-OD-31.1 verbatim)
# ----------------------------------------------------------------------------


MCP_TRUST_SPAN_NAMESPACE_SCHEMA: Mapping[str, AttributeSpec] = {
    "mcp.trust.server_name": AttributeSpec(
        attribute_name="mcp.trust.server_name",
        value_type=AttributeValueType.STRING,
        cardinality=Cardinality.MEDIUM,
        span_site=SPAN_SITE_MCP_TRUST_EVALUATE,
    ),
    "mcp.trust.primitive_kind": AttributeSpec(
        attribute_name="mcp.trust.primitive_kind",
        value_type=AttributeValueType.ENUM_REF,
        cardinality=Cardinality.LOW,
        span_site=SPAN_SITE_MCP_TRUST_EVALUATE,
    ),
    "mcp.trust.decision_reason": AttributeSpec(
        attribute_name="mcp.trust.decision_reason",
        value_type=AttributeValueType.ENUM_REF,
        cardinality=Cardinality.LOW,
        span_site=SPAN_SITE_MCP_TRUST_EVALUATE,
    ),
    "mcp.trust.audit_required": AttributeSpec(
        attribute_name="mcp.trust.audit_required",
        value_type=AttributeValueType.BOOL,
        cardinality=Cardinality.LOW,
        span_site=SPAN_SITE_MCP_TRUST_EVALUATE,
    ),
    "mcp.trust.tier_evaluated": AttributeSpec(
        attribute_name="mcp.trust.tier_evaluated",
        value_type=AttributeValueType.ENUM_REF,
        cardinality=Cardinality.LOW,
        span_site=SPAN_SITE_MCP_TRUST_EVALUATE,
    ),
}
"""The 5 `mcp.trust.*` span attributes per §C-OD-31.1 verbatim.

Keyed by attribute name for O(1) Pattern-P1 alignment lookup at the
`cp_audit_to_od_audit` converter + at consumer-side downstream filtering.
"""


# ----------------------------------------------------------------------------
# TrustEvaluationAuditPayload (§C-OD-31.2 audit-ledger projection)
# ----------------------------------------------------------------------------


class TrustEvaluationAuditPayload(BaseModel):
    """Audit-ledger projection emitted when `audit_required=true` on an
    `mcp.trust.evaluate` result (§C-OD-31.2).

    Written by `cp_audit_to_od_audit` converter at
    `harness-cxa/src/harness_cxa/cp_audit_conversion.py` via `mcp_trust:`
    action_id prefix per CXA v2.6 §0.3 + U-CP-72 expansion (8 prefixes).

    Extends the C-OD-24.6 CP-sourced sub-namespace discipline: the 4
    `audit_cp_*` fields are the common CP-sourced field-set; the 5 trailing
    fields are mcp-trust-specific. At serialization the payload composes into
    `AuditPayload.audit_namespace_attrs` as `audit.cp.*` + `audit.mcp_trust.*`
    sub-namespace keys.

    Note: per the U-OD-50 `ValidatorEscalationAuditPayload` precedent, this
    class is a STANDALONE projection container that the converter uses to
    compose `AuditPayload.audit_namespace_attrs` dict — literal Python
    `class Foo(AuditPayload)` inheritance is NOT what the spec requires; the
    §24.6 sub-namespace tagging discipline is what's preserved.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    # CP-sourced inherited per §C-OD-24.6 sub-namespace discipline:
    audit_cp_action_id: str
    """f"mcp_trust:{server_name}:{primitive_kind.value}" per §C-OD-31.2 +
    CXA v2.6 §0.3 + U-CP-72 expansion."""

    audit_cp_response: str
    """`"permitted"` | `"denied"` per §C-OD-31.2."""

    audit_cp_timestamp: str
    """ISO-8601 OR "" at MVP per v1.7 §24.4 NOTE 8a-iii."""

    audit_cp_prior_event_hash: str
    """SHA-256 hex (64) OR "0"*64 at MVP."""

    # MCP-trust-specific fields per §C-OD-31.2:
    server_name: str
    """The MCP server name evaluated."""

    primitive_kind: str
    """MCPPrimitive enum value (per CP spec v1.10 §27.2)."""

    decision_reason: str
    """TrustDecisionReason enum value (6-class per CP spec v1.10 §27.2)."""

    audit_required: bool
    """Always True when audit row written; redundant carry for query convenience
    per §C-OD-31.2 + AC #4."""

    tier_evaluated: str
    """MCPTrustTier enum value (per CP plan v2.8 U-CP-00c)."""
