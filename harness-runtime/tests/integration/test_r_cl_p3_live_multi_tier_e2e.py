"""R-CL-P3 — live multi-tier ``api.run`` e2e (free local Ollama).

The operator-gated/D-2 verification piece that the in-process posture proof
(``test_r_cl_p3_persona_tier_posture.py``) could not run in CI: a **full-workflow
``api.run`` against a real provider, exercised under each of the three
bridging-arc persona tiers** (``SOLO_DEVELOPER`` → ``TEAM_BINDING`` →
``MULTI_TENANT_COMPLIANCE``). It closes capability-completion inventory item #8
(``.harness/capability-completion-inventory-v1.md``) — "P3 live multi-tier e2e",
unblocked by R-CC-1 arc #4 (#515; tool-only/inference-conditional bootstrap).

**What this adds over the in-process posture test (the genuine, non-duplicative
increment).** The in-process test already proves — deterministically, and as the
*authority* for the claim — that the two run-path posture axes are tier-distinct
(OD sampler base-rate 1.0/0.1/0.2 + CP gate synchrony, jointly, TEAM≠both
neighbours). This test does **not** rebuild that distinctness on re-called
resolvers (that would be a hollow proof). It proves two things the in-process
test cannot:

1. **The full ``api.run`` workflow completes against a live provider under each
   tier.** An echo-MCP ``TOOL_STEP`` (real **remote streamable-HTTP** MCP server)
   followed by an ``INFERENCE_STEP`` answered by a live local Ollama model — the
   "echo-MCP multi-tier" shape named in inventory #8 / the dashboard / plan §P3 —
   runs green for SOLO, TEAM, and MTC. Setting ``persona_tier`` does not break the
   real run path. (Migrated from the in-process STDIO server, which the ADR-D2 §1.3
   STDIO transport floor raised to TIER_3 → fail-close; a remote L1 READ_ONLY server
   resolves to TIER_1 with the explicit ``default_sandbox_tier`` override, so the
   tool step completes host-process with no Docker — B-MCP-HOST-REMOTE-TRANSPORT.)
2. **``config.persona_tier`` actually threads through the bootstrap to the bound
   OD sampler, observed *during* the run.** Rather than monkeypatching in a
   provider we built (circular), we **spy** the production
   ``materialize_tracer_provider_stage``: capture the real function, let the
   bootstrap call it, and record the ``base_rate`` it actually bound for this
   run's config. The recorded rate equals the §10.3 per-tier envelope value —
   proving the config→sampler wiring on the live ``api.run`` path, which the
   in-process test (hand-built config, direct stage call, no workflow) does not.

**Scope honesty.** Gate synchrony is *not* observed here: the run carries
``hitl_placements=()`` and ``engine_class=PURE_PATTERN_NO_ENGINE``, so the gate
composer never fires on this path (and the distinct-on-all-columns synchrony
needs ``SAVE_POINT_CHECKPOINT``, an engine class this simple linear run does not
execute). The CP gate axis remains proven in-process. Redaction (collector
boundary) and cost (``RunResult.cost_attribution`` U-RT-49-struck) are likewise
out of the in-process run path and not asserted here.

**Free + skipif-gated.** Requires a local Ollama daemon on 127.0.0.1:11434
(``llama3.2:3b``); zero-token-billed, zero-secret. CI (no daemon) skips cleanly.
Run via ``just mvp-r-cl-p3-multi-tier``.

Authority: ``.harness/post-mvp-full-closure-plan-v1.md`` §P3; C-OD-10 §10.3
(per-tier sampler envelope); ``harness-runtime/tests/test_r_cl_p3_persona_tier_posture.py``
(both-axes distinctness); ``Persona_Document_v1.md`` bridging-arc.
"""

from __future__ import annotations

import re
import socket
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from .fixtures.streamable_http_echo import streamable_http_echo_server

_OLLAMA_HOST = "127.0.0.1"
_OLLAMA_PORT = 11434
_OLLAMA_MODEL = "llama3.2:3b"


def _ollama_reachable() -> bool:
    """True iff the local ollama daemon answers on 127.0.0.1:11434 (free, no creds)."""
    try:
        with socket.create_connection((_OLLAMA_HOST, _OLLAMA_PORT), timeout=1.0):
            return True
    except OSError:
        return False


# `SELF_HOSTED_SERVER` is the surface where all three tiers bind a valid sampler
# cell (the only EXCLUDED cell is MTC × LOCAL_DEVELOPMENT) — pinning it keeps the
# persona tier the sole varying dimension, matching the in-process posture test.
def _expected_base_rates() -> dict[Any, float]:
    from harness_core.persona_tier import PersonaTier

    return {
        PersonaTier.SOLO_DEVELOPER: 1.0,
        PersonaTier.TEAM_BINDING: 0.1,
        PersonaTier.MULTI_TENANT_COMPLIANCE: 0.2,
    }


def _extract_base_rate(provider: Any) -> float:
    """Read the bound sampler's base-rate off a materialized tracer provider.

    Mirrors the in-process posture test's extraction: the
    ``HarnessCompositeSampler`` description carries ``base_rate=<float>``.
    """
    description = provider.sampler.get_description()
    match = re.search(r"base_rate=([0-9.]+)", description)
    assert match is not None, f"no base_rate in sampler description: {description!r}"
    return float(match.group(1))


@pytest.mark.e2e
@pytest.mark.skipif(
    not _ollama_reachable(),
    reason="live multi-tier api.run e2e requires a local ollama daemon on 127.0.0.1:11434",
)
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "persona_tier_name",
    ["SOLO_DEVELOPER", "TEAM_BINDING", "MULTI_TENANT_COMPLIANCE"],
)
async def test_r_cl_p3_live_multi_tier_api_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    persona_tier_name: str,
) -> None:
    """``api.run`` (echo-MCP TOOL_STEP + live-Ollama INFERENCE_STEP) completes
    under ``persona_tier`` and the bootstrap binds that tier's §10.3 sampler
    base-rate on the live run path."""
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
    from harness_runtime.lifecycle.tracer_provider import (
        materialize_tracer_provider_stage as _real_materialize,
    )
    from harness_runtime.types import (
        CollectorConfig,
        MCPClientConfig,
        OTelConfig,
        PathBindingConfig,
        ProviderSecretsConfig,
        RuntimeConfig,
    )

    persona_tier = PersonaTier[persona_tier_name]
    surface = DeploymentSurface.SELF_HOSTED_SERVER
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

    # The live provider: a single valid-candidate ollama chain (no fallback).
    # LOCAL_OPEN_WEIGHT throughout; anthropic + openai degrade-optional (no keys).
    chain = FallbackChain(
        primary=ProviderCandidate(
            provider="ollama",
            model=_OLLAMA_MODEL,
            family=ProviderFamily.LOCAL_OPEN_WEIGHT,
        ),
        same_family=(),
        cross_family=(),
        terminal=None,
    )

    # --- Spy the production tracer-provider materializer: let the bootstrap bind
    # the REAL per-tier sampler (not one we inject — that would be circular) and
    # record the base_rate it actually bound for THIS run's config. We still
    # no-op the OTLP export path (span processor / collector / ring buffer) so the
    # run does not open a real socket; the spied provider is the one the run uses. ---
    recorded: dict[str, float] = {}

    def _spy_materialize(cfg: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("register_globally", False)
        stage = _real_materialize(cfg, **kwargs)
        recorded["base_rate"] = _extract_base_rate(stage.provider)
        return stage

    class _FakeDaemon:
        async def start(self) -> None:
            return None

        async def stop(self, *, timeout_seconds: float = 5.0) -> None:
            _ = timeout_seconds

    class _CollectorStage:
        def __init__(self, daemon: _FakeDaemon) -> None:
            self.daemon = daemon

    monkeypatch.setattr(_stage_4_od_mod, "materialize_tracer_provider_stage", _spy_materialize)
    monkeypatch.setattr(
        _stage_4_od_mod, "materialize_span_processor_stage", lambda cfg, _p, **_kw: None
    )
    monkeypatch.setattr(
        _stage_4_od_mod,
        "materialize_collector_daemon_stage",
        lambda cfg, **_kw: _CollectorStage(_FakeDaemon()),
    )
    monkeypatch.setattr(
        _stage_4_od_mod, "materialize_ring_buffer_stage", lambda cfg, _d, **_kw: None
    )

    echo_value = f"hello-p3-{persona_tier_name.lower()}"

    # The remote streamable-HTTP echo MCP server runs on a localhost port for the
    # duration of the bootstrap + dispatch (host.start() connects to it).
    with streamable_http_echo_server() as mcp_url:
        config = RuntimeConfig(
            deployment_surface=surface,
            persona_tier=persona_tier,
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
                    # stage-3a factory projects this onto the host's coarse
                    # "streamable_http" mechanism; host.start() connects to `mcp_url`.
                    transport=MCPTransport.STREAMABLE_HTTP_L1_PINNED,
                    trust_level=MCPServerTrustLevel.L1_SIGNED_PINNED,
                    blast_radius=BlastRadiusTier.READ_ONLY,
                    connection_url=mcp_url,
                    # remote L1 READ_ONLY → mcp_transport_floor = TIER_1_PROCESS; the
                    # explicit default_sandbox_tier override keeps resolved == TIER_1 on
                    # SELF_HOSTED_SERVER (no Docker). tier-floor consistency: resolver
                    # tier == converter minimum tier == TIER_1_PROCESS.
                    default_minimum_tier=SandboxTier.TIER_1_PROCESS,
                    default_sandbox_tier=SandboxTier.TIER_1_PROCESS,
                    default_sandbox_tech="host-process",
                    default_sandbox_provider="host",
                )
            ],
            anthropic_optional=True,
            openai_optional=True,
            ollama_optional=False,  # ollama is the constructed, required provider
            routing_manifest=RoutingManifest(
                manifest_version=1,
                per_role_bindings={},
                per_workload_overrides={},
                fallback_chains=(chain,),
                retry_policies={},
            ),
        )
        monkeypatch.setattr("harness_runtime.api._default_config", lambda: config)

        class _Workflow:
            @property
            def workflow_id(self) -> str:
                return "wf-r-cl-p3-multi-tier"

            @property
            def workload_class(self) -> WorkloadClass:
                return workload

            @property
            def manifest_entry(self) -> WorkflowManifestEntry:
                return WorkflowManifestEntry(
                    workflow_id="wf-r-cl-p3-multi-tier",
                    workload_class=workload,
                    persona_tier=persona_tier,
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
                        step_payload={"tool_id": "echo", "tool_args": {"value": echo_value}},
                    ),
                    WorkflowStep(
                        step_id=StepID("step-1"),
                        step_kind=StepKind.INFERENCE_STEP,
                        step_payload={
                            "messages": [{"role": "user", "content": "Say 'a'"}],
                            "tools": [],
                            # ollama params shape: options={"num_predict": ...}.
                            "params": {"options": {"num_predict": 4}},
                        },
                    ),
                )

            @property
            def step_dispatchers(self) -> Any:
                return None

            @property
            def default_model_binding(self) -> ModelBinding:
                return ModelBinding(provider="ollama", model=_OLLAMA_MODEL)

        # --- exercise: the operator api.run path under this persona tier. ---
        result = await _run(_Workflow(), config=config)

    # (1) the full workflow completed against the live provider under this tier.
    assert isinstance(result, RunResult), f"got {type(result).__name__}"
    assert result.status == "completed", (
        f"expected status=completed for persona_tier={persona_tier_name} via the "
        f"echo TOOL_STEP + live ollama INFERENCE_STEP; got status={result.status!r} "
        f"failure_cause={getattr(result, 'failure_cause', None)!r}"
    )

    # The echo tool returned its input verbatim (best-effort — terminal shape).
    terminal = getattr(result, "terminal_state", None)
    if isinstance(terminal, dict) and "step-0" in terminal:
        assert echo_value in repr(terminal["step-0"]), (
            f"echo TOOL_STEP did not return its input; terminal_state={terminal!r}"
        )

    # (2) config.persona_tier threaded through the bootstrap to the bound sampler:
    # the bootstrap bound THIS tier's §10.3 base-rate on the live run path.
    expected = _expected_base_rates()[persona_tier]
    assert "base_rate" in recorded, "tracer-provider materializer spy did not fire"
    assert recorded["base_rate"] == expected, (
        f"bootstrap bound base_rate={recorded['base_rate']} for "
        f"persona_tier={persona_tier_name}; expected the §10.3 envelope value {expected}"
    )
