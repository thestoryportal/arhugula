"""R-300-multi-llm-second-provider (B-2) — cross-family fallback exercise.

Roadmap forward-register B-2 (`.harness/post-phase-8-forward-register.md`):
"Multi-provider credentials + mixed-provider exercise — only Anthropic exercised
at R-100; needs OpenAI/Ollama creds + fixture." This module supplies the missing
*mixed-provider* exercise of the production cross-family fallback path.

Production path under test (grounded 2026-06-03):
  ``api.run`` -> ``workflow_driver`` step dispatch -> ``ctx.llm_dispatcher``
  (= ``RetryBreakerFallbackDispatcher``, C-RT-16 / U-RT-58, bound at
  ``stage_5_loop_init.py:279`` to ``ctx.fallback_chain`` =
  ``config.routing_manifest.fallback_chains[0]``). On a primary-provider failure
  the composer walks the C-CP-04 §4.2 traversal order
  (primary -> same-family -> cross-family -> terminal) via ``advance_or_raise``,
  rebinding the per-candidate ``(provider, model)``. The bare inner C-RT-15
  dispatcher's GenAI-semconv span carries ``gen_ai.provider.name`` per OD spec
  v1.19 §1.1 — the discriminating observable that proves *which* provider
  answered (fallback-fired vs. anthropic-just-worked).

Two tests:

  - ``test_r300_deterministic_cross_family_fallback_through_production_path``
    (CI-green, no creds): fakes ONLY the SDK leaf clients (anthropic raises ->
    openai returns a canned success) at the stage-3a
    ``materialize_provider_clients_stage`` seam — keeping chain / classification /
    advance / bootstrap / dispatcher REAL — and proves the cross-family fallback
    *plumbing* end-to-end through ``api.run``. Faking provider *leaves* (not the
    dispatcher/host wiring under test) is standard, NOT the
    ``test-bypass-as-runtime-truth`` anti-pattern.

  - ``test_r300_live_cross_family_fallback_against_real_providers`` (skipif on
    ANTHROPIC_API_KEY + OPENAI_API_KEY): the real-provider confirmation — primary
    anthropic invalid-model (real 404 -> transient -> retry-exhaust -> advance) ->
    real openai. Run via ``just mvp-r300-cross-family`` (operator-authorized paid
    run; ~1 cheap openai call + 3 unbilled anthropic 404s).
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from harness_runtime.api import RunResult
from harness_runtime.bootstrap import stage_4_od as _stage_4_od_mod
from harness_runtime.types import (
    CollectorConfig,
    OTelConfig,
    PathBindingConfig,
    ProviderSecretsConfig,
    RuntimeConfig,
)
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

# An anthropic model that does not exist -> the real Messages API returns a 404
# `not_found_error` (unbilled). Used only by the live test; the deterministic
# test fakes the leaf client and ignores the model string.
_INVALID_ANTHROPIC_MODEL = "claude-nonexistent-model-r300-fallback-probe"
_OPENAI_FALLBACK_MODEL = "gpt-4o-mini"


# ---------------------------------------------------------------------------
# Shared config + workflow (mirror of test_r100_real_workflow_e2e).
# ---------------------------------------------------------------------------


def _build_chain(primary_model: str) -> Any:
    """Cross-family chain: primary anthropic -> cross-family openai."""
    from harness_cp.cross_family_fallback_chain import (
        FallbackChain,
        ProviderCandidate,
        ProviderFamily,
    )

    return FallbackChain(
        primary=ProviderCandidate(
            provider="anthropic",
            model=primary_model,
            family=ProviderFamily.ANTHROPIC,
        ),
        same_family=(),
        cross_family=(
            ProviderCandidate(
                provider="openai",
                model=_OPENAI_FALLBACK_MODEL,
                family=ProviderFamily.OPENAI,
            ),
        ),
        terminal=None,
    )


def _build_config(
    tmp_path: Path,
    chain: Any,
    *,
    anthropic_optional: bool = False,
    openai_optional: bool = True,
    ollama_optional: bool = True,
) -> RuntimeConfig:
    from harness_core.deployment_surface import DeploymentSurface
    from harness_core.workload_class import WorkloadClass
    from harness_cp.routing_manifest_residence import RoutingManifest
    from harness_cp.topology_pattern import TopologyPattern
    from harness_is.path_class_registry import PathClass

    surface = DeploymentSurface.LOCAL_DEVELOPMENT
    workload = WorkloadClass.SOFTWARE_ENGINEERING
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
    return RuntimeConfig(
        deployment_surface=surface,
        repository_root=tmp_path,
        path_bindings=path_bindings,
        provider_secrets=ProviderSecretsConfig(),
        otel=OTelConfig(otlp_endpoint="http://localhost:4318"),
        collector=CollectorConfig(),
        default_topology=TopologyPattern.SINGLE_THREADED_LINEAR,
        mcp_clients=[],
        anthropic_optional=anthropic_optional,
        openai_optional=openai_optional,
        ollama_optional=ollama_optional,
        routing_manifest=RoutingManifest(
            manifest_version=1,
            per_role_bindings={},
            per_workload_overrides={},
            fallback_chains=(chain,),
            retry_policies={},
        ),
    )


def _make_workflow(chain: Any, *, params: dict[str, Any] | None = None) -> Any:
    from harness_core.identity import StepID
    from harness_core.persona_tier import PersonaTier
    from harness_core.workload_class import WorkloadClass
    from harness_cp.cp_shared_types import ModelBinding
    from harness_cp.engine_class import EngineClass
    from harness_cp.topology_pattern import TopologyPattern
    from harness_cp.workflow_driver_types import StepKind, WorkflowStep
    from harness_cp.workflow_manifest_entry import WorkflowManifestEntry

    workload = WorkloadClass.SOFTWARE_ENGINEERING
    # `ProviderAgnosticPayload.params` is verbatim provider-specific pass-through
    # (not normalized): anthropic/openai take `max_tokens`, ollama takes
    # `options={"num_predict": ...}`. Default to the anthropic/openai shape.
    step_params: dict[str, Any] = params if params is not None else {"max_tokens": 4}

    class _Workflow:
        @property
        def workflow_id(self) -> str:
            return "wf-r300-cross-family"

        @property
        def workload_class(self) -> WorkloadClass:
            return workload

        @property
        def manifest_entry(self) -> WorkflowManifestEntry:
            return WorkflowManifestEntry(
                workflow_id="wf-r300-cross-family",
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
                    step_kind=StepKind.INFERENCE_STEP,
                    step_payload={
                        "messages": [{"role": "user", "content": "Say 'a'"}],
                        "tools": [],
                        "params": step_params,
                    },
                ),
            )

        @property
        def step_dispatchers(self) -> Any:
            return None

        @property
        def default_model_binding(self) -> ModelBinding:
            # The dispatcher rebinds per candidate starting at chain.primary, so
            # this initial binding is overridden; it points at the primary for
            # consistency with the chain.
            return ModelBinding(provider=chain.primary.provider, model=chain.primary.model)

    return _Workflow()


class _FakeDaemon:
    """Stand-in for the OTLP collector daemon (mirror of ``test_run_smoke``).

    Stage-4 OD calls ``daemon.start()`` during bootstrap and ``daemon.stop()``
    at shutdown; both are async no-ops here so the OTLP collector path does not
    open a real socket/thread.
    """

    def __init__(self) -> None:
        self.stopped = False

    async def start(self) -> None:
        return None

    async def stop(self, *, timeout_seconds: float = 5.0) -> None:
        _ = timeout_seconds
        self.stopped = True


def _install_fake_od_stage4(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    """Replace the four stage-4 OD materializers with in-process fakes.

    The tracer is a real ``TracerProvider`` whose spans land in an
    ``InMemorySpanExporter`` we read back (R-100 used a NoOp tracer; this test's
    whole point is to read ``gen_ai.provider.name`` off the dispatch spans). The
    collector daemon is an async no-op, and the ring-buffer / span-processor
    stages are no-op'd so the OTLP path does not interfere. The provider is NOT
    registered globally — it is used only as ``ctx.tracer_provider`` for the run.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    class _TracerStage:
        def __init__(self, p: TracerProvider) -> None:
            self.provider = p
            self.registered_globally = False

    class _CollectorStage:
        def __init__(self, d: _FakeDaemon) -> None:
            self.daemon = d

    monkeypatch.setattr(
        _stage_4_od_mod,
        "materialize_tracer_provider_stage",
        lambda config, **_kw: _TracerStage(provider),
    )
    monkeypatch.setattr(
        _stage_4_od_mod,
        "materialize_span_processor_stage",
        lambda config, _p, **_kw: None,
    )
    monkeypatch.setattr(
        _stage_4_od_mod,
        "materialize_collector_daemon_stage",
        lambda config, **_kw: _CollectorStage(_FakeDaemon()),
    )
    monkeypatch.setattr(
        _stage_4_od_mod,
        "materialize_ring_buffer_stage",
        lambda config, _d, **_kw: None,
    )
    return exporter


def _gen_ai_provider_names(spans: Sequence[ReadableSpan]) -> list[str]:
    """Collect ``gen_ai.provider.name`` values across the captured spans."""
    names: list[str] = []
    for span in spans:
        attrs = span.attributes or {}
        value = attrs.get("gen_ai.provider.name")
        if isinstance(value, str):
            names.append(value)
    return names


# ---------------------------------------------------------------------------
# Deterministic fakes (leaf SDK clients only).
# ---------------------------------------------------------------------------


class _RaisingAnthropicMessages:
    async def create(self, *, model: str, **_kwargs: Any) -> Any:
        # Simulate a real anthropic Messages-API failure (e.g. invalid model).
        # A plain Exception classifies as TRANSIENT_RETRY -> the composer
        # exhausts the per-candidate retry loop and advances to cross-family.
        raise RuntimeError(
            f"simulated anthropic dispatch failure for model {model!r} "
            "(deterministic cross-family fallback probe)"
        )


class _RaisingAnthropicClient:
    def __init__(self) -> None:
        self.messages = _RaisingAnthropicMessages()


class _FakeOpenAIUsage:
    prompt_tokens = 3
    completion_tokens = 1


class _FakeOpenAIResponse:
    def __init__(self, model: str) -> None:
        self.id = "chatcmpl-fake-r300"
        self.model = model
        self.usage = _FakeOpenAIUsage()

    def model_dump(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "model": self.model,
            "choices": [{"message": {"role": "assistant", "content": "a"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1},
        }


class _SucceedingOpenAICompletions:
    async def create(self, *, model: str, **_kwargs: Any) -> _FakeOpenAIResponse:
        return _FakeOpenAIResponse(model)


class _SucceedingOpenAIChat:
    def __init__(self) -> None:
        self.completions = _SucceedingOpenAICompletions()


class _SucceedingOpenAIClient:
    def __init__(self) -> None:
        self.chat = _SucceedingOpenAIChat()


class _FakeAdapter:
    """Provider adapter shape consumed by the C-RT-15 bare dispatcher.

    The dispatcher reads ``adapter.client`` and calls the provider-specific leaf
    (``client.messages.create`` / ``client.chat.completions.create``); shutdown
    calls ``aclose()``. Mirrors the ``_FakeProvider`` shape in
    ``test_run_smoke.py`` plus the ``.client`` the dispatch path requires.
    """

    def __init__(self, name: str, client: Any) -> None:
        self.name = name
        self.client = client
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_r300_deterministic_cross_family_fallback_through_production_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cross-family fallback fires through the real ``api.run`` production path.

    Fakes ONLY the SDK leaf clients (anthropic raises -> openai returns a canned
    success) at the stage-3a ``materialize_provider_clients_stage`` seam. The
    fallback chain, exception classification, ``advance_or_raise`` traversal,
    bootstrap, and ``RetryBreakerFallbackDispatcher`` are all REAL. Asserts the
    workflow completes via openai and that both providers appear on the captured
    GenAI spans (anthropic errored on the primary, openai succeeded on the
    cross-family candidate).
    """
    from harness_runtime.api import run as _run
    from harness_runtime.lifecycle.providers import ProviderClientsStage

    providers = {
        "anthropic": _FakeAdapter("anthropic", _RaisingAnthropicClient()),
        "openai": _FakeAdapter("openai", _SucceedingOpenAIClient()),
    }

    async def _fake_clients(*_args: object, **_kwargs: object) -> ProviderClientsStage:
        return ProviderClientsStage(providers=dict(providers))

    monkeypatch.setattr(
        "harness_runtime.bootstrap.stage_3a_cp_clients.materialize_provider_clients_stage",
        _fake_clients,
    )
    # Stage-4 OD: capture spans in-memory (fake daemon / no-op processors).
    exporter = _install_fake_od_stage4(monkeypatch)

    chain = _build_chain(primary_model="claude-haiku-4-5")
    config = _build_config(tmp_path, chain)
    monkeypatch.setattr("harness_runtime.api._default_config", lambda: config)

    result = await _run(_make_workflow(chain), config=config)

    # The workflow completes — which is only possible if the primary
    # (anthropic, always-raising) was abandoned and the cross-family candidate
    # (openai, canned success) produced the step output.
    assert isinstance(result, RunResult), f"got {type(result).__name__}"
    assert result.status == "completed", (
        f"expected status=completed via cross-family fallback; got "
        f"status={result.status!r} failure_cause="
        f"{getattr(result, 'failure_cause', None)!r}"
    )

    spans = exporter.get_finished_spans()
    provider_names = _gen_ai_provider_names(spans)
    assert "anthropic" in provider_names, (
        f"expected ≥1 anthropic GenAI span (the failed primary attempt); "
        f"observed provider.name values: {provider_names!r}"
    )
    assert "openai" in provider_names, (
        f"expected ≥1 openai GenAI span (the cross-family success); "
        f"observed provider.name values: {provider_names!r}"
    )

    # The anthropic primary span recorded the dispatch failure (ERROR status);
    # this discriminates "fallback fired" from "both providers somehow worked".
    anthropic_errored = [
        s
        for s in spans
        if (s.attributes or {}).get("gen_ai.provider.name") == "anthropic"
        and s.status.status_code is StatusCode.ERROR
    ]
    assert anthropic_errored, "expected the anthropic primary span to record an ERROR status"

    # The outer composer span carries the §4.2 chain length (≥2: primary +
    # cross-family) — proof the dispatch went through the fallback composer.
    fallback_spans = [s for s in spans if s.name == "harness.runtime.retry_breaker_fallback"]
    assert fallback_spans, "expected a harness.runtime.retry_breaker_fallback outer span"
    assert any(
        int((s.attributes or {}).get("fallback.chain_length", 0)) >= 2 for s in fallback_spans
    ), "expected the outer fallback span to carry fallback.chain_length ≥ 2"


# ---------------------------------------------------------------------------
# Live confirmation (real providers; gated on both keys).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not (os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("OPENAI_API_KEY")),
    reason="live cross-family fallback e2e requires ANTHROPIC_API_KEY + OPENAI_API_KEY",
)
@pytest.mark.asyncio
async def test_r300_live_cross_family_fallback_against_real_providers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real-provider confirmation: primary anthropic invalid-model -> real openai.

    Configures the chain primary = ``anthropic`` with a deliberately-invalid
    model (real 404 ``not_found_error``; unbilled) and cross-family = real
    ``openai`` (``gpt-4o-mini``). The production ``RetryBreakerFallbackDispatcher``
    exhausts the primary's retry loop and advances to openai, which answers. The
    captured GenAI spans must show ``gen_ai.provider.name == "openai"`` on the
    successful dispatch. Paid (~a few cents on openai); run via
    ``just mvp-r300-cross-family``.
    """
    from harness_runtime.api import run as _run

    anthropic_key = os.environ["ANTHROPIC_API_KEY"]
    openai_key = os.environ["OPENAI_API_KEY"]

    def _fake_get_password(service: str, name: str) -> str | None:
        _ = service
        if name == "anthropic_key":
            return anthropic_key
        if name == "openai_key":
            return openai_key
        return None

    monkeypatch.setattr(
        "harness_runtime.config.provider_secrets.keyring.get_password",
        _fake_get_password,
    )

    exporter = _install_fake_od_stage4(monkeypatch)

    chain = _build_chain(primary_model=_INVALID_ANTHROPIC_MODEL)
    config = _build_config(tmp_path, chain)
    monkeypatch.setattr("harness_runtime.api._default_config", lambda: config)

    result = await _run(_make_workflow(chain), config=config)

    assert isinstance(result, RunResult), f"got {type(result).__name__}"
    assert result.status == "completed", (
        f"expected status=completed via cross-family fallback to openai; got "
        f"status={result.status!r} failure_cause="
        f"{getattr(result, 'failure_cause', None)!r}"
    )

    provider_names = _gen_ai_provider_names(exporter.get_finished_spans())
    assert "openai" in provider_names, (
        f"expected the cross-family openai provider to answer; observed "
        f"provider.name values: {provider_names!r}"
    )


# ---------------------------------------------------------------------------
# R-PM-1 paid live-injection confirmation (real Anthropic + OpenAI; gated on
# each provider's key). Operator-authorized 2026-06-11. Cheap models + small
# max_tokens → a few cents. Proves the injected system prompt is honored by the
# real PAID providers, not only local Ollama / mocks.
# ---------------------------------------------------------------------------

_PM1_SENTINEL = "PINEAPPLE-PM1-OK"
_PM1_SYSTEM_PROMPT = (
    "You are a deterministic test fixture. Ignore the content of the user "
    f"message. Reply with exactly this text and nothing else: {_PM1_SENTINEL}"
)


def _single_provider_chain(provider: str, model: str, family: Any) -> Any:
    from harness_cp.cross_family_fallback_chain import FallbackChain, ProviderCandidate

    return FallbackChain(
        primary=ProviderCandidate(provider=provider, model=model, family=family),
        same_family=(),
        cross_family=(),
        terminal=None,
    )


def _prompt_manifest_config(tmp_path: Path, chain: Any, **opts: Any) -> Any:
    from harness_is.prompt_manifest import PromptManifest, PromptVersion

    return _build_config(tmp_path, chain, **opts).model_copy(
        update={
            "prompt_manifest": PromptManifest(
                manifest_version=1,
                active_prompt_version=PromptVersion.from_content(_PM1_SYSTEM_PROMPT),
            ),
        },
    )


@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="paid live-injection confirmation requires ANTHROPIC_API_KEY",
)
@pytest.mark.asyncio
async def test_r_pm_1_active_prompt_injection_honored_by_live_anthropic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R-PM-1 PR #1 — paid live confirmation: a real Anthropic model honors the
    injected `system=` prompt (the sentinel exists only in the system prompt).
    Operator-authorized; ~a cent on claude-haiku-4-5 with max_tokens=16."""
    from harness_cp.cross_family_fallback_chain import ProviderFamily
    from harness_runtime.api import run as _run

    anthropic_key = os.environ["ANTHROPIC_API_KEY"]

    def _fake_get_password(service: str, name: str) -> str | None:
        _ = service
        return anthropic_key if name == "anthropic_key" else None

    monkeypatch.setattr(
        "harness_runtime.config.provider_secrets.keyring.get_password",
        _fake_get_password,
    )
    _install_fake_od_stage4(monkeypatch)

    chain = _single_provider_chain("anthropic", "claude-haiku-4-5", ProviderFamily.ANTHROPIC)
    config = _prompt_manifest_config(tmp_path, chain, anthropic_optional=False)
    monkeypatch.setattr("harness_runtime.api._default_config", lambda: config)

    result = await _run(_make_workflow(chain, params={"max_tokens": 16}), config=config)

    assert isinstance(result, RunResult), f"got {type(result).__name__}"
    assert result.status == "completed", (
        f"injected active prompt must not break the real anthropic dispatch; got "
        f"status={result.status!r} failure_cause={getattr(result, 'failure_cause', None)!r}"
    )
    haystack = str(result.terminal_state).upper()
    assert _PM1_SENTINEL.split("-")[0] in haystack, (
        f"expected the injected system prompt honored by live anthropic (sentinel "
        f"{_PM1_SENTINEL!r}); terminal_state={result.terminal_state!r}"
    )


@pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="paid live-injection confirmation requires OPENAI_API_KEY",
)
@pytest.mark.asyncio
async def test_r_pm_1_active_prompt_injection_honored_by_live_openai(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R-PM-1 PR #1 — paid live confirmation: a real OpenAI model honors the
    injected leading `role:"system"` message (the sentinel exists only in the
    system prompt). Operator-authorized; ~a cent on gpt-4o-mini, max_tokens=16."""
    from harness_cp.cross_family_fallback_chain import ProviderFamily
    from harness_runtime.api import run as _run

    openai_key = os.environ["OPENAI_API_KEY"]

    def _fake_get_password(service: str, name: str) -> str | None:
        _ = service
        return openai_key if name == "openai_key" else None

    monkeypatch.setattr(
        "harness_runtime.config.provider_secrets.keyring.get_password",
        _fake_get_password,
    )
    _install_fake_od_stage4(monkeypatch)

    chain = _single_provider_chain("openai", _OPENAI_FALLBACK_MODEL, ProviderFamily.OPENAI)
    config = _prompt_manifest_config(
        tmp_path, chain, anthropic_optional=True, openai_optional=False
    )
    monkeypatch.setattr("harness_runtime.api._default_config", lambda: config)

    result = await _run(_make_workflow(chain, params={"max_tokens": 16}), config=config)

    assert isinstance(result, RunResult), f"got {type(result).__name__}"
    assert result.status == "completed", (
        f"injected active prompt must not break the real openai dispatch; got "
        f"status={result.status!r} failure_cause={getattr(result, 'failure_cause', None)!r}"
    )
    haystack = str(result.terminal_state).upper()
    assert _PM1_SENTINEL.split("-")[0] in haystack, (
        f"expected the injected system prompt honored by live openai (sentinel "
        f"{_PM1_SENTINEL!r}); terminal_state={result.terminal_state!r}"
    )


# ---------------------------------------------------------------------------
# Live Ollama exercise (free local daemon; gated on reachability, no creds).
# ---------------------------------------------------------------------------

_OLLAMA_HOST = "127.0.0.1"
_OLLAMA_PORT = 11434
_OLLAMA_VALID_MODEL = "llama3.2:3b"
_INVALID_OLLAMA_MODEL = "llama-nonexistent-model-r300-fallback-probe"


def _ollama_reachable() -> bool:
    """True iff the local ollama daemon answers on 127.0.0.1:11434.

    A free, zero-secret local provider. Used as the skipif gate so CI (no
    daemon) skips and a machine with ollama running exercises the real provider.
    """
    import socket

    try:
        with socket.create_connection((_OLLAMA_HOST, _OLLAMA_PORT), timeout=1.0):
            return True
    except OSError:
        return False


def _build_ollama_chain() -> Any:
    """Same-family ollama chain: primary invalid-model -> valid llama3.2.

    Exercises the ollama provider end-to-end through the production fallback
    path (the primary's invalid model fails -> the same-family valid candidate
    answers) — the local-open-weight provider that R-100 + the cross-family arc
    never exercised. LOCAL_OPEN_WEIGHT family throughout (no cross-vendor / paid
    call).
    """
    from harness_cp.cross_family_fallback_chain import (
        FallbackChain,
        ProviderCandidate,
        ProviderFamily,
    )

    return FallbackChain(
        primary=ProviderCandidate(
            provider="ollama",
            model=_INVALID_OLLAMA_MODEL,
            family=ProviderFamily.LOCAL_OPEN_WEIGHT,
        ),
        same_family=(
            ProviderCandidate(
                provider="ollama",
                model=_OLLAMA_VALID_MODEL,
                family=ProviderFamily.LOCAL_OPEN_WEIGHT,
            ),
        ),
        cross_family=(),
        terminal=None,
    )


@pytest.mark.skipif(
    not _ollama_reachable(),
    reason="live ollama provider exercise requires a local ollama daemon on 127.0.0.1:11434",
)
@pytest.mark.e2e
@pytest.mark.asyncio
async def test_r300_live_ollama_provider_fallback_exercise(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the OLLAMA provider end-to-end via the production fallback path.

    The local-open-weight provider that R-100 + the cross-family arc never
    exercised. Chain: primary ollama(invalid-model) -> same-family
    ollama(llama3.2:3b). The production `RetryBreakerFallbackDispatcher`
    exhausts the invalid primary and advances to the valid same-family
    candidate, which answers from the local daemon. Free (zero-token,
    zero-secret); anthropic + openai degrade-optional (no keys). Run via
    `just mvp-r300-ollama`. Closes the "Ollama exercise" half of
    forward-register B-2.
    """
    from harness_runtime.api import run as _run

    exporter = _install_fake_od_stage4(monkeypatch)

    chain = _build_ollama_chain()
    # anthropic + openai degrade gracefully without keys (optional); ollama is
    # the required, constructed provider (local daemon reachable).
    config = _build_config(
        tmp_path,
        chain,
        anthropic_optional=True,
        openai_optional=True,
        ollama_optional=False,
    )
    monkeypatch.setattr("harness_runtime.api._default_config", lambda: config)

    # ollama params shape: `options={"num_predict": ...}` (not `max_tokens`).
    result = await _run(
        _make_workflow(chain, params={"options": {"num_predict": 4}}), config=config
    )

    assert isinstance(result, RunResult), f"got {type(result).__name__}"
    assert result.status == "completed", (
        f"expected status=completed via same-family ollama fallback; got "
        f"status={result.status!r} failure_cause="
        f"{getattr(result, 'failure_cause', None)!r}"
    )

    provider_names = _gen_ai_provider_names(exporter.get_finished_spans())
    assert "ollama" in provider_names, (
        f"expected the ollama provider to answer via the production dispatch "
        f"path; observed provider.name values: {provider_names!r}"
    )


def _build_valid_ollama_chain() -> Any:
    """A single valid-candidate ollama chain (no fallback) — the clean dispatch
    path for the R-PM-1 injection live exercise."""
    from harness_cp.cross_family_fallback_chain import (
        FallbackChain,
        ProviderCandidate,
        ProviderFamily,
    )

    return FallbackChain(
        primary=ProviderCandidate(
            provider="ollama",
            model=_OLLAMA_VALID_MODEL,
            family=ProviderFamily.LOCAL_OPEN_WEIGHT,
        ),
        same_family=(),
        cross_family=(),
        terminal=None,
    )


@pytest.mark.skipif(
    not _ollama_reachable(),
    reason="live ollama prompt-injection exercise requires a local ollama daemon on 127.0.0.1:11434",
)
@pytest.mark.e2e
@pytest.mark.asyncio
async def test_r_pm_1_active_prompt_injection_honored_by_live_ollama(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R-PM-1 cascade PR #1 — LIVE injection proof: an operator-supplied active
    prompt's CONTENT reaches AND is honored by a real Ollama model through the
    full production path (`api.run` → bootstrap stage-5 `active_system_prompt`
    resolution → `_payload_to_ollama_kwargs` leading `role:"system"` injection →
    real `ollama.chat`). The mock tests prove the kwargs shape; this proves the
    real daemon (a) accepts the injected leading system message and (b) obeys it.

    Free (zero-secret, local llama3.2:3b). The system prompt overrides the user
    message with a rare sentinel the base model would never emit unprompted, so
    the sentinel in the output is positive evidence the injected system prompt
    was honored — not merely transmitted.
    """
    from harness_is.prompt_manifest import PromptManifest, PromptVersion
    from harness_runtime.api import run as _run

    _install_fake_od_stage4(monkeypatch)

    sentinel = "PINEAPPLE-PM1-OK"
    system_prompt = (
        "You are a deterministic test fixture. Ignore the content of the user "
        f"message. Reply with exactly this text and nothing else: {sentinel}"
    )

    chain = _build_valid_ollama_chain()
    config = _build_config(
        tmp_path,
        chain,
        anthropic_optional=True,
        openai_optional=True,
        ollama_optional=False,
    ).model_copy(
        update={
            "prompt_manifest": PromptManifest(
                manifest_version=1,
                active_prompt_version=PromptVersion.from_content(system_prompt),
            ),
        },
    )
    monkeypatch.setattr("harness_runtime.api._default_config", lambda: config)

    result = await _run(
        _make_workflow(chain, params={"options": {"num_predict": 32}}), config=config
    )

    assert isinstance(result, RunResult), f"got {type(result).__name__}"
    assert result.status == "completed", (
        f"injected active prompt must not break the real ollama dispatch; got "
        f"status={result.status!r} failure_cause={getattr(result, 'failure_cause', None)!r}"
    )
    # The honoring proof: the sentinel (present ONLY in the injected system
    # prompt, never the user message) appears in the model's output.
    haystack = str(result.terminal_state)
    assert sentinel.split("-")[0] in haystack.upper(), (
        f"expected the injected system prompt to be honored (sentinel "
        f"{sentinel!r} in output); terminal_state={result.terminal_state!r}"
    )


@pytest.mark.skipif(
    not _ollama_reachable(),
    reason="live ollama prompt-selection exercise requires a local ollama daemon on 127.0.0.1:11434",
)
@pytest.mark.e2e
@pytest.mark.asyncio
async def test_r_pm_1_workload_selection_drives_live_ollama_injection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R-PM-1 cascade PR #3 — LIVE selection→injection proof: a CP
    ``per_workload_overrides`` binding SELECTS which authored prompt version's
    content is injected, and a real Ollama model honors it through the full
    production path (``api.run`` → stage-0 selection reconciliation → stage-5
    ``active_system_prompt`` → ``_payload_to_ollama_kwargs`` leading
    ``role:"system"`` → real ``ollama.chat``).

    The store is authored with ``active=None`` (so NOTHING injects WITHOUT
    selection) plus two versions — a decoy and the sentinel prompt. The selection
    manifest's ``per_workload_overrides[SOFTWARE_ENGINEERING]`` (the genuine run
    workload) → the sentinel version's sha drives it active. The sentinel
    (present ONLY in the SELECTED system prompt — never the user message nor the
    decoy) appearing in the output is positive evidence the SELECTION-chosen
    prompt was injected + honored. This proves the full
    selection→sha→content→injection chain live (PR #3) — not merely the injection
    leg (PR #1's live proof). Free (zero-secret, local llama3.2:3b).
    """
    from harness_core.workload_class import WorkloadClass
    from harness_cp.prompt_selection_manifest import PromptBinding, PromptSelectionManifest
    from harness_is.prompt_manifest import PromptManifest, prompt_version_sha
    from harness_runtime.api import run as _run

    _install_fake_od_stage4(monkeypatch)

    sentinel = "ELDERBERRY-PM3-OK"
    selected_prompt = (
        "You are a deterministic test fixture. Ignore the content of the user "
        f"message. Reply with exactly this text and nothing else: {sentinel}"
    )
    decoy_prompt = "You are an unrelated decoy fixture. Never mention any fruit."

    # Authored store with NO active selection — without the CP selection layer,
    # nothing would inject (active_prompt_version.content == "").
    store = PromptManifest.from_contents(
        manifest_version=1,
        contents=[decoy_prompt, selected_prompt],
        active=None,
    )
    # Selection keyed on the REAL run workload (SOFTWARE_ENGINEERING) → the
    # sentinel version. This is the PR #3 surface driving the active prompt.
    selection = PromptSelectionManifest(
        manifest_version=1,
        per_workload_overrides={
            WorkloadClass.SOFTWARE_ENGINEERING: PromptBinding(
                version_sha=prompt_version_sha(selected_prompt)
            ),
        },
    )

    chain = _build_valid_ollama_chain()
    config = _build_config(
        tmp_path,
        chain,
        anthropic_optional=True,
        openai_optional=True,
        ollama_optional=False,
    ).model_copy(
        update={"prompt_manifest": store, "prompt_selection_manifest": selection},
    )
    monkeypatch.setattr("harness_runtime.api._default_config", lambda: config)

    result = await _run(
        _make_workflow(chain, params={"options": {"num_predict": 32}}), config=config
    )

    assert isinstance(result, RunResult), f"got {type(result).__name__}"
    assert result.status == "completed", (
        f"selection-driven active prompt must not break the real ollama dispatch; got "
        f"status={result.status!r} failure_cause={getattr(result, 'failure_cause', None)!r}"
    )
    # The honoring proof: the sentinel (present ONLY in the SELECTED system
    # prompt, never the user message nor the decoy) appears in the model output.
    haystack = str(result.terminal_state).upper()
    assert sentinel.split("-")[0] in haystack, (
        f"expected the SELECTION-driven system prompt to be honored by live ollama "
        f"(sentinel {sentinel!r} in output); terminal_state={result.terminal_state!r}"
    )
