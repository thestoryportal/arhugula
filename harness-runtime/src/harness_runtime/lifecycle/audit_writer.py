"""Audit-ledger writer instantiation — stage 4 OD (U-RT-32, closes L6).

Per `Spec_Harness_Runtime_v1.md` v1.1 §6 (C-RT-04 `audit_writer` field) + §7
(C-RT-07 stage 4 invariants) + the cross-axis edge at §700:
`ctx.audit_writer.append(tenant_id, audit_entry)` wraps an
`AuditLedgerEntry` (OD-spec'd at `harness_od.audit_ledger_types`) into a
`StateLedgerEntry` (IS C-IS-10 §10.1) so the OD audit-ledger reaches the IS
hash-chain durability substrate and `chain_verification` passes per
C-IS-06 §6.4.

**Wrap design.** The `AuditLedgerEntry` arrives pre-signed — the OD emission
site (U-OD-30) runs `sign_audit_entry` before handing the entry to the
runtime writer; this module is pure persistence. The wrap encodes:

- `EntryPayload.action_id = "audit:<tag>:<entry_hash>"` — tag is `tenant_id`
  or `_single`; entry_hash is the OD-computed `AuditLedgerEntry.entry_hash`.
- `EntryPayload.idempotency_key = "audit:<tag>:<entry_hash>"` — identical to
  action_id; the entry_hash already uniquely identifies the audit entry,
  but the tenant prefix scopes idempotency per-tenant (so two tenants can
  in principle reference the same OD entry without dedup-conflating them).
- `EntryPayload.actor` = the runtime's bound IS actor (from
  `LedgerWriter.actor` — committed at materialize_state_ledger time).
- `EntryPayload.timestamp` = `time_source()` (default `datetime.now(UTC)`).
- `WriteKey.thread_id = "audit:<tag>"`; `step_id = action_id`;
  `idempotency_key = action_id`.

**Cross-tenant separation discipline (C-OD-21 §21.1).** The OD spec commits
per-tenant TRACE separation (OTLP-collector-routing / backend-partition),
not per-tenant audit-ledger STORAGE. The audit ledger persists all tenants'
entries into one IS chain; tenant separation at the READ surface is
enforced by `read_for_tenant(tenant_id)`, which filters by the
`audit:<tag>:` prefix. The `cross_tenant_aggregation_forbidden=True` rule
at §21.1's `PerTenantSeparation` model holds at the reader API surface —
tenant A's reader does not return tenant B's entries.

**Signing is deferred (not at writer).** `sign_audit_entry` (C-OD-21 §21.2)
runs at the OD emission site and produces the `AuditSignatureAttributes`
that already live on the `AuditLedgerEntry` this module receives. The
writer does not re-sign and does not consult signing config; the live
signing backend (HSM / KMS / keystore) is deferred per ADR-D5 v1.3 §1.4.1.

**Verification.** `verify_hash_chain_integrity` (C-OD-21 §21.2) verifies
the OD audit-chain links via `AuditPayload.prior_entry_hash` / `entry_hash`.
The IS `verify_chain` (C-IS-06 §6.4) verifies the underlying IS chain.
Both pass independently; the writer composes them by ensuring every audit
entry round-trips through the IS chain via `append_ledger_entry`.

**Module convention.** One module per unit. `materialize_audit_writer_stage`
composer returns a frozen `AuditWriterStage` dataclass with `slots=True`.
Typed `AuditWriterBindError` for bootstrap-time failures. Mirrors the L6
stage shape established at U-RT-27..31.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import ClassVar

from harness_is.state_ledger_entry_schema import Identifier, StateLedgerEntry, Timestamp
from harness_is.state_ledger_write import (
    EntryPayload,
    WriteKey,
    WriteResult,
    read_ledger,
)
from harness_od.audit_ledger_types import AuditLedgerEntry

from harness_runtime.lifecycle.state_ledger import LedgerWriter
from harness_runtime.types import RuntimeConfig


class AuditWriterBindError(Exception):
    """Raised when audit-writer stage materialization fails."""


@dataclass(frozen=True, slots=True)
class RuntimeAuditLedgerWriter:
    """Runtime multi-tenant audit-ledger writer (C-RT-04 `audit_writer`).

    Wraps the IS `LedgerWriter` (U-RT-12). Persists OD `AuditLedgerEntry`
    instances into the underlying IS hash-chained JSONL ledger; provides a
    tenant-scoped reader surface for C-OD-21 §21.1 cross-tenant separation.
    """

    ledger_writer: LedgerWriter
    """IS state-ledger writer (U-RT-12) — durable substrate for audit entries."""

    time_source: Callable[[], Timestamp]
    """Timestamp injection point (test determinism). Default: `datetime.now(UTC)`."""

    _SINGLE_TENANT_TAG: ClassVar[str] = "_single"
    _ACTION_ID_PREFIX: ClassVar[str] = "audit"

    @classmethod
    def _tenant_tag(cls, tenant_id: str | None) -> str:
        """Resolve the tenant scoping tag for an append/read call."""
        return tenant_id if tenant_id else cls._SINGLE_TENANT_TAG

    @classmethod
    def _action_id_for(cls, tenant_id: str | None, audit_entry: AuditLedgerEntry) -> Identifier:
        """Build the IS action_id for an audit-entry wrap."""
        tag = cls._tenant_tag(tenant_id)
        return Identifier(f"{cls._ACTION_ID_PREFIX}:{tag}:{audit_entry.entry_hash}")

    def append(
        self,
        tenant_id: str | None,
        audit_entry: AuditLedgerEntry,
    ) -> WriteResult:
        """Persist one pre-signed `AuditLedgerEntry` into the IS hash chain.

        Returns the IS `WriteResult` — `APPENDED` on a fresh entry,
        `IDEMPOTENT_NOOP` on a replay of the same audit entry within the same
        tenant scope. The OD-computed `entry_hash` provides the deduplication
        key (scoped by tenant via the action_id prefix).
        """
        action_id = self._action_id_for(tenant_id, audit_entry)
        # R-003: `procedural_tier_snapshot_ref` is left `None`-canonical here
        # (IS spec v1.3 §C-IS-05 §5.1). This append wraps pre-signed OD audit
        # entries — a separate ledger family, not an active-workflow-context
        # producer emission — so the D-derivative sidecar does not apply.
        payload = EntryPayload(
            action_id=action_id,
            idempotency_key=action_id,
            actor=self.ledger_writer.actor,
            timestamp=self.time_source(),
        )
        write_key = WriteKey(
            thread_id=Identifier(f"{self._ACTION_ID_PREFIX}:{self._tenant_tag(tenant_id)}"),
            step_id=action_id,
            idempotency_key=action_id,
        )
        return self.ledger_writer.append(payload, write_key)

    def read_for_tenant(self, tenant_id: str | None) -> list[StateLedgerEntry]:
        """Tenant-scoped reader (C-OD-21 §21.1 cross-tenant separation surface).

        Returns every IS entry whose `action_id` begins with the tenant's
        `audit:<tag>:` prefix. Entries from other tenants are excluded.
        Reads the underlying JSONL file fresh (no in-process cache); safe
        across concurrent writers (the IS read returns a snapshot).
        """
        tag = self._tenant_tag(tenant_id)
        prefix = f"{self._ACTION_ID_PREFIX}:{tag}:"
        entries = read_ledger(self.ledger_writer.handle)
        return [e for e in entries if e.action_id.startswith(prefix)]

    def read_all(self) -> list[StateLedgerEntry]:
        """Cross-tenant reader — returns every persisted audit-wrapped entry.

        Use restricted to runtime-internal verification surfaces (e.g.,
        `RunResult.audit_ledger_head_hash` derivation per C-RT-09 §9.1).
        Tenant-scoped consumers must use `read_for_tenant`.
        """
        entries = read_ledger(self.ledger_writer.handle)
        prefix = f"{self._ACTION_ID_PREFIX}:"
        return [e for e in entries if e.action_id.startswith(prefix)]


@dataclass(frozen=True, slots=True)
class AuditWriterStage:
    """Frozen result of stage 4 OD audit-writer materialization.

    The bootstrap orchestrator (U-RT-43) binds `writer` to
    `HarnessContext.audit_writer`. Mirrors the L5 / L6 stage shape.
    """

    writer: RuntimeAuditLedgerWriter


def materialize_audit_writer_stage(
    config: RuntimeConfig,
    ledger_writer: LedgerWriter,
    *,
    time_source: Callable[[], Timestamp] | None = None,
) -> AuditWriterStage:
    """Build the stage 4 OD audit-writer registry.

    The writer is constructed against the pre-existing IS `LedgerWriter`
    from stage 1 (U-RT-12); no new IS handle is created here — the audit
    ledger shares the IS hash chain with the runtime's other audit/event
    emissions per the cross-axis edge §700 commitment.

    `config` is read for API consistency with the L5 / L6 composers; no
    field is consumed at HEAD (the writer is stateless beyond its bound
    `LedgerWriter` + injected `time_source`).
    """
    _ = config
    ts: Callable[[], Timestamp] = (
        time_source if time_source is not None else lambda: datetime.now(UTC)
    )
    return AuditWriterStage(
        writer=RuntimeAuditLedgerWriter(ledger_writer=ledger_writer, time_source=ts),
    )
