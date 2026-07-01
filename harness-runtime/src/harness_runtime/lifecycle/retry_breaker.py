"""Retry / breaker / idempotency runtime registry — stage 3b CP_ROUTING (U-RT-24).

Per `Spec_Harness_Runtime_v1.md` v1.1 §5 (C-RT-02 stage 3b invariants) and the
Phase 2 Session 3 Track A atomic decomposition §L5 (U-RT-24). The runtime wires
retry + breaker + idempotency-join primitives over landed CP / OD modules:

- `harness_cp.routing_manifest_residence.RetryPolicy` — operator-supplied
  per-tool policy (`max_attempts` / `backoff` / `jitter`) carried on
  `RuntimeConfig.routing_manifest.retry_policies`.
- `harness_cp.validator_fail_transient_staircase.advance_staircase` — the
  C-CP-21 §21.2 transient-staircase advancement function (5-stage table,
  cause-attribution-conditioned branching).
- `harness_od.harness_breaker_schema` — OD-canonical 7-attribute
  `harness.breaker.*` schema + `emit_breaker_trip_span_event` (C-OD-07 §7.1).
- `harness_od.idempotency_join_dedup.dedupe_on_replay` — the C-OD-14 §14.5.1
  trace-ingestion dedup decision.

**Hand-rolled.** Per `Plan_Executability_Audit_v1.md` framework-pull discipline
+ CLAUDE.md §3.2 + I-6: NO `tenacity` / `pybreaker` / `circuitbreaker`. The
breaker state machine and the full-jitter backoff are written here against the
stdlib `random` module.

**Breaker config is spec-deferred.** C-CP-03 §3.5 explicitly defers "specific
breaker trip-threshold values per `{provider, model}` pair; specific cooldown
duration shape per cause class" to implementation discretion. This module
binds bootstrap defaults — `DEFAULT_FAIL_THRESHOLD` / `DEFAULT_COOLDOWN_SECONDS`
/ `DEFAULT_BASE_DELAY_SECONDS` / `DEFAULT_DELAY_CAP_SECONDS` — at the
materialize-stage composer; the composer keyword arguments let operators
override per-runtime without amending the spec.

**Two retry surfaces, by design.** The L4 `_attempt_with_bounded_retry` at
`harness_runtime.lifecycle.providers` is the BOOTSTRAP construction retry —
distinct from this L5 registry. Bootstrap retry has no breaker, no
idempotency join, no staircase; it bounds adapter-construction transient
failures only. This registry handles RUNTIME tool-call retries keyed by tool
name. The two are deliberately separate surfaces (matching the spec's
bootstrap-vs-runtime distinction at C-RT-02 §5).

**Staircase reading.** Per C-CP-21 §21.2, the transient staircase governs
class-to-class transitions (REFLEXION → RETRY_WITH_BACKOFF → CROSS_FAMILY_FALLBACK
→ LOCAL_TERMINAL → HITL_ESCALATION) keyed on `ValidatorRetryExitClass`. The
full-jitter sleep at stage 2 (RETRY_WITH_BACKOFF) is the per-attempt delay
schedule that this registry computes via `compute_full_jitter_delay_seconds`.
The staircase is the escalation envelope; the jitter schedule is the
within-stage retry cadence.

Per-component landing posture:

- `BreakerStateMachine` — mutable per-(scope, identifier) breaker state.
  closed → open at `fail_threshold` consecutive failures; open → half_open
  when the caller invokes `attempt_half_open()` after cooldown; half_open →
  closed on success, → open on failure.
- `BreakerTransition` — frozen record of one state-machine transition,
  consumed by `emit_breaker_transition_event` to produce the `breaker.tripped`
  span event.
- `compute_full_jitter_delay_seconds(attempt)` — pure full-jitter backoff
  computation. `uniform(0, min(cap, base * 2**attempt))`. Test-injectable
  `rng` parameter makes the staircase-observability test deterministic.
- `RuntimeRetryBreaker` — concrete `RetryBreakerRegistry` Protocol
  implementation. Lazily instantiates breakers keyed by `(BreakerScope, str)`.
- `RetryBreakerStage` — frozen materialization stage carrying the registry.
- `materialize_retry_breaker_stage(config, *, ...)` — composer.

Scope discipline (U-RT-24 boundary held): NO HITL/handoff registries
(U-RT-25/26), NO topology dispatch (U-RT-40), NO audit-ledger writer (U-RT-32 —
the dedup decision is exposed here as a pure function over `dedupe_on_replay`;
the writer that *applies* outcomes lands at U-RT-32), NO collector daemon
(U-RT-29). This registry is a binding-time + reference-time surface only; the
LOOP_INIT orchestrator (U-RT-43+) drives the actual retry loop.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Final

from harness_cp.routing_manifest_residence import RetryPolicy
from harness_cp.validator_fail_taxonomy import ValidatorRetryExitClass
from harness_cp.validator_fail_transient_staircase import (
    StaircaseStage,
    StaircaseTransition,
    advance_staircase,
)
from harness_od.harness_breaker_schema import (
    BreakerScope,
    BreakerState,
    HarnessBreakerEvent,
    emit_breaker_trip_span_event,
)
from harness_od.idempotency_join_dedup import (
    DedupOutcome,
    F2StateLedgerEntry,
    SpanIngestionView,
    dedupe_on_replay,
)
from harness_od.otel_genai_base import EventEmission, SpanRef

from harness_runtime.types import RuntimeConfig

__all__ = [
    "DEFAULT_BACKOFF_TOKEN",
    "DEFAULT_BASE_DELAY_SECONDS",
    "DEFAULT_COOLDOWN_SECONDS",
    "DEFAULT_DELAY_CAP_SECONDS",
    "DEFAULT_FAIL_THRESHOLD",
    "DEFAULT_JITTER_TOKEN",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_RETRY_POLICY",
    "BreakerStateMachine",
    "BreakerTransition",
    "RetryBreakerBindError",
    "RetryBreakerStage",
    "RuntimeRetryBreaker",
    "compute_full_jitter_delay_seconds",
    "materialize_retry_breaker_stage",
]


# --- Implementation-discretion defaults (C-CP-03 §3.5 spec-deferred) --------

#: Bounded-retry attempt cap default — three attempts (matches C-CP-03 §3
#: chain-advancement bound and the L4 bootstrap-construction default).
DEFAULT_MAX_ATTEMPTS: Final[int] = 3

#: Backoff strategy token default — "full-jitter" per C-CP-03 §3.5.
DEFAULT_BACKOFF_TOKEN: Final[str] = "full-jitter"

#: Jitter mode token default — composes with the full-jitter backoff.
DEFAULT_JITTER_TOKEN: Final[str] = "full-jitter"

#: Full-jitter base delay seconds — 500 ms first-attempt cap before jitter.
DEFAULT_BASE_DELAY_SECONDS: Final[float] = 0.5

#: Full-jitter delay cap seconds — caps exponential growth at 30 s per attempt.
DEFAULT_DELAY_CAP_SECONDS: Final[float] = 30.0

#: Breaker fail-threshold default — closed → open after 5 consecutive failures.
DEFAULT_FAIL_THRESHOLD: Final[int] = 5

#: Breaker cooldown seconds default — open → half-open after 30 s elapsed.
DEFAULT_COOLDOWN_SECONDS: Final[float] = 30.0

#: Default per-tool RetryPolicy applied when the manifest has no entry for a
#: tool. Faithful factor-out of the C-CP-03 §3.5 retry.policy three-field
#: vocabulary; carried as a constant so callers can identity-compare.
DEFAULT_RETRY_POLICY: Final[RetryPolicy] = RetryPolicy(
    max_attempts=DEFAULT_MAX_ATTEMPTS,
    backoff=DEFAULT_BACKOFF_TOKEN,
    jitter=DEFAULT_JITTER_TOKEN,
)


class RetryBreakerBindError(Exception):
    """Bootstrap-time retry/breaker registry bind failure (RT-FAIL-BOOTSTRAP).

    Raised when the manifest carries a malformed `RetryPolicy` — e.g.
    `max_attempts < 1` — or when the bootstrap-default policy is invalid.
    Surfaces at `materialize_retry_breaker_stage`, never at runtime."""


@dataclass(frozen=True, slots=True)
class BreakerTransition:
    """One breaker state-machine transition (C-OD-07 §7.1 input shape).

    Frozen → `Eq`. Carries the data needed to compose a `HarnessBreakerEvent`
    + dispatch `emit_breaker_trip_span_event`. The `(scope, identifier)`
    coordinates identify the breaker; `from_state` / `to_state` name the
    transition; `trigger_count` is the consecutive failure count that drove
    the transition (closed→open case) or zero (open→half_open, half_open→closed)."""

    from_state: BreakerState
    to_state: BreakerState
    scope: BreakerScope
    identifier: str
    trigger_count: int


@dataclass(slots=True)
class BreakerStateMachine:
    """Mutable per-(scope, identifier) breaker state machine.

    Hand-rolled per CLAUDE.md §3.2 — NO `pybreaker` / `circuitbreaker`.
    Three-state machine matching the OD canonical `BreakerState` enum
    (closed, open, half_open). The state transition discipline:

    - **closed → open**: at `fail_threshold` consecutive failures.
    - **open → half_open**: caller-driven via `attempt_half_open()` after
      `cooldown_seconds` elapses. The state machine does not hold a clock —
      the caller (L8 LOOP_INIT orchestrator) decides when cooldown has
      elapsed and invokes the transition.
    - **half_open → closed**: on a single success (`record_success`).
    - **half_open → open**: on a failure during the half-open trial.

    The lazy-clock model means this class is deterministic + clock-free in
    tests; the caller threads a clock + cooldown policy at the L8 layer.
    """

    scope: BreakerScope
    identifier: str
    state: BreakerState = BreakerState.CLOSED
    fail_count: int = 0
    fail_threshold: int = DEFAULT_FAIL_THRESHOLD
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS

    def record_failure(self) -> BreakerTransition | None:
        """Record one failure; return the transition iff the state changed.

        - In `closed`: increments `fail_count`; transitions to `open` if the
          count reaches `fail_threshold`.
        - In `half_open`: any failure → `open` (the half-open trial failed).
        - In `open`: no-op (the caller should have checked `should_attempt()`
          before invoking the call); returns `None`.

        Returns the `BreakerTransition` on a state change, else `None`.
        """
        if self.state is BreakerState.OPEN:
            return None
        if self.state is BreakerState.HALF_OPEN:
            prior = self.state
            self.fail_count += 1
            self.state = BreakerState.OPEN
            return BreakerTransition(
                from_state=prior,
                to_state=BreakerState.OPEN,
                scope=self.scope,
                identifier=self.identifier,
                trigger_count=self.fail_count,
            )
        # state is CLOSED
        self.fail_count += 1
        if self.fail_count >= self.fail_threshold:
            self.state = BreakerState.OPEN
            return BreakerTransition(
                from_state=BreakerState.CLOSED,
                to_state=BreakerState.OPEN,
                scope=self.scope,
                identifier=self.identifier,
                trigger_count=self.fail_count,
            )
        return None

    def record_success(self) -> BreakerTransition | None:
        """Record one success; return the transition iff the state changed.

        - In `closed`: resets `fail_count` to 0; no transition.
        - In `half_open`: transitions to `closed`; resets `fail_count`.
        - In `open`: no-op; returns `None`.
        """
        if self.state is BreakerState.HALF_OPEN:
            self.state = BreakerState.CLOSED
            self.fail_count = 0
            return BreakerTransition(
                from_state=BreakerState.HALF_OPEN,
                to_state=BreakerState.CLOSED,
                scope=self.scope,
                identifier=self.identifier,
                trigger_count=0,
            )
        if self.state is BreakerState.CLOSED:
            self.fail_count = 0
        return None

    def attempt_half_open(self) -> BreakerTransition | None:
        """Caller-driven open → half_open transition (cooldown elapsed).

        The state machine carries `cooldown_seconds` as a policy hint; the
        L8 LOOP_INIT orchestrator threads a monotonic clock and decides when
        to invoke this method. Returns the transition on success; `None` if
        not in `open` state.
        """
        if self.state is not BreakerState.OPEN:
            return None
        self.state = BreakerState.HALF_OPEN
        return BreakerTransition(
            from_state=BreakerState.OPEN,
            to_state=BreakerState.HALF_OPEN,
            scope=self.scope,
            identifier=self.identifier,
            trigger_count=0,
        )

    def should_attempt(self) -> bool:
        """Return True iff a retry attempt may proceed (state is not `open`)."""
        return self.state is not BreakerState.OPEN


def compute_full_jitter_delay_seconds(
    attempt: int,
    *,
    base_seconds: float = DEFAULT_BASE_DELAY_SECONDS,
    cap_seconds: float = DEFAULT_DELAY_CAP_SECONDS,
    rng: random.Random | None = None,
) -> float:
    """Full-jitter backoff delay per C-CP-03 §3.5 retry.policy.

    `delay = uniform(0, min(cap_seconds, base_seconds * 2**attempt))`. The
    "full-jitter" reading: the per-attempt delay is sampled uniformly between
    zero and the exponentially-growing cap. Hand-rolled per CLAUDE.md §3.2 —
    no tenacity. `attempt` is 0-indexed (the first retry uses `attempt=0`).
    Test injection: pass a seeded `random.Random` via `rng` to make the
    delay schedule deterministic across runs (used at the AC #1 transient-
    staircase observability test).
    """
    if attempt < 0:
        raise ValueError(f"attempt must be ≥ 0; got {attempt}")
    if base_seconds < 0:
        raise ValueError(f"base_seconds must be ≥ 0; got {base_seconds}")
    if cap_seconds < 0:
        raise ValueError(f"cap_seconds must be ≥ 0; got {cap_seconds}")
    bounded = min(cap_seconds, base_seconds * (2**attempt))
    chosen_rng = rng if rng is not None else random
    return chosen_rng.uniform(0, bounded)


@dataclass(slots=True)
class RuntimeRetryBreaker:
    """Concrete `RetryBreakerRegistry` Protocol implementation (U-RT-24).

    Carries the manifest's per-tool `RetryPolicy` mapping, a default policy
    for tools without an explicit entry, and a lazily-grown dict of
    `BreakerStateMachine` instances keyed by `(BreakerScope, identifier)`.

    The registry composes three landed primitives:
    - `harness_cp.validator_fail_transient_staircase.advance_staircase`
      (re-exported via `advance_staircase()` for staircase observability).
    - `harness_od.harness_breaker_schema.emit_breaker_trip_span_event`
      (composed via `emit_breaker_transition_event()`).
    - `harness_od.idempotency_join_dedup.dedupe_on_replay` (re-exported via
      `dedupe_decision()` for AC #3 idempotency-join dedup).

    Lookup discipline: `get_breaker` lazily instantiates a breaker on first
    reference; subsequent references return the same machine (identity-stable).
    """

    retry_policies: dict[str, RetryPolicy]
    default_policy: RetryPolicy
    fail_threshold: int = DEFAULT_FAIL_THRESHOLD
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS
    base_delay_seconds: float = DEFAULT_BASE_DELAY_SECONDS
    delay_cap_seconds: float = DEFAULT_DELAY_CAP_SECONDS
    _breakers: dict[tuple[BreakerScope, str], BreakerStateMachine] = field(
        default_factory=lambda: {}
    )

    def get_policy(self, tool_name: str) -> RetryPolicy:
        """Return the per-tool `RetryPolicy` or `default_policy` if unbound.

        Manifest-driven lookup; falls back to `default_policy` (`DEFAULT_RETRY_POLICY`
        at HEAD) when the tool name is absent from `retry_policies`."""
        return self.retry_policies.get(tool_name, self.default_policy)

    def get_breaker(
        self,
        scope: BreakerScope,
        identifier: str,
    ) -> BreakerStateMachine:
        """Return the breaker for `(scope, identifier)` — lazily instantiate.

        First reference for a `(scope, identifier)` key constructs a fresh
        `BreakerStateMachine` with the registry's threshold + cooldown defaults
        and caches it. Subsequent references return the same machine.
        """
        key = (scope, identifier)
        breaker = self._breakers.get(key)
        if breaker is None:
            breaker = BreakerStateMachine(
                scope=scope,
                identifier=identifier,
                fail_threshold=self.fail_threshold,
                cooldown_seconds=self.cooldown_seconds,
            )
            self._breakers[key] = breaker
        return breaker

    def compute_delay_seconds(
        self,
        attempt: int,
        rng: random.Random | None = None,
    ) -> float:
        """Full-jitter delay per `compute_full_jitter_delay_seconds` and registry
        defaults. `attempt` is 0-indexed."""
        return compute_full_jitter_delay_seconds(
            attempt,
            base_seconds=self.base_delay_seconds,
            cap_seconds=self.delay_cap_seconds,
            rng=rng,
        )

    def advance_staircase(
        self,
        current: StaircaseStage,
        cause: ValidatorRetryExitClass,
        attempt: int,
    ) -> StaircaseTransition:
        """Re-export of `harness_cp.validator_fail_transient_staircase.advance_staircase`.

        Composed at the registry so AC #1 (transient-staircase observability)
        can be verified through the registry surface — the runtime threads
        staircase advancement through this method, never imports the CP
        function directly."""
        return advance_staircase(current, cause, attempt)

    def emit_breaker_transition_event(
        self,
        transition: BreakerTransition,
        parent_span_ref: SpanRef,
        *,
        permanent_fail_repeats: int | None = None,
        tool_id: str | None = None,
        model_version: str | None = None,
    ) -> EventEmission:
        """Emit the `breaker.tripped` event for a state transition (C-OD-07 §7.1).

        Composes `transition` + OD-canonical `HarnessBreakerEvent` + the
        `emit_breaker_trip_span_event` emission. Optional `tool_id` /
        `model_version` correlate the event with the spec's per-model scope
        attributes (C-CP-03 §3.5: "harness.breaker.tool_id — per-model scope
        correlation"). When `tool_id` is omitted at PER_MODEL scope, the
        registry defaults to the transition's `identifier` (the breaker key).
        """
        effective_tool_id = tool_id
        if effective_tool_id is None and transition.scope is BreakerScope.PER_MODEL:
            effective_tool_id = transition.identifier
        event = HarnessBreakerEvent(
            scope=transition.scope,
            from_state=transition.from_state,
            to_state=transition.to_state,
            trigger_count=transition.trigger_count,
            permanent_fail_repeats=permanent_fail_repeats,
            tool_id=effective_tool_id,
            model_version=model_version,
        )
        return emit_breaker_trip_span_event(parent_span_ref, event)

    def dedupe_decision(
        self,
        span: SpanIngestionView,
        ledger_entry: F2StateLedgerEntry | None,
    ) -> DedupOutcome:
        """Idempotency-join dedup decision (C-OD-14 §14.5.1).

        Re-export of `harness_od.idempotency_join_dedup.dedupe_on_replay`.
        AC #3 (idempotency join dedupes a replayed request to a single
        ledger entry) is verified through this surface: a first ingestion
        (`ledger_entry is None`) yields `RECORD_FIRST_INGESTION`; a
        subsequent deterministic-replay span with a matching ledger entry
        yields `DROP_DETERMINISTIC_REPLAY_RE_READ` — collapsing to a single
        ledger entry when the U-RT-32 audit writer consumes outcomes."""
        return dedupe_on_replay(span, ledger_entry)


@dataclass(frozen=True, slots=True)
class RetryBreakerStage:
    """Frozen result of stage 3b CP_ROUTING retry/breaker registry materialization.

    Mirrors the L4 / U-RT-21 / U-RT-22 / U-RT-23 stage shape. The bootstrap
    orchestrator (U-RT-43) binds `registry` to `HarnessContext.retry_breaker`.
    """

    registry: RuntimeRetryBreaker


def materialize_retry_breaker_stage(
    config: RuntimeConfig,
    *,
    default_policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    fail_threshold: int = DEFAULT_FAIL_THRESHOLD,
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
    base_delay_seconds: float = DEFAULT_BASE_DELAY_SECONDS,
    delay_cap_seconds: float = DEFAULT_DELAY_CAP_SECONDS,
) -> RetryBreakerStage:
    """Build the retry/breaker registry stage at stage 3b CP_ROUTING.

    Stage 3b composer. Pulls `config.routing_manifest.retry_policies` into the
    registry's per-tool mapping. Validates that each policy's `max_attempts`
    is ≥ 1 at bootstrap (per C-CP-03 §3 chain-advancement bound); a malformed
    policy raises `RetryBreakerBindError` (bootstrap-time, never runtime).

    Bootstrap defaults (`fail_threshold` / `cooldown_seconds` /
    `base_delay_seconds` / `delay_cap_seconds`) are per-runtime implementation
    discretion per C-CP-03 §3.5 spec deferral ("specific breaker trip-threshold
    values per `{provider, model}` pair; specific cooldown duration shape per
    cause class" — deferred). Override via keyword arguments at composer time.

    Empty `retry_policies` is fine — `get_policy(tool_name)` falls back to
    `default_policy` for every tool. The runtime can operate with no per-tool
    overrides at all (the default policy + lazy breakers handle the universe).
    """
    if default_policy.max_attempts < 1:
        raise RetryBreakerBindError(
            f"default retry policy has invalid max_attempts="
            f"{default_policy.max_attempts} (must be ≥ 1)"
        )
    manifest = config.routing_manifest
    retry_policies: dict[str, RetryPolicy] = {}
    for tool_name, policy in manifest.retry_policies.items():
        if policy.max_attempts < 1:
            raise RetryBreakerBindError(
                f"retry policy for tool {tool_name!r} has invalid max_attempts="
                f"{policy.max_attempts} (must be ≥ 1)"
            )
        retry_policies[tool_name] = policy
    # Q2=c reserved registry key injection (U-RT-58, C-RT-16 §"Registry key
    # extension"): the runtime's LLM-dispatch retry policy lives under the
    # reserved ``"llm_dispatch"`` key. Operator manifests cannot supply this
    # key — the validator at `harness_cp.routing_manifest_residence.
    # validate_routing_manifest` raises ``ReservedToolNameError`` if they do.
    # Imported lazily here to avoid a `harness-runtime/lifecycle/retry_breaker`
    # → `harness-runtime/lifecycle/retry_breaker_fallback` → `harness-runtime/
    # lifecycle/retry_breaker` import cycle at module load.
    from harness_runtime.lifecycle.retry_breaker_fallback import (
        DEFAULT_LLM_DISPATCH_RETRY_POLICY,
        RESERVED_LLM_DISPATCH_KEY,
    )

    retry_policies[RESERVED_LLM_DISPATCH_KEY] = DEFAULT_LLM_DISPATCH_RETRY_POLICY
    registry = RuntimeRetryBreaker(
        retry_policies=retry_policies,
        default_policy=default_policy,
        fail_threshold=fail_threshold,
        cooldown_seconds=cooldown_seconds,
        base_delay_seconds=base_delay_seconds,
        delay_cap_seconds=delay_cap_seconds,
    )
    return RetryBreakerStage(registry=registry)
