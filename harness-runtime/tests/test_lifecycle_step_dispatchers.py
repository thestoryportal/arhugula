"""Tests for U-RT-59 — step-kind dispatcher registry (C-RT-17 §14.7.1 + §14.7.7).

Acceptance-criterion coverage (per Phase-2 Session-3 Track-A v2.5 L9-ter):
  AC #1 — StepKindDispatcherRegistry frozen + lookup + typed error
      → test_registry_is_frozen_post_construction
      → test_registry_lookup_returns_bound_dispatcher
      → test_registry_lookup_unbound_raises_step_kind_dispatcher_not_bound_error
      → test_registry_lookup_error_carries_step_kind
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from harness_cp.workflow_driver import StepDispatcher
from harness_cp.workflow_driver_types import StepExecutionContext, StepKind, WorkflowStep
from harness_runtime.lifecycle.step_dispatchers import (
    StepKindDispatcherNotBoundError,
    StepKindDispatcherRegistry,
)


class _StubDispatcher:
    """Minimal sync `StepDispatcher` Protocol impl."""

    def dispatch(
        self,
        binding: Any,
        step: WorkflowStep,
        *,
        step_context: StepExecutionContext,
    ) -> Mapping[str, Any]:
        _ = binding, step, step_context
        return {"stub": True}


def _stub() -> StepDispatcher:
    # Casts not needed — `_StubDispatcher` structurally conforms.
    return _StubDispatcher()  # type: ignore[return-value]


def test_registry_is_frozen_post_construction() -> None:
    """AC #1: StepKindDispatcherRegistry is frozen — mutation raises."""
    registry = StepKindDispatcherRegistry(dispatchers={StepKind.SUB_AGENT_DISPATCH: _stub()})
    with pytest.raises(Exception):
        registry.dispatchers = {}  # type: ignore[misc]


def test_registry_lookup_returns_bound_dispatcher() -> None:
    """AC #1: lookup of a bound kind returns the registered dispatcher."""
    dispatcher = _stub()
    registry = StepKindDispatcherRegistry(dispatchers={StepKind.SUB_AGENT_DISPATCH: dispatcher})
    assert registry.lookup(StepKind.SUB_AGENT_DISPATCH) is dispatcher


def test_registry_lookup_unbound_raises_step_kind_dispatcher_not_bound_error() -> None:
    """AC #1: lookup of an unbound kind raises StepKindDispatcherNotBoundError.

    Unit test of the registry primitive (independent of stage 5 wiring).
    Constructs a registry with only SUB_AGENT_DISPATCH bound; verifies the
    not-bound error path for the other 4 step kinds. Stage 5 at v1.7 binds
    INFERENCE_STEP + SUB_AGENT_DISPATCH (per the U-RT-59 async/sync fork
    Path B resolution); TOOL_STEP / HITL_STEP / DECLARATIVE_STEP remain
    unbound at the production registry pending their composer arcs.
    """
    registry = StepKindDispatcherRegistry(dispatchers={StepKind.SUB_AGENT_DISPATCH: _stub()})
    for unbound in (
        StepKind.INFERENCE_STEP,
        StepKind.TOOL_STEP,
        StepKind.HITL_STEP,
        StepKind.DECLARATIVE_STEP,
    ):
        with pytest.raises(StepKindDispatcherNotBoundError):
            registry.lookup(unbound)


def test_registry_lookup_error_carries_step_kind() -> None:
    """AC #1: typed error exposes the failing step_kind."""
    registry = StepKindDispatcherRegistry(dispatchers={StepKind.SUB_AGENT_DISPATCH: _stub()})
    with pytest.raises(StepKindDispatcherNotBoundError) as excinfo:
        registry.lookup(StepKind.INFERENCE_STEP)
    assert excinfo.value.step_kind == StepKind.INFERENCE_STEP
