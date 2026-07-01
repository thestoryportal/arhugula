"""U-RT-02 — `RuntimeConfig` + `HarnessContext` schema tests.

ACs per Phase 2 Session 3 plan v2.1 §2 L0:
- pyright-strict clean (verified out-of-band via `uv run pyright harness-runtime/`).
- `RuntimeConfig` round-trips.
- `HarnessContext` is frozen.
"""

from __future__ import annotations

from pathlib import Path

from harness_core.deployment_surface import DeploymentSurface
from harness_cp.topology_pattern import TopologyPattern
from harness_runtime.types import (
    CollectorConfig,
    HarnessContext,
    OTelConfig,
    PathBindingConfig,
    ProviderSecretsConfig,
    RuntimeConfig,
)


def _make_runtime_config() -> RuntimeConfig:
    """Build a minimal `RuntimeConfig` with stub sub-configs.

    Sub-config validators (path existence, keyring allowlist) land at U-RT-04+
    per spec C-RT-03 'Deferred to implementation discretion' clause; at L0 the
    sub-configs are empty placeholders.
    """
    return RuntimeConfig(
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        repository_root=Path("/tmp"),
        path_bindings=PathBindingConfig(),
        provider_secrets=ProviderSecretsConfig(),
        otel=OTelConfig(otlp_endpoint="http://localhost:4318"),
        collector=CollectorConfig(),
        default_topology=TopologyPattern.SINGLE_THREADED_LINEAR,
    )


def test_runtime_config_round_trips() -> None:
    """`RuntimeConfig` survives `model_dump()` → `model_validate()` byte-equal."""
    original = _make_runtime_config()
    dumped = original.model_dump()
    rebuilt = RuntimeConfig.model_validate(dumped)
    assert rebuilt == original
    assert rebuilt.model_dump() == dumped


def test_runtime_config_is_frozen() -> None:
    """`RuntimeConfig` instances reject post-construction mutation per C-RT-03."""
    cfg = _make_runtime_config()
    # Pydantic v2 frozen models raise `ValidationError` on attribute set.
    try:
        cfg.tenant_id = "should-fail"  # type: ignore[misc]
    except Exception:
        pass
    else:
        raise AssertionError("RuntimeConfig accepted mutation; frozen invariant violated")


def test_runtime_config_rejects_unknown_keys() -> None:
    """`extra='forbid'` invariant per C-RT-03."""
    base = _make_runtime_config().model_dump()
    base["unknown_field"] = "should-fail"
    try:
        RuntimeConfig.model_validate(base)
    except Exception:
        pass
    else:
        raise AssertionError("RuntimeConfig accepted unknown field; extra='forbid' violated")


def test_runtime_config_mcp_clients_defaults_empty() -> None:
    """`mcp_clients` defaults to `[]` per C-RT-03 row 7."""
    cfg = _make_runtime_config()
    assert cfg.mcp_clients == []


def test_runtime_config_tenant_id_defaults_none() -> None:
    """`tenant_id` defaults to `None` per C-RT-03 row 9 (single-tenant mode)."""
    cfg = _make_runtime_config()
    assert cfg.tenant_id is None


def test_runtime_config_step_dispatch_timeout_seconds_default_is_30_seconds() -> None:
    """`step_dispatch_timeout_seconds` defaults to 30.0 per C-RT-03 v1.31
    (per-step worker-thread blocking bound — RT-FAIL-STEP-DISPATCH-TIMEOUT)."""
    cfg = _make_runtime_config()
    assert cfg.step_dispatch_timeout_seconds == 30.0


def test_runtime_config_step_dispatch_timeout_seconds_independent_from_drain() -> None:
    """`step_dispatch_timeout_seconds` is independent from
    `drain_timeout_seconds` per spec v1.31 §3 (per-step vs whole-workflow)."""
    cfg = _make_runtime_config().model_copy(
        update={"step_dispatch_timeout_seconds": 5.0, "drain_timeout_seconds": 120.0}
    )
    assert cfg.step_dispatch_timeout_seconds == 5.0
    assert cfg.drain_timeout_seconds == 120.0


def test_harness_context_is_frozen() -> None:
    """`HarnessContext.model_config['frozen']` is `True` per C-RT-04."""
    assert HarnessContext.model_config.get("frozen") is True


def test_harness_context_allows_arbitrary_types() -> None:
    """`HarnessContext` supports `arbitrary_types_allowed=True` per C-RT-04.

    Required so Protocol stubs + non-Pydantic axis types (PathResolver,
    WorktreeIsolationManager) compose into the frozen schema without coercion.
    """
    assert HarnessContext.model_config.get("arbitrary_types_allowed") is True


def test_harness_context_declares_all_c_rt_04_fields() -> None:
    """Every field enumerated at Spec_Harness_Runtime_v1.md v1.1 §4 is declared.

    Source: the C-RT-04 §4 table (`config`, stage 1 IS bundle, stage 2 AS
    bundle, stage 3a/3b CP bundles, stage 4 OD bundle, stage 5 LOOP_INIT
    bundle, `drained_flag`).
    """
    expected = {
        # Stage 0.
        "config",
        "drained_flag",
        # R-FS-1 arc CA — run-scoped cost-record accumulator (Stage 0 PREAMBLE;
        # defaulted list, threaded into the stage-4/5 cost wrappers; read by
        # _build_run_result for the RunResult.cost_attribution rollup, v1.53 §9).
        "cost_record_accumulator",
        # Stage 1 IS.
        "path_resolver",
        "worktree_manager",
        "shadow_git",
        "ledger_writer",
        "ledger_reader",  # v2.12 — read-view counterpart of ledger_writer
        "index",
        "cache",
        # Stage 2 AS.
        "skills",
        "tool_contracts",
        "mcp_host",
        "mcp_clients",
        "mcp_server",  # U-RT-62 — C-RT-18 §14.8.3 v1.12 H_T-as-MCP-server hosting
        "sandbox_dispatch",
        # Stage 3a CP_CLIENTS.
        "providers",
        "mcp_client_hosts",  # U-RT-72/125 — C-RT-04 §4 v1.51 H_T-as-MCP-client hosts dict (U-RT-73/126 populates)
        # Stage 3b CP_ROUTING.
        "routing_manifest",
        "engine_selector",
        "fallback_chain",
        "retry_breaker",
        "hitl_registry",
        "handoff_registry",
        # Stage 4 OD.
        "tracer_provider",
        "collector_daemon",
        "cost_chain",
        "audit_writer",
        # Stage 5 LOOP_INIT.
        "override_evaluator",
        "topology_dispatcher",
        "lifecycle_emitter",
        "llm_dispatcher",  # U-RT-52 — C-RT-15 LLM-dispatch composer
        "sub_agent_dispatcher",  # U-RT-59 — C-RT-17 §14.7 sub-agent dispatch composer
        "ask_user_question_surface",  # U-RT-60 — C-RT-18 §14.8.3 MCP-backed HITL surface
        "step_dispatchers",  # U-RT-59 — C-RT-17 §14.7.1 + §14.7.7 step-kind routing registry
        "validator_framework",  # U-CP-61 — optional ValidatorFramework binding (Decision 2.D3 RATIFIED)
        "tool_dispatcher",  # U-RT-72 — C-RT-04 §4 v1.16 retry-wrapped TOOL_STEP dispatcher (U-RT-75 populates)
        "hitl_tool_loop",  # R-CXA-2 — model-driven HITL tool-loop producer
        "engine_recovery_loop",  # R-CXA-2 — engine pause/resume recovery-loop producer
        "per_server_trust_evaluator",  # U-RT-72 — C-RT-04 §4 v1.16 (U-RT-75 populates)
        "mcp_namespace_emitter",  # U-RT-72 — C-RT-04 §4 v1.16 (U-RT-75 populates)
        "memory_tool_registry",  # U-RT-79 — C-RT-04 §4 v1.17 Memory tool storage-backend registry (U-RT-80 populates)
        # U-RT-87 (v2.20) — pause/resume protocol binding + caller-signal flag
        # per runtime spec v1.21 §4 + §14.14.3 (CP composer authoring arc).
        "pause_resume_protocol",
        "pause_requested_flag",
        # U-RT-94 (v2.24) — ResumeContextHolder sidecar for one-shot
        # ResumeContext delivery across pause-resume cycle (runtime spec v1.25
        # §4 C-RT-04 NEW field row + §14.8.8.9 carrier).
        "resume_context_holder",
        # U-RT-96 (v2.25) — WebhookDeliveryComposer binding per runtime spec
        # v1.26 §4 C-RT-04 NEW field row + §14.16 C-RT-26 factory contract
        # (Reading A path 1 absorption of fork
        # class_1_fork_u_rt_94_webhook_delivery_composer_binding_chain_absence.md).
        "webhook_delivery_composer",
        # U-RT-100 (v2.28) — SkillActivationSpanEmitter binding per runtime
        # spec v1.32 §4 C-RT-04 NEW field row + §14.17 C-RT-27 factory
        # contract (Reading B operator-opt-in MVP absorption of fork
        # class_1_fork_as_8d_skill_activation_surface_absence.md).
        "skill_activation_emitter",
        # R-FS-1 arc M (runtime spec v1.55 §4 C-RT-04 + §14.20 C-RT-28) —
        # ManagedAgentsClientProtocol carrier; bound at stage 5 when opted-in on
        # DeploymentSurface.MANAGED_CLOUD. Operator-opt-in MVP mirroring
        # skill_activation_emitter; default None preserves pre-v1.55 behavior.
        "managed_agents_client",
        # U-RT-111 (v2.36 Phase 1 plumbing) — RuntimeCpIsWiring binding
        # surface per runtime plan v2.36 §1.2 ACs #3 + #11. Operator-opt-in
        # MVP; default None preserves pre-v2.36 production behavior. Typed
        # `object | None` to avoid CP-axis dependency on harness-runtime
        # (workspace dep-graph discipline).
        "cp_is_wiring",
        # R-CXA-3 — RuntimeCpAsWiring stage-6 binding for CP-consumed AS
        # terminal seam exports. Typed `object | None` mirroring cp_is_wiring.
        "cp_as_wiring",
        # R-003 Cluster B — procedural-tier resolver binding surface. Bound at
        # bootstrap stage 6 to make_procedural_tier_snapshot_resolver(ctx);
        # consumed by the CP driver's _append_step_ledger_entry per-step ledger
        # write to populate procedural_tier_snapshot_ref (IS spec v1.3 §5.1).
        # Typed `object | None` mirroring cp_is_wiring (no CP-axis import coupling).
        "procedural_tier_snapshot_resolver",
        # B-INTERSTEP (runtime spec §14.21 C-RT-34) — run-scoped inter-step output
        # channel (the shared run-context the dispatcher reads). Bound at stage 5
        # only when RuntimeConfig.inter_step_data_flow is True; None (default) →
        # byte-identical. A plain by-reference holder (the CostRecordAccumulator
        # CA #625 precedent), typed `InterStepOutputChannel | None`.
        "inter_step_output_channel",
        # B-ENGINE-OUTPUT-REPLAY (runtime spec C-RT-32) — the durable output-carrying
        # event-history store, bound at stage 5 only when
        # RuntimeConfig.engine_output_replay is True; None (default) → byte-identical.
        # A by-reference holder typed `EngineOutputStore | None`.
        "engine_output_store",
        # R-CL-P4 (runtime spec v1.x §4 C-RT-04 NEW field row) — prompts-
        # management carrier read by resolve_procedural_tier_snapshot as the
        # third procedural-tier hash component (IS spec v1.5 §C-IS-05 §5.2;
        # fork class_1_fork_prompts_management_surface_active_prompt_version.md).
        # Empty-defaultable PromptManifest mirroring routing_manifest.
        "prompt_manifest",
    }
    actual = set(HarnessContext.model_fields.keys())
    assert actual == expected, f"missing: {expected - actual}; extra: {actual - expected}"
