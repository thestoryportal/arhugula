"""C-OD-09 + C-OD-10 HarnessCompositeSampler tests.

Closes H_T-OD-3 retirement gate "project-authored composite head/tail
sampler subclass" at the SDK boundary. Tests verify §9.2 always-sampled
discipline (literals + dot-anchored prefixes) honored regardless of
trace_id ratio, base-rate gating for non-always-sampled events, and
ParentBased propagation through the canonical wrapping pattern.
"""

from __future__ import annotations

import pytest
from harness_od.composite_sampler import (
    HarnessCompositeSampler,
    build_default_sampler,
)
from harness_od.sampling_mode import (
    ALWAYS_SAMPLED_EVENT_CLASSES,
    FILES_OPERATION_KIND_ATTR,
    MEMORY_OPERATION_KIND_ATTR,
    is_always_sampled,
)
from opentelemetry import trace as ot_trace
from opentelemetry.sdk.trace.sampling import Decision, ParentBased
from opentelemetry.trace import SpanContext, SpanKind, TraceFlags
from opentelemetry.trace.span import NonRecordingSpan

# Concrete span names exercising each §9.2 row.
_LITERAL_ALWAYS_SAMPLED = (
    "sandbox.violation",
    "sandbox.tier_escalation",
    "hitl.gate.evaluated",
    "hitl.invocation.opened",
    "hitl.invocation.responded",
    "hitl.invocation.timed_out",
    "fallback.triggered",
    "breaker.tripped",
    "topology.fanout.opened",
    "topology.fanout.closed",
    "subagent.span",
    "mcp.tool.call",
    "files.operation",
    "memory.operation",
    "managed_agents.runtime",
    "skill.activation",
)


# ---------------------------------------------------------------------------
# §9.2 substrate — is_always_sampled literal + prefix resolution.
# ---------------------------------------------------------------------------


def test_canonical_set_carries_18_entries_per_spec_9_2() -> None:
    assert len(ALWAYS_SAMPLED_EVENT_CLASSES) == 18


@pytest.mark.parametrize("name", _LITERAL_ALWAYS_SAMPLED)
def test_is_always_sampled_matches_literal_entries(name: str) -> None:
    assert is_always_sampled(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "audit.signature.write",
        "audit.cp.dispatch",
        "audit.entry",
        "validator.fail.semantic_inconsistency",
        "validator.fail.permanence",
    ],
)
def test_is_always_sampled_matches_dot_anchored_prefixes(name: str) -> None:
    assert is_always_sampled(name) is True


def test_is_always_sampled_dot_anchor_forecloses_bare_prefix_collision() -> None:
    # "audit" alone (no dot) must not be a prefix match — the dot anchor in
    # `audit.` enforces sub-namespace structure per spec §9.2 prose.
    assert is_always_sampled("audit") is False
    assert is_always_sampled("auditor") is False
    assert is_always_sampled("validator.fail") is False
    assert is_always_sampled("validator.failed") is False


@pytest.mark.parametrize(
    "name", ["chat", "execute_tool", "sandbox.enter", "sandbox.exit", "tool.call"]
)
def test_is_always_sampled_rejects_base_rate_set_members(name: str) -> None:
    assert is_always_sampled(name) is False


# ---------------------------------------------------------------------------
# HarnessCompositeSampler — root sampling decision at SDK boundary.
# ---------------------------------------------------------------------------


def _result_records(decision: Decision) -> bool:
    return decision == Decision.RECORD_AND_SAMPLE


@pytest.mark.parametrize("name", _LITERAL_ALWAYS_SAMPLED)
def test_always_sampled_literal_at_base_rate_zero_still_samples(name: str) -> None:
    """§9.2 floor inviolable: always-sampled members sample regardless of
    base_rate; even base_rate=0.0 cannot suppress them."""
    sampler = HarnessCompositeSampler(base_rate=0.0)
    result = sampler.should_sample(
        parent_context=None,
        trace_id=0x12345678901234567890123456789012,
        name=name,
    )
    assert _result_records(result.decision)


def test_always_sampled_prefix_at_base_rate_zero_still_samples() -> None:
    sampler = HarnessCompositeSampler(base_rate=0.0)
    for name in ("audit.signature.write", "validator.fail.semantic_inconsistency"):
        result = sampler.should_sample(
            parent_context=None,
            trace_id=0x12345678901234567890123456789012,
            name=name,
        )
        assert _result_records(result.decision), f"failed at {name}"


def test_base_rate_one_samples_non_always_sampled_event() -> None:
    sampler = HarnessCompositeSampler(base_rate=1.0)
    result = sampler.should_sample(
        parent_context=None,
        trace_id=0x12345678901234567890123456789012,
        name="chat",
    )
    assert _result_records(result.decision)


def test_base_rate_zero_drops_non_always_sampled_event() -> None:
    sampler = HarnessCompositeSampler(base_rate=0.0)
    result = sampler.should_sample(
        parent_context=None,
        trace_id=0x12345678901234567890123456789012,
        name="chat",
    )
    assert not _result_records(result.decision)


def test_base_rate_validation_rejects_out_of_range() -> None:
    with pytest.raises(ValueError, match="base_rate must be in"):
        HarnessCompositeSampler(base_rate=-0.1)
    with pytest.raises(ValueError, match="base_rate must be in"):
        HarnessCompositeSampler(base_rate=1.5)


def test_base_rate_property_surfaces_constructor_value() -> None:
    assert HarnessCompositeSampler(base_rate=0.25).base_rate == 0.25


def test_get_description_surfaces_spec_citation() -> None:
    desc = HarnessCompositeSampler(base_rate=1.0).get_description()
    assert "C-OD-09" in desc
    assert "1.0" in desc


# ---------------------------------------------------------------------------
# build_default_sampler — ParentBased wrapping per OTel canonical pattern.
# ---------------------------------------------------------------------------


def test_build_default_sampler_returns_parent_based_wrapper() -> None:
    sampler = build_default_sampler()
    assert isinstance(sampler, ParentBased)


def test_parent_based_root_decision_at_always_sampled_name() -> None:
    sampler = build_default_sampler(base_rate=0.0)
    result = sampler.should_sample(
        parent_context=None,
        trace_id=0x12345678901234567890123456789012,
        name="sandbox.violation",
        kind=SpanKind.INTERNAL,
    )
    assert _result_records(result.decision)


def test_parent_based_inherits_sampled_parent_decision() -> None:
    """OTel ParentBased canonical contract: child of a sampled parent samples
    regardless of the inner root sampler's decision."""
    sampler = build_default_sampler(base_rate=0.0)
    parent_context = SpanContext(
        trace_id=0x12345678901234567890123456789012,
        span_id=0x1234567890123456,
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    ctx = ot_trace.set_span_in_context(NonRecordingSpan(parent_context))
    result = sampler.should_sample(
        parent_context=ctx,
        trace_id=parent_context.trace_id,
        name="chat",  # would not sample at base_rate=0.0 at root
        kind=SpanKind.INTERNAL,
    )
    assert _result_records(result.decision)


def test_parent_based_inherits_unsampled_parent_decision() -> None:
    """OTel ParentBased canonical contract: child of an unsampled parent does
    not sample even if the name would always-sample at root."""
    sampler = build_default_sampler(base_rate=1.0)
    parent_context = SpanContext(
        trace_id=0x12345678901234567890123456789012,
        span_id=0x1234567890123456,
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.DEFAULT),  # not sampled
    )
    ctx = ot_trace.set_span_in_context(NonRecordingSpan(parent_context))
    result = sampler.should_sample(
        parent_context=ctx,
        trace_id=parent_context.trace_id,
        name="sandbox.violation",  # always-sampled at root, but parent says no
        kind=SpanKind.INTERNAL,
    )
    assert not _result_records(result.decision)


# ---------------------------------------------------------------------------
# B7 §9.2 conditional rows — attribute-aware decision at the root sampler.
# ---------------------------------------------------------------------------
#
# Exercised at a ROOT span (parent_context=None) with base_rate=0.0 so the
# §10.1 base-rate branch DROPS — a span samples here iff §9.2 always-samples
# it. Attributes are supplied explicitly (as a producer passing them at span
# creation would); the production enforcement boundary (head sampler is
# consulted only for root spans; producers set *.kind post-creation) is
# documented in the `composite_sampler` module docstring.


@pytest.mark.parametrize(
    ("name", "attributes", "should_record"),
    [
        # files.operation — §9.2 mutation always-samples; §10.1 complement drops.
        ("files.operation", {FILES_OPERATION_KIND_ATTR: "upload"}, True),
        ("files.operation", {FILES_OPERATION_KIND_ATTR: "delete"}, True),
        ("files.operation", {FILES_OPERATION_KIND_ATTR: "list"}, False),
        ("files.operation", {FILES_OPERATION_KIND_ATTR: "reference"}, False),
        ("files.operation", None, True),  # conservative-absent → always-sample
        # memory.operation — §9.2 mutation always-samples; §10.1 complement drops.
        ("memory.operation", {MEMORY_OPERATION_KIND_ATTR: "write"}, True),
        ("memory.operation", {MEMORY_OPERATION_KIND_ATTR: "delete"}, True),
        ("memory.operation", {MEMORY_OPERATION_KIND_ATTR: "read"}, False),
        ("memory.operation", None, True),  # conservative-absent → always-sample
        # validator.fail.* — permanent always-samples; transient drops.
        ("validator.fail.semantic_inconsistency", {"validator.fail.permanence": "permanent"}, True),
        (
            "validator.fail.semantic_inconsistency",
            {"validator.fail.permanence": "transient"},
            False,
        ),
    ],
)
def test_should_sample_conditional_rows_at_root_base_rate_zero(
    name: str, attributes: dict[str, str] | None, should_record: bool
) -> None:
    """At a root span with base_rate=0.0, the §9.2 conditional rows sample iff
    their discriminating attribute mandates always-sampling; non-mutation /
    transient variants fall through to the dropped base-rate branch."""
    sampler = HarnessCompositeSampler(base_rate=0.0)
    result = sampler.should_sample(
        parent_context=None,
        trace_id=0x12345678901234567890123456789012,
        name=name,
        attributes=attributes,
    )
    assert _result_records(result.decision) is should_record


def test_conditional_row_non_root_inherits_parent_not_kind() -> None:
    """Enforcement-boundary lock: through `ParentBased`, a NON-root
    `files.operation` span inherits the parent's sampling decision and never
    reaches the inner sampler's §9.2-conditional logic — so its `kind` is NOT
    consulted. A non-mutation `kind=list` under a SAMPLED parent still samples
    (parent wins, not the §10.1 base-rate the kind would imply at root); the
    same span under an UNSAMPLED parent drops even at `base_rate=1.0`. This
    documents why the head-sampler refinement is a production no-op for the
    non-root files/memory producers (see `composite_sampler` module docstring +
    the `B-TAIL-CONDITIONAL-SAMPLING` forward arc)."""
    sampler = build_default_sampler(base_rate=1.0)
    non_mutation = {FILES_OPERATION_KIND_ATTR: "list"}

    sampled_parent = SpanContext(
        trace_id=0x12345678901234567890123456789012,
        span_id=0x1234567890123456,
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    sampled_ctx = ot_trace.set_span_in_context(NonRecordingSpan(sampled_parent))
    result_sampled = sampler.should_sample(
        parent_context=sampled_ctx,
        trace_id=sampled_parent.trace_id,
        name="files.operation",
        kind=SpanKind.INTERNAL,
        attributes=non_mutation,
    )
    assert _result_records(result_sampled.decision)  # parent wins, not kind=list

    unsampled_parent = SpanContext(
        trace_id=0x12345678901234567890123456789012,
        span_id=0x1234567890123456,
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.DEFAULT),
    )
    unsampled_ctx = ot_trace.set_span_in_context(NonRecordingSpan(unsampled_parent))
    result_unsampled = sampler.should_sample(
        parent_context=unsampled_ctx,
        trace_id=unsampled_parent.trace_id,
        name="files.operation",
        kind=SpanKind.INTERNAL,
        attributes={FILES_OPERATION_KIND_ATTR: "upload"},  # mutation, yet parent wins
    )
    assert not _result_records(result_unsampled.decision)  # parent wins, not kind=upload
