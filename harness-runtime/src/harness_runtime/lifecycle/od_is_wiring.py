"""OD → IS cross-axis wiring — stage 6 (U-RT-36, L7 §12.4 — 2 edges).

Per `Spec_Harness_Runtime_v1.md` v1.1 §12.4 (C-RT-12 OD → IS — 2 edges):

- **Edge 1 — U-OD-30 → U-IS-11.** Producer: OD audit-emission site
  (`harness_od.multi_tenant_trace_separation_and_audit_ledger.sign_audit_entry`
  + the OD AuditLedgerEntry compose path). Consumer surface:
  `ctx.audit_writer.append(tenant_id, audit_entry)`. Payload:
  `AuditLedgerEntry` wrapping into `StateLedgerEntry`. Post-wiring
  invariant: OD audit entry reaches IS chain; `chain_verification`
  passes per C-IS-06 §6.4.

  **Edge 1 is materialized at U-RT-32.** The
  `RuntimeAuditLedgerWriter.append` surface (from
  `harness_runtime.lifecycle.audit_writer`) IS the §12.4 edge 1
  consumer. U-RT-36 re-cites that surface here so the bootstrap
  orchestrator can bind it under the OD → IS wiring stage; no second
  audit-writer is constructed.

- **Edge 2 — U-OD-34 → U-IS-17.** Producer: OD terminal-exporter
  manifest declaration (`SubstrateSeamExport.cross_axis_edge_targets`
  carrying the string `"U-IS-17"`). Consumer surface: IS terminal-
  exporter manifest string reference resolution. Payload: manifest
  string reference (not a value). Post-wiring invariant: manifest
  string ID resolves at composition; downstream import-time consumers
  see a consistent string-ID → IS manifest binding.

  **Edge 2 implementation.** `resolve_od_is_manifest_references` walks
  the OD `SubstrateSeamExportsManifest.exports`, collects every export
  whose `cross_axis_edge_targets` includes `"U-IS-17"`, and binds each
  to the IS terminal manifest constant
  (`harness_is.substrate_seam_exports.IS_SUBSTRATE_SEAM_EXPORTS_MANIFEST`).
  An unresolvable string ID raises `OdIsManifestReferenceUnresolved` —
  a typed error per spec §12 invariants. The bound IS-manifest constant
  IS the U-RT-33-imported symbol (Pattern P1 identity-equality anchor
  for the U-RT-51 verification suite).

**Module convention.** One module per unit.
`materialize_od_is_wiring_stage` composer returns a frozen
`OdIsWiringStage` dataclass with `slots=True`. Typed
`OdIsWiringBindError` for bootstrap-time failures;
`OdIsManifestReferenceUnresolved` for edge-2 resolution failures.
Mirrors the L6 / L7 stage shape established at U-RT-27..35.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from harness_is.substrate_seam_exports import (
    IS_SUBSTRATE_SEAM_EXPORTS_MANIFEST,
)
from harness_is.substrate_seam_exports import (
    SubstrateSeamExport as ISSubstrateSeamExport,
)
from harness_od.substrate_seam_exports_aggregate_manifest import (
    SubstrateSeamExportsManifest,
)

from harness_runtime.lifecycle.audit_writer import RuntimeAuditLedgerWriter
from harness_runtime.types import RuntimeConfig


class OdIsWiringBindError(Exception):
    """Raised when OD → IS wiring stage materialization fails."""


class OdIsManifestReferenceUnresolved(Exception):  # noqa: N818 — domain-anchored name
    """Raised when an OD manifest string ID does not resolve to a known target."""


#: The IS-side string ID that the OD manifest references for U-IS-17.
_IS_TERMINAL_MANIFEST_STRING_ID: Final[str] = "U-IS-17"

#: Pinned binding from the §12.4 edge-2 string ID to the IS terminal manifest.
#: At HEAD only `"U-IS-17"` is in scope per spec §12.4 (OD → IS, edge 2).
_OD_IS_MANIFEST_STRING_BINDINGS: Final[dict[str, tuple[ISSubstrateSeamExport, ...]]] = {
    _IS_TERMINAL_MANIFEST_STRING_ID: IS_SUBSTRATE_SEAM_EXPORTS_MANIFEST,
}


@dataclass(frozen=True, slots=True)
class OdIsManifestReferenceResolution:
    """One resolved OD manifest export → IS terminal-manifest binding (edge 2).

    `od_export_name` identifies the OD export sub-section (per C-OD-23 §23.1)
    that carries the IS string reference. `od_source_unit` is the OD source
    unit declaring the reference. `is_manifest_string_id` is the
    declarative string (e.g. `"U-IS-17"`). `bound_is_manifest` is the IS
    terminal manifest constant the string resolves to (Pattern P1 anchor).
    """

    od_export_name: str
    od_source_unit: str
    is_manifest_string_id: str
    bound_is_manifest: tuple[ISSubstrateSeamExport, ...]


@dataclass(frozen=True, slots=True)
class RuntimeOdIsWiring:
    """Runtime OD → IS wiring surface (C-RT-12 §12.4 — 2 edges).

    Bundles the two §12.4 edge consumer surfaces:

    - `audit_writer`: the U-RT-32 `RuntimeAuditLedgerWriter` re-cited
      under the OD → IS stage (edge 1 — `ctx.audit_writer.append`).
    - `manifest_references`: the resolved OD → IS terminal manifest
      string-ID bindings (edge 2). Empty tuple if the OD manifest
      declares no `"U-IS-17"` references (spec compliance still holds —
      the verifier ran and found zero refs to resolve).
    """

    audit_writer: RuntimeAuditLedgerWriter
    """Edge 1 — `RuntimeAuditLedgerWriter` from U-RT-32."""

    manifest_references: tuple[OdIsManifestReferenceResolution, ...]
    """Edge 2 — resolved OD → IS manifest string-ID bindings."""


def resolve_od_is_manifest_references(
    od_manifest: SubstrateSeamExportsManifest,
) -> tuple[OdIsManifestReferenceResolution, ...]:
    """Walk OD manifest exports; bind every `"U-IS-17"` ref to the IS manifest.

    Per spec §12.4 edge 2 post-wiring invariant: "Manifest string ID
    resolves at composition; downstream import-time consumers see
    consistent string." Raises `OdIsManifestReferenceUnresolved` if any
    OD export carries an IS-axis target string that this module does
    not know how to bind (defense against silent drift between OD
    manifest declarations and the IS terminal-manifest surface).
    """
    resolutions: list[OdIsManifestReferenceResolution] = []
    for export in od_manifest.exports:
        for target_string in export.cross_axis_edge_targets:
            if target_string == _IS_TERMINAL_MANIFEST_STRING_ID:
                bound = _OD_IS_MANIFEST_STRING_BINDINGS.get(target_string)
                if bound is None:
                    raise OdIsManifestReferenceUnresolved(
                        f"OD export {export.export_name!r} declares cross-axis "
                        f"target {target_string!r} but no IS terminal manifest "
                        f"binding is registered at the runtime"
                    )
                resolutions.append(
                    OdIsManifestReferenceResolution(
                        od_export_name=export.export_name,
                        od_source_unit=export.source_unit,
                        is_manifest_string_id=target_string,
                        bound_is_manifest=bound,
                    )
                )
    return tuple(resolutions)


@dataclass(frozen=True, slots=True)
class OdIsWiringStage:
    """Frozen result of stage 6 OD → IS wiring materialization.

    The bootstrap orchestrator (U-RT-43) binds `wiring` to the composition
    root so OD audit-emission sites can route via `audit_writer.append`
    (edge 1) and OD manifest declarations have their string refs resolved
    (edge 2). Mirrors the L6 / L7 stage shape.
    """

    wiring: RuntimeOdIsWiring


def materialize_od_is_wiring_stage(
    config: RuntimeConfig,
    audit_writer: RuntimeAuditLedgerWriter,
    od_manifest: SubstrateSeamExportsManifest,
) -> OdIsWiringStage:
    """Build the stage 6 OD → IS wiring stage (both §12.4 edges).

    `audit_writer` is the pre-existing U-RT-32 writer (re-cited under
    the OD → IS stage for edge 1). `od_manifest` is the OD terminal
    aggregate exporter manifest from U-RT-33's import set
    (`harness_od.substrate_seam_exports_aggregate_manifest.OD_SUBSTRATE_SEAM_EXPORTS_MANIFEST`).
    Composer resolves edge 2's manifest string references at construction
    time so any unresolved string fails bootstrap fast.

    `config` is read for API consistency with the L6 / L7 composers; no
    field is consumed at HEAD.
    """
    _ = config
    references = resolve_od_is_manifest_references(od_manifest)
    return OdIsWiringStage(
        wiring=RuntimeOdIsWiring(
            audit_writer=audit_writer,
            manifest_references=references,
        ),
    )
