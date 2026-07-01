"""Tests for U-IS-01 — path-class registry schema (C-IS-01 §1).

Test set per the U-IS-01 `Tests:` field. Acceptance-criterion coverage:
  #1 enum cardinality + verbatim values -> test_path_class_registry_completeness
  #2 each PathClass registered            -> test_path_class_registry_completeness
  #3 in_memory_only == false              -> test_no_in_memory_only_artifacts
  #4 both observer flags true             -> test_visibility_surface_both_observers
  #5 statically validatable               -> Pydantic v2 + pyright strict
"""

from __future__ import annotations

from harness_is.path_class_registry import PATH_CLASS_REGISTRY, PathClass

# The 4 canonical path-class values, verbatim from Spec_Information_Substrate
# _v1.md §1 C-IS-01.
_SPEC_PATH_CLASSES = {"SKILLS", "PROMPTS", "ROUTING_MANIFEST", "STATE_LEDGER"}


def test_path_class_registry_completeness() -> None:
    """Enum cardinality == 4, values match spec §1 verbatim, all registered."""
    assert len(PathClass) == 4
    assert {pc.value for pc in PathClass} == _SPEC_PATH_CLASSES
    # Acceptance #2 — each PathClass value has a registered metadata instance.
    assert set(PATH_CLASS_REGISTRY.keys()) == set(PathClass)


def test_no_in_memory_only_artifacts() -> None:
    """Acceptance #3 — visibility_surface.in_memory_only is false everywhere."""
    for metadata in PATH_CLASS_REGISTRY.values():
        assert metadata.visibility_surface.in_memory_only is False


def test_visibility_surface_both_observers() -> None:
    """Acceptance #4 — both observer flags true for every registered class."""
    for metadata in PATH_CLASS_REGISTRY.values():
        assert metadata.visibility_surface.operator_readable_during_run is True
        assert metadata.visibility_surface.maintainer_readable_post_run is True
