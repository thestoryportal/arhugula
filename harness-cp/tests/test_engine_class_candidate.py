"""Tests for U-CP-16 — per-deployment-surface engine-class candidate mapping.

Acceptance-criterion coverage (C-CP-07 §7.2):
  #1 DeploymentSurface 3 values   -> test_deployment_surface_cardinality_three
  #2 3 entries per §7.2           -> test_engine_class_candidates_cardinality_three,
                                     test_candidate_sets_match_spec
  #2 local excludes reconciler    -> test_local_excludes_reconciler
  #2 server/cloud exclude pure    -> test_server_and_cloud_exclude_pure_pattern
  #3 exclusion inheritance        -> structural (observed at U-CP-40, not here)
  #4 specific candidates deferred -> structural (operator-discretion per §7.2)
"""

from __future__ import annotations

from harness_core import DeploymentSurface
from harness_cp.engine_class import EngineClass
from harness_cp.engine_class_candidate import (
    ENGINE_CLASS_CANDIDATES,
    EngineClassCandidate,
)


def _by_surface() -> dict[DeploymentSurface, EngineClassCandidate]:
    return {c.deployment_surface: c for c in ENGINE_CLASS_CANDIDATES}


def test_deployment_surface_cardinality_three() -> None:
    """#1 — `DeploymentSurface` declares exactly three values per §7.2."""
    assert len(DeploymentSurface) == 3
    assert {s.value for s in DeploymentSurface} == {
        "local-development",
        "self-hosted-server",
        "managed-cloud",
    }


def test_engine_class_candidates_cardinality_three() -> None:
    """#2 — `ENGINE_CLASS_CANDIDATES` declares exactly three entries."""
    assert len(ENGINE_CLASS_CANDIDATES) == 3
    assert {c.deployment_surface for c in ENGINE_CLASS_CANDIDATES} == set(DeploymentSurface)


def test_candidate_sets_match_spec() -> None:
    """#2 — candidate sets match the §7.2 / U-CP-16 acc#2 table."""
    by_surface = _by_surface()
    assert by_surface[DeploymentSurface.LOCAL_DEVELOPMENT].candidate_set == (
        frozenset(
            {
                EngineClass.EVENT_SOURCED_REPLAY,
                EngineClass.SAVE_POINT_CHECKPOINT,
                EngineClass.PURE_PATTERN_NO_ENGINE,
                EngineClass.WAL_SEGMENT,
            }
        )
    )
    expected_durable = frozenset(
        {
            EngineClass.EVENT_SOURCED_REPLAY,
            EngineClass.SAVE_POINT_CHECKPOINT,
            EngineClass.RECONCILER_LOOP,
            EngineClass.WAL_SEGMENT,
        }
    )
    assert by_surface[DeploymentSurface.SELF_HOSTED_SERVER].candidate_set == expected_durable
    assert by_surface[DeploymentSurface.MANAGED_CLOUD].candidate_set == expected_durable


def test_local_excludes_reconciler() -> None:
    """#2 — `local-development` structurally excludes `RECONCILER_LOOP`."""
    local = _by_surface()[DeploymentSurface.LOCAL_DEVELOPMENT]
    assert EngineClass.RECONCILER_LOOP not in local.candidate_set
    assert EngineClass.RECONCILER_LOOP in local.exclusion_reasons
    assert "K8s" in local.exclusion_reasons[EngineClass.RECONCILER_LOOP]


def test_server_and_cloud_exclude_pure_pattern() -> None:
    """#2 — server + cloud structurally exclude `PURE_PATTERN_NO_ENGINE`."""
    by_surface = _by_surface()
    for surface in (
        DeploymentSurface.SELF_HOSTED_SERVER,
        DeploymentSurface.MANAGED_CLOUD,
    ):
        entry = by_surface[surface]
        assert EngineClass.PURE_PATTERN_NO_ENGINE not in entry.candidate_set
        assert EngineClass.PURE_PATTERN_NO_ENGINE in entry.exclusion_reasons


def test_exclusion_reasons_complete() -> None:
    """Each excluded class carries an exclusion reason — no gap."""
    for entry in ENGINE_CLASS_CANDIDATES:
        excluded = set(EngineClass) - entry.candidate_set
        assert excluded == set(entry.exclusion_reasons)


def test_engine_class_candidate_frozen() -> None:
    """`EngineClassCandidate` is a frozen, extra-forbid record."""
    entry = ENGINE_CLASS_CANDIDATES[0]
    assert entry.model_config.get("frozen") is True
    assert entry.model_config.get("extra") == "forbid"
