"""Runtime-layer effective-palette computation (UNION-intersection per Reading B).

Implements runtime spec v1.22 §14.8.2 step 4d (Reading B landing) — UNION-
intersection composition of palette restrictions from two canonical CP-axis
surfaces:

- **C-CP-19 §19.4** ``deny``-row palette: when ``gate_level == DENY``, palette
  restricted to ``{REJECT, RESPOND}`` (removes ``APPROVE`` AND ``EDIT``).
- **C-CP-21 §21.3** cross-trust-boundary palette: when validator-escalation
  HITL composes with cross-family / local-terminal / untrusted-MCP active.

**CP-as-landed §21.3 divergence (Class 3 informational).** The CP-axis
``PALETTE_RESTRICTION_TABLE`` at ``harness_cp.validator_fail_transient_staircase``
maps cross-trust-active states to ``{REJECT, RESPOND}`` (matching CP plan
acc #5) — diverges from spec §21.3 narrative ``{APPROVE, REJECT, RESPOND}``
(edit-only removed). The runtime layer consumes the CP-as-landed restricted
palette via ``PALETTE_RESTRICTION_TABLE`` lookup — more restrictive than
spec-canonical at the ``gate_level == ASK AND cross-trust active`` case.
Full spec-canonical conformance is gated on CP plan reconciliation at
separate follow-on arc; documented at runtime plan v2.21 change-note
adjacent observation (CP plan acc #5 reading).

The 4-case truth-table at scoping doc §2 Q2 narrows under CP-as-landed:

==================  =====================  =====================
gate_level          cross_trust_state      effective palette
==================  =====================  =====================
DENY                NONE                   {REJECT, RESPOND}
DENY                CROSS_FAMILY_ACTIVE    {REJECT, RESPOND}
DENY                LOCAL_TERMINAL_ACTIVE  {REJECT, RESPOND}
DENY                UNTRUSTED_MCP_ACTIVE   {REJECT, RESPOND}
ASK                 NONE                   {APPROVE, EDIT, REJECT, RESPOND}
ASK                 CROSS_FAMILY_ACTIVE    {REJECT, RESPOND}
ASK                 LOCAL_TERMINAL_ACTIVE  {REJECT, RESPOND}
ASK                 UNTRUSTED_MCP_ACTIVE   {REJECT, RESPOND}
==================  =====================  =====================

When ``validator_escalation_brief`` is non-None, the result narrows further
via intersection with ``brief.proposed_response_palette`` per spec §14.15.4
invariant 4 ("final palette ≤ both constraints").
"""

from __future__ import annotations

from harness_cp.gate_level_rule import GateLevel
from harness_cp.hitl_response_palette import HITLResponse
from harness_cp.validator_fail_transient_staircase import (
    PALETTE_RESTRICTION_TABLE,
    CrossTrustBoundaryState,
)
from harness_cp.validator_framework_types import HITLEscalationBrief

__all__ = ["DENY_ROW_PALETTE", "FULL_PALETTE", "compute_effective_palette"]


FULL_PALETTE: frozenset[HITLResponse] = frozenset(
    {HITLResponse.APPROVE, HITLResponse.EDIT, HITLResponse.REJECT, HITLResponse.RESPOND}
)
"""The full 4-response palette per C-CP-16 §16.1. Default for non-restricted cases."""

DENY_ROW_PALETTE: frozenset[HITLResponse] = frozenset({HITLResponse.REJECT, HITLResponse.RESPOND})
"""C-CP-19 §19.4 deny-row restricted palette. Removes APPROVE and EDIT.

Per §19.4: when ``gate_level == DENY``, tool dispatch is structurally rejected
and the HITL palette is restricted to ``{REJECT, RESPOND}``.
"""

# CP-as-landed §21.3 palette restriction index — keyed by CrossTrustBoundaryState.
# `_PALETTE_INDEX` is module-private at CP-axis; this re-derives the lookup
# from the public `PALETTE_RESTRICTION_TABLE` tuple for runtime-layer access.
_CROSS_TRUST_PALETTE_INDEX: dict[CrossTrustBoundaryState, frozenset[HITLResponse]] = {
    r.cross_trust_state: r.restricted_palette for r in PALETTE_RESTRICTION_TABLE
}


def compute_effective_palette(
    gate_level: GateLevel,
    cross_trust_state: CrossTrustBoundaryState,
    validator_escalation_brief: HITLEscalationBrief | None,
) -> frozenset[HITLResponse]:
    """Compute the effective HITL response palette per UNION-intersection.

    Composes two canonical CP-axis palette-restriction surfaces in
    most-restrictive direction:

    1. **§19.4 deny-row**: when ``gate_level == DENY`` → restrict to
       ``DENY_ROW_PALETTE = {REJECT, RESPOND}``.
    2. **§21.3 cross-trust-boundary** (CP-as-landed via
       ``PALETTE_RESTRICTION_TABLE``): when ``cross_trust_state != NONE`` →
       intersect with the CP-as-landed restricted palette
       (``{REJECT, RESPOND}`` per CP plan acc #5).
    3. **Validator-proposed narrowing**: when ``validator_escalation_brief is
       not None`` → intersect with ``brief.proposed_response_palette``
       (default per CP spec v1.10 §25.2 is the full palette).

    Parameters
    ----------
    gate_level
        The computed gate level per C-CP-19 §19.1 4-axis composition. Only
        ``ASK`` and ``DENY`` reach this composer (``AUTO`` skips gate per
        step 4j).
    cross_trust_state
        The current cross-trust-boundary state per C-CP-21 §21.3 4-class
        enum (``NONE`` / ``CROSS_FAMILY_ACTIVE`` / ``LOCAL_TERMINAL_ACTIVE``
        / ``UNTRUSTED_MCP_ACTIVE``).
    validator_escalation_brief
        The ``HITLEscalationBrief`` typed payload when called from the §14.15
        mid-step re-entry composer; ``None`` when called from the §14.8
        wrap-time composer (validator-escalation context not in scope).

    Returns
    -------
    frozenset[HITLResponse]
        The effective palette — the intersection of all applicable
        restrictions. Spec §14.15.4 invariant 4: "final palette ≤ both
        constraints".
    """
    palette: frozenset[HITLResponse] = FULL_PALETTE

    # §19.4 deny-row narrowing.
    if gate_level == GateLevel.DENY:
        palette = palette & DENY_ROW_PALETTE

    # §21.3 cross-trust-boundary narrowing (CP-as-landed).
    cross_trust_palette = _CROSS_TRUST_PALETTE_INDEX.get(cross_trust_state)
    if cross_trust_palette is not None:
        palette = palette & cross_trust_palette

    # Validator-proposed narrowing (when called from §14.15 re-entry).
    if validator_escalation_brief is not None:
        palette = palette & validator_escalation_brief.proposed_response_palette

    return palette
