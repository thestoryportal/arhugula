"""Stage 3b CP_ROUTING — routing manifest, engine selector, fallback, retry/breaker, HITL, handoff.

Per `Spec_Harness_Runtime_v1.md` v1.1 §2 stage 3b post-conditions:
`ctx.routing_manifest`, `ctx.engine_selector`, `ctx.fallback_chain`,
`ctx.retry_breaker`, `ctx.hitl_registry`, `ctx.handoff_registry` all non-None.
Authority: C-RT-02 stage-3b invariants, U-RT-21, U-RT-22, U-RT-23,
U-RT-24, U-RT-25, and U-RT-26.

Composer order within the stage is free (no intra-stage dependencies among
the 6 composers); the orchestrator calls them in alphabetical-by-field order
for determinism.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from harness_core.workload_class import WorkloadClass
from harness_cp.cp_shared_types import ActorIdentity

from harness_runtime.bootstrap.mutable_context import _MutableHarnessContext
from harness_runtime.lifecycle.cp_is_wiring import (
    RuntimeCpIsWiring,
    materialize_cp_is_wiring_stage,
)
from harness_runtime.lifecycle.engine_selector import materialize_engine_selector
from harness_runtime.lifecycle.fallback_chain import materialize_fallback_chain_stage
from harness_runtime.lifecycle.handoff import materialize_handoff_stage
from harness_runtime.lifecycle.hitl_placement import materialize_hitl_placement_stage
from harness_runtime.lifecycle.procedural_tier_snapshot import (
    make_procedural_tier_snapshot_resolver,
)
from harness_runtime.lifecycle.retry_breaker import materialize_retry_breaker_stage
from harness_runtime.lifecycle.routing_manifest import materialize_routing_manifest_stage
from harness_runtime.types import RuntimeConfig

if TYPE_CHECKING:
    from harness_runtime.lifecycle.engine_selector import RuntimeEngineSelector
    from harness_runtime.types import HarnessContext

__all__ = ["execute"]

_ENGINE_SELECTOR_WORKFLOW_ID = "bootstrap-engine-selector"


async def execute(
    ctx: _MutableHarnessContext,
    config: RuntimeConfig,
    workload_class: WorkloadClass,
) -> None:
    """Populate stage 3b CP_ROUTING fields on `ctx`."""
    assert ctx.path_resolver is not None, "stage 1 IS must precede stage 3b"
    assert ctx.ledger_writer is not None, "stage 1 IS must precede stage 3b"
    assert ctx.skills is not None, "stage 2 AS must precede stage 3b"

    # 1. Routing manifest (depends on path resolver for residence path).
    routing = materialize_routing_manifest_stage(
        config,
        ctx.path_resolver,
        workload_class,
    )
    ctx.routing_manifest = routing.manifest

    # 2. CP → IS wiring needed by binding-time CP selections. The resolver
    # only needs stage-2 skills + the routing manifest just built above.
    _materialize_cp_is_wiring_for_routing(ctx, config)

    # 3. Engine selector.
    selector = materialize_engine_selector(config)
    ctx.engine_selector = selector
    await _emit_engine_selection_state_ledger_entries(ctx, selector)

    # 4. Cross-family fallback chain.
    fallback = materialize_fallback_chain_stage(config)
    ctx.fallback_chain = fallback.chain

    # 5. Retry/breaker registry.
    # The concrete registries narrow the Protocol's deliberately `object`-typed
    # method params (documented decoupling at types.py to avoid import cycles);
    # assigning concrete → Protocol-typed field is a benign contravariance.
    retry = materialize_retry_breaker_stage(config)
    ctx.retry_breaker = retry.registry  # pyright: ignore[reportAttributeAccessIssue]

    # 6. HITL placement registry.
    hitl = materialize_hitl_placement_stage(config)
    ctx.hitl_registry = hitl.registry  # pyright: ignore[reportAttributeAccessIssue]

    # 7. Sub-agent handoff registry.
    handoff = materialize_handoff_stage(config)
    ctx.handoff_registry = handoff.registry


def _materialize_cp_is_wiring_for_routing(
    ctx: _MutableHarnessContext,
    config: RuntimeConfig,
) -> None:
    """Bind CP → IS wiring as soon as stage 3b has its dependencies."""
    assert ctx.ledger_writer is not None
    assert ctx.routing_manifest is not None
    assert ctx.skills is not None

    if ctx.procedural_tier_snapshot_resolver is None:
        ctx.procedural_tier_snapshot_resolver = make_procedural_tier_snapshot_resolver(
            cast("HarnessContext", ctx),
        )
    if "cp_is_wiring" not in ctx.cxa_stages:
        ctx.cxa_stages["cp_is_wiring"] = materialize_cp_is_wiring_stage(
            config,
            ctx.ledger_writer,
            ctx.procedural_tier_snapshot_resolver,
        )


async def _emit_engine_selection_state_ledger_entries(
    ctx: _MutableHarnessContext,
    selector: RuntimeEngineSelector,
) -> None:
    """Persist binding-time engine-selection decisions at their production site."""
    assert ctx.ledger_writer is not None
    wiring = cast(RuntimeCpIsWiring, ctx.cxa_stages["cp_is_wiring"].wiring)
    actor = ActorIdentity(ctx.ledger_writer.actor.actor_id)

    for (workload_class, persona_tier), result in selector.selection_results.items():
        await wiring.emit_workload_class_selection_state_ledger_entry(
            workflow_id=_ENGINE_SELECTOR_WORKFLOW_ID,
            step_id=f"{workload_class.value}-{persona_tier.value}",
            selection_result=result,
            actor=actor,
        )
