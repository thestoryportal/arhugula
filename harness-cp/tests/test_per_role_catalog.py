"""Tests for the per-role binding catalog surface — R-FS-1 B4 Slice 2.

Covers the single-source-of-truth `step_id → AgentRole` derivation contract and
the deterministic catalog-coherence validator (CP spec v1.32 §25.14 / runtime
spec v1.48 §14.5.3 catalog; C-CP-01 §1.3 + C-CP-29 §29).

  derivation is the identity-on-string contract  -> test_derive_agent_role_is_string_identity
  driver + operator agree by construction        -> test_derive_matches_driver_literal
  fan-out role set is the derived set            -> test_derive_fanout_roles
  coherence: live / dead / unbound partition     -> test_catalog_coherence_partition
  fully-bound catalog has no dead/unbound         -> test_fully_bound_catalog
  empty catalog -> all roles unbound (fall-through)-> test_empty_catalog_all_unbound
  dead binding (typo) surfaced, never raises      -> test_dead_binding_surfaced_advisory
  validator is generic over both manifests        -> test_generic_over_routing_and_prompt_catalogs
"""

from __future__ import annotations

from harness_core import StepID, WorkloadClass
from harness_cp.cp_shared_types import AgentRole, ModelBinding
from harness_cp.per_role_catalog import (
    PerRoleCatalogCoherence,
    derive_agent_role,
    derive_fanout_roles,
    validate_per_role_catalog,
)
from harness_cp.prompt_selection_manifest import PromptBinding, PromptSelectionManifest
from harness_cp.routing_manifest_residence import RoleRoutingBinding, RoutingManifest


def test_derive_agent_role_is_string_identity() -> None:
    assert derive_agent_role(StepID("summarize")) == AgentRole("summarize")
    # AgentRole + StepID are both open str newtypes; the derivation is the
    # identity on the underlying string.
    assert str(derive_agent_role(StepID("writer-3"))) == "writer-3"


def test_derive_matches_driver_literal() -> None:
    # The contract MUST equal the literal the fan-out drivers historically inlined
    # (`AgentRole(str(step.step_id))`) — else an operator's catalog and the driver
    # would key on different roles. This is the regression guard for the refactor.
    for raw in ("orchestrator", "0", "worker.1", "decide-route"):
        step_id = StepID(raw)
        assert derive_agent_role(step_id) == AgentRole(str(step_id))


def test_derive_fanout_roles() -> None:
    roles = derive_fanout_roles([StepID("a"), StepID("b"), StepID("a")])
    assert roles == frozenset({AgentRole("a"), AgentRole("b")})


def test_catalog_coherence_partition() -> None:
    derivable = [AgentRole("researcher"), AgentRole("writer"), AgentRole("critic")]
    bound = [AgentRole("researcher"), AgentRole("writer"), AgentRole("STALE-typo")]
    report = validate_per_role_catalog(derivable_roles=derivable, bound_roles=bound)

    assert isinstance(report, PerRoleCatalogCoherence)
    assert report.live_roles == frozenset({AgentRole("researcher"), AgentRole("writer")})
    assert report.dead_bindings == frozenset({AgentRole("STALE-typo")})
    assert report.unbound_roles == frozenset({AgentRole("critic")})
    assert report.has_dead_bindings is True


def test_fully_bound_catalog() -> None:
    roles = [AgentRole("a"), AgentRole("b")]
    report = validate_per_role_catalog(derivable_roles=roles, bound_roles=roles)
    assert report.live_roles == frozenset(roles)
    assert report.dead_bindings == frozenset()
    assert report.unbound_roles == frozenset()
    assert report.has_dead_bindings is False


def test_empty_catalog_all_unbound() -> None:
    # An empty catalog binds nothing -> every fan-out role falls through to the
    # default (the committed §14.5.3 / §29 lookup-miss policy). Informational,
    # not an error.
    derivable = [AgentRole("a"), AgentRole("b")]
    report = validate_per_role_catalog(derivable_roles=derivable, bound_roles=[])
    assert report.live_roles == frozenset()
    assert report.dead_bindings == frozenset()
    assert report.unbound_roles == frozenset(derivable)


def test_dead_binding_surfaced_advisory() -> None:
    # A dead binding is surfaced but NEVER raises — a superset catalog reused
    # across workflows legitimately binds roles unused by any one of them.
    report = validate_per_role_catalog(
        derivable_roles=[AgentRole("real")],
        bound_roles=[AgentRole("ghost")],
    )
    assert report.dead_bindings == frozenset({AgentRole("ghost")})
    assert report.has_dead_bindings is True
    # frozen result
    assert report.model_config["frozen"] is True


def test_generic_over_routing_and_prompt_catalogs() -> None:
    # The validator takes a plain role iterable, so it serves BOTH manifests with
    # the same call shape (`manifest.per_role_bindings.keys()`).
    fanout = [StepID("researcher"), StepID("writer")]
    derivable = derive_fanout_roles(fanout)

    routing = RoutingManifest(
        manifest_version=1,
        per_role_bindings={
            AgentRole("researcher"): RoleRoutingBinding(
                preferred_model_binding=ModelBinding(provider="anthropic", model="opus"),
                layer_budget_overrides={},
            ),
        },
        per_workload_overrides={},
        fallback_chains=(),
        retry_policies={},
    )
    prompt = PromptSelectionManifest(
        manifest_version=1,
        per_role_bindings={
            AgentRole("writer"): PromptBinding(version_sha="abc123"),
        },
        per_workload_overrides={
            WorkloadClass.PIPELINE_AUTOMATION: PromptBinding(version_sha="zzz")
        },
    )

    routing_report = validate_per_role_catalog(
        derivable_roles=derivable, bound_roles=routing.per_role_bindings.keys()
    )
    prompt_report = validate_per_role_catalog(
        derivable_roles=derivable, bound_roles=prompt.per_role_bindings.keys()
    )

    assert routing_report.live_roles == frozenset({AgentRole("researcher")})
    assert routing_report.unbound_roles == frozenset({AgentRole("writer")})
    assert prompt_report.live_roles == frozenset({AgentRole("writer")})
    assert prompt_report.unbound_roles == frozenset({AgentRole("researcher")})
