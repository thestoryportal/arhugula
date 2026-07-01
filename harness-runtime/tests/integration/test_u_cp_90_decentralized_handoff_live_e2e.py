"""U-CP-90 `DECENTRALIZED_HANDOFF` — genuine 3-stage live e2e (real stack + Ollama).

The CP-axis unit suite (`harness-cp/tests/test_workflow_driver_decentralized_handoff.py`)
proves the single-owner sequential handoff strategy + the persisted-chain
discriminator at the driver level with a faithful dispatcher double. THIS test
removes the double: it runs a genuine 3-stage handoff pipeline through the FULL
real stack — `api.run` → bootstrap → the materialized `_execute_decentralized_handoff`
→ a real **Ollama** INFERENCE_STEP at every stage.

**This is a REAL green, NOT an xfail (the advisor's tripwire).** Unlike the U-CP-89
hierarchical e2e — which deadlocks on the `SUB_AGENT_DISPATCH` sync/async bridge —
DECENTRALIZED_HANDOFF dispatches each stage through the ordinary `StepDispatcher`
(the `HandoffContext` is a RECORD, never a dispatch), so there is NO nested
`run_coroutine_threadsafe` recursion and the multi-stage pipeline completes
end-to-end. Single-owner sequential ⟹ no concurrent drains either (no F1-01
sibling-drain timestamp gap on the shared zero-tolerance ledger). Free (local
Ollama, zero-secret); gated on daemon reachability.

Authority: `Spec_Control_Plane_v1_32.md` §25.11 (the DECENTRALIZED_HANDOFF row) +
`Implementation_Plan_Control_Plane_v2_32.md` §2.2 (U-CP-90);
`[[feedback-run-credential-gated-live-e2e-authorized]]`.
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


def _build_pipeline_config(tmp_path: Path, chain: Any) -> Any:
    """`_build_config` (reused for all the provider/OTel/routing setup) hardcodes
    SOFTWARE_ENGINEERING path bindings; DECENTRALIZED_HANDOFF is §10.3-admissible
    ONLY for PIPELINE_AUTOMATION, and bootstrap resolves PathBindings on the
    WORKFLOW's workload_class — so rebind the path-class cells to PIPELINE_AUTOMATION."""
    from harness_core.deployment_surface import DeploymentSurface
    from harness_core.workload_class import WorkloadClass
    from harness_is.path_class_registry import PathClass

    base = _build_config(
        tmp_path, chain, anthropic_optional=True, openai_optional=True, ollama_optional=False
    )
    surface = DeploymentSurface.LOCAL_DEVELOPMENT
    state_ledger_root = tmp_path / "state_ledger"
    pipeline_entries = tuple(
        {
            "path_class": pc,
            "workflow_class": WorkloadClass.PIPELINE_AUTOMATION,
            "deployment_surface": surface,
            "path": str(
                state_ledger_root if pc is PathClass.STATE_LEDGER else tmp_path / pc.value.lower()
            ),
        }
        for pc in PathClass
    )
    rebound = base.path_bindings.model_copy(update={"raw_entries": pipeline_entries})
    return base.model_copy(update={"path_bindings": rebound})


def _inference_stage(step_id: str, prompt: str) -> Any:
    from harness_core.identity import StepID
    from harness_cp.workflow_driver_types import StepKind, WorkflowStep

    return WorkflowStep(
        step_id=StepID(step_id),
        step_kind=StepKind.INFERENCE_STEP,
        step_payload={
            "messages": [{"role": "user", "content": prompt}],
            "tools": [],
            "params": {"options": {"num_predict": 4}},
        },
    )


def _decentralized_handoff_manifest(workflow_id: str, chain: Any) -> Any:
    from harness_core.persona_tier import PersonaTier
    from harness_core.workload_class import WorkloadClass
    from harness_cp.engine_class import EngineClass
    from harness_cp.topology_pattern import TopologyPattern
    from harness_cp.workflow_manifest_entry import WorkflowManifestEntry

    return WorkflowManifestEntry(
        workflow_id=workflow_id,
        # DECENTRALIZED_HANDOFF is §10.3-admissible ONLY for PIPELINE_AUTOMATION.
        workload_class=WorkloadClass.PIPELINE_AUTOMATION,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
        engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
        topology_pattern=TopologyPattern.DECENTRALIZED_HANDOFF,
        layer_budgets=(),
        fallback_chain=chain,
        hitl_placements=(),
        per_step_overrides={},
    )


def _make_three_stage_handoff_workflow(chain: Any) -> Any:
    """A 3-stage single-owner handoff [stage-a → stage-b → stage-c], each a real
    Ollama INFERENCE_STEP (3 sequential calls). The bootstrap binds INFERENCE_STEP,
    so each per-role stage-expert dispatches a genuine provider call."""
    from harness_core.workload_class import WorkloadClass

    manifest = _decentralized_handoff_manifest("wf-ucp90", chain)
    steps = [
        _inference_stage("stage-a", "Say 'a'"),
        _inference_stage("stage-b", "Say 'b'"),
        _inference_stage("stage-c", "Say 'c'"),
    ]

    class _Workflow:
        @property
        def workflow_id(self) -> str:
            return "wf-ucp90"

        @property
        def workload_class(self) -> WorkloadClass:
            return WorkloadClass.PIPELINE_AUTOMATION

        @property
        def manifest_entry(self) -> Any:
            return manifest

        @property
        def steps(self) -> Sequence[Any]:
            return tuple(steps)

        @property
        def default_model_binding(self) -> Any:
            from harness_cp.cp_shared_types import ModelBinding

            return ModelBinding(provider=chain.primary.provider, model=chain.primary.model)

    return _Workflow()


@pytest.mark.skipif(
    not _ollama_reachable(),
    reason="live decentralized-handoff 3-stage e2e requires a local ollama daemon on 127.0.0.1:11434",
)
@pytest.mark.asyncio
async def test_u_cp_90_decentralized_handoff_three_stage_live_ollama(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Genuine 3-stage single-owner handoff through the full real stack, with a real
    Ollama INFERENCE_STEP at every stage. Proves: (1) the materialized
    DECENTRALIZED_HANDOFF strategy is no longer rejected; (2) the run SUCCEEDS
    end-to-end (a REAL green — no SUB_AGENT_DISPATCH bridge, no deadlock); (3) Ollama
    answered at each stage (the discriminating `gen_ai.provider.name`)."""
    from harness_runtime.api import run as _run

    exporter = _install_fake_od_stage4(monkeypatch)
    chain = _build_valid_ollama_chain()
    config = _build_pipeline_config(tmp_path, chain)
    monkeypatch.setattr("harness_runtime.api._default_config", lambda: config)

    result = await _run(_make_three_stage_handoff_workflow(chain), config=config)

    assert isinstance(result, RunResult), f"got {type(result).__name__}"
    assert result.status == "completed", (
        f"3-stage decentralized handoff must SUCCEED end-to-end (single-owner "
        f"sequential dispatch — no sub-agent bridge); got status={result.status!r} "
        f"failure_cause={getattr(result, 'failure_cause', None)!r}"
    )
    provider_names = _gen_ai_provider_names(exporter.get_finished_spans())
    assert "ollama" in provider_names, (
        f"the real Ollama provider must answer at each handoff stage; "
        f"observed provider.name values: {provider_names!r}"
    )
