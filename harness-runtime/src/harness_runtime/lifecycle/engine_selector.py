"""Engine-class selection lifecycle — stage 3b CP_ROUTING (U-RT-22).

Per `Spec_Harness_Runtime_v1.md` v1.1 §5 (C-RT-02 stage 3b invariants) and the
Phase 2 Session 3 Track A atomic decomposition §L5 (U-RT-22). The runtime wires
engine-class selection over CP's binding-time selection procedure
(`harness_cp.workload_binding_engine_class_selection.select_engine_class` —
U-CP-17) and the `workload_engine_class_matrix` 2D matrix (U-CP-25 — informs
the spec posture; the runtime selector composes selection on demand rather
than reading the matrix).

Per-component landing posture:
- `EngineSelectorBindError` — bootstrap-time selection failure
  (`RT-FAIL-BOOTSTRAP`). Raised when *any* `(WorkloadClass, PersonaTier)`
  combination fails to bind at the runtime's `deployment_surface`. AC #2:
  missing binding raises typed error at bootstrap, not at runtime.
- `RuntimeEngineSelector` — frozen concrete `EngineSelector` Protocol
  implementation. Pre-resolves every `(WorkloadClass, PersonaTier)`
  combination at bootstrap; lookup is total at runtime.
- `materialize_engine_selector(config)` — composer; runs the bootstrap
  exhaustion check, applies manifest overrides per `WorkloadRoutingOverride.
  engine_class_override`, returns the frozen selector.

Manifest-override semantics. If a `WorkloadRoutingOverride.engine_class_override`
is set for a `WorkloadClass`, the runtime selector returns the override for
every `PersonaTier` at that workload class (the override forces the engine
class per the operator-ratified W-2 schema). The override SKIPS the
persona-tier-admissibility filter — the operator forcing pure-pattern at a
team-binding persona tier is the operator's call per W-2's "force" semantics.
This matches the W-2 docstring at `harness_cp.routing_manifest_residence`.

Scope discipline (U-RT-22 boundary held): NO topology dispatch (U-RT-40), NO
retry/breaker primitives (U-RT-24), NO HITL/handoff registries (U-RT-25/26),
NO fallback-chain runtime (U-RT-23). The selector is binding-time only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from harness_core import PersonaTier, WorkloadClass
from harness_cp.engine_class import EngineClass
from harness_cp.routing_manifest_residence import RoutingManifest
from harness_cp.workload_binding_engine_class_selection import (
    WorkloadBindingError,
    WorkloadBindingSelectionInput,
    WorkloadBindingSelectionResult,
    select_engine_class,
)

from harness_runtime.types import RuntimeConfig

__all__ = [
    "EngineSelectorBindError",
    "RuntimeEngineSelector",
    "materialize_engine_selector",
]


# Every (WorkloadClass, PersonaTier) combination — 4 * 3 = 12 at HEAD.
# Built eagerly so bootstrap exhaustion is total and deterministic; iteration
# order is `WorkloadClass`-then-`PersonaTier` (enum declaration order) for
# stable error-message ordering when bootstrap fails.
_ALL_COMBINATIONS: Final[tuple[tuple[WorkloadClass, PersonaTier], ...]] = tuple(
    (wc, pt) for wc in WorkloadClass for pt in PersonaTier
)


class EngineSelectorBindError(Exception):
    """One or more `(WorkloadClass, PersonaTier)` combinations failed to bind.

    Bootstrap-time failure per U-RT-22 AC: a missing binding raises here, not
    at runtime. Carries the exhaustive list of `(workload_class, persona_tier,
    reason)` tuples so the bootstrap orchestrator can surface every failing
    combination in one error.
    """

    def __init__(
        self,
        failures: tuple[tuple[WorkloadClass, PersonaTier, str], ...],
    ) -> None:
        self.failures = failures
        rendered = "; ".join(f"({wc.value}, {pt.value}): {reason}" for wc, pt, reason in failures)
        super().__init__(f"EngineSelectorBind: {len(failures)} unbound combination(s): {rendered}")


@dataclass(frozen=True, slots=True)
class RuntimeEngineSelector:
    """Frozen `EngineSelector` Protocol implementation.

    Pre-resolves every `(WorkloadClass, PersonaTier)` combination at the
    runtime's `deployment_surface`. Lookup is O(1) and total at runtime.

    `overrides` is the per-workload-class override mapping derived from the
    manifest's `per_workload_overrides[wc].engine_class_override`. When set
    for a workload class, `select()` returns the override regardless of
    `persona_tier`. `bindings` holds the `select_engine_class` results for
    every combination not overridden (still pre-resolved per AC #2 even when
    overridden — verifying the operator's runtime is internally consistent).
    """

    bindings: dict[tuple[WorkloadClass, PersonaTier], EngineClass]
    selection_results: dict[tuple[WorkloadClass, PersonaTier], WorkloadBindingSelectionResult]
    overrides: dict[WorkloadClass, EngineClass]

    def select(
        self,
        workload_class: WorkloadClass,
        persona_tier: PersonaTier,
    ) -> EngineClass:
        """Return the bound `EngineClass` for this `(workload_class, persona_tier)`.

        Manifest override (W-2 `engine_class_override`) wins over `select_engine_class`
        binding when set. Both routes were exercised at bootstrap (U-RT-22 AC):
        a `KeyError` from this method is a runtime invariant violation, never an
        expected control flow — every combination resolved at bootstrap.
        """
        override = self.overrides.get(workload_class)
        if override is not None:
            return override
        return self.bindings[(workload_class, persona_tier)]


def materialize_engine_selector(config: RuntimeConfig) -> RuntimeEngineSelector:
    """Build the `RuntimeEngineSelector` at stage 3b CP_ROUTING.

    Stage 3b composer. For every `(WorkloadClass, PersonaTier)` combination at
    the runtime's `config.deployment_surface`:

    1. Run `select_engine_class` over `WorkloadBindingSelectionInput`.
    2. Collect the resolved `EngineClass` into `bindings`.
    3. On `WorkloadBindingError`, accumulate the failure into a typed list.

    If any combination fails, raise `EngineSelectorBindError` carrying all
    failures (U-RT-22 AC: missing binding raises at bootstrap, not at runtime).

    Manifest overrides (`WorkloadRoutingOverride.engine_class_override`) are
    extracted into the `overrides` mapping. Bindings are still resolved for
    overridden workload classes — the operator's runtime should be internally
    consistent and overriding shouldn't hide a CP-side binding regression.
    """
    bindings: dict[tuple[WorkloadClass, PersonaTier], EngineClass] = {}
    selection_results: dict[tuple[WorkloadClass, PersonaTier], WorkloadBindingSelectionResult] = {}
    failures: list[tuple[WorkloadClass, PersonaTier, str]] = []

    for workload_class, persona_tier in _ALL_COMBINATIONS:
        try:
            result = select_engine_class(
                WorkloadBindingSelectionInput(
                    workload_class=workload_class,
                    deployment_surface=config.deployment_surface,
                    persona_tier=persona_tier,
                )
            )
        except WorkloadBindingError as exc:
            failures.append((workload_class, persona_tier, str(exc)))
            continue
        key = (workload_class, persona_tier)
        bindings[key] = result.selected_class
        selection_results[key] = result

    if failures:
        raise EngineSelectorBindError(tuple(failures))

    overrides = _extract_overrides(config.routing_manifest)
    return RuntimeEngineSelector(
        bindings=bindings,
        selection_results=selection_results,
        overrides=overrides,
    )


def _extract_overrides(manifest: RoutingManifest) -> dict[WorkloadClass, EngineClass]:
    """Pull per-workload `engine_class_override` entries from the manifest.

    Returns only workload classes whose `WorkloadRoutingOverride.engine_class_override`
    is non-None. Empty mapping is fine — the selector falls through to its
    `bindings` table for every workload class without an override.
    """
    return {
        wc: override.engine_class_override
        for wc, override in manifest.per_workload_overrides.items()
        if override.engine_class_override is not None
    }
