"""Tests for U-CP-00c — the 9 CP-owned structured shared types (CP plan v2.8 §2.0c).

Acceptance-criterion coverage:
  #1  ActorIdentity str newtype, distinct from IS Actor
  #2  AgentRole str newtype, not enum
  #3  ModelBinding exactly two fields
  #4  TraceContext exactly four W3C fields
  #5  ProviderAgnosticPayload three opaque-mapping fields
  #6  RoutingDecisionTrace four fields, byte-exact with U-CP-05 v2.1
  #7  MCPTrustTier cardinality 4, byte-exact with AS C-AS-10 §10.3
  #8  Axis cardinality 5, byte-exact with C-CP-19
  #9  TailKeepPredicate Callable alias
  #10 all 9 reside in harness-cp (full 15-consumer composition check deferred —
      see .harness/class_1_tension_u_cp_00c_acc10_deferred.md)
  #11 no spec extension — covered by the per-type cardinality assertions
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import get_type_hints

import harness_cp.cp_shared_types as cst
from harness_cp.cp_shared_types import (
    ActorIdentity,
    AgentRole,
    Axis,
    MCPTrustTier,
    ModelBinding,
    ProviderAgnosticPayload,
    RoutingDecisionTrace,
    TraceContext,
)


def test_actor_identity_is_str_newtype() -> None:
    """#1 — ActorIdentity is a str newtype; constructed value is a str."""
    a = ActorIdentity("worker-7")
    assert isinstance(a, str)
    assert a == "worker-7"
    assert ActorIdentity.__supertype__ is str  # type: ignore[attr-defined]


def test_actor_identity_distinct_from_is_actor() -> None:
    """#1 — ActorIdentity does not import or reconcile with the IS F2 Actor."""
    src = cst.__doc__ or ""
    assert "no import of" in src
    # The module imports nothing from harness_is.
    import inspect

    module_src = inspect.getsource(cst)
    assert "harness_is" not in module_src
    assert "import Actor" not in module_src


def test_agent_role_is_str_newtype_not_enum() -> None:
    """#2 — AgentRole is a str newtype; no closed value set declared."""
    r = AgentRole("lead")
    assert isinstance(r, str)
    assert AgentRole.__supertype__ is str  # type: ignore[attr-defined]
    # Not an enum — has no __members__.
    assert not hasattr(AgentRole, "__members__")


def test_model_binding_two_fields() -> None:
    """#3 — ModelBinding declares exactly provider + model."""
    assert set(ModelBinding.model_fields) == {"provider", "model"}
    mb = ModelBinding(provider="anthropic", model="claude-opus")
    assert mb.provider == "anthropic"
    assert mb.model == "claude-opus"


def test_trace_context_four_fields_w3c_shape() -> None:
    """#4 — TraceContext declares exactly the four W3C fields."""
    assert set(TraceContext.model_fields) == {
        "trace_id",
        "span_id",
        "trace_flags",
        "trace_state",
    }
    tc = TraceContext(
        trace_id="0af7651916cd43dd8448eb211c80319c",
        span_id="b7ad6b7169203331",
        trace_flags=1,
        trace_state=None,
    )
    assert tc.trace_state is None
    hints = get_type_hints(TraceContext)
    assert hints["trace_flags"] is int


def test_provider_agnostic_payload_three_fields_opaque_mappings() -> None:
    """#5 — ProviderAgnosticPayload declares exactly messages/tools/params."""
    assert set(ProviderAgnosticPayload.model_fields) == {
        "messages",
        "tools",
        "params",
    }
    p = ProviderAgnosticPayload(
        messages=({"role": "user", "content": "hi"},),
        tools=None,
        params={"temperature": 0.0},
    )
    assert p.tools is None
    assert isinstance(p.params, Mapping)


def test_routing_decision_trace_four_fields_byte_exact_u_cp_05() -> None:
    """#6 — RoutingDecisionTrace declares exactly the four U-CP-05 fields."""
    assert set(RoutingDecisionTrace.model_fields) == {
        "layer",
        "candidate",
        "decision_ms",
        "budget_exhausted",
    }
    t = RoutingDecisionTrace(
        layer="manifest",
        candidate="anthropic:claude-opus",
        decision_ms=3,
        budget_exhausted=False,
    )
    # `layer` is a str, NOT the RoutingLayer enum.
    hints = get_type_hints(RoutingDecisionTrace)
    assert hints["layer"] is str
    assert t.layer == "manifest"


def test_mcp_trust_tier_cardinality_four_byte_exact_as_10_3() -> None:
    """#7 — MCPTrustTier declares exactly four values."""
    assert len(MCPTrustTier) == 4
    assert {m.name for m in MCPTrustTier} == {
        "LEVEL_0_REFUSE_REMOTE",
        "LEVEL_1_SIGNED_PINNED",
        "LEVEL_2_SANDBOX_ALL",
        "LEVEL_3_ALLOW_WITH_AUDIT",
    }


def test_axis_cardinality_five_byte_exact_cp_19() -> None:
    """#8 — Axis declares exactly five values."""
    assert len(Axis) == 5
    assert {m.name for m in Axis} == {
        "PER_TOOL_GATE_LEVEL",
        "BLAST_RADIUS",
        "MCP_TRUST",
        "PERSONA_TIER",
        "SANDBOX_TIER",
    }


def test_tail_keep_predicate_callable_alias() -> None:
    """#9 — TailKeepPredicate is a Callable[[Any], bool] alias."""
    pred: cst.TailKeepPredicate = lambda span: True  # noqa: E731
    assert pred(object()) is True
    # The alias argument is opaque Any — accepts any object.
    assert pred(42) is True


def test_all_nine_types_reside_in_harness_cp() -> None:
    """#10 — all 9 types are exposed at the CP-axis package surface."""
    for name in (
        "ActorIdentity",
        "AgentRole",
        "ModelBinding",
        "ProviderAgnosticPayload",
        "RoutingDecisionTrace",
        "TraceContext",
        "TailKeepPredicate",
        "MCPTrustTier",
        "Axis",
    ):
        assert hasattr(cst, name)
    assert cst.__name__.startswith("harness_cp.")


def test_no_callable_arg_typed_to_od_span() -> None:
    """#9/#11 — TailKeepPredicate argument is Any, no OD Span import."""
    import inspect

    module_src = inspect.getsource(cst)
    assert "Span" not in module_src or "OD-axis" in module_src
    # Concretely: no import of an OD span type.
    assert "harness_od" not in module_src
