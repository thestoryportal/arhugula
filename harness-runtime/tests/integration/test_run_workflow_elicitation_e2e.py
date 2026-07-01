"""U-RT-62 AC #6 — End-to-end CC → run_workflow → HITL → ctx.elicit → response → continuation.

**Load-bearing test for criterion B verification per `Spec_Harness_Runtime_v1.md`
v1.12 §14.8.3 v1.12 RETIRE-READY → RETIRED gate.**

Exercises the full topology pinned at spec v1.12 §14.8.3 v1.12 workflow-
initiation topology pin (Reading α CC-initiates):

1. In-process MCP client (stand-in for Claude Code) calls `run_workflow` tool
   on H_T's FastMCP server (materialized at bootstrap stage 2 per AC #2).
2. Tool body executes the workflow via `execute_workflow` (worker thread).
3. HITL gate composer fires once at the matching PRE_ACTION placement.
4. Composer awaits `ctx.ask_user_question_surface.ask(...)` →
   `ServerCtxElicitCallback` (AC #4) → `await ctx.elicit(message, schema)`
   outbound on the active server session.
5. In-process ClientSession's `elicitation_callback` receives the request +
   delivers the canned response.
6. Composer continues to inner dispatcher (step body delegation).
7. Workflow completes; tool returns `RunResult` JSON to client.

**Criterion B verification (X-AL-2 strict reading):** The H_E `AskUserQuestion`
surface is reached only via the MCP envelope at v1.12 — the production
substitution site at the composer body is no longer the spec-substituted
direct-invocation H_E surface; it is the MCP server's `ctx.elicit(...)`.
This test demonstrates the topology end-to-end.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, cast

import pytest
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
from harness_cp.hitl_placement import HITLPlacement, HITLPlacementKind
from harness_cp.hitl_response_palette import HITLResponse
from harness_cp.routing_manifest_residence import RoutingManifest
from harness_cp.topology_pattern import TopologyPattern
from harness_cp.workflow_driver import StepDispatcher as _CpStepDispatcher
from harness_cp.workflow_driver_types import (
    StepKind,
    WorkflowStep,
)
from harness_cp.workflow_manifest_entry import WorkflowManifestEntry
from harness_is.path_class_registry import PathClass
from harness_is.state_ledger_entry_schema import Identifier as _Identifier
from harness_od.audit_ledger_types import SignatureAlgorithm
from harness_runtime.bootstrap import run_bootstrap
from harness_runtime.bootstrap import stage_4_od as _stage_4_od_mod
from harness_runtime.lifecycle.hitl_gate_composer import RuntimeHITLGateComposer
from harness_runtime.lifecycle.providers import ProviderClientsStage
from harness_runtime.lifecycle.sync_dispatcher_facade import (
    materialize_sync_dispatcher_facade,
)
from harness_runtime.shutdown import shutdown
from harness_runtime.types import (
    CollectorConfig,
    HarnessContext,
    OTelConfig,
    PathBindingConfig,
    ProviderSecretsConfig,
    RuntimeConfig,
)
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import ElicitResult

_WORKLOAD = WorkloadClass.SOFTWARE_ENGINEERING
_SURFACE = DeploymentSurface.LOCAL_DEVELOPMENT
_CHAIN = FallbackChain(
    primary=ProviderCandidate(
        provider="anthropic",
        model="claude-haiku-4-5",
        family=ProviderFamily.ANTHROPIC,
    ),
    same_family=(),
    cross_family=(),
    terminal=None,
)


def _path_bindings(tmp_path: Path) -> PathBindingConfig:
    return PathBindingConfig(
        raw_entries=tuple(
            {
                "path_class": pc,
                "workflow_class": _WORKLOAD,
                "deployment_surface": _SURFACE,
                "path": str(tmp_path / pc.value.lower()),
            }
            for pc in PathClass
        ),
    )


def _config(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(
        deployment_surface=_SURFACE,
        repository_root=tmp_path,
        path_bindings=_path_bindings(tmp_path),
        provider_secrets=ProviderSecretsConfig(),
        otel=OTelConfig(otlp_endpoint="http://localhost:4317"),
        collector=CollectorConfig(),
        default_topology=TopologyPattern.SINGLE_THREADED_LINEAR,
        mcp_clients=[],
        ollama_optional=True,
        routing_manifest=RoutingManifest(
            manifest_version=1,
            per_role_bindings={},
            per_workload_overrides={},
            fallback_chains=(_CHAIN,),
            retry_policies={},
        ),
    )


class _FakeProvider:
    def __init__(self, name: str) -> None:
        self.name = name
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _FakeDaemon:
    async def start(self) -> None:
        return None

    async def stop(self, *, timeout_seconds: float = 5.0) -> None:
        _ = timeout_seconds


def _make_real_tracer_provider() -> tuple[Any, Any]:
    """Build a real OTel SDK TracerProvider + InMemorySpanExporter pair.

    Per advisor reconciliation at the commit-7 tightening pass — the
    earlier no-op tracer silently absorbed span emissions, leaving the
    AC #6 criterion-B audit + span hierarchy verification unvalidated.
    The real exporter records every span the HITL composer emits, so
    the test can assert the 3 canonical HITL spans per matching
    placement per spec §14.8.5 (`hitl.gate.evaluated` + `hitl.invocation.
    opened` + `hitl.invocation.responded` — timeout-branch unexercised
    on accept path).
    """
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


@pytest.fixture
def _patched_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[dict[str, Any]]:
    """Patch providers + OD stage 4 with in-process fakes (same as smoke fixture)."""
    providers = {
        "anthropic": _FakeProvider("anthropic"),
        "openai": _FakeProvider("openai"),
        "ollama": _FakeProvider("ollama"),
    }

    async def _fake_clients(*_a: object, **_k: object) -> ProviderClientsStage:
        return ProviderClientsStage(providers=dict(providers))

    monkeypatch.setattr(
        "harness_runtime.bootstrap.stage_3a_cp_clients.materialize_provider_clients_stage",
        _fake_clients,
    )

    daemon = _FakeDaemon()
    tracer_provider, span_exporter = _make_real_tracer_provider()

    class _CollectorStage:
        def __init__(self, d: Any) -> None:
            self.daemon = d

    class _TracerStage:
        def __init__(self, p: Any) -> None:
            self.provider = p
            self.registered_globally = False

    monkeypatch.setattr(
        _stage_4_od_mod,
        "materialize_collector_daemon_stage",
        lambda config, **_k: _CollectorStage(daemon),
    )
    monkeypatch.setattr(
        _stage_4_od_mod,
        "materialize_ring_buffer_stage",
        lambda config, _d: None,
    )
    monkeypatch.setattr(
        _stage_4_od_mod,
        "materialize_tracer_provider_stage",
        lambda config, **_k: _TracerStage(tracer_provider),
    )
    monkeypatch.setattr(
        _stage_4_od_mod,
        "materialize_span_processor_stage",
        lambda config, _p, **_k: None,
    )

    yield {
        "providers": providers,
        "daemon": daemon,
        "tracer_provider": tracer_provider,
        "span_exporter": span_exporter,
    }


def _build_workflow_with_hitl_placement(
    step_dispatchers_override: Any,
) -> Any:
    """Construct a single-step WorkflowObject with a PRE_ACTION HITL placement.

    The placement is attached to the step via the adapter pattern from
    test_lifecycle_hitl_gate_composer.py (`WorkflowStep` is frozen +
    `extra="forbid"`; composer reads `getattr(step, "hitl_placements", ())`).
    """
    base_step = WorkflowStep(
        step_id=StepID("step-0"),
        step_kind=StepKind.INFERENCE_STEP,
        step_payload={"index": 0},
    )

    class _StepWithPlacements:
        def __init__(self) -> None:
            self.hitl_placements = (HITLPlacement(position=HITLPlacementKind.PRE_ACTION),)

        def __getattr__(self, name: str) -> Any:
            return getattr(base_step, name)

    enriched_step = cast(WorkflowStep, _StepWithPlacements())

    class _Workflow:
        @property
        def workflow_id(self) -> str:
            return "wf-u-rt-62-e2e"

        @property
        def workload_class(self) -> WorkloadClass:
            return _WORKLOAD

        @property
        def manifest_entry(self) -> WorkflowManifestEntry:
            return WorkflowManifestEntry(
                workflow_id="wf-u-rt-62-e2e",
                workload_class=_WORKLOAD,
                # Post-CP-v1.17 §6.5: persona_tier is now a required field on
                # StepEffectiveBinding and the HITL composer evaluates the
                # (persona_tier, engine_class) matrix cell at hitl_gate_composer
                # line 830. (TEAM_BINDING, PURE_PATTERN_NO_ENGINE) is EXCLUDED
                # per CP §18.1. Use (SOLO_DEVELOPER, PURE_PATTERN_NO_ENGINE) =
                # SYNC_BLOCKING so the elicit path remains exercised.
                persona_tier=PersonaTier.SOLO_DEVELOPER,
                engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
                topology_pattern=TopologyPattern.SINGLE_THREADED_LINEAR,
                layer_budgets=(),
                fallback_chain=_CHAIN,
                hitl_placements=(),
                per_step_overrides={},
            )

        @property
        def steps(self) -> Sequence[WorkflowStep]:
            return (enriched_step,)

        @property
        def step_dispatcher(self) -> _CpStepDispatcher:
            return cast(_CpStepDispatcher, step_dispatchers_override)

        @property
        def step_dispatchers(self) -> Any:
            return step_dispatchers_override

        @property
        def default_model_binding(self) -> ModelBinding:
            return ModelBinding(provider="anthropic", model="claude-haiku-4-5")

    return _Workflow()


def _build_workflow_via_manifest_placement(
    step_dispatchers_override: Any,
) -> Any:
    """R-FS-1 B-HITL-PLACEMENT-PER-STEP-PRODUCER — the full-chain witness builder.

    Unlike `_build_workflow_with_hitl_placement` (which attaches placements to the
    STEP via the `_StepWithPlacements` proxy), this declares the placement at the
    **WORKFLOW manifest** (`hitl_placements=(PRE_ACTION,)`) and dispatches a
    **PLAIN `WorkflowStep`** (no proxy). The gate therefore fires ONLY if the CP
    driver surfaces `manifest_entry.hitl_placements` onto `step_context` AND the
    composer reads it — i.e. it witnesses producer → step_context → real composer
    → gate in ONE real `execute_workflow` run (the chain the producer arc exists
    to close; the proxy builder would pass even if the producer were broken).

    `(SOLO_DEVELOPER, PURE_PATTERN_NO_ENGINE)` is a NON-excluded cell per CP §18.1,
    so the gate composes cleanly (not the HITLCellExcludedError path).
    """
    plain_step = WorkflowStep(
        step_id=StepID("step-0"),
        step_kind=StepKind.INFERENCE_STEP,
        step_payload={"index": 0},
    )

    class _Workflow:
        @property
        def workflow_id(self) -> str:
            return "wf-b-hitl-placement-producer-e2e"

        @property
        def workload_class(self) -> WorkloadClass:
            return _WORKLOAD

        @property
        def manifest_entry(self) -> WorkflowManifestEntry:
            return WorkflowManifestEntry(
                workflow_id="wf-b-hitl-placement-producer-e2e",
                workload_class=_WORKLOAD,
                persona_tier=PersonaTier.SOLO_DEVELOPER,
                engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
                topology_pattern=TopologyPattern.SINGLE_THREADED_LINEAR,
                layer_budgets=(),
                fallback_chain=_CHAIN,
                # The producer's source — declared at the WORKFLOW manifest, NOT
                # attached to the step. The driver surfaces it onto step_context.
                hitl_placements=(HITLPlacement(position=HITLPlacementKind.PRE_ACTION),),
                per_step_overrides={},
            )

        @property
        def steps(self) -> Sequence[WorkflowStep]:
            return (plain_step,)

        @property
        def step_dispatcher(self) -> _CpStepDispatcher:
            return cast(_CpStepDispatcher, step_dispatchers_override)

        @property
        def step_dispatchers(self) -> Any:
            return step_dispatchers_override

        @property
        def default_model_binding(self) -> ModelBinding:
            return ModelBinding(provider="anthropic", model="claude-haiku-4-5")

    return _Workflow()


def _make_test_step_dispatchers_override(
    ctx: HarnessContext,
    elicit_observer: list[int],
) -> Any:
    """Build a step_dispatcher registry binding INFERENCE_STEP to a manually-
    constructed HITL composer wrapping a fake inner dispatcher.

    The HITL composer is constructed against the production
    `ctx.ask_user_question_surface` (which carries the `ServerCtxElicitCallback`
    per stage 5 default binding at AC #4). The fake inner dispatcher returns
    a fixed dict so the workflow completes without hitting a real LLM.
    """

    class _FakeInnerDispatcher:
        """Sync inner dispatcher — returns a canned step result."""

        def dispatch(
            self,
            binding: Any,
            step: Any,
            *,
            step_context: Any,
        ) -> dict[str, Any]:
            _ = binding, step, step_context
            return {"step_id": "step-0", "ok": True, "via": "fake-inner"}

    hitl_composer = RuntimeHITLGateComposer(
        inner=_FakeInnerDispatcher(),
        applicable_placements=frozenset({HITLPlacementKind.PRE_ACTION}),
        ask_user_question_surface=cast(Any, ctx.ask_user_question_surface),
        ledger_writer=cast(Any, ctx.ledger_writer),
        audit_writer=cast(Any, ctx.audit_writer),
        tracer_provider=cast(Any, ctx.tracer_provider),
        audit_signing_key_id="harness-runtime-test",
        audit_signing_algorithm=SignatureAlgorithm.ED25519,
        procedural_tier_snapshot_resolver=lambda: _Identifier("b" * 64),
    )
    sync_facade = materialize_sync_dispatcher_facade(
        cast(Any, hitl_composer), result_timeout_seconds=30.0
    )
    _ = elicit_observer  # callback captures observer directly; passed for sym.

    class _SingleKindRegistry:
        def lookup(self, step_kind: Any) -> Any:
            _ = step_kind
            return sync_facade

    return _SingleKindRegistry()


@pytest.mark.asyncio
async def test_e2e_run_workflow_elicit_round_trip(
    tmp_path: Path,
    _patched_runtime: dict[str, Any],
) -> None:
    """AC #6 — full topology end-to-end with PRE_ACTION HITL placement.

    Asserts the load-bearing claims per spec v1.12 §14.8.3 v1.12 RETIRE-READY
    → RETIRED gate (criterion B):
    - Client successfully calls the `run_workflow` tool.
    - `ctx.elicit` is invoked exactly once with the composed prompt.
    - Operator's canned APPROVE response flows back through the surface +
      composer + workflow continuation.
    - Workflow completes; `RunResult` returned with status='completed'.
    - The H_E `AskUserQuestion` surface is reached only via the MCP envelope
      (the elicitation_callback IS the H_E surrogate; no direct invocation).
    """
    config = _config(tmp_path)
    ctx = await run_bootstrap(config, workload_class=_WORKLOAD)
    assert ctx.mcp_server is not None, "U-RT-62 AC #2 — mcp_server bound at stage 2"

    # AC #6 criterion-B verification surface — the audit_writer is a
    # frozen+slotted dataclass (no attribute assignment), so we use the
    # `read_all()` reader post-run to count audit-ledger entries emitted
    # by composer step 4h 4-substep audit-write (spec §14.8.6).
    audit_writer_for_count = ctx.audit_writer
    elicit_call_count: list[int] = [0]
    received_messages: list[str] = []

    async def _canned_elicitation_callback(
        request_context: Any,
        params: Any,
    ) -> ElicitResult:
        """Client-side elicitation handler — stands in for Claude Code's
        MCP-client elicitation UI per CC 2.1.76+ (March 2026).
        """
        _ = request_context
        elicit_call_count[0] += 1
        received_messages.append(params.message)
        return ElicitResult(
            action="accept",
            content={
                "response": HITLResponse.APPROVE.value,
                "edited_proposal": None,
                "response_text": None,
                "rejection_reason": None,
            },
        )

    # Override the production INFERENCE_STEP wrap chain with a test composer
    # bound to the production `ctx.ask_user_question_surface` (which carries
    # `ServerCtxElicitCallback` per stage 5 default binding at AC #4). This
    # avoids hitting a real LLM while preserving the elicit-routing path.
    workflow = _build_workflow_with_hitl_placement(
        _make_test_step_dispatchers_override(ctx, elicit_call_count)
    )

    # Bind the post-bootstrap context on `ctx.mcp_server` so the
    # `run_workflow` tool body can resolve `ctx.step_dispatchers` etc., and
    # register the workflow so the tool body can look it up by id.
    ctx.mcp_server._state["_harness_ctx"] = ctx
    ctx.mcp_server.workflow_registry[workflow.workflow_id] = workflow

    try:
        # Open in-process ClientSession with the canned elicitation handler.
        async with create_connected_server_and_client_session(
            ctx.mcp_server.server,
            elicitation_callback=_canned_elicitation_callback,
            raise_exceptions=True,
        ) as session:
            tool_result = await session.call_tool(
                "run_workflow", {"workflow_id": workflow.workflow_id}
            )

        # AC #6 (i): tool call succeeds.
        assert tool_result.isError is False, f"run_workflow tool error: {tool_result.content!r}"
        assert tool_result.content, "expected JSON RunResult content"

        # AC #6 (iii): ctx.elicit invoked exactly once.
        assert elicit_call_count[0] == 1, (
            f"expected exactly 1 elicit call (PRE_ACTION placement, single "
            f"step); saw {elicit_call_count[0]}"
        )

        # AC #6 (iv): elicitation_callback received the composed prompt.
        assert len(received_messages) == 1
        assert "HITL gate at pre-action" in received_messages[0]

        # AC #6 (vi)(vii)(viii): composer continued; workflow completed;
        # tool returned a RunResult-shaped payload. Parse the JSON payload.
        import json

        payload_text = tool_result.content[0].text  # type: ignore[union-attr]
        cp_result_dict = json.loads(payload_text)
        assert cp_result_dict["status"] == "success", (
            f"expected SUCCESS status from completed workflow; got {cp_result_dict.get('status')!r}"
        )
        assert cp_result_dict["workflow_id"] == workflow.workflow_id

        # AC #6 (v) — 4-span canonical HITL hierarchy per spec §14.8.5
        # (timeout branch unexercised on accept path → 3 spans emitted:
        # `hitl.gate.evaluated` + `hitl.invocation.opened` +
        # `hitl.invocation.responded`).
        span_exporter = _patched_runtime["span_exporter"]
        finished_spans = span_exporter.get_finished_spans()
        hitl_span_names = [s.name for s in finished_spans if "hitl" in s.name]
        assert len(hitl_span_names) >= 3, (
            f"expected ≥3 canonical HITL spans (gate.evaluated + "
            f"invocation.opened + invocation.responded) on accept path; "
            f"got {hitl_span_names}"
        )
        # Verify the 3 canonical names are present (timeout branch not
        # exercised on accept path).
        names_set = set(hitl_span_names)
        assert any("gate.evaluated" in n for n in names_set), (
            f"missing hitl.gate.evaluated span; got {names_set}"
        )
        assert any("invocation.opened" in n for n in names_set), (
            f"missing hitl.invocation.opened span; got {names_set}"
        )
        assert any("invocation.responded" in n for n in names_set), (
            f"missing hitl.invocation.responded span; got {names_set}"
        )

        # AC #6 (v) — 4-substep audit-write per spec §14.8.6 (composer
        # step 4h emits ≥1 audit-ledger entry on the matching placement).
        audit_entries = audit_writer_for_count.read_all()
        assert len(audit_entries) >= 1, (
            f"expected ≥1 audit-ledger entry from composer step 4h "
            f"4-substep audit-write on PRE_ACTION matching placement; "
            f"saw {len(audit_entries)}"
        )
    finally:
        ctx.mcp_server._state.pop("_harness_ctx", None)
        ctx.mcp_server.workflow_registry.pop(workflow.workflow_id, None)
        await shutdown(ctx, timeout=5.0)


@pytest.mark.asyncio
async def test_e2e_manifest_placement_fires_gate_via_producer(
    tmp_path: Path,
    _patched_runtime: dict[str, Any],
) -> None:
    """R-FS-1 B-HITL-PLACEMENT-PER-STEP-PRODUCER — the FULL-CHAIN witness.

    The headline claim ("the wrap-time HITL gates fire in production") requires
    ONE run where a manifest-declared placement flows through the real
    `execute_workflow` → real `RuntimeHITLGateComposer` → a fired gate. The
    producer test (driver → step_context, capturing dispatcher) and the composer
    test (hand-built step_context, bare binding) each prove a half; this test
    composes them: a PLAIN `WorkflowStep` + a WORKFLOW-manifest `hitl_placements`
    → the gate fires (`ctx.elicit` invoked) ONLY because the CP driver surfaced
    the manifest placement onto `step_context` and the composer read it. (The
    proxy-based `test_e2e_run_workflow_elicit_round_trip` would pass even if the
    producer were broken — it bypasses the producer via the step proxy.)
    """
    config = _config(tmp_path)
    ctx = await run_bootstrap(config, workload_class=_WORKLOAD)
    assert ctx.mcp_server is not None

    elicit_call_count: list[int] = [0]
    received_messages: list[str] = []

    async def _canned_elicitation_callback(
        request_context: Any,
        params: Any,
    ) -> ElicitResult:
        _ = request_context
        elicit_call_count[0] += 1
        received_messages.append(params.message)
        return ElicitResult(
            action="accept",
            content={
                "response": HITLResponse.APPROVE.value,
                "edited_proposal": None,
                "response_text": None,
                "rejection_reason": None,
            },
        )

    # NOTE: the workflow declares the placement at the MANIFEST + dispatches a
    # PLAIN WorkflowStep — no `_StepWithPlacements` proxy. The gate fires only via
    # the producer (driver → step_context) + composer read.
    workflow = _build_workflow_via_manifest_placement(
        _make_test_step_dispatchers_override(ctx, elicit_call_count)
    )
    ctx.mcp_server._state["_harness_ctx"] = ctx
    ctx.mcp_server.workflow_registry[workflow.workflow_id] = workflow

    try:
        async with create_connected_server_and_client_session(
            ctx.mcp_server.server,
            elicitation_callback=_canned_elicitation_callback,
            raise_exceptions=True,
        ) as session:
            tool_result = await session.call_tool(
                "run_workflow", {"workflow_id": workflow.workflow_id}
            )

        assert tool_result.isError is False, f"run_workflow tool error: {tool_result.content!r}"
        # The load-bearing assertion: the gate FIRED via the manifest→driver→
        # step_context→composer producer chain (NOT a step proxy).
        assert elicit_call_count[0] == 1, (
            f"expected exactly 1 elicit call from the manifest-declared "
            f"PRE_ACTION placement surfaced via the producer; saw "
            f"{elicit_call_count[0]} — the producer chain did not fire the gate"
        )
        assert len(received_messages) == 1
        assert "HITL gate at pre-action" in received_messages[0]

        import json

        payload_text = tool_result.content[0].text  # type: ignore[union-attr]
        cp_result_dict = json.loads(payload_text)
        assert cp_result_dict["status"] == "success"
        assert cp_result_dict["workflow_id"] == workflow.workflow_id

        # The canonical HITL spans emitted (gate genuinely composed, not a stub).
        span_exporter = _patched_runtime["span_exporter"]
        hitl_span_names = {s.name for s in span_exporter.get_finished_spans() if "hitl" in s.name}
        assert any("gate.evaluated" in n for n in hitl_span_names), (
            f"missing hitl.gate.evaluated span; got {hitl_span_names}"
        )
    finally:
        ctx.mcp_server._state.pop("_harness_ctx", None)
        ctx.mcp_server.workflow_registry.pop(workflow.workflow_id, None)
        await shutdown(ctx, timeout=5.0)


@pytest.mark.asyncio
async def test_e2e_run_workflow_decline_maps_to_reject(
    tmp_path: Path,
    _patched_runtime: dict[str, Any],
) -> None:
    """AC #6 + AC #4 negative-path: operator decline → workflow surfaces REJECT
    semantics. The composer maps `ServerCtxElicitCallback`'s
    `AskUserQuestionResult(REJECT, ...)` to step 4i REJECT branch which raises
    `HITLGateRejectedError`; the driver returns `RunStatus.FAILED` with
    `fail_class` set to the rejection class.

    Verifies the criterion-B-adjacent semantics: the elicitation response IS
    what drives the composer's branch selection (vs being silently absorbed).
    """
    config = _config(tmp_path)
    ctx = await run_bootstrap(config, workload_class=_WORKLOAD)
    assert ctx.mcp_server is not None

    async def _decline_callback(request_context: Any, params: Any) -> ElicitResult:
        _ = request_context, params
        return ElicitResult(action="decline", content=None)

    workflow = _build_workflow_with_hitl_placement(_make_test_step_dispatchers_override(ctx, []))
    ctx.mcp_server._state["_harness_ctx"] = ctx
    ctx.mcp_server.workflow_registry[workflow.workflow_id] = workflow

    try:
        async with create_connected_server_and_client_session(
            ctx.mcp_server.server,
            elicitation_callback=_decline_callback,
            raise_exceptions=True,
        ) as session:
            tool_result = await session.call_tool(
                "run_workflow", {"workflow_id": workflow.workflow_id}
            )

        # The decline → REJECT path surfaces as a FAILED workflow OR as a tool
        # error depending on how the composer's HITLGateRejectedError surfaces
        # through execute_workflow. Either outcome is acceptable for AC #6's
        # criterion-B reading: the elicitation response IS driving the path
        # (vs silent absorption).
        import json

        if not tool_result.isError:
            payload_text = tool_result.content[0].text  # type: ignore[union-attr]
            cp_result_dict = json.loads(payload_text)
            # The composer raises HITLGateRejectedError which the driver
            # surfaces as RunStatus.FAILED (or terminates the workflow).
            assert cp_result_dict["status"] in {"failed", "drained"}, (
                f"expected REJECT to surface as failed/drained status; got "
                f"{cp_result_dict.get('status')!r}"
            )
    finally:
        ctx.mcp_server._state.pop("_harness_ctx", None)
        ctx.mcp_server.workflow_registry.pop(workflow.workflow_id, None)
        await shutdown(ctx, timeout=5.0)
