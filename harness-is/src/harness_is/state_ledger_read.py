"""C2-pole selective bounded read contract — U-IS-12.

Implements C-IS-07 §7.2 (the C2-pole read contract — selective, bounded,
navigation-primitive-mediated) + §7.3 (composition format — indexable
per-entry access). Declares the `NavigationPrimitive` interface, the query /
window / result records, and four minimum-viable concrete primitives.

**Ledger source — in-memory loaded ledger.** The `NavigationPrimitive`
operates over a loaded, ordered `tuple[StateLedgerEntry, ...]`. The JSONL-file
→ `StateLedgerEntry` *load* (the §7.3 storage representation — one
JSON-serialized entry per line) is "emergent from write+read coordination"
(IS plan v2.3 §2.3 decomposition rationale) and is co-owned with U-IS-11 (the
C3-pole append-only write contract), which is not yet landed. U-IS-12 owns the
§7.2 read contract — selectivity, bounding, navigation-mediation — over a
loaded ledger; positions are 1-indexed (the §7.3 indexable-access property).

Reads are selective by construction: every read requires a `NavigationQuery`
*and* a `BoundedWindow`; there is no API that returns the ledger without them
(acceptance #1 — full-file `cat`-style reads precluded).

Authority: Implementation_Plan_Information_Substrate_v2_3.md §2.2 U-IS-12
(REVISED — R2); Spec_Information_Substrate_v1.md C-IS-07 §7.2 / §7.3;
ADR-F2 v1.2 §Consequences (c) + §Rationale (b)(ii).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from harness_core import WorkloadClass
from pydantic import BaseModel, ConfigDict

from harness_is.state_ledger_entry_schema import Identifier, StateLedgerEntry


class PositionRange(BaseModel):
    """An inclusive 1-indexed ledger position range."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start_position: int
    end_position: int


class NavigationQuery(BaseModel):
    """A selective read query — exactly one selector is consulted per read."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    by_action_id: Identifier | None = None
    by_idempotency_key: Identifier | None = None
    by_position_range: PositionRange | None = None
    most_recent_n: int | None = None


class BoundedWindow(BaseModel):
    """The bounding window for a read (C-IS-07 §7.2 — bounded)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_entries: int
    workload_class: WorkloadClass


class ReadResult(BaseModel):
    """The result of a navigation-primitive read."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entries: tuple[StateLedgerEntry, ...]
    truncated: bool
    next_position: int | None


class NavigationPrimitive(ABC):
    """The C2-pole read interface — every read passes through `read` (§7.2)."""

    @abstractmethod
    def read(self, query: NavigationQuery, bounded_window: BoundedWindow) -> ReadResult:
        """Selectively read a bounded window of ledger entries."""
        raise NotImplementedError


class LedgerNavigationPrimitive(NavigationPrimitive):
    """`NavigationPrimitive` over an in-memory loaded state-ledger.

    The ledger is held as an immutable tuple — reads take no lock and never
    mutate it, so concurrent reads (and reads concurrent with an
    append-elsewhere write) never block (acceptance #6/#7).
    """

    def __init__(self, ledger: tuple[StateLedgerEntry, ...]) -> None:
        self._ledger = ledger

    def _select(self, query: NavigationQuery) -> list[tuple[int, StateLedgerEntry]]:
        """Apply the query, returning `(1-indexed position, entry)` matches."""
        indexed = list(enumerate(self._ledger, start=1))
        if query.by_action_id is not None:
            return [(p, e) for p, e in indexed if e.action_id == query.by_action_id]
        if query.by_idempotency_key is not None:
            return [(p, e) for p, e in indexed if e.idempotency_key == query.by_idempotency_key]
        if query.by_position_range is not None:
            lo = query.by_position_range.start_position
            hi = query.by_position_range.end_position
            return [(p, e) for p, e in indexed if lo <= p <= hi]
        if query.most_recent_n is not None:
            return indexed[-query.most_recent_n :] if query.most_recent_n > 0 else []
        # No selector — selectivity is mandatory; an unscoped read returns nothing.
        return []

    def read(self, query: NavigationQuery, bounded_window: BoundedWindow) -> ReadResult:
        """Selectively read a bounded window (C-IS-07 §7.2).

        Applies the query selector, then truncates to `bounded_window.max_entries`.
        On truncation `next_position` is the 1-indexed ledger position of the
        first un-returned match — a follow-up read continues from there.
        """
        matches = self._select(query)
        within = matches[: bounded_window.max_entries]
        truncated = len(matches) > bounded_window.max_entries
        next_position = matches[bounded_window.max_entries][0] if truncated else None
        return ReadResult(
            entries=tuple(e for _, e in within),
            truncated=truncated,
            next_position=next_position,
        )

    def read_entry(self, action_id: Identifier, bounded_window: BoundedWindow) -> ReadResult:
        """Read entries by `action_id`."""
        return self.read(NavigationQuery(by_action_id=action_id), bounded_window)

    def read_range(
        self, start_position: int, end_position: int, bounded_window: BoundedWindow
    ) -> ReadResult:
        """Read entries in an inclusive 1-indexed position range."""
        return self.read(
            NavigationQuery(
                by_position_range=PositionRange(
                    start_position=start_position, end_position=end_position
                )
            ),
            bounded_window,
        )

    def read_recent(self, n: int, bounded_window: BoundedWindow) -> ReadResult:
        """Read the most recent `n` entries, chronological order."""
        return self.read(NavigationQuery(most_recent_n=n), bounded_window)

    def read_by_idempotency_key(
        self, idempotency_key: Identifier, bounded_window: BoundedWindow
    ) -> ReadResult:
        """Read entries by `idempotency_key`."""
        return self.read(NavigationQuery(by_idempotency_key=idempotency_key), bounded_window)
