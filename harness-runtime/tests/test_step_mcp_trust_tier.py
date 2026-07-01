"""R-FS-1 `B-TOOL-GATE` — per-step MCP-trust-tier resolver unit tests.

Covers `resolve_step_mcp_trust_tier` + `make_step_mcp_trust_tier_resolver`
(`harness_runtime.lifecycle.step_mcp_trust_tier`): the resolved-owning-host trust
feed for the tool-step HITL gate (CP spec v1.35 §19.1.2 Producer ¶ + runtime
§14.8.2 step-4c). Mirrors `test_step_blast_radius.py` (the sibling resolver).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from harness_core.identity import StepID
from harness_cp.cp_shared_types import MCPTrustTier
from harness_cp.workflow_driver_types import StepKind, WorkflowStep
from harness_runtime.lifecycle.step_mcp_trust_tier import (
    make_step_mcp_trust_tier_resolver,
    resolve_step_mcp_trust_tier,
)


class _RaiseRegistry:
    """Mimics the real `ToolRegistry`: `.get(name)` raises `KeyError` on a miss."""

    def __init__(self, tool_names: tuple[str, ...]) -> None:
        self._names = set(tool_names)

    def get(self, name: str) -> object:
        if name in self._names:
            return object()  # non-None ToolContract stand-in
        raise KeyError(name)  # mirrors ToolNameNotRegisteredError


class _DictRegistry:
    """Mimics a dict-shaped registry: `.get(name)` returns `None` on a miss."""

    def __init__(self, tool_names: tuple[str, ...]) -> None:
        self._names = set(tool_names)

    def get(self, name: str) -> object | None:
        return object() if name in self._names else None


class _FakeHost:
    def __init__(self, *, registry: object, trust_tier: MCPTrustTier) -> None:
        self.tool_registry = registry
        self.trust_tier = trust_tier


_MISSING = object()


def _tool_step(tool_id: object) -> WorkflowStep:
    payload: dict[str, Any] = {} if tool_id is _MISSING else {"tool_id": tool_id}
    return WorkflowStep(
        step_id=StepID("s-tool"), step_kind=StepKind.TOOL_STEP, step_payload=payload
    )


def _ctx_with_hosts() -> Any:
    return SimpleNamespace(
        mcp_client_hosts={
            "srv-untrusted": _FakeHost(
                registry=_RaiseRegistry(("danger_tool",)),
                trust_tier=MCPTrustTier.LEVEL_0_REFUSE_REMOTE,
            ),
            "srv-trusted": _FakeHost(
                registry=_DictRegistry(("safe_tool",)),
                trust_tier=MCPTrustTier.LEVEL_3_ALLOW_WITH_AUDIT,
            ),
        }
    )


def test_resolves_l0_owning_host_trust_tier() -> None:
    ctx = _ctx_with_hosts()
    assert (
        resolve_step_mcp_trust_tier(_tool_step("danger_tool"), ctx)
        is MCPTrustTier.LEVEL_0_REFUSE_REMOTE
    )


def test_resolves_l3_owning_host_trust_tier_via_dict_registry() -> None:
    ctx = _ctx_with_hosts()
    assert (
        resolve_step_mcp_trust_tier(_tool_step("safe_tool"), ctx)
        is MCPTrustTier.LEVEL_3_ALLOW_WITH_AUDIT
    )


def test_unresolvable_tool_id_returns_none() -> None:
    # Fail-soft: a tool in no host's registry → None → composer feeds the L3
    # no-floor default (the blast resolver is the fail-safe for this case).
    ctx = _ctx_with_hosts()
    assert resolve_step_mcp_trust_tier(_tool_step("unknown_tool"), ctx) is None


def test_missing_tool_id_returns_none() -> None:
    ctx = _ctx_with_hosts()
    assert resolve_step_mcp_trust_tier(_tool_step(_MISSING), ctx) is None


def test_non_str_tool_id_returns_none() -> None:
    ctx = _ctx_with_hosts()
    assert resolve_step_mcp_trust_tier(_tool_step(123), ctx) is None


def test_non_tool_step_returns_none() -> None:
    ctx = _ctx_with_hosts()
    step = WorkflowStep(step_id=StepID("s-inf"), step_kind=StepKind.INFERENCE_STEP, step_payload={})
    assert resolve_step_mcp_trust_tier(step, ctx) is None


def test_no_hosts_returns_none() -> None:
    ctx = cast(Any, SimpleNamespace(mcp_client_hosts={}))
    assert resolve_step_mcp_trust_tier(_tool_step("danger_tool"), ctx) is None


def test_make_resolver_closure_captures_ctx() -> None:
    ctx = _ctx_with_hosts()
    resolver = make_step_mcp_trust_tier_resolver(ctx)
    assert resolver(_tool_step("danger_tool")) is MCPTrustTier.LEVEL_0_REFUSE_REMOTE
    assert resolver(_tool_step("safe_tool")) is MCPTrustTier.LEVEL_3_ALLOW_WITH_AUDIT
    assert resolver(_tool_step("unknown_tool")) is None
