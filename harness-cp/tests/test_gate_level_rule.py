"""Tests for U-CP-43 — 4-axis multiplicative gate-level rule (C-CP-19 §19.1/§19.2/§19.4).

Acceptance-criterion coverage (v2.20 amendment per CP spec v1.15 §19.1.1):
  #1 GateLevel cardinality 3              -> test_gate_level_cardinality_three,
                                             test_gate_level_values_match_spec_19_1_16_2
  #2 max() over materialized floors       -> test_gate_level_max_composition_over_materialized_floors
  #3 BLAST_RADIUS floor §19.1 verbatim    -> test_blast_radius_floor_match_spec_19_1
  #4 PERSONA_TIER floor all three ASK     -> test_persona_tier_floor_all_three_ask
  #6 (v2.20 NEW) per_tool_gate_level axis -> test_per_tool_gate_level_axis_in_max_composition,
                                             test_per_tool_gate_level_degenerate_no_floor_table
  #7 cross-persona monotonicity §19.2     -> test_cross_persona_monotonicity
  #8 _hitl_required ask-or-deny §19.4     -> test_hitl_required_predicate_ask_or_deny
  #9 (v2.20 NEW) GateLevelInput field-set -> test_gate_level_input_no_deployment_surface_field
  #10 composition_winner attribution      -> test_composition_winner_attribution

Acc #5 (MCP_TRUST floor) MATERIALIZED at U-CP-98 / CP spec v1.35 §19.1.2 (the 4th,
last §19.1 axis; resolves the prior §0.8 row 2 PARTIAL-ADVANCE) -> the
test_u_cp_98_* block below (Table A per-tier floors + max() composition +
non-vacuous tier-distinctness + composition_winner + the load-bearing
override-cannot-bypass-an-untrusted-L0-server security test).
Acc #6 (DEPLOYMENT_SURFACE floor) RETIRED at v2.20 per CP spec v1.15 §19.1.1 (v)
non-axis statement. New per_tool_gate_level tests authored at v2.20 (NEW acc #6).
"""

from __future__ import annotations

import pytest
from harness_as import BlastRadiusTier
from harness_core import PersonaTier
from harness_cp.cp_shared_types import Axis, MCPTrustTier
from harness_cp.gate_level_rule import (
    BLAST_RADIUS_GATE_LEVEL_FLOOR,
    MCP_TRUST_GATE_LEVEL_FLOOR,
    PERSONA_TIER_GATE_LEVEL_FLOOR,
    GateLevel,
    GateLevelInput,
    assert_cross_persona_monotonicity,
    gate_level,
    hitl_required,
)


def _input(
    persona: PersonaTier = PersonaTier.SOLO_DEVELOPER,
    blast: BlastRadiusTier = BlastRadiusTier.READ_ONLY,
    per_tool: GateLevel = GateLevel.AUTO,
) -> GateLevelInput:
    return GateLevelInput(
        per_tool_gate_level=per_tool,
        persona_tier=persona,
        # U-CP-98: MCP_TRUST is now a composed axis. These tests isolate the
        # per_tool/blast/persona axes, so the helper feeds the L3 no-floor
        # default (→ AUTO, contributes nothing to max()) — the dedicated
        # MCP_TRUST tests below exercise the other tiers.
        blast_radius_tier=blast,
        mcp_trust_tier=MCPTrustTier.LEVEL_3_ALLOW_WITH_AUDIT,
    )


def test_gate_level_cardinality_three() -> None:
    """#1 — GateLevel declares exactly three values."""
    assert len(GateLevel) == 3


def test_gate_level_values_match_spec_19_1_16_2() -> None:
    """#1 — GateLevel values are {auto, ask, deny} per §19.1/§16.2 verbatim."""
    assert {m.value for m in GateLevel} == {"auto", "ask", "deny"}


def test_gate_level_max_composition_over_materialized_floors() -> None:
    """#2 (v2.20) — max() over the three materialized floors.

    PER_TOOL_GATE_LEVEL + BLAST_RADIUS + PERSONA_TIER floors compose by
    escalation rank. `external-irreversible` (ASK) + solo-developer (ASK) +
    per_tool AUTO -> ASK.
    """
    comp = gate_level(_input(PersonaTier.SOLO_DEVELOPER, BlastRadiusTier.EXTERNAL_IRREVERSIBLE))
    assert comp.computed_gate_level is GateLevel.ASK
    # read-only + solo + per_tool AUTO -> max(AUTO, AUTO, ASK) = ASK (persona wins).
    comp2 = gate_level(_input(PersonaTier.SOLO_DEVELOPER, BlastRadiusTier.READ_ONLY))
    assert comp2.computed_gate_level is GateLevel.ASK
    assert set(comp.per_axis_floors) == {
        Axis.PER_TOOL_GATE_LEVEL,
        Axis.BLAST_RADIUS,
        Axis.PERSONA_TIER,
        Axis.MCP_TRUST,
    }


def test_per_tool_gate_level_axis_in_max_composition() -> None:
    """#6 (v2.20 NEW) — per_tool_gate_level participates in max() composition.

    Per CP spec v1.15 §19.1.1 (i): per_tool_gate_level IS the gate-level value
    declared per-tool at C-AS-03 SKILL.md frontmatter; degenerate axis with no
    per-tier mapping; consumed directly at max().
    """
    # per_tool DENY dominates persona ASK + blast AUTO.
    comp = gate_level(_input(PersonaTier.SOLO_DEVELOPER, BlastRadiusTier.READ_ONLY, GateLevel.DENY))
    assert comp.computed_gate_level is GateLevel.DENY
    assert comp.composition_winner is Axis.PER_TOOL_GATE_LEVEL
    assert comp.per_axis_floors[Axis.PER_TOOL_GATE_LEVEL] is GateLevel.DENY


def test_per_tool_gate_level_degenerate_no_floor_table() -> None:
    """#6 (v2.20 NEW) — no PER_TOOL_GATE_LEVEL_FLOOR constant exists.

    per_tool_gate_level IS the GateLevel value (per CP spec v1.15 §19.1.1 (i)
    degenerate axis); no per-tier → gate-level mapping table needed.
    """
    import harness_cp.gate_level_rule as glr

    assert not hasattr(glr, "PER_TOOL_GATE_LEVEL_FLOOR")


def test_gate_level_input_no_deployment_surface_field() -> None:
    """#9 (v2.20 NEW) — GateLevelInput field-set conformed to spec-canonical 4-axis.

    Per CP spec v1.15 §19.1.1 (v) non-axis statement: deployment_surface is
    NOT a §19.1 D5-layer axis (belongs at §19.3 D2-layer sandbox composition
    only). v2.20 drops the field from GateLevelInput.
    """
    assert "deployment_surface" not in GateLevelInput.model_fields
    assert "per_tool_gate_level" in GateLevelInput.model_fields
    # U-CP-91 (F-B3-1 §19.5) ADDED the two optional floor-override fields
    # (persona_floor_override / blast_floor_override, default None) — the
    # in-`max()` operator-policy override carrier-shape. The 4 canonical axes
    # are unchanged; the overrides are optional + default-None (byte-identical
    # no-override path).
    assert set(GateLevelInput.model_fields) == {
        "per_tool_gate_level",
        "persona_tier",
        "blast_radius_tier",
        "mcp_trust_tier",
        "persona_floor_override",
        "blast_floor_override",
    }


def _base_input(**overrides: object) -> GateLevelInput:
    """A READ_ONLY/solo/auto-tool baseline GateLevelInput, with field overrides."""
    kwargs: dict[str, object] = {
        "per_tool_gate_level": GateLevel.AUTO,
        "persona_tier": PersonaTier.SOLO_DEVELOPER,
        "blast_radius_tier": BlastRadiusTier.READ_ONLY,
        # U-CP-98: the L3 no-floor default isolates the U-CP-91 override mechanism
        # under test (persona/blast). The dedicated security test below
        # (test_u_cp_98_override_cannot_bypass_untrusted_l0_server) exercises L0.
        "mcp_trust_tier": MCPTrustTier.LEVEL_3_ALLOW_WITH_AUDIT,
    }
    kwargs.update(overrides)
    return GateLevelInput(**kwargs)  # type: ignore[arg-type]


def test_u_cp_91_no_override_path_byte_identical() -> None:
    """U-CP-91 — default (no override) composes the canonical §19.1 table `max()`.

    The persona floor is all-ASK, so a default solo READ_ONLY gate is ASK →
    hitl_required True (the pre-U-CP-91 behavior, verbatim).
    """
    comp = gate_level(_base_input())
    assert comp.computed_gate_level is GateLevel.ASK
    assert hitl_required(_base_input()) is True


def test_u_cp_91_persona_floor_override_solo_read_only_skips() -> None:
    """U-CP-91 AC — a lowered `persona_tier_floor[SOLO]=AUTO` composes through
    `max()` to yield AUTO for a solo READ_ONLY action → `hitl_required` False
    (the smart-HITL conditional-skip; F-B3-1 default policy)."""
    inp = _base_input(persona_floor_override=GateLevel.AUTO)
    comp = gate_level(inp)
    assert comp.computed_gate_level is GateLevel.AUTO
    assert hitl_required(inp) is False


def test_u_cp_91_persona_override_does_not_lower_local_mutation() -> None:
    """U-CP-91 — overriding ONLY the persona floor leaves the blast floor at its
    table value: a LOCAL_MUTATION action still gates (ASK) under the persona-only
    override (the F-B3-1 default — LOCAL_MUTATION is opt-in via the blast override)."""
    inp = _base_input(
        blast_radius_tier=BlastRadiusTier.LOCAL_MUTATION,
        persona_floor_override=GateLevel.AUTO,
    )
    assert gate_level(inp).computed_gate_level is GateLevel.ASK


def test_u_cp_91_blast_floor_override_local_mutation_skips() -> None:
    """U-CP-91 — with BOTH the persona override AND the blast[LOCAL_MUTATION]
    override (the F-B3-1 LOCAL_MUTATION opt-in), a solo LOCAL_MUTATION action
    composes AUTO → skips."""
    inp = _base_input(
        blast_radius_tier=BlastRadiusTier.LOCAL_MUTATION,
        persona_floor_override=GateLevel.AUTO,
        blast_floor_override=GateLevel.AUTO,
    )
    assert gate_level(inp).computed_gate_level is GateLevel.AUTO


def test_u_cp_91_override_never_lowers_per_tool_deny() -> None:
    """U-CP-91 — the override only touches persona/blast; a `deny`-tier tool still
    composes DENY regardless of the persona/blast overrides (per_tool not
    override-able — the AC's never-lower-a-non-targeted-axis invariant)."""
    inp = _base_input(
        per_tool_gate_level=GateLevel.DENY,
        persona_floor_override=GateLevel.AUTO,
        blast_floor_override=GateLevel.AUTO,
    )
    assert gate_level(inp).computed_gate_level is GateLevel.DENY


def test_blast_radius_floor_match_spec_19_1() -> None:
    """#3 — BLAST_RADIUS_GATE_LEVEL_FLOOR matches §19.1 blast_radius_floor verbatim."""
    assert BLAST_RADIUS_GATE_LEVEL_FLOOR == {
        BlastRadiusTier.READ_ONLY: GateLevel.AUTO,
        BlastRadiusTier.LOCAL_MUTATION: GateLevel.ASK,
        BlastRadiusTier.EXTERNAL_REVERSIBLE: GateLevel.ASK,
        BlastRadiusTier.EXTERNAL_IRREVERSIBLE: GateLevel.ASK,
    }


def test_persona_tier_floor_all_three_ask() -> None:
    """#4 — all three persona tiers map to ASK per §19.1 persona_tier_floor."""
    assert set(PERSONA_TIER_GATE_LEVEL_FLOOR.values()) == {GateLevel.ASK}
    assert set(PERSONA_TIER_GATE_LEVEL_FLOOR) == set(PersonaTier)


def test_cross_persona_monotonicity() -> None:
    """#7 — persona-tier ascends monotonically; descent is structurally prohibited."""
    # Ascending / equal transitions are admissible.
    assert_cross_persona_monotonicity(PersonaTier.SOLO_DEVELOPER, PersonaTier.TEAM_BINDING)
    assert_cross_persona_monotonicity(PersonaTier.TEAM_BINDING, PersonaTier.MULTI_TENANT_COMPLIANCE)
    assert_cross_persona_monotonicity(PersonaTier.TEAM_BINDING, PersonaTier.TEAM_BINDING)
    # A descent raises (the §19.2 manifest-validation error).
    with pytest.raises(ValueError, match="monotonicity violated"):
        assert_cross_persona_monotonicity(
            PersonaTier.MULTI_TENANT_COMPLIANCE, PersonaTier.SOLO_DEVELOPER
        )


def test_hitl_required_predicate_ask_or_deny() -> None:
    """#8 — _hitl_required true iff computed_gate_level ∈ {ASK, DENY} per §19.4."""
    # Every materialized input produces ASK (both floors are AUTO/ASK) -> HITL.
    assert hitl_required(_input()) is True
    assert (
        hitl_required(
            _input(PersonaTier.MULTI_TENANT_COMPLIANCE, BlastRadiusTier.EXTERNAL_IRREVERSIBLE)
        )
        is True
    )


def test_composition_winner_attribution() -> None:
    """#10 — composition_winner identifies which axis set the winning floor."""
    comp = gate_level(_input(PersonaTier.SOLO_DEVELOPER, BlastRadiusTier.READ_ONLY))
    # read-only blast floor is AUTO, persona floor is ASK -> persona wins.
    assert comp.composition_winner is Axis.PERSONA_TIER
    assert comp.computed_gate_level is GateLevel.ASK


def test_gate_level_monotonic_ordering() -> None:
    """#1 — escalation ordering AUTO < ASK < DENY is preserved by the rule."""
    # AUTO < ASK < DENY escalation: read-only+solo -> ASK (persona ASK wins);
    # the rule never emits a level below the highest materialized floor.
    comp = gate_level(_input(PersonaTier.SOLO_DEVELOPER, BlastRadiusTier.READ_ONLY))
    assert comp.computed_gate_level is GateLevel.ASK
    assert comp.per_axis_floors[Axis.BLAST_RADIUS] is GateLevel.AUTO


# ---------------------------------------------------------------------------
# U-CP-98 — MCP_TRUST 4th-axis composition (CP spec v1.35 §19.1.2, Table A).
# ---------------------------------------------------------------------------


def test_u_cp_98_mcp_trust_floor_table_matches_spec_19_1_2() -> None:
    """§19.1.2 — MCP_TRUST_GATE_LEVEL_FLOOR is the operator-ratified Table A verbatim."""
    assert MCP_TRUST_GATE_LEVEL_FLOOR == {
        MCPTrustTier.LEVEL_0_REFUSE_REMOTE: GateLevel.DENY,
        MCPTrustTier.LEVEL_1_SIGNED_PINNED: GateLevel.ASK,
        MCPTrustTier.LEVEL_2_SANDBOX_ALL: GateLevel.ASK,
        MCPTrustTier.LEVEL_3_ALLOW_WITH_AUDIT: GateLevel.AUTO,
    }


def test_u_cp_98_each_tier_composes_its_table_a_floor() -> None:
    """§19.1.2 — every MCPTrustTier composes its Table A floor at `gate_level()`.

    The composed `per_axis_floors[Axis.MCP_TRUST]` equals `Table A[tier]` for all
    four tiers (read through the real `max()` composition, not just the table).
    """
    for tier, expected_floor in MCP_TRUST_GATE_LEVEL_FLOOR.items():
        comp = gate_level(_base_input(mcp_trust_tier=tier))
        assert comp.per_axis_floors[Axis.MCP_TRUST] is expected_floor


def test_u_cp_98_mcp_trust_axis_in_max_composition() -> None:
    """§19.1.2 — an untrusted (L0→DENY) server is the strict `max()` winner.

    With per_tool AUTO + blast READ_ONLY (AUTO) + persona ASK, an L0 server's DENY
    floor is the sole DENY → `computed_gate_level` is DENY and `composition_winner`
    names `Axis.MCP_TRUST`.
    """
    comp = gate_level(_base_input(mcp_trust_tier=MCPTrustTier.LEVEL_0_REFUSE_REMOTE))
    assert comp.computed_gate_level is GateLevel.DENY
    assert comp.composition_winner is Axis.MCP_TRUST


def test_u_cp_98_mcp_trust_non_vacuous_tier_distinctness() -> None:
    """§19.1.2 — the axis carries real signal (mirror #481 TEAM≠neighbours).

    SAME blast/persona/per_tool inputs compose a DIFFERENT gate at `mcp_trust=L0`
    (DENY) vs `=L3` (the persona-decided ASK) — proving MCP_TRUST is not a uniform
    pass-through table.
    """
    untrusted = gate_level(_base_input(mcp_trust_tier=MCPTrustTier.LEVEL_0_REFUSE_REMOTE))
    trusted = gate_level(_base_input(mcp_trust_tier=MCPTrustTier.LEVEL_3_ALLOW_WITH_AUDIT))
    assert untrusted.computed_gate_level is GateLevel.DENY
    assert trusted.computed_gate_level is GateLevel.ASK
    assert untrusted.computed_gate_level is not trusted.computed_gate_level


def test_u_cp_98_l3_trusted_server_adds_no_floor() -> None:
    """§19.1.2 invariant 3 — an L3 (allow-with-audit) server maps to AUTO (rank 0),
    contributing nothing to `max()`; the gate is the persona-decided ASK (identical
    to the pre-U-CP-98 3-axis path). This is the host-less-gate no-floor default."""
    comp = gate_level(_base_input(mcp_trust_tier=MCPTrustTier.LEVEL_3_ALLOW_WITH_AUDIT))
    assert comp.per_axis_floors[Axis.MCP_TRUST] is GateLevel.AUTO
    assert comp.computed_gate_level is GateLevel.ASK
    assert comp.composition_winner is Axis.PERSONA_TIER


def test_u_cp_98_override_cannot_bypass_untrusted_l0_server() -> None:
    """§19.1.2 regression (the LOAD-BEARING security property) — the U-CP-91
    operator-policy override lowers persona+blast to AUTO at solo, but CANNOT
    bypass an untrusted (L0) MCP server: `mcp_trust` is never floor-override-able,
    so its DENY composes verbatim → the gate is DENY (winner MCP_TRUST).

    Sibling of `test_u_cp_91_override_never_lowers_per_tool_deny` — the untrusted
    server is the MCP-trust analog of the `deny`-tier tool.
    """
    inp = _base_input(
        mcp_trust_tier=MCPTrustTier.LEVEL_0_REFUSE_REMOTE,
        persona_floor_override=GateLevel.AUTO,
        blast_floor_override=GateLevel.AUTO,
    )
    comp = gate_level(inp)
    assert comp.computed_gate_level is GateLevel.DENY
    assert comp.composition_winner is Axis.MCP_TRUST
    assert hitl_required(inp) is True
