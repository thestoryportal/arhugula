"""U-RT-88 — `materialize_pause_resume_protocol_stage` factory + stage-5 wiring tests.

ACs per runtime plan v2.20 §1 U-RT-88:

1. Factory authored with signature
   `async def materialize_pause_resume_protocol_stage(config, ctx, *, pause_context_reader=None) → PauseResumeProtocol | None`.
2. Opt-out branch: `config.pause_resume_protocol_config is None` → factory
   returns `None` unconditionally.
3. Opt-in branch: factory returns CP-canonical `PauseResumeProtocol` instance
   bound to `ctx.ledger_writer` + `ctx.ledger_reader` + composed reader.
4. Stage-5 LOOP_INIT wiring: factory invocation appended in `stage_5_loop_init.py`;
   output bound to `ctx.pause_resume_protocol`.
5. `pause_context_reader` composition: factory composes default reader when
   caller does not supply one; accepts custom reader override.
6. `PauseResumeStageMaterializeError` typed exception authored; carries the
   `RT-FAIL-PAUSE-RESUME-STAGE-MATERIALIZE:` prefix when raised.
7. Spec §14.14.5 invariants verified (single instance per bootstrap +
   empty-sentinel preserves backward compat + CP-canonical class satisfaction).
8. Importable.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any

import pytest
from harness_core.deployment_surface import DeploymentSurface
from harness_cp.handoff_context import StateSummary
from harness_cp.pause_resume_protocol import PauseResumeProtocol
from harness_cp.topology_pattern import TopologyPattern
from harness_is.state_ledger_entry_schema import Identifier
from harness_runtime.bootstrap.factories.pause_resume_protocol_factory import (
    _MVP_PAUSE_ANCHOR_SENTINEL,
    PauseResumeStageMaterializeError,
    _make_default_pause_context_reader,
    materialize_pause_resume_protocol_stage,
)
from harness_runtime.bootstrap.mutable_context import _MutableHarnessContext
from harness_runtime.lifecycle.pause_resume_protocol_types import (
    PauseResumeProtocolConfig,
)
from harness_runtime.types import (
    CollectorConfig,
    OTelConfig,
    PathBindingConfig,
    ProviderSecretsConfig,
    RuntimeConfig,
)


def _minimal_runtime_config_kwargs(tmp_path: Path) -> dict[str, Any]:
    return {
        "deployment_surface": DeploymentSurface.LOCAL_DEVELOPMENT,
        "repository_root": tmp_path,
        "path_bindings": PathBindingConfig(),
        "provider_secrets": ProviderSecretsConfig(),
        "otel": OTelConfig(otlp_endpoint="http://localhost:4318"),
        "collector": CollectorConfig(),
        "default_topology": TopologyPattern.SINGLE_THREADED_LINEAR,
    }


def _make_mutable_ctx_with_ledger_refs() -> _MutableHarnessContext:
    """Construct a _MutableHarnessContext with non-None ledger_writer + reader
    sentinels suitable for opt-in-branch factory invocation tests.

    The factory only checks `is None` on the ledger refs; it does not invoke
    any method on them. Object sentinels suffice for the factory-body unit
    tests (real stage-1 IS materialization is exercised at the U-RT-89 e2e).
    """
    ctx = _MutableHarnessContext()
    ctx.ledger_writer = object()  # type: ignore[assignment]
    ctx.ledger_reader = object()  # type: ignore[assignment]
    return ctx


# AC #1 — factory signature.


def test_factory_is_async() -> None:
    assert inspect.iscoroutinefunction(materialize_pause_resume_protocol_stage)


def test_factory_signature_accepts_config_ctx_keyword_reader() -> None:
    sig = inspect.signature(materialize_pause_resume_protocol_stage)
    params = list(sig.parameters)
    assert params == ["config", "ctx", "pause_context_reader"], (
        "factory signature must be (config, ctx, *, pause_context_reader=None) "
        f"per spec §14.14.1; got {params}"
    )
    # pause_context_reader is keyword-only with default None.
    reader_param = sig.parameters["pause_context_reader"]
    assert reader_param.kind == inspect.Parameter.KEYWORD_ONLY
    assert reader_param.default is None


# AC #2 — opt-out branch.


@pytest.mark.asyncio
async def test_factory_returns_none_when_config_is_none(tmp_path: Path) -> None:
    config = RuntimeConfig(
        **_minimal_runtime_config_kwargs(tmp_path),
        pause_resume_protocol_config=None,
    )
    ctx = _make_mutable_ctx_with_ledger_refs()
    result = await materialize_pause_resume_protocol_stage(config, ctx)
    assert result is None


@pytest.mark.asyncio
async def test_factory_opt_out_does_not_validate_ctx_prerequisites(
    tmp_path: Path,
) -> None:
    """Opt-out branch returns None unconditionally; does NOT inspect ctx for
    ledger_writer/reader prerequisites (spec §14.14.5 invariant 2 — empty-
    sentinel preserves backward compat even when stage-1 IS hasn't run)."""
    config = RuntimeConfig(
        **_minimal_runtime_config_kwargs(tmp_path),
        pause_resume_protocol_config=None,
    )
    ctx = _MutableHarnessContext()  # no ledger refs populated
    result = await materialize_pause_resume_protocol_stage(config, ctx)
    assert result is None


# AC #3 — opt-in branch with default reader composition.


@pytest.mark.asyncio
async def test_factory_returns_protocol_when_config_present(tmp_path: Path) -> None:
    config = RuntimeConfig(
        **_minimal_runtime_config_kwargs(tmp_path),
        pause_resume_protocol_config=PauseResumeProtocolConfig.default(),
    )
    ctx = _make_mutable_ctx_with_ledger_refs()
    result = await materialize_pause_resume_protocol_stage(config, ctx)
    assert result is not None
    assert isinstance(result, PauseResumeProtocol)


# AC #5 — pause_context_reader composition (default + override).


@pytest.mark.asyncio
async def test_factory_uses_default_reader_when_none_supplied(
    tmp_path: Path,
) -> None:
    config = RuntimeConfig(
        **_minimal_runtime_config_kwargs(tmp_path),
        pause_resume_protocol_config=PauseResumeProtocolConfig.default(),
    )
    ctx = _make_mutable_ctx_with_ledger_refs()
    result = await materialize_pause_resume_protocol_stage(config, ctx)
    assert result is not None
    # Default-composed reader returns the constant anchor sentinel.
    summary, anchor = result._pause_context_reader()
    assert anchor == _MVP_PAUSE_ANCHOR_SENTINEL
    assert isinstance(summary, StateSummary)


@pytest.mark.asyncio
async def test_factory_accepts_custom_reader_override(tmp_path: Path) -> None:
    custom_anchor = "deadbeef" * 8  # 64-char custom anchor
    custom_summary = StateSummary(
        relevant_entries=(),
        summary_text="custom",
        summary_hash="1" * 64,
        idempotency_key=Identifier("custom-key"),
        external_references=(),
    )

    def custom_reader() -> tuple[StateSummary, str]:
        return (custom_summary, custom_anchor)

    config = RuntimeConfig(
        **_minimal_runtime_config_kwargs(tmp_path),
        pause_resume_protocol_config=PauseResumeProtocolConfig.default(),
    )
    ctx = _make_mutable_ctx_with_ledger_refs()
    result = await materialize_pause_resume_protocol_stage(
        config, ctx, pause_context_reader=custom_reader
    )
    assert result is not None
    summary, anchor = result._pause_context_reader()
    assert anchor == custom_anchor
    assert summary.summary_text == "custom"


# AC #6 — RT-FAIL-PAUSE-RESUME-STAGE-MATERIALIZE fail-class on missing prerequisites.


@pytest.mark.asyncio
async def test_factory_raises_when_ledger_writer_missing(tmp_path: Path) -> None:
    config = RuntimeConfig(
        **_minimal_runtime_config_kwargs(tmp_path),
        pause_resume_protocol_config=PauseResumeProtocolConfig.default(),
    )
    ctx = _MutableHarnessContext()
    ctx.ledger_writer = None  # explicit absence
    ctx.ledger_reader = object()  # type: ignore[assignment]
    with pytest.raises(PauseResumeStageMaterializeError) as excinfo:
        await materialize_pause_resume_protocol_stage(config, ctx)
    assert "RT-FAIL-PAUSE-RESUME-STAGE-MATERIALIZE:" in str(excinfo.value)
    assert "ctx.ledger_writer" in str(excinfo.value)


@pytest.mark.asyncio
async def test_factory_raises_when_ledger_reader_missing(tmp_path: Path) -> None:
    config = RuntimeConfig(
        **_minimal_runtime_config_kwargs(tmp_path),
        pause_resume_protocol_config=PauseResumeProtocolConfig.default(),
    )
    ctx = _MutableHarnessContext()
    ctx.ledger_writer = object()  # type: ignore[assignment]
    ctx.ledger_reader = None  # explicit absence
    with pytest.raises(PauseResumeStageMaterializeError) as excinfo:
        await materialize_pause_resume_protocol_stage(config, ctx)
    assert "RT-FAIL-PAUSE-RESUME-STAGE-MATERIALIZE:" in str(excinfo.value)


def test_pause_resume_stage_materialize_error_is_exception_subclass() -> None:
    assert issubclass(PauseResumeStageMaterializeError, Exception)


# Default reader helper unit tests.


def test_default_reader_returns_state_summary_and_anchor() -> None:
    ctx = _MutableHarnessContext()
    reader = _make_default_pause_context_reader(ctx)
    summary, anchor = reader()
    assert isinstance(summary, StateSummary)
    assert anchor == _MVP_PAUSE_ANCHOR_SENTINEL


def test_default_reader_returns_minimal_placeholder_state_summary() -> None:
    """MVP shape per spec §14.14.7: empty relevant_entries + empty summary_text
    + 64-zero summary_hash + empty Identifier + empty external_references."""
    ctx = _MutableHarnessContext()
    reader = _make_default_pause_context_reader(ctx)
    summary, _ = reader()
    assert summary.relevant_entries == ()
    assert summary.summary_text == ""
    assert summary.summary_hash == "0" * 64
    assert summary.idempotency_key == ""
    assert summary.external_references == ()


def test_default_reader_anchor_is_64_char_sentinel() -> None:
    assert len(_MVP_PAUSE_ANCHOR_SENTINEL) == 64
    assert _MVP_PAUSE_ANCHOR_SENTINEL == "0" * 64


# AC #4 — stage-5 wiring (verified via source inspection — full integration
# verified at U-RT-89 e2e per [[verification-shape-sharpened-grep-vs-e2e]]).


def test_stage_5_loop_init_invokes_pause_resume_protocol_factory() -> None:
    """Source-level verification that stage_5_loop_init.py invokes the
    pause_resume_protocol factory (full integration at U-RT-89 e2e)."""
    from harness_runtime.bootstrap import stage_5_loop_init

    source = inspect.getsource(stage_5_loop_init)
    assert "materialize_pause_resume_protocol_stage" in source
    assert "ctx.pause_resume_protocol" in source


# AC #7 — spec §14.14.5 invariants (operationally verified via factory body).


@pytest.mark.asyncio
async def test_invariant_2_empty_sentinel_preserves_backward_compat(
    tmp_path: Path,
) -> None:
    """Spec §14.14.5 invariant 2: opt-out branch yields no-pause-protocol state;
    existing test suite passes without amendment."""
    config = RuntimeConfig(
        **_minimal_runtime_config_kwargs(tmp_path),
        pause_resume_protocol_config=None,
    )
    ctx = _make_mutable_ctx_with_ledger_refs()
    result = await materialize_pause_resume_protocol_stage(config, ctx)
    assert result is None  # ctx.pause_resume_protocol will be None


@pytest.mark.asyncio
async def test_invariant_3_cp_canonical_class_satisfaction(tmp_path: Path) -> None:
    """Spec §14.14.5 invariant 3: opt-in branch returns the CP-canonical
    PauseResumeProtocol class body (not a substitute or wrapper)."""
    config = RuntimeConfig(
        **_minimal_runtime_config_kwargs(tmp_path),
        pause_resume_protocol_config=PauseResumeProtocolConfig.default(),
    )
    ctx = _make_mutable_ctx_with_ledger_refs()
    result = await materialize_pause_resume_protocol_stage(config, ctx)
    assert result is not None
    # Must be the CP-canonical class from harness_cp.pause_resume_protocol —
    # not a runtime subclass or wrapper.
    assert type(result) is PauseResumeProtocol


# AC #8 — importable.


def test_factory_module_importable() -> None:
    from harness_runtime.bootstrap.factories import pause_resume_protocol_factory

    assert hasattr(pause_resume_protocol_factory, "materialize_pause_resume_protocol_stage")
    assert hasattr(pause_resume_protocol_factory, "PauseResumeStageMaterializeError")
    assert hasattr(pause_resume_protocol_factory, "_make_default_pause_context_reader")


def test_factory_invocation_is_awaitable(tmp_path: Path) -> None:
    config = RuntimeConfig(
        **_minimal_runtime_config_kwargs(tmp_path),
        pause_resume_protocol_config=None,
    )
    ctx = _make_mutable_ctx_with_ledger_refs()
    coro = materialize_pause_resume_protocol_stage(config, ctx)
    assert inspect.iscoroutine(coro)
    # Run it to completion to avoid coroutine-never-awaited warning.
    result = asyncio.run(coro)
    assert result is None
