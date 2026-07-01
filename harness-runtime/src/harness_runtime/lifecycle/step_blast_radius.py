"""Per-step blast-radius resolver — U-RT-115 (G1-blast).

Per `.harness/r-fs-1-b3-smart-hitl-design-v1.md` §3.2 (design decision D-cond.1)
+ runtime plan v2.44 §2.3 U-RT-115. The HITL gate's `gate_level()` `max()` needs
a per-step `blast_radius_tier` (C-CP-19 §19.1 `blast_radius_floor` axis), but
**no per-step carrier exists** at HEAD (`WorkflowStep` / `StepEffectiveBinding`
carry none — confirmed by direct grep). The blast radius of a step is therefore
**not a lookup — it is resolved per step-kind**, semantically well-determined by
the existing AS/CP contracts (design §3.2 table):

| Step kind            | blast_radius source                                          |
|----------------------|--------------------------------------------------------------|
| INFERENCE_STEP       | `READ_ONLY` — a provider chat-completion has no external      |
|                      |   side effect (side effects come from downstream TOOL/        |
|                      |   SUB_AGENT steps, each independently gated).                 |
| TOOL_STEP            | the tool's `ToolContract.blast_radius_tier` (AS C-AS-03),     |
|                      |   looked up by `tool_id` from the step payload.              |
| SUB_AGENT_DISPATCH   | the child ceiling — `compute_child_blast_radius_ceiling`      |
|                      |   (C-CP-12 §12.1; parent-independent → `READ_ONLY` default-   |
|                      |   downgrade ceiling).                                         |
| DECLARATIVE_STEP /   | `READ_ONLY` — no external effect.                            |
|   HITL_STEP          |                                                              |

**Impl-against-cleared-spec.** The spec's deferred-list explicitly leaves the
specific `blast_radius_floor(tool)` lookup to implementation (Action-Surface
territory) and the per-kind semantics are determined by the existing AS contracts
— **no new contract surface is minted** (design §3.2 D-cond.1).

**Fail-safe (AC).** An unresolvable TOOL_STEP (missing/non-str `tool_id`, or a
`tool_id` the registry cannot resolve) **raises** `StepBlastRadiusResolutionError`
— it does NOT silently default to `READ_ONLY` (which would auto-approve an
unclassifiable action — a C10 blast-radius safety violation).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from harness_as import BlastRadiusTier
from harness_cp.default_downgrade_rule import (
    DEFAULT_DOWNGRADE_RULE,
    compute_child_blast_radius_ceiling,
)
from harness_cp.workflow_driver_types import StepKind, WorkflowStep

if TYPE_CHECKING:  # pragma: no cover — type-only import (avoid runtime cycle)
    from harness_runtime.types import HarnessContext, ServerName

__all__ = [
    "StepBlastRadiusResolutionError",
    "make_step_blast_radius_resolver",
    "resolve_step_blast_radius",
]


class StepBlastRadiusResolutionError(LookupError):
    """A TOOL_STEP's blast radius cannot be resolved (fail-safe, per AC).

    Raised when `step.step_payload["tool_id"]` is missing / non-str / empty, or
    is not registered in `ctx.mcp_client_hosts[*].tool_registry` / `ctx.tool_contracts`.
    The resolver does NOT silently default to `READ_ONLY` — an unclassifiable
    action must gate, never auto-approve (C10 blast-radius safety; design §3.2).
    """


_READ_ONLY_KINDS = frozenset(
    {
        StepKind.INFERENCE_STEP,
        StepKind.DECLARATIVE_STEP,
        StepKind.HITL_STEP,
        # POST_JOIN_SYNTHESIS (CP spec v1.54 / runtime §14.24) — a read-only /
        # effect-free LLM compose of the fan-out siblings (no external effect; the
        # arc's load-bearing safety property). A PRE_ACTION-gated synthesis must
        # classify here, else the HITL composer's resolve_step_blast_radius would
        # raise StepBlastRadiusResolutionError → gated synthesis fails (Codex [P2]).
        StepKind.POST_JOIN_SYNTHESIS,
    }
)


def _lookup_tool_blast_radius(ctx: HarnessContext, tool_id: str) -> BlastRadiusTier:
    """Resolve `ToolContract.blast_radius_tier` for `tool_id` from `ctx`.

    Tries the runtime authority (the MCP host's `tool_registry`, the source
    of truth the `RuntimeToolDispatcher` consumes at TOOL_STEP dispatch) first,
    then the stage-2 registered `ctx.tool_contracts`. Raises if neither resolves.

    The real `ToolRegistry.get(name)` **raises** `ToolNameNotRegisteredError`
    (a `KeyError` subclass) on a miss — it does NOT return `None` (the
    `RuntimeToolDispatcher` wraps it in try/except for exactly this reason). So a
    registry miss is caught and falls through to the `ctx.tool_contracts` fallback
    + the fail-safe raise below — NOT leaked as a `KeyError` (the documented error
    taxonomy). `dict`-shaped registries / `tool_contracts` return `None` on miss
    (no raise); both miss-shapes are handled.
    """
    # U-RT-128: search ALL hosts' registries for `tool_id`. Blast-radius
    # resolution takes `ctx`, not the dispatcher, so the dispatcher-held routing
    # index is not reachable here — but the RT-FAIL-MCP-TOOL-NAME-COLLISION
    # bootstrap guarantee (a tool advertised by ≥2 hosts aborts startup) makes
    # the tool resolve in AT MOST one host, so the first match is unambiguous.
    # Still one-source-of-truth: the per-host registries are the authority.
    hosts: dict[ServerName, Any] = getattr(ctx, "mcp_client_hosts", None) or {}
    for host in hosts.values():
        registry = getattr(host, "tool_registry", None)
        if registry is None:
            continue
        try:
            contract = registry.get(tool_id)
        except KeyError:
            # Real ToolRegistry raises ToolNameNotRegisteredError (KeyError) on a
            # miss — try the next host, then fall through to tool_contracts.
            contract = None
        if contract is not None:
            return contract.blast_radius_tier
    contracts = getattr(ctx, "tool_contracts", None)
    if contracts is not None:
        contract = contracts.get(tool_id)
        if contract is not None:
            return contract.blast_radius_tier
    raise StepBlastRadiusResolutionError(
        f"TOOL_STEP tool_id={tool_id!r} not registered in "
        f"ctx.mcp_client_hosts[*].tool_registry or ctx.tool_contracts — cannot "
        f"resolve blast radius (fail-safe: an unclassifiable action gates, "
        f"never auto-approves)"
    )


def resolve_step_blast_radius(step: WorkflowStep, ctx: HarnessContext) -> BlastRadiusTier:
    """Resolve a step's blast radius per its step-kind (design §3.2 table).

    Pure function (no IO beyond the registry read). See the module docstring for
    the per-kind table + the fail-safe discipline.
    """
    kind = step.step_kind
    if kind in _READ_ONLY_KINDS:
        return BlastRadiusTier.READ_ONLY
    if kind is StepKind.SUB_AGENT_DISPATCH:
        # A sub-agent's blast radius (from the dispatching parent's gate view) is
        # bounded by the C-CP-12 §12.1 default-downgrade child ceiling. The
        # ceiling is parent-independent (always READ_ONLY per the U-CP-26
        # characterization) — the downstream sub-agent actions are each
        # independently gated, so dispatching the sub-agent is read-only at the
        # parent gate. Call the rule (not a hardcoded constant) so the resolver
        # tracks the §12.1 rule.
        return compute_child_blast_radius_ceiling(DEFAULT_DOWNGRADE_RULE.parent_blast_radius)
    if kind is StepKind.TOOL_STEP:
        tool_id = step.step_payload.get("tool_id")
        if not isinstance(tool_id, str) or not tool_id:
            raise StepBlastRadiusResolutionError(
                f"TOOL_STEP step_id={step.step_id!r} payload missing or non-str "
                f"'tool_id' — cannot resolve blast radius (fail-safe)"
            )
        return _lookup_tool_blast_radius(ctx, tool_id)
    raise StepBlastRadiusResolutionError(  # pragma: no cover — StepKind is exhaustive above
        f"unknown step_kind={kind!r} — cannot resolve blast radius"
    )


def make_step_blast_radius_resolver(
    ctx: HarnessContext,
) -> Callable[[WorkflowStep], BlastRadiusTier]:
    """Build a `(step) -> BlastRadiusTier` resolver closure capturing `ctx`.

    Mirrors the `make_procedural_tier_snapshot_resolver(ctx)` stage-5 closure
    precedent (`procedural_tier_snapshot.py`): the HITL gate composer holds this
    closure as instance state (no `ctx` field on the composer) and invokes it
    per step at the §14.8.2 step-4c gate-input composition. Bound at bootstrap
    stage-5 LOOP_INIT, where `ctx` is available.
    """

    def _resolver(step: WorkflowStep) -> BlastRadiusTier:
        return resolve_step_blast_radius(step, ctx)

    return _resolver
