"""4-axis multiplicative gate-level rule + monotonicity + `_hitl_required` — U-CP-43.

Implements C-CP-19 §19.1 (the multiplicative gate-level `max()` rule), §19.2
(cross-persona-tier monotonicity), and §19.4 (the `_hitl_required` predicate).

**4-of-4 axes materialized (U-CP-98 / R-FS-1 B2-impl-3).** The
`MCP_TRUST_GATE_LEVEL_FLOOR` per-tier → gate-level mapping — the CP plan v2.20 §0.8
row 2 PARTIAL-ADVANCE spec-silence — is now RESOLVED at CP spec **v1.35 §19.1.2**
(operator-ratified Table A: `level-0`→`DENY` / `level-1`→`ASK` / `level-2`→`ASK` /
`level-3`→`AUTO`; floor-only/monotone). `gate_level()` composes all four §19.1 axes.
The real per-server trust feed (resolved owning MCP host → `mcp_trust_tier`) targets
a tool-step gate site that does not yet exist — the forward `B-TOOL-GATE` arc; the
only gate sites at HEAD are host-less (inference + sub-agent), which feed the L3
no-floor default (runtime U-RT-131).

Per the v2.20 (B2) plan-follows-spec disposition (operator AskUserQuestion
2026-05-24 + CP spec v1.15 §19.1.1 publication), the v2.4-era `deployment_surface`
axis is DROPPED — it belongs at §19.3 D2-layer sandbox composition only, NOT
§19.1 D5-layer HITL composition. The `per_tool_gate_level` axis is ADDED — it
IS the gate-level value declared per-tool at C-AS-03 SKILL.md frontmatter /
MCP server manifest (degenerate axis: no per-tier mapping; consumed directly).

The materializable surface AS LANDED: the §19.1/§16.2-conformed `GateLevel`
3-value enum, the `BLAST_RADIUS` + `PERSONA_TIER` §19.1-verbatim floors, the
`per_tool_gate_level` direct-input axis (v2.20), the `MCP_TRUST` floor (v1.35
§19.1.2 / U-CP-98), the `gate_level` multiplicative `max()` rule over all four
axes, cross-persona-tier monotonicity (§19.2), and the `_hitl_required`
predicate (§19.4).

Authority: Implementation_Plan_Control_Plane_v2_20.md §1 U-CP-43 canonical-reading
amendment (v2.20 — `GateLevelInput` field-set conformed to spec-canonical
4-axis per CP spec v1.15 §19.1.1.1: add `per_tool_gate_level`, drop
`deployment_surface`); Spec_Control_Plane_v1_15.md §19.1.1 NEW canonical 4-axis
statement + Spec_Control_Plane_v1_2.md §19.1 (preserved-by-reference)
composition formula + §19.4 `_hitl_required` predicate.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum

from harness_as import BlastRadiusTier
from harness_core import PersonaTier
from pydantic import BaseModel, ConfigDict

from harness_cp.cp_shared_types import Axis, MCPTrustTier


class GateLevel(StrEnum):
    """The 3-value gate-level domain (C-CP-19 §19.1 + §16.2, verbatim).

    The v2.4 amendment conformed this enum to the CP spec §19.1 `gate_level`
    value domain (`{auto, ask, deny}`) — the v2.1/v2.3 enum carried the
    divergent 4-value ladder. Escalation-monotonic per §19.1/§19.4:
    `AUTO < ASK < DENY` (`auto` → no HITL; `ask` → HITL rewrite; `deny` →
    structural rejection + HITL). Closed at cardinality 3 (acceptance #1).
    """

    AUTO = "auto"
    ASK = "ask"
    DENY = "deny"


class _GateRank(IntEnum):
    """Monotonic escalation rank for `GateLevel` `max()` composition (§19.1)."""

    AUTO = 0
    ASK = 1
    DENY = 2


_RANK: dict[GateLevel, _GateRank] = {
    GateLevel.AUTO: _GateRank.AUTO,
    GateLevel.ASK: _GateRank.ASK,
    GateLevel.DENY: _GateRank.DENY,
}
_BY_RANK: dict[_GateRank, GateLevel] = {v: k for k, v in _RANK.items()}


class GateLevelInput(BaseModel):
    """The 4-axis input set for the gate-level rule (U-CP-43, v2.20 conformed).

    Conformed to spec-canonical 4-axis per CP spec v1.15 §19.1.1.1: `{
    per_tool_gate_level, blast_radius_tier, persona_tier, mcp_trust_tier }`.
    The `deployment_surface` axis was DROPPED at v2.20 — `deployment_surface`
    belongs at §19.3 D2-layer sandbox composition only, NOT §19.1 D5-layer
    HITL composition (CP spec v1.15 §19.1.1 (v) non-axis statement). The
    `per_tool_gate_level` axis was ADDED at v2.20 — it IS the gate-level value
    declared per-tool at C-AS-03 SKILL.md frontmatter / MCP server manifest
    (degenerate per CP spec v1.15 §19.1.1 (i): no per-tier mapping; consumed
    directly at `max()`).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    per_tool_gate_level: GateLevel
    """Spec-canonical axis (v2.20 ADDED per CP spec v1.15 §19.1.1 (i)). The
    per-tool gate-level value declared per-tool at C-AS-03 SKILL.md frontmatter
    / MCP server manifest `tier` field (`{auto, ask, deny}`). Degenerate axis:
    no per-tier mapping table; consumed directly at `gate_level()` `max()`."""

    persona_tier: PersonaTier
    blast_radius_tier: BlastRadiusTier

    mcp_trust_tier: MCPTrustTier
    """Spec-canonical axis (the 4th §19.1 axis). CONSUMED by `gate_level()` via
    `MCP_TRUST_GATE_LEVEL_FLOOR` (CP spec v1.35 §19.1.2 / U-CP-98 — resolves the
    prior §0.8 row 2 PARTIAL-ADVANCE spec-silence). AS spec C-AS-10 §10.3 4-level
    enumeration is canonical at AS-side (cardinality narrative reconciled at CP
    spec v1.14); the per-tier → gate-level mapping is now the operator-ratified
    Table A (floor-only/monotone). Never floor-override-able. The field type is
    the landed U-CP-00c `MCPTrustTier` enum."""

    persona_floor_override: GateLevel | None = None
    """U-CP-91 (F-B3-1 §19.5 in-`max()` floor-override; runtime spec §3.8). When
    non-`None`, REPLACES the `PERSONA_TIER_GATE_LEVEL_FLOOR` table lookup for the
    `persona_tier` axis in this single evaluation. The runtime HITL gate composer
    (U-RT-116) sets it to `AUTO` when the operator's `hitl_auto_approve_policy`
    lowers `persona_tier_floor[SOLO_DEVELOPER]` — the composer owns the
    solo-scoping + the per-tier gating + the only-`AUTO`/only-blessed-cell
    discipline (§19.5: solo permitted, team restricted, multi prohibited). Default
    `None` = the canonical table floor (existing callers byte-unaffected). This is
    the F-B3-1 §3.2 plan-carrier shape (NOT a C-CP-19 spec change); the override is
    applied as a floor cell WITHIN the existing `max()` (Reading C, in-`max()`)."""

    blast_floor_override: GateLevel | None = None
    """U-CP-91 (F-B3-1 §19.5). When non-`None`, REPLACES the
    `BLAST_RADIUS_GATE_LEVEL_FLOOR` table lookup for the `blast_radius_tier` axis.
    The composer (U-RT-116) sets it to `AUTO` when the policy lowers
    `blast_radius_floor[LOCAL_MUTATION]` at solo (operator opt-in). Default `None`
    = the canonical table floor. **Only the persona + blast floors are
    override-able** — `per_tool_gate_level` + `mcp_trust_tier` are NEVER overridden
    (a `deny`-tier tool / untrusted MCP server still composes its floor verbatim)."""


class GateLevelComputation(BaseModel):
    """The result of a gate-level computation (C-CP-19 §19.1).

    `per_axis_floors` carries all four materialized axes (`PER_TOOL_GATE_LEVEL`,
    `BLAST_RADIUS`, `PERSONA_TIER`, `MCP_TRUST`) — the `MCP_TRUST` floor landed at
    U-CP-98 per CP spec v1.35 §19.1.2 (operator-ratified Table A; resolves the
    prior §0.8 row 2 PARTIAL-ADVANCE). `composition_winner`
    identifies which axis set the winning floor — retained as an internal field
    with no downstream audit sink at v2.4 (the `audit.gate.*` namespace was
    dissolved at v2.4 U-CP-46).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    inputs: GateLevelInput
    per_axis_floors: dict[Axis, GateLevel]
    composition_winner: Axis
    computed_gate_level: GateLevel


# --- §19.1 conformed floors (the two materialized axes) ---------------------

BLAST_RADIUS_GATE_LEVEL_FLOOR: dict[BlastRadiusTier, GateLevel] = {
    BlastRadiusTier.READ_ONLY: GateLevel.AUTO,
    BlastRadiusTier.LOCAL_MUTATION: GateLevel.ASK,
    BlastRadiusTier.EXTERNAL_REVERSIBLE: GateLevel.ASK,
    BlastRadiusTier.EXTERNAL_IRREVERSIBLE: GateLevel.ASK,
}
"""C-CP-19 §19.1 `blast_radius_floor` block, verbatim (acceptance #3).

`read-only` → `AUTO`; `local-mutation` → `ASK` (configurable to `AUTO` at
solo-developer per §19.1); `external-reversible` → `ASK`;
`external-irreversible` → `ASK` (with dual-control at multi-tenant-compliance
per §19.1).
"""

PERSONA_TIER_GATE_LEVEL_FLOOR: dict[PersonaTier, GateLevel] = {
    PersonaTier.SOLO_DEVELOPER: GateLevel.ASK,
    PersonaTier.TEAM_BINDING: GateLevel.ASK,
    PersonaTier.MULTI_TENANT_COMPLIANCE: GateLevel.ASK,
}
"""C-CP-19 §19.1 `persona_tier_floor` block, verbatim (acceptance #4).

All three persona tiers map to `ASK` per §19.1: `solo-developer` (operator may
override to `AUTO` for non-irreversible), `team-binding` (audit ledger
required), `multi-tenant-compliance` (audit ledger + cryptographic signature;
dual-control on external-irreversible). The v2.1/v2.3 acc #3 divergent ladder
(`GATE_NONE` / `GATE_NOTIFY` / `GATE_APPROVE`) was conformed away at v2.4.
"""

MCP_TRUST_GATE_LEVEL_FLOOR: dict[MCPTrustTier, GateLevel] = {
    MCPTrustTier.LEVEL_0_REFUSE_REMOTE: GateLevel.DENY,
    MCPTrustTier.LEVEL_1_SIGNED_PINNED: GateLevel.ASK,
    MCPTrustTier.LEVEL_2_SANDBOX_ALL: GateLevel.ASK,
    MCPTrustTier.LEVEL_3_ALLOW_WITH_AUDIT: GateLevel.AUTO,
}
"""C-CP-19 §19.1.2 `MCP_TRUST_GATE_LEVEL_FLOOR` table — operator-ratified Table A
(R-FS-1 B2-spec-2; resolves the v1.15 §19.1.1.1 row-3 spec-silence — the 4th and
last §19.1 HITL-gate axis).

`level-0-refuse-remote` → `DENY`; `level-1-signed-pinned` → `ASK`;
`level-2-sandbox-all` → `ASK`; `level-3-allow-with-audit` → `AUTO`. **Floor-only /
monotone** (§19.1.2 invariant 3): the gate is `max()` over escalation rank, so a
floor can only RAISE the gate; the `level-3` cell (`AUTO`, rank 0) contributes
nothing — it is the no-floor value a host-less gate site legitimately composes (the
analog of `per_tool_gate_level=AUTO`). The `mcp_trust` axis is NEVER
floor-override-able (the U-CP-91 below "never overridden" commitment is honored;
an untrusted `level-0` server composes `DENY` verbatim regardless of any
persona/blast override).
"""


def gate_level(input: GateLevelInput) -> GateLevelComputation:
    """Compute the gate level — `max()` over the materialized per-axis floors.

    C-CP-19 §19.1 composition rule: `max()` over the per-axis floors.
    Deterministic given inputs. **v1.35 §19.1.2 — 4-axis materialized (U-CP-98):**
    the `max()` ranges over all four §19.1 axes (`PER_TOOL_GATE_LEVEL` direct,
    `BLAST_RADIUS` floor, `PERSONA_TIER` floor, `MCP_TRUST` floor via
    `MCP_TRUST_GATE_LEVEL_FLOOR`). `mcp_trust` is never floor-override-able; at the
    host-less gate sites the runtime composer evaluates it feeds the L3 no-floor
    default (U-RT-131), so it adds no floor there (the real per-server `L0→DENY`
    arrives at the forward `B-TOOL-GATE` tool-step gate site).
    """
    per_tool_floor = input.per_tool_gate_level  # degenerate — IS the value
    # U-CP-91 (F-B3-1 §19.5): the composer-supplied floor-override REPLACES the
    # table lookup for the persona/blast cells when present (the in-`max()`
    # operator-policy override). Default None → the canonical §19.1 table floor
    # (byte-identical to the pre-U-CP-91 path). per_tool + mcp_trust are never
    # override-able (no override field). The composer (U-RT-116) is the sole
    # producer and sets only `AUTO`, only at the §19.1-annotated solo cells.
    blast_floor = (
        input.blast_floor_override
        if input.blast_floor_override is not None
        else BLAST_RADIUS_GATE_LEVEL_FLOOR[input.blast_radius_tier]
    )
    persona_floor = (
        input.persona_floor_override
        if input.persona_floor_override is not None
        else PERSONA_TIER_GATE_LEVEL_FLOOR[input.persona_tier]
    )
    per_axis_floors: dict[Axis, GateLevel] = {
        Axis.PER_TOOL_GATE_LEVEL: per_tool_floor,
        Axis.BLAST_RADIUS: blast_floor,
        Axis.PERSONA_TIER: persona_floor,
        # §19.1.2 (U-CP-98): the 4th, last §19.1 axis. Floor-only; never
        # override-able (per_tool + mcp_trust carry no `*_floor_override`). The
        # host-less gate sites the runtime composer evaluates feed the L3 no-floor
        # default (U-RT-131) → AUTO contributes nothing; the real per-server
        # `L0→DENY` arrives at the forward `B-TOOL-GATE` tool-step gate site.
        Axis.MCP_TRUST: MCP_TRUST_GATE_LEVEL_FLOOR[input.mcp_trust_tier],
    }
    # max() over the materialized floors by escalation rank.
    winner_axis = max(per_axis_floors.items(), key=lambda kv: _RANK[kv[1]])[0]
    computed = _BY_RANK[max(_RANK[lv] for lv in per_axis_floors.values())]
    return GateLevelComputation(
        inputs=input,
        per_axis_floors=per_axis_floors,
        composition_winner=winner_axis,
        computed_gate_level=computed,
    )


def hitl_required(input: GateLevelInput) -> bool:
    """Return `true` iff `computed_gate_level ∈ {ASK, DENY}` (C-CP-19 §19.4).

    The CP plan signature names this predicate `_hitl_required`; `_hitl_required`
    is exported below as a module-level alias for spec-name fidelity. Consumed
    at the U-CP-39 rewriting algorithm.
    """
    return gate_level(input).computed_gate_level in (GateLevel.ASK, GateLevel.DENY)


_hitl_required = hitl_required
"""Spec-name alias — CP plan U-CP-43 signature names the predicate
`_hitl_required` (C-CP-19 §19.4). Public callers use `hitl_required`."""


def assert_cross_persona_monotonicity(from_tier: PersonaTier, to_tier: PersonaTier) -> None:
    """Enforce §19.2 cross-persona-tier monotonicity.

    Under bridging-arc traversal across persona tiers, `persona_tier_floor`
    ascends monotonically (never descends). A tier downgrade is structurally
    prohibited — it raises `ValueError` (the manifest-validation error per
    §19.2). The persona-tier order is `solo-developer < team-binding <
    multi-tenant-compliance`.
    """
    order: dict[PersonaTier, int] = {
        PersonaTier.SOLO_DEVELOPER: 0,
        PersonaTier.TEAM_BINDING: 1,
        PersonaTier.MULTI_TENANT_COMPLIANCE: 2,
    }
    if order[to_tier] < order[from_tier]:
        raise ValueError(
            f"cross-persona-tier monotonicity violated (§19.2): "
            f"{from_tier.value} -> {to_tier.value} descends the persona-tier axis"
        )
