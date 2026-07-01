"""Tests for U-CP-28 — `SubAgentBrief` schema (C-CP-13 §13.2).

Acceptance-criterion coverage:
  #1 5 fields (4 spec + summary_hash) -> test_sub_agent_brief_five_fields,
                                         test_spec_four_fields_present
  #2 ClearTaskBoundaries 3 fields     -> test_clear_task_boundaries_three_fields
  #3 summary hash = sha256(canon)     -> test_canonicalize_deterministic,
                                         test_summary_hash_round_trip
  #4 summary_hash is U-CP-27 join key -> structural (field present + hex)
"""

from __future__ import annotations

from harness_cp.sub_agent_brief import (
    ClearTaskBoundaries,
    OutputSchema,
    OutputSchemaKind,
    SubAgentBrief,
    canonicalize_brief,
    compute_brief_summary_hash,
)


def _brief(objective: str = "Summarize the API surface") -> SubAgentBrief:
    boundaries = ClearTaskBoundaries(
        in_scope=("public functions",),
        out_of_scope=("private helpers",),
        termination_criteria=("all public functions documented",),
    )
    output_format = OutputSchema(schema_kind=OutputSchemaKind.FREE_TEXT)
    guidance = "Prefer concise prose."

    def _build(summary_hash: str) -> SubAgentBrief:
        return SubAgentBrief(
            objective=objective,
            output_format=output_format,
            guidance=guidance,
            task_boundaries=boundaries,
            summary_hash=summary_hash,
        )

    # Build with a placeholder hash, then re-stamp with the real digest.
    return _build(compute_brief_summary_hash(_build("0" * 64)))


def test_sub_agent_brief_five_fields() -> None:
    """Acceptance #1 — five fields (4 §13.2 + plan-internal summary_hash)."""
    assert set(SubAgentBrief.model_fields) == {
        "objective",
        "output_format",
        "guidance",
        "task_boundaries",
        "summary_hash",
    }


def test_spec_four_fields_present() -> None:
    """Acceptance #1 — the 4 C-CP-13 §13.2 fields are present verbatim."""
    for field in ("objective", "output_format", "guidance", "task_boundaries"):
        assert field in SubAgentBrief.model_fields


def test_clear_task_boundaries_three_fields() -> None:
    """Acceptance #2 — `ClearTaskBoundaries` declares three §13.2 fields."""
    assert set(ClearTaskBoundaries.model_fields) == {
        "in_scope",
        "out_of_scope",
        "termination_criteria",
    }


def test_canonicalize_deterministic() -> None:
    """Acceptance #3 — canonicalization is deterministic."""
    a = _brief()
    b = _brief()
    assert canonicalize_brief(a) == canonicalize_brief(b)
    # summary_hash is excluded from the canonical form.
    assert b"summary_hash" not in canonicalize_brief(a)


def test_summary_hash_round_trip() -> None:
    """Acceptance #3 — summary_hash == sha256(canonicalize_brief(brief))."""
    brief = _brief()
    assert brief.summary_hash == compute_brief_summary_hash(brief)
    assert len(brief.summary_hash) == 64


def test_distinct_briefs_distinct_hashes() -> None:
    """Acceptance #4 — distinct briefs yield distinct join keys."""
    assert _brief("objective A").summary_hash != _brief("objective B").summary_hash


def test_output_schema_kind_cardinality_three() -> None:
    """`OutputSchemaKind` promoted to a real 3-value enum (v2.6 §0.11.4)."""
    assert {k.value for k in OutputSchemaKind} == {
        "json_schema",
        "free_text",
        "structured_record",
    }
