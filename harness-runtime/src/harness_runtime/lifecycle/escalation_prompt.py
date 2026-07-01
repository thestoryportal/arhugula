"""ValidatorEscalation HITL prompt composition helper.

Implements runtime spec v1.22 §14.15.8 deferred-discretion — implementation
selects shape of the operator-facing escalation prompt. v1.22 baseline shape
combines ``brief.escalation_reason`` + ``brief.fail_class`` + palette
enumeration for operator context.
Authority: C-RT-25 and U-RT-91, consuming C-CP-28 ``HITLEscalationBrief``.
"""

from __future__ import annotations

from harness_cp.hitl_response_palette import HITLResponse
from harness_cp.validator_framework_types import HITLEscalationBrief

__all__ = ["compose_escalation_prompt"]


def compose_escalation_prompt(
    brief: HITLEscalationBrief,
    palette: frozenset[HITLResponse],
) -> str:
    """Compose an operator-facing prompt for a validator-escalation HITL gate.

    Parameters
    ----------
    brief
        The ``HITLEscalationBrief`` typed payload from the validator framework
        per CP spec v1.10 §25.2.
    palette
        The effective response palette computed per UNION-intersection
        (``compute_effective_palette`` output).

    Returns
    -------
    str
        Human-readable prompt for the operator. v1.22 baseline shape;
        future arc may formalize via Protocol surface per spec §14.15.8.
    """
    palette_labels = ", ".join(sorted(r.value for r in palette))
    fail_class_label = brief.fail_class.value if brief.fail_class is not None else "unspecified"
    return (
        f"Validator escalation at step {brief.parent_step_id} "
        f"(action_id={brief.parent_action_id}). "
        f"Fail class: {fail_class_label}. "
        f"Reason: {brief.escalation_reason}. "
        f"Available responses: {palette_labels}."
    )
