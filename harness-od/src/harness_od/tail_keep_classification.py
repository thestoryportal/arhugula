"""§10.2 tail-keep-on-classification trigger predicate.

Closes H_T-OD-3 PARTIAL → RETIRE-READY gate (a) per OD spec v1.2 §9.1
production-time tail-based-sampling mandate + §10.2 3-trigger preservation
table + §9.3 implementer-discretion clause on the algorithm.

**The §10.2 classification triggers** (canonical at
`harness-od/src/harness_od/base_rate_set_and_envelope.py:TAIL_KEEP_RULES`):

| Trigger ID | Concrete carrier | Source |
|---|---|---|
| `validator.fail.permanent` | span attribute `validator.fail.permanence` == `"permanent"` | C-CP-21 §21.6 + `validator_fail_taxonomy.py:149` |
| `sandbox.violation` | span name == `"sandbox.violation"` | C-AS-15 §15.4 + `sandbox_attribute_schema.py:_VIOLATION` |
| `breaker.tripped` | span name == `"breaker.tripped"` | C-CP-03 §3.5 + `lifecycle_event_span_map.py:91` |

The trigger-ID strings at `TAIL_KEEP_RULES` are the conceptual classification
labels (per §10.2 row 1 column "Classification trigger"); the actual carriers
at the OTel span are heterogeneous — one is an attribute value match
(`validator.fail.permanence=permanent`), the other two are span-name matches.
This helper unifies the three under a single predicate over `ReadableSpan`.

`is_classification_trigger(span)` returns True iff the span carries any of
the three §10.2 triggers. Pure function; no side effects; tolerant of
missing attribute bag (returns False rather than raising).

Pairs with `TailKeepSpanProcessor` at `tail_keep_span_processor.py` (the
consumer — buffers per-trace and forwards-or-drops on root close).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import ReadableSpan

__all__ = [
    "BREAKER_TRIPPED_SPAN_NAME",
    "SANDBOX_VIOLATION_SPAN_NAME",
    "SUBAGENT_RESULT_STATUS_ATTR",
    "SUBAGENT_RESULT_STATUS_FAILED_VALUE",
    "SUBAGENT_SPAN_NAME",
    "VALIDATOR_FAIL_PERMANENCE_ATTR",
    "VALIDATOR_FAIL_PERMANENCE_PERMANENT_VALUE",
    "is_classification_trigger",
]


#: Span name carrying the §10.2 trigger row 2 (`sandbox.violation`).
SANDBOX_VIOLATION_SPAN_NAME: str = "sandbox.violation"

#: Span name carrying the §10.2 trigger row 3 (`breaker.tripped`).
BREAKER_TRIPPED_SPAN_NAME: str = "breaker.tripped"

#: Span attribute key carrying the §10.2 trigger row 1
#: (`validator.fail.permanent` classification ↔ `validator.fail.permanence`
#: attribute name per CP spec C-CP-21 §21.6 + `validator_fail_taxonomy.py`).
VALIDATOR_FAIL_PERMANENCE_ATTR: str = "validator.fail.permanence"

#: Attribute value flagging the row-1 trigger (per validator_fail_permanence()
#: derivation function at harness-cp/src/harness_cp/validator_fail_taxonomy.py).
VALIDATOR_FAIL_PERMANENCE_PERMANENT_VALUE: str = "permanent"

#: Span name carrying the §14.3 subagent tail-keep-on-failure row (`subagent.span`,
#: the real producer-emitted name per harness-runtime sub_agent_dispatch.py).
SUBAGENT_SPAN_NAME: str = "subagent.span"

#: Span attribute key carrying the subagent result status (CP C-CP-14 §14.2/§14.3;
#: ingested verbatim per the D6 namespace-ingestion pattern, like the validator attr above).
SUBAGENT_RESULT_STATUS_ATTR: str = "subagent.result_status"

#: Attribute value flagging the §14.3 subagent-failure tail-keep
#: (`SubAgentResultStatus.FAILED` value per CP topology_subagent_namespace.py; the runtime
#: producer emits the lowercase `"failed"` at sub_agent_dispatch.py).
SUBAGENT_RESULT_STATUS_FAILED_VALUE: str = "failed"


def is_classification_trigger(span: ReadableSpan) -> bool:
    """Return True iff `span` carries a tail-keep classification trigger: the 3
    §10.2 triggers (sandbox.violation / breaker.tripped / validator.fail-permanent)
    OR the §14.3 subagent-failure tail-keep.

    Pure predicate over an OTel `ReadableSpan`. Tolerant of missing
    attribute bag (returns False instead of raising). Used at the
    `TailKeepSpanProcessor` per-span inspection step to flag a trace for
    preservation on root close.

    Order of checks: span-name matches first (cheapest — single
    string-equality), then attribute lookup.
    """
    name = span.name
    if name == SANDBOX_VIOLATION_SPAN_NAME:
        return True
    if name == BREAKER_TRIPPED_SPAN_NAME:
        return True
    attrs = span.attributes
    if attrs is None:
        return False
    if attrs.get(VALIDATOR_FAIL_PERMANENCE_ATTR) == VALIDATOR_FAIL_PERMANENCE_PERMANENT_VALUE:
        return True
    # §14.3 (CP C-CP-14 `MULTI_AGENT_SPAN_SAMPLING`): a `subagent.span` is BASE_RATE
    # head-sampled with TAIL-KEEP ON FAILURE. Before B-TAIL this was crudely over-satisfied
    # by name-only always-sampling of every `subagent.span`; with the §9.2-root-only
    # refinement a non-root `subagent.span` now buffers, so its failure must trigger trace
    # preservation here (out-of-family Codex — else a failed nested subagent span drops,
    # regressing the §14.3 observability contract).
    return (
        name == SUBAGENT_SPAN_NAME
        and attrs.get(SUBAGENT_RESULT_STATUS_ATTR) == SUBAGENT_RESULT_STATUS_FAILED_VALUE
    )
