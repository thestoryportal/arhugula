"""U-RT-03 — `BootstrapStage` enum + `StageResult` protocol tests.

ACs per Phase 2 Session 3 plan v2.1 §2 L0:
- enum has exactly 9 members in the order PREAMBLE / IS / AS / CP_CLIENTS /
  CP_ROUTING / OD / LOOP_INIT / CXA_WIRING / INGRESS_ACCEPT;
- `len(BootstrapStage) == 9`;
- result protocol consumed by stage modules (verified by type-shape test +
  pyright-strict at module load).
"""

from __future__ import annotations

from harness_runtime.types import (
    BootstrapStage,
    StageLifecycleHook,
    StageResult,
)


def test_bootstrap_stage_cardinality_is_nine() -> None:
    """`len(BootstrapStage) == 9` per C-RT-01."""
    assert len(BootstrapStage) == 9


def test_bootstrap_stage_order_is_canonical() -> None:
    """The 9 stages traverse in C-RT-01 fixed order."""
    expected = [
        BootstrapStage.PREAMBLE,
        BootstrapStage.IS,
        BootstrapStage.AS,
        BootstrapStage.CP_CLIENTS,
        BootstrapStage.CP_ROUTING,
        BootstrapStage.OD,
        BootstrapStage.LOOP_INIT,
        BootstrapStage.CXA_WIRING,
        BootstrapStage.INGRESS_ACCEPT,
    ]
    assert list(BootstrapStage) == expected


def test_bootstrap_stage_names_match_spec() -> None:
    """Member names match `Spec_Harness_Runtime_v1.md` v1.1 §1 byte-exact."""
    names = [s.name for s in BootstrapStage]
    assert names == [
        "PREAMBLE",
        "IS",
        "AS",
        "CP_CLIENTS",
        "CP_ROUTING",
        "OD",
        "LOOP_INIT",
        "CXA_WIRING",
        "INGRESS_ACCEPT",
    ]


def test_bootstrap_stage_terminal_is_ingress_accept() -> None:
    """`INGRESS_ACCEPT` is the terminal stage; no `stage_8` per C-RT-01."""
    assert list(BootstrapStage)[-1] is BootstrapStage.INGRESS_ACCEPT


def test_stage_result_carries_stage() -> None:
    """`StageResult` instances name the stage that produced them."""
    result = StageResult(stage=BootstrapStage.PREAMBLE)
    assert result.stage is BootstrapStage.PREAMBLE


def test_stage_result_is_frozen() -> None:
    """`StageResult` is frozen per C-RT-02 implementation-discretion guidance."""
    assert StageResult.model_config.get("frozen") is True


def test_stage_result_rejects_extra_fields() -> None:
    """`StageResult` rejects unknown fields (`extra='forbid'`)."""
    try:
        StageResult.model_validate(
            {"stage": BootstrapStage.IS, "unknown": "should-fail"},
        )
    except Exception:
        pass
    else:
        raise AssertionError("StageResult accepted unknown field")


def test_stage_result_round_trips() -> None:
    """`StageResult` survives `model_dump()` → `model_validate()` byte-equal."""
    original = StageResult(stage=BootstrapStage.OD)
    rebuilt = StageResult.model_validate(original.model_dump())
    assert rebuilt == original


def test_stage_lifecycle_hook_is_protocol() -> None:
    """`StageLifecycleHook` is a structural Protocol; concretized at U-RT-41."""

    # Any object structurally satisfies the empty Protocol at L0.
    class _Stub:
        pass

    assert isinstance(_Stub(), StageLifecycleHook)
