"""The 9 CP-owned structured shared types — U-CP-00c (foundational carrier).

Declares the nine structured shared types deferred at CP plan v2.7 and
specified at v2.8 §2.0c, each as a faithful factor-out of its committing
contract per the operator-ratified T2 X-AL-3 FACTOR-OUT resolution
(`.harness/xal3_resolution_recommendations.md`):

- ``ActorIdentity`` — CP-owned identity `str` newtype (operator decision D9).
  Nominally distinct from the IS-exported F2 ``Actor`` — no import of, and no
  structural reconciliation with, the IS type. (C-CP-13 §13.5 + carrier-map.)
- ``AgentRole`` — open-string newtype (DECIDED kind, v2.8 §0.3.1). The CP spec
  uses ``agent_role`` only as a manifest-lookup key; it commits no closed
  agent-role value set. (C-CP-01 §1.3 + C-CP-13 §13.)
- ``ModelBinding`` — `(provider, model)` routing-binding record. (ADR-F1 v1.2
  §Decision + C-CP-01 §1.4 + C-CP-13 §13.3.)
- ``ProviderAgnosticPayload`` — provider-neutral `(messages, tools, params)`
  3-tuple record; sub-shapes are opaque mappings. (ADR-F1 v1.2 §Decision +
  C-CP-01 §1.1.)
- ``RoutingDecisionTrace`` — routing-decision trace record; ``layer`` typed
  `str` so this carrier depends on nothing. (C-CP-01 §1.4 + C-CP-02 §2.1;
  byte-exact with the U-CP-05 v2.1 unit-body record, re-homed per D7.)
- ``TraceContext`` — W3C Trace Context record. (C-CP-14 §14.1 +
  Target_Stack_Commitment C-STK-09.)
- ``MCPTrustTier`` — 4-level MCP server trust-tier enum; CP-axis re-declaration
  of the value set AS C-AS-10 §10.3 enumerates. (Spec_Action_Surface_v1.md
  C-AS-10 §10.3.)
- ``Axis`` — 5-value gate-level `max()` axis enum. (C-CP-19 §19.1 + §19.3.)
- ``TailKeepPredicate`` — opaque predicate-callable alias; the span argument is
  typed `Any` so this carrier carries no OD-axis dependency. (C-CP-21 §21.3.)

All nine reside in `harness-cp` and are exposed at the CP-axis package surface.
None is genuinely cross-axis shared (v2.8 §0.4): no type is re-homed to
`harness-core`.

Authority: Implementation_Plan_Control_Plane_v2_8.md §2.0c U-CP-00c;
`.harness/xal3_resolution_recommendations.md` (T2 X-AL-3 FACTOR-OUT
resolution); Spec_Control_Plane_v1_2.md C-CP-01 §1.1/§1.3/§1.4, C-CP-02 §2.1,
C-CP-13 §13.3/§13.5, C-CP-14 §14.1, C-CP-19 §19.1/§19.3, C-CP-21 §21.3
(preserved verbatim into v1.3); Spec_Action_Surface_v1.md C-AS-10 §10.3;
ADR-F1 v1.2 §Decision.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import Any, NewType

from pydantic import BaseModel, ConfigDict

# --- Identity ---------------------------------------------------------------

ActorIdentity = NewType("ActorIdentity", str)
"""CP-owned identity alias (operator decision D9 / Q-R4-7).

Distinct from the IS-exported F2 ``Actor`` (carrier-map "ActorIdentity vs IS
Actor"): no import of, and no structural reconciliation with, the IS type.
`str`-typed per C-CP-13 §13.5 ``LedgerEntryRef.actor`` ("parent actor
identity")."""

AgentRole = NewType("AgentRole", str)
"""Open-string newtype — DECIDED kind (v2.8 §0.3.1).

The CP spec uses ``agent_role`` only as a manifest-lookup key (C-CP-01 §1.3;
C-CP-02 §2.1 ``Lookup manifest entry by (agent_role, workflow_class, step)``);
it commits NO closed agent-role set. An enum would invent an uncommitted value
set (X-AL-3). The faithful factor-out is a string newtype."""


# --- Provider / routing -----------------------------------------------------


class ModelBinding(BaseModel):
    """A `(provider, model)` routing binding.

    Per ADR-F1 v1.2 §Decision capability-aware abstraction + C-CP-13 §13.3
    lead-agent model binding. The `(provider, model)` pair is the C-CP-01 §1.4
    routing-binding vocabulary. No field beyond the pair (acc #3)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    """Provider identity; cf. C-CP-01 §1.4 ``routing.provider``."""

    model: str
    """Model identifier within provider; cf. C-CP-01 §1.4 ``routing.model``."""


class ProviderAgnosticPayload(BaseModel):
    """A provider-neutral inference payload.

    Per ADR-F1 v1.2 §Decision provider-neutral thin core + C-CP-01 §1.1 (the
    generate/stream/tool_use ``(messages, tools, params)`` 3-tuple). Sub-shapes
    are opaque mappings — C-CP-01 §1.4 defers the provider-adapter binding
    library to implementation discretion; the opaque-mapping factor-out is
    faithful (acc #5). No provider-specific field is lifted into the record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    messages: tuple[Mapping[str, Any], ...]
    """Provider-neutral message list."""

    tools: tuple[Mapping[str, Any], ...] | None
    """Optional tool definitions."""

    params: Mapping[str, Any]
    """Generation params."""


class RoutingDecisionTrace(BaseModel):
    """A routing-decision trace record.

    Re-homed from the U-CP-05 v2.1 unit body per operator decision D7
    (Q-R4-5) — dissolves the U-CP-03 -> U-CP-05 level inversion. ``layer`` is
    typed `str` (not U-CP-05's ``RoutingLayer`` enum) so this carrier carries
    no dependency on U-CP-05; the `str` domain is the C-CP-01 §1.4
    ``routing.layer`` vocabulary. Exactly four fields (acc #6)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    layer: str
    """Routing layer; C-CP-01 §1.4 ``routing.layer`` vocabulary."""

    candidate: str
    """The selected ``"provider:model"`` tuple."""

    decision_ms: int
    """Wall-clock decision latency in milliseconds."""

    budget_exhausted: bool
    """Whether the layer budget was exhausted at decision time."""


class RouterResolution(BaseModel):
    """The Layer-3 router's resolution result — C-CP-02 §2.5.1.

    Returned by an injected ``RouterResolutionFn`` when the deterministic
    routing layers fall through and Layer 3 LLM_AS_ROUTER resolves at the
    already-async ``infer`` call surface (Reading B). The ``rationale`` is
    returned SEPARATELY from the ``candidate`` because the frozen four-field
    ``RoutingDecisionTrace`` cannot carry it (§2.5.4) — the trace is NOT
    widened; the rationale rides the additive optional ``binding_rationale``
    channel on the span-owning dispatch seam to the C-CP-01 §1.4
    ``routing.binding_rationale`` span attribute. Exactly two fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate: str
    """The selected ``"provider:model"`` tuple (well-formed)."""

    rationale: str
    """Short rationale token(s); the §2.1 ``router_rationale_summary``."""


# --- Observability ----------------------------------------------------------


class TraceContext(BaseModel):
    """A W3C Trace Context record.

    W3C Trace Context standard shape (Target_Stack_Commitment C-STK-09 OTel
    adoption) + C-CP-14 §14.1 ("child span; trace_id propagated;
    parent_span_id = ..."). Exactly four fields (acc #4)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    trace_id: str
    """W3C trace-id."""

    span_id: str
    """W3C span-id."""

    trace_flags: int
    """W3C trace-flags."""

    trace_state: str | None
    """W3C tracestate."""


TailKeepPredicate = Callable[[Any], bool]
"""A predicate over a span, evaluated at tail-sampling time.

The span argument is typed opaque (``Any``) — the ``Span`` handle is OD-axis-
owned observability substrate; typing it here would create a
U-CP-00c -> OD cross-axis dependency on a foundational L0 unit. U-CP-51
type-checks the ``Span`` argument at its own site against the OD span type.
Per C-CP-21 §21.3 operator-burden eval + tail-keep rules (acc #9)."""


# --- Gate-level composition -------------------------------------------------


class MCPTrustTier(StrEnum):
    """The 4-level MCP server trust-tier framework.

    Closed at cardinality 4 (acc #7). Byte-exact factor-out of the 4-level MCP
    server trust-tier framework enumerated at Spec_Action_Surface_v1.md
    C-AS-10 §10.3. A CP-axis re-declaration of the AS-owned value set — CP
    consumes it at gate-level composition (v2.8 §0.4)."""

    LEVEL_0_REFUSE_REMOTE = "level-0-refuse-remote"
    """Refuse-remote — REFUSE at registration."""

    LEVEL_1_SIGNED_PINNED = "level-1-signed-pinned"
    """Signed-pinned — signature + version pin."""

    LEVEL_2_SANDBOX_ALL = "level-2-sandbox-all"
    """Sandbox-all — tier-4-full-vm with egress allow-list."""

    LEVEL_3_ALLOW_WITH_AUDIT = "level-3-allow-with-audit"
    """Allow-with-audit — audit-ledger entry per fetch/call."""


class Axis(StrEnum):
    """The 5 gate-level ``max()`` composition axes.

    Closed at cardinality 5 (acc #8). Factor-out of the gate-level ``max()``
    axis set: the four D5-layer axes at C-CP-19 §19.1 + the fifth
    (``sandbox_tier``) at the D2 5-axis specialization C-CP-19 §19.3."""

    PER_TOOL_GATE_LEVEL = "per-tool-gate-level"
    """C-CP-19 §19.1 — C4 contract input."""

    BLAST_RADIUS = "blast-radius"
    """C-CP-19 §19.1 — C10 four-tier blast-radius floor."""

    MCP_TRUST = "mcp-trust"
    """C-CP-19 §19.1 — C10 five-tier per-MCP-server trust floor."""

    PERSONA_TIER = "persona-tier"
    """C-CP-19 §19.1 — D5 persona-tier floor."""

    SANDBOX_TIER = "sandbox-tier"
    """C-CP-19 §19.3 — D2 5-axis specialization (added axis)."""
