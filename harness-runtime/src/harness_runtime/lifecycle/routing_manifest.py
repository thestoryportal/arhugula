"""Routing-manifest lifecycle — stage 3b CP_ROUTING (U-RT-21).

Per `Spec_Harness_Runtime_v1.md` v1.1 §5 (C-RT-02 stage 3b invariants) and the
Phase 2 Session 3 Track A atomic decomposition §L5 (U-RT-21). The runtime owns
construction, structural validation, residence-policy enforcement, and replay-
deterministic canonical-JSON serialization of the routing manifest declared at
`harness_cp.routing_manifest_residence` (U-CP-04 — schema R-2 + W-2 +
RoutingManifest 5-field shape).

Per-function landing posture:
- `canonicalize_routing_manifest(manifest) -> bytes` — deterministic
  canonical-JSON serialization (sorted keys, separators (",", ":"),
  ensure_ascii=False, UTF-8 encoded). Matches the IS `canonicalize` shape at
  `harness_is.entry_hash:49` (sort_keys=True; separators=(",", ":")). Local
  helper at U-RT-21; a future promotion to `harness_core` is a separate
  cross-axis naming-convention pass.
- `build_routing_manifest(config) -> RoutingManifest` — pure (no I/O); returns
  the operator-supplied manifest from `RuntimeConfig.routing_manifest` after
  running `validate_routing_manifest`. Replay-deterministic per AC #3 because
  the manifest is itself frozen Pydantic and canonicalization is deterministic.
- `persist_routing_manifest(manifest, resolver, workload_class, deployment_surface)`
  — writes canonical-JSON bytes to the path resolved by
  `resolve_manifest_residence_path`, which routes through
  `PathClass.ROUTING_MANIFEST` (per C-CP-01 §1.3 → C-IS-10 §10.4 → IS
  registry — see the U-CP-04 residence-path Class 3 record at
  `.harness/class_3_tension_u_cp_04_routing_manifest_pathclass.md`).
- `materialize_routing_manifest_stage(config, resolver, workload_class)` —
  composer mirroring the L4 `materialize_provider_clients_stage` pattern;
  returns a frozen `RoutingManifestStage` carrying the validated manifest +
  the persisted residence path.

Failure-mode taxonomy:

| Function exception                      | RT-FAIL-* spec class               |
|-----------------------------------------|------------------------------------|
| `InvalidRoutingManifestError`           | `RT-FAIL-BOOTSTRAP` (permanent)    |

Scope discipline (U-RT-21 boundary): this unit lands the *structure* of the
manifest + its residence. It does NOT instantiate the engine selector (U-RT-22),
materialize the cross-family fallback chain runtime (U-RT-23), wire the
retry / breaker registry (U-RT-24), build the HITL placement registry
(U-RT-25), or compose the sub-agent handoff registry (U-RT-26).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from harness_core import DeploymentSurface, WorkloadClass
from harness_cp.routing_manifest_residence import (
    RoutingManifest,
    RoutingManifestValidationError,
    resolve_manifest_residence_path,
    validate_routing_manifest,
)
from harness_is.path_resolver import PathResolver

from harness_runtime.types import RuntimeConfig

__all__ = [
    "InvalidRoutingManifestError",
    "RoutingManifestStage",
    "build_routing_manifest",
    "canonicalize_routing_manifest",
    "materialize_routing_manifest_stage",
    "persist_routing_manifest",
]


class InvalidRoutingManifestError(Exception):
    """Routing-manifest structural validation failed at stage 3b.

    Carries the underlying `RoutingManifestValidationError.reason` so the
    bootstrap orchestrator can attribute the fail-class without re-running
    `validate_routing_manifest`."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"InvalidRoutingManifest: {reason}")


def canonicalize_routing_manifest(manifest: RoutingManifest) -> bytes:
    """Canonicalize a routing manifest to deterministic UTF-8 bytes.

    Two invocations against logically-equal manifests produce byte-identical
    output (U-RT-21 AC #3 — replay determinism). Matches the IS `canonicalize`
    convention at `harness_is.entry_hash`: `sort_keys=True`,
    `separators=(",", ":")`, `ensure_ascii=False`, UTF-8 encoded.

    Uses `model_dump(mode="json")` so Pydantic serializes enums to their
    primitive values (strings) before `json.dumps` sorts keys — matching the
    enum-on-disk representation a re-load via `load_routing_manifest` consumes.
    """
    payload = manifest.model_dump(mode="json")
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def build_routing_manifest(config: RuntimeConfig) -> RoutingManifest:
    """Return the validated operator-supplied routing manifest from `config`.

    Pure (no I/O). Runs `validate_routing_manifest` against the manifest
    carried at `RuntimeConfig.routing_manifest` and raises
    `InvalidRoutingManifestError` on a validation failure; otherwise returns
    the manifest unmodified.

    Per U-RT-21 AC #1 (R-2 + W-2 schema round-trip): because the manifest is
    declared at `RuntimeConfig` as a frozen `RoutingManifest` Pydantic schema,
    a `canonicalize → load_routing_manifest` round-trip reconstructs an
    equal manifest (assertion-tested at the test suite).
    """
    manifest = config.routing_manifest
    err: RoutingManifestValidationError | None = validate_routing_manifest(manifest)
    if err is not None:
        raise InvalidRoutingManifestError(err.reason)
    return manifest


def persist_routing_manifest(
    manifest: RoutingManifest,
    resolver: PathResolver,
    workload_class: WorkloadClass,
    deployment_surface: DeploymentSurface,
) -> Path:
    """Persist the routing manifest to its `PathClass.ROUTING_MANIFEST` residence.

    Resolves the residence path via `resolve_manifest_residence_path` (which
    routes through `PathClass.ROUTING_MANIFEST` per the U-CP-04 fix at
    `.harness/class_3_tension_u_cp_04_routing_manifest_pathclass.md`), creates
    parent directories if absent, and writes the canonical-JSON bytes.

    Returns the persisted path (U-RT-21 AC #2 — residence policy honored).
    Idempotent for byte-identical manifests: a second call against the same
    `(manifest, resolver, workload_class, deployment_surface)` tuple
    overwrites the file with byte-identical content.
    """
    residence = resolve_manifest_residence_path(resolver, workload_class, deployment_surface)
    residence.parent.mkdir(parents=True, exist_ok=True)
    residence.write_bytes(canonicalize_routing_manifest(manifest))
    return residence


@dataclass(frozen=True, slots=True)
class RoutingManifestStage:
    """Frozen result of stage 3b CP_ROUTING manifest materialization.

    Mirrors the L4 `ProviderClientsStage` shape: the composer returns this so
    the bootstrap orchestrator (U-RT-43) carries both the validated manifest
    and its persisted residence path through to `HarnessContext`."""

    manifest: RoutingManifest
    residence_path: Path


def materialize_routing_manifest_stage(
    config: RuntimeConfig,
    resolver: PathResolver,
    workload_class: WorkloadClass,
) -> RoutingManifestStage:
    """Build + persist the routing manifest; return the frozen stage record.

    Stage 3b composer (mirrors the L4 `materialize_provider_clients_stage`
    pattern). Calls `build_routing_manifest` (validates + returns the manifest)
    and `persist_routing_manifest` (writes canonical-JSON bytes at the
    `PathClass.ROUTING_MANIFEST` residence). The bootstrap orchestrator
    (U-RT-43) reads `config.deployment_surface` for the residence-path
    deployment-surface dimension; `workload_class` is passed explicitly
    because the runtime is workload-class-parameterized per invocation, not
    per-config.
    """
    manifest = build_routing_manifest(config)
    residence = persist_routing_manifest(
        manifest, resolver, workload_class, config.deployment_surface
    )
    return RoutingManifestStage(manifest=manifest, residence_path=residence)
