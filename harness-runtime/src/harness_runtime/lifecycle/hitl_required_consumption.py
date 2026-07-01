"""Runtime-layer consumption surface for the 4-axis `_hitl_required` evaluation.

Implements runtime spec v1.22 §14.8.2 step 4c (Reading B landing) — the full
4-axis `_hitl_required` consumption per C-CP-19 §19.1 multiplicative `max()`
composition rule + §19.4 runtime evaluation surface (returns True when
`gate_level ∈ {ASK, DENY}`).

**CP-as-landed consumption shape (Class 3 informational divergence from
spec-canonical 4-axis).** The CP-axis carrier surface at
`harness_cp.gate_level_rule` lands `GateLevelInput` with fields
`{persona_tier, blast_radius_tier, deployment_surface, mcp_trust_tier}` per
the U-CP-43 plan-signature shape — diverges from runtime spec v1.22's
spec-canonical 4-axis `{per_tool_gate_level, blast_radius, server_trust_tier,
persona_tier}`. Per CP plan v2.4 §0.8 carried items, only 2 of 4 floors
materialize at CP-axis: `BLAST_RADIUS_GATE_LEVEL_FLOOR` and
`PERSONA_TIER_GATE_LEVEL_FLOOR`. The runtime layer's `evaluate_hitl_required`
function consumes the CP-as-landed shape (thin wrapper around
`harness_cp.gate_level_rule.hitl_required`) — degraded 2-axis composition
relative to spec-canonical 4-axis. Full spec-canonical conformance is gated
on CP plan §0.8 carry resolution at separate follow-on arc; documented at
runtime plan v2.21 change-note adjacent observation (a).

The function below is a one-line re-export with type-level documentation.
"""

from __future__ import annotations

from harness_cp.gate_level_rule import GateLevelInput, hitl_required

__all__ = ["evaluate_hitl_required"]


def evaluate_hitl_required(input: GateLevelInput) -> bool:
    """Evaluate whether HITL is required for the given gate-level input.

    Returns ``True`` iff ``gate_level(input).computed_gate_level ∈ {ASK, DENY}``
    per C-CP-19 §19.4 runtime evaluation surface. Thin wrapper around the
    CP-axis ``hitl_required`` function — runtime layer re-export for
    spec-amendment discoverability + composer integration site.

    Parameters
    ----------
    input
        The 4-axis input carrier per CP-as-landed shape. Only
        ``persona_tier`` and ``blast_radius_tier`` axes materialize floors
        at CP-as-landed (per CP plan v2.4 §0.8 §0.8 carry).

    Returns
    -------
    bool
        ``True`` when the gate fires; ``False`` for ``GateLevel.AUTO``.
    """
    return hitl_required(input)
