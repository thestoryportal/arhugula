"""13-entry base-rate-sampled set + per-cell tuning envelope — U-OD-12.

Implements C-OD-10 §10.1 (base-rate-sampled set), §10.2 (tail-keep-on-
classification), §10.3 (per-cell base-rate tuning envelope).

`BASE_RATE_SAMPLED_EVENT_CLASSES` carries the §10.1 13-entry base-rate-sampled
set. `PerCellBaseRateEnvelope` carries one cell's base-rate default + min/max
envelope; `PER_CELL_BASE_RATE_ENVELOPE` declares one per ACTIVE cell per the
§10.3 table. `TailKeepRule` / `TAIL_KEEP_RULES` declare the §10.2 tail-keep-on-
classification post-classification keep decisions.

v2.8 (D-4): acc #2 (cross-set disjointness) is re-scoped. The spec §9.2 / §10.1
tables split `files.operation` and `memory.operation` by the `kind` attribute
(mutation → always-sampled per §9.2; non-mutation → base-rate per §10.1), so at
the declared `frozenset[str]` granularity both bare strings are members of both
regimes. The disjointness criterion is honest over `(event_class, kind)` pairs:
for every event class other than `files.operation` / `memory.operation`,
membership is in exactly one regime; the two dual-regime classes are documented,
and their regime is resolved by `kind` at the `sampling_decision` call site.
No signature change; nothing landed re-opened (U-OD-11 is not in defect).

Authority: Implementation_Plan_Operational_Discipline_v2_8.md §3.4.2 U-OD-12
(v2.8 D-4 revision — acc #2 re-scoped; all other surfaces preserved verbatim
from v2.5); Spec_Operational_Discipline_v1_2.md §10 C-OD-10 §10.1 + §10.2 +
§10.3 + §9.2 (preserved verbatim into v1.3 per v1.3 §0.1); ADR-D6 v1.1 §1.3.

Depends on: [U-OD-01, U-OD-11] — `CellID` / `ACTIVE_CELLS` from
`observability_matrix`; `ALWAYS_SAMPLED_EVENT_CLASSES` from `sampling_mode`.
"""

from __future__ import annotations

from harness_core import DeploymentSurface, PersonaTier
from pydantic import BaseModel, ConfigDict

from harness_od.observability_matrix import ACTIVE_CELLS, CellID

__all__ = [
    "BASE_RATE_SAMPLED_EVENT_CLASSES",
    "DUAL_REGIME_EVENT_CLASSES",
    "PER_CELL_BASE_RATE_ENVELOPE",
    "TAIL_KEEP_RULES",
    "PerCellBaseRateEnvelope",
    "TailKeepRule",
]


#: §10.1 verbatim — the base-rate-sampled set; exactly 13 entries (acc #1).
#: `files.operation` / `memory.operation` are the two dual-regime classes (see
#: `DUAL_REGIME_EVENT_CLASSES`): in the base-rate set at non-mutation `kind`,
#: in `ALWAYS_SAMPLED_EVENT_CLASSES` at mutation `kind`.
BASE_RATE_SAMPLED_EVENT_CLASSES: frozenset[str] = frozenset(
    {
        "chat",  # §10.1 — gen_ai.operation.name=chat
        "execute_tool",
        "sandbox.enter",
        "sandbox.exit",
        "tool.call",  # §10.1 — non-MCP tool calls only
        "retrieval",  # §10.1 — gen_ai.operation.name=retrieval
        "cache.events",  # §10.1 row "cache events (cache hit / cache miss / cache creation)"
        "embeddings",
        "text_completion",
        "files.operation",  # §10.1 — kind in {list, metadata, reference} (non-mutation)
        "memory.operation",  # §10.1 — kind in {read, list} (non-mutation)
        "lease.acquired_released",  # §10.1 row "lease.acquired / lease.released"
        "retry.attempt.first",  # §10.1 row "retry.attempt at 1st attempt"
    }
)  # exactly 13 entries per §10.1


#: The two dual-regime event classes (v2.8 D-4). The spec §9.2 / §10.1 tables
#: place these in *both* regimes; the regime is resolved by the `kind`
#: attribute at the `sampling_decision` call site (mutation → always-sampled;
#: non-mutation → base-rate). They are members of both `frozenset[str]`
#: constants by design — the bare-string set model cannot carry the `kind`
#: discriminator, so the disjointness criterion (acc #2) applies only where
#: `kind` does not discriminate.
DUAL_REGIME_EVENT_CLASSES: frozenset[str] = frozenset({"files.operation", "memory.operation"})


class PerCellBaseRateEnvelope(BaseModel):
    """One ACTIVE cell's base-rate default + tuning envelope (C-OD-10 §10.3).

    Frozen → `Eq`. `default_rate` is the §10.3 per-cell default;
    `min_rate` / `max_rate` bound the operator-tunable envelope. The envelope
    invariant `min_rate <= default_rate <= max_rate` holds per cell (acc #4).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: the canonical cell key (U-OD-01).
    cell_id: CellID
    #: the §10.3 per-cell base-rate default.
    default_rate: float
    #: the §10.3 envelope lower bound.
    min_rate: float
    #: the §10.3 envelope upper bound.
    max_rate: float


class TailKeepRule(BaseModel):
    """A §10.2 tail-keep-on-classification post-classification keep decision.

    Frozen → `Eq`. `classification_attribute` names the classification trigger;
    `keep_decision` is always `ALWAYS_KEEP` per §10.2 — failed traces are
    preserved at tail-based-prod cells regardless of base-rate.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: the classification trigger attribute (§10.2).
    classification_attribute: str
    #: the keep decision — always `ALWAYS_KEEP` per §10.2.
    keep_decision: str = "ALWAYS_KEEP"


def _cell(pt: PersonaTier, ds: DeploymentSurface) -> CellID:
    """Construct the canonical `CellID` for `(pt, ds)`."""
    return CellID(persona_tier=pt, deployment_surface=ds)


def _envelope(
    pt: PersonaTier,
    ds: DeploymentSurface,
    *,
    default_rate: float,
    min_rate: float,
    max_rate: float,
) -> tuple[CellID, PerCellBaseRateEnvelope]:
    """Build a `(CellID, PerCellBaseRateEnvelope)` entry per the §10.3 table."""
    cell = _cell(pt, ds)
    return cell, PerCellBaseRateEnvelope(
        cell_id=cell,
        default_rate=default_rate,
        min_rate=min_rate,
        max_rate=max_rate,
    )


_SOLO = PersonaTier.SOLO_DEVELOPER
_TEAM = PersonaTier.TEAM_BINDING
_MTC = PersonaTier.MULTI_TENANT_COMPLIANCE
_LOCAL = DeploymentSurface.LOCAL_DEVELOPMENT
_SELF = DeploymentSurface.SELF_HOSTED_SERVER
_CLOUD = DeploymentSurface.MANAGED_CLOUD


#: Per-cell base-rate tuning envelope — exactly 8 entries, one per ACTIVE cell
#: (C-OD-10 §10.3). Defaults + envelopes transcribed from the §10.3 table:
#: solo-developer cells default 1.0 (operator-tunable, full envelope);
#: team-binding cells 0.5 / 0.1 / 0.1; multi-tenant-compliance cells 0.2.
PER_CELL_BASE_RATE_ENVELOPE: dict[CellID, PerCellBaseRateEnvelope] = dict(
    [
        _envelope(_SOLO, _LOCAL, default_rate=1.0, min_rate=0.0, max_rate=1.0),
        _envelope(_SOLO, _SELF, default_rate=1.0, min_rate=0.0, max_rate=1.0),
        _envelope(_SOLO, _CLOUD, default_rate=1.0, min_rate=0.0, max_rate=1.0),
        _envelope(_TEAM, _LOCAL, default_rate=0.5, min_rate=0.1, max_rate=1.0),
        _envelope(_TEAM, _SELF, default_rate=0.1, min_rate=0.05, max_rate=0.5),
        _envelope(_TEAM, _CLOUD, default_rate=0.1, min_rate=0.05, max_rate=0.5),
        _envelope(_MTC, _SELF, default_rate=0.2, min_rate=0.1, max_rate=0.5),
        _envelope(_MTC, _CLOUD, default_rate=0.2, min_rate=0.1, max_rate=0.5),
    ]
)


#: §10.2 tail-keep-on-classification rules — failed traces
#: (`validator.fail.permanent` / sandbox violations / breaker trips) are
#: ALWAYS_KEEP at tail-based-prod cells regardless of base-rate (acc #6).
TAIL_KEEP_RULES: tuple[TailKeepRule, ...] = (
    TailKeepRule(classification_attribute="validator.fail.permanent"),
    TailKeepRule(classification_attribute="sandbox.violation"),
    TailKeepRule(classification_attribute="breaker.tripped"),
)


# Sanity-pin at import: the envelope must cover exactly the 8 ACTIVE cells, and
# the §10.3 envelope invariant `min <= default <= max` must hold per cell.
assert set(PER_CELL_BASE_RATE_ENVELOPE) == set(ACTIVE_CELLS), (
    "PER_CELL_BASE_RATE_ENVELOPE must cover exactly the 8 ACTIVE cells"
)
for _entry in PER_CELL_BASE_RATE_ENVELOPE.values():
    assert _entry.min_rate <= _entry.default_rate <= _entry.max_rate, (
        f"§10.3 envelope invariant violated at {_entry.cell_id}"
    )
