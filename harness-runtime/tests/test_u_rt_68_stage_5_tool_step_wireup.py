"""U-RT-68 — Stage 5 TOOL_STEP wire-up via materialize_runtime_tool_dispatcher_stage.

REWRITTEN at v2.12 per the U-RT-68 Class 1 fork Q1=B + Q1a=(i) + Q2=B2
ratification 2026-05-22. ACs per Implementation_Plan_Harness_Runtime_v2_13.md
§1B U-RT-68 (preserved from v2.12). Spec contract: Spec_Harness_Runtime_v1.md
v1.16 §14.9.3 stage-5 prose + §14.11 C-RT-21.

End-to-end bootstrap-driven AC coverage (#1/#2/#3/#5) is provided by the
extended test_bootstrap.py::test_bootstrap_stage_5_binds_inference_and_sub_agent_dispatchers
fixture which now asserts the TOOL_STEP binding. This file carries the
unit-level invariant checks (#4 — no new conditional in workflow_driver.py)
and source-grep proofs that the stage-5 body imports + invokes the factory
exactly once.
"""

from __future__ import annotations

from pathlib import Path


def test_workflow_driver_branching_via_typed_table_no_new_conditional() -> None:
    """AC #4 — workflow_driver.py per-step dispatch resolves TOOL_STEP via
    the typed step-dispatcher table without a new conditional in the driver
    body. The Path A discipline (single dispatch table; no per-StepKind
    branches) is preserved at the workflow driver."""
    from harness_cp import workflow_driver

    assert workflow_driver.__file__ is not None
    driver_src = Path(workflow_driver.__file__).read_text(encoding="utf-8")
    assert "step.step_kind == StepKind.TOOL_STEP" not in driver_src
    assert "step_kind == 'TOOL_STEP'" not in driver_src
    assert 'step_kind == "TOOL_STEP"' not in driver_src


def test_stage_5_imports_runtime_tool_dispatcher_factory() -> None:
    """AC #1 — stage 5 body imports the U-RT-75 factory + invokes it. Per
    the rewrite-provenance docstring: U-RT-68 becomes the thin "stage-5
    callsite invocation" unit consuming the new factory."""
    from harness_runtime.bootstrap import stage_5_loop_init

    assert stage_5_loop_init.__file__ is not None
    stage_src = Path(stage_5_loop_init.__file__).read_text(encoding="utf-8")
    assert (
        "from harness_runtime.bootstrap.factories.runtime_tool_dispatcher_factory import"
        in stage_src
    )
    assert "materialize_runtime_tool_dispatcher_stage" in stage_src
    # Single invocation site — AC #1 "exactly once".
    invocations = stage_src.count("materialize_runtime_tool_dispatcher_stage(")
    assert invocations == 1, (
        f"expected exactly 1 invocation of "
        f"`materialize_runtime_tool_dispatcher_stage(...)` in stage_5_loop_init.py; "
        f"found {invocations}"
    )


def test_stage_5_binds_tool_dispatcher_to_ctx() -> None:
    """AC #2 — wrapper binds to ctx.tool_dispatcher (not to bare or
    intermediate). Verified via source presence."""
    from harness_runtime.bootstrap import stage_5_loop_init

    assert stage_5_loop_init.__file__ is not None
    stage_src = Path(stage_5_loop_init.__file__).read_text(encoding="utf-8")
    assert "ctx.tool_dispatcher = await materialize_runtime_tool_dispatcher_stage(" in stage_src


def test_stage_5_extends_step_dispatcher_table_with_tool_step() -> None:
    """AC #3 — step-dispatcher table extended with TOOL_STEP →
    tool_step_dispatcher facade. Existing INFERENCE_STEP +
    SUB_AGENT_DISPATCH bindings preserved verbatim (source-presence)."""
    from harness_runtime.bootstrap import stage_5_loop_init

    assert stage_5_loop_init.__file__ is not None
    stage_src = Path(stage_5_loop_init.__file__).read_text(encoding="utf-8")
    # TOOL_STEP is bound unconditionally; INFERENCE_STEP / SUB_AGENT_DISPATCH
    # are bound conditionally on `requires_inference` (runtime spec v1.47 §2.1 —
    # a non-inference / tool-only workflow omits these rows).
    assert "StepKind.TOOL_STEP: tool_step_dispatcher" in stage_src
    assert "dispatchers[StepKind.INFERENCE_STEP] = inference_step_dispatcher" in stage_src
    assert "dispatchers[StepKind.SUB_AGENT_DISPATCH] = sub_agent_step_dispatcher" in stage_src


def test_end_to_end_ac5_covered_by_extended_bootstrap_fixture() -> None:
    """AC #5 — end-to-end test for INFERENCE_STEP + TOOL_STEP dispatch via
    their respective wrappers (C-RT-16 + C-RT-21) is exercised by the
    extended test_bootstrap.py::test_bootstrap_stage_5_binds_inference_and_sub_agent_dispatchers
    fixture, which (at U-RT-68 cluster close) now asserts the TOOL_STEP
    binding alongside the existing INFERENCE_STEP + SUB_AGENT_DISPATCH
    assertions.

    Marker test only — the actual assertion lives in the extended fixture.
    Verified by source presence."""
    test_src = (Path(__file__).parent / "test_bootstrap.py").read_text(encoding="utf-8")
    assert "TOOL_STEP bound at U-RT-68 cluster-close" in test_src
    assert "ctx.step_dispatchers.lookup(StepKind.TOOL_STEP)" in test_src
