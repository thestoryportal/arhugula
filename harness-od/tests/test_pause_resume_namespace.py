"""Tests for U-OD-51 — C-OD-30 `pause.*` + `resume.*` canonical namespace
schema + PauseResumeAuditPayload.

ACs from OD plan v2.16 §1 U-OD-51 (preserved from v2.15 with §0 status-block
cross-axis-block lift; AC text preserved verbatim from v2.14):
  AC #1 Schema declares 8 attributes per §C-OD-30.1
  AC #2 PauseResumeAuditPayload extends AuditPayload with 8 pause/resume-
        specific fields (pause OR resume path)
  AC #3 Pattern-P1 byte-exact alignment with CP spec v1.11 §26.4
  AC #4 Optional fields per path (pause_reason populated on pause path;
        resume_outcome on resume path)
  AC #5 Unit test: schema verbatim match
"""

from __future__ import annotations

import pytest
from harness_core import AttributeValueType, Cardinality
from harness_od.pause_resume_namespace import (
    PAUSE_RESUME_SPAN_NAMESPACE_SCHEMA,
    SPAN_SITE_PAUSE_CAPTURED,
    SPAN_SITE_RESUME_ATTEMPTED,
    PauseResumeAuditPayload,
)
from pydantic import ValidationError

# ----------------------------------------------------------------------------
# AC #1 + AC #5 — schema declares 8 attributes; row-by-row match
# ----------------------------------------------------------------------------


def test_schema_has_exactly_eight_attributes() -> None:
    """AC #1 — exactly 8 attributes across 2 span sites."""
    assert len(PAUSE_RESUME_SPAN_NAMESPACE_SCHEMA) == 8


def test_schema_has_two_distinct_span_sites() -> None:
    """AC #1 — 2 distinct span sites per §C-OD-30.1."""
    sites = {attr.span_site for attr in PAUSE_RESUME_SPAN_NAMESPACE_SCHEMA.values()}
    assert sites == {
        SPAN_SITE_PAUSE_CAPTURED,
        SPAN_SITE_RESUME_ATTEMPTED,
    }


@pytest.mark.parametrize(
    "site,expected_count",
    [
        (SPAN_SITE_PAUSE_CAPTURED, 4),
        (SPAN_SITE_RESUME_ATTEMPTED, 4),
    ],
)
def test_per_site_attribute_count(site: str, expected_count: int) -> None:
    """AC #1 — site-decomposition: 4 / 4 (= 8)."""
    site_attrs = [a for a in PAUSE_RESUME_SPAN_NAMESPACE_SCHEMA.values() if a.span_site == site]
    assert len(site_attrs) == expected_count


# ----------------------------------------------------------------------------
# AC #3 + AC #5 — Pattern-P1 alignment with CP spec v1.11 §26.4 producer-side
# (byte-exact attribute name match)
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "attribute_name",
    [
        # pause.captured site (4)
        "pause.reason",
        "pause.snapshot_hash",
        "pause.step_index",
        "pause.state_ledger_anchor",
        # resume.attempted site (4)
        "resume.snapshot_hash",
        "resume.diff_detected",
        "resume.diff_policy",
        "resume.outcome",
    ],
)
def test_pattern_p1_byte_exact_attribute_name(attribute_name: str) -> None:
    """AC #3 — byte-exact attribute name match per CP spec v1.11 §26.4
    producer-side. Schema key equals the attribute_name field equals the
    canonical name string at the spec table."""
    assert attribute_name in PAUSE_RESUME_SPAN_NAMESPACE_SCHEMA
    assert PAUSE_RESUME_SPAN_NAMESPACE_SCHEMA[attribute_name].attribute_name == (attribute_name)


# ----------------------------------------------------------------------------
# AC #5 — schema row-by-row verbatim match against §C-OD-30.1
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "attribute_name,expected_value_type,expected_cardinality,expected_span_site",
    [
        # pause.captured site
        (
            "pause.reason",
            AttributeValueType.ENUM_REF,
            Cardinality.LOW,
            SPAN_SITE_PAUSE_CAPTURED,
        ),
        (
            "pause.snapshot_hash",
            AttributeValueType.STRING,
            Cardinality.HIGH,
            SPAN_SITE_PAUSE_CAPTURED,
        ),
        (
            "pause.step_index",
            AttributeValueType.INT,
            Cardinality.HIGH,
            SPAN_SITE_PAUSE_CAPTURED,
        ),
        (
            "pause.state_ledger_anchor",
            AttributeValueType.STRING,
            Cardinality.HIGH,
            SPAN_SITE_PAUSE_CAPTURED,
        ),
        # resume.attempted site
        (
            "resume.snapshot_hash",
            AttributeValueType.STRING,
            Cardinality.HIGH,
            SPAN_SITE_RESUME_ATTEMPTED,
        ),
        (
            "resume.diff_detected",
            AttributeValueType.BOOL,
            Cardinality.LOW,
            SPAN_SITE_RESUME_ATTEMPTED,
        ),
        (
            "resume.diff_policy",
            AttributeValueType.ENUM_REF,
            Cardinality.LOW,
            SPAN_SITE_RESUME_ATTEMPTED,
        ),
        (
            "resume.outcome",
            AttributeValueType.ENUM_REF,
            Cardinality.LOW,
            SPAN_SITE_RESUME_ATTEMPTED,
        ),
    ],
)
def test_schema_row_verbatim_match(
    attribute_name: str,
    expected_value_type: AttributeValueType,
    expected_cardinality: Cardinality,
    expected_span_site: str,
) -> None:
    """AC #5 — schema declaration matches §C-OD-30.1 row-by-row."""
    spec = PAUSE_RESUME_SPAN_NAMESPACE_SCHEMA[attribute_name]
    assert spec.value_type == expected_value_type
    assert spec.cardinality == expected_cardinality
    assert spec.span_site == expected_span_site


def test_attribute_spec_is_frozen() -> None:
    """AC #5 — AttributeSpec is frozen Pydantic model (extra='forbid')."""
    # Pydantic v2 frozen=True raises ValidationError on field mutation.
    spec = next(iter(PAUSE_RESUME_SPAN_NAMESPACE_SCHEMA.values()))
    with pytest.raises(ValidationError):
        spec.attribute_name = "mutated"  # type: ignore[misc]


# ----------------------------------------------------------------------------
# AC #2 + AC #4 — PauseResumeAuditPayload field set + path-conditional
# Optional fields (pause path vs resume path)
# ----------------------------------------------------------------------------


def test_pause_resume_audit_payload_field_set() -> None:
    """AC #2 — PauseResumeAuditPayload declares 4 inherited audit_cp_* fields
    + 8 pause/resume-specific fields per §C-OD-30.2 (12 total)."""
    fields = PauseResumeAuditPayload.model_fields
    expected_field_names = {
        # Inherited per §C-OD-24.6:
        "audit_cp_action_id",
        "audit_cp_response",
        "audit_cp_timestamp",
        "audit_cp_prior_event_hash",
        # Always-populated common per §C-OD-30.2:
        "snapshot_hash",
        "step_index",
        # Pause-path-specific (Optional):
        "pause_reason",
        "state_ledger_anchor",
        # Resume-path-specific (Optional):
        "diff_detected",
        "diff_policy",
        "diff_summary_hash",
        "resume_outcome",
    }
    assert set(fields.keys()) == expected_field_names
    assert len(fields) == 12


def test_pause_path_construction() -> None:
    """AC #4 — pause-path construction: pause_reason populated; resume-path
    Optional fields = None."""
    payload = PauseResumeAuditPayload(
        audit_cp_action_id="pause:wf-1:5",
        audit_cp_response="paused",
        audit_cp_timestamp="2026-05-23T14:00:00Z",
        audit_cp_prior_event_hash="0" * 64,
        snapshot_hash="a" * 64,
        step_index=5,
        pause_reason="hitl_defer",  # WorkflowPauseReason enum value
        state_ledger_anchor="entry_hash:abc123",
        # Resume-path fields = None:
        diff_detected=None,
        diff_policy=None,
        diff_summary_hash=None,
        resume_outcome=None,
    )
    assert payload.audit_cp_response == "paused"
    assert payload.pause_reason == "hitl_defer"
    assert payload.state_ledger_anchor == "entry_hash:abc123"
    assert payload.diff_detected is None
    assert payload.resume_outcome is None


def test_resume_path_construction() -> None:
    """AC #4 — resume-path construction: diff_detected + resume_outcome
    populated; pause-path Optional fields = None."""
    payload = PauseResumeAuditPayload(
        audit_cp_action_id="resume:wf-1:5",
        audit_cp_response="resumed",
        audit_cp_timestamp="2026-05-23T14:00:00Z",
        audit_cp_prior_event_hash="0" * 64,
        snapshot_hash="a" * 64,
        step_index=5,
        # Pause-path fields = None:
        pause_reason=None,
        state_ledger_anchor=None,
        # Resume-path fields populated:
        diff_detected=False,
        diff_policy="STRICT",  # MaterialDiffPolicy enum value
        diff_summary_hash=None,  # no diff detected → no summary hash
        resume_outcome="resumed",
    )
    assert payload.audit_cp_response == "resumed"
    assert payload.diff_detected is False
    assert payload.diff_policy == "STRICT"
    assert payload.resume_outcome == "resumed"
    assert payload.pause_reason is None
    assert payload.state_ledger_anchor is None


def test_resume_path_with_diff_detected() -> None:
    """AC #4 — resume-path with material-diff detected: diff_summary_hash
    populated; resume_outcome reflects diff-arbitration disposition."""
    payload = PauseResumeAuditPayload(
        audit_cp_action_id="resume:wf-1:5",
        audit_cp_response="diff_detected",
        audit_cp_timestamp="2026-05-23T14:00:00Z",
        audit_cp_prior_event_hash="0" * 64,
        snapshot_hash="a" * 64,
        step_index=5,
        pause_reason=None,
        state_ledger_anchor=None,
        diff_detected=True,
        diff_policy="OPERATOR_ARBITRATE",
        diff_summary_hash="b" * 64,
        resume_outcome="arbitration_owed",
    )
    assert payload.diff_detected is True
    assert payload.diff_summary_hash == "b" * 64
    assert payload.resume_outcome == "arbitration_owed"


def test_pause_resume_audit_payload_is_frozen() -> None:
    """AC #2 — PauseResumeAuditPayload is frozen Pydantic model
    (extra='forbid')."""
    payload = PauseResumeAuditPayload(
        audit_cp_action_id="pause:wf-1:5",
        audit_cp_response="paused",
        audit_cp_timestamp="",
        audit_cp_prior_event_hash="0" * 64,
        snapshot_hash="a" * 64,
        step_index=5,
        pause_reason="engine_pause",
        state_ledger_anchor="entry_hash:abc",
        diff_detected=None,
        diff_policy=None,
        diff_summary_hash=None,
        resume_outcome=None,
    )
    with pytest.raises(ValidationError):
        payload.audit_cp_action_id = "mutated"  # type: ignore[misc]


def test_pause_resume_audit_payload_extra_forbid() -> None:
    """AC #2 — extra='forbid' rejects unknown fields."""
    with pytest.raises(ValidationError):
        PauseResumeAuditPayload(  # type: ignore[call-arg]
            audit_cp_action_id="pause:wf-1:5",
            audit_cp_response="paused",
            audit_cp_timestamp="",
            audit_cp_prior_event_hash="0" * 64,
            snapshot_hash="a" * 64,
            step_index=5,
            pause_reason="hitl_defer",
            state_ledger_anchor="entry_hash:abc",
            diff_detected=None,
            diff_policy=None,
            diff_summary_hash=None,
            resume_outcome=None,
            unknown_field="should_reject",
        )
