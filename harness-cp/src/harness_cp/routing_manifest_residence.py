"""Routing manifest residence + schema + `RetryPolicy` record — U-CP-04.

Implements C-CP-01 §1.3 (the routing-manifest schema + canonical residence)
and — per the Implementation Plan v2.9 factor-out delta — C-CP-03 §3.5 (the
`RetryPolicy` record, a faithful factor-out of the `retry.*` namespace
`retry.policy` full-jitter-default vocabulary).

FULL-LAND. `RetryPolicy`, `manifest_version`, `fallback_chains`, and
`retry_policies` materialize. The two `RoutingManifest` `Map` value-types
`RoleRoutingBinding` / `WorkloadRoutingOverride` were a Class 1 carry — their
field sets are not decomposed by any committing contract (C-CP-06 §6.1 does
not decompose them; C-CP-01 §1.3 gives prose grain only). The operator ruled
on the field schemas 2026-05-16 (`.harness/class_1_tension_role_routing_binding_
underspec.md` — RESOLVED, operator-ratified factor-out, schema R-2 / W-2):

- `RoleRoutingBinding` — `(preferred_model_binding, layer_budget_overrides,
  fallback_chain_ref)`.
- `WorkloadRoutingOverride` — `(engine_class_override, sandbox_tier_override,
  model_binding_override)`.

Each field composes only landed types (`ModelBinding` U-CP-00c; `RoutingLayer`
U-CP-06; `EngineClass` U-CP-15; AS-owned `SandboxTier` via the sanctioned CP→AS
edge). No field beyond the operator-approved set is added.

`ToolName` (the `retry_policies` map key) is the AS-owned tool-name concept;
no `ToolName` NewType is landed in `harness_as` — the spec treats tool names as
plain strings (`ToolContract.name: str`), so the key type is `str` (a faithful
materialization; a future AS `ToolName` NewType is a `str` alias). See the
Class 3 note at `.harness/phase-7-progress.md`.

Authority: Implementation_Plan_Control_Plane_v2_9.md §2A U-CP-04 (revised body
— `RetryPolicy` factor-out; partial-land); Spec_Control_Plane_v1_3.md §1
C-CP-01 §1.3; §3 C-CP-03 §3 + §3.5 `retry.*` namespace; CLAUDE.md §3.2
(hand-rolled retry — NO tenacity/pybreaker).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from harness_as import SandboxTier
from harness_core import DeploymentSurface, WorkloadClass
from harness_is.path_class_registry import PathClass
from harness_is.path_resolver import PathResolver
from pydantic import BaseModel, ConfigDict

from harness_cp.cp_shared_types import AgentRole, ModelBinding
from harness_cp.cross_family_fallback_chain import FallbackChain
from harness_cp.engine_class import EngineClass
from harness_cp.routing_layer import RoutingLayer


class RoleRoutingBinding(BaseModel):
    """Routing settings attached to one agent role (schema R-2).

    Operator-ratified factor-out 2026-05-16 (`.harness/class_1_tension_role_
    routing_binding_underspec.md` — RESOLVED). The CP routing surface lets a
    role nominate a model, tune its per-layer time budgets, and point at a
    named fallback chain — the three settings the surrounding routing units
    (U-CP-02/03/05/09) visibly consume. No speculative field added."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    preferred_model_binding: ModelBinding
    """The `(provider, model)` this role prefers (U-CP-00c `ModelBinding`)."""

    layer_budget_overrides: Mapping[RoutingLayer, int]
    """Per-routing-layer time-budget overrides (ms) for this role; an empty
    mapping means "use the default per-layer budgets" (U-CP-03)."""

    fallback_chain_ref: str | None = None
    """Optional name of a fallback chain to apply for this role (U-CP-09)."""


class WorkloadRoutingOverride(BaseModel):
    """Override settings for one workload category (schema W-2).

    Operator-ratified factor-out 2026-05-16 (same record). Sits "on top of"
    role bindings / defaults: a workload category may force a durable-execution
    engine class, a sandbox tier, or a model binding. Each field is optional —
    an unset field means "no override for this dimension"."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    engine_class_override: EngineClass | None = None
    """Force a durable-execution engine class for this workload (U-CP-15)."""

    sandbox_tier_override: SandboxTier | None = None
    """Force a blast-radius sandbox tier (AS-owned `SandboxTier`; sanctioned
    CP→AS cross-axis edge per CXA v2.1 §2.3.4)."""

    model_binding_override: ModelBinding | None = None
    """Force a `(provider, model)` for this workload (U-CP-00c)."""


class RetryPolicy(BaseModel):
    """A retry policy — faithful factor-out of C-CP-03 §3.5 `retry.policy`.

    The `retry.*` namespace `retry.policy` attribute commits a "full-jitter
    default per Cluster 4 §2.2.7 [HIGH]". Hand-rolled retry per CLAUDE.md §3.2
    — NO tenacity/pybreaker. Exactly three fields; no field invented beyond the
    §3.5 retry-policy vocabulary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_attempts: int
    """Retry-attempt cap per C-CP-03 §3 chain-advancement."""

    backoff: str
    """Backoff-strategy token; "full-jitter" default per C-CP-03 §3.5."""

    jitter: str
    """Jitter-mode token; composes with `backoff`."""


class RoutingManifest(BaseModel):
    """The routing manifest — canonical role x workload model-binding source.

    Exactly five top-level fields per C-CP-01 §1.3 + cross-references to
    C-CP-03 §3.5 + C-CP-04 §4.1. `per_role_bindings` / `per_workload_overrides`
    value-types are the operator-ratified R-2 / W-2 records (see module
    docstring)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest_version: int
    per_role_bindings: Mapping[AgentRole, RoleRoutingBinding]
    """Per-role routing settings; value-type R-2 (operator-ratified 2026-05-16)."""

    per_workload_overrides: Mapping[WorkloadClass, WorkloadRoutingOverride]
    """Per-workload override settings; value-type W-2 (operator-ratified)."""

    fallback_chains: tuple[FallbackChain, ...]
    """Populated per C-CP-04 (U-CP-09 `FallbackChain`)."""

    retry_policies: Mapping[str, RetryPolicy]
    """Per-tool retry policies, keyed by tool name; populated per C-CP-03 §3.5."""


class RoutingManifestValidationError(BaseModel):
    """A routing-manifest validation failure."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: str


class ReservedToolNameError(Exception):
    """Raised when the routing manifest declares a tool retry policy under a
    name reserved by the runtime (U-RT-58, C-RT-16 §"Registry key extension").

    Currently reserves ``"llm_dispatch"`` — the runtime's LLM-dispatch retry
    policy key, injected by ``materialize_retry_breaker_stage`` post-
    validation. Operator-supplied entries collide with the reservation and
    raise this typed error at manifest validation time.

    The reservation is owned by the runtime axis (per `Spec_Harness_Runtime_v1.md`
    v1.4 §14.6); the error is homed at the CP-side validator because that is
    where ``RoutingManifest`` validation lives. The CP-side definition is
    operationally an inversion-of-control surface — the runtime extension is
    referenced here by name only."""

    def __init__(self, reserved_name: str) -> None:
        self.reserved_name = reserved_name
        super().__init__(
            f"tool name {reserved_name!r} is reserved by the runtime "
            f"(C-RT-16 §'Registry key extension'); operator manifests may "
            f"not declare a retry policy under this key"
        )


_RESERVED_TOOL_NAMES: frozenset[str] = frozenset({"llm_dispatch"})
"""Tool names reserved by runtime composers. Currently:

- ``"llm_dispatch"`` — reserved by U-RT-58 ``RetryBreakerFallbackDispatcher``
  for LLM-dispatch retry policy lookup (`Spec_Harness_Runtime_v1.md` v1.4
  §14.6)."""


def validate_routing_manifest(
    manifest: RoutingManifest,
) -> RoutingManifestValidationError | None:
    """Validate a routing manifest; return `None` if valid, else the error.

    Structural validation: `manifest_version` must be positive. Per-role
    model-presence checks against the U-AS-29 model-binding catalog are a
    cross-axis runtime check (acceptance #3, runtime-deferred). Deterministic.

    Raises
    ------
    ReservedToolNameError
        If ``retry_policies`` declares a policy under a runtime-reserved
        name (e.g., ``"llm_dispatch"``). Raised rather than returned because
        reserved-name collisions indicate a manifest authoring error that
        the operator must correct, not a recoverable validation failure."""
    if manifest.manifest_version < 1:
        return RoutingManifestValidationError(reason="manifest_version must be a positive integer")
    for tool_name in manifest.retry_policies:
        if tool_name in _RESERVED_TOOL_NAMES:
            raise ReservedToolNameError(tool_name)
    return None


ROUTING_MANIFEST_FILENAME = "routing.manifest.json"
"""Canonical filename for the routing manifest within the
`PathClass.ROUTING_MANIFEST` directory (IS spec v1.3 §1 amendment,
2026-05-20 per `[[fork-state-ledger-path-dir-vs-file]]` Path A)."""


def resolve_manifest_residence_path(
    resolver: PathResolver,
    workload_class: WorkloadClass,
    deployment_surface: DeploymentSurface,
) -> Path:
    """Resolve the canonical routing-manifest residence path.

    Delegates to the U-IS-02 `PathResolver` against the U-IS-01 `PathClass`;
    per-deployment-surface residence is the resolver's `deployment_surface`
    dimension (acceptance #2). Per IS spec v1.3 §1 amendment,
    `PathClass.ROUTING_MANIFEST` resolves to a *directory*; the manifest
    file lives inside as `routing.manifest.json`.

    The manifest resides under the `ROUTING_MANIFEST` path-class — the
    dedicated typed class per C-IS-01 §1 citing `ADR-F1 v1.2 Consequences
    §(a)` ("manifest-layer model assignment as auditable default at every
    call site"). C-CP-01 §1.3 routes residence through `C-IS-10 §10.4`
    filesystem-path-contract export, which the IS registry materializes at
    `PathClass.ROUTING_MANIFEST` (distinct from `PathClass.PROMPTS`;
    IS-AL-1 names the four typed classes — SKILLS, PROMPTS,
    ROUTING_MANIFEST, STATE_LEDGER — as distinct, not aliases)."""
    directory = resolver.resolve_path(
        PathClass.ROUTING_MANIFEST, workload_class, deployment_surface
    )
    return directory / ROUTING_MANIFEST_FILENAME


def load_routing_manifest(raw: Mapping[str, Any]) -> RoutingManifest:
    """Load a routing manifest from a parsed configuration mapping.

    The concrete on-disk format (JSON vs YAML vs TOML) is deferred to
    implementation discretion per C-CP-01 §1.3; this function consumes the
    already-parsed mapping and validates it against the `RoutingManifest`
    schema."""
    return RoutingManifest.model_validate(raw)
