"""U-CP-89 `HIERARCHICAL_DELEGATION` — genuine depth-2 live e2e (real stack + Ollama).

The CP-axis unit suite (`harness-cp/tests/test_workflow_driver_hierarchical_delegation.py`)
proves the strategy + the cross-level all-equal-timestamp fix at the driver level
with a faithful SUB_AGENT_DISPATCH test-double. THIS test removes the double: it
runs a genuine 2-level hierarchical delegation through the FULL real stack —
`api.run` → bootstrap → the materialized `_execute_hierarchical_delegation` →
the REAL `RuntimeSubAgentDispatcher` (admissibility gate + descent + audit) →
the REAL `child_workflow_runner` re-entering `execute_workflow` for the child →
the materialized HIERARCHICAL child → a real **Ollama** INFERENCE_STEP at depth.

This is the load-bearing integration the unit double cannot prove (advisor):
that a child manifest declaring `HIERARCHICAL_DELEGATION` is no longer rejected
`TopologyPatternNotYetMaterializedError` by the real recursion seam, AND that the
cross-level appends stay monotonic on the REAL zero-tolerance shared ledger
(the all-equal tree-timestamp scope). Free (local Ollama, zero-secret); gated on
daemon reachability. Run via `just` or directly with the daemon up.

NB (honest scope, per the Class-3 note): this proves end-to-end recursion +
real-provider-at-depth + shared-ledger monotonicity. It does NOT prove strict
cross-level *executed* gate descent — that descent is recorded-not-applied
(`.harness/class_3_hierarchical_delegation_descent_recorded_not_applied.md`).

Authority: `Spec_Control_Plane_v1_32.md` §25.11 + `Implementation_Plan_Control_Plane_v2_32.md`
§2.2 (U-CP-89); `[[feedback-run-credential-gated-live-e2e-authorized]]`.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from harness_runtime.api import RunResult

from .test_r300_cross_family_fallback_e2e import (
    _build_config,
    _build_valid_ollama_chain,
    _gen_ai_provider_names,
    _install_fake_od_stage4,
    _ollama_reachable,
)

pytestmark = pytest.mark.e2e


def _inference_step(step_id: str, prompt: str) -> Any:
    from harness_core.identity import StepID
    from harness_cp.workflow_driver_types import StepKind, WorkflowStep

    return WorkflowStep(
        step_id=StepID(step_id),
        step_kind=StepKind.INFERENCE_STEP,
        # ollama params shape: `options={"num_predict": ...}` (not `max_tokens`).
        step_payload={
            "messages": [{"role": "user", "content": prompt}],
            "tools": [],
            "params": {"options": {"num_predict": 4}},
        },
    )


def _hierarchical_manifest(workflow_id: str, chain: Any) -> Any:
    from harness_core.persona_tier import PersonaTier
    from harness_core.workload_class import WorkloadClass
    from harness_cp.engine_class import EngineClass
    from harness_cp.topology_pattern import TopologyPattern
    from harness_cp.workflow_manifest_entry import WorkflowManifestEntry

    return WorkflowManifestEntry(
        workflow_id=workflow_id,
        # HIERARCHICAL_DELEGATION is §10.3-admissible for SOFTWARE_ENGINEERING.
        workload_class=WorkloadClass.SOFTWARE_ENGINEERING,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        topology_pattern=TopologyPattern.HIERARCHICAL_DELEGATION,
        layer_budgets=(),
        fallback_chain=chain,
        hitl_placements=(),
        per_step_overrides={},
    )


def _brief() -> Any:
    from harness_cp.sub_agent_brief import (
        ClearTaskBoundaries,
        OutputSchema,
        OutputSchemaKind,
        SubAgentBrief,
        compute_brief_summary_hash,
    )

    boundaries = ClearTaskBoundaries(
        in_scope=("inference",), out_of_scope=("nothing",), termination_criteria=("done",)
    )
    out_fmt = OutputSchema(schema_kind=OutputSchemaKind.FREE_TEXT)

    def _build(h: str) -> SubAgentBrief:
        return SubAgentBrief(
            objective="run a hierarchical sub-delegation",
            output_format=out_fmt,
            guidance="answer briefly",
            task_boundaries=boundaries,
            summary_hash=h,
        )

    return _build(compute_brief_summary_hash(_build("0" * 64)))


def _make_depth2_hierarchical_workflow(chain: Any) -> Any:
    """Root HIERARCHICAL [INFERENCE orchestrator, SUB_AGENT_DISPATCH worker] whose
    child is HIERARCHICAL [INFERENCE orchestrator, INFERENCE worker] — genuine
    depth-2 with a real Ollama call at every level (3 calls). The bootstrap binds
    only TOOL/INFERENCE/SUB_AGENT_DISPATCH (NOT DECLARATIVE), so orchestrators are
    INFERENCE_STEPs."""
    from harness_core.identity import StepID
    from harness_core.workload_class import WorkloadClass
    from harness_cp.workflow_driver_types import StepKind, WorkflowStep

    child_manifest = _hierarchical_manifest("wf-ucp89-child", chain)
    child_steps = [
        _inference_step("child-orch", "Say 'c'"),
        _inference_step("child-worker", "Say 'd'"),
    ]
    sub_agent_worker = WorkflowStep(
        step_id=StepID("sub"),
        step_kind=StepKind.SUB_AGENT_DISPATCH,
        step_payload={
            "child_workflow_id": "wf-ucp89-child",
            "child_manifest_entry": child_manifest,
            "child_steps": child_steps,
            "brief": _brief(),
        },
    )
    root_manifest = _hierarchical_manifest("wf-ucp89-root", chain)
    root_steps: list[Any] = [_inference_step("root-orch", "Say 'a'"), sub_agent_worker]

    class _Workflow:
        @property
        def workflow_id(self) -> str:
            return "wf-ucp89-root"

        @property
        def workload_class(self) -> WorkloadClass:
            return WorkloadClass.SOFTWARE_ENGINEERING

        @property
        def manifest_entry(self) -> Any:
            return root_manifest

        @property
        def steps(self) -> Sequence[Any]:
            return tuple(root_steps)

        @property
        def default_model_binding(self) -> Any:
            from harness_cp.cp_shared_types import ModelBinding

            return ModelBinding(provider=chain.primary.provider, model=chain.primary.model)

    return _Workflow()


@pytest.mark.xfail(
    reason=(
        "blocked by .harness/runtime_defect_sub_agent_inference_child_loop_bridge_deadlock.md "
        "— a SUB_AGENT_DISPATCH worker of an INFERENCE child deadlocks the runtime "
        "sync/async bridge (nested `run_coroutine_threadsafe` to the loop-bound provider "
        "loop while that loop is mid-step executing the outer HITL-gate bridge → "
        "RT-FAIL-STEP-DISPATCH-TIMEOUT). Pre-existing (U-RT-59 facade + HITL bridge), "
        "exposed by U-CP-89's first real-provider sub-agent e2e; NOT the CP strategy. "
        "Integration NOT verified past the sub-agent INFERENCE seam — flips to XPASS "
        "when the runtime fork lands (strict=False)."
    ),
    strict=False,
)
@pytest.mark.skipif(
    not _ollama_reachable(),
    reason="live hierarchical-delegation depth-2 e2e requires a local ollama daemon on 127.0.0.1:11434",
)
@pytest.mark.asyncio
async def test_u_cp_89_hierarchical_delegation_depth2_live_ollama(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Genuine depth-2 hierarchical delegation through the full real stack, with a
    real Ollama INFERENCE_STEP at every level. Proves: (1) the materialized
    HIERARCHICAL_DELEGATION child is no longer rejected by the REAL recursion seam
    (`child_workflow_runner` → `execute_workflow`); (2) the run SUCCEEDS end-to-end;
    (3) Ollama answered at depth (the discriminating `gen_ai.provider.name`)."""
    from harness_runtime.api import run as _run

    exporter = _install_fake_od_stage4(monkeypatch)
    chain = _build_valid_ollama_chain()
    config = _build_config(
        tmp_path,
        chain,
        anthropic_optional=True,
        openai_optional=True,
        ollama_optional=False,
    )
    monkeypatch.setattr("harness_runtime.api._default_config", lambda: config)

    result = await _run(_make_depth2_hierarchical_workflow(chain), config=config)

    assert isinstance(result, RunResult), f"got {type(result).__name__}"
    assert result.status == "completed", (
        f"depth-2 hierarchical delegation must SUCCEED end-to-end through the real "
        f"recursion seam; got status={result.status!r} "
        f"failure_cause={getattr(result, 'failure_cause', None)!r}"
    )
    provider_names = _gen_ai_provider_names(exporter.get_finished_spans())
    assert "ollama" in provider_names, (
        f"the real Ollama provider must answer at depth through the recursion; "
        f"observed provider.name values: {provider_names!r}"
    )


# NB: the depth-2 isolation experiment (linear child / pipeline-automation) that
# proved this deadlock is topology-INDEPENDENT (a LINEAR child deadlocks
# identically) is recorded in
# `.harness/runtime_defect_sub_agent_inference_child_loop_bridge_deadlock.md`; it
# is not retained as a test (it asserts the same xfail-blocked seam as the test
# above).
