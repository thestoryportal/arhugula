"""B-EFFECT-FENCE (runtime spec §14.22 C-RT-31) — at-most-once execution tests.

Proves the durable effect fence is NON-VACUOUS (the live trap this workspace
keeps re-hitting — a wired-but-production-dead surface, cf. B-TOOL-GATE #653):

  * `RuntimeEffectFence` mechanics — first claim wins, any re-claim loses, and the
    claim is DURABLE across a fresh instance (a restarted process — the genuine
    crash-then-resume witness). Output capture/read round-trips + fails closed on
    absent/corrupt.
  * B-EFFECT-FENCE-HITL-ROUTE (§14.22 two-case split, v1.72): a re-dispatch of the
    same effect that lost the reserve SPLITS on the captured output. Output present
    (the first dispatch fired + captured before commit) → SUPPRESS-AND-CONTINUE:
    the sink returns the captured result, NEVER re-firing the tool body (EXACTLY
    ONCE across two dispatches with the same per-(run, step, tool) idempotency key).
    Output absent/corrupt (a crash in the fire→capture window) →
    `EffectFenceAmbiguousUncommittedError` (the driver routes it to a §26.2 PAUSE /
    FAILED — proven at the harness-cp driver layer). NEVER auto-re-fire either way.
  * The NEGATIVE CONTROL: without the fence, the same two dispatches double-fire
    the tool — the window `_determine_resume_at` cannot close on its own (the
    prefix-skip protects only COMMITTED steps; the effect fires inside the step,
    the per-step ledger commit lands after dispatch returns, so a crash in between
    re-dispatches the effected-but-uncommitted step).
  * The fresh-dispatcher-over-the-same-fence-dir test is the genuine no-proxy
    crash-then-resume proof: a SECOND dispatcher instance (a restarted process)
    re-dispatching the same effect reads the on-disk captured output and
    suppress-and-continues against the durable claim.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from harness_as.sandbox_tier import BlastRadiusTier, SandboxTier
from harness_as.tool_contract import ToolContract
from harness_core import PersonaTier
from harness_cp.cp_shared_types import MCPTrustTier, ModelBinding
from harness_cp.engine_class import EngineClass
from harness_cp.gate_level_rule import GateLevel
from harness_cp.mcp_client_namespace_emitter import (
    MCPClientNamespaceEmitter,
    MCPServerInfo,
)
from harness_cp.pause_resume_protocol_types import (
    EffectFenceResolution,
    EffectFenceResolutionDirective,
)
from harness_cp.per_server_trust_evaluator import PerServerTrustEvaluator
from harness_cp.per_server_trust_types import TierDerivationRule, TrustPolicy
from harness_cp.per_step_override_evaluator import StepEffectiveBinding
from harness_cp.workflow_driver_types import (
    StepExecutionContext,
    StepKind,
    WorkflowStep,
)
from harness_is.state_ledger_entry_schema import Actor, ActorClass
from harness_runtime.lifecycle.effect_fence import (
    EffectFenceAbortedError,
    EffectFenceAmbiguousUncommittedError,
    RuntimeEffectFence,
)
from harness_runtime.lifecycle.mcp_client_host import MCPClientHost
from harness_runtime.lifecycle.runtime_tool_dispatcher import (
    RuntimeToolDispatcher,
    SandboxDispatchDecision,
)
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session

# ---------- fence-unit tests -----------------------------------------------


def test_try_reserve_first_wins_second_loses(tmp_path: Path) -> None:
    fence = RuntimeEffectFence(fence_dir=tmp_path / "fence")
    assert fence.try_reserve("effect-key-1") is True  # fresh → won → fire
    assert fence.try_reserve("effect-key-1") is False  # re-claim → lost → fail-close


def test_try_reserve_distinct_keys_independent(tmp_path: Path) -> None:
    """Run-scoping (finding O-E3b-1): a different run derives a different
    idempotency key → a disjoint claim namespace, so a fresh run of the same
    workflow is never falsely fenced by a prior run's claims."""
    fence = RuntimeEffectFence(fence_dir=tmp_path / "fence")
    assert fence.try_reserve("run-A:step-0:tool") is True
    assert fence.try_reserve("run-B:step-0:tool") is True  # distinct key → won
    assert fence.try_reserve("run-A:step-0:tool") is False  # the run-A claim persists


def test_fresh_fence_instance_sees_prior_claim(tmp_path: Path) -> None:
    """Crash-survival: the claim is on disk, so a NEW fence instance over the same
    directory (a restarted process) loses the re-claim — the durability the
    at-most-once guarantee rests on."""
    fence_dir = tmp_path / "fence"
    assert RuntimeEffectFence(fence_dir=fence_dir).try_reserve("k") is True
    # Simulate a process restart: a brand-new instance over the same directory.
    assert RuntimeEffectFence(fence_dir=fence_dir).try_reserve("k") is False


# ---------- output capture/read unit tests (B-EFFECT-FENCE-HITL-ROUTE) ------


def test_capture_output_then_read_round_trips(tmp_path: Path) -> None:
    """`capture_output` persists a validated output that `read_output` returns
    verbatim — and durably across a fresh instance (a restarted process)."""
    fence_dir = tmp_path / "fence"
    payload = {"did": "fire", "nested": [1, 2, {"k": "v"}]}
    RuntimeEffectFence(fence_dir=fence_dir).capture_output("k", payload)
    # A brand-new instance over the same directory reads the on-disk output.
    assert RuntimeEffectFence(fence_dir=fence_dir).read_output("k") == payload


def test_read_output_absent_returns_none(tmp_path: Path) -> None:
    """No captured output for the key → None (the ambiguous case the dispatcher
    maps to a PAUSE/FAILED, never a suppress source)."""
    fence = RuntimeEffectFence(fence_dir=tmp_path / "fence")
    fence.try_reserve("k")  # reserve taken, but NO output captured (fire→capture crash)
    assert fence.read_output("k") is None


def test_read_output_corrupt_returns_none(tmp_path: Path) -> None:
    """A present-but-corrupt output fails closed to None (defensive — a torn write
    is NEVER a valid suppress source; `[[durable-recovery-presence-validity-scope]]`)."""
    fence_dir = tmp_path / "fence"
    fence = RuntimeEffectFence(fence_dir=fence_dir)
    fence.capture_output("k", {"did": "fire"})
    output_file = next(iter(fence_dir.glob("*.output")))
    output_file.write_bytes(b"{not valid json")
    assert fence.read_output("k") is None


# ---------- real-dispatcher sink fixtures ----------------------------------


def _counting_server(fired: list[str]) -> FastMCP:
    server = FastMCP(name="dispatcher-test-srv")

    @server.tool(description="non-idempotent side effect")
    def do_effect(message: str) -> str:
        fired.append(message)  # the genuine effect — counts every real fire
        return f"did: {message}"

    return server


def _session_factory(server: FastMCP):
    @asynccontextmanager
    async def factory():
        async with create_connected_server_and_client_session(
            server, raise_exceptions=True
        ) as session:
            yield session

    return factory


def _tool_converter(tool):
    return ToolContract(
        name=tool.name,
        description=tool.description or "",
        input_schema=tool.inputSchema or {"type": "object"},
        output_schema={"type": "object"},
        minimum_tier=SandboxTier.TIER_1_PROCESS,
        blast_radius_tier=BlastRadiusTier.READ_ONLY,
    )


async def _build_started_counting_host(fired: list[str]) -> MCPClientHost:
    host = MCPClientHost(
        transport="stdio",
        server_name="dispatcher-test-srv",
        trust_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        transport_config={"command": "unused"},
        tool_contract_converter=_tool_converter,
        session_context_factory=_session_factory(_counting_server(fired)),
        auth_present=False,
    )
    await host.start()
    return host


def _emitter() -> MCPClientNamespaceEmitter:
    def lookup(_server_name: str) -> MCPServerInfo:
        return MCPServerInfo(
            transport="stdio",
            protocol_version="2025-06-18",
            auth_present=False,
            trust_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        )

    return MCPClientNamespaceEmitter(info_lookup=lookup)


def _sandbox_resolver(_contract, _step):
    return SandboxDispatchDecision(
        tier=SandboxTier.TIER_1_PROCESS,
        tech="host",
        provider="host",
        assigned_tier_reason="test",
        cost_tier_overhead_ms=1,
    )


def _trust_policy() -> TrustPolicy:
    return TrustPolicy(
        default_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
        require_audit_below_tier=MCPTrustTier.LEVEL_3_ALLOW_WITH_AUDIT,
        allow_list=frozenset({"dispatcher-test-srv"}),
        deny_list=frozenset(),
        per_server_overrides={},
        tier_derivation=TierDerivationRule.CONSERVATIVE,
    )


def _dispatcher(host: MCPClientHost, fence: RuntimeEffectFence | None) -> RuntimeToolDispatcher:
    return RuntimeToolDispatcher.for_single_host(
        mcp_client_host=host,
        per_server_trust_evaluator=PerServerTrustEvaluator(),
        mcp_namespace_emitter=_emitter(),
        trust_policy=_trust_policy(),
        sandbox_decision_resolver=_sandbox_resolver,
        effect_fence=fence,
        # B-EFFECT-FENCE-DURABLE-AUTO — these original #655 tests assert the fence
        # fires when SUPPLIED (the pre-arc "fence present → reserve" semantic). Post-
        # arc the per-run gate also requires explicit-opt-in OR a durable run engine
        # class; supplying a fence here maps to the explicit opt-in (fence every step),
        # preserving the original intent. (The durable-AUTO path is covered by the new
        # dispatcher witnesses in test_lifecycle_runtime_tool_dispatcher.py.)
        effect_fencing_explicit=fence is not None,
    )


def _step_context(
    effect_fence_resolution: EffectFenceResolutionDirective | None = None,
) -> StepExecutionContext:
    return StepExecutionContext(
        workflow_id="wf-1",
        parent_action_id="workflow:wf-1:step:0",
        parent_gate_level=GateLevel.AUTO,
        parent_sandbox_tier=SandboxTier.TIER_1_PROCESS,
        parent_actor=Actor(actor_class=ActorClass.OPERATOR, actor_id="harness-runtime"),
        parent_entry_hash="",
        # The run-scoped per-step key the driver computes at `:1985`
        # (`_compute_step_idempotency_key(run_idempotency_key, step_index)`),
        # byte-identical across a fresh run and its resume re-dispatch.
        parent_idempotency_key="run-1:step-0",
        tenant_id=None,
        step_index=0,
        effect_fence_resolution=effect_fence_resolution,
    )


def _binding() -> StepEffectiveBinding:
    return StepEffectiveBinding(
        step_id="step-1",
        model_binding=ModelBinding(provider="anthropic", model="claude-opus-4-7"),
        engine_class=EngineClass.SAVE_POINT_CHECKPOINT,
        override_applied=False,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
    )


def _step() -> WorkflowStep:
    return WorkflowStep(
        step_id="step-1",
        step_kind=StepKind.TOOL_STEP,
        step_payload={"tool_id": "do_effect", "tool_args": {"message": "fire"}},
    )


# ---------- real-dispatcher sink tests -------------------------------------


@pytest.mark.asyncio
async def test_dispatcher_redispatch_suppress_and_continues(tmp_path: Path) -> None:
    """THE deliverable (B-EFFECT-FENCE-HITL-ROUTE): the first dispatch fires +
    captures its validated output; a re-dispatch of the same effect loses the
    reserve, reads the captured output, and SUPPRESS-AND-CONTINUEs — returning the
    captured result WITHOUT re-firing. The tool body fires EXACTLY ONCE."""
    fired: list[str] = []
    fence = RuntimeEffectFence(fence_dir=tmp_path / "fence")
    host = await _build_started_counting_host(fired)
    dispatcher = _dispatcher(host, fence)
    try:
        result = await dispatcher.dispatch(_binding(), _step(), step_context=_step_context())
        assert result["tool_id"] == "do_effect"  # first dispatch fired
        # The resume re-dispatch (same key): the effect already fired + captured,
        # so the sink returns the captured output rather than re-firing.
        result2 = await dispatcher.dispatch(_binding(), _step(), step_context=_step_context())
    finally:
        await host.shutdown()
    assert fired == ["fire"]  # at-most-once — the second dispatch never fired
    assert result2["response"] == result["response"]  # suppress returns the captured output
    assert result2["tool_id"] == "do_effect"


@pytest.mark.asyncio
async def test_dispatcher_redispatch_ambiguous_when_output_absent(tmp_path: Path) -> None:
    """The ambiguous case: the reserve was taken but NO output captured (a crash in
    the fire→capture window). A re-dispatch raises
    `EffectFenceAmbiguousUncommittedError` (the driver routes it to PAUSE/FAILED) —
    NEVER re-firing."""
    fired: list[str] = []
    fence_dir = tmp_path / "fence"
    fence = RuntimeEffectFence(fence_dir=fence_dir)
    host = await _build_started_counting_host(fired)
    dispatcher = _dispatcher(host, fence)
    try:
        await dispatcher.dispatch(_binding(), _step(), step_context=_step_context())
        # Simulate a crash AFTER the effect fired but BEFORE the output fsync'd:
        # delete the captured output, leaving only the reserve claim.
        next(iter(fence_dir.glob("*.output"))).unlink()
        with pytest.raises(EffectFenceAmbiguousUncommittedError):
            await dispatcher.dispatch(_binding(), _step(), step_context=_step_context())
    finally:
        await host.shutdown()
    assert fired == ["fire"]  # at-most-once — the ambiguous re-dispatch never re-fired


@pytest.mark.asyncio
async def test_dispatcher_redispatch_ambiguous_when_output_corrupt(tmp_path: Path) -> None:
    """Defensive negative control: a present-but-corrupt captured output is NOT a
    valid suppress source — the re-dispatch fails closed to
    `EffectFenceAmbiguousUncommittedError`, never re-firing."""
    fired: list[str] = []
    fence_dir = tmp_path / "fence"
    fence = RuntimeEffectFence(fence_dir=fence_dir)
    host = await _build_started_counting_host(fired)
    dispatcher = _dispatcher(host, fence)
    try:
        await dispatcher.dispatch(_binding(), _step(), step_context=_step_context())
        next(iter(fence_dir.glob("*.output"))).write_bytes(b"{torn")
        with pytest.raises(EffectFenceAmbiguousUncommittedError):
            await dispatcher.dispatch(_binding(), _step(), step_context=_step_context())
    finally:
        await host.shutdown()
    assert fired == ["fire"]  # at-most-once — corrupt output never auto-re-fired


@pytest.mark.asyncio
async def test_dispatcher_without_fence_double_fires(tmp_path: Path) -> None:
    """NEGATIVE CONTROL: without the fence the same two dispatches BOTH fire — the
    crash-between-effect-and-commit window is real and unguarded. Proves the fence
    is load-bearing (this test fails-to-double-fire only because no fence is bound)."""
    fired: list[str] = []
    host = await _build_started_counting_host(fired)
    dispatcher = _dispatcher(host, fence=None)
    try:
        await dispatcher.dispatch(_binding(), _step(), step_context=_step_context())
        await dispatcher.dispatch(_binding(), _step(), step_context=_step_context())
    finally:
        await host.shutdown()
    assert fired == ["fire", "fire"]  # DOUBLE-FIRE — the gap the fence closes


@pytest.mark.asyncio
async def test_dispatcher_fence_survives_process_restart(tmp_path: Path) -> None:
    """No-proxy crash-then-resume: a SECOND dispatcher instance (a restarted
    process) over the SAME on-disk fence directory re-dispatching the same effect
    reads the DURABLE captured output and suppress-and-continues — the effect fires
    once across the crash, and the restarted run proceeds with the captured result."""
    fence_dir = tmp_path / "fence"
    fired: list[str] = []

    host1 = await _build_started_counting_host(fired)
    dispatcher1 = _dispatcher(host1, RuntimeEffectFence(fence_dir=fence_dir))
    try:
        result1 = await dispatcher1.dispatch(_binding(), _step(), step_context=_step_context())
    finally:
        await host1.shutdown()

    # Restart: a brand-new dispatcher + fence instance over the same directory.
    host2 = await _build_started_counting_host(fired)
    dispatcher2 = _dispatcher(host2, RuntimeEffectFence(fence_dir=fence_dir))
    try:
        result2 = await dispatcher2.dispatch(_binding(), _step(), step_context=_step_context())
    finally:
        await host2.shutdown()

    assert fired == ["fire"]  # at-most-once across the process restart
    assert result2["response"] == result1["response"]  # durable captured output suppresses


# ---------- clear_claim unit (the RE_FIRE substrate) -----------------------


def test_clear_claim_removes_held_claim_then_reserve_wins(tmp_path: Path) -> None:
    """`clear_claim` removes the REAL held claim file (not a no-op) so a subsequent
    `try_reserve` WINS — the RE_FIRE substrate (B-EFFECT-FENCE-PAUSE-RESOLUTION).
    Witnessed at the file level AND behaviorally."""
    fence_dir = tmp_path / "fence"
    fence = RuntimeEffectFence(fence_dir=fence_dir)
    assert fence.try_reserve("k") is True  # claim held
    assert fence.try_reserve("k") is False  # held → a re-reserve LOSES
    assert list(fence_dir.glob("*.claim"))  # the claim file EXISTS (held)
    fence.clear_claim("k")
    assert not list(fence_dir.glob("*.claim"))  # clear_claim REMOVED the real file
    assert fence.try_reserve("k") is True  # cleared → a fresh reserve WINS (re-fire fresh)


def test_clear_claim_also_removes_captured_output(tmp_path: Path) -> None:
    """`clear_claim` removes any captured output too, so a re-fire's fresh
    `capture_output` is not shadowed by a stale/corrupt prior output."""
    fence = RuntimeEffectFence(fence_dir=tmp_path / "fence")
    fence.try_reserve("k")
    fence.capture_output("k", {"did": "fire"})
    assert fence.read_output("k") == {"did": "fire"}
    fence.clear_claim("k")
    assert fence.read_output("k") is None  # output gone with the claim


def test_clear_claim_missing_ok(tmp_path: Path) -> None:
    """`clear_claim` is idempotent — clearing an absent claim is a no-op (a re-resume
    that already cleared + re-fired)."""
    fence = RuntimeEffectFence(fence_dir=tmp_path / "fence")
    fence.clear_claim("never-claimed")  # no raise
    assert fence.try_reserve("never-claimed") is True


# ---------- resume-side resolution witnesses (B-EFFECT-FENCE-PAUSE-RESOLUTION) ----


async def _ambiguous_state(dispatcher: RuntimeToolDispatcher, fence_dir: Path) -> str:
    """Drive the dispatcher to the ambiguous state: the first dispatch fires +
    captures, then delete the output file → claim held, NO output (the crash-in-
    fire→capture-window state). Return the held reserve's idempotency_key (off the
    first dispatch's result wrapper) so a witness can key-bind a resolution to it."""
    result = await dispatcher.dispatch(_binding(), _step(), step_context=_step_context())
    next(iter(fence_dir.glob("*.output"))).unlink()
    return result["idempotency_key"]


@pytest.mark.asyncio
async def test_resolution_re_fire_clears_claim_and_fires_fresh(tmp_path: Path) -> None:
    """RE_FIRE: the operator asserts the effect did NOT fire → the dispatcher clears
    the held claim and fires the effect FRESH (a first-and-only execution). The tool
    body fires AGAIN (vs the ambiguous-raise without a resolution)."""
    fired: list[str] = []
    fence_dir = tmp_path / "fence"
    host = await _build_started_counting_host(fired)
    dispatcher = _dispatcher(host, RuntimeEffectFence(fence_dir=fence_dir))
    try:
        key = await _ambiguous_state(dispatcher, fence_dir)
        directive = EffectFenceResolutionDirective(
            resolution=EffectFenceResolution.RE_FIRE, idempotency_key=key
        )
        result2 = await dispatcher.dispatch(
            _binding(), _step(), step_context=_step_context(directive)
        )
    finally:
        await host.shutdown()
    assert fired == ["fire", "fire"]  # RE_FIRE re-fired fresh (the claim was cleared)
    assert result2["tool_id"] == "do_effect"


@pytest.mark.asyncio
async def test_resolution_skip_as_fired_empty_output_no_refire(tmp_path: Path) -> None:
    """SKIP_AS_FIRED: the operator asserts the effect FIRED; its output is
    unrecoverable → proceed with EMPTY output, NEVER re-fire."""
    fired: list[str] = []
    fence_dir = tmp_path / "fence"
    host = await _build_started_counting_host(fired)
    dispatcher = _dispatcher(host, RuntimeEffectFence(fence_dir=fence_dir))
    try:
        key = await _ambiguous_state(dispatcher, fence_dir)
        directive = EffectFenceResolutionDirective(
            resolution=EffectFenceResolution.SKIP_AS_FIRED, idempotency_key=key
        )
        result2 = await dispatcher.dispatch(
            _binding(), _step(), step_context=_step_context(directive)
        )
    finally:
        await host.shutdown()
    assert fired == ["fire"]  # SKIP_AS_FIRED never re-fired
    assert result2["response"] == {}  # empty output (the lost output is unrecoverable)
    assert result2["tool_id"] == "do_effect"


@pytest.mark.asyncio
async def test_resolution_abort_raises_aborted_error_no_refire(tmp_path: Path) -> None:
    """ABORT: the operator cannot determine whether the effect fired → the dispatcher
    raises `EffectFenceAbortedError` (the driver maps it to FAILED); never re-fires."""
    fired: list[str] = []
    fence_dir = tmp_path / "fence"
    host = await _build_started_counting_host(fired)
    dispatcher = _dispatcher(host, RuntimeEffectFence(fence_dir=fence_dir))
    try:
        key = await _ambiguous_state(dispatcher, fence_dir)
        directive = EffectFenceResolutionDirective(
            resolution=EffectFenceResolution.ABORT, idempotency_key=key
        )
        with pytest.raises(EffectFenceAbortedError):
            await dispatcher.dispatch(_binding(), _step(), step_context=_step_context(directive))
    finally:
        await host.shutdown()
    assert fired == ["fire"]  # ABORT never re-fired


@pytest.mark.asyncio
async def test_resolution_key_mismatch_is_ignored(tmp_path: Path) -> None:
    """Key-bind correctness: a resolution whose `idempotency_key` does NOT match the
    dispatch's recomputed key is IGNORED — the fence behaves as if no resolution
    (ambiguous → raise), so a stale resolution can never mis-apply to a different
    fenced effect."""
    fired: list[str] = []
    fence_dir = tmp_path / "fence"
    host = await _build_started_counting_host(fired)
    dispatcher = _dispatcher(host, RuntimeEffectFence(fence_dir=fence_dir))
    try:
        await _ambiguous_state(dispatcher, fence_dir)
        directive = EffectFenceResolutionDirective(
            resolution=EffectFenceResolution.RE_FIRE, idempotency_key="a-different-effect-key"
        )
        with pytest.raises(EffectFenceAmbiguousUncommittedError):
            await dispatcher.dispatch(_binding(), _step(), step_context=_step_context(directive))
    finally:
        await host.shutdown()
    assert fired == ["fire"]  # mismatched resolution ignored → ambiguous, never re-fired


@pytest.mark.asyncio
async def test_resolution_re_fire_retry_does_not_double_fire(tmp_path: Path) -> None:
    """Codex [P1] regression: a RE_FIRE wrapped by `RetryBreakerToolDispatcher` must NOT
    clear-and-re-fire on EACH retry attempt. The FIRST RE_FIRE attempt wins the durable
    consume-once latch + clears + fires fresh; a SECOND dispatch with the SAME RE_FIRE
    `step_context` (modeling a retry after a transient error before capture) LOSES the
    latch → does NOT re-clear → loses the reserve to the re-fire's own claim → ambiguous
    (NEVER a third fire). Without the latch, the unconditional clear would re-fire."""
    fired: list[str] = []
    fence_dir = tmp_path / "fence"
    host = await _build_started_counting_host(fired)
    dispatcher = _dispatcher(host, RuntimeEffectFence(fence_dir=fence_dir))
    try:
        key = await _ambiguous_state(dispatcher, fence_dir)  # original fire #1, then output deleted
        ctx = _step_context(
            EffectFenceResolutionDirective(
                resolution=EffectFenceResolution.RE_FIRE, idempotency_key=key
            )
        )
        # Attempt 1 (RE_FIRE): wins the latch → clears the stale claim → fires fresh (#2).
        await dispatcher.dispatch(_binding(), _step(), step_context=ctx)
        # Model a retryable error BEFORE capture on the re-fire: delete its output,
        # leaving the re-fire's claim held + no output.
        next(iter(fence_dir.glob("*.output"))).unlink()
        # Attempt 2 (the retry, SAME RE_FIRE ctx): the latch is consumed → no re-clear →
        # ambiguous, NOT a second re-fire.
        with pytest.raises(EffectFenceAmbiguousUncommittedError):
            await dispatcher.dispatch(_binding(), _step(), step_context=ctx)
    finally:
        await host.shutdown()
    # The original fire + exactly ONE re-fire — the retry did NOT double-fire.
    assert fired == ["fire", "fire"]
