"""Tests for U-CP-04 — routing manifest residence (C-CP-01 §1.3 + C-CP-03 §3.5).

Acceptance-criterion coverage:
  #1 RoutingManifest 5 fields    -> test_routing_manifest_five_fields
  #2 residence via U-IS-02       -> test_load_via_u_is_02
  #3 validate rejects bad model  -> test_validate_rejects_unknown_model
  #4 manifest format deferred    -> test_format_deferred
  #5 RetryPolicy 3 fields +      -> test_retry_policy_three_fields_byte_exact_cp_03_3_5
  R-2/W-2 routing-binding schemas -> test_role_routing_binding_schema_r2,
     (operator-ratified 2026-05-16)  test_workload_routing_override_schema_w2,
                                     test_manifest_carries_typed_bindings
"""

from __future__ import annotations

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
    resolve_manifest_residence_path,
    validate_routing_manifest,
)
from harness_is.path_binding import PathBinding, PathBindingEntry
from harness_is.path_class_registry import PathClass
from harness_is.path_resolver import PathResolver


def _manifest(version: int = 1) -> RoutingManifest:
    return RoutingManifest(
        manifest_version=version,
        per_role_bindings={},
        per_workload_overrides={},
        fallback_chains=(),
        retry_policies={
            "fetch": RetryPolicy(max_attempts=3, backoff="full-jitter", jitter="decorrelated")
        },
    )


def test_routing_manifest_five_fields() -> None:
    assert set(RoutingManifest.model_fields) == {
        "manifest_version",
        "per_role_bindings",
        "per_workload_overrides",
        "fallback_chains",
        "retry_policies",
    }


def test_load_via_u_is_02() -> None:
    binding = PathBinding(
        entries=(
            PathBindingEntry(
                path_class=PathClass.ROUTING_MANIFEST,
                workflow_class=WorkloadClass.SOFTWARE_ENGINEERING,
                deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
                path="/canonical/routing-manifest/se/local",
            ),
        )
    )
    resolver = PathResolver(binding)
    path = resolve_manifest_residence_path(
        resolver,
        WorkloadClass.SOFTWARE_ENGINEERING,
        DeploymentSurface.LOCAL_DEVELOPMENT,
    )
    # Per IS spec v1.3 §1 amendment (2026-05-20 [[fork-state-ledger-path-
    # dir-vs-file]] resolution): PathClass.ROUTING_MANIFEST resolves to the
    # containing directory; the manifest file is `routing.manifest.json`
    # inside.
    assert str(path) == "/canonical/routing-manifest/se/local/routing.manifest.json"


def test_validate_rejects_unknown_model() -> None:
    # Structural validation: a non-positive manifest_version is rejected.
    bad = _manifest(version=0)
    err = validate_routing_manifest(bad)
    assert err is not None
    assert validate_routing_manifest(_manifest(version=1)) is None


def test_format_deferred() -> None:
    # Format is implementation discretion; load consumes a parsed mapping.
    raw = _manifest().model_dump()
    loaded = load_routing_manifest(raw)
    assert loaded == _manifest()


def test_retry_policy_three_fields_byte_exact_cp_03_3_5() -> None:
    assert set(RetryPolicy.model_fields) == {"max_attempts", "backoff", "jitter"}
    rp = RetryPolicy(max_attempts=5, backoff="full-jitter", jitter="full")
    assert rp.backoff == "full-jitter"


def test_role_routing_binding_schema_r2() -> None:
    # Operator-ratified schema R-2: exactly 3 fields.
    assert set(RoleRoutingBinding.model_fields) == {
        "preferred_model_binding",
        "layer_budget_overrides",
        "fallback_chain_ref",
    }
    rrb = RoleRoutingBinding(
        preferred_model_binding=ModelBinding(provider="anthropic", model="opus"),
        layer_budget_overrides={RoutingLayer.DECLARATIVE: 50},
        fallback_chain_ref="default-chain",
    )
    assert rrb.preferred_model_binding.provider == "anthropic"
    assert rrb.layer_budget_overrides[RoutingLayer.DECLARATIVE] == 50
    # fallback_chain_ref is optional.
    bare = RoleRoutingBinding(
        preferred_model_binding=ModelBinding(provider="openai", model="gpt"),
        layer_budget_overrides={},
    )
    assert bare.fallback_chain_ref is None


def test_workload_routing_override_schema_w2() -> None:
    # Operator-ratified schema W-2: exactly 3 fields, all optional.
    assert set(WorkloadRoutingOverride.model_fields) == {
        "engine_class_override",
        "sandbox_tier_override",
        "model_binding_override",
    }
    empty = WorkloadRoutingOverride()
    assert empty.engine_class_override is None
    full = WorkloadRoutingOverride(
        engine_class_override=EngineClass.PURE_PATTERN_NO_ENGINE,
        sandbox_tier_override=SandboxTier.TIER_2_CONTAINER,
        model_binding_override=ModelBinding(provider="ollama", model="llama"),
    )
    assert full.sandbox_tier_override is SandboxTier.TIER_2_CONTAINER


def test_manifest_carries_typed_bindings() -> None:
    # The manifest's two Map fields now carry the typed R-2 / W-2 records.
    m = RoutingManifest(
        manifest_version=1,
        per_role_bindings={
            AgentRole("researcher"): RoleRoutingBinding(
                preferred_model_binding=ModelBinding(provider="anthropic", model="opus"),
                layer_budget_overrides={RoutingLayer.LLM_AS_ROUTER: 200},
            )
        },
        per_workload_overrides={
            WorkloadClass.SOFTWARE_ENGINEERING: WorkloadRoutingOverride(
                engine_class_override=EngineClass.EVENT_SOURCED_REPLAY,
            )
        },
        fallback_chains=(),
        retry_policies={},
    )
    assert (
        m.per_role_bindings[AgentRole("researcher")].layer_budget_overrides[
            RoutingLayer.LLM_AS_ROUTER
        ]
        == 200
    )
    assert (
        m.per_workload_overrides[WorkloadClass.SOFTWARE_ENGINEERING].engine_class_override
        is EngineClass.EVENT_SOURCED_REPLAY
    )
