"""OTel TracerProvider construction + global registration — stage 4 OD (U-RT-27).

Per `Spec_Harness_Runtime_v1.md` v1.1 §6 (C-RT-06 TracerProvider lifecycle —
F-P2-3 absorption) and the Phase 2 Session 3 Track A atomic decomposition §L6
(U-RT-27). The runtime constructs the SDK `TracerProvider`, registers it
globally, and stores the handle on the materialization stage so the
bootstrap orchestrator (U-RT-43) can bind it to `HarnessContext.tracer_provider`.

C-RT-06 construction sequence (4 steps; U-RT-27 lands steps 1, 2, 4):

1. Build `Resource` from `RuntimeConfig.deployment_surface` + the 15-row
   namespace map (delegated to `harness_runtime.config.otel_config.build_resource_attributes`,
   landed at U-RT-07).
2. Construct `TracerProvider(resource=resource, sampler=sampler)` — sampler
   resolved per C-OD-09 §9.1 (delegated to `resolve_sampling_mode`, U-RT-07).
3. [DEFERRED to U-RT-28] Attach `BatchSpanProcessor(OTLPSpanExporter(...))`.
4. Call `opentelemetry.trace.set_tracer_provider(provider)` — the global
   registration that landed OD `operator_burden_eval_primitives.py`'s
   `get_tracer_provider()` call depends on.

**Sampler choice (project-authored, C-OD-09 §9.2-honoring).** This module
binds `ParentBased(root=HarnessCompositeSampler(base_rate=1.0))` per OD
spec v1.2 §9.2 always-sampled-set discipline:

- `HEAD_BASED_DEV` (local-development): MVP base_rate=1.0 matches §10.3
  solo-developer × local-development row; the §9.2 always-sampled set is
  honored as defense-in-depth (members sample at head=1.0 regardless of
  ratio).
- `TAIL_BASED_PROD` (self-hosted-server + managed-cloud): SDK emits
  per-base_rate at root spans NOT in the always-sampled set; tail-decision
  at the OTLP collector boundary is owed at a follow-on arc (C-OD-09 §9.1
  tail-keep-on-classification + per-attribute conditional-row refinement
  at §9.2 — both deferred).

H_T-OD-3 STILL-BOUNDED → PARTIAL at this binding (substrate retired; full
RETIRE-READY gates on tail-keep-on-classification + persona-tier-aware
base_rate envelope).

**Global-state caveat.** `set_tracer_provider(...)` is process-global and
one-shot per OTel SDK semantics (subsequent calls log a warning and are
no-ops). The composer accepts `register_globally: bool = True` to skip the
global registration for tests; the AC test that verifies registration uses
a single fresh provider per test session.

Per-component landing posture:

- `TracerProviderBindError` — bootstrap-time bind failure (RT-FAIL-BOOTSTRAP).
- `TracerProviderConcurrentRegistrationError` — RT-FAIL-CONCURRENT-REGISTRATION
  (C-RT-14): a second `set_tracer_provider` call from within the runtime
  (orchestrator bug surface; explicit error rather than silent OTel-SDK
  warning swallow).
- `TracerProviderStage` — frozen materialization stage carrying the
  registered `TracerProvider`.
- `materialize_tracer_provider_stage(config, *, register_globally)` —
  composer.

Scope discipline (U-RT-27 boundary held): NO `BatchSpanProcessor` attachment
(U-RT-28), NO OTLP exporter wiring (U-RT-28), NO collector daemon
supervision (U-RT-29), NO ring-buffer or sqlite rotation (U-RT-30), NO
cost-attribution chain (U-RT-31), NO audit-ledger writer (U-RT-32). This
unit lands the provider construction + global registration only.
"""

from __future__ import annotations

from dataclasses import dataclass

from harness_od.base_rate_set_and_envelope import PER_CELL_BASE_RATE_ENVELOPE
from harness_od.composite_sampler import build_default_sampler
from harness_od.observability_matrix import CellID, reject_excluded_cell
from opentelemetry import trace as ot_trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import Sampler

from harness_runtime.config.otel_config import (
    build_resource_attributes,
    resolve_sampling_mode,
)
from harness_runtime.types import RuntimeConfig

__all__ = [
    "TracerProviderBindError",
    "TracerProviderConcurrentRegistrationError",
    "TracerProviderStage",
    "materialize_tracer_provider_stage",
]


# NOTE: As of the OD-3+OD-4 persona_tier plumbing arc (fork doc
# `class_1_fork_od_3_od_4_retire_ready_persona_tier_plumbing.md`,
# operator-ratified 2026-05-28), the default sampler is constructed per-call
# inside `materialize_tracer_provider_stage` reading
# `(config.persona_tier, config.deployment_surface)` per OD spec §10.3 8-row
# base_rate envelope. The module-level `_DEFAULT_SAMPLER: Final[Sampler] =
# build_default_sampler()` constant has been retired — it forced base_rate=1.0
# at every cell and was the production gap blocking OD-3 PARTIAL → RETIRE-READY.


class TracerProviderBindError(Exception):
    """Bootstrap-time TracerProvider bind failure (RT-FAIL-BOOTSTRAP).

    Raised when the resource-attribute build or sampler resolution fails
    before the SDK `TracerProvider` can be constructed. Surfaces at
    `materialize_tracer_provider_stage`, never at runtime."""


class TracerProviderConcurrentRegistrationError(Exception):
    """RT-FAIL-CONCURRENT-REGISTRATION (C-RT-14 + C-RT-06 invariants).

    Raised when the composer detects that `set_tracer_provider(...)` has
    already been called within this process by the runtime — indicating
    either an orchestrator bug or a `run()` concurrent-invocation violation
    (C-RT-08). The OTel SDK's own one-shot semantics also detect this, but
    the SDK only logs a warning; this typed error surfaces the violation as
    a runtime fault.
    """


# Module-private flag tracking whether the runtime has registered a provider
# this process. Distinct from the OTel SDK's internal one-shot flag — this
# tracks runtime-driven registrations (so tests that call set_tracer_provider
# directly aren't confused for runtime double-registration).
_registered_by_runtime: bool = False


def reset_runtime_registration_for_tests() -> None:
    """Reset the runtime-registration flag for test isolation only.

    NOT exported in `__all__`; tests in `test_lifecycle_tracer_provider`
    invoke this between materialize calls to reset the per-process state.
    Production code does not call this — `set_tracer_provider` remains
    one-shot per process per C-RT-06 invariants. The name carries no leading
    underscore so pyright's `reportPrivateUsage` does not flag cross-module
    test use; the `_for_tests` suffix carries the intent.
    """
    global _registered_by_runtime
    _registered_by_runtime = False


@dataclass(frozen=True, slots=True)
class TracerProviderStage:
    """Frozen result of stage 4 OD TracerProvider materialization.

    Mirrors the L4 / L5 stage shape. The bootstrap orchestrator (U-RT-43)
    binds `provider` to `HarnessContext.tracer_provider` for diagnostic
    introspection; consumers acquire tracers via
    `opentelemetry.trace.get_tracer(...)` (which reads the global provider)
    rather than reaching into `HarnessContext.tracer_provider` directly,
    per C-RT-06 invariant.

    `registered_globally` records whether the composer called
    `set_tracer_provider(...)`; in production this is always `True`, the
    field exists for test introspection.
    """

    provider: TracerProvider
    registered_globally: bool


def materialize_tracer_provider_stage(
    config: RuntimeConfig,
    *,
    register_globally: bool = True,
    sampler: Sampler | None = None,
) -> TracerProviderStage:
    """Build the stage 4 OD `TracerProviderStage` per C-RT-06.

    Stage 4 composer. Construction sequence:

    1. Build the resource attribute dict via `build_resource_attributes` (U-RT-07).
    2. Construct `Resource.create(attrs)`.
    3. Resolve the sampling mode via `resolve_sampling_mode` (U-RT-07); the
       sampler keyword argument overrides this composition for tests that need
       a deterministic sampler.
    4. Construct `TracerProvider(resource=..., sampler=...)`.
    5. If `register_globally`, call `opentelemetry.trace.set_tracer_provider(...)`;
       raise `TracerProviderConcurrentRegistrationError` if the runtime has
       already registered a provider this process.

    Parameters
    ----------
    config :
        Frozen `RuntimeConfig` (U-RT-04). Drives resource attributes + sampler
        resolution.
    register_globally :
        When True (production), the composer calls `set_tracer_provider(...)`.
        When False (tests), the provider is constructed but NOT registered
        globally — allowing per-test isolation against the OTel SDK's one-shot
        global state.
    sampler :
        Optional SDK `Sampler` override. When `None` (production), the
        composer constructs
        `ParentBased(root=HarnessCompositeSampler(base_rate=base_rate_for(
        config.persona_tier, config.deployment_surface)))` per OD spec §10.3
        8-row table at `harness_od.base_rate_envelope`. Tests pass
        deterministic samplers; production code does not thread this argument.

    Returns
    -------
    TracerProviderStage
        Frozen handle carrying the constructed provider + the registration flag.

    Raises
    ------
    TracerProviderConcurrentRegistrationError
        When `register_globally=True` and the runtime has already registered
        a provider this process.
    TracerProviderBindError
        Wrap-and-re-raise for resource / sampler resolution failures (none at
        HEAD; reserved surface for future config-validation extensions).
    """
    global _registered_by_runtime

    try:
        attrs = build_resource_attributes(
            config.otel,
            config.deployment_surface,
            tenant_id=config.tenant_id,
        )
        if sampler is not None:
            resolved_sampler = sampler
        else:
            # OD spec §10.3 8-row table — base_rate per cell
            # (persona_tier × deployment_surface) via canonical substrate
            # `PER_CELL_BASE_RATE_ENVELOPE` (U-OD-12). `reject_excluded_cell`
            # raises `CellBindingViolation` at the single EXCLUDED cell
            # (multi-tenant × local-development), surfacing misconfiguration
            # at bootstrap rather than silent fallback.
            cell = CellID(
                persona_tier=config.persona_tier,
                deployment_surface=config.deployment_surface,
            )
            reject_excluded_cell(cell)
            base_rate = PER_CELL_BASE_RATE_ENVELOPE[cell].default_rate
            resolved_sampler = build_default_sampler(base_rate=base_rate)
        # Resolve the sampling mode for the composer's audit trail; the
        # current default sampler ignores the mode (over-sample at SDK,
        # defer tail to collector — see module docstring). Future units may
        # wire mode-conditional samplers.
        _ = resolve_sampling_mode(config.otel, config.deployment_surface)
    except Exception as exc:
        raise TracerProviderBindError(
            f"TracerProvider bind failed at resource-attribute build / sampler resolution: {exc}"
        ) from exc

    resource = Resource.create(attrs)
    provider = TracerProvider(resource=resource, sampler=resolved_sampler)

    if register_globally:
        if _registered_by_runtime:
            raise TracerProviderConcurrentRegistrationError(
                "set_tracer_provider was already called by the runtime in this "
                "process (C-RT-06 invariant — exactly once per process). "
                "Likely cause: orchestrator double-bootstrap or concurrent "
                "run() invocation (C-RT-08)."
            )
        ot_trace.set_tracer_provider(provider)
        _registered_by_runtime = True

    return TracerProviderStage(
        provider=provider,
        registered_globally=register_globally,
    )
