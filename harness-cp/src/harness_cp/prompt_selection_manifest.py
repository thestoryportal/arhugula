"""Prompts per-role/workload selection surface — `PromptSelectionManifest`.

R-PM-1 cascade PR #3 (CP spec v1.31 §29 / C-CP-29). The CP-axis prompt-selection layer:
operator-supplied per-role + per-workload bindings that resolve *which* authored
prompt version (named by its content-addressed ``version_sha``) is active for a
given ``(role, workload)``. Mirrors ``RoutingManifest.per_role_bindings`` /
``per_workload_overrides`` (C-CP-01 §1.3) exactly — the same operator-supplied,
frozen, ``extra="forbid"`` carrier shape.

**Selection-ownership split** (R-PM-1 design tension (i), IS ⊥ CP, probe-resolved
by the ``RoutingManifest`` precedent): authoring/versioning is IS (the
``PromptManifest.versions`` content-addressed store, IS spec v1.7 §5.3); the
per-role/workload SELECTION-binding is CP (this module). The resolver yields a
``version_sha``; the IS store resolves ``version_sha → content``; the runtime
(the consumer site) injects the content as a system prompt per runtime spec
v1.44 §14.5.2. The CP→IS store consultation is the CXA seam registered at
cascade PR #5 — this module is **CP-pure**: it resolves an sha and never imports
or consults the IS store (axis isolation).

**Resolution precedence** mirrors ``RoutingManifest`` (a workload override sits
"on top of" role bindings): ``per_workload_overrides[workload]`` >
``per_role_bindings[role]`` > ``None`` (fall-through to the manifest's standing
``active_prompt_version`` — the #496/PR-#1 single inline active prompt). An empty
manifest (or one that binds neither the role nor the workload) selects ``None``
→ the runtime falls through to the inline active prompt: zero config burden, the
local-first default (C11). Selection only *adds* resolution; it never gates
whether dispatch runs (variability-in-values, not control-flow).

**Cross-axis store-membership** (a bound ``version_sha`` is an authored member of
the IS ``PromptManifest.versions`` store) is a CP↔IS check. It is
**runtime-deferred** at the consumer site — fail-loud, detect-then-refuse —
mirroring ``validate_routing_manifest``'s own runtime-deferred per-role
model-presence check (acceptance #3, cross-axis runtime check). This CP-side
validator is **structural-only**.

Authority: CP spec v1.31 §29 / C-CP-29 (NEW; mirrors C-CP-01 §1.3 ``RoutingManifest``);
R-PM-1 design ``.harness/r-pm-1-prompts-management-design-v1.md`` §4.2.
"""

from __future__ import annotations

from collections.abc import Mapping

from harness_core import WorkloadClass
from pydantic import BaseModel, ConfigDict, Field

from harness_cp.cp_shared_types import AgentRole

__all__ = [
    "PromptBinding",
    "PromptSelectionManifest",
    "PromptSelectionManifestValidationError",
    "resolve_active_prompt_version_sha",
    "validate_prompt_selection_manifest",
]


class PromptBinding(BaseModel):
    """A prompt-selection binding — names the active prompt version by sha.

    Frozen + ``extra="forbid"``, mirroring ``RoleRoutingBinding``. Exactly one
    field: the content-addressed identity of the prompt version this binding
    selects. That ``version_sha`` MUST be an authored member of the IS
    ``PromptManifest.versions`` store — a cross-axis check verified
    runtime-deferred at the consumer site (fail-loud), not here."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version_sha: str
    """Content-addressed identity of the selected prompt version (an authored
    member of the IS ``PromptManifest.versions`` store; ``version_sha =
    prompt_version_sha(content)`` per IS spec v1.6 §5.2). Store-membership is a
    CP↔IS check verified runtime-deferred at the runtime consumer site."""


class PromptSelectionManifest(BaseModel):
    """The prompt-selection manifest — canonical role × workload → prompt-version source.

    Mirrors ``RoutingManifest`` (frozen + ``extra="forbid"``; a
    ``manifest_version`` plus per-role + per-workload binding maps). Both maps
    default to empty — the "empty binding falls through to the inline active
    prompt" semantics (the #496/PR-#1 behavior; zero config burden). Operators
    supply only the dimension(s) they bind."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest_version: int
    """Manifest schema version (mirrors ``RoutingManifest.manifest_version``)."""

    per_role_bindings: Mapping[AgentRole, PromptBinding] = Field(
        default_factory=lambda: dict[AgentRole, PromptBinding]()
    )
    """Per-role prompt-version selection. Empty (default) → no per-role binding.

    NOTE: the runtime has no per-step agent-role dimension at MVP (dispatch keys
    on ``_MVP_DEFAULT_AGENT_ROLE``; routing's own ``per_role_bindings`` is
    likewise role-keyed only at R-300-second-provider), so per-role selection is
    carried faithfully but resolved against the default role until real per-role
    dispatch lands. ``per_workload_overrides`` keys on the REAL run workload."""

    per_workload_overrides: Mapping[WorkloadClass, PromptBinding] = Field(
        default_factory=lambda: dict[WorkloadClass, PromptBinding]()
    )
    """Per-workload prompt-version selection; sits "on top of" ``per_role_bindings``
    (override precedence, mirroring ``WorkloadRoutingOverride``). Empty (default)
    → no per-workload override. The run's ``WorkloadClass`` is a genuine runtime
    dimension (threaded into ``run_bootstrap``), so per-workload selection is
    behavior-driving end-to-end."""


class PromptSelectionManifestValidationError(BaseModel):
    """A prompt-selection-manifest validation failure (mirrors
    ``RoutingManifestValidationError``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: str


def validate_prompt_selection_manifest(
    manifest: PromptSelectionManifest,
) -> PromptSelectionManifestValidationError | None:
    """Validate a prompt-selection manifest; return ``None`` if valid, else the error.

    Structural validation only (mirrors ``validate_routing_manifest``):
    ``manifest_version`` must be positive. The cross-axis check — a bound
    ``version_sha`` is an authored member of the IS ``PromptManifest.versions``
    store — is **runtime-deferred** at the consumer site (fail-loud), mirroring
    ``validate_routing_manifest``'s runtime-deferred per-role model-presence
    check. Deterministic."""
    if manifest.manifest_version < 1:
        return PromptSelectionManifestValidationError(
            reason="manifest_version must be a positive integer"
        )
    return None


def resolve_active_prompt_version_sha(
    manifest: PromptSelectionManifest,
    *,
    role: AgentRole,
    workload: WorkloadClass,
) -> str | None:
    """Resolve the selected prompt ``version_sha`` for ``(role, workload)``, or ``None``.

    Precedence (mirrors ``RoutingManifest`` workload-override-on-top-of-role):
    ``per_workload_overrides[workload]`` > ``per_role_bindings[role]`` > ``None``.
    ``None`` → no selection → the runtime consumer falls through to the manifest's
    standing ``active_prompt_version`` (the #496/PR-#1 inline active prompt). Pure
    (no I/O, no store consultation); CP-axis-isolated."""
    override = manifest.per_workload_overrides.get(workload)
    if override is not None:
        return override.version_sha
    binding = manifest.per_role_bindings.get(role)
    if binding is not None:
        return binding.version_sha
    return None
