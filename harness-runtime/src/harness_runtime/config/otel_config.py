"""U-RT-07 — OTel runtime-config derivations.

Per `Spec_Harness_Runtime_v1.md` v1.1 §3 (C-RT-03 `otel` field) and Phase 2
Session 3 plan v2.1 §2 L1, this module:

- Resolves the per-deployment-surface sampling mode (C-OD-09 §9.1) with
  optional `OTelConfig.sampling_mode` override.
- Builds the OTel resource attributes from the deployment surface + the
  15-row namespace map (C-OD-05 §5.1) + operator-supplied additional attrs.

The TracerProvider construction at U-RT-27 consumes the resolved sampling
mode + resource attributes; this module produces the inputs to that step.

Implementation-discretion choices (per C-RT-03 + ADR-D6 v1.2):
- Resource-attr keys mirror the namespace prefixes in `harness_od.NAMESPACE_MAP`
  (15 rows). Each declared namespace is materialized as a resource attribute
  `namespace.<prefix>.declared = "true"` so OTel collectors can confirm
  declaration coverage at runtime.
- `deployment.surface` resource key carries the `DeploymentSurface` value
  per ADR-D6 v1.2 §1.2 row 1.
"""

from __future__ import annotations

from harness_core.deployment_surface import DeploymentSurface
from harness_od.namespace_map import NAMESPACE_MAP
from harness_od.sampling_mode import PER_DEPLOYMENT_SURFACE_SAMPLING, SamplingMode

from harness_runtime.types import OTelConfig

__all__ = [
    "build_resource_attributes",
    "resolve_sampling_mode",
]


def resolve_sampling_mode(
    otel: OTelConfig,
    deployment_surface: DeploymentSurface,
) -> SamplingMode:
    """Resolve the effective sampling mode.

    Override on `OTelConfig.sampling_mode` wins; otherwise the
    per-deployment-surface default at C-OD-09 §9.1 applies.
    """
    if otel.sampling_mode is not None:
        return otel.sampling_mode
    return PER_DEPLOYMENT_SURFACE_SAMPLING[deployment_surface]


def build_resource_attributes(
    otel: OTelConfig,
    deployment_surface: DeploymentSurface,
    tenant_id: str | None = None,
) -> dict[str, str]:
    """Build the OTel resource-attribute dict.

    Composition:
    1. `deployment.surface` carries the `DeploymentSurface.value` per ADR-D6
       v1.2 §1.2 row 1.
    2. One `namespace.<prefix>.declared = "true"` attribute per row of the
       15-row `NAMESPACE_MAP` (C-OD-05 §5.1). Provides runtime declaration
       coverage attestation.
    3. Operator-supplied `additional_resource_attrs` override default attrs.
    4. `tenant.id` is added from `RuntimeConfig.tenant_id` when present and
       wins over operator-supplied attrs; it is the C-OD-21 §21.1 tenant
       separation key, not an arbitrary deployment label.

    Total rows attested: `1 + len(NAMESPACE_MAP) + len(additional_resource_attrs)`.
    """
    attrs: dict[str, str] = {
        "deployment.surface": deployment_surface.value,
    }
    for row in NAMESPACE_MAP:
        attrs[f"namespace.{row.namespace_prefix}declared"] = "true"
    for key, value in otel.additional_resource_attrs:
        attrs[key] = value
    if tenant_id:
        attrs["tenant.id"] = tenant_id
    return attrs
