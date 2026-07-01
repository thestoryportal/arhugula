"""Stage 4 OD — tracer + span_processor + collector + ring buffer + cost chain + audit writer.

Per `Spec_Harness_Runtime_v1.md` v1.1 §2 stage 4 post-conditions:
`opentelemetry.trace.get_tracer_provider()` returns the runtime-registered
provider; `ctx.collector_daemon` is running (health-check ok); `ctx.cost_chain`,
`ctx.audit_writer` non-None.

Composer call order (intra-stage dependencies):
1. `materialize_tracer_provider_stage` — globally registers TracerProvider.
2. `materialize_audit_writer_stage(config, ledger_writer)` — depends on stage 1.
3. `materialize_span_processor_stage(config, provider, audit_writer=...)` —
   attaches redaction + BSP + exporter; multi-tenant token maps can persist
   through the audit writer.
4. `materialize_collector_daemon_stage(config)` — supervisor (NOT started yet).
5. `await daemon.start()` — start the collector daemon explicitly (docstring
   at `lifecycle/collector_daemon.py` line ~60: "The bootstrap orchestrator
   (U-RT-43) calls `await stage.daemon.start()` at the stage 4 entry").
6. `materialize_ring_buffer_stage(config, daemon)` — depends on running daemon.
7. `materialize_cost_attribution_stage(config)`.

**Tracer rollback (Class 3 informational).** OTel has no `unset_tracer_provider`
API. On stage 4 failure, rollback leaves the global provider registered;
subsequent process invocations replace via `set_tracer_provider`. Deferred to
U-RT-44/45 shutdown work if a true unregister API is needed.
"""

from __future__ import annotations

from harness_core.workload_class import WorkloadClass

from harness_runtime.bootstrap.factories.validator_framework_factory import (
    materialize_validator_framework_stage,
)
from harness_runtime.bootstrap.mutable_context import _MutableHarnessContext
from harness_runtime.lifecycle.audit_writer import materialize_audit_writer_stage
from harness_runtime.lifecycle.collector_daemon import materialize_collector_daemon_stage
from harness_runtime.lifecycle.cost_attribution import materialize_cost_attribution_stage
from harness_runtime.lifecycle.ring_buffer import materialize_ring_buffer_stage
from harness_runtime.lifecycle.span_processor import materialize_span_processor_stage
from harness_runtime.lifecycle.tracer_provider import materialize_tracer_provider_stage
from harness_runtime.types import RuntimeConfig

__all__ = ["execute"]


async def execute(
    ctx: _MutableHarnessContext,
    config: RuntimeConfig,
    workload_class: WorkloadClass,
) -> None:
    """Populate stage 4 OD fields on `ctx`."""
    _ = workload_class
    assert ctx.ledger_writer is not None, "stage 1 IS must precede stage 4 OD"

    # 1. Tracer provider — globally registered.
    tracer = materialize_tracer_provider_stage(config)
    ctx.tracer_provider = tracer.provider

    # 2. Audit-ledger writer (depends on stage 1 ledger writer). The redaction
    # token-map path consumes this at multi-tenant cells, so it must exist
    # before the span processor is attached.
    audit = materialize_audit_writer_stage(config, ctx.ledger_writer)
    ctx.audit_writer = audit.writer

    # 3. Span processor + exporter (attaches to the registered tracer provider).
    materialize_span_processor_stage(config, tracer.provider, audit_writer=ctx.audit_writer)
    # The span processor's lifetime is tied to the tracer provider; the stage
    # record is not held on HarnessContext (the BSP is reachable via the
    # tracer provider's processor list). The C-RT-10 shutdown will need to
    # retain a typed handle — addressed at U-RT-45.

    # 4. Collector daemon supervisor (constructed; NOT yet started).
    daemon_stage = materialize_collector_daemon_stage(config)

    # 5. Start the daemon. Failure here surfaces as a stage 4 failure and
    # triggers rollback of stages 0-3 + stage-4 partial-state cleanup.
    await daemon_stage.daemon.start()
    ctx.collector_daemon = daemon_stage.daemon

    # 6. Ring buffer (depends on running daemon).
    materialize_ring_buffer_stage(config, daemon_stage.daemon)
    # Like the span processor, the ring buffer's lifetime is tied to the
    # daemon supervisor; HarnessContext exposes the daemon, not the buffer.

    # 7. Cost attribution chain. The concrete chain narrows the Protocol's
    # deliberately `object`-typed method params (documented decoupling at
    # types.py); concrete → Protocol-typed field is a benign contravariance.
    cost = materialize_cost_attribution_stage(config)
    ctx.cost_chain = cost.chain  # pyright: ignore[reportAttributeAccessIssue]

    # 8. Validator framework (U-RT-84) — final stage-4 binding per runtime
    # spec v1.18 §14.13.3. Operator-opt-in: `None` (default) preserves the
    # v1.17 production-default state; non-`None` constructs an empty-registry
    # ConcreteValidatorFramework per Reading A scope.
    #
    # U-OD-40 cost-attribution hook wiring per CP spec v1.24 §28.10.5
    # mechanism (a): pass rate_table + cost_chain + audit_writer through to
    # the factory. When opt-in and all 3 substrates bound, the factory
    # constructs a CostAttributingValidatorHook and injects it via the
    # ConcreteValidatorFramework's optional post_evaluate_hook ctor param
    # per CP spec v1.24 §28.10.2.
    from harness_od.rate_table_v1 import RATE_TABLE_V1

    ctx.validator_framework = await materialize_validator_framework_stage(
        config,
        rate_table=RATE_TABLE_V1,
        cost_chain=ctx.cost_chain,
        audit_writer=ctx.audit_writer,
        # R-FS-1 arc CA + B-INTERSTEP-PERRUN-ISOLATION — thread the run-scoped
        # accumulator PROXY (not its `.records` list) so per-validator
        # SpanCostRecords `append` through to the current run's accumulator →
        # `RunResult.cost_attribution` (runtime v1.53 §9).
        cost_record_sink=ctx.cost_record_accumulator,
    )
