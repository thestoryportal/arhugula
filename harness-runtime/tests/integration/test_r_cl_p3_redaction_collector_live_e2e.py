"""R-CL-P3 — MULTI_TENANT pre-collector redaction, CI-discoverable live-collector e2e.

Closes the redaction sub-part of capability-completion inventory item #9
(``.harness/capability-completion-inventory-v1.md``) — "P3 redaction
collector-boundary proof" — as R-CC-1 arc #7.

**What this is (and is not).** The redaction *mechanism* is already proven, at
three levels, and this test does NOT re-prove it:

  - unit — ``test_lifecycle_span_processor.py`` proves the production
    ``materialize_span_processor_stage`` wires a ``RedactionSpanProcessor`` before
    the ``BatchSpanProcessor`` and strips all 13 ``DEFAULT_OFF_CONTENT_ATTRIBUTES``
    at ``on_end`` (against an in-memory test exporter);
  - cross-surface — ``test_cross_surface_emission_suite.py`` proves the redaction
    processor is wired at every surface carrying the right ``persona_tier``;
  - live (manual) — the ``tools/r500_multitenant_selfhosted_live_e2e.py`` operator
    tool (``just r500-multitenant-live-e2e``) injects content sentinels and proves
    they never reach Tempo through the real R-420 collector stack.

The genuine — and deliberately modest — increment here is **CI-discoverability of
the live ``export → collector → Tempo`` round-trip at MULTI_TENANT**: the existing
unit proof stops at the SpanProcessor ``on_end`` boundary with an in-memory
exporter; the only proof that a content-bearing span actually traverses the real
OTLP exporter + the real OTel collector and lands *content-stripped* in the trace
store is the manual R-500 operator tool, which is not collected by the suite. This
relocates R-500's non-vacuous redaction assertion into the ``@pytest.mark.e2e``,
docker-gated test surface (it is deselected by the blocking ``-m "not e2e"`` CI
lane and runs only when the R-420 stack is up).

**Why the dashboard's "real ``api.run`` workflow asserts content redacted"
framing is not honored.** Grepping ``harness-{runtime,cp,as,od}/src`` returns ZERO
production ``span.set_attribute(...)`` calls against the 13 content keys (the
``RedactionSpanProcessor`` is defense-in-depth at HEAD; see
``harness-od/src/harness_od/redaction_span_processor.py`` docstring). A real
echo-MCP + Ollama workflow therefore emits no content attributes, so asserting
they are "absent at the collector" on such a workflow would be **vacuous**. The
non-vacuous proof must *inject* a content sentinel and prove it is stripped — which
is exactly R-500's mechanism, and is workflow-independent (the redaction fires at
the SpanProcessor ``on_end``, regardless of what produced the span). This test
therefore reuses R-500's emit + assert mechanism rather than driving a workflow.

**Non-vacuousness.** The reused ``_emit_tenant_trace`` sets ``CONTENT_SENTINEL`` on
two ``DEFAULT_OFF_CONTENT_ATTRIBUTES`` keys (``gen_ai.input.messages`` +
``mcp.tool.call.arguments``) and ``STRUCTURE_SENTINEL`` on a structure key
(``audit.signature.sha256``). The structure sentinel *surviving* to Tempo is the
positive control — it proves the redaction is *selective* (not a blanket drop that
would make "content absent" trivially true), so the content-absent assertion has
discriminating power.

**Docker-gated, zero-cost.** Requires the local R-420 collector + Tempo stack on
127.0.0.1 (``just r420-self-hosted-stack-up``); no provider inference, no secrets,
no paid calls. The blocking CI lane (no stack) deselects it via ``-m "not e2e"``;
with the stack up it is skipped cleanly only if the ports are unreachable.

Authority: ``.harness/capability-completion-inventory-v1.md`` item #9;
``.harness/post-mvp-full-closure-plan-v1.md`` §P3; OD spec §C-OD-13 §13.2
(pre-collector redaction at the SDK / wrapper boundary BEFORE the
``BatchSpanProcessor`` buffer at multi-tenant-compliance cells);
``tools/r500_multitenant_selfhosted_live_e2e.py`` (reused mechanism).
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

# The shared live-collector emit/poll helpers are reused from the repo-root
# `tools/` package (the R-500 operator tool). pytest's `importlib` import mode —
# unlike `python` — does not place the invocation cwd on sys.path, so make the
# repo root importable here for the lazy `from tools.<...>` import in the test.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_COLLECTOR_HOST = "127.0.0.1"
_COLLECTOR_OTLP_PORT = 4317
_TEMPO_QUERY_PORT = 3200
_TENANT_ID = "arc7-redaction-tenant"


def _tcp_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def _collector_stack_reachable() -> bool:
    """True iff the R-420 collector OTLP port + Tempo query port both answer."""
    return _tcp_open(_COLLECTOR_HOST, _COLLECTOR_OTLP_PORT) and _tcp_open(
        _COLLECTOR_HOST, _TEMPO_QUERY_PORT
    )


@pytest.mark.e2e
@pytest.mark.skipif(
    not _collector_stack_reachable(),
    reason=(
        "live-collector redaction e2e requires the R-420 collector+Tempo stack on "
        "127.0.0.1:4317/3200 (just r420-self-hosted-stack-up)"
    ),
)
def test_multi_tenant_pre_collector_redaction_through_live_collector(tmp_path: Path) -> None:
    """At MULTI_TENANT_COMPLIANCE, content-bearing span attributes are stripped by
    the production-materialized ``RedactionSpanProcessor`` before the
    ``BatchSpanProcessor`` exports, so they never reach Tempo through the real OTel
    collector — while a structure-bearing attribute survives (selective redaction).
    """
    from harness_core import DeploymentSurface, PersonaTier
    from harness_core.workload_class import WorkloadClass
    from harness_cp.topology_pattern import TopologyPattern
    from harness_is.path_class_registry import PathClass
    from harness_od.content_structure_discipline import DEFAULT_OFF_CONTENT_ATTRIBUTES
    from harness_od.per_cell_collector_placement_matrix import CollectorPlacement
    from harness_runtime.types import (
        CollectorConfig,
        OTelConfig,
        PathBindingConfig,
        ProviderSecretsConfig,
        RuntimeConfig,
    )

    from tools.r500_multitenant_selfhosted_live_e2e import (
        CHILD_SPAN,
        CONTENT_SENTINEL,
        DEFAULT_TEMPO_URL,
        ROOT_SPAN,
        STRUCTURE_SENTINEL,
        _emit_tenant_trace,
        _wait_for_trace_records,
    )

    surface = DeploymentSurface.SELF_HOSTED_SERVER
    workload = WorkloadClass.PIPELINE_AUTOMATION
    path_bindings = PathBindingConfig(
        raw_entries=tuple(
            {
                "path_class": pc,
                "workflow_class": workload,
                "deployment_surface": surface,
                "path": str(tmp_path / pc.value.lower()),
            }
            for pc in PathClass
        ),
    )
    # MULTI_TENANT_COMPLIANCE × SELF_HOSTED_SERVER — the cell whose redaction posture
    # is non-toggleable pre-collector eval-grade (OD §C-OD-13 §13.2) and whose
    # sampler base-rate is 0.2 (which `_emit_tenant_trace` asserts). OTLP points at
    # the live collector so the BSP exports across the real process boundary.
    #
    # `placement=SELF_HOSTED_BACKEND_COLLECTOR` matches the actual R-420 route — the
    # `deploy/self-hosted-local/` compose stack is a single shared backend collector,
    # and the example toml + the R-500 operator tool bind exactly this placement for
    # the MTC posture proof. The default `CollectorConfig()` (`IN_PROCESS`) would
    # bypass the production `assert_otlp_reachable_from_sandbox` placement/reachability
    # path that `materialize_span_processor_stage` runs for an external collector, so
    # we set the real route explicitly. (The `_CELL_7` per-cell matrix-canonical
    # placements — SIDECAR_WITH_PER_TENANT_ROUTING / PER_TENANT_COLLECTOR_INSTANCE —
    # describe the *production* per-tenant physical topology the local single-collector
    # stack does not deploy; redaction is placement-independent: it strips at the
    # SpanProcessor `on_end`, BEFORE any export, so the boundary proof holds on the
    # local route. `materialize_span_processor_stage` enforces the sandbox-tier ×
    # placement reachability matrix, not the persona × surface placement matrix.)
    config = RuntimeConfig(
        deployment_surface=surface,
        persona_tier=PersonaTier.MULTI_TENANT_COMPLIANCE,
        tenant_id=_TENANT_ID,
        repository_root=tmp_path,
        path_bindings=path_bindings,
        provider_secrets=ProviderSecretsConfig(),
        otel=OTelConfig(otlp_endpoint=f"http://{_COLLECTOR_HOST}:{_COLLECTOR_OTLP_PORT}"),
        collector=CollectorConfig(placement=CollectorPlacement.SELF_HOSTED_BACKEND_COLLECTOR),
        default_topology=TopologyPattern.SINGLE_THREADED_LINEAR,
    )

    # Emit a root + child span, each carrying CONTENT_SENTINEL on two content-bearing
    # keys (gen_ai.input.messages + mcp.tool.call.arguments) and STRUCTURE_SENTINEL on
    # audit.signature.sha256, through the PRODUCTION span-processor stage
    # (RedactionSpanProcessor → BatchSpanProcessor → real OTLP exporter).
    trace_id = _emit_tenant_trace(config, flush_timeout_millis=30_000)

    records = _wait_for_trace_records(
        tempo_url=DEFAULT_TEMPO_URL,
        trace_id=trace_id,
        expected_names={ROOT_SPAN, CHILD_SPAN},
        timeout_seconds=30.0,
        query_interval_seconds=1.0,
    )

    matched = [record for record in records if record.name in {ROOT_SPAN, CHILD_SPAN}]
    assert len(matched) == 2, (
        f"expected the root + child redaction spans in Tempo, got {[r.name for r in matched]}"
    )

    for record in matched:
        # (a) content stripped pre-collector: no DEFAULT_OFF content key reached Tempo.
        leaked = DEFAULT_OFF_CONTENT_ATTRIBUTES & set(record.span_attributes)
        assert not leaked, (
            f"content-bearing attributes reached Tempo (not redacted pre-collector) "
            f"on {record.name}: {sorted(leaked)}"
        )
        # (b) positive control on the raw value: the injected content sentinel string
        # is nowhere in the surviving attribute bag.
        assert CONTENT_SENTINEL not in repr(record.span_attributes), (
            f"raw content sentinel value reached Tempo on {record.name}: {record.span_attributes!r}"
        )
        # (c) selective-redaction discriminator: the structure-bearing attribute
        # survived (so (a) is non-vacuous — redaction strips content, not everything).
        assert record.span_attributes.get("audit.signature.sha256") == STRUCTURE_SENTINEL, (
            f"structure attribute did not survive redaction on {record.name}: "
            f"{record.span_attributes!r}"
        )
