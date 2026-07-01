"""R-400-deployment-surface-conditional-emission-suite — cross-surface OD emission.

The one CI-runnable Surface-V row. Consolidates the three deployment-conditional
OD-emission behaviors into ONE materializer-layer regression driven through the
*real* stage-4 composers across all DeploymentSurfaces:

  1. sampler base-rate envelope    — ``materialize_tracer_provider_stage`` resolves
     the default sampler's ``base_rate`` to ``PER_CELL_BASE_RATE_ENVELOPE[CellID(
     persona_tier, deployment_surface)].default_rate`` for every ACTIVE cell
     (the 3x3 product minus the one structurally-EXCLUDED cell).
  2. tail-keep conditional         — ``materialize_span_processor_stage`` wraps a
     ``TailKeepSpanProcessor`` IFF ``deployment_surface != LOCAL_DEVELOPMENT``
     (LOCAL uses the §9.1 head-based mandate; no wrap).
  3. redaction toggle resolution   — a ``RedactionSpanProcessor`` is present at
     every surface, constructed with the config's ``persona_tier`` so the §13.1
     per-persona toggle *resolves* through the stage.

Honest scope (mirrors the PR #190 deterministic-suite caveat). Unit coverage of
each behavior in isolation already exists:

  - base-rate envelope cardinality / invariants → ``test_base_rate_set_and_envelope.py``
  - per-persona redaction posture + toggleability → ``test_redaction_gradient.py``
  - tail-keep wrap-vs-bypass per surface          → ``test_lifecycle_span_processor.py``

The gap this suite closes is the *composed cross-surface* assertion driven
through the real materialize stages — NOT new behavior. Two honest MVP caveats:

  - The §13.1 per-persona toggle is *plumbed but not consumed at the SDK boundary*
    at MVP (``RedactionSpanProcessor`` is always constructed with the default
    ``redacted_attributes`` at ``span_processor.py``; the behavioral differential
    — empty-frozenset accepted at SOLO_DEVELOPER, refused at MULTI_TENANT_COMPLIANCE
    — is exercised in ``test_redaction_gradient.py``, not reachable through the
    stage). The assertion here is that the persona threads through to the
    processor, i.e. the toggle *resolves*.
  - The EXCLUDED cell (multi-tenant-compliance x local-development, C-OD-01 §1.4)
    is structurally rejected: ``materialize_tracer_provider_stage`` raises
    ``TracerProviderBindError`` (wrapping the ``CellBindingViolation`` thrown by
    ``reject_excluded_cell`` inside the composer's try-block). A negative case
    asserts this, mirroring #190's negative admissibility test.

Determinism (CI-runnable, free, flake-free): no key / ollama / daemon / real
collector. Every ``materialize_tracer_provider_stage`` call passes
``register_globally=False`` (the C-RT-06 one-shot global is a per-process
invariant; an autouse fixture resets the runtime-registration flag per test).
The span processor stage receives an ``InMemorySpanExporter`` override so no OTLP
network construction occurs. Carries no ``@pytest.mark.e2e`` — runs in the
default (not-e2e) CI suite.

Verification (roadmap R-400 ``must_pass``):
  - sampler base_rate == envelope default_rate for each surface (per ACTIVE cell)
  - tail-keep wrapped IFF deployment_surface != LOCAL_DEVELOPMENT
  - RedactionSpanProcessor present at all 3 surfaces; per-persona toggle resolves
  - deterministic; no key/ollama/daemon/real collector; not-e2e marker
"""

from __future__ import annotations

from pathlib import Path

import pytest
from harness_core import DeploymentSurface, PersonaTier
from harness_cp.topology_pattern import TopologyPattern
from harness_od.base_rate_set_and_envelope import PER_CELL_BASE_RATE_ENVELOPE
from harness_od.observability_matrix import (
    ACTIVE_CELLS,
    EXCLUDED_CELL,
    CellBindingViolation,
    CellID,
)
from harness_od.redaction_gradient import PER_PERSONA_TIER_REDACTION
from harness_od.redaction_span_processor import RedactionSpanProcessor
from harness_runtime.lifecycle.span_processor import materialize_span_processor_stage
from harness_runtime.lifecycle.tracer_provider import (
    TracerProviderBindError,
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
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# The 3 surfaces in canonical order; SOLO_DEVELOPER is admissible at every one
# (it is never the persona of the single EXCLUDED cell), so it is the persona
# used for the surface-conditional (tail-keep / redaction) sweeps.
_SURFACES: tuple[DeploymentSurface, ...] = (
    DeploymentSurface.LOCAL_DEVELOPMENT,
    DeploymentSurface.SELF_HOSTED_SERVER,
    DeploymentSurface.MANAGED_CLOUD,
)


@pytest.fixture(autouse=True)
def _reset_runtime_registration() -> None:
    """Reset the per-process runtime-registration flag before each test so the
    C-RT-06 one-shot global does not leak across the per-cell loop."""
    reset_runtime_registration_for_tests()


def _config(
    tmp_path: Path,
    *,
    deployment_surface: DeploymentSurface,
    persona_tier: PersonaTier,
) -> RuntimeConfig:
    """Minimal cross-surface ``RuntimeConfig`` (no network / no secrets)."""
    return RuntimeConfig(
        deployment_surface=deployment_surface,
        persona_tier=persona_tier,
        repository_root=tmp_path,
        path_bindings=PathBindingConfig(),
        provider_secrets=ProviderSecretsConfig(),
        otel=OTelConfig(otlp_endpoint="http://localhost:4317"),
        collector=CollectorConfig(),
        default_topology=TopologyPattern.SINGLE_THREADED_LINEAR,
    )


# ---------------------------------------------------------------------------
# must_pass[0] — sampler base_rate resolves to the per-cell envelope default
# across every surface, for every ACTIVE (persona_tier x deployment_surface) cell.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cell", sorted(ACTIVE_CELLS, key=str), ids=str)
def test_sampler_base_rate_matches_envelope_for_active_cell(tmp_path: Path, cell: CellID) -> None:
    """For each ACTIVE cell, the default sampler the tracer-provider composer
    builds carries ``base_rate == PER_CELL_BASE_RATE_ENVELOPE[cell].default_rate``.

    Reads the rate back through ``ParentBased._root`` (OTel stores the root
    sampler as a private attribute — verified empirically; there is no public
    ``.root``) → ``HarnessCompositeSampler.base_rate``.
    """
    stage = materialize_tracer_provider_stage(
        _config(
            tmp_path,
            deployment_surface=cell.deployment_surface,
            persona_tier=cell.persona_tier,
        ),
        register_globally=False,
    )
    # ParentBased stores the root sampler privately (no public `.root`).
    resolved = stage.provider.sampler._root.base_rate
    assert resolved == PER_CELL_BASE_RATE_ENVELOPE[cell].default_rate


def test_active_cells_cover_all_three_surfaces() -> None:
    """Completeness guard: the ACTIVE-cell sweep exercises every surface (so the
    per-cell sampler assertion above is genuinely *cross-surface*)."""
    surfaces_covered = {cell.deployment_surface for cell in ACTIVE_CELLS}
    assert surfaces_covered == set(_SURFACES)


def test_excluded_cell_is_structurally_rejected(tmp_path: Path) -> None:
    """The single EXCLUDED cell (multi-tenant x local-development, C-OD-01 §1.4)
    cannot be materialized: the composer raises ``TracerProviderBindError``
    wrapping the ``CellBindingViolation`` from ``reject_excluded_cell``."""
    with pytest.raises(TracerProviderBindError) as excinfo:
        materialize_tracer_provider_stage(
            _config(
                tmp_path,
                deployment_surface=EXCLUDED_CELL.deployment_surface,
                persona_tier=EXCLUDED_CELL.persona_tier,
            ),
            register_globally=False,
        )
    assert isinstance(excinfo.value.__cause__, CellBindingViolation)


# ---------------------------------------------------------------------------
# must_pass[1] — tail-keep wrapped IFF deployment_surface != LOCAL_DEVELOPMENT.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("surface", _SURFACES, ids=lambda s: s.value)
def test_tail_keep_wrapped_iff_not_local(tmp_path: Path, surface: DeploymentSurface) -> None:
    """``materialize_span_processor_stage`` wraps a ``TailKeepSpanProcessor`` at
    every non-LOCAL surface and bypasses it at LOCAL (§9.1 head-based mandate)."""
    config = _config(tmp_path, deployment_surface=surface, persona_tier=PersonaTier.SOLO_DEVELOPER)
    tracer_stage = materialize_tracer_provider_stage(config, register_globally=False)
    span_stage = materialize_span_processor_stage(
        config, tracer_stage.provider, exporter=InMemorySpanExporter()
    )

    is_local = surface == DeploymentSurface.LOCAL_DEVELOPMENT
    assert (span_stage.tail_keep_processor is None) is is_local
    if not is_local:
        # The wrap forwards into the BSP per §10.2.
        assert span_stage.tail_keep_processor is not None
        assert span_stage.tail_keep_processor.downstream is span_stage.processor


# ---------------------------------------------------------------------------
# must_pass[2] — RedactionSpanProcessor present at all 3 surfaces; the §13.1
# per-persona toggle resolves (persona threads through to the processor).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("surface", _SURFACES, ids=lambda s: s.value)
def test_redaction_processor_present_at_every_surface(
    tmp_path: Path, surface: DeploymentSurface
) -> None:
    """A ``RedactionSpanProcessor`` is wired at every surface, carrying the
    config's ``persona_tier`` — the §13.1 toggle *resolves* through the stage.

    The behavioral toggle differential (empty-frozenset accepted at
    SOLO_DEVELOPER, refused at MULTI_TENANT_COMPLIANCE) is not reachable through
    materialize at MVP — it is covered in ``test_redaction_gradient.py``.
    """
    persona = PersonaTier.SOLO_DEVELOPER  # admissible at every surface
    config = _config(tmp_path, deployment_surface=surface, persona_tier=persona)
    tracer_stage = materialize_tracer_provider_stage(config, register_globally=False)
    span_stage = materialize_span_processor_stage(
        config, tracer_stage.provider, exporter=InMemorySpanExporter()
    )

    assert isinstance(span_stage.redaction_processor, RedactionSpanProcessor)
    assert span_stage.redaction_processor.persona_tier == persona


def test_redaction_persona_threads_for_every_active_cell(tmp_path: Path) -> None:
    """Across every ACTIVE cell, the redaction processor the stage builds carries
    that cell's persona, and the resolved §13.1 posture is the canonical
    ``PER_PERSONA_TIER_REDACTION`` row for that persona (toggle resolution)."""
    for cell in ACTIVE_CELLS:
        reset_runtime_registration_for_tests()
        config = _config(
            tmp_path,
            deployment_surface=cell.deployment_surface,
            persona_tier=cell.persona_tier,
        )
        tracer_stage = materialize_tracer_provider_stage(config, register_globally=False)
        span_stage = materialize_span_processor_stage(
            config, tracer_stage.provider, exporter=InMemorySpanExporter()
        )
        assert span_stage.redaction_processor.persona_tier == cell.persona_tier
        # The §13.1 posture for this persona is resolvable + canonical.
        assert cell.persona_tier in PER_PERSONA_TIER_REDACTION
