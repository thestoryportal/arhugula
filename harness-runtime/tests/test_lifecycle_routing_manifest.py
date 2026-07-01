"""U-RT-21 — routing-manifest construction + residence + replay-determinism tests.

ACs per Phase 2 Session 3 Track A atomic decomposition §L5 U-RT-21:
  #1 manifest validates against R-2 + W-2 (schema round-trip)
     -> test_build_round_trips_through_load_routing_manifest
     -> test_build_round_trips_with_populated_r2_w2_bindings
  #2 residence policy honored (manifest persists at `PathClass.ROUTING_MANIFEST`)
     -> test_persist_writes_at_routing_manifest_pathclass
     -> test_persist_creates_parent_directories
  #3 replay determinism — two `build_routing_manifest(config)` invocations
     against identical config produce byte-identical canonical-JSON output
     -> test_replay_determinism_empty_manifest
     -> test_replay_determinism_populated_manifest

Test convention notes:
- No live filesystem I/O outside `tmp_path`; the `PathBinding` fixture pins
  the residence under a `tmp_path`-rooted directory.
- Structural validation failure mode (`InvalidRoutingManifestError`) is
  covered via a `manifest_version=0` construct (the only structural reject
  in `validate_routing_manifest` at v1.3).
- The R-2 / W-2 schemas are imported from `harness_cp.routing_manifest_residence`
  (their canonical residence); this test exercises them via the runtime's
  `build_routing_manifest` rather than re-asserting their field sets (those
  belong to `harness_cp/tests/test_routing_manifest_residence.py`).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from harness_as import SandboxTier
from harness_core import DeploymentSurface, WorkloadClass
from harness_cp.cp_shared_types import AgentRole, ModelBinding
from harness_cp.engine_class import EngineClass
from harness_cp.routing_layer import RoutingLayer
from harness_cp.routing_manifest_residence import (
    RetryPolicy,
    RoleRoutingBinding,
    RoutingManifest,
    WorkloadRoutingOverride,
    load_routing_manifest,
)
from harness_cp.topology_pattern import TopologyPattern
from harness_is.path_binding import PathBinding, PathBindingEntry
from harness_is.path_class_registry import PathClass
from harness_is.path_resolver import PathResolver
from harness_runtime.lifecycle.routing_manifest import (
    InvalidRoutingManifestError,
    RoutingManifestStage,
    build_routing_manifest,
    canonicalize_routing_manifest,
    materialize_routing_manifest_stage,
    persist_routing_manifest,
)
from harness_runtime.types import (
    CollectorConfig,
    OTelConfig,
    PathBindingConfig,
    ProviderSecretsConfig,
    RuntimeConfig,
)

# ---------------------------------------------------------------------------
# Fixtures — RuntimeConfig + PathResolver bound to a tmp_path residence root.
# ---------------------------------------------------------------------------


def _config(tmp_path: Path, *, manifest: RoutingManifest | None = None) -> RuntimeConfig:
    """Build a minimal `RuntimeConfig` for U-RT-21 tests.

    Default `routing_manifest` is the empty default from `RuntimeConfig`
    (manifest_version=1; all collections empty). Tests that need a populated
    manifest pass one explicitly.
    """
    if manifest is None:
        return RuntimeConfig(
            deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
            repository_root=tmp_path,
            path_bindings=PathBindingConfig(),
            provider_secrets=ProviderSecretsConfig(),
            otel=OTelConfig(otlp_endpoint="http://localhost:4317"),
            collector=CollectorConfig(),
            default_topology=TopologyPattern.SINGLE_THREADED_LINEAR,
        )
    return RuntimeConfig(
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        repository_root=tmp_path,
        path_bindings=PathBindingConfig(),
        provider_secrets=ProviderSecretsConfig(),
        otel=OTelConfig(otlp_endpoint="http://localhost:4317"),
        collector=CollectorConfig(),
        default_topology=TopologyPattern.SINGLE_THREADED_LINEAR,
        routing_manifest=manifest,
    )


def _resolver(tmp_path: Path) -> PathResolver:
    """Build a `PathResolver` binding `ROUTING_MANIFEST` under `tmp_path`."""
    binding = PathBinding(
        entries=(
            PathBindingEntry(
                path_class=PathClass.ROUTING_MANIFEST,
                workflow_class=WorkloadClass.SOFTWARE_ENGINEERING,
                deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
                path=str(tmp_path / "routing-manifest" / "se" / "local" / "manifest.json"),
            ),
        )
    )
    return PathResolver(binding)


def _populated_manifest() -> RoutingManifest:
    """A manifest exercising both R-2 (`RoleRoutingBinding`) and W-2
    (`WorkloadRoutingOverride`) value-type round-trips plus a `RetryPolicy`."""
    return RoutingManifest(
        manifest_version=1,
        per_role_bindings={
            AgentRole("researcher"): RoleRoutingBinding(
                preferred_model_binding=ModelBinding(provider="anthropic", model="opus"),
                layer_budget_overrides={RoutingLayer.LLM_AS_ROUTER: 200},
                fallback_chain_ref="default-chain",
            ),
        },
        per_workload_overrides={
            WorkloadClass.SOFTWARE_ENGINEERING: WorkloadRoutingOverride(
                engine_class_override=EngineClass.EVENT_SOURCED_REPLAY,
                sandbox_tier_override=SandboxTier.TIER_2_CONTAINER,
                model_binding_override=ModelBinding(provider="openai", model="gpt-5"),
            ),
        },
        fallback_chains=(),
        retry_policies={
            "fetch": RetryPolicy(max_attempts=3, backoff="full-jitter", jitter="decorrelated"),
        },
    )


# ---------------------------------------------------------------------------
# AC #1 — R-2 + W-2 round-trip via canonical-JSON.
# ---------------------------------------------------------------------------


def test_build_returns_config_manifest(tmp_path: Path) -> None:
    """`build_routing_manifest` returns the manifest carried at config."""
    cfg = _config(tmp_path)
    manifest = build_routing_manifest(cfg)
    assert manifest is cfg.routing_manifest


def test_build_rejects_non_positive_manifest_version(tmp_path: Path) -> None:
    """Structural validation rejects `manifest_version < 1` as
    `InvalidRoutingManifestError` (RT-FAIL-BOOTSTRAP)."""
    bad = RoutingManifest(
        manifest_version=0,
        per_role_bindings={},
        per_workload_overrides={},
        fallback_chains=(),
        retry_policies={},
    )
    cfg = _config(tmp_path, manifest=bad)
    with pytest.raises(InvalidRoutingManifestError) as excinfo:
        build_routing_manifest(cfg)
    assert "manifest_version" in excinfo.value.reason


def test_build_round_trips_through_load_routing_manifest(tmp_path: Path) -> None:
    """Empty default manifest round-trips through canonical-JSON +
    `load_routing_manifest` (R-2 + W-2 schemas — vacuously empty)."""
    cfg = _config(tmp_path)
    manifest = build_routing_manifest(cfg)
    raw = canonicalize_routing_manifest(manifest)
    import json

    reloaded = load_routing_manifest(json.loads(raw))
    assert reloaded == manifest


def test_build_round_trips_with_populated_r2_w2_bindings(tmp_path: Path) -> None:
    """Populated manifest with non-empty R-2 + W-2 + RetryPolicy round-trips
    through canonical-JSON + `load_routing_manifest`. AC #1."""
    cfg = _config(tmp_path, manifest=_populated_manifest())
    manifest = build_routing_manifest(cfg)
    raw = canonicalize_routing_manifest(manifest)
    import json

    reloaded = load_routing_manifest(json.loads(raw))
    assert reloaded == manifest
    # Spot-check the R-2 field survives the round-trip.
    rrb = reloaded.per_role_bindings[AgentRole("researcher")]
    assert rrb.fallback_chain_ref == "default-chain"
    assert rrb.layer_budget_overrides[RoutingLayer.LLM_AS_ROUTER] == 200
    # Spot-check the W-2 field survives.
    wro = reloaded.per_workload_overrides[WorkloadClass.SOFTWARE_ENGINEERING]
    assert wro.engine_class_override is EngineClass.EVENT_SOURCED_REPLAY
    assert wro.sandbox_tier_override is SandboxTier.TIER_2_CONTAINER


# ---------------------------------------------------------------------------
# AC #2 — Residence policy honored (PathClass.ROUTING_MANIFEST).
# ---------------------------------------------------------------------------


def test_persist_writes_at_routing_manifest_pathclass(tmp_path: Path) -> None:
    """`persist_routing_manifest` writes at the path resolved through
    `PathClass.ROUTING_MANIFEST` — the dedicated typed class per the U-CP-04
    residence fix (`.harness/class_3_tension_u_cp_04_routing_manifest_pathclass.md`)."""
    cfg = _config(tmp_path, manifest=_populated_manifest())
    manifest = build_routing_manifest(cfg)
    resolver = _resolver(tmp_path)
    # Per IS spec v1.3 §1 amendment (2026-05-20 [[fork-state-ledger-path-
    # dir-vs-file]] resolution): PathClass.ROUTING_MANIFEST resolves to the
    # containing directory; the manifest file is `routing.manifest.json`
    # inside.
    expected_directory = tmp_path / "routing-manifest" / "se" / "local" / "manifest.json"
    expected_path = expected_directory / "routing.manifest.json"
    residence = persist_routing_manifest(
        manifest,
        resolver,
        WorkloadClass.SOFTWARE_ENGINEERING,
        DeploymentSurface.LOCAL_DEVELOPMENT,
    )
    assert residence == expected_path
    assert residence.exists()
    # Reloading the persisted bytes reproduces the manifest.
    import json

    reloaded = load_routing_manifest(json.loads(residence.read_bytes()))
    assert reloaded == manifest


def test_persist_creates_parent_directories(tmp_path: Path) -> None:
    """Parent directories under the residence path are created on demand."""
    cfg = _config(tmp_path)
    manifest = build_routing_manifest(cfg)
    resolver = _resolver(tmp_path)
    # Sanity: parent doesn't exist before persist.
    parent = tmp_path / "routing-manifest" / "se" / "local"
    assert not parent.exists()
    persist_routing_manifest(
        manifest,
        resolver,
        WorkloadClass.SOFTWARE_ENGINEERING,
        DeploymentSurface.LOCAL_DEVELOPMENT,
    )
    assert parent.is_dir()


def test_materialize_stage_returns_frozen_record_with_residence(tmp_path: Path) -> None:
    """Composer returns a frozen `RoutingManifestStage` carrying the validated
    manifest + the persisted residence path."""
    cfg = _config(tmp_path, manifest=_populated_manifest())
    resolver = _resolver(tmp_path)
    stage = materialize_routing_manifest_stage(cfg, resolver, WorkloadClass.SOFTWARE_ENGINEERING)
    assert isinstance(stage, RoutingManifestStage)
    assert stage.manifest is cfg.routing_manifest
    assert stage.residence_path.exists()
    # Frozen — assignment is rejected (FrozenInstanceError on dataclasses).
    from dataclasses import FrozenInstanceError

    with pytest.raises(FrozenInstanceError):
        stage.manifest = stage.manifest  # type: ignore[misc]


# ---------------------------------------------------------------------------
# AC #3 — Replay determinism: byte-identical canonical-JSON across invocations.
# ---------------------------------------------------------------------------


def test_replay_determinism_empty_manifest(tmp_path: Path) -> None:
    """Two builds against the same (empty default) config produce byte-identical
    canonical-JSON. AC #3."""
    cfg = _config(tmp_path)
    a = canonicalize_routing_manifest(build_routing_manifest(cfg))
    b = canonicalize_routing_manifest(build_routing_manifest(cfg))
    assert a == b


def test_replay_determinism_populated_manifest(tmp_path: Path) -> None:
    """Two builds against the same populated config produce byte-identical
    canonical-JSON. AC #3 — the load-bearing case (sorted keys protect against
    dict-insertion-order drift in Pydantic's serialization)."""
    cfg = _config(tmp_path, manifest=_populated_manifest())
    a = canonicalize_routing_manifest(build_routing_manifest(cfg))
    b = canonicalize_routing_manifest(build_routing_manifest(cfg))
    assert a == b


def test_replay_determinism_persist_overwrite_is_byte_identical(
    tmp_path: Path,
) -> None:
    """Persisting twice against the same manifest yields a byte-identical file
    on disk (the residence file content is replay-deterministic, not just the
    in-memory canonical bytes)."""
    cfg = _config(tmp_path, manifest=_populated_manifest())
    manifest = build_routing_manifest(cfg)
    resolver = _resolver(tmp_path)
    path1 = persist_routing_manifest(
        manifest,
        resolver,
        WorkloadClass.SOFTWARE_ENGINEERING,
        DeploymentSurface.LOCAL_DEVELOPMENT,
    )
    bytes1 = path1.read_bytes()
    path2 = persist_routing_manifest(
        manifest,
        resolver,
        WorkloadClass.SOFTWARE_ENGINEERING,
        DeploymentSurface.LOCAL_DEVELOPMENT,
    )
    assert path1 == path2
    assert path2.read_bytes() == bytes1


def test_canonical_json_sorts_keys(tmp_path: Path) -> None:
    """Sanity check on the canonicalization contract: the emitted JSON has
    keys in sorted order (so a future Pydantic dict-ordering shift can't break
    replay determinism)."""
    manifest = _populated_manifest()
    raw = canonicalize_routing_manifest(manifest).decode("utf-8")
    # Top-level keys appear in lex-sorted order in the emitted JSON.
    expected_order = sorted(
        [
            "manifest_version",
            "per_role_bindings",
            "per_workload_overrides",
            "fallback_chains",
            "retry_policies",
        ]
    )
    last_idx = -1
    for key in expected_order:
        idx = raw.find(f'"{key}":')
        assert idx > last_idx, f"key {key!r} out of sorted order in canonical JSON"
        last_idx = idx
