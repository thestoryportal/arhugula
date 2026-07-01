"""5-class validator-fail taxonomy + `validator.fail.*` namespace — U-CP-47.

Implements C-CP-21 §21.1 (discriminated five-class fail taxonomy) and §21.5
(three `validator.fail.*` attribute declarations). Declares the closed 5-value
`ValidatorRetryExitClass` enum, the `ValidatorFailMetadata` record + 5-entry
`VALIDATOR_FAIL_METADATA`, the `ValidatorFailAttributeSchema` record + 3-entry
`VALIDATOR_FAIL_NAMESPACE_SCHEMA`, and `validator_fail_permanence` — the
class → permanence derivation.

**Canonical body.** CP plan v2.4 U-CP-47 — the v2.4 amendment conformed
`ValidatorRetryExitClass`, `VALIDATOR_FAIL_METADATA`, and
`VALIDATOR_FAIL_NAMESPACE_SCHEMA` to CP spec §21.1 / §21.5 verbatim per the §4A
verbatim-divergence cluster resolution. The v2.1/v2.3 divergent values
(`SCHEMA_MISMATCH` / `TIMEOUT` / `RATE_LIMIT` / `PERMANENT_REJECTION` /
`SANDBOX_VIOLATION` and `validator.fail.is_transient` /
`validator.fail.retry_attempt`) are NOT used.

**Carrier note.** `AttributeValueType` / `Cardinality` resolve to
`harness-core` (carrier U-CP-00b, re-homed per the U-AS-31 fork). The fork-queue
item-18 Pattern-C "no carrier reachability" defect is resolved — `harness-core`
is an always-available dependency. U-AS-03's `SandboxFailClass` is consumed as
a cross-axis *composition reference* only (per the U-CP-47 Cross-axis substrate
note), not imported as a type here.

Authority: Implementation_Plan_Control_Plane_v2_4.md §2.8 U-CP-47 (v2.4 §4A
verbatim-divergence conformance — 5-class taxonomy + metadata + namespace
conformed to spec §21.1/§21.5); Spec_Control_Plane_v1_2.md §21 C-CP-21 §21.1 +
§21.5 (preserved verbatim into v1.3); ADR-D5 v1.3 §1.10 + §1.10.1.
"""

from __future__ import annotations

from enum import StrEnum

from harness_core import AttributeValueType, Cardinality
from pydantic import BaseModel, ConfigDict


class ValidatorRetryExitClass(StrEnum):
    """The 5 validator-fail retry-exit classes (C-CP-21 §21.1, verbatim).

    Member string values are the §21.1 `validator.fail.class` taxonomy
    verbatim. Closed at cardinality 5 — extension is a Workflow §4.1.2 Class-2
    D5 revision.
    """

    TRANSIENT_RETRY = "transient-retry"
    """Transient staircase (§21.2); C9 backoff + retry, full-jitter."""

    REFLEXION_RECOVERABLE = "Reflexion-recoverable"
    """Transient staircase (§21.2); C5 reflect-step verbal feedback +
    C1 retry-loop."""

    HITL_RECOVERABLE = "HITL-recoverable"
    """C11 HITL primitive (validator-HITL placement per §17.1
    `validator-escalation`)."""

    PERMANENT_FAIL_EXIT = "permanent-fail-exit"
    """SKIP STAIRCASE; route directly to C11 HITL (validator-escalation)."""

    TERMINAL_FAIL_EXIT = "terminal-fail-exit"
    """SKIP STAIRCASE; workflow halts; HITL escalation with no recovery."""


class ValidatorFailMetadata(BaseModel):
    """One §21.1 fail-class row — Routing + Recovery-path columns."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fail_class: ValidatorRetryExitClass
    """The validator-fail class this metadata row describes."""

    routing: str
    """C-CP-21 §21.1 "Routing" column."""

    recovery_path: str
    """C-CP-21 §21.1 "Recovery path" column."""


VALIDATOR_FAIL_METADATA: tuple[ValidatorFailMetadata, ...] = (
    ValidatorFailMetadata(
        fail_class=ValidatorRetryExitClass.TRANSIENT_RETRY,
        routing="Transient staircase (§21.2)",
        recovery_path=(
            "C9 backoff + retry (full-jitter); cause-attribution-conditioned "
            "policy per c9-reliability-recovery SKILL.md §4.1.1"
        ),
    ),
    ValidatorFailMetadata(
        fail_class=ValidatorRetryExitClass.REFLEXION_RECOVERABLE,
        routing="Transient staircase (§21.2)",
        recovery_path=(
            "C5 reflect-step verbal feedback + C1 retry-loop; C2 stitches "
            "feedback into next iteration's prompt"
        ),
    ),
    ValidatorFailMetadata(
        fail_class=ValidatorRetryExitClass.HITL_RECOVERABLE,
        routing=(
            "C11 HITL primitive (validator-HITL placement per §17.1 "
            "validator-escalation); palette {approve, request-changes, "
            "reject}; request-changes routes back as Reflexion-recoverable"
        ),
        recovery_path="HITL invocation",
    ),
    ValidatorFailMetadata(
        fail_class=ValidatorRetryExitClass.PERMANENT_FAIL_EXIT,
        routing=(
            "SKIP STAIRCASE; route directly to C11 HITL (validator-escalation "
            "per §17.1); palette {approve, edit, reject, respond}, restricted "
            "to {approve, reject, respond} at cross-trust-boundary actions"
        ),
        recovery_path="Direct HITL",
    ),
    ValidatorFailMetadata(
        fail_class=ValidatorRetryExitClass.TERMINAL_FAIL_EXIT,
        routing=(
            "SKIP STAIRCASE; workflow halts; HITL escalation per "
            "c11-operator-local SKILL.md with no recovery path"
        ),
        recovery_path="Halt + HITL notification",
    ),
)
"""The 5 §21.1 fail-class metadata rows — one per `ValidatorRetryExitClass`."""


class ValidatorFailAttributeSchema(BaseModel):
    """One `validator.fail.*` span attribute (C-CP-21 §21.5 table row)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attribute_name: str
    value_type: AttributeValueType
    cardinality: Cardinality


VALIDATOR_FAIL_NAMESPACE_SCHEMA: tuple[ValidatorFailAttributeSchema, ...] = (
    ValidatorFailAttributeSchema(
        attribute_name="validator.fail.class",
        value_type=AttributeValueType.ENUM_REF,
        cardinality=Cardinality.LOW,
    ),
    ValidatorFailAttributeSchema(
        attribute_name="validator.fail.cause_attribution",
        value_type=AttributeValueType.ENUM_REF,
        cardinality=Cardinality.MEDIUM,
    ),
    ValidatorFailAttributeSchema(
        attribute_name="validator.fail.permanence",
        value_type=AttributeValueType.ENUM_REF,
        cardinality=Cardinality.LOW,
    ),
)
"""The 3 `validator.fail.*` span attributes (C-CP-21 §21.5 verbatim).

`validator.fail.class` is bounded(5) → `LOW`; `validator.fail.cause_attribution`
is the §21.5 "medium (open set)" → `MEDIUM`; `validator.fail.permanence` is
bounded(2) → `LOW`."""


_PERMANENT_CLASSES: frozenset[ValidatorRetryExitClass] = frozenset(
    {
        ValidatorRetryExitClass.PERMANENT_FAIL_EXIT,
        ValidatorRetryExitClass.TERMINAL_FAIL_EXIT,
    }
)


def validator_fail_permanence(fail_class: ValidatorRetryExitClass) -> str:
    """Derive `validator.fail.permanence` from `validator.fail.class`.

    Per C-CP-21 §21.5: `permanent` if class ∈ {permanent-fail-exit,
    terminal-fail-exit}; `transient` otherwise. This derived discriminator is
    consumed at U-CP-48's transient-staircase entry decision (acc#4).
    """
    return "permanent" if fail_class in _PERMANENT_CLASSES else "transient"
