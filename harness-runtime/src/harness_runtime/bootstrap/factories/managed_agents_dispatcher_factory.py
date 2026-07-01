"""C-RT-28 §14.20.3 — ManagedAgentsStepDispatcher stage-5 LOOP_INIT factory.

Implements the runtime spec v1.55 §14.20.3 factory signature + stage-5
LOOP_INIT placement + §14.20.4 failure-mode taxonomy + §14.20.5 surface-gating
invariants (R-FS-1 arc M; operator-ratified 2026-06-17, Option B).

Surface-gated operator-opt-in (mirrors `materialize_skill_activation_emitter_stage`):

- Opt-out branch (`config.managed_agents_config is None`) → return `None`
  (no `StepKind.MANAGED_AGENTS` binding; a managed-agents step fails closed at
  `registry.lookup`).
- Non-managed-cloud branch (`config.deployment_surface != MANAGED_CLOUD`) →
  return `None` (the H_T-AS-8f local-development exclusion remains TRUE; the
  opt-in is honored only on managed-cloud).
- Opt-in branch (config present AND surface == MANAGED_CLOUD) → construct a
  `ManagedAgentsStepDispatcher` over the operator-supplied client.
- Construction failure raises `ManagedAgentsStageMaterializeError` (fail class
  `RT-FAIL-MANAGED-AGENTS-STAGE-MATERIALIZE`, permanent → bootstrap rollback
  per C-RT-02).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from harness_core.deployment_surface import DeploymentSurface

from harness_runtime.lifecycle.effect_fence import RuntimeEffectFence
from harness_runtime.lifecycle.managed_agents_dispatch import (
    ManagedAgentsStageMaterializeError,
    ManagedAgentsStepDispatcher,
)
from harness_runtime.types import RuntimeConfig

if TYPE_CHECKING:
    from harness_runtime.bootstrap.mutable_context import _MutableHarnessContext


async def materialize_managed_agents_dispatcher_stage(
    config: RuntimeConfig,
    ctx: _MutableHarnessContext,
) -> ManagedAgentsStepDispatcher | None:
    """Stage-5 LOOP_INIT factory for `ManagedAgentsStepDispatcher`.

    Per runtime spec v1.55 §14.20.3.

    Opt-out short-circuit: `config.managed_agents_config is None` → `None`.
    Surface gate: `config.deployment_surface != DeploymentSurface.MANAGED_CLOUD`
    → `None` (the local-development exclusion; opt-in honored only on
    managed-cloud).
    Opt-in construction: config present AND surface == MANAGED_CLOUD → construct
    the dispatcher over `config.managed_agents_config.client` + `ctx.tracer_provider`.

    Raises
    ------
    ManagedAgentsStageMaterializeError
        Opted-in on MANAGED_CLOUD but `ctx.tracer_provider` is None at stage-5
        entry, no client was supplied, or dispatcher construction fails.
    """
    managed_config = config.managed_agents_config
    if managed_config is None:
        return None
    if config.deployment_surface != DeploymentSurface.MANAGED_CLOUD:
        return None

    if ctx.tracer_provider is None:
        raise ManagedAgentsStageMaterializeError(
            "tracer_provider unbound at stage-5 entry — stage-4 OD-bucket "
            "landing must complete before materialize_managed_agents_dispatcher_stage"
        )
    if managed_config.client is None:
        raise ManagedAgentsStageMaterializeError(
            "managed_agents_config is opted-in on DeploymentSurface.MANAGED_CLOUD "
            "but no ManagedAgentsClientProtocol client was supplied "
            "(managed_agents_config.client is None)"
        )

    try:
        return ManagedAgentsStepDispatcher(
            client=managed_config.client,
            tracer_provider=ctx.tracer_provider,
            # B-FANOUT-CRASH-RESUME-MAYBE-RAN-UNFENCED-EXTERNAL (R-FS-1) — the SAME claim
            # dir as the tool dispatcher (`.harness/effect-fence`); the managed-agents key
            # namespace is disjoint (the `:managed_agents` domain tag), so sharing the dir
            # is collision-free. The per-run gate (`effect_fencing` opt-in OR a durable run
            # engine class) decides whether a reserve fires, keeping non-durable runs
            # fence-free (the §14.22.7 lazy-claim-dir footprint).
            effect_fence=RuntimeEffectFence(
                fence_dir=config.repository_root / ".harness" / "effect-fence"
            ),
            effect_fencing_explicit=config.effect_fencing,
        )
    except Exception as e:
        raise ManagedAgentsStageMaterializeError(
            f"ManagedAgentsStepDispatcher construction failed: {e!r}"
        ) from e
