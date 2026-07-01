"""R-CL-P3 — cross-axis multi-tier persona posture proof.

Proves the bridging-arc **middle tier** (`TEAM_BINDING`) exhibits a runtime
posture distinct from *both* `SOLO_DEVELOPER` and `MULTI_TENANT_COMPLIANCE`
across two axes, by driving the **real run-path surfaces** from a
``persona_tier``-parametrized config / binding — not by re-asserting the
declarative per-tier tables:

- **OD / observability — trace sampler base-rate.** ``materialize_tracer_provider_stage``
  consumes ``config.persona_tier`` at bootstrap and binds
  ``HarnessCompositeSampler(base_rate=PER_CELL_BASE_RATE_ENVELOPE[(persona_tier,
  surface)].default_rate)`` (``tracer_provider.py``; OD spec C-OD-10 §10.3).
- **CP / HITL — gate synchrony class.** The runtime gate composer resolves the
  HITL synchrony via ``_evaluate_cell_synchrony(binding)`` →
  ``matrix_cell_for(binding.persona_tier, engine_class).synchrony_class``
  (``hitl_gate_composer.py``; CP spec C-CP-19 §18.1, runtime §14.8.8.3).

At ``SELF_HOSTED_SERVER`` the sampler base-rate is **SOLO 1.0 / TEAM 0.1 /
MTC 0.2** and at ``SAVE_POINT_CHECKPOINT`` the gate synchrony is **SOLO
SYNC_BLOCKING / TEAM BOTH_BY_TIER / MTC DURABLE_ASYNC** — on each axis the
middle tier differs from both neighbours, so the distinctness assertions are
non-vacuous (every compared value genuinely differs).

**Scope honesty — the four §P3 behaviours are not equally run-observable.**
Only the two proven here flow into in-process runtime behaviour:

- *Redaction* (``PER_PERSONA_TIER_REDACTION``) describes the OTLP-collector
  *boundary* posture (operator-self-redact → collector-boundary processor →
  pre-collector eval-grade pipeline). It is consumed by the OD verification /
  composition surfaces (``bridging_arc_table``, ``cross_deployment_monotonic_tightening``),
  **not** the in-process span-emission path, so it is not observable from a
  local run.
- *Cost attribution* is not surfaced through a run: ``RunResult.cost_attribution``
  is an empty tuple while the U-RT-49 cost-surfacing AC stays STRUCK
  (``api.py``; ``.harness/class_1_tension_u_od_21_span_cost_record_missing_rollup_keys.md``).

Authority: ``.harness/post-mvp-full-closure-plan-v1.md`` §P2→P3; C-CP-19,
C-OD-10; `Persona_Document_v1.md` bridging-arc (solo → team-binding →
multi-tenant-compliance).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from harness_core.deployment_surface import DeploymentSurface
from harness_core.persona_tier import PersonaTier
from harness_cp.cp_shared_types import ModelBinding
from harness_cp.engine_class import EngineClass
from harness_cp.per_step_override_evaluator import StepEffectiveBinding
from harness_cp.persona_engine_hitl_matrix import SynchronyClass
from harness_cp.topology_pattern import TopologyPattern
from harness_runtime.lifecycle.hitl_gate_composer import _evaluate_cell_synchrony
from harness_runtime.lifecycle.tracer_provider import (
    materialize_tracer_provider_stage,
    reset_runtime_registration_for_tests,
)
from harness_runtime.types import (
    CollectorConfig,
    OTelConfig,
    PathBindingConfig,
    ProviderSecretsConfig,
    RuntimeConfig,
)

# `SELF_HOSTED_SERVER` is the surface where all three tiers bind a valid cell
# (the only EXCLUDED cell is MTC × LOCAL_DEVELOPMENT); pinning it keeps the
# tier the sole varying dimension of the posture comparison.
_SURFACE = DeploymentSurface.SELF_HOSTED_SERVER

# `SAVE_POINT_CHECKPOINT` is the engine class whose §18.1 matrix column gives a
# distinct synchrony for each tier; other columns collapse TEAM≡MTC, which would
# make a "distinct from both" assertion vacuous on this axis.
_ENGINE_CLASS = EngineClass.SAVE_POINT_CHECKPOINT

#: Expected per-tier sampler base-rate at `SELF_HOSTED_SERVER` (C-OD-10 §10.3).
_EXPECTED_BASE_RATE: dict[PersonaTier, float] = {
    PersonaTier.SOLO_DEVELOPER: 1.0,
    PersonaTier.TEAM_BINDING: 0.1,
    PersonaTier.MULTI_TENANT_COMPLIANCE: 0.2,
}

#: Expected per-tier gate synchrony at `SAVE_POINT_CHECKPOINT` (C-CP-19 §18.1).
_EXPECTED_SYNCHRONY: dict[PersonaTier, SynchronyClass] = {
    PersonaTier.SOLO_DEVELOPER: SynchronyClass.SYNC_BLOCKING,
    PersonaTier.TEAM_BINDING: SynchronyClass.BOTH_BY_TIER,
    PersonaTier.MULTI_TENANT_COMPLIANCE: SynchronyClass.DURABLE_ASYNC,
}


@pytest.fixture(autouse=True)
def _reset_runtime_registration() -> None:
    """Reset the per-process tracer-provider registration flag per test."""
    reset_runtime_registration_for_tests()


def _config(tmp_path: Path, persona_tier: PersonaTier) -> RuntimeConfig:
    """A minimal `RuntimeConfig` varying only `persona_tier` (surface pinned)."""
    return RuntimeConfig(
        deployment_surface=_SURFACE,
        persona_tier=persona_tier,
        repository_root=tmp_path,
        path_bindings=PathBindingConfig(),
        provider_secrets=ProviderSecretsConfig(),
        otel=OTelConfig(otlp_endpoint="http://localhost:4317"),
        collector=CollectorConfig(),
        default_topology=TopologyPattern.SINGLE_THREADED_LINEAR,
    )


def _bound_sampler_base_rate(tmp_path: Path, persona_tier: PersonaTier) -> float:
    """The base-rate the real bootstrap binds for `persona_tier` (no global reg)."""
    stage = materialize_tracer_provider_stage(
        _config(tmp_path, persona_tier), register_globally=False
    )
    description = stage.provider.sampler.get_description()
    match = re.search(r"base_rate=([0-9.]+)", description)
    assert match is not None, f"no base_rate in sampler description: {description!r}"
    return float(match.group(1))


def _resolved_gate_synchrony(persona_tier: PersonaTier) -> SynchronyClass | None:
    """The synchrony the runtime gate composer resolves for `persona_tier`."""
    binding = StepEffectiveBinding(
        step_id="r-cl-p3-gate",
        model_binding=ModelBinding(provider="anthropic", model="claude-opus-4-7"),
        engine_class=_ENGINE_CLASS,
        override_applied=False,
        persona_tier=persona_tier,
    )
    return _evaluate_cell_synchrony(binding)


# ---------------------------------------------------------------------------
# Per-tier bound values (drives the real run-path surfaces).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("persona_tier", list(_EXPECTED_BASE_RATE))
def test_bootstrap_binds_per_tier_sampler_base_rate(
    tmp_path: Path, persona_tier: PersonaTier
) -> None:
    """`config.persona_tier` flows through bootstrap to the bound sampler rate."""
    assert _bound_sampler_base_rate(tmp_path, persona_tier) == _EXPECTED_BASE_RATE[persona_tier]


@pytest.mark.parametrize("persona_tier", list(_EXPECTED_SYNCHRONY))
def test_runtime_gate_resolves_per_tier_synchrony(persona_tier: PersonaTier) -> None:
    """The runtime gate composer resolves the §18.1 synchrony for the tier."""
    assert _resolved_gate_synchrony(persona_tier) == _EXPECTED_SYNCHRONY[persona_tier]


# ---------------------------------------------------------------------------
# The middle tier is distinct from BOTH neighbours (the §P3 deliverable).
# ---------------------------------------------------------------------------


def test_team_binding_sampler_posture_distinct_from_both_neighbours(
    tmp_path: Path,
) -> None:
    """TEAM_BINDING sampler base-rate differs from SOLO and from MTC."""
    solo = _bound_sampler_base_rate(tmp_path, PersonaTier.SOLO_DEVELOPER)
    team = _bound_sampler_base_rate(tmp_path, PersonaTier.TEAM_BINDING)
    mtc = _bound_sampler_base_rate(tmp_path, PersonaTier.MULTI_TENANT_COMPLIANCE)
    assert team != solo
    assert team != mtc


def test_team_binding_gate_posture_distinct_from_both_neighbours() -> None:
    """TEAM_BINDING gate synchrony differs from SOLO and from MTC."""
    solo = _resolved_gate_synchrony(PersonaTier.SOLO_DEVELOPER)
    team = _resolved_gate_synchrony(PersonaTier.TEAM_BINDING)
    mtc = _resolved_gate_synchrony(PersonaTier.MULTI_TENANT_COMPLIANCE)
    assert team != solo
    assert team != mtc


def test_team_binding_distinct_on_both_axes_jointly(tmp_path: Path) -> None:
    """The cross-axis claim: TEAM_BINDING is the unique tier whose (sampler,
    gate) posture pair matches neither neighbour on either axis."""
    postures = {
        tier: (
            _bound_sampler_base_rate(tmp_path, tier),
            _resolved_gate_synchrony(tier),
        )
        for tier in PersonaTier
    }
    team = postures[PersonaTier.TEAM_BINDING]
    solo = postures[PersonaTier.SOLO_DEVELOPER]
    mtc = postures[PersonaTier.MULTI_TENANT_COMPLIANCE]
    # Differs from each neighbour on BOTH axes (not just one).
    assert team[0] != solo[0] and team[1] != solo[1]
    assert team[0] != mtc[0] and team[1] != mtc[1]
