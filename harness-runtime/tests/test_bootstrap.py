"""U-RT-43 — 9-stage bootstrap orchestrator tests (opens L9).

Acceptance criteria per Phase 2 Session 3 atomic decomposition §3.8.4:

1. Full bootstrap returns a `HarnessContext` (frozen Pydantic at stage 7).
2. Injected stage failure at each of the 9 substages triggers reverse-order rollback.
3. Each stage emits exactly one lifecycle event (buffered until emitter exists at stage 5).

Additional coverage:
- `_MutableHarnessContext.freeze()` raises `IncompleteBootstrapError` on missing field.
- `WorkflowObject` Protocol structural check passes with `workflow_id` + `workload_class`.
- `api.run` calls `run_bootstrap` with `workflow.workload_class`.
- Post-Lane-6: `api.run()` delegates to the CP workflow driver and returns a `RunResult`.
- Lifecycle event buffer drains in arrival order at stage 5.
- Rollback handlers are best-effort (one handler's exception doesn't halt others).
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from harness_core.deployment_surface import DeploymentSurface
from harness_core.persona_tier import PersonaTier
from harness_core.workload_class import WorkloadClass
from harness_cp.cp_shared_types import AgentRole
from harness_cp.cross_family_fallback_chain import (
    FallbackChain,
    ProviderCandidate,
    ProviderFamily,
)
from harness_cp.prompt_selection_manifest import PromptBinding, PromptSelectionManifest
from harness_cp.routing_manifest_residence import RoutingManifest
from harness_cp.topology_pattern import TopologyPattern
from harness_is.path_class_registry import PathClass
from harness_is.prompt_manifest import PromptManifest, PromptVersion, prompt_version_sha
from harness_is.state_ledger_write import read_ledger
from harness_runtime.bootstrap import (
    BootstrapFailure,
    BootstrapStageCompleteEvent,
    IncompleteBootstrapError,
    run_bootstrap,
)
from harness_runtime.bootstrap import stage_4_od as _stage_4_od_mod
from harness_runtime.bootstrap.mutable_context import _MutableHarnessContext
from harness_runtime.lifecycle.procedural_tier_snapshot import (
    resolve_procedural_tier_snapshot,
)
from harness_runtime.lifecycle.prompt_selection import (
    PromptSelectionUnauthoredError,
    PromptVersionUnapprovedError,
)
from harness_runtime.lifecycle.providers import ProviderClientsStage
from harness_runtime.types import (
    COST_ACCUM_VAR,
    BootstrapStage,
    CollectorConfig,
    CostRecordAccumulator,
    HarnessContext,
    OTelConfig,
    PathBindingConfig,
    ProviderSecretsConfig,
    RunScopedCostRecordAccumulator,
    RuntimeConfig,
)

# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


_WORKLOAD = WorkloadClass.SOFTWARE_ENGINEERING
_SURFACE = DeploymentSurface.LOCAL_DEVELOPMENT
_WORKLOAD_SELECTION_ACTION_ID = "cp.workload-binding-class-selection"


def _path_bindings(tmp_path: Path) -> PathBindingConfig:
    """All 4 PathClass entries under `tmp_path` for stage 1 IS."""
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


def _config(tmp_path: Path) -> RuntimeConfig:
    """Minimal valid `RuntimeConfig` for bootstrap tests."""
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
    """Minimal `ProviderClient` Protocol implementation for tests."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def _patch_providers(monkeypatch: pytest.MonkeyPatch) -> dict[str, _FakeProvider]:
    """Replace `materialize_provider_clients_stage` with a no-op fake."""
    fakes = {
        "anthropic": _FakeProvider("anthropic"),
        "openai": _FakeProvider("openai"),
        "ollama": _FakeProvider("ollama"),
    }

    async def _fake(*_args: object, **_kwargs: object) -> ProviderClientsStage:
        return ProviderClientsStage(providers=dict(fakes))

    monkeypatch.setattr(
        "harness_runtime.bootstrap.stage_3a_cp_clients.materialize_provider_clients_stage",
        _fake,
    )
    return fakes


class _FakeDaemon:
    """In-process collector daemon stub — records start/stop calls."""

    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


def _patch_collector(monkeypatch: pytest.MonkeyPatch) -> _FakeDaemon:
    """Replace stage 4's collector materialization + ring buffer + tracer
    with no-op fakes. Tracer must not globally register (one-time-per-process
    invariant per C-RT-06 forbids repeated `set_tracer_provider`)."""
    daemon = _FakeDaemon()

    class _Stage:
        def __init__(self, d: _FakeDaemon) -> None:
            self.daemon = d

    class _TracerStage:
        def __init__(self) -> None:
            class _P:
                pass

            self.provider = _P()
            self.registered_globally = False

    monkeypatch.setattr(
        _stage_4_od_mod,
        "materialize_collector_daemon_stage",
        lambda config, **_: _Stage(daemon),
    )
    monkeypatch.setattr(
        _stage_4_od_mod,
        "materialize_ring_buffer_stage",
        lambda config, _d: None,
    )
    monkeypatch.setattr(
        _stage_4_od_mod,
        "materialize_tracer_provider_stage",
        lambda config, **_: _TracerStage(),
    )
    monkeypatch.setattr(
        _stage_4_od_mod,
        "materialize_span_processor_stage",
        lambda config, _p, **_k: None,
    )
    return daemon


# ---------------------------------------------------------------------------
# AC #1 — Full bootstrap returns frozen HarnessContext.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_returns_frozen_harness_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_providers(monkeypatch)
    _patch_collector(monkeypatch)
    ctx = await run_bootstrap(_config(tmp_path), workload_class=_WORKLOAD)
    assert isinstance(ctx, HarnessContext)
    assert ctx.model_config["frozen"] is True
    assert ctx.cp_as_wiring is not None
    assert ctx.hitl_tool_loop is not None
    assert ctx.engine_recovery_loop is not None


@pytest.mark.asyncio
async def test_bootstrap_cost_accumulator_survives_freeze_by_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R-FS-1 arc CA + B-INTERSTEP-PERRUN-ISOLATION regression —
    `cost_record_accumulator` must survive `freeze()` BY REFERENCE so the
    per-dispatch cost wrappers' captured sink IS resolvable to the accumulator
    `_build_run_result` reads. Pydantic v2 COPIES a typed `list[...]` field at
    construction, which would sever the dispatchers' sink from the run-result
    read → `cost_attribution` always `()`. The accumulator holder (an arbitrary
    type, like `asyncio.Event`) is stored opaquely, so it is the SAME object
    across freeze.

    Per B-INTERSTEP-PERRUN-ISOLATION the bound holder is a
    `RunScopedCostRecordAccumulator` PROXY, and the wrappers now capture the
    PROXY (not a `.records` list captured once at bootstrap) so each appended
    record routes — at append-time — to the *current run's* accumulator resolved
    from `COST_ACCUM_VAR`. This guards the exact freeze seam every other CA test
    bypasses, AND the per-run resolution seam end-to-end."""
    _patch_providers(monkeypatch)
    _patch_collector(monkeypatch)
    ctx = await run_bootstrap(_config(tmp_path), workload_class=_WORKLOAD)

    # Survives freeze as the run-scoped accumulator PROXY (an arbitrary-type
    # holder, NOT a Pydantic-copied list).
    assert isinstance(ctx.cost_record_accumulator, RunScopedCostRecordAccumulator)

    # The seam: the stage-5 tool-dispatcher factory captured the holder PROXY
    # (`ctx.cost_record_accumulator`) BEFORE freeze; surviving by-reference means
    # the bare dispatcher's sink IS the frozen ctx's proxy (the SAME object).
    bare_tool = ctx.tool_dispatcher.inner  # RetryBreakerToolDispatcher.inner
    assert bare_tool._cost_record_sink is ctx.cost_record_accumulator

    # End-to-end per-run resolution: with a fresh run-scoped accumulator bound in
    # the var, a record appended through the wrapper's sink lands in THAT
    # accumulator, and `_build_run_result`'s read (`ctx.cost_record_accumulator.
    # records`) resolves to the same list — the captured-list defeat is closed.
    per_run = CostRecordAccumulator()
    token = COST_ACCUM_VAR.set(per_run)
    try:
        sentinel = object()
        bare_tool._cost_record_sink.append(sentinel)  # type: ignore[arg-type]
        assert per_run.records == [sentinel]
        assert ctx.cost_record_accumulator.records == [sentinel]
    finally:
        COST_ACCUM_VAR.reset(token)
    # Outside the run scope the proxy falls back to its (empty) bootstrap default.
    assert ctx.cost_record_accumulator.records == []


@pytest.mark.asyncio
async def test_bootstrap_threads_operator_supplied_prompt_manifest_into_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R-CL-P4 production path: an operator-supplied `config.prompt_manifest`
    flows through the real bootstrap (stage 0 copy) onto the frozen ctx AND
    participates in the procedural-tier snapshot — i.e. the third hash
    component is reachable through the normal bootstrap, not only at
    direct test-constructed contexts (Codex review finding)."""
    _patch_providers(monkeypatch)
    _patch_collector(monkeypatch)

    populated = _config(tmp_path).model_copy(
        update={
            "prompt_manifest": PromptManifest(
                manifest_version=1,
                # R-PM-1 PR #1 — content-bearing carrier; version_sha derives
                # from content (the derive-invariant).
                active_prompt_version=PromptVersion.from_content("operator system prompt"),
            ),
        },
    )
    ctx_populated = await run_bootstrap(populated, workload_class=_WORKLOAD)
    # Stage-0 copy landed the operator-supplied carrier on the frozen ctx.
    assert ctx_populated.prompt_manifest.active_prompt_version.content == "operator system prompt"
    assert ctx_populated.prompt_manifest.active_prompt_version.version_sha == prompt_version_sha(
        "operator system prompt"
    )

    # The snapshot through the production path reflects it: a default
    # (empty) prompt_manifest yields a different snapshot ref.
    ctx_empty = await run_bootstrap(_config(tmp_path), workload_class=_WORKLOAD)
    assert ctx_empty.prompt_manifest.active_prompt_version.version_sha == ""
    assert resolve_procedural_tier_snapshot(ctx_populated) != resolve_procedural_tier_snapshot(
        ctx_empty,
    )


@pytest.mark.asyncio
async def test_bootstrap_resolves_active_prompt_content_onto_llm_dispatcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R-PM-1 cascade PR #1 — bootstrap reachability (proof (b)): the active
    prompt's CONTENT resolves through the real stage-5 wiring onto the bare
    `RuntimeLLMDispatcher.active_system_prompt`. This is the exact seam #496
    left inert (a content-bearing carrier that never reached the dispatch path);
    asserting the bare dispatcher's field — not just the snapshot — proves the
    `stage_5_loop_init` resolution line, not only the factory pass-through."""
    from harness_runtime.lifecycle.llm_dispatch import RuntimeLLMDispatcher

    _patch_providers(monkeypatch)
    _patch_collector(monkeypatch)

    populated = _config(tmp_path).model_copy(
        update={
            "prompt_manifest": PromptManifest(
                manifest_version=1,
                active_prompt_version=PromptVersion.from_content("operator system prompt"),
            ),
        },
    )
    ctx = await run_bootstrap(populated, workload_class=_WORKLOAD)

    # Wrap chain: ctx.llm_dispatcher (C-RT-16 retry) → .inner (PRE_ACTION HITL
    # composer) → .inner (bare C-RT-15 RuntimeLLMDispatcher).
    bare = ctx.llm_dispatcher.inner.inner  # type: ignore[attr-defined]
    assert isinstance(bare, RuntimeLLMDispatcher)
    assert bare.active_system_prompt == "operator system prompt"

    # No active prompt → None (the no-injection default; byte-identical dispatch).
    ctx_empty = await run_bootstrap(_config(tmp_path), workload_class=_WORKLOAD)
    bare_empty = ctx_empty.llm_dispatcher.inner.inner  # type: ignore[attr-defined]
    assert bare_empty.active_system_prompt is None


@pytest.mark.asyncio
async def test_bootstrap_workload_selection_drives_active_prompt_and_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R-PM-1 cascade PR #3 — the load-bearing e2e: a CP `per_workload_overrides`
    binding (keyed on the REAL run workload) drives WHICH authored prompt version
    is active through the real bootstrap. The store (PR #2) gains its consumer:
    selection → sha → store content → injection. AND the C-IS-05 §5.2
    procedural-tier hash reflects the SAME selected version (coherence — both the
    injection reader and the hash reader move together)."""
    from harness_runtime.lifecycle.llm_dispatch import RuntimeLLMDispatcher

    _patch_providers(monkeypatch)
    _patch_collector(monkeypatch)

    # An authored versioned store with two members; 'default inline' is the
    # standing active. Selection will override it to 'se-workload prompt'.
    store = PromptManifest.from_contents(
        manifest_version=1,
        contents=["se-workload prompt", "default inline"],
        active="default inline",
    )
    selection = PromptSelectionManifest(
        manifest_version=1,
        per_workload_overrides={
            _WORKLOAD: PromptBinding(version_sha=prompt_version_sha("se-workload prompt")),
        },
    )
    populated = _config(tmp_path).model_copy(
        update={"prompt_manifest": store, "prompt_selection_manifest": selection},
    )
    ctx = await run_bootstrap(populated, workload_class=_WORKLOAD)

    # Selection reconciled the active version away from the standing 'default inline'.
    assert ctx.prompt_manifest.active_prompt_version.content == "se-workload prompt"
    assert ctx.prompt_manifest.active_prompt_version.version_sha == prompt_version_sha(
        "se-workload prompt"
    )
    # Injection reader (the bare dispatcher) sees the selected content.
    bare = ctx.llm_dispatcher.inner.inner  # type: ignore[attr-defined]
    assert isinstance(bare, RuntimeLLMDispatcher)
    assert bare.active_system_prompt == "se-workload prompt"

    # The §5.2 procedural-tier hash moved WITH selection: the selection-driven ctx
    # differs from a no-selection baseline (which keeps 'default inline' active).
    ctx_inline = await run_bootstrap(
        _config(tmp_path).model_copy(update={"prompt_manifest": store}),
        workload_class=_WORKLOAD,
    )
    assert ctx_inline.prompt_manifest.active_prompt_version.content == "default inline"
    assert resolve_procedural_tier_snapshot(ctx) != resolve_procedural_tier_snapshot(ctx_inline)


@pytest.mark.asyncio
async def test_bootstrap_threads_per_role_prompt_map_onto_llm_dispatcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R-FS-1 arc B4 — bootstrap reachability (the NEW this-arc wiring): an
    operator-supplied `per_role_bindings` prompt-selection manifest resolves,
    through the REAL stage-0 → stage-5 thread, onto the bare
    `RuntimeLLMDispatcher.per_role_system_prompts`. Asserting the bare
    dispatcher's field — not just the builder in isolation — proves the
    stage-0 `resolve_per_role_system_prompts` call + the stage-5 factory
    pass-through are wired (the green-unit-but-unreachable mode this guards)."""
    from harness_runtime.lifecycle.llm_dispatch import RuntimeLLMDispatcher

    _patch_providers(monkeypatch)
    _patch_collector(monkeypatch)

    store = PromptManifest.from_contents(
        manifest_version=1,
        contents=["researcher prompt", "default inline"],
        active="default inline",
    )
    # Per-role binding ONLY (no workload override, no default-role binding) →
    # the default-role active prompt is untouched; only the per-role map populates.
    selection = PromptSelectionManifest(
        manifest_version=1,
        per_role_bindings={
            AgentRole("researcher"): PromptBinding(
                version_sha=prompt_version_sha("researcher prompt")
            ),
        },
    )
    populated = _config(tmp_path).model_copy(
        update={"prompt_manifest": store, "prompt_selection_manifest": selection},
    )
    ctx = await run_bootstrap(populated, workload_class=_WORKLOAD)

    bare = ctx.llm_dispatcher.inner.inner  # type: ignore[attr-defined]
    assert isinstance(bare, RuntimeLLMDispatcher)
    # The per-role map reached the dispatcher through the real bootstrap thread.
    assert bare.per_role_system_prompts == {AgentRole("researcher"): "researcher prompt"}
    # Default-role active prompt untouched by a per-role-only selection.
    assert bare.active_system_prompt == "default inline"

    # The §5.2 procedural-tier hash is sensitive to the per-role selection manifest
    # (it reads config.prompt_selection_manifest) — a per-role-only binding, which
    # does NOT move active_prompt_version, still changes the snapshot vs no-selection.
    ctx_inline = await run_bootstrap(
        _config(tmp_path).model_copy(update={"prompt_manifest": store}),
        workload_class=_WORKLOAD,
    )
    bare_inline = ctx_inline.llm_dispatcher.inner.inner  # type: ignore[attr-defined]
    assert bare_inline.per_role_system_prompts == {}
    assert bare_inline.active_system_prompt == "default inline"
    assert resolve_procedural_tier_snapshot(ctx) != resolve_procedural_tier_snapshot(ctx_inline)


@pytest.mark.asyncio
async def test_bootstrap_threads_per_step_prompt_store_onto_llm_dispatcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R-FS-1 arc B4 Slice 3 — bootstrap reachability (the NEW this-arc wiring): the
    IS `PromptManifest.versions` store projects to
    `RuntimeLLMDispatcher.prompt_versions_by_sha` and
    `config.approved_prompt_version_shas` reaches the dispatcher's
    `approved_prompt_version_shas` through the REAL stage-5 thread. Asserting the
    bare dispatcher's fields — not the projection in isolation — proves the per-step
    prompt-override resolution + governance path is reachable in production (the
    green-unit-but-unreachable mode #496/§14.5.2 acceptance guards)."""
    from harness_runtime.lifecycle.llm_dispatch import RuntimeLLMDispatcher

    _patch_providers(monkeypatch)
    _patch_collector(monkeypatch)

    store = PromptManifest.from_contents(
        manifest_version=1,
        contents=["step-A prompt", "step-B prompt", "default inline"],
        active="default inline",
    )
    approved = frozenset({prompt_version_sha("step-A prompt")})
    populated = _config(tmp_path).model_copy(
        update={"prompt_manifest": store, "approved_prompt_version_shas": approved},
    )
    ctx = await run_bootstrap(populated, workload_class=_WORKLOAD)

    bare = ctx.llm_dispatcher.inner.inner  # type: ignore[attr-defined]
    assert isinstance(bare, RuntimeLLMDispatcher)
    # The whole versions store reached the dispatcher as {sha: content} so a per-step
    # `prompt_version_sha` can resolve to content at dispatch (else fail-loud).
    assert bare.prompt_versions_by_sha == {
        prompt_version_sha("step-A prompt"): "step-A prompt",
        prompt_version_sha("step-B prompt"): "step-B prompt",
        prompt_version_sha("default inline"): "default inline",
    }
    # The operator-approved sha set reached the dispatcher (binding-tier governance).
    assert bare.approved_prompt_version_shas == approved


@pytest.mark.asyncio
async def test_bootstrap_selection_no_match_falls_through_to_inline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A selection manifest that binds neither the MVP-default role nor the run
    workload → fall-through to the standing inline active prompt (the #496/PR-#1
    behavior; selection only ADDS resolution, never gates dispatch)."""
    from harness_runtime.lifecycle.llm_dispatch import RuntimeLLMDispatcher

    _patch_providers(monkeypatch)
    _patch_collector(monkeypatch)

    store = PromptManifest.from_contents(
        manifest_version=1,
        contents=["other-workload prompt", "inline active"],
        active="inline active",
    )
    # Override is for a DIFFERENT workload than the run workload (_WORKLOAD = SE).
    selection = PromptSelectionManifest(
        manifest_version=1,
        per_workload_overrides={
            WorkloadClass.RESEARCH: PromptBinding(
                version_sha=prompt_version_sha("other-workload prompt")
            ),
        },
    )
    populated = _config(tmp_path).model_copy(
        update={"prompt_manifest": store, "prompt_selection_manifest": selection},
    )
    ctx = await run_bootstrap(populated, workload_class=_WORKLOAD)

    assert ctx.prompt_manifest.active_prompt_version.content == "inline active"
    bare = ctx.llm_dispatcher.inner.inner  # type: ignore[attr-defined]
    assert isinstance(bare, RuntimeLLMDispatcher)
    assert bare.active_system_prompt == "inline active"


@pytest.mark.asyncio
async def test_bootstrap_selection_unauthored_sha_fails_loud(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A selection binding to a `version_sha` not authored in the store is
    fail-loud (detect-then-refuse) — it surfaces as a `BootstrapFailure` whose
    cause is `PromptSelectionUnauthoredError`, never a silent fall-through."""
    _patch_providers(monkeypatch)
    _patch_collector(monkeypatch)

    store = PromptManifest.from_contents(
        manifest_version=1,
        contents=["only authored"],
        active=None,
    )
    selection = PromptSelectionManifest(
        manifest_version=1,
        per_workload_overrides={
            _WORKLOAD: PromptBinding(version_sha="cafebabe" * 8),  # not in the store
        },
    )
    populated = _config(tmp_path).model_copy(
        update={"prompt_manifest": store, "prompt_selection_manifest": selection},
    )
    with pytest.raises(BootstrapFailure) as excinfo:
        await run_bootstrap(populated, workload_class=_WORKLOAD)
    assert isinstance(excinfo.value.cause, PromptSelectionUnauthoredError)
    # Reconciliation runs at stage 0 PREAMBLE (before the first procedural-tier
    # snapshot at the stage-3b producer sites) so audit hashes stay coherent.
    assert excinfo.value.failed_stage is BootstrapStage.PREAMBLE


@pytest.mark.asyncio
async def test_bootstrap_binding_tier_unapproved_selection_fails_loud(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R-PM-1 cascade PR #4 — at a binding persona tier (team-binding), a selection
    that DRIVES an active prompt version whose sha is NOT operator-approved is
    fail-loud through the real bootstrap: `BootstrapFailure` (cause
    `PromptVersionUnapprovedError`) at stage 0 PREAMBLE — never a silent activation
    of an unapproved prompt at a binding tier (OD spec C-OD-34)."""
    _patch_providers(monkeypatch)
    _patch_collector(monkeypatch)

    store = PromptManifest.from_contents(
        manifest_version=1,
        contents=["governed prompt", "inline active"],
        active="inline active",
    )
    selection = PromptSelectionManifest(
        manifest_version=1,
        per_workload_overrides={
            _WORKLOAD: PromptBinding(version_sha=prompt_version_sha("governed prompt")),
        },
    )
    # team-binding requires approval; approved set is EMPTY → the driven sha is unapproved.
    populated = _config(tmp_path).model_copy(
        update={
            "prompt_manifest": store,
            "prompt_selection_manifest": selection,
            "persona_tier": PersonaTier.TEAM_BINDING,
        },
    )
    with pytest.raises(BootstrapFailure) as excinfo:
        await run_bootstrap(populated, workload_class=_WORKLOAD)
    assert isinstance(excinfo.value.cause, PromptVersionUnapprovedError)
    assert excinfo.value.failed_stage is BootstrapStage.PREAMBLE


@pytest.mark.asyncio
async def test_bootstrap_binding_tier_approved_selection_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R-PM-1 cascade PR #4 — at a binding tier, an APPROVED selection-driven version
    activates normally: the gate has teeth (it fails the unapproved case above) yet
    does not block a deployment that has attested the version (OD spec C-OD-34).
    Multi-tenant-compliance proves the redaction-bearing tier still activates once
    approved."""
    from harness_runtime.lifecycle.llm_dispatch import RuntimeLLMDispatcher

    _patch_providers(monkeypatch)
    _patch_collector(monkeypatch)

    store = PromptManifest.from_contents(
        manifest_version=1,
        contents=["governed prompt", "inline active"],
        active="inline active",
    )
    governed_sha = prompt_version_sha("governed prompt")
    selection = PromptSelectionManifest(
        manifest_version=1,
        per_workload_overrides={_WORKLOAD: PromptBinding(version_sha=governed_sha)},
    )
    populated = _config(tmp_path).model_copy(
        update={
            "prompt_manifest": store,
            "prompt_selection_manifest": selection,
            "persona_tier": PersonaTier.MULTI_TENANT_COMPLIANCE,
            "approved_prompt_version_shas": frozenset({governed_sha}),
        },
    )
    ctx = await run_bootstrap(populated, workload_class=_WORKLOAD)

    # Approved → selection activates as normal (the gate did not block it).
    assert ctx.prompt_manifest.active_prompt_version.content == "governed prompt"
    bare = ctx.llm_dispatcher.inner.inner  # type: ignore[attr-defined]
    assert isinstance(bare, RuntimeLLMDispatcher)
    assert bare.active_system_prompt == "governed prompt"


@pytest.mark.asyncio
async def test_bootstrap_writes_pidfile_at_stage_7(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """U-RT-48: stage 7 INGRESS_ACCEPT writes the pidfile per spec §13."""
    _patch_providers(monkeypatch)
    _patch_collector(monkeypatch)
    pidfile = tmp_path / ".harness/runtime.pid"
    assert not pidfile.exists()

    await run_bootstrap(_config(tmp_path), workload_class=_WORKLOAD)

    assert pidfile.is_file()
    assert pidfile.read_text().strip() == str(os.getpid())


@pytest.mark.asyncio
async def test_bootstrap_populates_every_required_harness_context_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_providers(monkeypatch)
    _patch_collector(monkeypatch)
    ctx = await run_bootstrap(_config(tmp_path), workload_class=_WORKLOAD)
    # Spot-check one field from each stage.
    assert ctx.config is not None  # stage 0
    assert ctx.path_resolver is not None  # stage 1
    assert ctx.sandbox_dispatch is not None  # stage 2
    assert ctx.mcp_host is not None  # stage 2 (U-RT-15)
    assert ctx.mcp_server is not None  # stage 2 (U-RT-62 — H_T-as-MCP-server)
    assert ctx.mcp_server.started is True  # U-RT-62 AC #2
    assert "anthropic" in ctx.providers  # stage 3a
    assert ctx.routing_manifest is not None  # stage 3b
    assert ctx.audit_writer is not None  # stage 4
    assert ctx.lifecycle_emitter is not None  # stage 5
    assert ctx.hitl_tool_loop is not None  # stage 5 (R-CXA-2)
    assert ctx.engine_recovery_loop is not None  # stage 5 (R-CXA-2)


@pytest.mark.asyncio
async def test_bootstrap_binds_cp_is_wiring_during_cp_routing_before_od(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_providers(monkeypatch)
    observed: dict[str, bool] = {}

    async def _fail_od(
        ctx: _MutableHarnessContext,
        config: RuntimeConfig,
        workload_class: WorkloadClass,
    ) -> None:
        _ = config, workload_class
        observed["cp_is_wiring"] = "cp_is_wiring" in ctx.cxa_stages
        observed["procedural_tier_snapshot_resolver"] = (
            ctx.procedural_tier_snapshot_resolver is not None
        )
        observed["audit_writer"] = ctx.audit_writer is not None
        raise RuntimeError("stop before OD materialization")

    monkeypatch.setattr(_stage_4_od_mod, "execute", _fail_od)

    with pytest.raises(BootstrapFailure) as exc_info:
        await run_bootstrap(_config(tmp_path), workload_class=_WORKLOAD)

    assert exc_info.value.failed_stage is BootstrapStage.OD
    assert observed == {
        "cp_is_wiring": True,
        "procedural_tier_snapshot_resolver": True,
        "audit_writer": False,
    }


@pytest.mark.asyncio
async def test_bootstrap_emits_engine_selection_state_ledger_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_providers(monkeypatch)
    _patch_collector(monkeypatch)

    ctx = await run_bootstrap(_config(tmp_path), workload_class=_WORKLOAD)

    selection_entries = [
        entry
        for entry in read_ledger(ctx.ledger_writer.handle)
        if str(entry.action_id) == _WORKLOAD_SELECTION_ACTION_ID
    ]
    assert len(selection_entries) == len(WorkloadClass) * len(PersonaTier)
    assert {str(entry.actor.actor_id) for entry in selection_entries} == {
        str(ctx.ledger_writer.actor.actor_id),
    }
    assert all(entry.procedural_tier_snapshot_ref is not None for entry in selection_entries)


@pytest.mark.asyncio
async def test_bootstrap_stage_5_binds_inference_and_sub_agent_dispatchers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """U-RT-59 AC #11 + U-RT-60 AC #13 (v1.11 wrap-asymmetry chain): stage 5
    binds both step kinds through the C-RT-18 wrap chain.

    Verifies the post-U-RT-60-wrap-asymmetry-fork-APPLIED stage 5 wiring per
    ``.harness/class_1_tension_u_rt_60_wrap_asymmetry_sync_async_mismatch.md``
    §7.2 Q1 materialized chain:

    Row 1 (INFERENCE_STEP):
        bare C-RT-15 → HITL(PRE_ACTION) → C-RT-16 retry → SyncDispatcherFacade

    Row 2 (SUB_AGENT_DISPATCH):
        bare C-RT-17 sub_agent_dispatcher → HITL(SUB_AGENT_BOUNDARY)
          → SyncDispatcherFacade

    AC #13 stage-5 post-condition:
    - ``ctx.step_dispatchers`` is a populated ``StepKindDispatcherRegistry``.
    - ``INFERENCE_STEP`` resolves to a ``SyncDispatcherFacade`` wrapping
      ``ctx.llm_dispatcher`` (the C-RT-16 wrapper) whose ``inner`` is
      a ``RuntimeHITLGateComposer`` with
      ``applicable_placements={PRE_ACTION}``.
    - ``SUB_AGENT_DISPATCH`` resolves to a ``SyncDispatcherFacade`` wrapping
      a ``RuntimeHITLGateComposer`` with
      ``applicable_placements={SUB_AGENT_BOUNDARY}`` whose ``inner`` is the
      bare ``RuntimeSubAgentDispatcher``.
    - ``ctx.sub_agent_dispatcher`` is the row-2 HITL composer (field type
      widened from sync ``_CpStepDispatcher`` to async ``Any`` per fork
      §7.2 Q3 ratification).
    - ``ctx.ask_user_question_surface`` is bound to a
      ``MCPBackedAskUserQuestionSurface`` per spec §14.8.3 v1.11 pin.
    - The 3 unbound step kinds (TOOL / HITL / DECLARATIVE) raise
      ``StepKindDispatcherNotBoundError`` on lookup per registry contract.
    """
    from harness_cp.hitl_placement import HITLPlacementKind
    from harness_cp.workflow_driver_types import StepKind
    from harness_runtime.lifecycle.engine_recovery_loop import RuntimeEngineRecoveryLoop
    from harness_runtime.lifecycle.hitl_gate_composer import (
        RuntimeHITLGateComposer,
    )
    from harness_runtime.lifecycle.hitl_tool_loop import RuntimeHITLToolLoop
    from harness_runtime.lifecycle.mcp_backed_ask_user_question_surface import (
        MCPBackedAskUserQuestionSurface,
    )
    from harness_runtime.lifecycle.step_dispatchers import (
        StepKindDispatcherNotBoundError,
    )
    from harness_runtime.lifecycle.sub_agent_dispatch import (
        RuntimeSubAgentDispatcher,
    )
    from harness_runtime.lifecycle.sync_dispatcher_facade import (
        SyncDispatcherFacade,
    )

    _patch_providers(monkeypatch)
    _patch_collector(monkeypatch)
    ctx = await run_bootstrap(_config(tmp_path), workload_class=_WORKLOAD)

    assert ctx.step_dispatchers is not None

    # AC #13: ask_user_question_surface bound to MCP-backed concrete impl.
    assert isinstance(ctx.ask_user_question_surface, MCPBackedAskUserQuestionSurface)

    # AC #13 row 1: SyncDispatcherFacade(C-RT-16(HITL(PRE_ACTION)(bare C-RT-15)))
    inference_dispatcher = ctx.step_dispatchers.lookup(StepKind.INFERENCE_STEP)
    assert isinstance(inference_dispatcher, SyncDispatcherFacade)
    assert inference_dispatcher.inner is ctx.llm_dispatcher
    # Per spec v1.31 §3, facade reads config.step_dispatch_timeout_seconds
    # (was config.drain_timeout_seconds pre-v1.31; conflated per-step ↔
    # whole-workflow bound resolved at fork doc
    # class_1_fork_step_dispatch_timeout_seconds_field_extension.md).
    assert inference_dispatcher.result_timeout_seconds == ctx.config.step_dispatch_timeout_seconds
    # ctx.llm_dispatcher.inner is the PRE_ACTION HITL composer per the wrap chain
    hitl_inference = ctx.llm_dispatcher.inner  # type: ignore[attr-defined]
    assert isinstance(hitl_inference, RuntimeHITLGateComposer)
    assert hitl_inference.applicable_placements == frozenset({HITLPlacementKind.PRE_ACTION})

    # AC #13 row 2: SyncDispatcherFacade(HITL(SUB_AGENT_BOUNDARY)(bare sub_agent))
    sub_agent_step = ctx.step_dispatchers.lookup(StepKind.SUB_AGENT_DISPATCH)
    assert isinstance(sub_agent_step, SyncDispatcherFacade)
    # Field-type-widened ctx.sub_agent_dispatcher is the HITL composer (not
    # the bare sub-agent dispatcher) per fork §7.2 Q3 ratification.
    assert isinstance(ctx.sub_agent_dispatcher, RuntimeHITLGateComposer)
    assert ctx.sub_agent_dispatcher.applicable_placements == frozenset(
        {HITLPlacementKind.SUB_AGENT_BOUNDARY}
    )
    assert isinstance(ctx.sub_agent_dispatcher.inner, RuntimeSubAgentDispatcher)
    assert sub_agent_step.inner is ctx.sub_agent_dispatcher

    # TOOL_STEP bound at U-RT-68 cluster-close per spec v1.16 §14.9.3 +
    # §14.11 C-RT-21 RetryBreakerToolDispatcher wrap. HITL / DECLARATIVE
    # remain unbound (follow-on composer arcs).
    #
    # R-FS-1 `B-TOOL-GATE` (CP spec v1.35 §19.1.2 Producer ¶) — the TOOL_STEP
    # registry path now wraps the tool dispatcher in a third RuntimeHITLGateComposer
    # (the tool-step MCP-trust gate site): `facade → hitl_tool composer → ctx.tool_dispatcher`.
    # The composer carries the `mcp_trust_tier_resolver` (the resolved-owning-host
    # feed) and gates on PRE_ACTION, so an L0-trust server's tool floors its gate to
    # DENY. `ctx.tool_dispatcher` itself is NOT mutated (the R-CXA-2 producer loop +
    # the provider-turn tool loop still read the un-gated dispatcher).
    tool_step = ctx.step_dispatchers.lookup(StepKind.TOOL_STEP)
    assert isinstance(tool_step, SyncDispatcherFacade)
    assert isinstance(tool_step.inner, RuntimeHITLGateComposer)
    assert tool_step.inner.inner is ctx.tool_dispatcher
    assert tool_step.inner.applicable_placements == frozenset({HITLPlacementKind.PRE_ACTION})
    assert tool_step.inner.mcp_trust_tier_resolver is not None
    assert isinstance(ctx.hitl_tool_loop, RuntimeHITLToolLoop)
    assert isinstance(ctx.engine_recovery_loop, RuntimeEngineRecoveryLoop)
    for unbound in (StepKind.HITL_STEP, StepKind.DECLARATIVE_STEP):
        with pytest.raises(StepKindDispatcherNotBoundError):
            ctx.step_dispatchers.lookup(unbound)


# ---------------------------------------------------------------------------
# AC #3 — Each stage emits exactly one lifecycle event (9 total).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_emits_nine_lifecycle_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_providers(monkeypatch)
    _patch_collector(monkeypatch)
    ctx = await run_bootstrap(_config(tmp_path), workload_class=_WORKLOAD)
    emitted = ctx.lifecycle_emitter.emitted_bootstrap_stages  # type: ignore[attr-defined]
    assert len(emitted) == 9


@pytest.mark.asyncio
async def test_bootstrap_emits_events_in_canonical_stage_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_providers(monkeypatch)
    _patch_collector(monkeypatch)
    ctx = await run_bootstrap(_config(tmp_path), workload_class=_WORKLOAD)
    emitted = ctx.lifecycle_emitter.emitted_bootstrap_stages  # type: ignore[attr-defined]
    assert list(emitted) == list(BootstrapStage)


# ---------------------------------------------------------------------------
# AC #2 — Rollback at each stage failure.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failure_at_stage_0_raises_bootstrap_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("stage 0 boom")

    monkeypatch.setattr(
        "harness_runtime.bootstrap.stage_0_preamble.execute",
        _boom,
    )
    with pytest.raises(BootstrapFailure) as excinfo:
        await run_bootstrap(_config(tmp_path), workload_class=_WORKLOAD)
    assert excinfo.value.failed_stage is BootstrapStage.PREAMBLE
    assert isinstance(excinfo.value.cause, RuntimeError)


@pytest.mark.asyncio
async def test_failure_at_stage_1_rolls_back_stage_0(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stage 1 failure → rollback handler for stage 0 invoked."""
    rollback_calls: list[BootstrapStage] = []

    from harness_runtime.bootstrap import _rollback_preamble  # type: ignore[attr-defined]

    async def _rec_rollback_preamble(ctx: Any) -> None:
        rollback_calls.append(BootstrapStage.PREAMBLE)
        await _rollback_preamble(ctx)

    async def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("stage 1 boom")

    import harness_runtime.bootstrap as _boot

    monkeypatch.setattr(
        "harness_runtime.bootstrap._ROLLBACK_HANDLERS",
        {**_boot._ROLLBACK_HANDLERS, BootstrapStage.PREAMBLE: _rec_rollback_preamble},
    )
    monkeypatch.setattr("harness_runtime.bootstrap.stage_1_is.execute", _boom)
    with pytest.raises(BootstrapFailure):
        await run_bootstrap(_config(tmp_path), workload_class=_WORKLOAD)
    assert rollback_calls == [BootstrapStage.PREAMBLE]


@pytest.mark.asyncio
async def test_failure_at_stage_3a_closes_already_constructed_providers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stage 3a failure after partial provider construction → close those providers."""
    # Construct fakes via patch; then arrange stage 3b to fail; rollback should
    # close all providers since their stage completed before the failure.
    fakes = _patch_providers(monkeypatch)
    _patch_collector(monkeypatch)

    async def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("stage 3b boom")

    monkeypatch.setattr(
        "harness_runtime.bootstrap.stage_3b_cp_routing.execute",
        _boom,
    )
    with pytest.raises(BootstrapFailure) as excinfo:
        await run_bootstrap(_config(tmp_path), workload_class=_WORKLOAD)
    assert excinfo.value.failed_stage is BootstrapStage.CP_ROUTING
    # All 3 providers closed (best-effort rollback).
    assert all(p.closed for p in fakes.values())


@pytest.mark.asyncio
async def test_rollback_cp_clients_drains_started_mcp_hosts() -> None:
    """U-RT-126/127 — a post-stage-3a abort (e.g. a stage-5
    RT-FAIL-MCP-TOOL-NAME-COLLISION) drains the MCP hosts stage 3a started, not
    just providers — no leaked subprocess/session. Unstarted hosts are skipped."""
    from types import SimpleNamespace

    from harness_runtime.bootstrap import _rollback_cp_clients

    class _FakeHost:
        def __init__(self, *, started: bool) -> None:
            self.started = started
            self.shutdown_calls = 0

        async def shutdown(self) -> None:
            self.shutdown_calls += 1

    started = _FakeHost(started=True)
    unstarted = _FakeHost(started=False)
    ctx = SimpleNamespace(
        mcp_client_hosts={"started-server": started, "unstarted-server": unstarted},
        providers=None,
    )
    await _rollback_cp_clients(ctx)  # type: ignore[arg-type]
    assert started.shutdown_calls == 1  # started host drained
    assert unstarted.shutdown_calls == 0  # unstarted host skipped


@pytest.mark.asyncio
async def test_failure_at_stage_5_stops_collector_daemon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stage 5 failure → stage 4 rollback stops the daemon."""
    _patch_providers(monkeypatch)
    daemon = _patch_collector(monkeypatch)

    async def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("stage 5 boom")

    monkeypatch.setattr(
        "harness_runtime.bootstrap.stage_5_loop_init.execute",
        _boom,
    )
    with pytest.raises(BootstrapFailure) as excinfo:
        await run_bootstrap(_config(tmp_path), workload_class=_WORKLOAD)
    assert excinfo.value.failed_stage is BootstrapStage.LOOP_INIT
    assert daemon.stopped is True


@pytest.mark.asyncio
async def test_stage_0_failure_skips_rollback_entirely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If stage 0 fails, no stage completed → no rollback handlers called."""
    rollback_calls: list[BootstrapStage] = []

    import harness_runtime.bootstrap as boot

    original_handlers = boot._ROLLBACK_HANDLERS  # type: ignore[attr-defined]

    async def _record(stage: BootstrapStage, ctx: Any) -> None:
        rollback_calls.append(stage)

    monkeypatch.setattr(
        boot,
        "_ROLLBACK_HANDLERS",
        {s: (lambda c, _s=s: _record(_s, c)) for s in original_handlers},
    )

    async def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("stage 0 boom")

    monkeypatch.setattr("harness_runtime.bootstrap.stage_0_preamble.execute", _boom)
    with pytest.raises(BootstrapFailure):
        await run_bootstrap(_config(tmp_path), workload_class=_WORKLOAD)
    assert rollback_calls == []


@pytest.mark.asyncio
async def test_rollback_continues_when_handler_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising rollback handler does not halt later handlers in the reverse order."""
    _patch_providers(monkeypatch)
    _patch_collector(monkeypatch)

    handler_calls: list[BootstrapStage] = []

    import harness_runtime.bootstrap as boot

    async def _failing(ctx: Any) -> None:
        handler_calls.append(BootstrapStage.AS)
        raise RuntimeError("rollback handler failure")

    async def _ok_is(ctx: Any) -> None:
        handler_calls.append(BootstrapStage.IS)

    async def _ok_preamble(ctx: Any) -> None:
        handler_calls.append(BootstrapStage.PREAMBLE)

    patched = dict(boot._ROLLBACK_HANDLERS)  # type: ignore[attr-defined]
    patched[BootstrapStage.PREAMBLE] = _ok_preamble
    patched[BootstrapStage.IS] = _ok_is
    patched[BootstrapStage.AS] = _failing
    monkeypatch.setattr(boot, "_ROLLBACK_HANDLERS", patched)

    async def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("stage 3a boom")

    monkeypatch.setattr(
        "harness_runtime.bootstrap.stage_3a_cp_clients.execute",
        _boom,
    )
    with pytest.raises(BootstrapFailure):
        await run_bootstrap(_config(tmp_path), workload_class=_WORKLOAD)
    # Order is reverse: AS (raises) → IS → PREAMBLE.
    assert handler_calls == [
        BootstrapStage.AS,
        BootstrapStage.IS,
        BootstrapStage.PREAMBLE,
    ]


# ---------------------------------------------------------------------------
# _MutableHarnessContext + freeze.
# ---------------------------------------------------------------------------


def test_freeze_raises_incomplete_when_required_field_none() -> None:
    ctx = _MutableHarnessContext()
    with pytest.raises(IncompleteBootstrapError) as excinfo:
        ctx.freeze()
    # All 41 required fields are missing (U-RT-52 +1 for `llm_dispatcher`;
    # U-RT-59 +2 for `sub_agent_dispatcher` + `step_dispatchers`;
    # U-RT-60 +1 for `ask_user_question_surface`; U-RT-72 +4 for
    # `mcp_client_hosts` + `tool_dispatcher` + `per_server_trust_evaluator`
    # + `mcp_namespace_emitter` per spec v1.16 §4 C-RT-04 extension
    # (mcp_client_host reshaped singular→dict at U-RT-125 / spec v1.51);
    # U-RT-80 +1 for `memory_tool_registry` per spec v1.17 §4 C-RT-04 +
    # §14.12 C-RT-22 extension).
    assert "config" in excinfo.value.missing_fields
    assert "lifecycle_emitter" in excinfo.value.missing_fields
    assert "ledger_reader" in excinfo.value.missing_fields
    assert "llm_dispatcher" in excinfo.value.missing_fields
    assert "sub_agent_dispatcher" in excinfo.value.missing_fields
    assert "step_dispatchers" in excinfo.value.missing_fields
    assert "ask_user_question_surface" in excinfo.value.missing_fields
    assert "mcp_client_hosts" in excinfo.value.missing_fields
    assert "tool_dispatcher" in excinfo.value.missing_fields
    assert "hitl_tool_loop" in excinfo.value.missing_fields
    assert "engine_recovery_loop" in excinfo.value.missing_fields
    assert "per_server_trust_evaluator" in excinfo.value.missing_fields
    assert "mcp_namespace_emitter" in excinfo.value.missing_fields
    assert "memory_tool_registry" in excinfo.value.missing_fields
    assert "pause_requested_flag" in excinfo.value.missing_fields
    assert "resume_context_holder" in excinfo.value.missing_fields
    assert len(excinfo.value.missing_fields) == 41


def test_bootstrap_stage_complete_event_is_frozen() -> None:
    event = BootstrapStageCompleteEvent(stage=BootstrapStage.PREAMBLE)
    with pytest.raises(Exception):
        event.stage = BootstrapStage.IS  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Lifecycle-event buffering discipline.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_events_for_stages_0_to_4_buffered_until_stage_5(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stages 0-4 complete before the emitter exists; events buffer."""
    _patch_providers(monkeypatch)
    _patch_collector(monkeypatch)

    # Stop bootstrap right after stage 4 by injecting a stage-5 failure;
    # capture the pending buffer state via the orchestrator's internal stash.
    # We intercept via a stage_5 wrapper that records ctx state before failing.
    captured: dict[str, Any] = {}

    async def _capture_then_fail(ctx: Any, config: Any, wc: Any) -> None:
        captured["stages_completed_pre_emit"] = list(ctx.completed_stages)
        captured["emitter_present"] = ctx.lifecycle_emitter is not None
        raise RuntimeError("stop here")

    monkeypatch.setattr(
        "harness_runtime.bootstrap.stage_5_loop_init.execute",
        _capture_then_fail,
    )
    with pytest.raises(BootstrapFailure):
        await run_bootstrap(_config(tmp_path), workload_class=_WORKLOAD)
    # At the moment LOOP_INIT (BootstrapStage value 6) began, stages 0-5
    # (PREAMBLE..OD = 6 stages) had completed but the emitter did not yet
    # exist (LOOP_INIT had not populated it).
    assert captured["emitter_present"] is False
    assert len(captured["stages_completed_pre_emit"]) == 6


@pytest.mark.asyncio
async def test_buffered_events_flush_in_arrival_order_at_stage_5(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When LOOP_INIT (stage 6) completes, buffered events 0..6 flush in order."""
    _patch_providers(monkeypatch)
    _patch_collector(monkeypatch)
    ctx = await run_bootstrap(_config(tmp_path), workload_class=_WORKLOAD)
    emitted = ctx.lifecycle_emitter.emitted_bootstrap_stages  # type: ignore[attr-defined]
    # The first 7 events were buffered through LOOP_INIT's completion (6 buffered
    # while emitter was None + LOOP_INIT itself); they flush in arrival order.
    assert list(emitted[:7]) == [
        BootstrapStage.PREAMBLE,
        BootstrapStage.IS,
        BootstrapStage.AS,
        BootstrapStage.CP_CLIENTS,
        BootstrapStage.CP_ROUTING,
        BootstrapStage.OD,
        BootstrapStage.LOOP_INIT,
    ]


# ---------------------------------------------------------------------------
# api.run integration.
# ---------------------------------------------------------------------------


class _Workflow:
    """Structural `WorkflowObject` carrying the full Lane 6 property set."""

    def __init__(
        self,
        workflow_id: str = "wf-bootstrap-test",
        workload_class: WorkloadClass = _WORKLOAD,
    ) -> None:
        from harness_core.identity import StepID
        from harness_core.persona_tier import PersonaTier
        from harness_cp.cp_shared_types import ModelBinding
        from harness_cp.engine_class import EngineClass
        from harness_cp.workflow_driver_types import StepKind, WorkflowStep
        from harness_cp.workflow_manifest_entry import WorkflowManifestEntry

        self._wid = workflow_id
        self._wc = workload_class
        self._manifest = WorkflowManifestEntry(
            workflow_id=workflow_id,
            workload_class=workload_class,
            persona_tier=PersonaTier.TEAM_BINDING,
            engine_class=EngineClass.PURE_PATTERN_NO_ENGINE,
            topology_pattern=TopologyPattern.SINGLE_THREADED_LINEAR,
            layer_budgets=(),
            fallback_chain=_CHAIN,
            hitl_placements=(),
            per_step_overrides={},
        )
        self._steps = (
            WorkflowStep(
                step_id=StepID("step-0"),
                step_kind=StepKind.INFERENCE_STEP,
                step_payload={},
            ),
        )
        self._binding = ModelBinding(provider="anthropic", model="claude-haiku-4-5")

        class _Noop:
            def dispatch(self, binding: object, step: object) -> dict[str, object]:
                _ = binding, step
                return {}

        self._dispatcher = _Noop()

    @property
    def workflow_id(self) -> str:
        return self._wid

    @property
    def workload_class(self) -> WorkloadClass:
        return self._wc

    @property
    def manifest_entry(self) -> Any:
        return self._manifest

    @property
    def steps(self) -> Any:
        return self._steps

    @property
    def step_dispatcher(self) -> Any:
        return self._dispatcher

    @property
    def step_dispatchers(self) -> Any:
        # U-RT-59 (C-RT-17 §14.7.7): workflow-supplied registry overrides
        # ctx.step_dispatchers per the api.py override path. v1.6 MVP test
        # registry binds INFERENCE_STEP → workflow's noop dispatcher so the
        # test step routes through the driver.
        from harness_cp.workflow_driver_types import StepKind

        class _Reg:
            def __init__(self, kind: Any, disp: Any) -> None:
                self._kind = kind
                self._disp = disp

            def lookup(self, step_kind: Any) -> Any:
                return self._disp

        return _Reg(StepKind.INFERENCE_STEP, self._dispatcher)

    @property
    def default_model_binding(self) -> Any:
        return self._binding


@pytest.mark.asyncio
async def test_api_run_passes_workload_class_into_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`run(workflow)` extracts `workflow.workload_class` and threads to bootstrap."""
    captured: dict[str, Any] = {}

    async def _fake_bootstrap(
        config: Any, *, workload_class: Any, requires_inference: bool = True
    ) -> None:
        captured["workload_class"] = workload_class
        captured["requires_inference"] = requires_inference
        return None

    monkeypatch.setattr("harness_runtime.bootstrap.run_bootstrap", _fake_bootstrap)
    monkeypatch.setattr(
        "harness_runtime.api._default_config",
        lambda: _config(tmp_path),
    )
    # Short-circuit the driver + shutdown — this test only verifies that
    # `run()` threads `workflow.workload_class` into bootstrap. Real
    # bootstrap-to-shutdown end-to-end is exercised in test_run_smoke.py.
    # U-RT-62 AC #5 — `api.run()` now delegates execution via the in-
    # process MCP tool path; the fake bootstrap must carry a `mcp_server`
    # namespace + the stub site moves from `asyncio.to_thread` to
    # `_invoke_run_workflow_via_in_process_mcp` per the v1.12 internal
    # layout.
    import sys
    from types import SimpleNamespace

    _shutdown_mod = sys.modules["harness_runtime.shutdown"]
    from harness_runtime import api as _api_mod
    from harness_runtime.api import run

    # Wrap the existing `_fake_bootstrap` to return a ctx carrying
    # `mcp_server` (the original captured the workload_class via closure;
    # we preserve that behavior by re-using the same captured dict).
    _original_fake_bootstrap = _fake_bootstrap

    async def _wrapped_fake_bootstrap(
        config: Any, *, workload_class: Any, requires_inference: bool = True
    ) -> Any:
        await _original_fake_bootstrap(
            config, workload_class=workload_class, requires_inference=requires_inference
        )
        return SimpleNamespace(
            mcp_server=SimpleNamespace(
                server=object(),
                _state={},
                workflow_registry={},
            ),
            cost_record_accumulator=CostRecordAccumulator(),
        )

    monkeypatch.setattr("harness_runtime.bootstrap.run_bootstrap", _wrapped_fake_bootstrap)

    async def _fake_invoke(fastmcp_server: Any, workflow_id: str) -> Any:
        _ = fastmcp_server, workflow_id
        from harness_cp.workflow_driver_types import (
            RunResult as _CpRR,
        )
        from harness_cp.workflow_driver_types import (
            RunStatus as _CpRS,
        )

        return _CpRR(
            workflow_id="wf-bootstrap-test",
            run_id="run-fake",
            status=_CpRS.SUCCESS,
            final_state={},
        )

    async def _fake_shutdown(ctx: Any, *, timeout: float = 5.0) -> Any:
        _ = ctx, timeout
        return _shutdown_mod.ShutdownReport(
            already_shutdown=False,
            timed_out=False,
            flush=_shutdown_mod.FlushReport(
                tracer_flushed=True,
                ledger_fsynced=True,
                cost_chain_noop=True,
                timed_out=False,
                failures=(),
            ),
            failures=(),
            audit_ledger_head_hash=None,
        )

    monkeypatch.setattr(_api_mod, "_invoke_run_workflow_via_in_process_mcp", _fake_invoke)
    monkeypatch.setattr(_shutdown_mod, "shutdown", _fake_shutdown)

    result = await run(_Workflow(workload_class=WorkloadClass.RESEARCH))
    assert result.status == "completed"
    assert captured["workload_class"] is WorkloadClass.RESEARCH


@pytest.mark.asyncio
async def test_api_run_propagates_bootstrap_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing `run_bootstrap` surfaces `BootstrapFailure` through `run()`."""

    async def _fake_bootstrap(*_a: object, **_k: object) -> None:
        raise BootstrapFailure(
            failed_stage=BootstrapStage.OD,
            cause=RuntimeError("synthetic"),
        )

    monkeypatch.setattr("harness_runtime.bootstrap.run_bootstrap", _fake_bootstrap)
    monkeypatch.setattr(
        "harness_runtime.api._default_config",
        lambda: _config(tmp_path),
    )
    from harness_runtime.api import run

    with pytest.raises(BootstrapFailure):
        await run(_Workflow())


# ---------------------------------------------------------------------------
# WorkflowObject Protocol growth.
# ---------------------------------------------------------------------------


def test_workflow_object_protocol_requires_workload_class() -> None:
    """A workflow missing `workload_class` fails the structural check."""
    from harness_runtime.api import WorkflowObject

    class _Half:
        @property
        def workflow_id(self) -> str:
            return "x"

    assert not isinstance(_Half(), WorkflowObject)
    assert isinstance(_Workflow(), WorkflowObject)


# ---------------------------------------------------------------------------
# Smoke — orchestrator records emitted events on the mutable context.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_stages_complete_in_canonical_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`completed_stages` post-bootstrap matches `list(BootstrapStage)`."""
    _patch_providers(monkeypatch)
    _patch_collector(monkeypatch)
    # Pre-build the orchestrator's ctx via a wrapper around stage_7 so we can
    # snapshot it before it gets frozen out of reach.
    snapshot: dict[str, Any] = {}

    from harness_runtime.bootstrap import stage_7_ingress as _stage_7

    original = _stage_7.execute

    async def _wrap(ctx: Any, config: Any, wc: Any) -> None:
        snapshot["completed_stages"] = list(ctx.completed_stages)
        await original(ctx, config, wc)

    monkeypatch.setattr(_stage_7, "execute", _wrap)
    await run_bootstrap(_config(tmp_path), workload_class=_WORKLOAD)
    # At INGRESS_ACCEPT (stage 8) entry, stages 0-7 (PREAMBLE..CXA_WIRING =
    # 8 stages) have completed; INGRESS_ACCEPT itself has not yet appended.
    assert snapshot["completed_stages"] == list(BootstrapStage)[:8]


_ = Awaitable, Callable  # silence unused-import; reserved for future helper types


# ---------------------------------------------------------------------------
# R-CC-1 arc #4 (runtime spec v1.47 §2.1) — inference-conditional provider
# materialization at the full-bootstrap level.
# ---------------------------------------------------------------------------


def _patch_providers_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace stage-3a provider construction with a provider-free result
    (empty `ctx.providers`), simulating a tool-only / non-inference bootstrap."""

    async def _none(*_args: object, **_kwargs: object) -> ProviderClientsStage:
        return ProviderClientsStage(providers={})

    monkeypatch.setattr(
        "harness_runtime.bootstrap.stage_3a_cp_clients.materialize_provider_clients_stage",
        _none,
    )


@pytest.mark.asyncio
async def test_bootstrap_non_inference_omits_inference_dispatcher_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime spec v1.47 §2.1 — a non-inference (tool-only) bootstrap
    (`requires_inference=False`) with NO providers OMITS the INFERENCE_STEP /
    SUB_AGENT_DISPATCH step-dispatcher rows (fail-loud
    `StepKindDispatcherNotBoundError` backstop) while TOOL_STEP stays bound.
    `ctx.providers` is empty (provider-free bootstrap)."""
    from harness_cp.workflow_driver_types import StepKind
    from harness_runtime.lifecycle.step_dispatchers import StepKindDispatcherNotBoundError

    # No provider patch — stage 3a SKIPS provider construction entirely for a
    # non-inference workflow (v1.47 §2.1), so the real skip path is exercised
    # (the default `_config` does not mark providers optional; the skip is what
    # makes this succeed provider-free).
    _patch_collector(monkeypatch)

    ctx = await run_bootstrap(_config(tmp_path), workload_class=_WORKLOAD, requires_inference=False)

    assert ctx.providers == {}
    # TOOL_STEP bound; INFERENCE_STEP + SUB_AGENT_DISPATCH omitted → lookup raises.
    assert ctx.step_dispatchers.lookup(StepKind.TOOL_STEP) is not None
    with pytest.raises(StepKindDispatcherNotBoundError):
        ctx.step_dispatchers.lookup(StepKind.INFERENCE_STEP)
    with pytest.raises(StepKindDispatcherNotBoundError):
        ctx.step_dispatchers.lookup(StepKind.SUB_AGENT_DISPATCH)


@pytest.mark.asyncio
async def test_bootstrap_inference_with_no_providers_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C9 preservation (runtime spec v1.47 §2.1) — an inference-bearing
    bootstrap (`requires_inference=True`) with NO providers FAILS; it never
    silently proceeds provider-less. The contrasting baseline to the tool-only
    provider-free success above."""
    _patch_providers_empty(monkeypatch)
    _patch_collector(monkeypatch)

    with pytest.raises(BootstrapFailure):
        await run_bootstrap(_config(tmp_path), workload_class=_WORKLOAD, requires_inference=True)


# ---------------------------------------------------------------------------
# U-RT-116 (G1-skip; §3.8 / F-B3-1) AC-1 wiring — the smart-HITL skip's
# production binding chain (config → stage-5 → composer instance state) is
# verified THROUGH THE REAL run_bootstrap path, not just grep'd from the diff.
# Closes the stage-factory→composer link AC-1 names ("MUST NOT go live before
# verified wired") — composes with the by-execution skip+audit composer test
# at test_lifecycle_hitl_gate_composer.py. [[verification-shape-sharpened-grep-vs-e2e]]
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_threads_hitl_auto_approve_policy_onto_both_composers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator's `config.hitl_auto_approve_policy` reaches BOTH stage-5 HITL
    composers as instance state, and the per-step blast-radius resolver is bound
    to the real ctx (resolves through the closure) — the production wiring that
    makes the §3.8 skip go live."""
    from harness_as import BlastRadiusTier
    from harness_cp.workflow_driver_types import StepKind, WorkflowStep
    from harness_runtime.lifecycle.hitl_auto_approve_policy import HITLAutoApprovePolicy

    _patch_providers(monkeypatch)
    _patch_collector(monkeypatch)

    # Non-default policy → proves the config VALUE threads (not just the default).
    policy = HITLAutoApprovePolicy(solo_local_mutation_floor_auto=True)
    populated = _config(tmp_path).model_copy(update={"hitl_auto_approve_policy": policy})
    ctx = await run_bootstrap(populated, workload_class=_WORKLOAD)

    # Wrap chain: ctx.llm_dispatcher (C-RT-16 retry) → .inner (PRE_ACTION HITL
    # composer). ctx.sub_agent_dispatcher IS the SUB_AGENT_BOUNDARY HITL composer.
    pre_action = ctx.llm_dispatcher.inner  # type: ignore[attr-defined]
    sub_agent = ctx.sub_agent_dispatcher

    inference_step = WorkflowStep(
        step_id="bootstrap-wiring-probe",
        step_kind=StepKind.INFERENCE_STEP,
        step_payload={},
    )
    for composer in (pre_action, sub_agent):
        # (a) the config-supplied policy is held as composer instance state.
        assert composer.hitl_auto_approve_policy == policy  # type: ignore[attr-defined]
        # (b) the blast-radius resolver is bound + resolves through the real ctx
        # closure (not None / not a dead closure).
        resolver = composer.blast_radius_resolver  # type: ignore[attr-defined]
        assert resolver is not None
        assert resolver(inference_step) is BlastRadiusTier.READ_ONLY

    # Default config → default policy threads (READ_ONLY auto-ON / LOCAL_MUTATION
    # opt-in OFF) — the no-override baseline.
    ctx_default = await run_bootstrap(_config(tmp_path), workload_class=_WORKLOAD)
    default_policy = ctx_default.llm_dispatcher.inner.hitl_auto_approve_policy  # type: ignore[attr-defined]
    assert default_policy.solo_persona_floor_auto is True
    assert default_policy.solo_local_mutation_floor_auto is False
