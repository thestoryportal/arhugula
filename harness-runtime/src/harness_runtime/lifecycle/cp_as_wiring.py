"""CP -> AS cross-axis wiring — stage 6 (R-CXA-3).

The CP -> AS seam is a typed substrate-consumption seam: CP runtime and
CP-axis modules consume AS-owned policy and tool-contract carriers such as
``BlastRadiusTier``, ``ToolContract``, and ``fetch_secret``. This composer
materializes the runtime registry for the AS substrate exports that declare
the Control Plane as a consuming axis.
Authority: H_T-CXA-3 plus the U-CP-68 -> U-AS-03 CP-to-AS ToolContract seam.

The composer deliberately does not invent a callback path. Its job is to bind
the AS terminal export manifest into stage 6, fail fast if the CP-consumed
export set drifts, and expose the bound registry for runtime inspection.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from harness_as.as_substrate_seam_exports import (
    AS_SUBSTRATE_SEAM_EXPORTS,
    ASConsumingAxis,
    ASSeamId,
    ASSubstrateSeamExport,
)

from harness_runtime.types import RuntimeConfig


class CpAsWiringBindError(Exception):
    """Raised when CP -> AS wiring stage materialization fails."""


class CpConsumedAsSeamUnresolved(Exception):  # noqa: N818 - domain-anchored name
    """Raised when an expected CP-consumed AS seam export is absent."""


class AsCpConsumerCoverageMismatch(Exception):  # noqa: N818 - domain-anchored name
    """Raised when AS manifest CP-consumer coverage drifts."""


_EXPECTED_CP_CONSUMED_AS_SEAMS: Final[frozenset[ASSeamId]] = frozenset(
    {
        ASSeamId.SANDBOX_BOUNDED_SPAN_SCHEMA_EXPORT,
        ASSeamId.FIVE_AXIS_MULTIPLICATIVE_TUNABLE_EXPORT,
        ASSeamId.PER_TOOL_REQUIRED_SECRETS_EXPORT,
        ASSeamId.ELEVEN_PRIMITIVE_ADOPTION_DEPTH_MATRIX_EXPORT,
        ASSeamId.FORCING_CONDITION_EXPORT,
    }
)


@dataclass(frozen=True, slots=True)
class CpConsumedAsSeamResolution:
    """One runtime binding from a CP-consumed AS seam ID to its AS export."""

    seam_id: ASSeamId
    as_export: ASSubstrateSeamExport
    bound_as_export: ASSubstrateSeamExport


@dataclass(frozen=True, slots=True)
class CpConsumedAsSeamsCoverage:
    """Coverage record for AS exports declaring CP as a consuming axis."""

    expected_cp_consumed_seams: frozenset[ASSeamId]
    declared_cp_consumed_seams: frozenset[ASSeamId]
    coverage_match: bool


@dataclass(frozen=True, slots=True)
class RuntimeCpAsWiring:
    """Runtime CP -> AS wiring surface for the R-CXA-3 seam."""

    cp_consumed_as_seams: tuple[CpConsumedAsSeamResolution, ...]
    coverage: CpConsumedAsSeamsCoverage


@dataclass(frozen=True, slots=True)
class CpAsWiringStage:
    """Frozen result of stage 6 CP -> AS wiring materialization."""

    wiring: RuntimeCpAsWiring


def verify_cp_consumed_as_seam_coverage() -> CpConsumedAsSeamsCoverage:
    """Verify AS terminal exports still declare the expected CP-consumed seams."""

    declared = frozenset(
        export.seam_id
        for export in AS_SUBSTRATE_SEAM_EXPORTS
        if ASConsumingAxis.CONTROL_PLANE in export.consuming_axes
    )
    coverage = CpConsumedAsSeamsCoverage(
        expected_cp_consumed_seams=_EXPECTED_CP_CONSUMED_AS_SEAMS,
        declared_cp_consumed_seams=declared,
        coverage_match=declared == _EXPECTED_CP_CONSUMED_AS_SEAMS,
    )
    if not coverage.coverage_match:
        missing = sorted(s.value for s in _EXPECTED_CP_CONSUMED_AS_SEAMS - declared)
        unexpected = sorted(s.value for s in declared - _EXPECTED_CP_CONSUMED_AS_SEAMS)
        raise AsCpConsumerCoverageMismatch(
            f"AS manifest CP-consumer coverage drifted: missing={missing}; unexpected={unexpected}"
        )
    return coverage


def resolve_cp_consumed_as_seams() -> tuple[CpConsumedAsSeamResolution, ...]:
    """Bind each expected CP-consumed AS seam to its AS terminal export."""

    by_id = {export.seam_id: export for export in AS_SUBSTRATE_SEAM_EXPORTS}
    resolutions: list[CpConsumedAsSeamResolution] = []
    for seam_id in sorted(_EXPECTED_CP_CONSUMED_AS_SEAMS, key=lambda s: s.value):
        export = by_id.get(seam_id)
        if export is None:
            raise CpConsumedAsSeamUnresolved(
                f"AS terminal export manifest does not declare {seam_id.value!r}"
            )
        if ASConsumingAxis.CONTROL_PLANE not in export.consuming_axes:
            raise CpConsumedAsSeamUnresolved(
                f"AS seam {seam_id.value!r} exists but does not declare "
                "the Control Plane as a consuming axis"
            )
        resolutions.append(
            CpConsumedAsSeamResolution(
                seam_id=seam_id,
                as_export=export,
                bound_as_export=export,
            )
        )
    return tuple(resolutions)


def materialize_cp_as_wiring_stage(config: RuntimeConfig) -> CpAsWiringStage:
    """Build the stage 6 CP -> AS runtime registry."""

    _ = config
    coverage = verify_cp_consumed_as_seam_coverage()
    return CpAsWiringStage(
        wiring=RuntimeCpAsWiring(
            cp_consumed_as_seams=resolve_cp_consumed_as_seams(),
            coverage=coverage,
        )
    )
