"""Tests for U-CP-47 — 5-class validator-fail taxonomy + namespace.

Acceptance-criterion coverage (CP plan v2.4 U-CP-47, C-CP-21 §21.1/§21.5):
  #1 ValidatorRetryExitClass 5 values verbatim -> test_validator_fail_class_cardinality_five,
                                            test_validator_fail_class_values_match_spec_21_1_verbatim
  #2 VALIDATOR_FAIL_METADATA per §21.1     -> test_validator_fail_metadata_match_spec_21_1
  #3 namespace 3 attrs per §21.5           -> test_validator_fail_namespace_cardinality_three,
                                            test_validator_fail_attributes_match_spec_21_5_verbatim
  #4 permanence derived from class         -> test_validator_fail_permanence_derived_from_class
  #5 D6 ingestion delegates to U-CP-54     -> structural (out of unit scope)
"""

from __future__ import annotations

from harness_core import AttributeValueType, Cardinality
from harness_cp.validator_fail_taxonomy import (
    VALIDATOR_FAIL_METADATA,
    VALIDATOR_FAIL_NAMESPACE_SCHEMA,
    ValidatorRetryExitClass,
    validator_fail_permanence,
)


def test_validator_fail_class_cardinality_five() -> None:
    """#1 — `ValidatorRetryExitClass` declares exactly five values."""
    assert len(ValidatorRetryExitClass) == 5


def test_validator_fail_class_values_match_spec_21_1_verbatim() -> None:
    """#1 — the five §21.1 retry-exit taxonomy values, byte-exact."""
    assert {c.value for c in ValidatorRetryExitClass} == {
        "transient-retry",
        "Reflexion-recoverable",
        "HITL-recoverable",
        "permanent-fail-exit",
        "terminal-fail-exit",
    }


def test_validator_fail_metadata_match_spec_21_1() -> None:
    """#2 — `VALIDATOR_FAIL_METADATA` declares 5 rows, one per class,
    each carrying Routing + Recovery-path columns."""
    assert len(VALIDATOR_FAIL_METADATA) == 5
    assert {m.fail_class for m in VALIDATOR_FAIL_METADATA} == set(ValidatorRetryExitClass)
    by_class = {m.fail_class: m for m in VALIDATOR_FAIL_METADATA}
    # SKIP-STAIRCASE classes name the skip in the Routing column.
    assert "SKIP STAIRCASE" in by_class[ValidatorRetryExitClass.PERMANENT_FAIL_EXIT].routing
    assert "SKIP STAIRCASE" in by_class[ValidatorRetryExitClass.TERMINAL_FAIL_EXIT].routing
    # Staircase classes route to the transient staircase.
    assert "Transient staircase" in by_class[ValidatorRetryExitClass.TRANSIENT_RETRY].routing
    assert all(m.routing and m.recovery_path for m in VALIDATOR_FAIL_METADATA)


def test_validator_fail_namespace_cardinality_three() -> None:
    """#3 — `VALIDATOR_FAIL_NAMESPACE_SCHEMA` declares exactly 3 attributes."""
    assert len(VALIDATOR_FAIL_NAMESPACE_SCHEMA) == 3


def test_validator_fail_attributes_match_spec_21_5_verbatim() -> None:
    """#3 — the three §21.5 attribute names + value-type/cardinality."""
    by_name = {a.attribute_name: a for a in VALIDATOR_FAIL_NAMESPACE_SCHEMA}
    assert set(by_name) == {
        "validator.fail.class",
        "validator.fail.cause_attribution",
        "validator.fail.permanence",
    }
    assert by_name["validator.fail.class"].cardinality is Cardinality.LOW
    assert by_name["validator.fail.cause_attribution"].cardinality is Cardinality.MEDIUM
    assert by_name["validator.fail.permanence"].cardinality is Cardinality.LOW
    assert all(a.value_type is AttributeValueType.ENUM_REF for a in VALIDATOR_FAIL_NAMESPACE_SCHEMA)


def test_validator_fail_permanence_derived_from_class() -> None:
    """#4 — `permanence` is `permanent` for the two exit classes,
    `transient` otherwise."""
    assert validator_fail_permanence(ValidatorRetryExitClass.PERMANENT_FAIL_EXIT) == "permanent"
    assert validator_fail_permanence(ValidatorRetryExitClass.TERMINAL_FAIL_EXIT) == "permanent"
    for transient in (
        ValidatorRetryExitClass.TRANSIENT_RETRY,
        ValidatorRetryExitClass.REFLEXION_RECOVERABLE,
        ValidatorRetryExitClass.HITL_RECOVERABLE,
    ):
        assert validator_fail_permanence(transient) == "transient"


def test_validator_fail_metadata_frozen() -> None:
    """`ValidatorFailMetadata` is frozen + extra-forbid."""
    entry = VALIDATOR_FAIL_METADATA[0]
    assert entry.model_config.get("frozen") is True
    assert entry.model_config.get("extra") == "forbid"
