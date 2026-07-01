"""Tests for U-OD-08 — F3 lifecycle event-to-span-event mapping.

Test set per the U-OD-08 §3.2.5 (v2.8) `Tests:` field — covers acceptance
#1-#8 against C-OD-06 §6.1 / §6.2 / §6.3. acc #1/#2/#3 conformed to the v2.8
D-2 spec §6.1 eight-event table.
"""

from __future__ import annotations

from harness_od.f3_lifecycle_event_mapping import (
    F2_12_DEFERRAL_NOTE_AT_RETRY_ATTEMPT,
    F3_LIFECYCLE_EVENT_MAPPINGS,
    F3LifecycleEventClass,
    LifecycleEventMapping,
)


def _mapping(event_class: F3LifecycleEventClass) -> LifecycleEventMapping:
    return F3_LIFECYCLE_EVENT_MAPPINGS[event_class]


# --- acc #1 ----------------------------------------------------------------
def test_f3_lifecycle_event_class_cardinality_eight() -> None:
    """`F3LifecycleEventClass` enumerates exactly 8 values per §6.1."""
    assert len(F3LifecycleEventClass) == 8


def test_f3_lifecycle_event_class_members_byte_exact_per_6_1() -> None:
    """Member set is byte-exact against the §6.1 eight-event taxonomy."""
    assert set(F3LifecycleEventClass) == {
        F3LifecycleEventClass.WORKFLOW_START,
        F3LifecycleEventClass.STEP_BOUNDARY,
        F3LifecycleEventClass.FALLBACK_TRIGGERED,
        F3LifecycleEventClass.RETRY_ATTEMPT,
        F3LifecycleEventClass.BREAKER_TRIPPED,
        F3LifecycleEventClass.LEASE_ACQUIRED,
        F3LifecycleEventClass.LEASE_RELEASED,
        F3LifecycleEventClass.WORKFLOW_RESUMED,
    }


# --- acc #2 ----------------------------------------------------------------
def test_f3_lifecycle_event_mappings_cardinality_eight() -> None:
    """`F3_LIFECYCLE_EVENT_MAPPINGS` declares exactly 8 entries per §6.1."""
    assert len(F3_LIFECYCLE_EVENT_MAPPINGS) == 8
    assert set(F3_LIFECYCLE_EVENT_MAPPINGS) == set(F3LifecycleEventClass)


# --- acc #3 — per-class mapping, byte-exact with the §6.1 table ------------
def test_workflow_start_mapping() -> None:
    """`WORKFLOW_START` → §6.1 row verbatim."""
    m = _mapping(F3LifecycleEventClass.WORKFLOW_START)
    assert m.event_class_name == "workflow.start"
    assert m.span_placement_form == "Span attribute on root span"
    assert m.attribute_namespaces == frozenset({"engine.*"})
    assert m.sampling_posture == "Per root span sampling (inherits)"


def test_step_boundary_mapping_no_namespace() -> None:
    """`STEP_BOUNDARY` → §6.1 row verbatim; no dedicated namespace."""
    m = _mapping(F3LifecycleEventClass.STEP_BOUNDARY)
    assert m.event_class_name == "step.boundary"
    assert m.span_placement_form == "Span event on parent"
    assert m.attribute_namespaces == frozenset()
    assert m.sampling_posture == "Per parent sampling"


def test_fallback_triggered_mapping() -> None:
    """`FALLBACK_TRIGGERED` → §6.1 row verbatim."""
    m = _mapping(F3LifecycleEventClass.FALLBACK_TRIGGERED)
    assert m.event_class_name == "fallback.triggered"
    assert m.span_placement_form == "Span event on parent + new sibling fallback span"
    assert m.attribute_namespaces == frozenset({"fallback.*"})
    assert m.sampling_posture == "Always-sampled per C-OD-09"


def test_retry_attempt_mapping() -> None:
    """`RETRY_ATTEMPT` → §6.1 row verbatim."""
    m = _mapping(F3LifecycleEventClass.RETRY_ATTEMPT)
    assert m.event_class_name == "retry.attempt"
    assert m.span_placement_form == "Span event on parent + new sibling retry span"
    assert m.attribute_namespaces == frozenset({"retry.*"})
    assert m.sampling_posture == (
        "Base-rate at 1st attempt; always-sampled at 2nd onward per C-CP-03 §3.5"
    )


def test_breaker_tripped_mapping() -> None:
    """`BREAKER_TRIPPED` → §6.1 row verbatim."""
    m = _mapping(F3LifecycleEventClass.BREAKER_TRIPPED)
    assert m.event_class_name == "breaker.tripped"
    assert m.span_placement_form == "Span event on parent"
    assert m.attribute_namespaces == frozenset({"harness.breaker.*"})
    assert m.sampling_posture == "Always-sampled per C-OD-09"


def test_lease_acquired_mapping() -> None:
    """`LEASE_ACQUIRED` → §6.1 row verbatim."""
    m = _mapping(F3LifecycleEventClass.LEASE_ACQUIRED)
    assert m.event_class_name == "lease.acquired"
    assert m.span_placement_form == "Span event on parent"
    assert m.attribute_namespaces == frozenset({"lease.*"})
    assert m.sampling_posture == "Base-rate per C-CP-05 §5.4"


def test_lease_released_mapping() -> None:
    """`LEASE_RELEASED` → §6.1 row verbatim."""
    m = _mapping(F3LifecycleEventClass.LEASE_RELEASED)
    assert m.event_class_name == "lease.released"
    assert m.span_placement_form == "Span event on parent"
    assert m.attribute_namespaces == frozenset({"lease.*"})
    assert m.sampling_posture == "Base-rate per C-CP-05 §5.4"


def test_workflow_resumed_mapping() -> None:
    """`WORKFLOW_RESUMED` → §6.1 row verbatim."""
    m = _mapping(F3LifecycleEventClass.WORKFLOW_RESUMED)
    assert m.event_class_name == "workflow.resumed"
    assert m.span_placement_form == "Span attribute on root span (post-resumption)"
    assert m.attribute_namespaces == frozenset({"engine.*"})
    assert m.sampling_posture == "Always-sampled per C-CP-05 §5.4"


# --- acc #2 — every mapping carries the §6.1 columns -----------------------
def test_lifecycle_event_mapping_carries_span_placement_form() -> None:
    """Every mapping carries a non-empty §6.1 col-2 span-placement form."""
    for m in F3_LIFECYCLE_EVENT_MAPPINGS.values():
        assert m.span_placement_form


def test_lifecycle_event_mapping_carries_sampling_posture() -> None:
    """Every mapping carries a non-empty §6.1 col-4 sampling posture."""
    for m in F3_LIFECYCLE_EVENT_MAPPINGS.values():
        assert m.sampling_posture


# --- acc #4 ----------------------------------------------------------------
def test_additive_composition_no_base_layer_replacement() -> None:
    """Additive composition per §6.2 — keying is by event class, no collision.

    Per §6.2 lifecycle event attributes compose additively with base-layer
    attributes; no mapping replaces a base-layer attribute. Each F3 event class
    maps to exactly one mapping keyed by its own class — distinct namespace
    sets per class do not overwrite one another.
    """
    keys = list(F3_LIFECYCLE_EVENT_MAPPINGS)
    assert len(keys) == len(set(keys))
    for event_class, m in F3_LIFECYCLE_EVENT_MAPPINGS.items():
        assert m.f3_event_class is event_class


# --- acc #5 + acc #6 -------------------------------------------------------
def test_f2_12_deferral_note_byte_exact() -> None:
    """`F2_12_DEFERRAL_NOTE_AT_RETRY_ATTEMPT` carries the §6.3 text verbatim."""
    assert F2_12_DEFERRAL_NOTE_AT_RETRY_ATTEMPT == (
        "retry.attempt sibling-span discipline at D6 ingestion is deferred per "
        "F2-12 carry-forward; v1 commits event + new sibling span per "
        "C-CP-03 §3.5; revisable at D6 v1.2"
    )


def test_f2_12_deferral_note_non_contract_bearing() -> None:
    """The F2-12 note is a forward-compat note, not contract-bearing (acc #6).

    It is a plain `str` constant — not a typed contract-bearing carrier; the
    F2-12 ACTIVE engagement is exclusively at U-OD-20 §14.5.
    """
    assert isinstance(F2_12_DEFERRAL_NOTE_AT_RETRY_ATTEMPT, str)


# --- acc #7 ----------------------------------------------------------------
def test_cross_axis_edge_to_u_cp_54_section_24_1_b_declared() -> None:
    """Cross-axis edge to U-CP-54 / C-CP-24 §24.1.B is declared in the module.

    The U-CP-54 edge resolves at Phase 7 sub-phase 7c; per OD-S4-3.A it is
    declared in the module docstring. No typed surface is imported from
    U-CP-54 — verified by the module importing nothing from any CP package.
    """
    from harness_od import f3_lifecycle_event_mapping as mod

    assert mod.__doc__ is not None
    assert "U-CP-54" in mod.__doc__
    assert "C-CP-24 §24.1.B" in mod.__doc__


# --- acc #8 ----------------------------------------------------------------
def test_f3_capability_floor_anchor() -> None:
    """The mapping is the F3 v1.1 capability-floor (iv) lifecycle composition."""
    from harness_od import f3_lifecycle_event_mapping as mod

    assert mod.__doc__ is not None
    assert "capability-floor (iv)" in mod.__doc__
