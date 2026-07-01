"""`harness_runtime.run` Python API + `RunResult` shape (U-RT-42; U-RT-43 wires bootstrap).

Per `Spec_Harness_Runtime_v1.md` v1.1 §8 (C-RT-08 — `run()` Python API
contract; F-P2-2 absorption) + §9 (C-RT-09 — `RunResult` shape) + §14
(C-RT-14 — runtime-local fail-class taxonomy; FailureCause mirror).

**Closes L8 stage 5 LOOP_INIT.** U-RT-42 lands the operator-facing
ingress signature + result schema. Per the spec §16 #4 decided posture
(async-only at Track A), `run` is the single Track A entry. The
bootstrap orchestrator body lands at U-RT-43; this module wires the
pre-bootstrap ingress (workflow validation + concurrency guard) and
stubs the bootstrap call so the bootstrap-not-yet-landed path is a
typed surface.

**Resolves the standing entrypoint design gap.** The Class 1 record at
`.harness/class_1_tension_runtime_entrypoint_design_gap.md` (filed
2026-05-16) documented the absence of an entrypoint signature in the
design corpus. `Spec_Harness_Runtime_v1.md` v1.1 (filed Phase 2
Session 4) is the operator resolution; U-RT-42 implements that
resolution at the package root.

**WorkflowObject typing decision (Option C — runtime-local Protocol).**
Spec C-RT-08 Risk surface enumerates 3 options for the `WorkflowObject`
typed source: (a) CP spec extension, (b) `harness-core` carrier,
(c) runtime structural Protocol. "The choice is made at U-RT-42
landing time, not now." This landing picks Option C — smallest blast
radius, doesn't touch CP or harness-core, satisfies the C-RT-08
signature requirement, defers cross-axis type negotiation until a
caller surfaces a real `WorkflowObject` shape. Runtime-local
`WorkflowObject` Protocol declared here.

**CostAttribution spec-vs-OD-type drift (Class 3 informational).** Spec
C-RT-09 names the `cost_attribution` field type as `CostAttribution
(OD type)`. The OD axis exports no type literally named
`CostAttribution`; the closest OD-materialized aggregate is
`harness_od.cross_family_rollup.CrossFamilyCostRollup` (C-OD-15 §15.1
— aggregated cost rollup along one `RollupAxis`). This landing types
the field as `tuple[CrossFamilyCostRollup, ...]` — the natural
materialized shape of the spec's "aggregated 5-step cost-attribution
rollup". **RESOLVED at runtime spec v1.53 §9 C-RT-09 (R-FS-1 arc CA):**
the spec Type cell is reconciled to `tuple[CrossFamilyCostRollup, ...]`
and the run-result rollup axis is named `PER_PROVIDER_AND_MODEL` (the
field is now populated at `_build_run_result`, no longer the v1.4 empty
tuple).

**Workflow execution landed at Lane 6 (2026-05-20).** `run()` now
delegates workflow body execution to `harness_cp.workflow_driver.
execute_workflow()` (C-CP-25 §25) per `Spec_Harness_Runtime_v1.md` §11
risk-surface guidance ("If CP later surfaces a native drain primitive,
refactor harness-runtime to delegate drain to CP — this contract
becomes a thin adapter"). The `WorkflowExecutionNotYetLandedError`
stub surface is removed at this landing. `run()` owns the full
bootstrap → execute → shutdown lifecycle per C-RT-08 + C-RT-10. The
synchronous CP driver is offloaded to a worker thread via
`asyncio.to_thread` so the asyncio loop remains responsive to signal
handlers — this composition is what materializes U-RT-44 AC #2's
in-flight step bounded-wait (drain flag set by signal → driver returns
DRAINED at next boundary → to_thread future resolves). The
`WorkflowObject` Protocol grows with 4 new read-only properties
(`manifest_entry`, `steps`, `default_model_binding`)
per the §8 risk-surface "growth is non-breaking when fields are
optional or read-only" authorization; Path A operator-ratified
2026-05-20. Closes `[[fork-u-rt-44-workflow-loop-drain]]` for U-RT-44
AC #2 + U-RT-49 state-ledger / lifecycle-event ACs. Residual:
cost-attribution AC stays STRUCK pending U-OD-21 fork resolution.

**Concurrency guard via module-level `asyncio.Lock`.** Per C-RT-08
v1.1 idempotency-and-concurrency invariant: "Concurrent invocations
from the same process surface typed `ConcurrentRunNotSupported` — the
second concurrent call detects an existing in-flight `HarnessContext`
(via process-local lock initialized at module import) and fails fast
before stage 0." The lock is module-level; acquired non-blocking by
`run()`; release in `finally` so a failed bootstrap doesn't permanently
poison the lock.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast, runtime_checkable

from harness_core.identity import WorkflowID
from harness_core.workload_class import WorkloadClass
from harness_cp.cp_shared_types import ModelBinding
from harness_cp.pause_resume_protocol_types import PauseSnapshot, ResumeContext
from harness_cp.workflow_driver_types import RunResult as _CpRunResult
from harness_cp.workflow_driver_types import (
    RunStatus as _CpRunStatus,
)
from harness_cp.workflow_driver_types import StepKind, WorkflowStep
from harness_cp.workflow_manifest_entry import WorkflowManifestEntry
from harness_od.cross_family_rollup import (
    CrossFamilyCostRollup,
    RollupAxis,
    rollup_costs_by_axis,
)
from harness_od.idempotency_join_dedup import SpanCostRecord
from pydantic import BaseModel, ConfigDict

from harness_runtime.types import COST_ACCUM_VAR, CostRecordAccumulator, RuntimeConfig

if TYPE_CHECKING:
    from harness_runtime.lifecycle.mcp_server import (
        HarnessMCPServer as _ConcreteHarnessMCPServer,
    )

# ---------------------------------------------------------------------------
# Typed errors (C-RT-08 + C-RT-14).
# ---------------------------------------------------------------------------


class InvalidWorkflowError(Exception):
    """`RT-FAIL-INVALID-WORKFLOW` — pre-bootstrap workflow-type rejection."""


class ConcurrentRunNotSupported(Exception):  # noqa: N818 — domain-anchored name
    """`RT-FAIL-CONCURRENT-RUN` — second concurrent `run()` detected (C-RT-08 v1.1)."""


class HarnessDraining(Exception):  # noqa: N818 — domain-anchored name
    """`RT-FAIL-HARNESS-DRAINING` — `run()` invoked after process-level drain set.

    Per `Spec_Harness_Runtime_v1.md` v1.1 §11 C-RT-11: "After flag-set,
    `harness_runtime.run(...)` rejects new invocations with typed
    `HarnessDraining` error." The process-level drain flag at
    `harness_runtime.drain._process_drained` is one-way; a new harness
    invocation requires process restart.

    Raised pre-bootstrap before the module-level `_run_lock` is acquired
    so a drained process surfaces the typed error without constructing a
    new `HarnessContext`.
    """


class ResumeProtocolNotBoundError(Exception):
    """`RT-FAIL-RESUME-PROTOCOL-NOT-BOUND` — `resume()` called without the
    pause/resume protocol opted in (C-RT-35).

    Resume requires `config.pause_resume_protocol_config` (the same opt-in
    that produced the pause). Without it the driver's entry-point resume
    detection is inert and the workflow would **silently re-run from step 0**
    — re-executing already-completed prefix steps + their side effects.
    Detect-then-refuse: `resume()` fails fast rather than silently re-running.
    """


class ResumeWorkflowMismatchError(Exception):
    """`RT-FAIL-RESUME-WORKFLOW-MISMATCH` — `pause_snapshot.workflow_id` does
    not match the resumed `workflow.workflow_id` (C-RT-35).

    A snapshot's `snapshot_hash` validates against its OWN embedded fields, so
    a snapshot from workflow A would otherwise be applied (A's `run_id` +
    `step_index`) against workflow B's steps — skipping B's prefix or reporting
    success for the wrong workflow. Detect-then-refuse before bootstrap.
    """


class ResumeStepIndexOutOfRangeError(Exception):
    """`RT-FAIL-RESUME-STEP-INDEX-OUT-OF-RANGE` — `pause_snapshot.step_index`
    is not a valid step of the resumed `workflow` (C-RT-35).

    If the supplied workflow changed since the pause so `step_index` is `< 0`
    or `>= len(workflow.steps)`, the driver's `resume_at_step_index` would slice
    `steps[resume_at:]` to empty and return a **successful completed run that
    executed nothing** — a silent false-success. Detect-then-refuse before
    bootstrap.
    """


class ResumeArgsError(Exception):
    """`RT-FAIL-RESUME-ARGS` — `resume()` was not given exactly one snapshot
    source (C-RT-35, R-CC-1 arc #3 cascade step 2).

    `resume()` accepts EITHER a caller-supplied `pause_snapshot` (cascade step 1)
    OR a `resume_handle` that the harness reads back from its own durable store
    (cascade step 2) — exactly one, never both, never neither. A `resume_handle`
    additionally requires the durable opt-in
    (`pause_resume_protocol_config.durable=True`); without it no harness-owned
    store exists to read. Detect-then-refuse before bootstrap.
    """


class ResumeHandleUnknownError(Exception):
    """`RT-FAIL-RESUME-HANDLE-UNKNOWN` — no durable `PauseSnapshot` is journaled
    for the supplied `resume_handle` (C-RT-35, R-CC-1 arc #3 cascade step 2).

    The harness-owned `JournalWorkflowPauseStore` (co-located under the resolved
    `STATE_LEDGER` dir) has no record for the workflow_id, or its latest record
    is corrupt (fail-closed read → `None`). Detect-then-refuse before bootstrap
    rather than silently re-run from step 0.
    """


# ---------------------------------------------------------------------------
# `WorkflowObject` runtime-local Protocol (Option C per spec §8 Risk surface).
# ---------------------------------------------------------------------------


@runtime_checkable
class WorkflowObject(Protocol):
    """Structural workflow-object surface (C-RT-08 Option C resolution).

    **Growth at Lane 6 (2026-05-20) — Path A (operator-ratified).** Per the
    spec §8 risk surface's "growth is non-breaking when fields are optional
    or read-only" authorization, this Protocol grows from the U-RT-43
    minimum-viable shape (`workflow_id` + `workload_class`) to carry the
    surface the CP workflow driver (`harness_cp.workflow_driver.execute_
    workflow`, C-CP-25 §25) needs. Callers pre-Lane-6 still conform if
    they implement the four new read-only properties; `run()` now
    delegates execution to the CP driver rather than raising
    `WorkflowExecutionNotYetLandedError`.

    All six surfaces are `@property` (read-only), satisfying the
    non-breaking-growth invariant. The driver consumes the substrate
    via runtime-local Protocols on `HarnessContext` — those Protocols
    are at `harness_cp.workflow_driver.{LedgerWriterLike,
    LifecycleEventEmitterLike, DriverContext}` and are structurally
    satisfied by the bootstrapped context.
    """

    @property
    def workflow_id(self) -> str:
        """Stable identity of the workflow being executed."""
        ...

    @property
    def workload_class(self) -> WorkloadClass:
        """The workflow's workload class — threaded into bootstrap stage 1
        state-ledger composition + stage 3b routing-manifest residence."""
        ...

    @property
    def manifest_entry(self) -> WorkflowManifestEntry:
        """CP workflow manifest entry per C-CP-06 §6.1.

        Carries `engine_class`, `topology_pattern`, per-step overrides,
        fallback chain, HITL placements, layer budgets. Consumed by the
        CP driver at C-CP-25 §25.3.1 validation + §25.6 replay-resumption
        composition + §25.5 lifecycle-event filtering.
        """
        ...

    @property
    def steps(self) -> Sequence[WorkflowStep]:
        """Step sequence in declaration order (CP spec v1.4 §25 amendment §E).

        Decoupled from `manifest_entry` per the operator-ratified Path A
        decision at U-CP-56 land time: manifest carries config; steps
        carry the declarative body. The CP driver iterates this sequence
        under SINGLE_THREADED_LINEAR topology (no parallel branching at
        v1.4).
        """
        ...

    @property
    def default_model_binding(self) -> ModelBinding:
        """Default `(provider, model)` binding for steps without per-step
        override (C-CP-06 §6.2 + C-CP-13 §13.3 lead-agent binding)."""
        ...


# ---------------------------------------------------------------------------
# `FailureCause` (C-RT-09 deferred-to-discretion; C-RT-14 mirror per spec).
# ---------------------------------------------------------------------------


class FailureCause(BaseModel):
    """`RunResult.failure_cause` typed shape (C-RT-09 + C-RT-14 mirror).

    Spec C-RT-09 says "Deferred to implementation discretion;
    alternatively reuse C-RT-14 runtime-local fail-class set." This
    landing reuses the C-RT-14 set as a string-tagged record. CP-side
    workflow-step failures (per `validator_fail_taxonomy`) compose
    through `validator_fail_class` when they bubble through the runtime
    boundary; pre-bootstrap and shutdown-side failures use the
    runtime-local tag.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    runtime_fail_class: str
    """The `RT-FAIL-*` tag from C-RT-14 (e.g. `'RT-FAIL-INVALID-WORKFLOW'`)."""

    detail: str
    """Operator-readable failure detail (no PII / secret content per X-AL-3)."""

    validator_fail_class: str | None = None
    """CP-side validator-fail tag, populated when the failure bubbled through
    CP's workflow-step validator (per C-CP-05 §5 5-class set)."""


# ---------------------------------------------------------------------------
# `RunResult` (C-RT-09).
# ---------------------------------------------------------------------------


class RunResult(BaseModel):
    """Terminal `run()` result schema (C-RT-09).

    Frozen Pydantic v2; `arbitrary_types_allowed=True` to carry the
    `CrossFamilyCostRollup` rollup tuple (a Pydantic v2 model). Field
    invariants per C-RT-09 — `status='failed'` implies
    `failure_cause is not None`; `audit_ledger_head_hash` always
    present.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    status: Literal["completed", "drained", "failed", "paused", "partial"]
    """Terminal status of the workflow execution.

    `'paused'` (C-RT-35, R-CC-1 arc #3) is a non-terminal outcome: a
    workflow-layer pause (DURABLE_ASYNC HITL gate / EXPLICIT_OPERATOR) was
    captured; `pause_snapshot` is populated for `harness_runtime.resume(...)`.
    Type-widen of the existing Literal — minor bump per C-RT-09 §9.

    `'partial'` (U-RT-113, R-FS-1 B1 §9) is the graceful-degradation outcome:
    a `proceed`-cascade fan-out run (CP `RunStatus.PARTIAL`) where ≥1 branch
    failed but the run aggregated a partial result. `failure_cause` stays
    `None` (a degraded run did not fail — `status=='partial'` is the single
    source of truth; no `degraded` field). Minor type-widen mirroring
    `'paused'` per C-RT-09 §9."""

    workflow_id: WorkflowID
    """Identity of the executed workflow."""

    terminal_state: dict[str, Any]
    """Workflow's terminal state object per CP lifecycle loop contract.
    May be `{}` for trivial workflows."""

    audit_ledger_head_hash: str
    """Post-execution audit-ledger head hash (hex) for verification.
    Always present per C-RT-09 invariants."""

    trace_ids: tuple[str, ...]
    """Root span trace IDs emitted by the workflow execution."""

    cost_attribution: tuple[CrossFamilyCostRollup, ...]
    """Per-run cost rollup along `RollupAxis.PER_PROVIDER_AND_MODEL` (C-OD-15
    §15.1), computed at `_build_run_result` from the run-scoped accumulated
    `SpanCostRecord`s via `rollup_costs_by_axis`. `()` when no cost-bearing
    dispatch occurred; single axis ⟹ `sum(e.total_cost)` is the total run cost.

    Runtime spec v1.53 §9 C-RT-09 (R-FS-1 arc CA) reconciled the spec Type cell
    from the phantom `CostAttribution (OD type)` (OD exports no type literally
    named `CostAttribution`) to this materialized `tuple[CrossFamilyCostRollup,
    ...]`, and named the axis. The orthogonal dispatch-type breakdown lives in
    `cost_attribution_by_dispatch_kind` (runtime spec v1.57,
    `B-COST-DISCRIMINATOR-TAXONOMY`).
    """

    cost_attribution_by_dispatch_kind: tuple[CrossFamilyCostRollup, ...] = ()
    """Per-run cost rollup along `RollupAxis.PER_DISPATCH_KIND` (OD spec v1.30
    §15.1) — the operator-meaningful dispatch-type breakdown (llm/tool/validator/
    webhook), computed at `_build_run_result` via `rollup_costs_by_axis`. `()`
    when no cost-bearing dispatch occurred.

    Runtime spec v1.57 §9 C-RT-09 (R-FS-1 `B-COST-DISCRIMINATOR-TAXONOMY`).
    Orthogonal to `cost_attribution` (per-(provider, model)): each record has
    exactly one `dispatch_kind`, so this is a separate single-axis partition of
    the same total ⟹ `sum(e.total_cost)` independently equals the total run cost
    (no double-count — kept in a separate field, not concatenated). Optional with
    default `()` (minor bump per the §9 version-evolution invariant — the v1.45
    `pause_snapshot` precedent).
    """

    cost_attribution_by_provider_discriminator: tuple[CrossFamilyCostRollup, ...] = ()
    """Per-run cost rollup along `RollupAxis.PER_PROVIDER_DISCRIMINATOR` (OD spec
    v1.2 §15.1 / §15.3) — the cross-family family-tag breakdown (`frontier_managed`
    / `frontier_managed_alt` / `local_ollama`), giving per-family cost visibility
    under fallback. Computed at `_build_run_result` via `rollup_costs_by_axis`.

    Runtime spec v1.58 §9 C-RT-09 (R-FS-1 `B-FALLBACK-CHAIN-FAMILY-COST-COMPOSITION`).
    Populated by the LLM-dispatch cost path tagging each record's
    `provider_discriminator` with the dispatched provider's family
    (`cross_family_tag_for_provider`). **Unlike `cost_attribution` /
    `cost_attribution_by_dispatch_kind` (full-run partitions), this axis
    partitions only the LLM subtotal** — tool / validator / webhook records carry
    `provider_discriminator = None` and are skipped, so `sum(e.total_cost)` equals
    the LLM-dispatch cost, not the total run cost. `()` when no LLM dispatch
    occurred. Optional with default `()` (minor bump — the v1.45 `pause_snapshot`
    precedent).
    """

    failure_cause: FailureCause | None = None
    """`None` unless `status == 'failed'` (C-RT-09 invariant)."""

    pause_snapshot: PauseSnapshot | None = None
    """The captured workflow-layer `PauseSnapshot` when `status == 'paused'`
    (C-RT-35, R-CC-1 arc #3); `None` otherwise. The caller persists it and
    passes it to `harness_runtime.resume(workflow, pause_snapshot=...)` to
    continue the workflow after a process restart (workflow-layer
    durable-resume). Optional field with default — minor bump per C-RT-09 §9."""


# ---------------------------------------------------------------------------
# Module-level concurrency lock (C-RT-08 v1.1 idempotency-and-concurrency).
# ---------------------------------------------------------------------------


_run_lock: asyncio.Lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# U-RT-62 AC #5 — in-process MCP tool invocation helper.
# ---------------------------------------------------------------------------


async def _refuse_elicitation_in_api_run(context: Any, params: Any) -> Any:
    """Elicitation callback for `api.run()`'s in-process ClientSession.

    Per the operator-ratified Q2+Q3 reading at the C-RT-18 v1.12 fork:
    `api.run()` is the carrier-preservation surface for the 4 existing
    test callsites importing `from harness_runtime.api import run`.
    Production HITL routes via Claude Code as the registered MCP client
    per Reading α; `api.run()` is NOT structurally an HITL-bearing
    entry point at v1.12 MVP.

    If a workflow invoked via `api.run()` fires a HITL gate, this callback
    runs on the in-process ClientSession side. We respond with `decline`
    + a clear rejection reason — the composer at step 4i REJECT branch
    maps this to a controlled workflow termination (rather than a hang
    or silent absorption). Future arcs may bind a richer test-fixture
    callback to exercise the elicitation surface from api.run-driven
    integration tests.
    """
    _ = (context, params)
    from mcp.shared.context import (
        RequestContext,  # noqa: F401  # pyright: ignore[reportUnusedImport] — type-pin
    )
    from mcp.types import ElicitResult

    return ElicitResult(
        action="decline",
        content=None,
    )


async def _invoke_run_workflow_via_in_process_mcp(
    fastmcp_server: Any,
    workflow_id: str,
) -> _CpRunResult:
    """Open an in-memory MCP client session against `fastmcp_server` +
    call the `run_workflow` tool with `workflow_id`.

    Per U-RT-62 AC #5 + the MCP SDK 1.27.1 `create_connected_server_and_
    client_session` in-memory transport helper. Returns the unmarshalled
    CP `RunResult`; raises `MCPToolError` (passes through) on any
    non-result tool-side exception.

    The tool body returns a JSON-serializable dict via
    `cp_result.model_dump(mode="json")`; FastMCP 1.27.1 places the
    serialization at `CallToolResult.content[0].text` (TextContent, JSON
    string). `structuredContent` is None at this transport — we parse
    the text payload directly via `_CpRunResult.model_validate_json`.
    """
    import json

    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(
        fastmcp_server,
        elicitation_callback=_refuse_elicitation_in_api_run,
        raise_exceptions=True,
    ) as session:
        tool_result = await session.call_tool("run_workflow", {"workflow_id": workflow_id})

    if tool_result.isError:
        # Tool body raised. FastMCP serializes the error text into
        # `content[0].text`; surface as a runtime error (the existing
        # api.run semantics propagate driver-side exceptions).
        # `content[0]` is an mcp content union; `.text` exists only on
        # TextContent. The bare ignore suppresses the union access (mcp types
        # are loosely typed), leaving the value Unknown — pin it to str.
        error_text: str = (  # pyright: ignore[reportUnknownVariableType]
            tool_result.content[0].text  # type: ignore[union-attr]  # pyright: ignore[reportUnknownMemberType]
            if tool_result.content
            else "unknown tool error"
        )
        raise RuntimeError(
            f"run_workflow MCP tool raised inside in-process invocation: {error_text}"
        )

    if not tool_result.content:
        raise RuntimeError(
            "run_workflow MCP tool returned empty content; expected "
            "TextContent with JSON-serialized RunResult dict"
        )

    text_block = tool_result.content[0]
    payload_text = getattr(text_block, "text", None)
    if payload_text is None:
        raise RuntimeError(
            f"run_workflow MCP tool returned non-text content: {type(text_block).__name__}"
        )

    # FastMCP wraps dict returns in a top-level JSON object; verify the
    # shape matches the CP RunResult schema rather than the dict wrapper.
    parsed = json.loads(payload_text)
    # FastMCP 1.27.1 returns the dict as-is (not wrapped in {"result": ...});
    # the v1.11 baseline test substrate validates this assumption via
    # _CpRunResult.model_validate which raises on schema mismatch.
    return _CpRunResult.model_validate(parsed)


# ---------------------------------------------------------------------------
# `run()` entry point (C-RT-08).
# ---------------------------------------------------------------------------

# Runtime spec v1.47 §2.1 — the inference-need step kinds (those that reach an
# LLM provider): INFERENCE_STEP → ctx.llm_dispatcher, SUB_AGENT_DISPATCH →
# ctx.sub_agent_dispatcher, and POST_JOIN_SYNTHESIS → PostJoinSynthesisStepDispatcher
# (runtime §14.24 C-RT-33, bound under requires_inference — it LLM-composes the
# fan-out siblings). A workflow whose fan-out branches are DECLARATIVE/TOOL but
# whose terminal POST_JOIN_SYNTHESIS is the only LLM step still requires a
# provider; omitting it here would skip provider setup + the registry row →
# StepKindDispatcherNotBoundError at the synthesis dispatch (out-of-family Codex [P1]).
_INFERENCE_STEP_KINDS = frozenset(
    {StepKind.INFERENCE_STEP, StepKind.SUB_AGENT_DISPATCH, StepKind.POST_JOIN_SYNTHESIS}
)


def _workflow_requires_inference(workflow: WorkflowObject) -> bool:
    """Runtime spec v1.47 §2.1 inference-need predicate.

    A workflow is inference-bearing iff it contains a step whose `StepKind`
    reaches an LLM provider (`INFERENCE_STEP` / `SUB_AGENT_DISPATCH` /
    `POST_JOIN_SYNTHESIS`). `DECLARATIVE_STEP` / `TOOL_STEP` / `HITL_STEP` never
    reach a provider. The predicate is **exact** — it reads the same
    `workflow.steps` the CP driver dispatches through the frozen
    `{StepKind → StepDispatcher}` registry, so it cannot under-count an inference
    need (no false negatives). When `False`, the bootstrap requires no provider
    (tool-only workflows run provider-free).
    """
    return any(step.step_kind in _INFERENCE_STEP_KINDS for step in workflow.steps)


async def run(
    workflow: WorkflowObject,
    *,
    config: RuntimeConfig | None = None,
) -> RunResult:
    """Execute one workflow end-to-end (C-RT-08 Track A operator-facing API).

    Bootstrap → execute → shutdown per C-RT-02. Async-only at Track A
    per spec §16 #4 (decided 2026-05-19). Single workflow per call;
    multi-workflow ingest is Track B (out of scope).

    Raises
    ------
    InvalidWorkflowError
        `RT-FAIL-INVALID-WORKFLOW` — `workflow` does not conform to the
        `WorkflowObject` structural Protocol. Pre-bootstrap rejection;
        no `HarnessContext` constructed.
    ConcurrentRunNotSupported
        `RT-FAIL-CONCURRENT-RUN` — a second `run()` invocation detected
        the module-level lock held by an in-flight call. Caller
        serializes or moves to a cached-context entry point (Track B).
    harness_runtime.bootstrap.BootstrapFailure
        `RT-FAIL-BOOTSTRAP` — one of the 9 bootstrap stages raised;
        stages 0..N-1 rolled back in reverse order. Original cause
        attached.
    """
    # Pre-bootstrap drain check (C-RT-11 surface (3)). Checked before
    # `_run_lock` acquisition so a drained process surfaces `HarnessDraining`
    # without constructing a new `HarnessContext`. Import lazily so the
    # api.py → drain.py edge stays at runtime (drain.py is a leaf module).
    from harness_runtime.drain import is_process_drained

    if is_process_drained():
        raise HarnessDraining(
            "process-level drain flag set (SIGTERM/SIGINT received in a "
            "prior `run()` invocation); spec §11 invariant: the flag is "
            "one-way for process lifetime — a new invocation requires "
            "process restart."
        )
    if not isinstance(workflow, WorkflowObject):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise InvalidWorkflowError(
            f"`run()` requires a `WorkflowObject` (with `workflow_id`, "
            f"`workload_class`, `manifest_entry`, `steps`, "
            f"`default_model_binding` properties); got "
            f"{type(workflow).__name__!r}"
        )
    if _run_lock.locked():
        raise ConcurrentRunNotSupported(
            "a `run()` invocation is already in flight in this process; "
            "Track A is bootstrap-per-call (no cached context). Serialize "
            "calls or move to a cached-context entry point (Track B)."
        )
    # Lazy imports to keep api.py → bootstrap / shutdown edges one-way at
    # type-check time and to avoid bootstrap-time import cost when
    # `InvalidWorkflowError` / `ConcurrentRunNotSupported` fire pre-bootstrap.
    from harness_runtime.bootstrap import run_bootstrap
    from harness_runtime.shutdown import shutdown as _shutdown

    async with _run_lock:
        resolved_config = config if config is not None else _default_config()
        # B-INTERSTEP-PERRUN-ISOLATION (runtime spec §14.21 C-RT-34 invariant 7) —
        # establish THIS run's isolated cost accumulator in `COST_ACCUM_VAR` for
        # the whole run. The set propagates into the `run_workflow` tool handler +
        # its `asyncio.to_thread` worker (so the per-dispatch cost wrappers, which
        # thread the ctx accumulator PROXY, append HERE), and the post-run cost
        # read below resolves the SAME accumulator (a handler-set value would NOT
        # propagate back up to this frame; a caller-set one DOES propagate down).
        # The `finally` reset prevents the var leaking into a later direct-stage
        # test on a reused task (where the proxy must fall back to its default).
        _cost_token = COST_ACCUM_VAR.set(CostRecordAccumulator())
        try:
            ctx = await run_bootstrap(
                resolved_config,
                workload_class=workflow.workload_class,
                requires_inference=_workflow_requires_inference(workflow),
            )
            try:
                # U-RT-62 AC #5 — thin-wrapper reframe per spec v1.12 §14.8.3
                # workflow-initiation topology pin (Reading α CC-initiates). The
                # operator-facing `api.run()` symbol is preserved as carrier-
                # preservation per Q2 ratification at the C-RT-18 v1.12 fork;
                # the body now invokes the MCP-tool path via an in-process
                # `ClientSession` against `ctx.mcp_server.server` (the FastMCP
                # server materialized at bootstrap stage 2 per AC #2). This
                # exercises the same H_T-as-MCP-server hosting topology that
                # production CC→`run_workflow` invocation uses, so the e2e
                # contract is unified at criterion B verification per spec
                # v1.12 §14.8.3 v1.12 RETIRE-READY → RETIRED gate (AC #6).
                #
                # The tool body internalizes the asyncio.to_thread → wait_for
                # composition + the workflow.step_dispatchers override fallback;
                # api.run's role here is bootstrap → workflow registration →
                # in-process tool call → CP result parse → shutdown.
                assert ctx.mcp_server is not None, (
                    "ctx.mcp_server is None post-bootstrap — stage 2 AS did not "
                    "materialize the FastMCP server per U-RT-62 AC #2"
                )
                # `ctx.mcp_server` is typed via the empty `HarnessMCPServer` Protocol
                # (the concrete is not re-exported to keep Pydantic forward-refs
                # clean); narrow to the concrete here to reach its `_state` /
                # `workflow_registry` / `server` surface.
                mcp_server = cast("_ConcreteHarnessMCPServer", ctx.mcp_server)
                # Bind the post-bootstrap context on the mutable holder so the
                # `run_workflow` tool body can reach `ctx.step_dispatchers` etc.
                mcp_server._state["_harness_ctx"] = ctx  # pyright: ignore[reportPrivateUsage]
                # Register the workflow keyed by workflow_id so the tool body
                # can look it up. WorkflowObject is a runtime-local structural
                # Protocol; not MCP-serializable. The serializable wire is the
                # `workflow_id` string only.
                mcp_server.workflow_registry[workflow.workflow_id] = workflow
                try:
                    cp_result = await _invoke_run_workflow_via_in_process_mcp(
                        mcp_server.server, workflow.workflow_id
                    )
                finally:
                    # Defensive cleanup — `_run_lock` serializes invocations but
                    # the registry + state holders MUST NOT carry stale entries
                    # across calls (Track B future cached-context entry point;
                    # pytest-asyncio loop reuse).
                    mcp_server.workflow_registry.pop(workflow.workflow_id, None)
                    mcp_server._state.pop("_harness_ctx", None)  # pyright: ignore[reportPrivateUsage]
                # Per AC #5 advisor reconcile pin — `timed_out` derived from the
                # CP result (the tool body absorbed the TimeoutError and emitted
                # a DRAINED CP result with `RT-FAIL-DRAIN-TIMEOUT` fail_class).
                timed_out = (
                    cp_result.status == _CpRunStatus.DRAINED
                    and cp_result.fail_class == "RT-FAIL-DRAIN-TIMEOUT"
                )
            finally:
                # Per C-RT-08 + C-RT-10: run() owns the full bootstrap → execute
                # → shutdown lifecycle. Shutdown after execute, regardless of CP
                # status. ShutdownReport carries the audit-ledger head hash.
                shutdown_report = await _shutdown(ctx)
            return _build_run_result(
                cp_result,
                shutdown_report,
                timed_out=timed_out,
                cost_records=ctx.cost_record_accumulator.records,
            )
        finally:
            COST_ACCUM_VAR.reset(_cost_token)


def _read_durable_pause_snapshot(
    config: RuntimeConfig,
    workflow: WorkflowObject,
    resume_handle: str,
) -> PauseSnapshot | None:
    """Read the latest durably-journaled `PauseSnapshot` for the handle (C-RT-35).

    Resolves the pause-journal directory the SAME way the stage-5 factory does at
    capture — `<STATE_LEDGER resolved dir>/pause-journal` for this workflow's
    `(workload_class, deployment_surface)` — and reads the latest record. Pure
    (no bootstrap side effects); `PathResolver.resolve_path` does not create the
    directory. Returns `None` (fail-closed) when no record exists or the latest
    record is corrupt → the caller raises `RT-FAIL-RESUME-HANDLE-UNKNOWN`.
    """
    from harness_is.path_class_registry import PathClass
    from harness_is.path_resolver import PathResolver

    from harness_runtime.config.path_bindings import build_path_binding
    from harness_runtime.lifecycle.journal_workflow_pause_store import (
        JournalWorkflowPauseStore,
        pause_journal_dir_for,
    )

    resolver = PathResolver(build_path_binding(config.path_bindings))
    state_ledger_dir = resolver.resolve_path(
        PathClass.STATE_LEDGER,
        workflow.workload_class,
        config.deployment_surface,
    )
    store = JournalWorkflowPauseStore(journal_dir=pause_journal_dir_for(state_ledger_dir))
    return store.read_latest(resume_handle)


async def resume(
    workflow: WorkflowObject,
    *,
    pause_snapshot: PauseSnapshot | None = None,
    resume_handle: str | None = None,
    resume_context: ResumeContext | None = None,
    config: RuntimeConfig | None = None,
) -> RunResult:
    """Resume a paused workflow — caller-supplied snapshot OR durable-store handle (C-RT-35).

    The workflow-layer durable-resume Track-A sibling of `run()` (R-CC-1
    arc #3). `run()` surfaces `RunResult(status='paused', pause_snapshot=...)`
    when a workflow-layer pause fires (DURABLE_ASYNC HITL gate / explicit
    operator pause). Resume continues the workflow from the paused step.
    Resume position is restored from the snapshot's `step_index`; for the linear
    / single-step execution model resume is position-only — data-stateless between
    steps, so no working-state rehydration is required (R-CC-1 design §1.1). The
    ONE exception is a `cascade_policy=pause` fan-out resume (B-FANOUT-PAUSE): the
    snapshot's `fan_out_resume` carries the completed-branch outputs (the §1.1 §6
    re-open trigger materialized for the fan-out case — the ledger carries
    causality + terminal_status, not the dispatch output), threaded OPAQUELY by
    this surface to the CP ORCHESTRATOR_WORKERS strategy via `pause_snapshot_input`.

    Two snapshot sources (supply EXACTLY ONE):

    - `pause_snapshot` (cascade step 1, #513) — the caller persisted the
      `PauseSnapshot` from a prior `RunResult.pause_snapshot` and passes it back.
    - `resume_handle` (cascade step 2) — the workflow_id; the harness reads the
      latest durably-journaled snapshot back from its own
      `JournalWorkflowPauseStore` (co-located under the resolved `STATE_LEDGER`
      dir). Requires the durable opt-in (`pause_resume_protocol_config.durable`).
      This is the crash-recovery surface: the caller need NOT have persisted the
      snapshot — even a process that died holding (and never serializing) the
      `RunResult` can resume by workflow_id.

    Like `run()`, this is bootstrap-per-call (a fresh `HarnessContext`): the
    fresh process re-bootstraps, the driver's entry-point resume detection
    (C-RT-24 §14.14.3 / `workflow_driver.py`) validates the snapshot via
    `attempt_resume(...)` and overrides `resume_at_step_index`, and execution
    continues. Resume admission anchor-validation is deferred (the MVP
    `pause_context_reader` returns a constant sentinel → no material diff →
    STRICT admits; the real anchor-reachability check is the U-CP-22 arc) — this
    holds identically for a fresh-bootstrap durable-store resume (the fresh
    ledger does not invalidate position-only resume; design §1.1 / §7b).

    Parameters
    ----------
    workflow
        The same `WorkflowObject` that produced the pause (re-supplied; the
        snapshot carries position, the workflow carries the steps).
    pause_snapshot
        The `PauseSnapshot` returned in a prior `RunResult.pause_snapshot`.
        Mutually exclusive with `resume_handle`.
    resume_handle
        The `workflow_id` to read the latest durable snapshot for, from the
        harness-owned store. Mutually exclusive with `pause_snapshot`; requires
        `config.pause_resume_protocol_config.durable=True`.
    resume_context
        Operator-supplied resume-time context (e.g. the HITL response the
        paused gate awaits); delivered one-shot to the resumed-step gate.
    config
        Runtime config; `None` → defaults + env per C-RT-03. MUST opt into
        the pause/resume protocol (`pause_resume_protocol_config`) — the same
        config that produced the pause.

    Raises
    ------
    ResumeArgsError
        `RT-FAIL-RESUME-ARGS` — not exactly one of `pause_snapshot` /
        `resume_handle`, or `resume_handle` without the durable opt-in.
    ResumeHandleUnknownError
        `RT-FAIL-RESUME-HANDLE-UNKNOWN` — no durable snapshot for the handle.
    InvalidWorkflowError
        `RT-FAIL-INVALID-WORKFLOW` — `workflow` is not a `WorkflowObject`.
    ConcurrentRunNotSupported
        `RT-FAIL-CONCURRENT-RUN` — a `run()`/`resume()` call is in flight.
    harness_runtime.bootstrap.BootstrapFailure
        `RT-FAIL-BOOTSTRAP` — a bootstrap stage raised.

    Notes
    -----
    A corrupt snapshot (snapshot_hash mismatch) or a material-diff abort
    surfaces as `RunResult(status='failed')` with the CP fail-class
    (`CP-FAIL-PAUSE-SNAPSHOT-CORRUPTION` / `CP-FAIL-RESUME-MATERIAL-DIFF-
    DETECTED`) on `failure_cause.validator_fail_class` — the driver returns
    FAILED before any step runs (`RT-FAIL-RESUME-*` family, C-RT-35).
    """
    from harness_runtime.drain import is_process_drained

    if is_process_drained():
        raise HarnessDraining(
            "process-level drain flag set in a prior `run()`/`resume()` "
            "invocation; spec §11 invariant: the flag is one-way for process "
            "lifetime — a new invocation requires process restart."
        )
    if not isinstance(workflow, WorkflowObject):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise InvalidWorkflowError(
            f"`resume()` requires a `WorkflowObject`; got {type(workflow).__name__!r}"
        )

    # Detect-then-refuse: EXACTLY ONE snapshot source. Both → ambiguous; neither
    # → nothing to resume. C-RT-35 cascade step 2.
    if (pause_snapshot is None) == (resume_handle is None):
        _supplied = "both" if pause_snapshot is not None else "neither"
        raise ResumeArgsError(
            "resume() requires exactly one of `pause_snapshot` (caller-supplied) "
            f"or `resume_handle` (harness durable-store read); got {_supplied} "
            "(C-RT-35)."
        )

    # Detect-then-refuse: resume REQUIRES the pause/resume opt-in (the same
    # config that produced the pause). Without it the driver's entry-point
    # resume detection is inert and the workflow would SILENTLY re-run from
    # step 0 — re-executing completed prefix steps + side effects. Fail fast
    # (pre-bootstrap) rather than silently re-run. C-RT-35.
    resolved_config = config if config is not None else _default_config()
    if resolved_config.pause_resume_protocol_config is None:
        raise ResumeProtocolNotBoundError(
            "resume() requires config.pause_resume_protocol_config (the "
            "pause/resume opt-in that produced the pause); without it the "
            "driver's resume detection is inert and the workflow would "
            "silently re-run from step 0 (C-RT-35 detect-then-refuse)."
        )

    # Concurrency guard BEFORE the durable-store read (Codex-caught, this arc):
    # the `resume_handle` path reads the shared pause journal, which an in-flight
    # `run()`/`resume()` mutates (under this same lock). Checking `_run_lock`
    # first means a concurrent call surfaces the documented
    # `ConcurrentRunNotSupported` rather than a spurious
    # `RT-FAIL-RESUME-HANDLE-UNKNOWN` from observing a partially-written / not-yet-
    # written snapshot. Correct because: (a) the store is only written while the
    # lock is held (capture fires inside `execute_workflow` under `_run_lock`),
    # and (b) the guard → store read → `async with _run_lock` segment below is
    # `await`-free, so under asyncio's cooperative scheduling no other coroutine
    # can acquire the lock (and thus write the store) between this check and our
    # own acquisition. C-RT-35.
    if _run_lock.locked():
        raise ConcurrentRunNotSupported(
            "a `run()`/`resume()` invocation is already in flight in this "
            "process; Track A is bootstrap-per-call. Serialize calls."
        )

    # Resolve the snapshot to resume from: the caller-supplied one, or the
    # latest record the harness durably journaled for the handle. C-RT-35.
    if resume_handle is not None:
        if not resolved_config.pause_resume_protocol_config.durable:
            raise ResumeArgsError(
                "resume_handle requires the durable opt-in "
                "(pause_resume_protocol_config.durable=True); without it the "
                "harness owns no snapshot store to read (C-RT-35)."
            )
        snapshot = _read_durable_pause_snapshot(resolved_config, workflow, resume_handle)
        if snapshot is None:
            raise ResumeHandleUnknownError(
                f"no durable PauseSnapshot journaled for resume_handle="
                f"{resume_handle!r} under the resolved STATE_LEDGER pause-journal "
                f"(C-RT-35)."
            )
    else:
        assert pause_snapshot is not None  # exactly-one-of guard guarantees this
        snapshot = pause_snapshot

    # Detect-then-refuse: a snapshot's hash validates against its own embedded
    # fields, so a snapshot from another workflow would otherwise be applied
    # (its run_id + step_index) against THIS workflow's steps. C-RT-35.
    if snapshot.workflow_id != workflow.workflow_id:
        raise ResumeWorkflowMismatchError(
            f"snapshot.workflow_id={snapshot.workflow_id!r} != "
            f"workflow.workflow_id={workflow.workflow_id!r}; a snapshot may only "
            f"resume its own workflow (C-RT-35)."
        )
    # Detect-then-refuse: a step_index outside the supplied workflow's steps
    # (the workflow changed since the pause) would slice `steps[resume_at:]`
    # to empty → a silent SUCCESS that executed nothing. C-RT-35.
    _step_count = len(workflow.steps)
    if not (0 <= snapshot.step_index < _step_count):
        raise ResumeStepIndexOutOfRangeError(
            f"snapshot.step_index={snapshot.step_index} is not a "
            f"valid step of the resumed workflow (0 <= i < {_step_count}); the "
            f"workflow may have changed since the pause (C-RT-35)."
        )

    from harness_runtime.bootstrap import run_bootstrap
    from harness_runtime.shutdown import shutdown as _shutdown

    async with _run_lock:
        # B-INTERSTEP-PERRUN-ISOLATION — isolate this resume's cost accumulator in
        # `COST_ACCUM_VAR` for the whole run (mirrors `run()`); the post-run cost
        # read resolves the SAME accumulator the wrappers appended to, and the
        # `finally` reset prevents var leakage into a later direct-stage test.
        _cost_token = COST_ACCUM_VAR.set(CostRecordAccumulator())
        try:
            ctx = await run_bootstrap(
                resolved_config,
                workload_class=workflow.workload_class,
                requires_inference=_workflow_requires_inference(workflow),
            )
            try:
                assert ctx.mcp_server is not None, (
                    "ctx.mcp_server is None post-bootstrap — stage 2 AS did not "
                    "materialize the FastMCP server per U-RT-62 AC #2"
                )
                mcp_server = cast("_ConcreteHarnessMCPServer", ctx.mcp_server)
                mcp_server._state["_harness_ctx"] = ctx  # pyright: ignore[reportPrivateUsage]
                # In-process resume handoff (NOT over the MCP wire) — the
                # `run_workflow` tool reads these from `_state`, mirroring how
                # `_harness_ctx` is passed. Presence of `_resume_pause_snapshot`
                # switches the tool to the resume path (snapshot.run_id continuity
                # + `pause_snapshot_input=` to the driver). C-RT-35.
                mcp_server._state["_resume_pause_snapshot"] = snapshot  # pyright: ignore[reportPrivateUsage]
                mcp_server._state["_resume_context"] = resume_context  # pyright: ignore[reportPrivateUsage]
                mcp_server.workflow_registry[workflow.workflow_id] = workflow
                try:
                    cp_result = await _invoke_run_workflow_via_in_process_mcp(
                        mcp_server.server, workflow.workflow_id
                    )
                finally:
                    mcp_server.workflow_registry.pop(workflow.workflow_id, None)
                    mcp_server._state.pop("_harness_ctx", None)  # pyright: ignore[reportPrivateUsage]
                    mcp_server._state.pop("_resume_pause_snapshot", None)  # pyright: ignore[reportPrivateUsage]
                    mcp_server._state.pop("_resume_context", None)  # pyright: ignore[reportPrivateUsage]
                timed_out = (
                    cp_result.status == _CpRunStatus.DRAINED
                    and cp_result.fail_class == "RT-FAIL-DRAIN-TIMEOUT"
                )
            finally:
                shutdown_report = await _shutdown(ctx)
            return _build_run_result(
                cp_result,
                shutdown_report,
                timed_out=timed_out,
                cost_records=ctx.cost_record_accumulator.records,
            )
        finally:
            COST_ACCUM_VAR.reset(_cost_token)


# ---------------------------------------------------------------------------
# CP RunResult → runtime RunResult conversion (C-RT-09 + C-CP-25 §25.2).
# ---------------------------------------------------------------------------


_CP_TO_RT_STATUS: dict[
    _CpRunStatus, Literal["completed", "drained", "failed", "paused", "partial"]
] = {
    _CpRunStatus.SUCCESS: "completed",
    _CpRunStatus.DRAINED: "drained",
    _CpRunStatus.FAILED: "failed",
    # PAUSED (C-RT-35, R-CC-1 arc #3) — a workflow-layer pause was captured;
    # `pause_snapshot` is carried through `_build_run_result` for `resume()`.
    _CpRunStatus.PAUSED: "paused",
    # PARTIAL (U-RT-113, R-FS-1 B1 §9) — a `proceed`-cascade fan-out run
    # gracefully degraded (≥1 branch failed; a partial result aggregated). The
    # v1.4 defensive `PARTIAL → "failed"` placeholder is flipped to "partial"
    # now that the non-linear strategies (CP §25.10–§25.18) can return it.
    # `failure_cause` stays None for "partial" (the `elif status == "failed"`
    # branch in `_build_run_result` does not fire) — a degraded run did not
    # fail. Exit code already maps "partial" → 1 (CLI `_CP_STATUS_TO_EXIT_CODE`).
    _CpRunStatus.PARTIAL: "partial",
}


def _rollup_cost_attribution(
    cost_records: list[SpanCostRecord] | None,
) -> tuple[CrossFamilyCostRollup, ...]:
    """Roll run-scoped cost records up to `RunResult.cost_attribution` (C-RT-09 §9, v1.53).

    Single axis `RollupAxis.PER_PROVIDER_AND_MODEL` — the per-(provider, model)
    breakdown. A single axis preserves the sum-invariant:
    `sum(e.total_cost for e in result)` == total run cost. Empty / None records →
    `()` (the trivial-/no-cost-workflow shape). The orthogonal dispatch-type
    breakdown is `_rollup_cost_attribution_by_dispatch_kind` (v1.57).
    """
    if not cost_records:
        return ()
    return tuple(rollup_costs_by_axis(cost_records, RollupAxis.PER_PROVIDER_AND_MODEL))


def _rollup_cost_attribution_by_dispatch_kind(
    cost_records: list[SpanCostRecord] | None,
) -> tuple[CrossFamilyCostRollup, ...]:
    """Roll cost records up to `RunResult.cost_attribution_by_dispatch_kind` (C-RT-09 §9, v1.57).

    Single axis `RollupAxis.PER_DISPATCH_KIND` — the operator-meaningful
    dispatch-type (llm/tool/validator/webhook) breakdown (OD spec v1.30 §15.1,
    `B-COST-DISCRIMINATOR-TAXONOMY`). Keyed on the typed `SpanCostRecord.dispatch_kind`
    (no `CrossFamilyTag` validation). Orthogonal single-axis partition of the same
    total as `_rollup_cost_attribution` ⟹ `sum(e.total_cost)` == total run cost.
    Empty / None records → `()`.
    """
    if not cost_records:
        return ()
    return tuple(rollup_costs_by_axis(cost_records, RollupAxis.PER_DISPATCH_KIND))


def _rollup_cost_attribution_by_provider_discriminator(
    cost_records: list[SpanCostRecord] | None,
) -> tuple[CrossFamilyCostRollup, ...]:
    """Roll cost records up to `RunResult.cost_attribution_by_provider_discriminator`
    (C-RT-09 §9, v1.58).

    Single axis `RollupAxis.PER_PROVIDER_DISCRIMINATOR` — the cross-family
    family-tag (`frontier_managed` / `frontier_managed_alt` / `local_ollama`)
    breakdown (OD spec v1.2 §15.1 / §15.3, populated by R-FS-1
    `B-FALLBACK-CHAIN-FAMILY-COST-COMPOSITION`). Keys on
    `SpanCostRecord.provider_discriminator`; **skips `None`-tag records** (a
    per-dispatch record with no chain-level family context, §15.1.2) and
    validates the rest against `CrossFamilyTag`.

    Unlike `cost_attribution` / `cost_attribution_by_dispatch_kind` (which
    partition the *full* run total), this axis partitions only the **LLM
    subtotal**: tool / validator / webhook records carry `provider_discriminator
    = None` (no provider family) and are correctly skipped, so
    `sum(e.total_cost)` here equals the sum of LLM-dispatch records' cost, not
    the total run cost. Empty / None records (or an all-non-LLM run) → `()`.
    """
    if not cost_records:
        return ()
    return tuple(rollup_costs_by_axis(cost_records, RollupAxis.PER_PROVIDER_DISCRIMINATOR))


def _build_run_result(
    cp_result: _CpRunResult,
    shutdown_report: Any,
    *,
    timed_out: bool = False,
    cost_records: list[SpanCostRecord] | None = None,
) -> RunResult:
    """Project a CP driver `RunResult` + runtime `ShutdownReport` into the
    runtime-facing `RunResult` per C-RT-09.

    Field mapping:
    - `status`: closed enum projection per `_CP_TO_RT_STATUS`.
    - `workflow_id`: pass-through from CP result.
    - `terminal_state`: `cp_result.final_state` if SUCCESS, else `partial_state`
      (drained) or `{}` (failed without partial). C-RT-09 invariant: dict, never
      None.
    - `audit_ledger_head_hash`: from `shutdown_report.audit_ledger_head_hash`.
      Spec C-RT-09 says "always present"; in practice may be empty string at
      v1.4 if no audit-ledger entries were written (state-ledger entries from
      the CP driver land on `ctx.ledger_writer`, a distinct writer from
      `ctx.audit_writer`). Class 3 informational drift; non-blocking — the
      U-RT-49 ledger-entry AC verifies via `read_ledger(handle)`, not via
      this field.
    - `trace_ids`: empty tuple at v1.4 (driver does not surface root span trace
      IDs through `RunResult`; spec §25 deferred-to-discretion). Class 3
      informational gap; queued for a future runtime + driver coherence pass.
    - `cost_attribution`: (v1.53, R-FS-1 arc CA) per-run rollup along
      `RollupAxis.PER_PROVIDER_AND_MODEL` computed from `cost_records` — the
      run-scoped `ctx.cost_record_accumulator`, appended by the LLM / tool /
      validator / webhook per-dispatch cost wrappers — via `rollup_costs_by_axis`
      (C-OD-15 §15.1). `()` when no cost-bearing dispatch occurred. **Supersedes
      the v1.4 empty-tuple carry-forward**: the `U-OD-21` HALTED Class-1 tension is
      CLOSED.
    - `cost_attribution_by_dispatch_kind`: (v1.57, `B-COST-DISCRIMINATOR-TAXONOMY`)
      the orthogonal per-`RollupAxis.PER_DISPATCH_KIND` breakdown from the same
      `cost_records` — the operator-meaningful llm/tool/validator/webhook split.
      A separate single-axis field (each record carries one `dispatch_kind`), so
      it independently satisfies the sum-invariant. `()` when no cost-bearing
      dispatch occurred.
    - `cost_attribution_by_provider_discriminator`: (v1.58,
      `B-FALLBACK-CHAIN-FAMILY-COST-COMPOSITION`) the per-`RollupAxis.PER_PROVIDER_DISCRIMINATOR`
      cross-family family-tag breakdown — per-family cost visibility under
      fallback. Partitions only the **LLM subtotal** (non-LLM records carry
      `provider_discriminator = None` and are skipped), so its `Σ total_cost` is
      the LLM-dispatch cost, NOT the full run total. `()` when no LLM dispatch
      occurred.
    - `failure_cause`: populated when status is "failed"; tags through
      `cp_result.fail_class`.
    """
    status = _CP_TO_RT_STATUS[cp_result.status]

    if cp_result.status is _CpRunStatus.SUCCESS:
        terminal_state: dict[str, Any] = dict(cp_result.final_state or {})
    elif cp_result.status is _CpRunStatus.DRAINED:
        terminal_state = dict(cp_result.partial_state or {})
    else:
        terminal_state = dict(cp_result.partial_state or {})

    failure_cause: FailureCause | None = None
    if timed_out:
        # U-RT-44 AC #2 typed-timeout branch — surface
        # `RT-FAIL-DRAIN-TIMEOUT` per C-RT-14 even though status is
        # `drained` (DRAINED-with-timeout-cause). Caller introspects
        # `failure_cause.runtime_fail_class` to disambiguate
        # graceful-drain from timeout-forced-drain.
        failure_cause = FailureCause(
            runtime_fail_class="RT-FAIL-DRAIN-TIMEOUT",
            detail=(
                "in-flight workflow step did not complete within "
                "`RuntimeConfig.drain_timeout_seconds`; the driver thread "
                "was not cancelled — spec §11 invariant ('in-flight step "
                "may be in inconsistent state') applies"
            ),
            validator_fail_class=None,
        )
    elif status == "failed":
        failure_cause = FailureCause(
            runtime_fail_class="RT-FAIL-WORKFLOW",
            detail=(
                f"workflow execution returned status={cp_result.status.value!r} "
                f"with fail_class={cp_result.fail_class!r}"
            ),
            validator_fail_class=cp_result.fail_class,
        )

    head_hash: str = shutdown_report.audit_ledger_head_hash or ""

    return RunResult(
        status=status,
        workflow_id=WorkflowID(cp_result.workflow_id),
        terminal_state=terminal_state,
        audit_ledger_head_hash=head_hash,
        trace_ids=(),
        cost_attribution=_rollup_cost_attribution(cost_records),
        cost_attribution_by_dispatch_kind=_rollup_cost_attribution_by_dispatch_kind(cost_records),
        cost_attribution_by_provider_discriminator=(
            _rollup_cost_attribution_by_provider_discriminator(cost_records)
        ),
        failure_cause=failure_cause,
        # C-RT-35 (R-CC-1 arc #3) — surface the captured PauseSnapshot on a
        # 'paused' outcome so the caller can persist it + resume(). None on
        # every terminal outcome (the CP driver only populates it on PAUSED).
        pause_snapshot=cp_result.pause_snapshot if status == "paused" else None,
    )


def _default_config() -> RuntimeConfig:
    """Fallback `RuntimeConfig` when caller passes `config=None`.

    Per C-RT-08 §8 "config=None default behavior": materialize
    `RuntimeConfig` from defaults + env vars per C-RT-03 precedence.
    Full env-var ingestion lives at `harness_runtime.config.loader`;
    this fallback is the minimal-defaults form used when no caller-side
    config is supplied. Raises if required fields cannot be resolved.
    """
    from harness_runtime.config.loader import materialize_runtime_config

    return materialize_runtime_config()
