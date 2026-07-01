"""R-100 AC #2 — TOOL_STEP dispatch via the operator `api.run` path (the proof).

The canonical end-to-end proof that the full bootstrap TOOL_STEP path is wired
(spec v1.41 §14.9.8 Reading B + Gaps B/C/D/E/F): one echo-MCP-via-`api.run`
workflow that dispatches a real `TOOL_STEP` against a real **remote streamable-HTTP**
MCP server and completes. Proves the chain BY EXECUTION:
    host.start() → list_tools → converter → registry → trust → resolver →
    floor → call_tool → result.

**Remote streamable-HTTP transport (B-MCP-HOST-REMOTE-TRANSPORT).** The MCP server is a
remote `streamable_http_l1` (trust L1_SIGNED_PINNED, READ_ONLY) echo fixture served over
HTTP on a localhost port. A remote L1 READ_ONLY server resolves to `TIER_1_PROCESS` via
`mcp_transport_floor` (`blast_radius_floor(READ_ONLY)`), so the §14.9.8 B6 Slice-1
transport floor leaves it at the host-process tier — no Docker/microVM driver needed,
keeping this a provider-free DEFAULT-LANE e2e. The earlier in-process STDIO variant was
floored to TIER_3 by ADR-D2 §1.3 (B6 Slice 1) and fail-closed at bootstrap; this remote
variant is the registered restoration. The stage-3a factory's granular→coarse transport
projection (the B-MCP-HOST-REMOTE-TRANSPORT fix) is what lets the remote host materialize.

**Provider-free + unconditional (runtime spec v1.47 §2.1 — R-CC-1 arc #4).**
Gap D (the bootstrap pinged ≥1 provider regardless of step kind, so this e2e was
`skipif`-gated on a live provider it never used) is CLOSED. This workflow is
tool-only (a single `TOOL_STEP`, no inference), so the `run()` predicate derives
`requires_inference == False` and the bootstrap requires NO provider: the config
marks all providers optional, stage 3a tolerates an empty `ctx.providers`, and
stage 5 omits the `INFERENCE_STEP` / `SUB_AGENT_DISPATCH` registry rows. The test
runs in CI with no live provider + no paid call.

Tier-floor consistency (spec v1.41 §14.9.8): the echo server declares both
`default_minimum_tier` (the v1.40 converter stamp) AND `default_sandbox_tier`
(the Reading B resolver) as `TIER_1_PROCESS`, so the §14.9.4 floor
(resolved.tier >= contract.minimum_tier) passes.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from .fixtures.streamable_http_echo import streamable_http_echo_server


def _read_ledger(state_ledger_root: Path) -> list[dict[str, Any]]:
    import json

    entries: list[dict[str, Any]] = []
    for jsonl in sorted(state_ledger_root.rglob("*.jsonl")):
        for line in jsonl.read_text().splitlines():
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


@pytest.mark.asyncio
async def test_r100_ac2_tool_step_via_api_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from harness_as.discriminators import MCPTransport
    from harness_as.sandbox_tier import BlastRadiusTier, SandboxTier
    from harness_as.sandbox_tier_floor import MCPServerTrustLevel
    from harness_core import ClientName
    from harness_core.deployment_surface import DeploymentSurface
    from harness_core.identity import StepID
    from harness_core.persona_tier import PersonaTier
    from harness_core.workload_class import WorkloadClass
    from harness_cp.cp_shared_types import ModelBinding
    from harness_cp.cross_family_fallback_chain import (
        FallbackChain,
        ProviderCandidate,
        ProviderFamily,
    )
    from harness_cp.engine_class import EngineClass
    from harness_cp.routing_manifest_residence import RoutingManifest
    from harness_cp.topology_pattern import TopologyPattern
    from harness_cp.workflow_driver_types import StepKind, WorkflowStep
    from harness_cp.workflow_manifest_entry import WorkflowManifestEntry
    from harness_is.path_class_registry import PathClass
    from harness_runtime.api import RunResult
    from harness_runtime.api import run as _run
    from harness_runtime.bootstrap import stage_4_od as _stage_4_od_mod
    from harness_runtime.types import (
        CollectorConfig,
        MCPClientConfig,
        OTelConfig,
        PathBindingConfig,
        ProviderSecretsConfig,
        RuntimeConfig,
    )

    surface = DeploymentSurface.LOCAL_DEVELOPMENT
    workload = WorkloadClass.PIPELINE_AUTOMATION  # permits SINGLE_THREADED_LINEAR
    state_ledger_root = tmp_path / "state_ledger"
    path_bindings = PathBindingConfig(
        raw_entries=tuple(
            {
                "path_class": pc,
                "workflow_class": workload,
                "deployment_surface": surface,
                "path": str(
                    state_ledger_root
                    if pc is PathClass.STATE_LEDGER
                    else tmp_path / pc.value.lower()
                ),
            }
            for pc in PathClass
        ),
    )

    # --- provider-free config (runtime spec v1.47 §2.1). The workflow is
    # tool-only (no inference) → `requires_inference == False` → stage 3a SKIPS
    # provider construction entirely (no keyring/network work, no per-provider
    # failure can abort the bootstrap), yielding an empty `ctx.providers`; stage
    # 5 omits the INFERENCE_STEP / SUB_AGENT_DISPATCH rows. The nominal ollama
    # binding/chain + `*_optional` flags below only satisfy the config schema —
    # none is read (the composer is never invoked). No live provider, no paid
    # call, no credentials needed. ---
    chain = FallbackChain(
        primary=ProviderCandidate(
            provider="ollama", model="llama3.2", family=ProviderFamily.LOCAL_OPEN_WEIGHT
        ),
        same_family=(),
        cross_family=(),
        terminal=None,
    )
    binding = ModelBinding(provider="ollama", model="llama3.2")
    provider_flags: dict[str, Any] = {
        "anthropic_optional": True,
        "openai_optional": True,
        "ollama_optional": True,
    }

    # NoOp tracer (this AC asserts dispatch + ledger, not spans).
    class _FakeTracerProvider:
        def force_flush(self, timeout_millis: int = 30_000) -> bool:
            _ = timeout_millis
            return True

        def shutdown(self) -> None:
            return None

        def get_tracer(self, instrumenting_module_name: str, /) -> object:
            from opentelemetry.trace import NoOpTracer

            _ = instrumenting_module_name
            return NoOpTracer()

    class _TracerStage:
        def __init__(self, provider: _FakeTracerProvider) -> None:
            self.provider = provider
            self.registered_globally = False

    def _fake_tracer_stage(config: Any, **_kwargs: Any) -> _TracerStage:
        _ = config
        return _TracerStage(_FakeTracerProvider())

    def _fake_span_processor(config: Any, _p: Any, **_kwargs: Any) -> None:
        _ = config
        return None

    monkeypatch.setattr(_stage_4_od_mod, "materialize_tracer_provider_stage", _fake_tracer_stage)
    monkeypatch.setattr(_stage_4_od_mod, "materialize_span_processor_stage", _fake_span_processor)

    # The remote streamable-HTTP echo MCP server runs on a localhost port for the
    # duration of the bootstrap + dispatch (host.start() connects to it).
    with streamable_http_echo_server() as mcp_url:
        config = RuntimeConfig(
            deployment_surface=surface,
            repository_root=tmp_path,
            path_bindings=path_bindings,
            provider_secrets=ProviderSecretsConfig(),
            otel=OTelConfig(otlp_endpoint="http://localhost:4318"),
            collector=CollectorConfig(),
            default_topology=TopologyPattern.SINGLE_THREADED_LINEAR,
            mcp_clients=[
                MCPClientConfig(
                    client_name=ClientName("echo-server"),
                    # Remote streamable-HTTP L1 (B-MCP-HOST-REMOTE-TRANSPORT): the
                    # stage-3a factory projects this granular value onto the host's
                    # coarse "streamable_http" mechanism; host.start() connects via
                    # streamable_http_client to `mcp_url`.
                    transport=MCPTransport.STREAMABLE_HTTP_L1_PINNED,
                    trust_level=MCPServerTrustLevel.L1_SIGNED_PINNED,
                    blast_radius=BlastRadiusTier.READ_ONLY,
                    connection_url=mcp_url,
                    # remote L1 READ_ONLY → mcp_transport_floor = blast_radius_floor(
                    # READ_ONLY) = TIER_1_PROCESS (no Docker/microVM driver — the
                    # provider-free default-lane property). tier-floor consistency:
                    # resolver tier == converter minimum tier.
                    default_minimum_tier=SandboxTier.TIER_1_PROCESS,
                    default_sandbox_tier=SandboxTier.TIER_1_PROCESS,
                    default_sandbox_tech="host-process",
                    default_sandbox_provider="host",
                )
            ],
            routing_manifest=RoutingManifest(
                manifest_version=1,
                per_role_bindings={},
                per_workload_overrides={},
                fallback_chains=(chain,),
                retry_policies={},
            ),
            **provider_flags,
        )
        monkeypatch.setattr("harness_runtime.api._default_config", lambda: config)

        class _Workflow:
            @property
            def workflow_id(self) -> str:
                return "wf-r100-ac2-tool"

            @property
            def workload_class(self) -> WorkloadClass:
                return workload

            @property
            def manifest_entry(self) -> WorkflowManifestEntry:
                return WorkflowManifestEntry(
                    workflow_id="wf-r100-ac2-tool",
                    workload_class=workload,
                    persona_tier=PersonaTier.TEAM_BINDING,
                    engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
                    topology_pattern=TopologyPattern.SINGLE_THREADED_LINEAR,
                    layer_budgets=(),
                    fallback_chain=chain,
                    hitl_placements=(),
                    per_step_overrides={},
                )

            @property
            def steps(self) -> Sequence[WorkflowStep]:
                return (
                    WorkflowStep(
                        step_id=StepID("step-0"),
                        step_kind=StepKind.TOOL_STEP,
                        step_payload={"tool_id": "echo", "tool_args": {"value": "hello-ac2"}},
                    ),
                )

            @property
            def step_dispatchers(self) -> Any:
                return None

            @property
            def default_model_binding(self) -> ModelBinding:
                return binding

        # --- exercise: the operator api.run path with a TOOL_STEP. ---
        result = await _run(_Workflow(), config=config)

    # AC #2 — the TOOL_STEP dispatched through the full bootstrap and completed.
    assert isinstance(result, RunResult), f"got {type(result).__name__}"
    assert result.status == "completed", (
        f"expected status=completed; got status={result.status!r} "
        f"failure_cause={getattr(result, 'failure_cause', None)!r}"
    )

    # The echo tool returned the input verbatim (best-effort — terminal state shape).
    terminal = getattr(result, "terminal_state", None)
    if isinstance(terminal, dict) and "step-0" in terminal:
        assert "hello-ac2" in repr(terminal["step-0"])

    # A state-ledger entry was written for the dispatched step.
    entries = _read_ledger(state_ledger_root)
    assert entries, f"no ledger entries under {state_ledger_root}"
    assert any(
        str(e.get("action_id", "")).startswith("workflow:wf-r100-ac2-tool:step:") for e in entries
    ), "no per-step ledger entry for the TOOL_STEP dispatch"
