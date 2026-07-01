"""OD → AS cross-axis wiring — stage 6 (U-RT-37, L7 §12.5 — 1 edge).

Per `Spec_Harness_Runtime_v1.md` v1.1 §12.5 (C-RT-12 OD → AS — 1 edge):

- **Edge — U-OD-34 → U-AS-33.** Producer: OD terminal-exporter manifest
  declaration (AS namespace verification target;
  `SubstrateSeamExport.cross_axis_edge_targets` carrying the string
  `"U-AS-33"`). Consumer surface: AS namespace exports surface (per
  C-AS-16 §16). Payload: manifest string reference (not a value).
  Post-wiring invariant: AS namespace verification runs at bootstrap;
  mismatch surfaces typed.

**Wiring shape.** Parallel to U-RT-36's edge 2 (OD → IS manifest string
resolution) but for the AS axis. The runtime resolves OD's
`cross_axis_edge_targets` strings containing `"U-AS-33"` to the AS
terminal manifest constant
(`harness_as.as_substrate_seam_exports.AS_SUBSTRATE_SEAM_EXPORTS`). The
bound manifest IS the U-RT-33-imported symbol (Pattern P1 identity
anchor for the U-RT-51 verification suite).

**AS namespace verification.** Spec §12.5 commits "AS namespace
verification runs at bootstrap; mismatch surfaces typed". At HEAD the
verification confirms (a) the bound AS manifest is non-empty and (b) the
declared seam ID set covers the 7 `ASSeamId` enum values (per C-AS-16
§16.1-§16.7). A coverage mismatch raises `AsNamespaceVerificationMismatch`
— a typed error that surfaces at bootstrap stage 6 rather than at a
downstream consumer site. Spec leaves the verification mechanism to
"AS namespace exports surface (per C-AS-16 §16)" without further
constraint; this implementation reads the AS-side `ASSeamId` enum + the
manifest tuple as the authority for namespace coverage.

**Module convention.** One module per unit.
`materialize_od_as_wiring_stage` composer returns a frozen
`OdAsWiringStage` dataclass with `slots=True`. Typed
`OdAsWiringBindError` for bootstrap-time failures;
`OdAsManifestReferenceUnresolved` for edge resolution failures;
`AsNamespaceVerificationMismatch` for the §12.5 typed mismatch surface.
Mirrors the L6 / L7 stage shape established at U-RT-27..36.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from harness_as.as_substrate_seam_exports import (
    AS_SUBSTRATE_SEAM_EXPORTS,
    ASSeamId,
    ASSubstrateSeamExport,
)
from harness_od.substrate_seam_exports_aggregate_manifest import (
    SubstrateSeamExportsManifest,
)

from harness_runtime.types import RuntimeConfig


class OdAsWiringBindError(Exception):
    """Raised when OD → AS wiring stage materialization fails."""


class OdAsManifestReferenceUnresolved(Exception):  # noqa: N818 — domain-anchored name
    """Raised when an OD manifest string ID does not resolve to a known AS target."""


class AsNamespaceVerificationMismatch(Exception):  # noqa: N818 — domain-anchored name
    """Raised when AS namespace verification at bootstrap surfaces a mismatch.

    Per spec §12.5 post-wiring invariant: "AS namespace verification runs
    at bootstrap; mismatch surfaces typed." The verification covers the
    `ASSeamId` enum vs the seam IDs declared by the AS terminal manifest.
    """


#: The AS-side string ID that the OD manifest references for U-AS-33.
_AS_TERMINAL_MANIFEST_STRING_ID: Final[str] = "U-AS-33"

#: Pinned binding from the §12.5 edge string ID to the AS terminal manifest.
_OD_AS_MANIFEST_STRING_BINDINGS: Final[dict[str, tuple[ASSubstrateSeamExport, ...]]] = {
    _AS_TERMINAL_MANIFEST_STRING_ID: AS_SUBSTRATE_SEAM_EXPORTS,
}


@dataclass(frozen=True, slots=True)
class OdAsManifestReferenceResolution:
    """One resolved OD manifest export → AS terminal-manifest binding (§12.5).

    `od_export_name` identifies the OD export sub-section that carries the
    AS string reference. `od_source_unit` is the OD source unit declaring
    the reference. `as_manifest_string_id` is the declarative string
    (e.g. `"U-AS-33"`). `bound_as_manifest` is the AS terminal manifest
    constant the string resolves to (Pattern P1 anchor).
    """

    od_export_name: str
    od_source_unit: str
    as_manifest_string_id: str
    bound_as_manifest: tuple[ASSubstrateSeamExport, ...]


@dataclass(frozen=True, slots=True)
class AsNamespaceVerificationResult:
    """The §12.5 typed AS namespace verification record.

    `declared_seam_ids` is the AS-manifest-declared set of seam IDs (the
    `seam_id` values from `AS_SUBSTRATE_SEAM_EXPORTS`). `enum_seam_ids` is
    the `ASSeamId` enum value set. `coverage_match` is `True` iff the two
    sets are equal — the §12.5 mismatch condition. When `coverage_match`
    is `False`, the composer raises `AsNamespaceVerificationMismatch` and
    this record is not returned.
    """

    declared_seam_ids: frozenset[ASSeamId]
    enum_seam_ids: frozenset[ASSeamId]
    coverage_match: bool


@dataclass(frozen=True, slots=True)
class RuntimeOdAsWiring:
    """Runtime OD → AS wiring surface (C-RT-12 §12.5 — 1 edge).

    Bundles the §12.5 edge consumer surfaces:

    - `manifest_references`: the resolved OD → AS terminal manifest
      string-ID bindings. Empty tuple if the OD manifest declares no
      `"U-AS-33"` references.
    - `namespace_verification`: the typed bootstrap-time verification
      result — present iff the AS namespace coverage matches the
      `ASSeamId` enum (otherwise the composer raises and no stage
      materializes).
    """

    manifest_references: tuple[OdAsManifestReferenceResolution, ...]
    namespace_verification: AsNamespaceVerificationResult


def resolve_od_as_manifest_references(
    od_manifest: SubstrateSeamExportsManifest,
) -> tuple[OdAsManifestReferenceResolution, ...]:
    """Walk OD manifest exports; bind every `"U-AS-33"` ref to the AS manifest.

    Per spec §12.5 post-wiring invariant: manifest string ID resolves at
    composition. Raises `OdAsManifestReferenceUnresolved` if any OD
    export carries an AS-axis target string that this module does not
    know how to bind (defense against silent drift between OD manifest
    declarations and the AS terminal-manifest surface).
    """
    resolutions: list[OdAsManifestReferenceResolution] = []
    for export in od_manifest.exports:
        for target_string in export.cross_axis_edge_targets:
            if target_string == _AS_TERMINAL_MANIFEST_STRING_ID:
                bound = _OD_AS_MANIFEST_STRING_BINDINGS.get(target_string)
                if bound is None:
                    raise OdAsManifestReferenceUnresolved(
                        f"OD export {export.export_name!r} declares cross-axis "
                        f"target {target_string!r} but no AS terminal manifest "
                        f"binding is registered at the runtime"
                    )
                resolutions.append(
                    OdAsManifestReferenceResolution(
                        od_export_name=export.export_name,
                        od_source_unit=export.source_unit,
                        as_manifest_string_id=target_string,
                        bound_as_manifest=bound,
                    )
                )
    return tuple(resolutions)


def verify_as_namespace_coverage() -> AsNamespaceVerificationResult:
    """Verify the AS namespace coverage at bootstrap (§12.5 typed mismatch).

    Compares the `ASSeamId` enum value set against the seam IDs declared
    by the AS terminal manifest (`AS_SUBSTRATE_SEAM_EXPORTS`). Coverage
    holds iff the two sets are equal. Returns the typed verification
    record on success; raises `AsNamespaceVerificationMismatch` on
    mismatch.
    """
    declared = frozenset(export.seam_id for export in AS_SUBSTRATE_SEAM_EXPORTS)
    enumerated = frozenset(ASSeamId)
    match = declared == enumerated
    result = AsNamespaceVerificationResult(
        declared_seam_ids=declared,
        enum_seam_ids=enumerated,
        coverage_match=match,
    )
    if not match:
        missing = enumerated - declared
        extra = declared - enumerated
        raise AsNamespaceVerificationMismatch(
            "AS namespace coverage mismatch at bootstrap: "
            f"missing seam IDs={sorted(s.value for s in missing)!r}, "
            f"unexpected seam IDs={sorted(s.value for s in extra)!r}"
        )
    return result


@dataclass(frozen=True, slots=True)
class OdAsWiringStage:
    """Frozen result of stage 6 OD → AS wiring materialization."""

    wiring: RuntimeOdAsWiring


def materialize_od_as_wiring_stage(
    config: RuntimeConfig,
    od_manifest: SubstrateSeamExportsManifest,
) -> OdAsWiringStage:
    """Build the stage 6 OD → AS wiring stage (§12.5 edge).

    Runs both the OD manifest-reference resolution (edge consumer
    surface) and the AS namespace verification (post-wiring invariant)
    at composer call. Either surface failing raises a typed error so the
    bootstrap orchestrator fails fast at stage 6.

    `config` is read for API consistency with the L6 / L7 composers; no
    field is consumed at HEAD.
    """
    _ = config
    references = resolve_od_as_manifest_references(od_manifest)
    verification = verify_as_namespace_coverage()
    return OdAsWiringStage(
        wiring=RuntimeOdAsWiring(
            manifest_references=references,
            namespace_verification=verification,
        ),
    )
