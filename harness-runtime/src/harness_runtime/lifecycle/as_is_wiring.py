"""AS → IS cross-axis wiring — stage 6 (U-RT-34, L7 §12.2).

Per `Spec_Harness_Runtime_v1.md` v1.1 §12.2 (C-RT-12 AS → IS — 1 edge):
the runtime hands `ctx.ledger_writer.append` to the AS audit-emission site
via callback registration. Edge: U-AS-27 → U-IS-11; payload:
`StateLedgerEntry` (per C-IS-05 §5); post-wiring invariant: emitted event
appears in `.harness/state.jsonl` with chain integrity intact (verifiable
via C-IS-06 §6).

**Spec/plan wording drift (Class 3 — informational).** Spec §12.2 prose
describes the producer call site as "AS skill-load completion site
(skill-discovery emission)" carrying "skill-load metadata"; the actual
U-AS-27 plan-level unit body (`Implementation_Plan_Action_Surface_v1.md`
§2.6) implements per-fetch SECRET-fetch audit-ledger emission per
C-AS-08 §8.4 (carrier module: `harness_as.secret_fetch_audit` +
`harness_as.secret_fetch_emission`). The wiring CONTRACT
(`StateLedgerEntry` via U-IS-11 ledger append; chain integrity) is the
same in both readings; the descriptive prose mismatch is non-blocking
spec drift (no Class 1 fork — the materializable surface is secret-fetch
audit emission). Logged at the workspace `class_3_*` channel for future
spec revision pass.

**Wiring shape.** AS already exports `compose_secret_fetch_audit_entry`
(U-AS-26 per C-AS-08 §8.2) which returns a candidate `StateLedgerEntry`
from a `SecretFetchEvent`. AS does NOT perform the durable write per
U-AS-27 AC #5 — "ledger write delegates to U-IS-11"; the actual
`.harness/state.jsonl` append is a runtime concern. This module is the
callback-registration surface the runtime exposes: it composes via the
AS surface, extracts the IS-relevant fields into `EntryPayload`, builds
the `WriteKey` from the event's `(thread_id, step_id, idempotency_key)`,
and calls `ledger_writer.append`. Downstream tool-call sites invoke this
callback at emission time.

**Field-extraction discipline.** `compose_secret_fetch_audit_entry`
populates `response_hash` via the AS-side `compute_outputs_hash` (the
structure-not-content output hash per C-AS-08 §8.2). The IS append
contract at `harness_is.state_ledger_write.append_ledger_entry`
re-computes `response_hash` and `prior_event_hash` internally
(C-IS-07 §7.1 acceptance #8 of U-IS-11). The runtime therefore extracts
the IS-routable fields (`action_id`, `idempotency_key`, `actor`,
`timestamp`) from AS's composed entry; the AS-computed `response_hash`
is informational for AS-side downstream consumers and is NOT propagated
into the IS write (the IS chain owns its own hash discipline).

**Idempotency.** The AS `_idempotency_key` formula (`sha256` over
`(thread_id, step_id, secret_name, secret_scope.name)`) gives a stable
event identity; a replay of the same `SecretFetchEvent` yields the same
key and the IS append returns `IDEMPOTENT_NOOP` (C-IS-07 §7.1
acceptance #4). This satisfies U-AS-27 AC #5 — "duplicate writes no-op".

**Procedural-tier sidecar.** Active workflow-context AS→IS emissions receive
the R-003 procedural-tier snapshot resolver at bootstrap composition time and
populate `EntryPayload.procedural_tier_snapshot_ref`. Legacy/direct bootstrap
or unit-test wiring may omit the resolver; those non-workflow contexts retain
the prior `None`-canonical sidecar.

**Module convention.** One module per unit.
`materialize_as_is_wiring_stage` composer returns a frozen
`AsIsWiringStage` dataclass with `slots=True`. Typed `AsIsWiringBindError`
for bootstrap-time failures. Mirrors the L6 / L7 stage shape established
at U-RT-27..33.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from harness_as.secret_fetch_audit import (
    SecretFetchEvent,
    compose_secret_fetch_audit_entry,
)
from harness_is.state_ledger_entry_schema import Identifier, StateLedgerEntry
from harness_is.state_ledger_write import (
    EntryPayload,
    WriteKey,
    WriteResult,
)

from harness_runtime.lifecycle.state_ledger import LedgerWriter
from harness_runtime.types import RuntimeConfig


class AsIsWiringBindError(Exception):
    """Raised when AS → IS wiring stage materialization fails."""


@dataclass(frozen=True, slots=True)
class RuntimeAsIsWiring:
    """Runtime AS → IS callback-registration surface (C-RT-12 §12.2).

    Wraps the IS `LedgerWriter` (U-RT-12). Exposes
    `emit_secret_fetch_audit_entry`, which composes via the AS surface
    (`compose_secret_fetch_audit_entry`) and delegates the durable write
    to the IS ledger. The composition root binds this to AS emission sites
    at runtime; the AS module retains compose-only semantics per
    U-AS-27 AC #5.
    """

    ledger_writer: LedgerWriter
    """IS state-ledger writer (U-RT-12) — durable substrate for AS audit entries."""

    procedural_tier_snapshot_resolver: Callable[[], Identifier] | None = None
    """Optional R-003 procedural-tier resolver for workflow-context emissions."""

    def emit_secret_fetch_audit_entry(
        self,
        event: SecretFetchEvent,
        *,
        prior_entry: StateLedgerEntry | None = None,
    ) -> WriteResult:
        """Compose + persist one secret-fetch audit entry into the IS chain.

        Returns the IS `WriteResult` — `APPENDED` on a fresh event,
        `IDEMPOTENT_NOOP` on a replay (per U-AS-27 AC #5). The AS-computed
        `response_hash` is dropped at the IS boundary; the IS chain
        re-computes it per C-IS-07 §7.1 acceptance #8.
        """
        composed = compose_secret_fetch_audit_entry(event, prior_entry)
        procedural_tier_snapshot_ref = (
            self.procedural_tier_snapshot_resolver()
            if self.procedural_tier_snapshot_resolver is not None
            else None
        )
        payload = EntryPayload(
            action_id=composed.action_id,
            idempotency_key=composed.idempotency_key,
            actor=composed.actor,
            timestamp=composed.timestamp,
            procedural_tier_snapshot_ref=procedural_tier_snapshot_ref,
        )
        write_key = WriteKey(
            thread_id=event.thread_id,
            step_id=event.step_id,
            idempotency_key=composed.idempotency_key,
        )
        return self.ledger_writer.append(payload, write_key)


@dataclass(frozen=True, slots=True)
class AsIsWiringStage:
    """Frozen result of stage 6 AS → IS wiring materialization.

    The bootstrap orchestrator (U-RT-43) binds `wiring` to the composition
    root so AS emission sites can route via the runtime callback. Mirrors
    the L6 / L7 stage shape.
    """

    wiring: RuntimeAsIsWiring


def materialize_as_is_wiring_stage(
    config: RuntimeConfig,
    ledger_writer: LedgerWriter,
    procedural_tier_snapshot_resolver: Callable[[], Identifier] | None = None,
) -> AsIsWiringStage:
    """Build the stage 6 AS → IS wiring registry.

    Constructed against the pre-existing IS `LedgerWriter` from stage 1
    (U-RT-12); no new IS handle is created. AS audit entries share the
    IS hash chain with the runtime's other event emissions per the
    cross-axis edge §12.2 commitment.

    `config` is read for API consistency with the L6 / L7 composers; no
    field is consumed at HEAD.
    """
    _ = config
    return AsIsWiringStage(
        wiring=RuntimeAsIsWiring(
            ledger_writer=ledger_writer,
            procedural_tier_snapshot_resolver=procedural_tier_snapshot_resolver,
        ),
    )
