"""U-RT-45 — flush_observability primitive tests.

Acceptance criteria per Phase 2 Session 3 atomic decomposition L10 U-RT-45 +
spec §10 C-RT-10 step 2:

- AC #1: all spans visible in collector sqlite post-flush.
- AC #2: ledger chain head consistent post-flush.

Test surfaces:
- FlushReport schema invariants
- tracer force_flush dispatched via asyncio.to_thread (sync OTel call doesn't block loop)
- ledger fsync executed on canonical_path
- cost-chain no-op (stateless-by-design per U-RT-31)
- per-resource failure isolation (one fails, others still run)
- timeout surfaced when force_flush returns False
- idempotent re-flush
- typed FlushTimeoutError surface available for callers that want to escalate
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from harness_runtime.shutdown import (
    AlreadyShutDown,
    FlushReport,
    FlushTimeoutError,
    ShutdownReport,
    ShutdownTimeout,
    flush_observability,
    shutdown,
)
from pydantic import ValidationError

# `harness_runtime.shutdown` attribute is shadowed by the `shutdown` function
# re-exported in `harness_runtime/__init__.py`. Go through sys.modules to
# reach the actual submodule for registry introspection.
shutdown_mod = sys.modules["harness_runtime.shutdown"]

# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


class _FakeTracerProvider:
    """OTel-shaped tracer provider stub — records the timeout passed."""

    def __init__(self, *, returns: bool = True, raises: Exception | None = None) -> None:
        self.calls: list[int] = []
        self._returns = returns
        self._raises = raises

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        self.calls.append(timeout_millis)
        if self._raises is not None:
            raise self._raises
        return self._returns


def _ctx_with(
    tmp_path: Path,
    *,
    tracer: object,
    ledger_path: Path | None = None,
) -> Any:
    """Build a duck-typed `HarnessContext` good enough for flush_observability.

    The flush primitive uses two attributes only — `tracer_provider` and
    `ledger_writer.handle.canonical_path`. We don't need the full bootstrap.
    """
    path = ledger_path if ledger_path is not None else tmp_path / "state.jsonl"
    path.write_text("")  # ensure file exists for fsync
    handle = SimpleNamespace(canonical_path=path)
    ledger_writer = SimpleNamespace(handle=handle)
    return SimpleNamespace(tracer_provider=tracer, ledger_writer=ledger_writer)


# ---------------------------------------------------------------------------
# FlushReport schema.
# ---------------------------------------------------------------------------


def test_flush_report_is_frozen() -> None:
    report = FlushReport(
        tracer_flushed=True,
        ledger_fsynced=True,
        cost_chain_noop=True,
        timed_out=False,
        failures=(),
    )
    with pytest.raises(ValidationError):
        report.tracer_flushed = False  # type: ignore[misc]


def test_flush_report_failures_must_be_tuple() -> None:
    report = FlushReport(
        tracer_flushed=False,
        ledger_fsynced=True,
        cost_chain_noop=True,
        timed_out=False,
        failures=("tracer",),
    )
    assert report.failures == ("tracer",)
    assert isinstance(report.failures, tuple)


# ---------------------------------------------------------------------------
# Happy path.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flush_observability_happy_path(tmp_path: Path) -> None:
    """Both surfaces succeed; cost-chain reports no-op."""
    tracer = _FakeTracerProvider(returns=True)
    ctx = _ctx_with(tmp_path, tracer=tracer)

    report = await flush_observability(ctx, timeout_millis=5_000)

    assert report.tracer_flushed is True
    assert report.ledger_fsynced is True
    assert report.cost_chain_noop is True
    assert report.timed_out is False
    assert report.failures == ()
    assert tracer.calls == [5_000]


@pytest.mark.asyncio
async def test_flush_observability_uses_default_timeout(tmp_path: Path) -> None:
    tracer = _FakeTracerProvider()
    ctx = _ctx_with(tmp_path, tracer=tracer)

    await flush_observability(ctx)

    assert tracer.calls == [30_000]


# ---------------------------------------------------------------------------
# Per-surface execution.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flush_observability_runs_tracer_in_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sync `force_flush` must be dispatched off the event loop."""
    to_thread_calls: list[tuple[object, tuple[object, ...]]] = []

    async def _spy_to_thread(fn: object, *args: object) -> object:
        to_thread_calls.append((fn, args))
        return fn(*args)  # type: ignore[operator]

    monkeypatch.setattr(asyncio, "to_thread", _spy_to_thread)
    tracer = _FakeTracerProvider(returns=True)
    ctx = _ctx_with(tmp_path, tracer=tracer)

    await flush_observability(ctx, timeout_millis=1_000)

    assert len(to_thread_calls) == 1
    fn, args = to_thread_calls[0]
    # Bound-method identity isn't stable across attribute accesses; compare
    # by __func__ (the underlying function) instead.
    assert getattr(fn, "__func__", None) is _FakeTracerProvider.force_flush
    assert args == (1_000,)


@pytest.mark.asyncio
async def test_flush_observability_fsyncs_ledger_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`os.fsync` is called with the open fd of the ledger canonical_path."""
    ledger_path = tmp_path / "state.jsonl"
    ledger_path.write_text("entry-1\n")

    fsynced_fds: list[int] = []
    real_fsync = os.fsync

    def _spy_fsync(fd: int) -> None:
        fsynced_fds.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", _spy_fsync)
    tracer = _FakeTracerProvider(returns=True)
    ctx = _ctx_with(tmp_path, tracer=tracer, ledger_path=ledger_path)

    report = await flush_observability(ctx)

    assert report.ledger_fsynced is True
    assert len(fsynced_fds) == 1
    # fd is process-local; can't assert exact value but it must be valid.
    assert fsynced_fds[0] >= 0


# ---------------------------------------------------------------------------
# Failure isolation.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tracer_failure_does_not_block_ledger_flush(tmp_path: Path) -> None:
    tracer = _FakeTracerProvider(raises=RuntimeError("BSP broken"))
    ctx = _ctx_with(tmp_path, tracer=tracer)

    report = await flush_observability(ctx)

    assert report.tracer_flushed is False
    assert report.ledger_fsynced is True
    assert report.failures == ("tracer",)


@pytest.mark.asyncio
async def test_ledger_failure_does_not_block_tracer_flush(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _broken_fsync(fd: int) -> None:
        raise OSError("fsync failed")

    monkeypatch.setattr(os, "fsync", _broken_fsync)
    tracer = _FakeTracerProvider(returns=True)
    ctx = _ctx_with(tmp_path, tracer=tracer)

    report = await flush_observability(ctx)

    assert report.tracer_flushed is True
    assert report.ledger_fsynced is False
    assert report.failures == ("ledger",)


@pytest.mark.asyncio
async def test_both_surfaces_fail_both_recorded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _broken_fsync(fd: int) -> None:
        raise OSError("fsync failed")

    monkeypatch.setattr(os, "fsync", _broken_fsync)
    tracer = _FakeTracerProvider(raises=RuntimeError("BSP broken"))
    ctx = _ctx_with(tmp_path, tracer=tracer)

    report = await flush_observability(ctx)

    assert report.tracer_flushed is False
    assert report.ledger_fsynced is False
    assert set(report.failures) == {"tracer", "ledger"}


# ---------------------------------------------------------------------------
# Timeout surfacing.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_force_flush_returning_false_surfaces_timed_out(tmp_path: Path) -> None:
    """OTel BSP returns False on internal timeout — propagate to FlushReport."""
    tracer = _FakeTracerProvider(returns=False)
    ctx = _ctx_with(tmp_path, tracer=tracer)

    report = await flush_observability(ctx, timeout_millis=100)

    assert report.tracer_flushed is False
    assert report.timed_out is True
    assert report.failures == ()  # not a failure — it's a timeout result


# ---------------------------------------------------------------------------
# Idempotency.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flush_observability_is_idempotent(tmp_path: Path) -> None:
    """Calling flush twice surfaces identical reports."""
    tracer = _FakeTracerProvider(returns=True)
    ctx = _ctx_with(tmp_path, tracer=tracer)

    r1 = await flush_observability(ctx)
    r2 = await flush_observability(ctx)

    assert r1 == r2
    assert tracer.calls == [30_000, 30_000]


# ---------------------------------------------------------------------------
# Typed surfaces.
# ---------------------------------------------------------------------------


def test_flush_timeout_error_is_timeout_subclass() -> None:
    assert issubclass(FlushTimeoutError, TimeoutError)


def test_flush_observability_package_root_re_export() -> None:
    import harness_runtime

    assert harness_runtime.flush_observability is flush_observability
    assert harness_runtime.FlushReport is FlushReport
    assert harness_runtime.FlushTimeoutError is FlushTimeoutError


# ===========================================================================
# U-RT-46 — shutdown() orchestrator tests.
# ===========================================================================


class _FakeCollectorDaemon:
    def __init__(self, *, raises: Exception | None = None, sleep: float = 0.0) -> None:
        self.stopped = False
        self._raises = raises
        self._sleep = sleep
        self.last_timeout: float | None = None

    async def stop(self, *, timeout_seconds: float = 5.0) -> None:
        self.last_timeout = timeout_seconds
        if self._sleep:
            await asyncio.sleep(self._sleep)
        if self._raises is not None:
            raise self._raises
        self.stopped = True


class _FakeTracerWithShutdown(_FakeTracerProvider):
    def __init__(
        self,
        *,
        returns: bool = True,
        raises: Exception | None = None,
        shutdown_raises: Exception | None = None,
    ) -> None:
        super().__init__(returns=returns, raises=raises)
        self.shutdown_called = False
        self._shutdown_raises = shutdown_raises

    def shutdown(self) -> None:
        self.shutdown_called = True
        if self._shutdown_raises is not None:
            raise self._shutdown_raises


class _FakeProvider:
    def __init__(self, *, raises: Exception | None = None) -> None:
        self.closed = False
        self._raises = raises

    async def aclose(self) -> None:
        if self._raises is not None:
            raise self._raises
        self.closed = True


class _FakeAuditWriter:
    """`response_hash` matches the real StateLedgerEntry schema (bytes per C-IS-05).

    Earlier fixture used `chain_hash=` against the (now-fixed) defect in
    `shutdown._read_audit_head_hash`; this aligns with the real schema.
    """

    def __init__(self, head_hash: str | None = "deadbeef") -> None:
        self._head = head_hash

    def read_all(self) -> list[object]:
        if self._head is None:
            return []
        return [SimpleNamespace(response_hash=bytes.fromhex(self._head))]


class _FakeCtx:
    """Plain class so weakref + WeakValueDictionary work (SimpleNamespace doesn't)."""


def _shutdown_ctx(
    tmp_path: Path,
    *,
    tracer: _FakeTracerWithShutdown,
    daemon: _FakeCollectorDaemon,
    providers: dict[str, _FakeProvider],
    audit: _FakeAuditWriter | None = None,
    ledger_path: Path | None = None,
) -> Any:
    path = ledger_path if ledger_path is not None else tmp_path / "state.jsonl"
    path.write_text("")
    handle = SimpleNamespace(canonical_path=path)
    ledger_writer = SimpleNamespace(handle=handle)
    ctx = _FakeCtx()
    ctx.tracer_provider = tracer  # type: ignore[attr-defined]
    ctx.ledger_writer = ledger_writer  # type: ignore[attr-defined]
    ctx.drained_flag = asyncio.Event()  # type: ignore[attr-defined]
    ctx.collector_daemon = daemon  # type: ignore[attr-defined]
    ctx.providers = providers  # type: ignore[attr-defined]
    ctx.audit_writer = audit if audit is not None else _FakeAuditWriter()  # type: ignore[attr-defined]
    return ctx


@pytest.fixture(autouse=True)
def _isolate_shutdown_registry() -> Any:  # pyright: ignore[reportUnusedFunction]
    """Reset module-level registries between tests."""
    shutdown_mod._shutdown_registry.clear()  # type: ignore[attr-defined]
    shutdown_mod._cached_reports.clear()  # type: ignore[attr-defined]
    yield
    shutdown_mod._shutdown_registry.clear()  # type: ignore[attr-defined]
    shutdown_mod._cached_reports.clear()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Schema.
# ---------------------------------------------------------------------------


def test_shutdown_report_is_frozen() -> None:
    flush = FlushReport(
        tracer_flushed=True,
        ledger_fsynced=True,
        cost_chain_noop=True,
        timed_out=False,
        failures=(),
    )
    report = ShutdownReport(
        flush=flush,
        already_shutdown=False,
        timed_out=False,
        failures=(),
        audit_ledger_head_hash="deadbeef",
    )
    with pytest.raises(ValidationError):
        report.already_shutdown = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Happy path + per-step verification.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_happy_path(tmp_path: Path) -> None:
    tracer = _FakeTracerWithShutdown(returns=True)
    daemon = _FakeCollectorDaemon()
    providers = {"anthropic": _FakeProvider(), "openai": _FakeProvider()}
    ctx = _shutdown_ctx(tmp_path, tracer=tracer, daemon=daemon, providers=providers)

    report = await shutdown(ctx)

    assert report.already_shutdown is False
    assert report.timed_out is False
    assert report.failures == ()
    assert report.audit_ledger_head_hash == "deadbeef"
    assert report.flush.tracer_flushed is True
    assert report.flush.ledger_fsynced is True
    assert daemon.stopped is True
    assert tracer.shutdown_called is True
    assert all(p.closed for p in providers.values())


@pytest.mark.asyncio
async def test_shutdown_step_1_sets_drained_flag(tmp_path: Path) -> None:
    tracer = _FakeTracerWithShutdown()
    ctx = _shutdown_ctx(tmp_path, tracer=tracer, daemon=_FakeCollectorDaemon(), providers={})
    assert ctx.drained_flag.is_set() is False

    await shutdown(ctx)

    assert ctx.drained_flag.is_set() is True


@pytest.mark.asyncio
async def test_shutdown_delegates_step_2_to_flush(tmp_path: Path) -> None:
    """The report's inner `flush` field is populated by flush_observability."""
    tracer = _FakeTracerWithShutdown(returns=True)
    ctx = _shutdown_ctx(tmp_path, tracer=tracer, daemon=_FakeCollectorDaemon(), providers={})
    await shutdown(ctx, timeout=2.0)
    # force_flush should have been called with ~2000ms budget (allow drift).
    assert len(tracer.calls) == 1
    assert 0 < tracer.calls[0] <= 2_000


@pytest.mark.asyncio
async def test_shutdown_stops_collector_daemon(tmp_path: Path) -> None:
    daemon = _FakeCollectorDaemon()
    ctx = _shutdown_ctx(
        tmp_path,
        tracer=_FakeTracerWithShutdown(),
        daemon=daemon,
        providers={},
    )
    await shutdown(ctx, timeout=5.0)
    assert daemon.stopped is True
    assert daemon.last_timeout is not None
    assert daemon.last_timeout >= 0


@pytest.mark.asyncio
async def test_shutdown_invokes_tracer_shutdown_via_to_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sync OTel `shutdown()` must be dispatched off the event loop."""
    to_thread_targets: list[object] = []

    real_to_thread = asyncio.to_thread

    async def _spy(fn: object, *args: object) -> object:
        to_thread_targets.append(fn)
        return await real_to_thread(fn, *args)  # type: ignore[arg-type]

    monkeypatch.setattr(asyncio, "to_thread", _spy)
    tracer = _FakeTracerWithShutdown(returns=True)
    ctx = _shutdown_ctx(tmp_path, tracer=tracer, daemon=_FakeCollectorDaemon(), providers={})

    await shutdown(ctx)

    # to_thread was used for both flush (force_flush) AND shutdown.
    assert tracer.shutdown_called is True
    assert len(to_thread_targets) >= 2  # force_flush + shutdown


@pytest.mark.asyncio
async def test_shutdown_acloses_each_provider(tmp_path: Path) -> None:
    providers = {
        "anthropic": _FakeProvider(),
        "openai": _FakeProvider(),
        "ollama": _FakeProvider(),
    }
    ctx = _shutdown_ctx(
        tmp_path,
        tracer=_FakeTracerWithShutdown(),
        daemon=_FakeCollectorDaemon(),
        providers=providers,
    )
    await shutdown(ctx)
    assert all(p.closed for p in providers.values())


# ---------------------------------------------------------------------------
# Per-resource failure isolation.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_continues_past_collector_failure(tmp_path: Path) -> None:
    tracer = _FakeTracerWithShutdown()
    providers = {"anthropic": _FakeProvider()}
    ctx = _shutdown_ctx(
        tmp_path,
        tracer=tracer,
        daemon=_FakeCollectorDaemon(raises=RuntimeError("daemon broken")),
        providers=providers,
    )

    report = await shutdown(ctx)

    assert "collector_daemon" in report.failures
    assert tracer.shutdown_called is True  # tracer still closed
    assert providers["anthropic"].closed is True  # provider still closed


@pytest.mark.asyncio
async def test_shutdown_continues_past_tracer_shutdown_failure(tmp_path: Path) -> None:
    daemon = _FakeCollectorDaemon()
    providers = {"anthropic": _FakeProvider()}
    ctx = _shutdown_ctx(
        tmp_path,
        tracer=_FakeTracerWithShutdown(shutdown_raises=RuntimeError("tracer broken")),
        daemon=daemon,
        providers=providers,
    )

    report = await shutdown(ctx)

    assert "tracer_provider" in report.failures
    assert daemon.stopped is True
    assert providers["anthropic"].closed is True


@pytest.mark.asyncio
async def test_shutdown_per_provider_failure_granularity(tmp_path: Path) -> None:
    providers = {
        "anthropic": _FakeProvider(),
        "openai": _FakeProvider(raises=RuntimeError("openai broken")),
        "ollama": _FakeProvider(),
    }
    ctx = _shutdown_ctx(
        tmp_path,
        tracer=_FakeTracerWithShutdown(),
        daemon=_FakeCollectorDaemon(),
        providers=providers,
    )

    report = await shutdown(ctx)

    assert "provider:openai" in report.failures
    assert "provider:anthropic" not in report.failures
    assert "provider:ollama" not in report.failures
    assert providers["anthropic"].closed is True
    assert providers["ollama"].closed is True


# ---------------------------------------------------------------------------
# Idempotency.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_idempotent_second_call_returns_cached(tmp_path: Path) -> None:
    daemon = _FakeCollectorDaemon()
    tracer = _FakeTracerWithShutdown()
    providers = {"anthropic": _FakeProvider()}
    ctx = _shutdown_ctx(tmp_path, tracer=tracer, daemon=daemon, providers=providers)

    r1 = await shutdown(ctx)
    # Reset side-effect markers; second call must NOT re-invoke closes.
    daemon.stopped = False
    tracer.shutdown_called = False
    providers["anthropic"].closed = False

    r2 = await shutdown(ctx)

    assert r1.already_shutdown is False
    assert r2.already_shutdown is True
    # Apart from the flag, the body is identical.
    assert r2.flush == r1.flush
    assert r2.failures == r1.failures
    assert r2.audit_ledger_head_hash == r1.audit_ledger_head_hash
    # Close primitives NOT re-invoked.
    assert daemon.stopped is False
    assert tracer.shutdown_called is False
    assert providers["anthropic"].closed is False


@pytest.mark.asyncio
async def test_shutdown_cached_report_freed_on_ctx_gc(tmp_path: Path) -> None:
    """weakref.finalize discards the cached report when ctx is gc'd."""
    import gc

    ctx = _shutdown_ctx(
        tmp_path,
        tracer=_FakeTracerWithShutdown(),
        daemon=_FakeCollectorDaemon(),
        providers={},
    )
    ctx_id = id(ctx)
    await shutdown(ctx)
    assert ctx_id in shutdown_mod._cached_reports  # type: ignore[attr-defined]

    del ctx
    gc.collect()

    assert ctx_id not in shutdown_mod._cached_reports  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Timeout / audit head / typed errors.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_records_audit_ledger_head(tmp_path: Path) -> None:
    audit = _FakeAuditWriter(head_hash="cafef00d")
    ctx = _shutdown_ctx(
        tmp_path,
        tracer=_FakeTracerWithShutdown(),
        daemon=_FakeCollectorDaemon(),
        providers={},
        audit=audit,
    )
    report = await shutdown(ctx)
    assert report.audit_ledger_head_hash == "cafef00d"


@pytest.mark.asyncio
async def test_shutdown_audit_head_none_on_empty_ledger(tmp_path: Path) -> None:
    audit = _FakeAuditWriter(head_hash=None)
    ctx = _shutdown_ctx(
        tmp_path,
        tracer=_FakeTracerWithShutdown(),
        daemon=_FakeCollectorDaemon(),
        providers={},
        audit=audit,
    )
    report = await shutdown(ctx)
    assert report.audit_ledger_head_hash is None


@pytest.mark.asyncio
async def test_shutdown_audit_head_from_real_state_ledger_entry(tmp_path: Path) -> None:
    """Regression test for the 2026-05-20 U-RT-46 chain_hash fix.

    Earlier `_read_audit_head_hash` read `entry.chain_hash` — an attribute
    `StateLedgerEntry` doesn't expose. Real entries have `response_hash:
    Bytes32` per C-IS-05 / C-IS-06. The fake fixture masked this. This
    test builds an entry with the real schema and asserts the helper
    returns the lowercase hex of `response_hash`.
    """
    from harness_is.entry_hash import compute_response_hash
    from harness_is.state_ledger_entry_schema import (
        Actor,
        ActorClass,
        Identifier,
        StateLedgerEntry,
    )

    actor = Actor(actor_class=ActorClass.AGENT, actor_id="harness-runtime")
    # Build a draft to compute response_hash, then a real entry.
    draft = StateLedgerEntry(
        action_id=Identifier("action-1"),
        idempotency_key=Identifier("idem-1"),
        actor=actor,
        response_hash=b"\x00" * 32,  # placeholder; recomputed below
        timestamp=0,
        prior_event_hash=b"\x00" * 32,
    )
    entry = draft.model_copy(update={"response_hash": compute_response_hash(draft)})

    class _RealishAudit:
        def read_all(self) -> list[object]:
            return [entry]

    ctx = _shutdown_ctx(
        tmp_path,
        tracer=_FakeTracerWithShutdown(),
        daemon=_FakeCollectorDaemon(),
        providers={},
        audit=_RealishAudit(),  # type: ignore[arg-type]
    )
    report = await shutdown(ctx)

    assert report.audit_ledger_head_hash == entry.response_hash.hex()
    assert len(report.audit_ledger_head_hash) == 64  # SHA-256 hex


# ---------------------------------------------------------------------------
# U-RT-48 — pidfile removal at end of shutdown().
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_removes_pidfile(tmp_path: Path) -> None:
    """U-RT-48: shutdown() removes the pidfile written by stage 7 per spec §13."""
    from harness_runtime.admin.pidfile import write_pidfile

    pidfile = tmp_path / ".harness/runtime.pid"
    write_pidfile(pidfile, 12345)
    assert pidfile.exists()

    class _ConfigStub:
        repository_root = tmp_path
        pidfile_path = None

    ctx = _shutdown_ctx(
        tmp_path,
        tracer=_FakeTracerWithShutdown(),
        daemon=_FakeCollectorDaemon(),
        providers={},
    )
    ctx.config = _ConfigStub()  # type: ignore[attr-defined]

    report = await shutdown(ctx)

    assert not pidfile.exists()
    assert "pidfile" not in report.failures


@pytest.mark.asyncio
async def test_shutdown_pidfile_removal_idempotent_on_missing(
    tmp_path: Path,
) -> None:
    """Removing an already-gone pidfile is a clean no-op (no failure recorded)."""

    class _ConfigStub:
        repository_root = tmp_path
        pidfile_path = None

    ctx = _shutdown_ctx(
        tmp_path,
        tracer=_FakeTracerWithShutdown(),
        daemon=_FakeCollectorDaemon(),
        providers={},
    )
    ctx.config = _ConfigStub()  # type: ignore[attr-defined]

    # No pidfile written; shutdown should still succeed.
    report = await shutdown(ctx)
    assert "pidfile" not in report.failures


@pytest.mark.asyncio
async def test_shutdown_timed_out_when_collector_slow(tmp_path: Path) -> None:
    """Collector exhausts budget — `timed_out=True` after deadline check."""
    daemon = _FakeCollectorDaemon(sleep=0.05)
    ctx = _shutdown_ctx(
        tmp_path,
        tracer=_FakeTracerWithShutdown(),
        daemon=daemon,
        providers={},
    )
    # Tight budget — collector sleeps 50ms, budget is 20ms.
    report = await shutdown(ctx, timeout=0.02)
    assert report.timed_out is True


def test_shutdown_timeout_is_timeout_subclass() -> None:
    assert issubclass(ShutdownTimeout, TimeoutError)


def test_already_shutdown_is_exception_subclass() -> None:
    assert issubclass(AlreadyShutDown, Exception)
    assert not issubclass(AlreadyShutDown, NotImplementedError)


def test_shutdown_package_root_re_export() -> None:
    import harness_runtime

    assert harness_runtime.shutdown is shutdown
    assert harness_runtime.ShutdownReport is ShutdownReport
    assert harness_runtime.ShutdownTimeout is ShutdownTimeout
    assert harness_runtime.AlreadyShutDown is AlreadyShutDown
