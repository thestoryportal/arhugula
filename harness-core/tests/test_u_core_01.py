"""Tests for U-CORE-01 — the cross-cutting `harness-core` shared-type set.

Test set per the U-CORE-01 `Tests:` field (Implementation_Plan_Harness_Core_v1_1.md
§2) — covers acceptance criteria #1-#6. The v1.0 test
`test_workflow_event_payload_matches_spec_5_2` is struck (carrier-thin Class 1
fork resolution — no `WorkflowEvent` payload model is declared; see plan §0.4).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import harness_core
import pytest
from harness_core.deployment_surface import DeploymentSurface
from harness_core.identity import (
    ActionID,
    ContractID,
    EntryID,
    ReferenceToUnit,
    StageID,
    StepID,
    ThreadID,
    UnitId,
    WorkflowID,
)
from harness_core.persona_tier import PersonaTier
from harness_core.workflow_event_class import WorkflowEventClass
from harness_core.workload_class import WorkloadClass

# Verbatim from the cited spec contracts.
_SPEC_DEPLOYMENT_SURFACES = {  # C-AS-09 §9.1 matrix deployment-surface axis
    "local-development",
    "self-hosted-server",
    "managed-cloud",
}
_SPEC_PERSONA_TIERS = {  # C-AS-09 §9.4 override-scope table
    "solo-developer",
    "team-binding",
    "multi-tenant-compliance",
}
_SPEC_WORKFLOW_EVENT_CLASSES = {  # C-CP-05 §5.1 event class table
    "workflow-start",
    "step-boundary",
    "fallback-trigger",
    "retry-attempt",
    "breaker-trip",
    "lease-acquired",
    "lease-released",
    "resumption",
}

_ALL_NINE_ALIASES = (
    ActionID,
    EntryID,
    WorkflowID,
    StepID,
    ThreadID,
    StageID,
    ContractID,
    UnitId,
    ReferenceToUnit,
)


# --- acceptance criterion #1 — DeploymentSurface -----------------------------


def test_deployment_surface_cardinality_three() -> None:
    """§9.1 — exactly 3 deployment surfaces."""
    assert len(DeploymentSurface) == 3


def test_deployment_surface_values_match_as_spec_9_1_verbatim() -> None:
    """§9.1 — member string values are the matrix row labels, verbatim."""
    assert {ds.value for ds in DeploymentSurface} == _SPEC_DEPLOYMENT_SURFACES


def test_deployment_surface_closed() -> None:
    """§9.1 — closed at cardinality 3: a members-bearing enum cannot be
    subclassed (no runtime extension)."""
    with pytest.raises(TypeError):

        class _Extended(DeploymentSurface):  # type: ignore[misc]
            HYBRID = "hybrid"


# --- acceptance criterion #2 — PersonaTier -----------------------------------


def test_persona_tier_cardinality_three() -> None:
    """§9.4 — exactly 3 persona tiers."""
    assert len(PersonaTier) == 3


def test_persona_tier_values_match_as_spec_9_4_verbatim() -> None:
    """§9.4 — member string values are the persona-tier ladder, verbatim."""
    assert {pt.value for pt in PersonaTier} == _SPEC_PERSONA_TIERS


def test_persona_tier_closed() -> None:
    """§9.4 — closed at cardinality 3: no runtime extension."""
    with pytest.raises(TypeError):

        class _Extended(PersonaTier):  # type: ignore[misc]
            ENTERPRISE = "enterprise"


# --- acceptance criterion #3 — identity aliases ------------------------------


def test_identity_aliases_all_nine_declared() -> None:
    """Exactly nine identity aliases, each a `str`-based `NewType`."""
    assert len(_ALL_NINE_ALIASES) == 9
    names = {alias.__name__ for alias in _ALL_NINE_ALIASES}
    assert names == {
        "ActionID",
        "EntryID",
        "WorkflowID",
        "StepID",
        "ThreadID",
        "StageID",
        "ContractID",
        "UnitId",
        "ReferenceToUnit",
    }
    for alias in _ALL_NINE_ALIASES:
        # `NewType.__supertype__` exists at runtime but is not in the typeshed
        # `NewType` stub — read it via `getattr` to keep `pyright` strict happy.
        assert getattr(alias, "__supertype__") is str  # noqa: B009


def test_identity_alias_nominal_distinct_under_pyright() -> None:
    """Each alias is a distinct nominal type — distinct `NewType` objects, no
    two interchangeable. `pyright` strict enforces non-assignability of a bare
    `str` (and of one alias to another) at the type layer; this runtime test
    asserts the nine carriers are distinct objects so the type-layer check has
    nine distinct nominal targets."""
    assert len({id(alias) for alias in _ALL_NINE_ALIASES}) == 9
    # `NewType` constructors are identity at runtime; nominal distinctness is a
    # type-layer property — verified by the `pyright` strict gate over this
    # package. The `TYPE_CHECKING` block below gives `pyright` an assertion.
    if TYPE_CHECKING:
        from typing import assert_type

        action = ActionID("a-1")
        assert_type(action, ActionID)
        entry = EntryID("e-1")
        assert_type(entry, EntryID)


# --- acceptance criterion #4 — WorkflowEventClass ----------------------------


def test_workflow_event_class_cardinality_eight() -> None:
    """§5.1 — exactly 8 lifecycle event classes."""
    assert len(WorkflowEventClass) == 8


def test_workflow_event_class_values_match_cp_spec_5_1_verbatim() -> None:
    """§5.1 — member string values are the event-class identifiers, verbatim."""
    assert {wec.value for wec in WorkflowEventClass} == _SPEC_WORKFLOW_EVENT_CLASSES


def test_workflow_event_class_closed() -> None:
    """§5.1 — closed at cardinality 8: no runtime extension."""
    with pytest.raises(TypeError):

        class _Extended(WorkflowEventClass):  # type: ignore[misc]
            CANCELLED = "cancelled"


# --- acceptance criterion #5 — residence + package API surface ---------------


def test_all_u_core_01_types_reside_in_harness_core() -> None:
    """Every U-CORE-01 type resides in the `harness-core` package."""
    assert DeploymentSurface.__module__ == "harness_core.deployment_surface"
    assert PersonaTier.__module__ == "harness_core.persona_tier"
    assert WorkflowEventClass.__module__ == "harness_core.workflow_event_class"
    for alias in _ALL_NINE_ALIASES:
        # `NewType` records the defining module on `__module__`.
        assert alias.__module__ == "harness_core.identity"


def test_harness_core_init_reexports_u_core_01_set() -> None:
    """Every U-CORE-01 type is exposed at the `harness-core` package public
    API surface so consuming axes import from one path."""
    expected = {
        "DeploymentSurface",
        "PersonaTier",
        "WorkflowEventClass",
        "ActionID",
        "EntryID",
        "WorkflowID",
        "StepID",
        "ThreadID",
        "StageID",
        "ContractID",
        "UnitId",
        "ReferenceToUnit",
    }
    assert expected <= set(harness_core.__all__)
    for name in expected:
        assert hasattr(harness_core, name)


# --- acceptance criterion #5 (cont.) — WorkloadClass unaffected --------------


def test_workload_class_unaffected_by_u_core_01() -> None:
    """`WorkloadClass` (U-CP-00, landed) is unaffected and remains beside the
    U-CORE-01 set in `harness-core`."""
    assert WorkloadClass.__module__ == "harness_core.workload_class"
    assert len(WorkloadClass) == 4
    assert harness_core.WorkloadClass is WorkloadClass
