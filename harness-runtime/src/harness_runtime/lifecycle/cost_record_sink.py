"""`SupportsCostRecordAppend` — structural sink for per-dispatch cost records.

B-INTERSTEP-PERRUN-ISOLATION (B-INTERSTEP fork §3/§5; runtime spec §14.21
C-RT-34 invariant 7). A tiny leaf module (imports only `SpanCostRecord` from the
OD axis + `typing.Protocol`) so every per-dispatch cost wrapper can be typed
against it without importing `harness_runtime.types` — `types.py` imports several
of those wrapper modules (e.g. `webhook_delivery_composer`), so a wrapper →
`types` import would cycle.

Why a Protocol (not `list[SpanCostRecord]`)
-------------------------------------------
Before this arc the cost wrappers received `ctx.cost_record_accumulator.records`
— a list captured ONCE at bootstrap — and `.append`ed to it. That capture
defeated per-run isolation: on a reused bootstrap `HarnessContext` (daemon-client
mode) every run appended to the same list. The fix threads the run-scoped
accumulator PROXY (`RunScopedCostRecordAccumulator`) instead, whose `.append`
resolves the *current run's* accumulator at append-time. The wrappers therefore
need a param type satisfied by BOTH that proxy (a `CostRecordAccumulator`
subclass) AND a bare `list[SpanCostRecord]` (test fixtures still pass lists) —
i.e. "anything you can append a `SpanCostRecord` to".
"""

from __future__ import annotations

from typing import Protocol

from harness_od.idempotency_join_dedup import SpanCostRecord

__all__ = ["SupportsCostRecordAppend"]


class SupportsCostRecordAppend(Protocol):
    """Structural append-sink for `SpanCostRecord`s.

    Satisfied by `list[SpanCostRecord]` (test fixtures), `CostRecordAccumulator`,
    and `RunScopedCostRecordAccumulator` (the production run-scoped proxy).
    """

    def append(self, record: SpanCostRecord) -> None: ...
