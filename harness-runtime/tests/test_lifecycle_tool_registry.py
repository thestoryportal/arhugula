"""U-RT-14 — `ToolRegistry` + `materialize_tool_registry` tests.

ACs per Phase 2 Session 3 plan v2.1 §2 L3:
- Contracts discoverable by name.
- Discriminator dispatch wired (single name → single contract; duplicates rejected).
"""

from __future__ import annotations

import pytest
from harness_as.sandbox_tier import BlastRadiusTier, SandboxTier
from harness_as.tool_contract import ToolContract
from harness_runtime.lifecycle.tool_registry import (
    DuplicateToolNameError,
    ToolNameNotRegisteredError,
    ToolRegistry,
    materialize_tool_registry,
)
from harness_runtime.types import ToolName


def _contract(name: str = "test-tool") -> ToolContract:
    """Minimal ToolContract for registry tests."""
    return ToolContract(
        name=name,
        description="test",
        input_schema={},
        output_schema={},
        minimum_tier=SandboxTier.TIER_1_PROCESS,
        blast_radius_tier=BlastRadiusTier.LOCAL_MUTATION,
    )


# ---------------------------------------------------------------------------
# Empty registry at stage 2 AS bootstrap.
# ---------------------------------------------------------------------------


def test_materialize_returns_empty_registry() -> None:
    """At L3, registry is empty by default."""
    registry = materialize_tool_registry({})
    assert isinstance(registry, ToolRegistry)
    assert len(registry) == 0


def test_materialize_ignores_skills_input() -> None:
    """`skills` parameter is reserved; L3 hook is a no-op."""
    # An empty skills dict + a populated skills dict produce the same empty
    # registry at L3 (the skill→tool wiring is a future hook).
    assert len(materialize_tool_registry({})) == 0


# ---------------------------------------------------------------------------
# Contracts discoverable by name (plan AC).
# ---------------------------------------------------------------------------


def test_register_then_get_round_trip() -> None:
    """`register` + `get` round-trip recovers the contract."""
    registry = ToolRegistry()
    contract = _contract("my-tool")
    registry.register(contract)
    assert registry.get(ToolName("my-tool")) is contract


def test_registered_contract_in_membership() -> None:
    """`in` check works."""
    registry = ToolRegistry()
    registry.register(_contract("alpha"))
    assert ToolName("alpha") in registry
    assert ToolName("missing") not in registry


def test_names_iterates_registered_keys() -> None:
    """`names()` yields the registered tool names."""
    registry = ToolRegistry()
    for name in ("a", "b", "c"):
        registry.register(_contract(name))
    assert set(registry.names()) == {ToolName("a"), ToolName("b"), ToolName("c")}


# ---------------------------------------------------------------------------
# Discriminator dispatch — duplicates rejected (plan AC).
# ---------------------------------------------------------------------------


def test_duplicate_name_rejected() -> None:
    """Registering two contracts with the same name → `DuplicateToolNameError`."""
    registry = ToolRegistry()
    registry.register(_contract("dup"))
    with pytest.raises(DuplicateToolNameError) as exc_info:
        registry.register(_contract("dup"))
    assert exc_info.value.name == ToolName("dup")


# ---------------------------------------------------------------------------
# Lookup-missing surfaces typed error.
# ---------------------------------------------------------------------------


def test_get_missing_raises_typed_error() -> None:
    """`get` on a missing name raises `ToolNameNotRegisteredError`."""
    registry = ToolRegistry()
    with pytest.raises(ToolNameNotRegisteredError) as exc_info:
        registry.get(ToolName("never-registered"))
    assert exc_info.value.name == ToolName("never-registered")


# ---------------------------------------------------------------------------
# Len matches registration count.
# ---------------------------------------------------------------------------


def test_len_matches_registration_count() -> None:
    """`len(registry)` matches the number of registered contracts."""
    registry = ToolRegistry()
    assert len(registry) == 0
    registry.register(_contract("a"))
    assert len(registry) == 1
    registry.register(_contract("b"))
    assert len(registry) == 2
