"""Chain-link construction primitive — U-IS-09.

Implements C-IS-06 §6.3 (chain construction at write-time). Declares
`construct_prior_event_hash` — the pure write-time function that produces the
`prior_event_hash` field value for a new state-ledger entry.

Pure: no I/O, no global state, no logging side effects. The caller (the
C-IS-07 C3-pole write contract, U-IS-11) inserts the returned value into the
new entry's `prior_event_hash` field and is responsible for serializing
concurrent writers on the chain head.

Authority: Implementation_Plan_Information_Substrate_v2_3.md §2.1 U-IS-09
(preserved verbatim from v2.1 §2); Spec_Information_Substrate_v1.md C-IS-06
§6.3; ADR-F2 v1.2 §Rationale (a.1).
"""

from __future__ import annotations

from harness_is.entry_hash import compute_response_hash
from harness_is.state_ledger_entry_schema import (
    ALL_ZEROS_SENTINEL,
    Bytes32,
    StateLedgerEntry,
)


def construct_prior_event_hash(prior_entry: StateLedgerEntry | None) -> Bytes32:
    """Produce the `prior_event_hash` for a new entry (C-IS-06 §6.3).

    Chain inception (`prior_entry is None`) → `ALL_ZEROS_SENTINEL`; otherwise
    → `compute_response_hash(prior_entry)`, the SHA-256 of the prior entry's
    canonical form. Pure — no I/O, no state mutation.
    """
    if prior_entry is None:
        return ALL_ZEROS_SENTINEL
    return compute_response_hash(prior_entry)
