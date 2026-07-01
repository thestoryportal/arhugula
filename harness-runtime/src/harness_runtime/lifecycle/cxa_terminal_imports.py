"""CXA terminal aggregate exporter manifest import — stage 6 (U-RT-33, opens L7).

Per `Spec_Harness_Runtime_v1.md` v1.1 §12 (C-RT-12 stage 6 CXA_WIRING) +
§12.1 (terminal aggregate exporter manifest import — side-effect): the
composition root imports the 5 terminal aggregate exporter manifests so
their import-time side-effects realize. Per CXA v2.3 §3, the 22 genuine
Pattern P1 typed seams are realized at module-import time; this module's
import of the consumer manifests is what causes them to load and bind
their producer references.

**The 5 terminal manifests (per spec §12.1 table).**

- IS substrate seam exports — `harness_is.substrate_seam_exports`
  exposes `IS_SUBSTRATE_SEAM_EXPORTS_MANIFEST`.
- AS substrate seam exports — `harness_as.as_substrate_seam_exports`
  exposes `AS_SUBSTRATE_SEAM_EXPORTS`.
- CP namespace export manifest — `harness_cp.cp_namespace_export_manifest`
  exposes `CP_NAMESPACE_EXPORT_MANIFEST`.
- CP cross-axis composition manifest —
  `harness_cp.cp_cross_axis_composition_manifest` exposes
  `CP_CROSS_AXIS_COMPOSITION_MANIFEST`.
- OD substrate seam exports aggregate —
  `harness_od.substrate_seam_exports_aggregate_manifest` exposes
  `OD_SUBSTRATE_SEAM_EXPORTS_MANIFEST`.

**Side-effect-only at stage 6.** The runtime's job at this stage is to
realize the import; no per-edge wiring happens here (that is U-RT-34..38,
§12.2—§12.6). Verification of Pattern P1 identity-equality (consumer's
imported symbol IS the producer's exported symbol) is deferred to U-RT-51
per spec §12.1 final paragraph — verification lives in tests, not in
runtime code. This module exposes the 5 imported manifest references so
tests can perform the U-RT-51 identity checks without re-importing.

**Failure surface.** A manifest module failing to import surfaces as a
plain Python `ImportError` at module load — the composition root cannot
proceed past stage 6. `CxaTerminalImportError` is typed for symptoms that
survive past import (e.g., a manifest module loads but its expected
top-level constant is missing); the L6 composer pattern is reused for
shape consistency, but at HEAD all 5 manifests + their constants are
materialized so the typed error path is currently dormant.

**Module convention.** One module per unit. `materialize_cxa_terminal_imports_stage`
composer returns a frozen `CxaTerminalImportsStage` dataclass with
`slots=True`. Mirrors the L6 stage shape established at U-RT-27..32.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import ModuleType
from typing import Final

from harness_as import as_substrate_seam_exports as _as_seam_exports
from harness_cp import (
    cp_cross_axis_composition_manifest as _cp_xa_manifest,
)
from harness_cp import (
    cp_namespace_export_manifest as _cp_ns_manifest,
)
from harness_is import substrate_seam_exports as _is_seam_exports
from harness_od import substrate_seam_exports_aggregate_manifest as _od_seam_exports

from harness_runtime.types import RuntimeConfig


class CxaTerminalImportError(Exception):
    """Raised when terminal-manifest materialization detects a structural defect.

    Currently dormant — all 5 manifests + their top-level constants
    materialize cleanly at HEAD. Reserved for the case where a manifest
    module loads but its expected `*_MANIFEST` constant is absent or
    mis-typed at a future revision.
    """


#: The 5 terminal aggregate exporter manifest modules, in spec §12.1 order.
TERMINAL_MANIFEST_MODULES: Final[tuple[ModuleType, ...]] = (
    _is_seam_exports,
    _as_seam_exports,
    _cp_ns_manifest,
    _cp_xa_manifest,
    _od_seam_exports,
)

#: Per-manifest top-level constant references (test-side identity check anchor).
#: Keys are the qualified module names; values are the manifest constants.
TERMINAL_MANIFEST_CONSTANTS: Final[dict[str, object]] = {
    "harness_is.substrate_seam_exports": (_is_seam_exports.IS_SUBSTRATE_SEAM_EXPORTS_MANIFEST),
    "harness_as.as_substrate_seam_exports": (_as_seam_exports.AS_SUBSTRATE_SEAM_EXPORTS),
    "harness_cp.cp_namespace_export_manifest": (_cp_ns_manifest.CP_NAMESPACE_EXPORT_MANIFEST),
    "harness_cp.cp_cross_axis_composition_manifest": (
        _cp_xa_manifest.CP_CROSS_AXIS_COMPOSITION_MANIFEST
    ),
    "harness_od.substrate_seam_exports_aggregate_manifest": (
        _od_seam_exports.OD_SUBSTRATE_SEAM_EXPORTS_MANIFEST
    ),
}


@dataclass(frozen=True, slots=True)
class CxaTerminalImportsStage:
    """Frozen result of stage 6 CXA terminal-manifest import.

    The bootstrap orchestrator (U-RT-43) treats successful construction of
    this stage as the stage 6 completion signal for §12.1; per-edge wiring
    at §12.2—§12.6 (U-RT-34..38) consumes the same composition root and
    proceeds in stage-6 order. Mirrors the L5 / L6 stage shape.
    """

    imported_manifests: tuple[ModuleType, ...]
    """The 5 terminal manifest modules (in spec §12.1 table order)."""

    manifest_constants: dict[str, object]
    """Per-module top-level manifest constant references for test identity checks."""


def materialize_cxa_terminal_imports_stage(
    config: RuntimeConfig,
) -> CxaTerminalImportsStage:
    """Realize the 5 terminal-manifest module imports for stage 6 CXA_WIRING.

    The substantive work (import-time Pattern P1 side-effect realization)
    happens at module load — by the time this composer runs, the 5
    manifests are already imported via this module's top-level
    `from harness_* import ...` statements. The composer returns the
    materialized handles + constants for downstream wiring units
    (U-RT-34..38) + the U-RT-51 identity-equality verification suite.

    Verifies at runtime that every advertised manifest constant is
    reachable; raises `CxaTerminalImportError` if a constant is missing
    (a defect that would imply silent drift between this module's
    advertised contract and a manifest module's actual surface).
    """
    _ = config
    for module_name, constant in TERMINAL_MANIFEST_CONSTANTS.items():
        if constant is None:
            raise CxaTerminalImportError(
                f"manifest constant for {module_name} is None — "
                f"manifest module loaded but top-level export is absent"
            )
    return CxaTerminalImportsStage(
        imported_manifests=TERMINAL_MANIFEST_MODULES,
        manifest_constants=dict(TERMINAL_MANIFEST_CONSTANTS),
    )
