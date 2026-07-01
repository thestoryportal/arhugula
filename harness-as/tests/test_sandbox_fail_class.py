"""Tests for U-AS-03 — sandbox-fail-class taxonomy (C-AS-04 §4.1-§4.3)."""

from __future__ import annotations

from harness_as.sandbox_fail_class import (
    C5FailClass,
    C9RetryPosture,
    SandboxFailClass,
    fail_class_metadata,
    permanent_fail_skips_staircase,
)

# 7 fail-class identifiers byte-exact snake_case per spec §4.1.
_SPEC_FAIL_CLASSES = {
    "escape_attempt",
    "egress_denied",
    "timeout",
    "oom",
    "signal",
    "exit_nonzero",
    "policy_override",
}

# Per-class metadata verbatim per acceptance #3:
# fail_class -> (c5, c9, skips, always_sampled, tamper)
_SPEC_METADATA: dict[SandboxFailClass, tuple[C5FailClass, C9RetryPosture, bool, bool, bool]] = {
    SandboxFailClass.ESCAPE_ATTEMPT: (
        C5FailClass.PERMANENT_FAIL,
        C9RetryPosture.NO_RETRY,
        True,
        True,
        True,
    ),
    SandboxFailClass.EGRESS_DENIED: (
        C5FailClass.PERMANENT_FAIL,
        C9RetryPosture.NO_RETRY,
        True,
        True,
        False,
    ),
    SandboxFailClass.TIMEOUT: (
        C5FailClass.TRANSIENT_FAIL,
        C9RetryPosture.C9_BACKOFF_RETRY,
        False,
        True,
        False,
    ),
    SandboxFailClass.OOM: (
        C5FailClass.TRANSIENT_FAIL,
        C9RetryPosture.C9_BACKOFF_RETRY,
        False,
        True,
        False,
    ),
    SandboxFailClass.SIGNAL: (
        C5FailClass.PERMANENT_FAIL,
        C9RetryPosture.NO_RETRY,
        True,
        True,
        False,
    ),
    SandboxFailClass.EXIT_NONZERO: (
        C5FailClass.GATE_CONTRACT_DEPENDENT,
        C9RetryPosture.PER_TOOL_RETRY_EXIT,
        False,
        True,
        False,
    ),
    SandboxFailClass.POLICY_OVERRIDE: (
        C5FailClass.INFORMATIONAL,
        C9RetryPosture.AUDIT_LEDGER_ONLY,
        False,
        True,
        False,
    ),
}

_STAIRCASE_SKIPPERS = {
    SandboxFailClass.ESCAPE_ATTEMPT,
    SandboxFailClass.EGRESS_DENIED,
    SandboxFailClass.SIGNAL,
}


def test_sandbox_fail_class_enum_cardinality_seven() -> None:
    """Acceptance #1 + #2 — exactly 7 values; an 8th would fail this audit."""
    assert len(SandboxFailClass) == 7


def test_sandbox_fail_class_identifier_strings_snake_case_byte_exact() -> None:
    """Acceptance #1 — fail-class identifier strings byte-exact snake_case."""
    assert {c.value for c in SandboxFailClass} == _SPEC_FAIL_CLASSES


def test_fail_class_metadata_table_complete() -> None:
    """Acceptance #3 — fail_class_metadata returns a row for every class."""
    for fail_class in SandboxFailClass:
        assert fail_class_metadata(fail_class).fail_class is fail_class


def test_fail_class_metadata_per_spec_table_verbatim() -> None:
    """Acceptance #3 — per-class metadata matches spec §4.1 verbatim."""
    for fail_class, (c5, c9, skips, sampled, tamper) in _SPEC_METADATA.items():
        meta = fail_class_metadata(fail_class)
        assert meta.c5_classification is c5
        assert meta.c9_retry_posture is c9
        assert meta.skips_pre_hitl_staircase is skips
        assert meta.always_sampled is sampled
        assert meta.tamper_evidence_relevant is tamper


def test_permanent_fail_skips_staircase_for_escape_egress_signal_only() -> None:
    """Acceptance #4 — staircase-skip true exactly for escape/egress/signal."""
    for fail_class in SandboxFailClass:
        expected = fail_class in _STAIRCASE_SKIPPERS
        assert permanent_fail_skips_staircase(fail_class) is expected


def test_always_sampled_uniform_true_across_classes() -> None:
    """Acceptance #5 — always_sampled uniformly true (C-AS-04 §4.3)."""
    for fail_class in SandboxFailClass:
        assert fail_class_metadata(fail_class).always_sampled is True


def test_tamper_evidence_relevant_only_for_escape_attempt() -> None:
    """Acceptance #6 — tamper_evidence_relevant true only for escape_attempt."""
    for fail_class in SandboxFailClass:
        expected = fail_class is SandboxFailClass.ESCAPE_ATTEMPT
        assert fail_class_metadata(fail_class).tamper_evidence_relevant is expected
