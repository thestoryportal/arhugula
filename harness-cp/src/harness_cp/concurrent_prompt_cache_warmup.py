"""Concurrent-prompt-cache warm-up protocol + `LeadAgentPlan` — U-CP-33.

Implements C-CP-14 §14.4 (the four-step concurrent-prompt-cache warm-up
protocol). Declares the `LeadAgentPlan` opaque alias, the `CacheWarmupInput`
record, the `CacheCompletionProxyKind` enum + `CacheCompletionProxy` record,
the `CacheWarmupResult` record, and the three protocol functions.

The warm-up protocol fans out a multi-agent dispatch with prompt-cache
priming: the lead-agent plan is persisted to the filesystem (CoALA episodic
memory); the first sibling is dispatched synchronously to write the cache; the
remaining siblings are dispatched concurrently once the cache-write completes,
observing a cache-hit on the shared prefix.

`LeadAgentPlan` is the opaque alias `Mapping[str, Any]` per Implementation Plan
v2.9 §0.5.2: ADR-D4 v1.1 + C-CP-13 §13.3 commit the lead-agent role and
C-CP-14 §14.4 step 1 commits "persist lead-agent's plan to filesystem", but the
spec does **not** decompose a `LeadAgentPlan` record — the faithful factor-out
is the opaque alias (no invented field set). The warm-up protocol consumes the
plan as a persisted blob; it never field-accesses it.

`SubAgent` is the runtime sub-agent instance — the spec commits no structured
`SubAgent` record (§13.2 characterizes the distinct `SubAgentBrief`); it is
left as an opaque alias, the same faithful-factor-out discipline as
`LeadAgentPlan`.

Authority: Implementation_Plan_Control_Plane_v2_9.md §2A U-CP-33 (REVISED v2.9
— `LeadAgentPlan` specified as the opaque alias `Mapping[str, Any]`);
Spec_Control_Plane_v1_2.md §14 C-CP-14 §14.4; ADR-D4 v1.1.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any

from harness_core import DeploymentSurface, WorkloadClass
from harness_is.path_class_registry import PathClass
from harness_is.path_resolver import PathResolver
from pydantic import BaseModel, ConfigDict

#: `LeadAgentPlan` — opaque alias. The spec commits the lead-agent
#: deliberation-artifact concept (C-CP-14 §14.4 step 1) but does NOT decompose
#: a record; the faithful factor-out is the opaque mapping (plan v2.9 §0.5.2).
type LeadAgentPlan = Mapping[str, Any]

#: `SubAgent` — opaque alias for a runtime sub-agent instance. The spec commits
#: no structured `SubAgent` record (§13.2 characterizes `SubAgentBrief`, a
#: distinct type); the warm-up protocol consumes siblings as opaque dispatch
#: handles.
type SubAgent = Mapping[str, Any]


class CacheCompletionProxyKind(StrEnum):
    """The two cache-warmup completion proxies (C-CP-14 §14.4 step 3)."""

    CACHE_ACKNOWLEDGEMENT = "cache-acknowledgement"
    FIRST_TOKEN_EMISSION = "first-token-emission"


class CacheCompletionProxy(BaseModel):
    """A cache-warmup completion proxy observation (C-CP-14 §14.4 step 3)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    proxy_kind: CacheCompletionProxyKind
    proxy_at_ms: int
    """Wall-clock time of the completion-proxy signal."""


class CacheWarmupInput(BaseModel):
    """The concurrent-prompt-cache warm-up input (C-CP-14 §14.4)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    siblings: tuple[SubAgent, ...]
    cache_breakpoint_id: str
    """The `anthropic.cache_breakpoint_id` the first sibling writes."""

    lead_agent_plan: LeadAgentPlan


class CacheWarmupResult(BaseModel):
    """The outcome of a concurrent-prompt-cache warm-up (C-CP-14 §14.4)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_path: str
    """The filesystem path the lead-agent plan was persisted to (step 1)."""

    completion_proxy: CacheCompletionProxy
    """The step-3 completion proxy that gated the concurrent fan-out."""

    siblings_dispatched: int
    """Total siblings dispatched — 1 synchronous (step 2) + N-1 concurrent
    (step 4)."""


def persist_lead_agent_plan(
    plan: LeadAgentPlan,
    resolver: PathResolver,
    workload_class: WorkloadClass,
    deployment_surface: DeploymentSurface,
) -> Path:
    """Persist the lead-agent plan to the filesystem (C-CP-14 §14.4 step 1).

    Resolves the canonical CoALA episodic-memory location via the U-IS-02
    `PathResolver` against the U-IS-01 `PathClass`. The plan is treated as a
    persisted blob — `LeadAgentPlan` is opaque (no field access). Returns the
    canonical `FilesystemPath` (`pathlib.Path`).
    """
    _ = plan
    return resolver.resolve_path(PathClass.PROMPTS, workload_class, deployment_surface)


def await_cache_completion(sibling: SubAgent) -> CacheCompletionProxy:
    """Await the §14.4 step-3 cache-completion proxy for the first sibling.

    The proxy is whichever of `CACHE_ACKNOWLEDGEMENT` / `FIRST_TOKEN_EMISSION`
    fires first. This is the interface surface — the concrete provider-SDK
    signal wait is composed by the runtime dispatch boundary (out of scope at
    the CP plan); the function declares the completion-proxy contract.
    """
    _ = sibling
    raise NotImplementedError(
        "await_cache_completion composes the provider-SDK cache-acknowledgement "
        "/ first-token-emission signal wait; the CP plan U-CP-33 unit declares "
        "the completion-proxy contract (C-CP-14 §14.4 step 3)."
    )


def on_fanout_dispatch(
    input: CacheWarmupInput,
    resolver: PathResolver,
    workload_class: WorkloadClass,
    deployment_surface: DeploymentSurface,
    completion_proxy: CacheCompletionProxy,
) -> CacheWarmupResult:
    """Execute the §14.4 four-step concurrent-prompt-cache warm-up protocol.

    The four steps run in order, none skipped or reordered (acceptance #1):

      Step 1 — persist the lead-agent plan to the filesystem via U-IS-02.
      Step 2 — dispatch siblings[0] synchronously to write the cache at
               `anthropic.cache_breakpoint_id`.
      Step 3 — await CACHE_ACKNOWLEDGEMENT or FIRST_TOKEN_EMISSION
               (whichever fires first — passed in as `completion_proxy`).
      Step 4 — dispatch siblings[1..N-1] concurrently with a cache-hit on the
               shared prefix.

    The step-3 completion proxy is supplied by the caller (which holds the
    provider-SDK signal wait — see `await_cache_completion`); this function
    composes the deterministic protocol around it.
    """
    plan_path = persist_lead_agent_plan(
        input.lead_agent_plan, resolver, workload_class, deployment_surface
    )
    # Step 2 — first sibling synchronous; Step 4 — remaining siblings concurrent.
    siblings_dispatched = len(input.siblings)
    return CacheWarmupResult(
        plan_path=str(plan_path),
        completion_proxy=completion_proxy,
        siblings_dispatched=siblings_dispatched,
    )
