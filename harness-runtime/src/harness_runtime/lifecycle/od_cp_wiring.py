"""OD → CP cross-axis wiring — stage 6 (U-RT-38, L7 §12.6 — 3 edges, closes L7).

Per `Spec_Harness_Runtime_v1.md` v1.1 §12.6 (C-RT-12 OD → CP — 3 edges,
"inversion/manifest"):

- **Edge 1 — U-OD-09 → U-CP-54 (`harness.breaker.*` namespace inversion).**
  Producer: OD `harness.breaker.*` namespace export (F-CP-01 Stage 3b
  inversion — OD owns the canonical 7-attribute schema at C-OD-07 §7.1;
  CP exports the namespace declaration with
  `SUBSTRATE_ANCHORED_OUTSIDE_CP` posture). Consumer surface: CP
  namespace ingestion at composition. Payload: namespace declaration
  (typed; per CP C-CP-09 §9 `engine.*` pattern, applied to
  `harness.breaker.*`). Post-wiring invariant: CP ingestion of
  `harness.breaker.*` observable. Verification: confirm CP's declared
  `harness.breaker.*` entry's `attribute_count` matches the length of
  OD's `HARNESS_BREAKER_ATTRIBUTES` tuple (the canonical 7-attribute set).

- **Edge 2 — U-OD-34 → U-CP-54.** Producer: OD terminal-exporter
  manifest declaration (CP namespace target;
  `cross_axis_edge_targets` carrying `"U-CP-54"`). Consumer surface: CP
  namespace export manifest (per C-CP-24). Payload: manifest string
  reference. Post-wiring invariant: manifest reference resolves.

- **Edge 3 — U-OD-34 → U-CP-55.** Producer: OD terminal-exporter
  manifest declaration (CP carry-forward target — F2-12 inheritance;
  `cross_axis_edge_targets` carrying `"U-CP-55"`). Consumer surface: CP
  cross-axis composition manifest (per C-CP-24). Payload: manifest
  string reference (F2-12 carry-forward). Post-wiring invariant:
  manifest reference resolves; dashboard bindings observable.

**Wiring shape.** Edges 2 + 3 parallel U-RT-36's edge 2 (OD → IS
manifest string resolution) for two distinct CP targets. Edge 1 is the
F-CP-01 Stage 3b inversion verification — OD owns the canonical
`harness.breaker.*` schema; CP exports the namespace; the runtime
verifies they agree on attribute count. All bound symbols ARE the
U-RT-33-imported constants (Pattern P1 identity anchors for U-RT-51).

**Closes L7.** U-RT-38 is the last L7 stage 6 CXA_WIRING unit; with
this landing, §12.1 (terminal manifest imports — U-RT-33) + §12.2..§12.6
(per-axis wiring — U-RT-34..38) are all materialized.

**Module convention.** One module per unit.
`materialize_od_cp_wiring_stage` composer returns a frozen
`OdCpWiringStage` dataclass with `slots=True`. Typed
`OdCpWiringBindError` for bootstrap-time failures;
`OdCpManifestReferenceUnresolved` for edge-2/3 resolution failures;
`HarnessBreakerNamespaceInversionMismatch` for the §12.6 edge-1 typed
mismatch surface. Mirrors the L6 / L7 stage shape established at
U-RT-27..37.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from harness_cp.cp_cross_axis_composition_manifest import (
    CP_CROSS_AXIS_COMPOSITION_MANIFEST,
    CrossAxisCompositionExport,
)
from harness_cp.cp_namespace_export_manifest import (
    CP_NAMESPACE_EXPORT_MANIFEST,
    NamespaceExport,
)
from harness_od.harness_breaker_schema import HARNESS_BREAKER_ATTRIBUTES
from harness_od.substrate_seam_exports_aggregate_manifest import (
    SubstrateSeamExportsManifest,
)

from harness_runtime.types import RuntimeConfig


class OdCpWiringBindError(Exception):
    """Raised when OD → CP wiring stage materialization fails."""


class OdCpManifestReferenceUnresolved(Exception):  # noqa: N818 — domain-anchored name
    """Raised when an OD manifest string ID does not resolve to a known CP target."""


class HarnessBreakerNamespaceInversionMismatch(Exception):  # noqa: N818 — domain-anchored
    """Raised when CP's `harness.breaker.*` export disagrees with OD's schema.

    Per §12.6 edge 1 (F-CP-01 Stage 3b inversion): OD owns the canonical
    7-attribute `harness.breaker.*` schema (C-OD-07 §7.1); CP exports the
    namespace declaration. The runtime verifies they agree on
    attribute count.
    """


#: CP-side namespace string the OD aggregate manifest references.
_CP_NAMESPACE_MANIFEST_STRING_ID: Final[str] = "U-CP-54"

#: CP-side cross-axis composition manifest string the OD manifest references.
_CP_CROSS_AXIS_COMPOSITION_STRING_ID: Final[str] = "U-CP-55"

#: Pinned bindings from the §12.6 edge-2/3 string IDs to the CP manifest constants.
_OD_CP_MANIFEST_STRING_BINDINGS: Final[dict[str, object]] = {
    _CP_NAMESPACE_MANIFEST_STRING_ID: CP_NAMESPACE_EXPORT_MANIFEST,
    _CP_CROSS_AXIS_COMPOSITION_STRING_ID: CP_CROSS_AXIS_COMPOSITION_MANIFEST,
}

#: The CP-side namespace name for the §12.6 edge-1 inversion check.
_HARNESS_BREAKER_NAMESPACE_NAME: Final[str] = "harness.breaker.*"


@dataclass(frozen=True, slots=True)
class OdCpManifestReferenceResolution:
    """One resolved OD manifest export → CP target binding (§12.6 edges 2/3).

    `od_export_name` / `od_source_unit` identify the OD export sub-section
    declaring the reference. `cp_target_string_id` is the declarative
    string (`"U-CP-54"` or `"U-CP-55"`). `bound_cp_manifest` is the CP
    terminal manifest tuple the string resolves to.
    """

    od_export_name: str
    od_source_unit: str
    cp_target_string_id: str
    bound_cp_manifest: tuple[NamespaceExport, ...] | tuple[CrossAxisCompositionExport, ...]


@dataclass(frozen=True, slots=True)
class HarnessBreakerNamespaceInversion:
    """The §12.6 edge-1 typed F-CP-01 Stage 3b inversion record.

    `od_canonical_attribute_count` is the length of OD's
    `HARNESS_BREAKER_ATTRIBUTES` (the canonical 7-attribute schema per
    C-OD-07 §7.1). `cp_declared_attribute_count` is the count CP's
    namespace export entry advertises. `match` is `True` iff the two
    counts agree (the inversion contract). The composer raises
    `HarnessBreakerNamespaceInversionMismatch` on disagreement; this
    record is only constructed on match.
    """

    od_canonical_attribute_count: int
    cp_declared_attribute_count: int
    match: bool


@dataclass(frozen=True, slots=True)
class RuntimeOdCpWiring:
    """Runtime OD → CP wiring surface (C-RT-12 §12.6 — 3 edges).

    - `harness_breaker_inversion`: edge 1 record (F-CP-01 Stage 3b
      inversion verification).
    - `manifest_references`: edges 2 + 3 resolved OD → CP terminal
      manifest string-ID bindings.
    """

    harness_breaker_inversion: HarnessBreakerNamespaceInversion
    manifest_references: tuple[OdCpManifestReferenceResolution, ...]


def resolve_od_cp_manifest_references(
    od_manifest: SubstrateSeamExportsManifest,
) -> tuple[OdCpManifestReferenceResolution, ...]:
    """Walk OD manifest exports; bind `"U-CP-54"` + `"U-CP-55"` refs to CP manifests.

    Per spec §12.6 edges 2/3 post-wiring invariant: manifest reference
    resolves. Raises `OdCpManifestReferenceUnresolved` if any OD export
    declares a CP target string that this module does not know how to
    bind (defense against silent drift).
    """
    resolutions: list[OdCpManifestReferenceResolution] = []
    for export in od_manifest.exports:
        for target_string in export.cross_axis_edge_targets:
            if target_string in _OD_CP_MANIFEST_STRING_BINDINGS:
                bound = _OD_CP_MANIFEST_STRING_BINDINGS[target_string]
                if bound is None:
                    raise OdCpManifestReferenceUnresolved(
                        f"OD export {export.export_name!r} declares cross-axis "
                        f"target {target_string!r} but no CP terminal manifest "
                        f"binding is registered at the runtime"
                    )
                resolutions.append(
                    OdCpManifestReferenceResolution(
                        od_export_name=export.export_name,
                        od_source_unit=export.source_unit,
                        cp_target_string_id=target_string,
                        bound_cp_manifest=bound,  # type: ignore[arg-type]
                    )
                )
    return tuple(resolutions)


def verify_harness_breaker_namespace_inversion() -> HarnessBreakerNamespaceInversion:
    """Verify the §12.6 edge-1 F-CP-01 Stage 3b inversion at bootstrap.

    Confirms CP's `harness.breaker.*` namespace export advertises the
    same attribute count as OD's canonical `HARNESS_BREAKER_ATTRIBUTES`
    tuple (per C-OD-07 §7.1). Raises
    `HarnessBreakerNamespaceInversionMismatch` on disagreement or if CP
    does not declare the namespace at all.
    """
    od_count = len(HARNESS_BREAKER_ATTRIBUTES)
    cp_entry: NamespaceExport | None = next(
        (
            e
            for e in CP_NAMESPACE_EXPORT_MANIFEST
            if e.namespace_name == _HARNESS_BREAKER_NAMESPACE_NAME
        ),
        None,
    )
    if cp_entry is None:
        raise HarnessBreakerNamespaceInversionMismatch(
            f"CP namespace export manifest does not declare "
            f"{_HARNESS_BREAKER_NAMESPACE_NAME!r}; OD canonical schema "
            f"has {od_count} attributes (C-OD-07 §7.1)"
        )
    cp_count = cp_entry.attribute_count
    match = od_count == cp_count
    if not match:
        raise HarnessBreakerNamespaceInversionMismatch(
            f"`harness.breaker.*` attribute-count inversion mismatch: "
            f"OD canonical schema declares {od_count} attributes "
            f"(C-OD-07 §7.1); CP namespace export advertises {cp_count}"
        )
    return HarnessBreakerNamespaceInversion(
        od_canonical_attribute_count=od_count,
        cp_declared_attribute_count=cp_count,
        match=match,
    )


@dataclass(frozen=True, slots=True)
class OdCpWiringStage:
    """Frozen result of stage 6 OD → CP wiring materialization. Closes L7."""

    wiring: RuntimeOdCpWiring


def materialize_od_cp_wiring_stage(
    config: RuntimeConfig,
    od_manifest: SubstrateSeamExportsManifest,
) -> OdCpWiringStage:
    """Build the stage 6 OD → CP wiring stage (all 3 §12.6 edges).

    Runs the §12.6 edge-1 inversion verification + the edge-2/3
    manifest-reference resolution at composer call. Either surface
    failing raises a typed error so the bootstrap orchestrator fails
    fast at stage 6.

    `config` is read for API consistency with the L6 / L7 composers; no
    field is consumed at HEAD.
    """
    _ = config
    inversion = verify_harness_breaker_namespace_inversion()
    references = resolve_od_cp_manifest_references(od_manifest)
    return OdCpWiringStage(
        wiring=RuntimeOdCpWiring(
            harness_breaker_inversion=inversion,
            manifest_references=references,
        ),
    )
