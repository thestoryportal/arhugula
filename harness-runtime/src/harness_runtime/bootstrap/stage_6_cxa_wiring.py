"""Stage 6 CXA_WIRING — terminal manifest import + cross-axis wiring composers.

Per `Spec_Harness_Runtime_v1.md` v1.1 §2 stage 6 post-conditions: all 5
terminal exporter manifests imported; the phase-2-runtime cross-axis edges
wired at stage 6 (CXA v2.19 §2.1 canonical count 22; the §2.3.x bucket
row-tables enumerate 24 R-tagged edges — per-edge disposition, incl. the 3
deferred U-RT-35 CP→IS edges, at
`.harness/r-cl-p5-cxa-cost-validator-verification.md`).

Composer call order:
1. `materialize_cxa_terminal_imports_stage` — realizes the 5 manifest imports
   (side-effect import; Pattern P1 typed-seam binding).
2. `materialize_as_is_wiring_stage(config, ledger_writer, resolver)` — AS→IS (1 edge).
3. CP→IS wiring — materialized earlier in stage 3b when present; stage 6
   reuses it or binds it as a compatibility fallback.
4. `materialize_od_is_wiring_stage(config, audit_writer, od_manifest)` — OD→IS
   (2 edges).
5. `materialize_od_as_wiring_stage(config, od_manifest)` — OD→AS (1 edge).
6. `materialize_cp_as_wiring_stage(config)` — CP→AS runtime registry.
7. `materialize_od_cp_wiring_stage(config, od_manifest)` — OD→CP (3 edges,
   includes F-CP-01 Stage 3b inversion verification).

The 5 wiring composers' returned `*Stage` records are stashed on
`ctx.cxa_stages` for verification + test introspection; they are not lifted
to `HarnessContext` (the wiring side-effects + the runtime registries
populated at earlier stages are what matters).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from harness_core.workload_class import WorkloadClass
from harness_od.substrate_seam_exports_aggregate_manifest import (
    OD_SUBSTRATE_SEAM_EXPORTS_MANIFEST,
)

from harness_runtime.bootstrap.mutable_context import _MutableHarnessContext
from harness_runtime.lifecycle.as_is_wiring import materialize_as_is_wiring_stage
from harness_runtime.lifecycle.cp_as_wiring import materialize_cp_as_wiring_stage
from harness_runtime.lifecycle.cp_is_wiring import materialize_cp_is_wiring_stage
from harness_runtime.lifecycle.cxa_terminal_imports import (
    materialize_cxa_terminal_imports_stage,
)
from harness_runtime.lifecycle.od_as_wiring import materialize_od_as_wiring_stage
from harness_runtime.lifecycle.od_cp_wiring import materialize_od_cp_wiring_stage
from harness_runtime.lifecycle.od_is_wiring import materialize_od_is_wiring_stage
from harness_runtime.lifecycle.procedural_tier_snapshot import (
    make_procedural_tier_snapshot_resolver,
)
from harness_runtime.types import RuntimeConfig

if TYPE_CHECKING:
    from harness_runtime.lifecycle.audit_writer import RuntimeAuditLedgerWriter
    from harness_runtime.types import HarnessContext

__all__ = ["execute"]


async def execute(
    ctx: _MutableHarnessContext,
    config: RuntimeConfig,
    workload_class: WorkloadClass,
) -> None:
    """Populate stage 6 CXA_WIRING `cxa_stages` on `ctx`."""
    _ = workload_class
    assert ctx.ledger_writer is not None, "stage 1 IS must precede stage 6 CXA_WIRING"
    assert ctx.audit_writer is not None, "stage 4 OD must precede stage 6 CXA_WIRING"

    od_manifest = OD_SUBSTRATE_SEAM_EXPORTS_MANIFEST

    ctx.cxa_stages["cxa_terminal_imports"] = materialize_cxa_terminal_imports_stage(
        config,
    )
    # CP spec v1.30 §1.4 / R-CXA-1: the procedural-tier-snapshot resolver
    # binds at stage 5 for workflow-context producer sites. Stage 6 preserves
    # compatibility for direct stage invocation and older partial bootstraps.
    if ctx.procedural_tier_snapshot_resolver is None:
        ctx.procedural_tier_snapshot_resolver = make_procedural_tier_snapshot_resolver(
            cast("HarnessContext", ctx),
        )
    ctx.cxa_stages["as_is_wiring"] = materialize_as_is_wiring_stage(
        config,
        ctx.ledger_writer,
        ctx.procedural_tier_snapshot_resolver,
    )
    if "cp_is_wiring" not in ctx.cxa_stages:
        ctx.cxa_stages["cp_is_wiring"] = materialize_cp_is_wiring_stage(
            config,
            ctx.ledger_writer,
            ctx.procedural_tier_snapshot_resolver,
        )
    ctx.cxa_stages["od_is_wiring"] = materialize_od_is_wiring_stage(
        config,
        # ctx.audit_writer is the concrete RuntimeAuditLedgerWriter at runtime
        # (bound at stage 4); the field is typed via the AuditLedgerWriter
        # Protocol, so narrow at this concrete call site per the workspace idiom.
        cast("RuntimeAuditLedgerWriter", ctx.audit_writer),
        od_manifest,
    )
    ctx.cxa_stages["od_as_wiring"] = materialize_od_as_wiring_stage(
        config,
        od_manifest,
    )
    ctx.cxa_stages["cp_as_wiring"] = materialize_cp_as_wiring_stage(config)
    ctx.cxa_stages["od_cp_wiring"] = materialize_od_cp_wiring_stage(
        config,
        od_manifest,
    )
