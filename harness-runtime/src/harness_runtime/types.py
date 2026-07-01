"""`harness_runtime.types` — runtime composition primitives + schemas.

Authority:
- `design-substrate/Spec_Harness_Runtime_v1.md` v1.1 §3 (C-RT-03 `RuntimeConfig`)
  and §4 (C-RT-04 `HarnessContext`).
- Class 2 Tension 2026-05-19: 13 axis-typed `HarnessContext` fields name types
  absent from the landed library
  (`.harness/class_2_tension_phase_2_session_5_harness_context_axis_type_mapping.md`).
  Per the operator-confirmed Class 2 resolution, those types are declared
  here as `typing.Protocol` stubs; L2-L6 units narrow or concretize via
  implementations in `harness_runtime.lifecycle.*` / `harness_runtime.wiring.*`.

Module scope at L0 (U-RT-02):
- `BootstrapStage` lands at U-RT-03 (separate L0 unit), not here.
- `_MutableHarnessContext` builder is implementation-discretion (spec §4) and
  is deferred to U-RT-43 (the bootstrap orchestrator).
- Field-level Pydantic validators on `RuntimeConfig` (path-existence checks,
  allowlist-key enforcement) land at U-RT-04 (config precedence resolver),
  per the spec's "Deferred to implementation discretion" clause.

What this module ships at L0:
- `RuntimeConfig` — frozen Pydantic v2 schema; round-trips with empty sub-configs.
- `HarnessContext` — frozen Pydantic v2 schema with `arbitrary_types_allowed=True`;
  every C-RT-04 field declared with its spec-typed surface.
- Sub-config placeholders (empty `BaseModel`s that L1 units enrich).
- 13 `Protocol` stubs for the Class 2 unresolved axis types.
- 5 `Protocol` stubs for the spec-acknowledged runtime-defined types.
- 1 `ProviderClient` `Protocol` (C-RT-05; concretized at U-RT-17/18/19/20).
- 1 local `ToolName` NewType. `SkillID` + `ClientName` were promoted to
  `harness_core.identity` at Session 5; `ToolName` stays local pending a
  cross-axis naming-convention pass (see CP precedent at
  `harness_cp.hitl_as_tool_call_rewriting:38` `type ToolName = str`).
"""

from __future__ import annotations

import asyncio
import contextvars
import re
from collections.abc import Mapping
from enum import Enum, StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, NewType, Protocol, Self, runtime_checkable

# ----------------------------------------------------------------------------
# Concrete axis-type imports (the 6 names that resolve at HEAD).
# ----------------------------------------------------------------------------
from harness_as.discriminators import MCPTransport
from harness_as.sandbox_tier import BlastRadiusTier, SandboxTier
from harness_as.sandbox_tier_floor import MCPServerTrustLevel
from harness_as.tool_contract import SecretAllowlistEntry, ToolContract
from harness_core import ClientName, SandboxDecisionPolicy, SkillID
from harness_core.deployment_surface import DeploymentSurface
from harness_core.identity import ActionID
from harness_core.persona_tier import PersonaTier
from harness_core.workflow_event_class import WorkflowEventClass
from harness_core.workload_class import WorkloadClass
from harness_cp.brief_authoring_inheritance import BriefAuthoringInheritance
from harness_cp.cp_shared_types import ModelBinding
from harness_cp.cross_family_fallback_chain import FallbackChain
from harness_cp.engine_class import EngineClass
from harness_cp.gate_level_rule import GateLevel as CPGateLevel
from harness_cp.hitl_as_tool_call_rewriting import (
    HITLSemanticVariant,
    RewrittenToolCall,
)
from harness_cp.hitl_timeout_degradation import TimeoutDegradationKind

# U-RT-87 — PauseResumeProtocol class carrier import (per spec v1.21 §4
# C-RT-04 field-table extension). Class body landed at U-CP-62 cluster 10-CP-B.
from harness_cp.pause_resume_protocol import PauseResumeProtocol, ResumeOutcomeKind
from harness_cp.per_server_trust_types import TrustPolicy
from harness_cp.per_step_override_evaluator import CPAuditLedgerEntry, StepEffectiveBinding
from harness_cp.persona_engine_hitl_matrix import SynchronyClass
from harness_cp.prompt_selection_manifest import PromptSelectionManifest
from harness_cp.routing_manifest_residence import RetryPolicy, RoutingManifest
from harness_cp.sub_agent_brief import SubAgentBrief
from harness_cp.sub_agent_gate_level_descent import (
    GateOverride,
    SubAgentGateLevelDescent,
)
from harness_cp.topology_pattern import TopologyPattern
from harness_cp.validator_fail_taxonomy import ValidatorRetryExitClass
from harness_cp.validator_fail_transient_staircase import (
    CrossTrustBoundaryState,
    StaircaseStage,
    StaircaseTransition,
)
from harness_cp.validator_framework_types import ValidatorFramework
from harness_cp.workflow_driver import StepDispatcherRegistry as _CpStepDispatcherRegistry
from harness_cp.workflow_driver_types import StepExecutionContext, WorkflowStep
from harness_cp.workflow_manifest_entry import WorkflowManifestEntry
from harness_cp.workload_binding_engine_class_selection import HITLInvocation
from harness_is.path_resolver import PathResolver
from harness_is.prompt_manifest import PromptManifest, PromptVersion
from harness_is.workload_manifest_opt_in_schema import WorkloadManifestOptIns
from harness_is.worktree_isolation import WorktreeIsolationManager
from harness_od.harness_breaker_schema import BreakerScope
from harness_od.idempotency_join_dedup import (
    DedupOutcome,
    F2StateLedgerEntry,
    SpanCostRecord,
    SpanIngestionView,
)
from harness_od.local_first_otlp_collector import (
    BATCH_SPAN_PROCESSOR_BATCH_SIZE,
    BATCH_SPAN_PROCESSOR_WINDOW_SECONDS,
)
from harness_od.otel_genai_base import EventEmission, SpanRef
from harness_od.per_cell_collector_placement_matrix import CollectorPlacement
from harness_od.sampling_mode import SamplingMode
from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

# B-ENGINE-OUTPUT-REPLAY — durable output store (runtime spec C-RT-32). No harness
# imports in that module → no import cycle.
from harness_runtime.lifecycle.engine_output_store import EngineOutputStore

# U-RT-116 — HITL auto-approve policy carrier import (per spec v1.49 §3 C-RT-03
# field-table extension + §3.8 sub-model). HITLAutoApprovePolicy declared at U-RT-116.
from harness_runtime.lifecycle.hitl_auto_approve_policy import HITLAutoApprovePolicy

# B-INTERSTEP — InterStepOutputChannel by-reference holder (runtime spec §14.21
# C-RT-34, new at v1.59). No harness imports in that module → no import cycle.
from harness_runtime.lifecycle.inter_step_output_channel import InterStepOutputChannel

# C-RT-28 §14.20 (R-FS-1 arc M) — ManagedAgents executable-consumer carriers.
# `ManagedAgentsConfig` is the RuntimeConfig opt-in sub-model (§14.20.1);
# `ManagedAgentsClientProtocol` is the HarnessContext client field type.
# Operator-ratified 2026-06-17 (Option B; new StepKind.MANAGED_AGENTS).
from harness_runtime.lifecycle.managed_agents import ManagedAgentsClientProtocol
from harness_runtime.lifecycle.managed_agents_dispatch import ManagedAgentsConfig

# U-RT-79 — Memory tool backend config carrier import (per spec v1.17 §3 C-RT-02
# field-table extension). MemoryToolBackendConfig declared at U-RT-76.
from harness_runtime.lifecycle.memory_tool_types import MemoryToolBackendConfig

# U-RT-87 — Pause/resume protocol config carrier import (per spec v1.21 §3
# C-RT-02 field-table extension). PauseResumeProtocolConfig declared at U-RT-87.
from harness_runtime.lifecycle.pause_resume_protocol_types import (
    PauseResumeProtocolConfig,
)

# U-RT-94 — ResumeContextHolder sidecar import (per spec v1.25 §4 C-RT-04
# NEW field row + §14.8.8.9 carrier definition).
from harness_runtime.lifecycle.resume_context_holder import ResumeContextHolder

# U-RT-99 — SkillActivationHookConfig + SkillActivationSpanEmitter carriers
# per runtime spec v1.32 §14.17 (NEW C-RT-27). Operator-opt-in MVP shape per
# .harness/class_1_fork_as_8d_skill_activation_surface_absence.md Reading B
# (2026-05-28). Closes H_T-AS-8d producer-site absence at STILL-BOUNDED →
# RETIRE-READY transit.
from harness_runtime.lifecycle.skill_activation import (
    SkillActivationHookConfig,
    SkillActivationSpanEmitter,
    UnknownSkillError,
)

# Concrete carriers re-exported as the canonical field types (realizations of
# the former empty `Protocol` stubs at U-RT-12 / U-RT-13). The empty Protocols
# broke assignment between the concrete (what the loaders/wiring return) and the
# Protocol-typed fields; their modules do not import this module nor reference
# api-level types, so the runtime re-export is cycle-free and Pydantic-safe.
from harness_runtime.lifecycle.skills import Skill
from harness_runtime.lifecycle.state_ledger import LedgerWriter
from harness_runtime.lifecycle.validator_framework_types import (
    ValidatorFrameworkConfig,
)

# U-RT-96 — WebhookDeliveryComposer class carrier import (per spec v1.26 §4
# C-RT-04 field-table extension). Class body landed at U-RT-69 in
# lifecycle/webhook_delivery_composer.py.
from harness_runtime.lifecycle.webhook_delivery_composer import (
    WebhookDeliveryComposer,
)

# U-RT-96 — WebhookDeliveryComposer config carrier import (per spec v1.26 §3
# C-RT-02 field-table extension). WebhookDeliveryComposerConfig declared at
# U-RT-96 in lifecycle/webhook_delivery_composer_types.py.
from harness_runtime.lifecycle.webhook_delivery_composer_types import (
    WebhookDeliveryComposerConfig,
)

if TYPE_CHECKING:
    from harness_is.state_ledger_write import WriteResult
    from harness_od.audit_ledger_types import AuditLedgerEntry

__all__ = [
    "AuditLedgerWriter",
    "BootstrapStage",
    "ClientName",
    "CollectorConfig",
    "CollectorDaemonHandle",
    "ContentAddressedIndex",
    "CostAttributionChain",
    "EngineSelector",
    "ExternalCLIProviderConfig",
    "ExternalCLIProviderKind",
    "HITLPlacementRegistry",
    "HandoffRegistry",
    "HarnessContext",
    "HarnessMCPServer",
    "LLMDispatcher",
    "LedgerReader",
    "LedgerWriter",
    "LifecycleEventEmitter",
    "MCPClient",
    "MCPClientConfig",
    "MCPHost",
    "OTelConfig",
    "PathBindingConfig",
    "PerStepOverrideEvaluator",
    "ProviderClient",
    "ProviderSecretBackend",
    "ProviderSecretsConfig",
    "RetryBreakerRegistry",
    "RuntimeConfig",
    "SandboxDispatchTable",
    "SemanticCache",
    "ServerName",
    "ShadowGitSupervisor",
    "Skill",
    "SkillID",
    "StageLifecycleHook",
    "StageResult",
    "ToolName",
    "TopologyDispatcher",
]


# ----------------------------------------------------------------------------
# `BootstrapStage` - C-RT-01 v1.1 9-value enum, fixed order.
# Total enum cardinality = 9; file count = 9 (with stage_3a + stage_3b split).
# ----------------------------------------------------------------------------
class BootstrapStage(Enum):
    """The 9 bootstrap stages of the runtime, in fixed traversal order.

    Per `Spec_Harness_Runtime_v1.md` v1.1 §1 (C-RT-01) the order is normative:
    `list(BootstrapStage)` MUST equal `[PREAMBLE, IS, AS, CP_CLIENTS,
    CP_ROUTING, OD, LOOP_INIT, CXA_WIRING, INGRESS_ACCEPT]`. The two stage-3
    members (`CP_CLIENTS`, `CP_ROUTING`) correspond to file-naming convention
    `stage_3a_*.py` / `stage_3b_*.py`.

    Invariants (C-RT-01):
    - `len(BootstrapStage) == 9`.
    - No stage runs before its strict predecessor completes (orchestrator
      invariant; see C-RT-02).
    - The enum is immutable across v1; adding a stage is a v2.0 event.
    """

    PREAMBLE = 0
    IS = 1
    AS = 2
    CP_CLIENTS = 3  # stage 3a
    CP_ROUTING = 4  # stage 3b
    OD = 5
    LOOP_INIT = 6
    CXA_WIRING = 7
    INGRESS_ACCEPT = 8


# ----------------------------------------------------------------------------
# `StageResult` - return shape of a single stage's `execute()` call.
# Per C-RT-02 "implementation discretion": minimal shape at L0; per-stage
# extensions (e.g., per-stage post-condition attestations) land with the
# stage units (L1-L9).
# ----------------------------------------------------------------------------
class StageResult(BaseModel):
    """Result of a single bootstrap stage's `execute()` call (C-RT-02).

    On success, names the stage that produced the result. Failure modes
    raise typed exceptions per the runtime-local fail-class taxonomy
    (C-RT-14); stages do not return failure results - the orchestrator
    treats a returned `StageResult` as success and an exception as the
    `RT-FAIL-BOOTSTRAP` / `RT-FAIL-TRANSIENT` / `RT-FAIL-PARTIAL-ROLLBACK-
    REQUIRED` taxonomy entry.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    stage: BootstrapStage
    """The stage that produced this result."""


# ----------------------------------------------------------------------------
# `StageLifecycleHook` - per-stage entry + exit hook stub.
# Per C-RT-01 invariant "Each stage emits exactly one workflow_event_class
# lifecycle event on entry and exit". Concretized at U-RT-41 (lifecycle
# event emission); at L0 this is a structural Protocol stub.
# ----------------------------------------------------------------------------
@runtime_checkable
class StageLifecycleHook(Protocol):
    """Per-stage entry/exit hook (C-RT-01 lifecycle invariant; U-RT-41)."""


# ----------------------------------------------------------------------------
# Identity NewTypes — local-only.
#
# `SkillID` and `ClientName` are promoted to `harness_core.identity` and
# imported above. `ToolName` stays local: `harness_cp.hitl_as_tool_call_
# rewriting` already carries `type ToolName = str` with documented "future
# cross-axis decision" rationale; promoting here would force a concurrent CP
# refactor + a cross-axis naming-convention pass. Deferred to that pass.
#
# `ServerName` stays local for the same reason: the cross-host MCP routing
# index + the `HarnessContext.mcp_client_hosts` carrier (U-RT-125 / runtime
# spec v1.51 §14.9.10 D1) are runtime-side. It aliases the per-deployment
# `MCPClientHost.server_name` registry ID — DISTINCT from the config-side
# `ClientName` (the two hold the same value today; `server_name=client_name`
# at the factory), preserving the config-key/runtime-identity split as a
# forward property. Promote to `harness_core` only if a cross-axis consumer
# surfaces (mirrors the `ClientName` promotion).
# ----------------------------------------------------------------------------
ToolName = NewType("ToolName", str)
ServerName = NewType("ServerName", str)


# ----------------------------------------------------------------------------
# Sub-config placeholders (Pydantic BaseModel stubs).
# L1 units (U-RT-04..U-RT-08) enrich these with concrete fields + validators.
# ----------------------------------------------------------------------------
class PathBindingConfig(BaseModel):
    """Path-binding input — U-RT-05 (L1).

    Holds the raw path-binding entry records the operator declares plus the
    workload-manifest opt-in declaration that gates shadow-Git checkpoint
    cadence and worktree-isolation concurrency. The runtime materializes a
    validated `harness_is.PathBinding` via `config.path_bindings.build_path_binding`
    at stage 1 IS bootstrap (U-RT-10).

    Per C-IS-08 §8.1 the opt-ins default to all-off; downstream stage 1 units
    fail-open on the (unset → off) interpretation rather than requiring an
    explicit declaration at every site.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    raw_entries: tuple[Mapping[str, object], ...] = ()
    """Raw `(path_class, workflow_class, deployment_surface, path)` records.

    Validated into `PathBindingEntry` instances by `load_path_binding` at
    `build_path_binding` time (U-RT-05).
    """

    opt_ins: WorkloadManifestOptIns = Field(default_factory=WorkloadManifestOptIns)
    """Workload-class opt-in declaration (shadow-Git + worktree).

    Defaults to all-off per C-IS-08 §8.1 / C-IS-09 §9.1.
    """


class ProviderSecretBackend(StrEnum):
    """Provider-secret backend selector."""

    LOCAL_KEYRING_ENV_FALLBACK = "local-keyring-env-fallback"
    SELF_HOSTED_KEYRING = "self-hosted-keyring"
    GCP_SECRET_MANAGER = "gcp-secret-manager"


_GCP_PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")


class ProviderSecretsConfig(BaseModel):
    """Provider-secret config — U-RT-06 (L1).

    Holds the OS-keyring service identifier + the operator-policy allowlist
    per C-AS-06 §6.2. Secret VALUES never live in this config; only ALLOWLIST
    KEYS. Per ADR-F5 v1.1 + `Target_Stack_Commitment_v1.md` §5.1 the runtime
    binds `python-keyring` as the keyring library (AS spec §5.4 defers this
    binding to implementation discretion).

    The driver is built at `config.provider_secrets.make_keyring_resolver`
    and invoked at tool-fetch time (post-L3); audit-event composition
    (SecretFetchEvent) is the CALLER's responsibility per U-AS-26 separation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: ProviderSecretBackend = ProviderSecretBackend.LOCAL_KEYRING_ENV_FALLBACK
    """Backend selector for provider-secret resolution."""

    keyring_service: str = "harness"
    """OS-keyring service-name identifier (python-keyring `service` arg)."""

    gcp_project_id: str | None = None
    """GCP project id for the `gcp-secret-manager` backend."""

    gcp_secret_version: str = "latest"
    """GCP Secret Manager version selector used for provider-secret names."""

    operator_allowlist: tuple[SecretAllowlistEntry, ...] = ()
    """Operator-policy allowlist (C-AS-06 §6.2 override set).

    `tuple[SecretAllowlistEntry, ...]` (Pydantic-friendly); converted to
    `frozenset` at resolver-construction time for `check_secret_allowlist`.
    Empty default means no operator-allowlisted secrets — every fetch is
    DENIED_NOT_IN_OPERATOR_POLICY_OVERRIDE until populated.
    """

    @field_validator("gcp_secret_version")
    @classmethod
    def _gcp_secret_version_non_empty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("gcp_secret_version must be non-empty")
        return stripped

    @field_validator("gcp_project_id")
    @classmethod
    def _gcp_project_id_is_resource_identifier(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            return stripped
        if stripped.isdecimal() or _GCP_PROJECT_ID_RE.fullmatch(stripped):
            return stripped
        raise ValueError(
            "gcp_project_id must be a Google Cloud project ID or numeric project number; "
            "do not use the display name such as 'My First Project'"
        )

    @model_validator(mode="after")
    def _require_gcp_project_for_gcp_backend(self) -> Self:
        if (
            self.backend is ProviderSecretBackend.GCP_SECRET_MANAGER
            and not (self.gcp_project_id or "").strip()
        ):
            raise ValueError("gcp_project_id is required when backend is gcp-secret-manager")
        return self


class OTelConfig(BaseModel):
    """OTel runtime config — U-RT-07 (L1).

    Carries the OTLP endpoint, an optional sampling-mode override, and
    operator-supplied additional resource attributes. The sampler mode
    defaults to the per-deployment-surface mapping at C-OD-09 §9.1
    (`PER_DEPLOYMENT_SURFACE_SAMPLING` in `harness_od.sampling_mode`); a
    non-None override here wins (operator-tunable for self-hosted-server
    deployments running mixed regimes).

    Endpoint validation runs at construction time per the field validator
    (URL must include `://`); detailed schema validation (gRPC vs HTTP) is
    deferred to U-RT-27 (TracerProvider construction).

    Resource attributes for the 12 ADR-D6 v1.2 §1.2 namespaces are built at
    `config.otel_config.build_resource_attributes()` from `deployment_surface`
    + `additional_resource_attrs`; not stored on the config itself (derived).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    otlp_endpoint: str
    """OTLP exporter endpoint URL (e.g. `http://localhost:4318`)."""

    sampling_mode: SamplingMode | None = None
    """Optional override of the per-deployment-surface default (C-OD-09 §9.1)."""

    additional_resource_attrs: tuple[tuple[str, str], ...] = ()
    """Operator-supplied additional resource attrs; merged into the OTel
    resource at TracerProvider construction (U-RT-27)."""

    @field_validator("otlp_endpoint")
    @classmethod
    def _endpoint_has_scheme(cls, value: str) -> str:
        """Reject endpoints without a `://` scheme separator at construction time."""
        if "://" not in value:
            raise ValueError(
                f"otlp_endpoint must include a `://` scheme (got {value!r})",
            )
        return value


class CollectorConfig(BaseModel):
    """In-process collector daemon config — U-RT-08 (L1).

    Carries the placement selection (architectural class per C-OD-20 §20.1),
    the runtime bootstrap sandbox tier used for C-OD-20 reachability checks,
    ring-buffer size, sqlite rotation thresholds, and BatchSpanProcessor
    cadence inherited from C-OD-19 §19.1 defaults. The collector daemon
    supervisor at U-RT-29 (F-P2-5) consumes these settings.

    All numeric thresholds are validated as positive at construction time;
    defaults match the OD-spec-committed BSP constants
    (`BATCH_SPAN_PROCESSOR_WINDOW_SECONDS=5`, `BATCH_SPAN_PROCESSOR_BATCH_SIZE=512`).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    placement: CollectorPlacement = CollectorPlacement.IN_PROCESS
    """Architectural collector placement (C-OD-20 §20.1). Defaults to
    `IN_PROCESS` per F-P2-5 (runtime owns the in-process collector daemon)."""

    bootstrap_sandbox_tier: SandboxTier = SandboxTier.TIER_1_PROCESS
    """Sandbox tier that materializes the runtime bootstrap span processor.

    The default preserves LOCAL/self-hosted host-process bootstrap behavior.
    MANAGED_CLOUD/FULL_VM deployment bindings may set `TIER_4_FULL_VM` so
    bootstrap OTLP reachability is checked against the actual network-capable
    runtime tier instead of weakening the Tier-1 matrix.
    """

    ring_buffer_size: int = 4096
    """Span ring-buffer capacity for the collector daemon (bounded > 0)."""

    sqlite_rotation_max_rows: int = 100_000
    """Row-count rotation threshold for the collector sqlite store (> 0)."""

    sqlite_rotation_max_bytes: int = 100_000_000
    """Byte-size rotation threshold for the collector sqlite store (> 0)."""

    batch_window_seconds: int = BATCH_SPAN_PROCESSOR_WINDOW_SECONDS
    """BSP batching window in seconds (C-OD-19 §19.1 default = 5; > 0)."""

    batch_size: int = BATCH_SPAN_PROCESSOR_BATCH_SIZE
    """BSP batch size (C-OD-19 §19.1 default = 512; > 0)."""

    sqlite_retention_days: int = 7
    """Retention horizon for sqlite span-store rows per OD spec v1.8 §C-OD-27.2
    row 3 (operator-configurable; default 7 days). U-OD-44 lazy-on-write
    cleanup applies this at every `RuntimeRingBuffer.flush_to_sqlite` call."""

    tail_keep_max_buffered_traces: int = 4096
    """Operator-tunable ceiling on the number of traces the production
    `TailKeepSpanProcessor` buffers pending root-close, per OD spec §C-OD-09
    §9.3 (the implementer-discretion bounded-buffer follow-on closed at OD spec
    v1.28). A pathological producer that opens roots without ever closing them
    would otherwise accumulate without bound (v1.27 §2(a) carve-out). When the
    ceiling is reached, the oldest buffered trace is evicted (drop-oldest) and
    counted at `TailKeepSpanProcessor.dropped_trace_count`. Default 4096 matches
    the ring-buffer scale; only pathological producers reach it (legitimate
    traces close fast and free their slot)."""

    tail_keep_max_spans_per_trace: int = 4096
    """Operator-tunable ceiling on the number of non-always-sampled spans the
    production `TailKeepSpanProcessor` buffers for a single trace pending
    root-close, per OD spec §C-OD-09 §9.3 (v1.28 closure). Bounds a single
    never-closing trace from accumulating spans without limit. Overflow
    non-root spans are dropped and counted at
    `TailKeepSpanProcessor.dropped_span_count`; the root-close span always
    processes so the trace can materialize and free its slot."""

    @field_validator(
        "ring_buffer_size",
        "sqlite_rotation_max_rows",
        "sqlite_rotation_max_bytes",
        "batch_window_seconds",
        "batch_size",
        "sqlite_retention_days",
        "tail_keep_max_buffered_traces",
        "tail_keep_max_spans_per_trace",
    )
    @classmethod
    def _positive(cls, value: int) -> int:
        """All collector thresholds must be strictly positive."""
        if value <= 0:
            raise ValueError(f"value must be > 0 (got {value})")
        return value


class SandboxDriverConfig(BaseModel):
    """Per-server tool-execution-driver config — runtime spec v1.43 §14.9.9 (FR-1).

    Carries the operator-supplied parameters needed to construct the sandboxed
    `ToolExecutionDriver` selected for a server's resolved sandbox tier
    (`> TIER_1_PROCESS`). `TIER_1_PROCESS` needs no config (in-process host
    driver). For `TIER_2_CONTAINER` / `TIER_3_MICROVM`, `image` + `command` are
    required (the in-container JSON runner). For `TIER_4_FULL_VM` (E2B),
    `command` is required (the in-sandbox JSON runner; the E2B provider
    credential is env-sourced by the `e2b` SDK, not carried here).

    Absence of this config when the resolved tier is `> TIER_1_PROCESS` is
    `RT-FAIL-SANDBOX-DRIVER-UNAVAILABLE` (FR-2(i), fail-loud at bootstrap) — the
    factory never silently falls through to in-process execution.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: tuple[str, ...]
    """In-sandbox JSON tool-runner argv (reads one JSON object from stdin, writes
    one JSON object to stdout). Required for every non-`TIER_1_PROCESS` driver."""

    image: str | None = None
    """Container image for `TIER_2_CONTAINER` / `TIER_3_MICROVM` drivers. Required
    for those tiers (absence → `RT-FAIL-SANDBOX-DRIVER-UNAVAILABLE`); ignored for
    `TIER_4_FULL_VM` (E2B uses an env-configured template)."""

    network: str = "none"
    """Container network mode (`TIER_2`/`TIER_3`). Defaults to `"none"` (egress-off)."""

    docker_binary: str = "docker"
    """Container CLI binary (`TIER_2`/`TIER_3`)."""

    timeout_seconds: float = 30.0
    """Per-call execution timeout."""

    sandbox_timeout_seconds: int = 60
    """`TIER_4_FULL_VM` (E2B) sandbox lifetime ceiling."""

    allow_internet_access: bool = False
    """`TIER_4_FULL_VM` (E2B) network-egress toggle. Defaults to egress-off."""


class MCPClientConfig(BaseModel):
    """MCP client connection config — U-RT-15 (L3).

    Carries the per-client transport + trust-level surface that
    `harness_as.mcp_transport_floor` validates at stage 2 AS bootstrap.
    Real connection URL + auth-secret reference are operator-supplied;
    the connection-URL schema (stdio: command line; remote: HTTP URL) is
    runtime implementation-discretion at L3.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    client_name: ClientName
    """Operator-supplied client identifier — key in `HarnessContext.mcp_clients`."""

    transport: MCPTransport
    """Transport class (C-AS-10 §10.1 — stdio / streamable_http / ssecached)."""

    trust_level: MCPServerTrustLevel
    """Trust-tier framework class (C-AS-10 §10.3) — gates remote registration."""

    blast_radius: BlastRadiusTier
    """Blast-radius capability of the tool surfaces this client proxies."""

    connection_url: str
    """Stdio command-line OR remote HTTP URL (per `transport`)."""

    default_minimum_tier: SandboxTier | None = None
    """Operator-declared per-server default sandbox tier (Reading B, spec v1.40
    §14.9.3). The stage-3a factory builds a default-policy
    `MCPToolContractConverter` that stamps every tool discovered from this
    server with this `minimum_tier`.

    `None` (the default) defers to the **deployment-surface-aware default policy**
    (runtime spec v1.43 §14.9.9 + fork §7.1, Reading A+): resolved at the factory
    from `RuntimeConfig.deployment_surface` via
    `harness_runtime.config.sandbox_defaults.resolve_effective_sandbox_defaults`
    (`local-development → TIER_1_PROCESS`; `self-hosted-server`/`managed-cloud →
    TIER_2_CONTAINER`). Reconciled with `default_sandbox_tier` by the same helper
    so the §14.9.4 tier-floor never spuriously violates on a bare config."""

    default_blast_radius: BlastRadiusTier = BlastRadiusTier.READ_ONLY
    """Operator-declared per-server default blast radius (Reading B, spec v1.40
    §14.9.3). The stage-3a converter stamps every discovered tool's
    `blast_radius_tier` with this value. Conservative default per fork §0.

    NOTE (spec v1.40 Class 3 finding): overlaps the pre-existing unconsumed
    `blast_radius` field above; consolidation owed at a future hygiene arc
    (requires operator ratification per X-AL-3)."""

    default_forces_computer_use: bool = False
    """Operator-declared per-server default for the §2.2 `ToolMetadata.forces_computer_use`
    discriminator (B6 Slice 2, runtime spec v1.56 §14.9.11). The stage-3a converter stamps
    every discovered tool's `ToolContract.forces_computer_use` with this value — MCP
    advertisements do not carry the H_T forcing semantics, so a per-server default is the
    Reading-B policy source (sibling to `default_blast_radius`). An operator declaring a
    computer-use MCP server raises ALL its tools to TIER_4 (C-AS-02 §2.3 row 1) at the
    per-tool resolver. Conservative default `False`; a heterogeneous server supplies a
    custom `MCPToolContractConverter` for per-tool granularity."""

    default_forces_code_execution: bool = False
    """Operator-declared per-server default for `ToolMetadata.forces_code_execution`
    (→ TIER_4, C-AS-02 §2.3 row 2), stamped by the stage-3a converter (B6 Slice 2).
    Conservative default `False`."""

    default_is_deterministic_inhouse: bool = False
    """Operator-declared per-server default for `ToolMetadata.is_deterministic_inhouse`
    (C-AS-02 §2.3 row 7 — read-only deterministic in-house → TIER_1, bounded below by the
    surface default + blast floor), stamped by the stage-3a converter (B6 Slice 2).
    Conservative default `False`."""

    default_idempotent: bool = False
    """Operator-declared per-server default for `ToolContract.idempotent` (AS spec
    C-AS-03 §3.1 v1.12 — `B-EFFECT-FENCE-PER-TOOL`), stamped by the stage-3a converter
    onto every discovered tool. When the runtime effect fence (§14.22 / §14.22.7) is
    active for a run, a tool with `idempotent=True` is NOT reserved (fires + retryable;
    re-execution has no additional effect). MCP advertisements carry no idempotency
    semantics, so this per-server default is the policy source for discovered tools (a
    read-only data server declares `default_idempotent=True`; a heterogeneous server
    supplies a custom `MCPToolContractConverter` for per-tool granularity). Conservative
    default `False` → fenced (byte-identical to pre-v1.12). NOT a sandbox discriminator."""

    default_sandbox_tier: SandboxTier | None = None
    """Operator-declared per-server default *resolved* sandbox tier (Reading B,
    spec v1.41 §14.9.8). The stage-5 factory builds a per-server default-policy
    `SandboxDecisionResolver` that returns this tier for every TOOL_STEP dispatch
    against this server. Distinct from `default_minimum_tier` (the tool's REQUIRED
    floor): the §14.9.4 tier-floor check compares resolved `default_sandbox_tier`
    >= `default_minimum_tier`.

    `None` (the default) defers to the deployment-surface-aware default policy
    (runtime spec v1.43 §14.9.9 + fork §7.1, Reading A+) — see
    `default_minimum_tier`. The helper reconciles both to the same surface-derived
    tier so a bare local-development config is honest `TIER_1_PROCESS` in-process
    (no lie, no spurious floor violation), and production surfaces default to
    `TIER_2_CONTAINER` (FR-2(i) fail-loud unless a `sandbox_driver` is configured).
    An explicit value overrides the policy."""

    default_sandbox_tech: str | None = None
    """Operator-declared per-server default sandbox mechanism (Reading B, spec
    v1.41 §14.9.8) — emitted on the `sandbox.enter` span per §14.9.4. `None`
    defers to the surface-aware default policy (derived to match the effective
    `default_sandbox_tier`, e.g. `"host-process"` for `TIER_1_PROCESS`)."""

    default_sandbox_provider: str | None = None
    """Operator-declared per-server default sandbox provider (Reading B, spec
    v1.41 §14.9.8) — emitted on the `sandbox.enter` span per §14.9.4. `None`
    defers to the surface-aware default policy (derived to match the effective
    `default_sandbox_tier`)."""

    sandbox_driver: SandboxDriverConfig | None = None
    """Per-server tool-execution-driver config (runtime spec v1.43 §14.9.9 FR-1).
    Required when the resolved sandbox tier is `> TIER_1_PROCESS` — absence is
    `RT-FAIL-SANDBOX-DRIVER-UNAVAILABLE` (FR-2(i), fail-loud at the stage-5
    factory). `None` is valid only when the resolved tier is `TIER_1_PROCESS`
    (in-process host driver; the local-development out-of-box default)."""


# ----------------------------------------------------------------------------
# Protocol stubs - spec-acknowledged runtime-defined types (C-RT-04).
# Empty bodies at L0; method shapes narrow as L2-L6 units consume them.
# ----------------------------------------------------------------------------
@runtime_checkable
class ShadowGitSupervisor(Protocol):
    """Runtime-defined per C-RT-04. Concretized at U-RT-11."""


@runtime_checkable
class LedgerReader(Protocol):
    """Runtime-defined read-side wrapper around IS state-ledger.

    Concretized at `harness_runtime.lifecycle.state_ledger.LedgerReader`
    (added at CP plan v2.12 to resolve `[[fork-u-cp-56-resumption-underspec]]`).
    Structurally satisfies the CP-axis
    `harness_cp.workflow_driver.LedgerReaderLike` Protocol.
    """


@runtime_checkable
class AuditLedgerWriter(Protocol):
    """Runtime-defined wrapper around IS+OD audit-ledger. Concretized at U-RT-32.

    Narrowed to declare the writer's reference-time surface (the `append`
    method `RuntimeAuditLedgerWriter` implements at U-RT-32) so consumers —
    the cost-attribution dispatch hooks + the CP audit-write seam — compose
    against a documented API instead of an empty Protocol body.
    """

    def append(
        self,
        tenant_id: str | None,
        audit_entry: AuditLedgerEntry,
    ) -> WriteResult:
        """Persist one pre-signed `AuditLedgerEntry` into the IS hash chain."""
        ...


@runtime_checkable
class CollectorDaemonHandle(Protocol):
    """Runtime-defined supervisor handle (F-P2-5; C-RT-07). Concretized at U-RT-29."""


@runtime_checkable
class LifecycleEventEmitter(Protocol):
    """Runtime-defined `workflow_event_class` emitter (U-RT-41 PARTIAL-LAND).

    Concretized by
    `harness_runtime.lifecycle.lifecycle_emitter.RuntimeLifecycleEventEmitter`.
    Narrowed at U-RT-41 to declare the emitter's reference-time surface.
    The emitter emits any of the 8 canonical `WorkflowEventClass` values
    (per `harness_core.workflow_event_class` — C-CP-05 §5.1 verbatim);
    consumers (workflow execution at U-RT-42+) bind emit calls to the
    lifecycle loop's hook surfaces. The emitter records every emit in
    an in-memory ring (test-introspectable) so the L9 verification
    suite can assert event ordering.

    **PARTIAL-LAND scope.** C-RT-11 §11 step 2's
    `WorkflowEventClass.DRAINED` emit is STRUCK at this landing
    (`WorkflowEventClass` is closed at cardinality 8 per `harness_core`
    — no `DRAINED` value). Spec §16 open question #9 explicitly
    authorizes split. Class 1 record at
    `.harness/class_1_tension_u_rt_41_drained_event_class_alignment.md`;
    operator-decision pending. Drain observability remains via the two
    other C-RT-11 surfaces — `ctx.drained_flag` (signal) +
    `RunResult.status='drained'` (terminal return).
    """

    def emit(self, event_class: WorkflowEventClass) -> None:
        """Emit a lifecycle event of the given canonical class."""
        ...

    def emit_bootstrap_stage_complete(self, stage: BootstrapStage) -> None:
        """Emit one bootstrap-stage-complete lifecycle record (U-RT-43 AC #3).

        Distinct surface from `emit()` because `WorkflowEventClass` is
        closed at cardinality 8 (no `DRAINED` / no bootstrap entries) and
        addresses workflow lifecycle, not bootstrap. The bootstrap
        orchestrator emits one such record per stage post-bootstrap.
        """
        ...


# ----------------------------------------------------------------------------
# Protocol stubs - Class 2 unresolved axis-typed names (Tension 2026-05-19).
# Mapping decision: runtime composes these from landed contracts/policies.
# L2-L6 units provide concrete implementations.
# ----------------------------------------------------------------------------
@runtime_checkable
class ContentAddressedIndex(Protocol):
    """Composed at U-RT-09 from landed IS index primitives."""


@runtime_checkable
class SemanticCache(Protocol):
    """Composed at U-RT-09 from landed IS cache primitives."""


@runtime_checkable
class MCPHost(Protocol):
    """Composed at U-RT-15 wrapping `mcp` (FastMCP) host runtime."""


@runtime_checkable
class MCPClient(Protocol):
    """Composed at U-RT-15 wrapping `mcp` client runtime."""


@runtime_checkable
class HarnessMCPServer(Protocol):
    """Composed at U-RT-62 wrapping a `mcp.server.fastmcp.FastMCP` instance
    that hosts the `run_workflow` MCP tool (H_T-as-MCP-server topology per
    spec v1.12 §14.8.3 v1.12 workflow-initiation topology pin).

    Distinct from `MCPHost` (H_T-as-MCP-client surface, U-RT-15). The two
    MCP roles coexist on `HarnessContext` post-bootstrap per Q4 sibling-
    primitive ratification at the C-RT-18 v1.12 fork.

    Kept as an empty structural Protocol (NOT re-exported as the concrete
    `harness_runtime.lifecycle.mcp_server.HarnessMCPServer`) because the
    concrete references `WorkflowObject` (api.py), which would break Pydantic
    forward-ref resolution of `HarnessContext`. Consumers that need the
    concrete surface (`api.py`) narrow via `cast` at the call site.
    """


@runtime_checkable
class SandboxDispatchTable(Protocol):
    """Composed at U-RT-16 from landed AS sandbox-tier primitives."""


@runtime_checkable
class EngineSelector(Protocol):
    """Engine-class selection wired at U-RT-22 over CP's binding-time selection.

    Concretized at `harness_runtime.lifecycle.engine_selector.RuntimeEngineSelector`
    which composes U-CP-17's `select_engine_class` across all
    `(WorkloadClass, PersonaTier)` combinations for the runtime's
    `deployment_surface`, honoring per-workload `WorkloadRoutingOverride.
    engine_class_override` from the operator-supplied routing manifest.
    """

    def select(
        self,
        workload_class: WorkloadClass,
        persona_tier: PersonaTier,
    ) -> EngineClass:
        """Return the bound `EngineClass` for this workload/persona combination.

        Total: every `(WorkloadClass, PersonaTier)` combination at the runtime's
        `deployment_surface` is pre-resolved at bootstrap (U-RT-22 AC: missing
        binding raises typed error at bootstrap, not at runtime).
        """
        ...


@runtime_checkable
class RetryBreakerRegistry(Protocol):
    """Retry / breaker / idempotency runtime registry surface (U-RT-24).

    Per `Plan_Executability_Audit_v1.md` framework-pull discipline: NO
    `tenacity` / `pybreaker` / `circuitbreaker`. Concretized by
    `harness_runtime.lifecycle.retry_breaker.RuntimeRetryBreaker`. Narrowed at
    U-RT-24 to declare the registry's reference-time surface so consumer-side
    type checks (L8 LOOP_INIT orchestrator) compose against a documented API.

    `get_breaker` returns the runtime's `BreakerStateMachine` (a concrete
    dataclass at `harness_runtime.lifecycle.retry_breaker`); the Protocol
    types it as `object` to avoid a `types` → `lifecycle.retry_breaker` →
    `types` import cycle. Callers narrow via `isinstance` or by going through
    the concrete `RuntimeRetryBreaker` type.
    """

    def get_policy(self, tool_name: str) -> RetryPolicy:
        """Return the per-tool `RetryPolicy` or the registry's default policy."""
        ...

    def get_breaker(self, scope: BreakerScope, identifier: str) -> object:
        """Return the per-(scope, identifier) breaker state machine (concrete
        type at `harness_runtime.lifecycle.retry_breaker.BreakerStateMachine`)."""
        ...

    def compute_delay_seconds(self, attempt: int) -> float:
        """Full-jitter delay for the given 0-indexed retry attempt."""
        ...

    def advance_staircase(
        self,
        current: StaircaseStage,
        cause: ValidatorRetryExitClass,
        attempt: int,
    ) -> StaircaseTransition:
        """Wrap `harness_cp.validator_fail_transient_staircase.advance_staircase`."""
        ...

    def emit_breaker_transition_event(
        self,
        transition: object,
        parent_span_ref: SpanRef,
    ) -> EventEmission:
        """Emit the C-OD-07 §7.1 `breaker.tripped` event for a state transition."""
        ...

    def dedupe_decision(
        self,
        span: SpanIngestionView,
        ledger_entry: F2StateLedgerEntry | None,
    ) -> DedupOutcome:
        """C-OD-14 §14.5.1 idempotency-join dedup decision."""
        ...


@runtime_checkable
class HITLPlacementRegistry(Protocol):
    """HITL placement runtime registry surface (U-RT-25).

    Concretized by `harness_runtime.lifecycle.hitl_placement.RuntimeHITLPlacementRegistry`.
    Narrowed at U-RT-25 to declare the registry's reference-time surface so
    consumer-side type checks (L8 LOOP_INIT orchestrator) compose against a
    documented API. The registry composes the 5 CP HITL primitives —
    `hitl_response_palette`, `hitl_placement`, `hitl_timeout_degradation`,
    `hitl_as_tool_call_rewriting`, `pause_resume_protocol`.

    Method `rewrite_tool_call` takes `ProposedAction` (from
    `harness_cp.handoff_context`) and tool / server identifiers — typed here
    as `object` to keep the L0 stub free of additional cross-axis imports.
    Callers narrow at concrete call sites.
    """

    def on_timeout(
        self,
        invocation: HITLInvocation,
        persona_tier: PersonaTier,
    ) -> TimeoutDegradationKind:
        """Typed timeout-degradation event for a timed-out HITL invocation."""
        ...

    def select_variant(
        self,
        cell_synchrony_class: SynchronyClass,
    ) -> HITLSemanticVariant:
        """Select the C-CP-17 §17.2 HITL semantic variant for a cell synchrony class."""
        ...

    def rewrite_tool_call(
        self,
        tool: str,
        server: str,
        persona_tier: PersonaTier,
        proposed_action: object,
        cell_synchrony_class: SynchronyClass,
        cross_trust_boundary_state: CrossTrustBoundaryState,
        hitl_required: bool,
    ) -> RewrittenToolCall:
        """Rewrite a tool call into a HITL semantic variant per C-CP-17 §17.2."""
        ...

    def classify_resume(
        self,
        diff: tuple[object, ...],
        revalidation_succeeded: bool,
    ) -> ResumeOutcomeKind:
        """Classify a resume outcome from the material-diff set (C-CP-22 §22.1)."""
        ...


@runtime_checkable
class HandoffRegistry(Protocol):
    """Sub-agent handoff + brief runtime registry surface (U-RT-26).

    Concretized by `harness_runtime.lifecycle.handoff.RuntimeHandoffRegistry`.
    Narrowed at U-RT-26 to declare the registry's reference-time surface so
    consumer-side type checks (L8 LOOP_INIT orchestrator) compose against a
    documented API. The registry composes the 4 CP sub-agent-dispatch
    primitives — `handoff_context`, `sub_agent_brief`,
    `sub_agent_gate_level_descent`, `brief_authoring_inheritance`.

    Brief-schema enforcement (AC #2) is delegated to Pydantic v2 — `SubAgentBrief`
    is frozen + `extra="forbid"`; construction with extra or missing fields
    raises `ValidationError`.
    """

    def dispatch(
        self,
        parent_action_id: ActionID,
        parent_gate_level: CPGateLevel,
        parent_sandbox_tier: SandboxTier,
        sub_agent_brief: SubAgentBrief,
        operator_override: GateOverride | None = ...,
    ) -> SubAgentGateLevelDescent:
        """Resolve the sub-agent gate-level descent at a dispatch site."""
        ...

    def assert_descent(
        self,
        parent_gate_level: CPGateLevel,
        child_gate_level: CPGateLevel,
    ) -> None:
        """Enforce C-CP-12 §12.2 monotonic-descent (child <= parent gate level)."""
        ...

    def assert_ascent(
        self,
        parent_sandbox_tier: SandboxTier,
        child_sandbox_tier: SandboxTier,
    ) -> None:
        """Enforce C-AS-11 monotonic-ascent (child >= parent sandbox tier)."""
        ...

    def inheritance_for(self, workload_class: WorkloadClass) -> BriefAuthoringInheritance:
        """Return the C-CP-13 §13.3 brief-authoring inheritance rule."""
        ...

    def compute_brief_summary_hash(self, brief: SubAgentBrief) -> str:
        """`sha256(canonicalize_brief(brief))` per C-CP-13 §13.2."""
        ...

    def canonicalize_brief(self, brief: SubAgentBrief) -> bytes:
        """Deterministically serialize a brief for hashing (C-CP-13 §13.2)."""
        ...

    def dispatch_response_hash(self, brief: SubAgentBrief) -> str:
        """`response_hash = sha256(canonicalize(SubAgentBrief))` per C-CP-12 §12.5."""
        ...

    def compose_dispatch_audit(
        self,
        parent_action_id: ActionID,
        descent: SubAgentGateLevelDescent,
        brief_hash: str,
    ) -> CPAuditLedgerEntry:
        """Compose the C-CP-12 §12.5 sub-agent-dispatch audit-ledger entry."""
        ...


@runtime_checkable
class PerStepOverrideEvaluator(Protocol):
    """Per-step override evaluator runtime surface (U-RT-39).

    Concretized by
    `harness_runtime.lifecycle.override_evaluator.RuntimePerStepOverrideEvaluator`.
    Narrowed at U-RT-39 to declare the evaluator's reference-time surface so
    consumer-side type checks (L8 LOOP_INIT orchestrator) compose against a
    documented API. The evaluator is stateless — it composes the CP
    `resolve_step_binding` pure function (C-CP-06 §6.2). Returns an
    `object` to keep the Protocol free of a runtime → CP concrete-type
    dependency at the typing layer; callers narrow via the concrete
    `RuntimePerStepOverrideEvaluator` type when they need the
    `StepEffectiveBinding` shape.
    """

    def resolve_step_binding(
        self,
        manifest_entry: WorkflowManifestEntry,
        step_id: str,
        *,
        default_model_binding: ModelBinding,
        persona_tier: PersonaTier,
    ) -> object:
        """Resolve the effective per-step binding (delegates to CP C-CP-06 §6.2)."""
        ...


@runtime_checkable
class TopologyDispatcher(Protocol):
    """Topology-pattern dispatcher runtime surface (U-RT-40).

    Concretized by
    `harness_runtime.lifecycle.topology_dispatcher.RuntimeTopologyDispatcher`.
    Narrowed at U-RT-40 to declare the dispatcher's reference-time surface
    so consumer-side type checks (workflow-execution units U-RT-42+)
    compose against a documented API. The dispatcher is stateless — it
    composes the CP `TopologyPattern` enum + `is_admissible` predicate
    (C-CP-10 §10.1, §10.3).

    **Risk-gate clearance at U-RT-40 landing.** Tension 002 (TopologyPattern
    3-way divergence between plan/spec/ADR) was RESOLVED 2026-05-15 per
    operator decision (Set 2 — conformed to spec C-CP-10 §10.1 verbatim
    at 4 loci). CP's landed `TopologyPattern` enum carries the canonical
    6-value taxonomy. No carry-forward; U-RT-40 lands cleanly against
    the spec-conformed CP enum.
    """

    def dispatch(self, manifest_entry: WorkflowManifestEntry) -> TopologyPattern:
        """Return the bound `TopologyPattern` for a workflow manifest entry."""
        ...

    def is_admissible(self, pattern: TopologyPattern, workload: WorkloadClass) -> bool:
        """Cross-pattern admissibility per C-CP-10 §10.3 (delegates to CP)."""
        ...

    def is_topology_permitted(self, pattern: TopologyPattern, workload: WorkloadClass) -> bool:
        """Primary OR cross-pattern admissibility (C-CP-11 §11.1 ∪ C-CP-10 §10.3).

        The correct gate for sub-agent dispatch composer step 4 — see the
        U-RT-59 topology-admissibility Class 1 fork resolution at
        ``.harness/class_1_tension_u_rt_59_topology_admissibility_predicate.md``.
        Delegates to ``harness_cp.per_workload_class_topology
        .is_topology_permitted_for_workload``.
        """
        ...


@runtime_checkable
class LLMDispatcher(Protocol):
    """LLM-dispatch composer runtime surface (U-RT-52, C-RT-15).

    Concretized by
    `harness_runtime.lifecycle.llm_dispatch.RuntimeLLMDispatcher`.
    Satisfies the CP-side `harness_cp.workflow_driver.StepDispatcher`
    Protocol (declared at `workflow_driver.py:151`); narrowed at
    U-RT-52 to declare the dispatcher's reference-time surface so
    consumer-side type checks compose against a documented API.
    The dispatcher is stateless — each `dispatch` invocation is
    driven by its arguments + frozen provider/tracer substrate.

    Per C-RT-15 §Specification content, the composer dispatches the
    per-provider SDK call (anthropic / openai / ollama) under a
    GenAI-semconv 1.41.0 span. ``binding.model_binding.provider``
    selects the per-provider branch.
    """

    async def dispatch(
        self,
        binding: StepEffectiveBinding,
        step: WorkflowStep,
        *,
        step_context: StepExecutionContext,
    ) -> Mapping[str, Any]:
        """Dispatch the step under the effective binding; return step output."""
        ...


@runtime_checkable
class CostAttributionChain(Protocol):
    """Cost-attribution 5-step chain runtime surface (U-RT-31).

    Concretized by `harness_runtime.lifecycle.cost_attribution.RuntimeCostAttributionChain`.
    Narrowed at U-RT-31 to declare the chain's reference-time surface so
    consumer-side type checks (L8 LOOP_INIT orchestrator + U-RT-32 audit
    writer) compose against a documented API. The chain composes the 5 OD
    cost-attribution primitives — `cost_formula`, `cost_attribution_sandbox_fanout`,
    `idempotency_join_dedup`, `cost_attribution_dashboard_binding`,
    `operator_burden_eval_primitives`.

    Method signatures use `object` for OD-typed parameters where importing
    the concrete OD types would add cross-axis import surface; consumers
    narrow at concrete call sites or go through the concrete
    `RuntimeCostAttributionChain` directly.
    """

    def compute_per_attempt_cost(
        self,
        inputs: object,
        rates: object,
    ) -> float:
        """Step 1 — C-OD-14 §14.1 per-span cost formula."""
        ...

    def compose_total_cost(
        self,
        span_cost: float,
        span_duration_ms: int,
        sandbox_overhead: object,
    ) -> object:
        """Step 2 — C-OD-14 §14.2 sandbox-overhead composition."""
        ...

    def attach_idempotency_key(
        self,
        span: object,
        parent_idempotency_key: str,
        cost_record: object,
    ) -> object:
        """Step 3 — C-OD-14 §14.4 idempotency-key join."""
        ...

    def rollup_fanout(
        self,
        parent_span_ref: object,
        sibling_costs: list[object],
        pattern: object,
    ) -> object:
        """Step 4 — C-OD-14 §14.3 fan-out aggregation at close."""
        ...

    def dedupe_on_replay(
        self,
        span: object,
        ledger_entry: object,
    ) -> object:
        """Step 5 — C-OD-14 §14.5.1 replay-aware dedup decision."""
        ...


# ----------------------------------------------------------------------------
# Protocol — provider SDK structural shape (C-RT-05).
# Concrete adapters (Anthropic / OpenAI / Ollama) wrap each async SDK client
# behind this Protocol uniformly. Concretized at U-RT-17.
#
# Per spec §5 (C-RT-05 v1.1 lines 326-344): the Protocol is intentionally
# minimal — only the lifecycle obligation the runtime owns. The capability-
# aware abstraction layer (CP `provider_capabilities`) is what dispatches to
# provider-specific completion methods, not this Protocol (per advisor +
# spec docstring at line 335-339).
# ----------------------------------------------------------------------------
@runtime_checkable
class ProviderClient(Protocol):
    """Structural protocol every async provider adapter satisfies (C-RT-05 v1.1).

    Concrete adapters at `harness_runtime.lifecycle.providers`:
    `AnthropicAdapter` (U-RT-17), `OpenAIAdapter` (U-RT-18), `OllamaAdapter`
    (U-RT-19). Each adapter wraps its SDK's async client so the runtime can
    `aclose()` all three uniformly at C-RT-10 reverse-shutdown.
    """

    async def aclose(self) -> None:
        """Close the underlying SDK client + connections. Idempotent.

        Per C-RT-05 §5 + C-RT-10 reverse-shutdown: called at runtime shutdown
        for every entry in `HarnessContext.providers`. Adapters MUST tolerate
        repeated invocation without raising (idempotent post-condition).
        """
        ...


class ExternalCLIProviderKind(StrEnum):
    """Supported subscription-backed local CLI provider adapters."""

    CLAUDE_CODE = "claude-code"


class ExternalCLIProviderConfig(BaseModel):
    """Config for an already-authenticated local CLI-backed provider.

    Carries executable metadata only. OAuth/session material stays exclusively
    inside the official local CLI's own auth/session store.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    """Provider key used by routing, for example ``"claude_code"``."""

    kind: ExternalCLIProviderKind
    """Adapter implementation kind. R-CLI-1 v1 supports ``"claude-code"``."""

    command: str = "claude"
    """Executable name/path passed as argv[0] to create_subprocess_exec."""

    timeout_seconds: float = 120.0
    """Per-process timeout for auth checks and inference calls."""

    auth_check: bool = True
    """If true, construction probes official CLI auth status before routing."""

    optional: bool = False
    """If true, construction failure degrades like the built-in optional providers."""

    @field_validator("provider", "command")
    @classmethod
    def _non_empty_string(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must be a non-empty string")
        return stripped

    @field_validator("timeout_seconds")
    @classmethod
    def _positive_timeout(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("timeout_seconds must be > 0")
        return value


# ----------------------------------------------------------------------------
# `RuntimeConfig` — C-RT-03 v1.1 schema.
# ----------------------------------------------------------------------------
class RuntimeConfig(BaseModel):
    """Input configuration to the runtime; frozen post-construction.

    Field order and type discipline are normative per C-RT-03. Path-existence,
    keyring-allowlist, and precedence-resolution validators are deferred to
    U-RT-04 (config precedence resolver) per the spec's "Deferred to
    implementation discretion" clause.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    deployment_surface: DeploymentSurface
    """Local / hybrid / cloud — drives OTel resource attrs + collector placement."""

    repository_root: Path
    """Absolute path; must exist (validator at U-RT-04). Basis for `.harness/`."""

    path_bindings: PathBindingConfig = Field(default_factory=PathBindingConfig)
    """Inputs to `PathResolver(binding)`. Enriched at U-RT-05.

    Default-factory'd so operators can omit the sub-table from `harness.toml`
    when they want the empty-config defaults. Per
    `[[finding-runtime-config-loader-unreachable-sub-configs]]` resolution
    fix (A): required sub-configs without defaults forced operators to author
    every sub-table, which combined with the plaintext-secret detector
    false-match on `provider_secrets` (see config_source.py fix (B)) made the
    documented file-loader pathway unreachable from any source."""

    provider_secrets: ProviderSecretsConfig = Field(default_factory=ProviderSecretsConfig)
    """Keyring allowlist *keys* only — no secret values. Enriched at U-RT-06.

    Default-factory'd per finding-fix (A) above. Carries ALLOWLIST KEYS only
    (per the docstring on `ProviderSecretsConfig`); actual secret values come
    from the OS keyring at request time per ADR-F5."""

    otel: OTelConfig
    """OTLP endpoint, sampler mode, additional resource attrs. Enriched at U-RT-07.

    Required (no default) — `otlp_endpoint` is genuinely operator-specific
    and cannot reasonably default. Operators MUST provide either
    `[runtime.otel] otlp_endpoint = "..."` in their config file OR pass via
    CLI override."""

    collector: CollectorConfig = Field(default_factory=CollectorConfig)
    """Ring buffer size, sqlite rotation thresholds, placement-matrix. U-RT-08.

    Default-factory'd per finding-fix (A) above. `CollectorConfig()` provides
    sensible defaults for ring-buffer size + rotation thresholds + placement."""

    default_topology: TopologyPattern
    """TopologyPattern dispatched when no per-workflow override is set."""

    mcp_clients: list[MCPClientConfig] = []
    """MCP client connection configs; empty list permitted."""

    ollama_host: str | None = None
    """Ollama daemon host URL (per spec §5 line 354 `AsyncClient(host=...)`).

    `None` → `ollama.AsyncClient()` falls back to its built-in default
    (`http://localhost:11434`). Top-level placement (vs. nested under
    `ProviderSecretsConfig`) per spec §5 deferred-discretion note line 373:
    Ollama is local-tier and credential-less, so this is a *behavior* knob,
    not a key-allowlist concern. U-RT-17 amendment per advisor.
    """

    ollama_optional: bool = False
    """If True, Ollama unreachability at stage 3a → `RT-FAIL-PROVIDER-DEGRADED`
    (typed warning; stage continues with 2-provider context). Default False:
    Ollama unreachability is a hard stage 3a failure per the multi-LLM
    commitment (ADR-F1 v1.2). U-RT-19 wires the degraded branch; field is
    declared here at U-RT-17 to keep schema additions in one commit."""

    anthropic_optional: bool = False
    """If True, Anthropic construction failure at stage 3a (keyring miss OR
    network unreachable) → `RT-FAIL-PROVIDER-DEGRADED` (typed warning; stage
    continues without `"anthropic"` in providers). Default False: Anthropic
    failure is a hard stage 3a failure per the multi-LLM commitment
    (ADR-F1 v1.2). Auth errors (`ProviderAuthError` — 401/403) ALWAYS surface
    regardless of `anthropic_optional` because they indicate operator intent
    + misconfig (keyring entry present but invalid).

    Added per `.harness/class_1_fork_provider_construction_allowlist_semantic.md`
    operator-ratified 2026-05-28 (E-prod-3). Symmetric extension of the
    `ollama_optional` precedent at line 1938 deferred-to-discretion clause.
    Unblocks daemon-mode subprocess e2e for operators without all keyring
    entries configured."""

    openai_optional: bool = False
    """If True, OpenAI construction failure at stage 3a (keyring miss OR
    network unreachable) → `RT-FAIL-PROVIDER-DEGRADED` (typed warning; stage
    continues without `"openai"` in providers). Default False: OpenAI
    failure is a hard stage 3a failure per the multi-LLM commitment
    (ADR-F1 v1.2). Auth errors (`ProviderAuthError` — 401/403) ALWAYS surface
    regardless of `openai_optional`.

    Added per `.harness/class_1_fork_provider_construction_allowlist_semantic.md`
    operator-ratified 2026-05-28 (E-prod-3). Symmetric extension of the
    `ollama_optional` precedent."""

    enabled_provider_names: tuple[str, ...] = ("anthropic", "openai", "ollama")
    """Provider keys stage 3a should construct.

    Defaults to the existing three built-in providers for backwards-compatible
    bootstrap behavior. Operators can opt into an external CLI provider by
    naming its `ExternalCLIProviderConfig.provider` here.
    """

    external_cli_providers: tuple[ExternalCLIProviderConfig, ...] = ()
    """Local CLI-backed provider configs. Contains no secret/token fields."""

    inter_step_data_flow: bool = False
    """B-INTERSTEP (R-FS-1 standalone arc; runtime spec §14.21 C-RT-34, new at
    v1.59) — opt-in to the inter-step output channel (the shared run-context a
    dispatcher reads). When `True`, stage 5 LOOP_INIT constructs + binds a fresh
    `InterStepOutputChannel` on `ctx.inter_step_output_channel`, the workflow
    driver records each completed step's output, and the LLM dispatcher injects
    the immediately-prior step's output into the dispatched payload (making
    EVALUATOR_OPTIMIZER's draft→evaluate / feedback→regenerate data flow real).
    Default `False` → `ctx.inter_step_output_channel is None` → byte-identical to
    pre-v1.59 (no recording, no injection). The injection *changes* the dispatched
    payload, so it MUST be opt-in (unlike `cost_record_accumulator`, which is
    always-on additive observability)."""

    engine_output_replay: bool = False
    """B-ENGINE-OUTPUT-REPLAY (R-FS-1 standalone arc; runtime spec C-RT-32, new) —
    opt-in to the durable output-carrying event-history store. When `True`, stage 5
    LOOP_INIT constructs + binds a fresh `EngineOutputStore` on
    `ctx.engine_output_store` (co-located under the resolved STATE_LEDGER dir); the
    workflow driver durably records each completed step's output BEFORE the F2
    ledger-append (RESERVE-before-COMMIT), and on an EVENT_SOURCED_REPLAY resume the
    driver rehydrates the inter-step output channel from the stored prefix outputs —
    materializing the C-CP-08 §8.1 "activity outputs cached and replayed" clause
    (degenerate today: on a skip-prefix resume the prefix is NOT re-dispatched, so a
    downstream consumer reads an empty channel — fresh-run ≠ resumed-run). The
    distinguishing replay effect is observable ONLY when composed with
    `inter_step_data_flow=True` (the consumer channel) + an EVENT_SOURCED_REPLAY
    workflow; default `False` → `ctx.engine_output_store is None` → no recording, no
    rehydration (byte-identical).

    The store binding is engine-class-AGNOSTIC (gated only on this flag): the same
    durable per-step output store ALSO backs the resume final_state reconstruction
    (B-CHILD/TOP-LEVEL/SAVE-POINT/RECONCILER-CRASH-RESUME-FINAL-STATE-RECONSTRUCT) for
    ALL FOUR durable resumable classes EVENT_SOURCED_REPLAY / WAL_SEGMENT /
    SAVE_POINT_CHECKPOINT / RECONCILER_LOOP — the CP driver seeds the committed prefix
    back into `accumulated` on resume so a resumed run reports its COMPLETE final_state,
    not a suffix-only one. Which classes record + reconstruct is a CP-side gate
    (`_FINAL_STATE_RECONSTRUCT_ENGINE_CLASSES`), so RECONCILER reconstruction needs no
    runtime change — the reconciler's U-RT-123 substrate persists a CONVERGENCE DIGEST
    (StateSummary) for its CAS-lease, a different datum than the per-step output map this
    store carries, so they are not competing output authorities."""

    effect_fencing: bool = False
    """B-EFFECT-FENCE (R-FS-1 standalone arc; runtime spec §14.22 C-RT-31, new at
    v1.60) — opt-in to at-most-once EXECUTION of non-idempotent tool-step effects
    across durable-engine retries/resumes. When `True`, the tool-dispatcher factory
    constructs a durable `RuntimeEffectFence` (under `repository_root/.harness/
    effect-fence`) and the `RuntimeToolDispatcher` `try_reserve`s the per-(run,
    step, tool) `idempotency_key` BEFORE `call_tool`: the first dispatch wins (the
    effect fires) and `capture_output`s the validated response post-fire/pre-commit.
    A re-dispatch of the same effect (a crash-then-resume re-run of an
    effected-but-uncommitted step, or an in-process retry) loses the reserve and
    SPLITS on the captured output (B-EFFECT-FENCE-HITL-ROUTE, v1.72): output present
    → suppress-and-continue (return the captured result, never re-fire); output
    absent/corrupt → `EffectFenceAmbiguousUncommittedError`, which the workflow
    driver routes to a §26.2 `WorkflowPauseReason.EFFECT_FENCE_AMBIGUOUS` PAUSE when
    a `PauseResumeProtocol` is bound, else FAILED (no auto-re-fire either way).
    Default `False` → no fence constructed → byte-identical (no reserve, no claim
    files). Meaningful only under a durable engine class (where a resume
    re-dispatches uncommitted steps); auto-activation under durable engines is the
    `B-EFFECT-FENCE-DURABLE-AUTO` follow-on. Cf. the reconciler (U-RT-123):
    single-host, fail-closed residual, COMMIT = the existing per-step ledger
    entry."""

    routing_activation: bool = False
    """B-L2-EMBEDDING-ACTIVATION (R-FS-1 standalone arc; C-CP-02 §2.2 — the
    routing-activation gate) — opt-in to §2.2-faithful cheapest-deterministic-first
    layered routing. When `True`, the LLM dispatcher's DECLARATIVE layer DECLINES
    (falls through to the EMBEDDING classifier, then LLM_AS_ROUTER) when the routing
    manifest does NOT bind the request's tuple — i.e. `agent_role ∉
    per_role_bindings` AND `workload_class ∉ per_workload_overrides` (the §2.2 "When
    it resolves: the manifest binds the (agent_role, workflow_class, step) tuple"
    contract). Default `False` → DECLARATIVE always resolves the effective binding
    (the #213 MVP behavior-preserving echo) → byte-identical, ZERO blast radius on
    existing deployments. This is the HIGHEST-blast-radius opt-in (it changes WHICH
    model serves a workload), so default-off is load-bearing; flag-on additionally
    requires the operator to wire an embedding classifier (+ install the optional
    `[embedding]` extra) and a partial manifest, and the LIVE multi-provider exercise
    needs a second configured provider (a deployment gate, not a build gate)."""

    tenant_id: str | None = None
    """Multi-tenant separation key per OD audit-ledger. `None` = single-tenant."""

    persona_tier: PersonaTier = PersonaTier.SOLO_DEVELOPER
    """Per-deployment persona classification per OD spec §C-OD-10 §10.3 + §C-OD-13 §13.1.

    Drives (a) `HarnessCompositeSampler` base_rate at
    `materialize_tracer_provider_stage` per §10.3 8-row table at
    `harness_od.base_rate_envelope.BASE_RATE_DEFAULTS`; (b)
    `RedactionSpanProcessor` per-persona override toggle at
    `materialize_span_processor_stage` per §13.1 toggleability gradient at
    `harness_od.redaction_gradient.PER_PERSONA_TIER_REDACTION`.

    Default `SOLO_DEVELOPER` preserves backward-compat at all existing test
    fixtures + the MVP `base_rate=1.0` defense-in-depth pre-arc behavior.
    Operators MUST opt-in to TEAM_BINDING / MULTI_TENANT_COMPLIANCE explicitly
    via env (`HARNESS_PERSONA_TIER`) / harness.toml (`persona_tier`) / CLI
    flag per U-RT-103 3-source resolution.

    Added per `.harness/class_1_fork_od_3_od_4_retire_ready_persona_tier_plumbing.md`
    operator-ratified 2026-05-28 (Q1=A + Q2=A + Q3=a + Q4=i + Q5=α; single
    bundled binding-lift arc per `tenant_id` v1.22 precedent).

    Distinct from CP-axis `StepEffectiveBinding.persona_tier` (CP spec v1.17
    §6.5) which is per-step / per-workflow for gate-level / engine-class /
    HITL-matrix purposes. The two surfaces co-exist by design: CP-axis
    carries per-step persona_tier for per-workflow decisions; OD-axis reads
    per-deployment persona_tier for sampling + redaction discipline. Per fork
    doc §3 + OD spec v1.26 §13.1 canonical-reading amendment."""

    drain_timeout_seconds: float = 60.0
    """Bounded-wait timeout on workflow-execution drain (U-RT-44 AC #2 typed-
    timeout branch; C-RT-11 + C-RT-14 RT-FAIL-DRAIN-TIMEOUT).

    `harness_runtime.api.run()` wraps the CP workflow driver call in
    `asyncio.wait_for(...)` with this bound. If the in-flight step does not
    complete (and the driver does not reach a boundary) within this window,
    `run()` surfaces `FailureCause(runtime_fail_class='RT-FAIL-DRAIN-TIMEOUT')`
    on a DRAINED RunResult and proceeds to shutdown per `Spec_Harness_
    Runtime_v1.md` §11 invariant ("exceeding the bound forces shutdown to
    proceed regardless; in-flight step may be in inconsistent state"). The
    spawned thread is not cancelled — Python threads cannot be cancelled
    cooperatively without driver support; the inconsistent-state surface is
    documented at C-RT-11.

    Default 60.0 seconds is generous for the v1.4 PURE_PATTERN_NO_ENGINE
    scope; production deployments override per workflow-class budget.
    Lane 6 (2026-05-20) addition; spec §3 (C-RT-03) "Deferred to
    implementation discretion" clause covers this configuration."""

    step_dispatch_timeout_seconds: float = 30.0
    """Per-step worker-thread blocking bound (C-RT-03 v1.31; RT-FAIL-STEP-
    DISPATCH-TIMEOUT).

    Threaded into all 3 stage-5 `materialize_sync_dispatcher_facade(...)`
    callsites as the facade's `result_timeout_seconds` constructor parameter
    (INFERENCE_STEP / SUB_AGENT_DISPATCH / TOOL_STEP). A single step's hang
    surfaces `RT-FAIL-STEP-DISPATCH-TIMEOUT` BEFORE the whole-workflow
    `drain_timeout_seconds` bound fires.

    Independent of `drain_timeout_seconds`: the drain bound serves as
    backstop ensuring shutdown progress when the per-step bound is exceeded
    or other progress conditions fail. Default 30.0 (~2× headroom against
    drain default 60.0). Worker threads are not cancelled — Python threads
    cannot be cancelled cooperatively; result is discarded on per-step
    timeout.

    Resolves the v1.7..v1.30 per-step ↔ whole-workflow timeout-budget
    conflation documented at `class_3_tension_u_rt_59_spec_prose_drift.md`
    §7 (filed 2026-05-20; CLOSED 2026-05-28 at v1.31 / v2.27). Added at
    v1.31 per `.harness/class_1_fork_step_dispatch_timeout_seconds_field_
    extension.md` Reading A ratification (Q1=A, Q2=30.0s)."""

    hitl_auto_approve_policy: HITLAutoApprovePolicy = Field(
        default_factory=lambda: HITLAutoApprovePolicy(),
    )
    """Operator-supply surface for the CP §19.5 operator-policy override of a
    `max()` floor (C-RT-03 v1.49 §3 field + §3.8 sub-model).

    Reading C (tunable floor; design §3.3): a two-bool **named-cell** override of
    the two §19.1-annotated floor cells only — `persona_tier_floor[SOLO_DEVELOPER]
    → AUTO` (§19.1 line 1639) + `blast_radius_floor[LOCAL_MUTATION] → AUTO` (§19.1
    line 1634) — applied **in-`max()`** at §14.8.2 step-4c by `RuntimeHITLGateComposer`
    (read at stage-5 construction, held as composer instance state — no C-RT-04
    field, per F-B3-1 §3.1). Solo-scoped: the composer applies the knobs only when
    `binding.persona_tier == SOLO_DEVELOPER`, so multi-tenant-compliance is
    structurally foreclosed and team-binding override is a registered follow-on
    (F-B3-1 §6). Default `HITLAutoApprovePolicy()` = `{solo_persona_floor_auto: True,
    solo_local_mutation_floor_auto: False}` → READ_ONLY auto-ON / LOCAL_MUTATION
    opt-in / EXTERNAL_* hard-stop at solo-developer. Full sub-model + arithmetic +
    ACs at §3.8. Added at v1.49 per `.harness/class_1_fork_b3_1_hitl_auto_approve_
    policy_field.md` (F-B3-1; R-FS-1 B3-spec-1)."""

    pidfile_path: Path | None = None
    """Override for the pidfile location (spec §13 deferred-to-discretion).

    `None` → `repository_root / ".harness/runtime.pid"` per
    `harness_runtime.admin.pidfile.default_pidfile_path`. The pidfile is
    written at stage 7 INGRESS_ACCEPT and removed at the end of
    `shutdown()` (both U-RT-48). Read by the `harness-shutdown` CLI to
    locate the running harness.
    """

    routing_manifest: RoutingManifest = Field(
        default_factory=lambda: RoutingManifest(
            manifest_version=1,
            per_role_bindings={},
            per_workload_overrides={},
            fallback_chains=(),
            retry_policies={},
        ),
    )
    """Operator-supplied routing manifest (CP v2.10 R-2 read / W-2 write schemas).

    Enriched at U-RT-21 (L5 stage 3b CP_ROUTING). Default is an empty manifest
    (`manifest_version=1`, no role bindings, no workload overrides, no fallback
    chains, no retry policies) — sufficient to drive the bootstrap path through
    stage 3b in test scenarios that don't exercise routing dispatch. Operators
    supply a populated manifest via kwarg at runtime construction; the manifest
    is persisted to `PathClass.ROUTING_MANIFEST` at stage 3b per C-CP-01 §1.3.
    """

    prompt_manifest: PromptManifest = Field(
        default_factory=lambda: PromptManifest(
            manifest_version=1,
            active_prompt_version=PromptVersion(version_sha=""),
        ),
    )
    """Operator-supplied prompts-management carrier (IS spec v1.5 §5.2 third
    procedural-tier hash component).

    The operator-supply surface (mirroring `routing_manifest`): a populated
    manifest supplied via kwarg at runtime construction is copied to
    `HarnessContext.prompt_manifest` at bootstrap stage 0 PREAMBLE and read by
    `resolve_procedural_tier_snapshot` as the `active_prompt_version` hash
    component. Default is an empty manifest (`active_prompt_version.version_sha=""`
    → no active prompt), so operators that do not version prompts carry zero
    config burden. The fuller prompts-management surface (multi-prompt
    versioning + selection) is a separate forward arc per the §5.2 fork DP-4.
    """

    prompt_selection_manifest: PromptSelectionManifest | None = None
    """Operator-supplied CP prompt-selection manifest (R-PM-1 cascade PR #3; CP
    spec v1.31 §29).

    Resolves *which* authored prompt version (by `version_sha`) is active for a
    `(role, workload)`, mirroring `routing_manifest`'s per-role/workload binding
    shape. `None` (the default) → no selection → the bootstrap falls through to
    `prompt_manifest.active_prompt_version` (the #496/PR-#1 inline active prompt;
    zero config burden, the local-first default). When supplied, bootstrap stage
    5 reconciles `prompt_manifest.active_prompt_version` to the selected store
    member (per-workload selection keys on the REAL run `workload_class`;
    per-role on the MVP-default role until real per-role dispatch) via
    `reconcile_active_prompt_via_selection`, so BOTH the injected system prompt
    and the C-IS-05 §5.2 procedural-tier hash read the selected version. A
    binding to an unauthored `version_sha` is fail-loud
    (`RT-FAIL-PROMPT-SELECTION-UNAUTHORED`).
    """

    approved_prompt_version_shas: frozenset[str] = frozenset()
    """Operator-attested approved prompt `version_sha`s for binding-tier prompt
    governance (R-PM-1 cascade PR #4; OD spec C-OD-34).

    At a binding persona tier (team-binding / multi-tenant-compliance, where
    `resolve_prompt_governance(persona_tier).approval_required` is `True`) a
    *selection-driven* active prompt version is a governed artifact: its
    `version_sha` MUST appear in this set, else bootstrap fails loud
    (`RT-FAIL-PROMPT-VERSION-UNAPPROVED`). At the solo-developer tier the gate is
    inert (local-first, no approval burden — the default `frozenset()` carries zero
    burden, so all existing fixtures are unaffected). The gate governs only the
    versions the CP selection layer *drives* (a supplied `prompt_selection_manifest`
    resolving a `version_sha` for the run's `(role, workload)`); an inline-only
    deployment (no selection manifest) is unaffected. Enforced at bootstrap stage 0
    by `enforce_prompt_version_approval` (lifecycle/prompt_selection.py).
    """

    trust_policy: TrustPolicy | None = None
    """Operator-supplied per-server trust policy (CP spec v1.11 §27.2 carrier).

    Added at U-RT-71 per `Spec_Harness_Runtime_v1.md` v1.16 §3 C-RT-02
    field-table extension. Optional — when `None` the stage-5 factory
    (`materialize_runtime_tool_dispatcher_stage`, U-RT-75) constructs the
    `PerServerTrustEvaluator` with a runtime-supplied conservative default;
    operators supply a populated `TrustPolicy` via kwarg at runtime
    construction to override the default trust posture.
    """

    sandbox_decision_policy: SandboxDecisionPolicy | None = None
    """Operator-supplied sandbox decision policy (harness-core empty-marker
    carrier per U-CORE-02; cite re-pointed at runtime spec v1.16 / runtime
    plan v2.13 absorbing Q1=C-i Class 1 fork resolution 2026-05-22 at
    `.harness/class_1_fork_sandbox_decision_policy_phantom_cite.md`).

    Added at U-RT-71 per `Spec_Harness_Runtime_v1.md` v1.16 §3 C-RT-02
    field-table extension. Optional — when `None` the stage-5 factory
    (`materialize_runtime_tool_dispatcher_stage`, U-RT-75) supplies
    `SandboxDecisionPolicy.default()` (the empty-marker instance). The
    carrier is empty-marker shape at v1.16 per X-AL-3 + carrier-shape
    decision; future operator-driven extension surfaces via spec extension
    + planner revision pass.
    """

    memory_tool_backend_config: MemoryToolBackendConfig | None = None
    """Operator-supplied Memory tool storage-backend selection override.

    Added at U-RT-79 per `Spec_Harness_Runtime_v1.md` v1.17 §3 C-RT-02
    field-table extension (Class 1 fork H_T-CP-16+17 §16 ratified Memory-
    only arc absorption at `.harness/class_1_fork_h_t_cp_16_17_executable_
    consumer_absence.md`; operator-ratified 2026-05-23).

    Optional. `None` defers backend resolution to the deployment-surface-
    keyed graceful-degradation resolver at
    `harness_as.anthropic_graceful_degradation.memory_tool_storage_backend
    (config.deployment_surface)`. Non-`None` overrides the resolver for
    explicit backend pinning (e.g., S3 at LOCAL_DEVELOPMENT for test-fixture
    purposes; encrypted-filesystem at MANAGED_CLOUD for additional
    discipline). Ingested at stage 5 by `materialize_memory_tool_registry_
    stage` factory (U-RT-80) per §14.12.3.
    """

    validator_framework_config: ValidatorFrameworkConfig | None = None
    """Operator-supplied validator framework opt-in marker.

    Added at U-RT-83 per `Spec_Harness_Runtime_v1.md` v1.18 §3 C-RT-02
    field-table extension (Class 1 fork validator-composer arc stage-4
    absence Reading A absorption at `.harness/class_1_fork_validator_
    composer_arc_stage_4_absence.md`; operator-ratified 2026-05-24).

    Empty-marker shape at v1.18 Reading A scope per spec §14.13.1. `None`
    (the default) → operator opt-out → stage-4 factory returns `None` →
    `ctx.validator_framework is None` → the `workflow_driver.py:668`
    post-dispatch hook branch evaluates False (production-default state
    preserved). Non-`None` → operator opt-in → stage-4 factory constructs
    a `ConcreteValidatorFramework` instance bound to `ctx.validator_
    framework`; the driver hook True-arm fires per C-CP-25 §25.3.3.4.

    Internal operator-supply shape (validator catalog, per-validator config,
    discovery mechanism) deferred to implementation discretion per
    §14.13.7. Ingested at stage 4 OD bucket by
    `materialize_validator_framework_stage` factory (U-RT-84) per §14.13.3.
    """

    pause_resume_protocol_config: PauseResumeProtocolConfig | None = None
    """Operator-supplied pause/resume protocol opt-in marker.

    Added at U-RT-87 per `Spec_Harness_Runtime_v1.md` v1.21 §3 C-RT-02
    field-table extension (CP composer authoring arc — operator-ratified
    narrow-scope AskUserQuestion 2026-05-24).

    Empty-marker shape at v1.21 Reading A scope per spec §14.14.1. `None`
    (the default) → operator opt-out → stage-5 factory returns `None` →
    `ctx.pause_resume_protocol is None` → workflow_driver per-step pre-entry
    pause-trigger detection branch sibling to `drained_flag.is_set()` evaluates
    False (production-default state preserved). Non-`None` → operator opt-in
    → stage-5 factory constructs a `PauseResumeProtocol` instance bound to
    `ctx.pause_resume_protocol`; driver per-step pre-entry True-arm fires
    `ctx.pause_resume_protocol.capture_pause_snapshot(...)` on
    `ctx.pause_requested_flag.is_set()` + returns `RunStatus.PAUSED` per
    C-RT-24 §14.14.3.

    Internal operator-supply shape (snapshot-storage substrate, pause-trigger
    detection mechanism, resume-API-surface) deferred to implementation
    discretion per §14.14.7. Ingested at stage 5 LOOP_INIT by
    `materialize_pause_resume_protocol_stage` factory (U-RT-88) per §14.14.3.
    """

    webhook_delivery_composer_config: WebhookDeliveryComposerConfig | None = None
    """Operator-supplied webhook-delivery composer opt-in marker.

    Added at U-RT-96 per `Spec_Harness_Runtime_v1.md` v1.26 §3 C-RT-02
    field-table extension (Reading A path 1 absorption of fork
    `class_1_fork_u_rt_94_webhook_delivery_composer_binding_chain_absence.md`;
    operator-ratified 2026-05-24).

    Empty-marker shape at v1.26 authoring scope per spec §14.16.1. `None`
    (the default) → operator opt-out → stage-5 factory returns `None` →
    `ctx.webhook_delivery_composer is None` → §14.8.8.1 step 0 OR-form
    precondition AND-arm evaluates False (durable-async branch falls through
    to sync-blocking; production-default state preserved). Non-`None` →
    operator opt-in → stage-5 factory constructs a `WebhookDeliveryComposer`
    instance bound to `ctx.webhook_delivery_composer`; durable-async branch
    at §14.8.8.1 step 3 invokes `ctx.webhook_delivery_composer.deliver_webhook(...)`.

    Internal operator-supply shape (per-endpoint URL, per-retry-policy,
    per-idempotency-key-store substrate, outbound HTTP timeout, TLS/auth)
    deferred to implementation discretion at C-RT-26 landing arc per FM-2
    (spec §14.16.1 + change-note adjacent defect (i)). Ingested at stage 5
    LOOP_INIT by `materialize_webhook_delivery_composer_stage` factory
    (U-RT-97) per §14.16.3.
    """

    skill_activation_hook_config: SkillActivationHookConfig | None = None
    """Operator-supplied Skill activation hook policy + opt-in marker.

    Added at U-RT-99 per runtime spec v1.32 §3 C-RT-02 field-table extension
    (Reading B operator-opt-in MVP shape per
    `.harness/class_1_fork_as_8d_skill_activation_surface_absence.md`;
    operator-ratified 2026-05-28).

    `None` (the default) → operator opt-out → stage-5 factory returns `None`
    → `ctx.skill_activation_emitter is None` → all 3 hook binding sites
    (per-LLM-dispatch / per-workflow-init / operator-explicit
    `ctx.activate_skill(...)`) evaluate False arm (no spans emitted;
    production-default state preserved). Non-`None` → operator opt-in →
    stage-5 factory constructs a `SkillActivationSpanEmitter` instance
    bound to `ctx.skill_activation_emitter`; hook binding sites query the
    bound `SkillActivationHook` Protocol implementation (sourced from
    `skill_activation_hook_config.hook`) at firing time + emit one
    `skill.activation` span per activated skill.

    Ingested at stage 5 LOOP_INIT by
    `materialize_skill_activation_emitter_stage` factory (U-RT-100) per
    §14.17.3.
    """

    managed_agents_config: ManagedAgentsConfig | None = None
    """Operator opt-in marker + supplied client for managed-agents dispatch.

    Added at C-RT-28 per runtime spec v1.55 §14.20.1 (R-FS-1 arc M;
    operator-ratified 2026-06-17, Option B). `None` (the default) → operator
    opt-out → the stage-5 `materialize_managed_agents_dispatcher_stage` factory
    returns `None` → `StepKind.MANAGED_AGENTS` is NOT bound in the
    `StepKindDispatcherRegistry` → a managed-agents step fails closed with
    `StepKindDispatcherNotBoundError` → `RT-FAIL-STEP-KIND-DISPATCHER-NOT-BOUND`
    (no silent under-execution). Non-`None` AND
    `deployment_surface == DeploymentSurface.MANAGED_CLOUD` → opt-in → the
    factory constructs a `ManagedAgentsStepDispatcher` (over
    `managed_agents_config.client`) bound to `StepKind.MANAGED_AGENTS`. On any
    non-managed-cloud surface the opt-in is silently not bound (the H_T-AS-8f
    local-development exclusion remains TRUE). No credentials embedded — the
    SDK client is operator-constructed.
    """


class CostRecordAccumulator:
    """R-FS-1 arc CA — run-scoped, by-reference cost-record sink.

    A plain (NON-Pydantic) holder so `HarnessContext` stores it **by reference**
    under `arbitrary_types_allowed` — exactly like `asyncio.Event` /
    `tracer_provider`. A typed `list[SpanCostRecord]` field would be **copied** by
    Pydantic v2 at `freeze()` (Pydantic validates/rebuilds known containers),
    silently disconnecting the per-dispatch wrappers (which captured the mutable
    builder's list pre-freeze) from `_build_run_result` (which reads the frozen
    ctx) → `cost_attribution` always `()`. Arbitrary types are stored opaquely, so
    the holder — and its `records` list — survive `freeze()` as the same object.

    The per-dispatch cost wrappers receive `accumulator.records` (a stable list
    created once at construction) and `.append` to it; `_build_run_result` reads
    `ctx.cost_record_accumulator.records` (the same list) and rolls it up along
    `RollupAxis.PER_PROVIDER_AND_MODEL` (runtime spec v1.53 §9 C-RT-09).
    """

    __slots__ = ("records",)

    def __init__(self) -> None:
        self.records: list[SpanCostRecord] = []

    def append(self, record: SpanCostRecord) -> None:
        self.records.append(record)


# B-INTERSTEP-PERRUN-ISOLATION (B-INTERSTEP fork §3/§5; §82 Class-3) — the per-run
# cost-accumulator ContextVar. The IDENTICAL bootstrap-scoped shared-holder
# exposure the inter-step channel carries: `cost_record_accumulator` is always-on,
# so on a REUSED bootstrap `HarnessContext` (daemon-client mode) its `.records`
# would accumulate across `run_workflow` invocations (a wrong cost rollup, both
# sequentially AND concurrently). Closed by the SAME mechanism as the channel: a
# stable proxy on the frozen ctx resolving the current run's accumulator from this
# var. `api.run`/`resume` set a fresh accumulator around `[invoke + read]` (so
# their post-run `ctx.cost_record_accumulator.records` read resolves to the SAME
# accumulator the cost wrappers appended to — caller-set propagates into the
# `to_thread` worker via `copy_context`); the `run_workflow` handler sets a fresh
# one per run iff still unset (the daemon path, where no `api.run` caller set it).
# Default `None` → the proxy falls back to its bound bootstrap default
# (direct-stage / child-workflow paths with no active run boundary).
COST_ACCUM_VAR: contextvars.ContextVar[CostRecordAccumulator | None] = contextvars.ContextVar(
    "harness.cost_record_accumulator", default=None
)


class RunScopedCostRecordAccumulator(CostRecordAccumulator):
    """Stable ctx-bound proxy resolving the per-run `CostRecordAccumulator` from
    `COST_ACCUM_VAR` (B-INTERSTEP-PERRUN-ISOLATION).

    Bound on `HarnessContext.cost_record_accumulator` (always-on). It IS-A
    `CostRecordAccumulator` (the field type) but stores no records itself: serves
    as BOTH the reader (`.records`, read by `_build_run_result`) AND the sink
    (`.append`, threaded to the per-dispatch cost wrappers) — every access
    delegates to `_current()` (the run-scoped accumulator in the var, or a bound
    bootstrap default when no run is active). Threading this proxy (not its
    `.records` list) is what makes the wrappers append to the *current run's*
    accumulator at append-time rather than a list captured once at bootstrap.
    """

    __slots__ = ("_default",)

    def __init__(self) -> None:
        # Deliberately NOT calling super().__init__() — the inherited `records`
        # slot is shadowed by the property below and never used. The bootstrap
        # default is a real accumulator used only when no per-run one is bound.
        self._default = CostRecordAccumulator()

    def _current(self) -> CostRecordAccumulator:
        current = COST_ACCUM_VAR.get()
        return current if current is not None else self._default

    @property
    def records(self) -> list[SpanCostRecord]:  # pyright: ignore[reportIncompatibleVariableOverride]
        return self._current().records

    def append(self, record: SpanCostRecord) -> None:
        self._current().append(record)


# ----------------------------------------------------------------------------
# `HarnessContext` — C-RT-04 v1.1 schema.
# Frozen post-bootstrap. The `_MutableHarnessContext` builder used during the
# 9-stage bootstrap is implementation-discretion and lands at U-RT-43.
# ----------------------------------------------------------------------------
class HarnessContext(BaseModel):
    """Post-bootstrap handle through which `run()` reaches every wired component.

    Mutation during bootstrap goes through a separate `_MutableHarnessContext`
    builder (U-RT-43); at stage 7 INGRESS_ACCEPT the builder is materialized
    into this frozen final form. Every field is non-`None` at stage 7 EXCEPT
    `mcp_clients` (empty dict permitted) and `tenant_id`-derived audit-writer
    scoping.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    # Stage 0 PREAMBLE.
    config: RuntimeConfig
    drained_flag: asyncio.Event
    # U-RT-87 — Caller-side pause-signaling primitive per runtime spec v1.21
    # §4 + §14.14.3 sibling-pattern to `drained_flag`. Set by external caller
    # (operator API, MCP tool, etc.) to request driver pause at the next
    # per-step pre-entry; polled by CP driver at the per-step pre-entry as a
    # sibling check to `drained_flag.is_set()` per workflow_driver.py:549
    # precedent. When set + `ctx.pause_resume_protocol is not None`: driver
    # invokes `ctx.pause_resume_protocol.capture_pause_snapshot(...)` +
    # returns `RunStatus.PAUSED`. When set + `ctx.pause_resume_protocol is
    # None`: driver behavior unchanged from pre-v1.21 (silently no-op).
    # Initialized at `_MutableHarnessContext` builder during stage 0 PREAMBLE.
    pause_requested_flag: asyncio.Event

    # R-FS-1 arc CA — run-scoped cost-record accumulator. Per-dispatch cost helpers
    # (LLM/tool/validator/webhook) append their returned `SpanCostRecord` into
    # `accumulator.records` via their best-effort wrappers; `_build_run_result`
    # reads `accumulator.records` and rolls up `RunResult.cost_attribution` along
    # `RollupAxis.PER_PROVIDER_AND_MODEL` (runtime spec v1.53 §9 C-RT-09). It is a
    # `CostRecordAccumulator` holder (NOT a bare `list`) precisely so Pydantic
    # stores it BY REFERENCE under `arbitrary_types_allowed` — like `drained_flag`
    # /`tracer_provider` — surviving `freeze()` as the same object (a typed
    # `list[...]` field would be copied, disconnecting the dispatchers' captured
    # list from this read). `list.append` is atomic under the GIL; the only read is
    # post-join (after the `asyncio.to_thread` driver returns), no lock needed.
    # The `_MutableHarnessContext` builder's `default_factory` makes a fresh holder
    # per run; `freeze()` threads the SAME holder onto this frozen ctx; direct
    # (test) constructions default to a fresh empty holder.
    cost_record_accumulator: CostRecordAccumulator = Field(default_factory=CostRecordAccumulator)

    # Stage 1 IS.
    path_resolver: PathResolver
    worktree_manager: WorktreeIsolationManager
    shadow_git: ShadowGitSupervisor
    ledger_writer: LedgerWriter
    ledger_reader: LedgerReader
    index: ContentAddressedIndex
    cache: SemanticCache

    # Stage 2 AS.
    skills: dict[SkillID, Skill]
    tool_contracts: dict[ToolName, ToolContract]
    mcp_host: MCPHost
    mcp_clients: dict[ClientName, MCPClient]
    mcp_server: HarnessMCPServer | None = None
    """H_T-as-MCP-server hosting (U-RT-62; C-RT-18 §14.8.3 v1.12
    workflow-initiation topology pin).

    Materialized at bootstrap stage 2 alongside `mcp_host` (the
    H_T-as-MCP-client surface per U-RT-15). Sibling primitive per Q4
    ratification at the C-RT-18 v1.12 fork — the two MCP roles are
    orthogonal (server hosts the `run_workflow` tool; host/client
    consumes other MCP servers like filesystem / GitHub / sandbox).

    Defaulted to `None` for transitional bootstrap-builder shapes that
    don't materialize the server (carrier-preservation for prior-arc
    test substrates); post-stage-2 bootstrap completion writes a
    `HarnessMCPServer` instance.
    """

    sandbox_dispatch: SandboxDispatchTable

    # Stage 3a CP_CLIENTS.
    providers: dict[str, ProviderClient]

    # Stage 3b CP_ROUTING.
    routing_manifest: RoutingManifest
    engine_selector: EngineSelector
    fallback_chain: FallbackChain
    retry_breaker: RetryBreakerRegistry
    hitl_registry: HITLPlacementRegistry
    handoff_registry: HandoffRegistry

    prompt_manifest: PromptManifest = Field(
        default_factory=lambda: PromptManifest(
            manifest_version=1,
            active_prompt_version=PromptVersion(version_sha=""),
        ),
    )
    """Operator-supplied prompts-management carrier (IS spec v1.5 §5.2 third
    procedural-tier hash component).

    Mirrors `routing_manifest` (a frozen operator-supplied carrier on the
    context; the resolver reads a derived value at write-time). Unlike
    `routing_manifest` (stage-3b-enriched, required), `prompt_manifest` has no
    enrichment stage — it defaults to an empty manifest
    (`active_prompt_version.version_sha=""` → no active prompt), so operators
    that do not version prompts carry zero config burden.
    `resolve_procedural_tier_snapshot` reads `active_prompt_version.version_sha`
    as the `active_prompt_version` recipe component per IS spec v1.5 §C-IS-05
    §5.2. The fuller prompts-management surface (multi-prompt versioning +
    selection, with a materialization stage) is a separate forward arc per the
    §5.2 fork DP-4.
    """

    # Stage 4 OD.
    # `tracer_provider` is informational per C-RT-04 — consumers call
    # `opentelemetry.trace.get_tracer_provider()` per ADR-F5. We type it
    # arbitrarily (Protocol-ish via arbitrary_types_allowed) to avoid pulling
    # the OTel SDK type into the schema at L0; U-RT-27 fills it.
    tracer_provider: object
    collector_daemon: CollectorDaemonHandle
    cost_chain: CostAttributionChain
    audit_writer: AuditLedgerWriter
    # U-RT-84 — ValidatorFramework Protocol-typed binding per runtime spec
    # v1.18 §4 + §14.13. Narrowed from the v1.17-era `object | None` carrier
    # to the CP-canonical Protocol surface from CP spec v1.11 §25.1
    # (Mapping[str, Validator] + async evaluate(...)). Stage 4 OD-bucket
    # factory `materialize_validator_framework_stage` produces this; `None`
    # is the production-default (operator opt-out → driver hook False arm).
    # See §14.13.1 + plan v2.17 U-RT-84 AC #5.
    validator_framework: ValidatorFramework | None = None

    # U-RT-87 — PauseResumeProtocol carrier per runtime spec v1.21 §4 +
    # §14.14 (CP spec v1.13 §26 carrier from harness_cp.pause_resume_protocol).
    # Stage 5 LOOP_INIT-bucket factory `materialize_pause_resume_protocol_stage`
    # produces this; `None` is the production-default (operator opt-out → driver
    # per-step pre-entry pause-trigger detection branch False arm).
    # See §14.14.1 + plan v2.20 U-RT-87 AC #2.
    pause_resume_protocol: PauseResumeProtocol | None = None

    # U-RT-96 — Webhook delivery composer carrier. Populated at stage 5
    # LOOP_INIT by `materialize_webhook_delivery_composer_stage` per spec
    # v1.26 §14.16.3. `None` when `RuntimeConfig.webhook_delivery_composer_config
    # is None` (operator opt-out — production-default state; §14.8.8.1 step 0
    # OR-form precondition AND-arm evaluates False, durable-async branch
    # falls through to sync-blocking). Non-`None` when operator supplies the
    # config; the durable-async branch at §14.8.8.1 step 3 invokes
    # `ctx.webhook_delivery_composer.deliver_webhook(brief, idempotency_key)`.
    # See spec v1.26 §4 row + §14.16.
    webhook_delivery_composer: WebhookDeliveryComposer | None = None

    # U-RT-100 — SkillActivationSpanEmitter carrier per runtime spec v1.32
    # §4 + §14.17 (NEW C-RT-27). Operator-opt-in MVP per Reading B Q1=(B);
    # bound at stage-5 LOOP_INIT factory `materialize_skill_activation_emitter_stage`.
    # `None` when `RuntimeConfig.skill_activation_hook_config is None`
    # (operator opt-out — production-default state). See spec §14.17.1.
    skill_activation_emitter: SkillActivationSpanEmitter | None = None

    # C-RT-28 §14.20 (R-FS-1 arc M) — ManagedAgentsClientProtocol carrier.
    # Bound at stage-5 LOOP_INIT factory `materialize_managed_agents_dispatcher_stage`
    # when opted-in on `DeploymentSurface.MANAGED_CLOUD` (the operator-supplied
    # `RuntimeConfig.managed_agents_config.client`); `None` otherwise (opt-out /
    # non-managed-cloud — the StepKind.MANAGED_AGENTS dispatcher is then unbound,
    # so a managed-agents step fails closed). Operator-ratified 2026-06-17
    # (Option B). See spec §14.20.1.
    managed_agents_client: ManagedAgentsClientProtocol | None = None

    # U-RT-111 (v2.36) — `RuntimeCpIsWiring` carrier per runtime plan v2.36 §1.2.
    # Exposes the stage-6 CXA wiring (already materialized at
    # `_MutableHarnessContext.cxa_stages["cp_is_wiring"]`) at the frozen
    # HarnessContext so workflow_driver + sub_agent_dispatch + engine_selector
    # caller-sites can invoke U-RT-110's emit methods (override / workload-class-
    # selection / pause-resume workflow-layer / sibling-ledger). Operator-opt-in
    # default `None` — when None, caller-sites silent-skip emission (preserves
    # pre-v2.36 production behavior); when bound, fires the §16.5 composer
    # contract per CP spec v1.26 §16.5.7 firing-site discipline. Typed `object`
    # to avoid pulling `harness_runtime.lifecycle.cp_is_wiring.RuntimeCpIsWiring`
    # into the L0 schema (HarnessContext arbitrary_types_allowed=True per
    # `model_config`; consumers cast).
    cp_is_wiring: object | None = None

    # R-CXA-3 — `RuntimeCpAsWiring` carrier. Exposes the stage-6 CP -> AS
    # runtime registry that binds CP-consumed AS terminal seam exports. Typed
    # `object` to avoid importing the lifecycle module into the L0 schema.
    cp_as_wiring: object | None = None

    # R-003 producer-site lift — zero-arg resolver returning the
    # `procedural_tier_snapshot_ref` Identifier D-derivative sidecar per IS
    # spec v1.3 §C-IS-05 §5.1. Bound at bootstrap stage 6 (CXA_WIRING) to the
    # same `make_procedural_tier_snapshot_resolver(ctx)` closure that wires the
    # §16.5 CP composers (cp_is_wiring). Consumed by the CP driver's
    # `_append_step_ledger_entry` (§25.3.3.7 per-step state-ledger write) via
    # the `DriverContext.procedural_tier_snapshot_resolver` Protocol field.
    # Typed `object | None` mirroring `cp_is_wiring` (arbitrary_types_allowed;
    # consumers call dynamically); `None` = operator opt-out → sidecar `None`.
    procedural_tier_snapshot_resolver: object | None = None

    # B-INTERSTEP (R-FS-1 standalone arc; spec §14.21 C-RT-34, new at v1.59) —
    # run-scoped inter-step output channel (the shared run-context the dispatcher
    # reads). Bound at stage 5 LOOP_INIT to a fresh `InterStepOutputChannel` ONLY
    # when `RuntimeConfig.inter_step_data_flow` is True; `None` (default) = opt-out
    # → the driver records nothing + the LLM dispatcher injects nothing
    # (byte-identical to pre-v1.59). A plain by-reference holder (the
    # `CostRecordAccumulator` CA #625 pattern) stored under
    # `arbitrary_types_allowed`; a typed container field would be Pydantic-copied
    # at `freeze()`, disconnecting the driver's records from the dispatcher's read.
    inter_step_output_channel: InterStepOutputChannel | None = None

    engine_output_store: EngineOutputStore | None = None
    """B-ENGINE-OUTPUT-REPLAY (runtime spec C-RT-32) — the durable per-run
    output-carrying event-history store, bound at stage 5 LOOP_INIT when
    `RuntimeConfig.engine_output_replay` is True (else `None` → no recording /
    rehydration). The CP driver records each completed step output here BEFORE the
    F2 ledger-append (RESERVE-before-COMMIT) and rehydrates the inter-step channel
    from it on an EVENT_SOURCED_REPLAY resume. Read by the driver via the
    `cp_is_wiring` getattr idiom (harness-cp does not import the holder)."""

    # U-RT-94 — Runtime-internal sidecar carrier for one-shot ResumeContext
    # delivery across the pause-resume cycle. Bound at stage 5 LOOP_INIT to
    # an empty holder (``ResumeContextHolder()`` with ``_current_context = None``
    # default). Driver-side resume entry-point per CP spec v1.16 §26.8.5 calls
    # ``ctx.resume_context_holder.set(resume_context)`` after operator-supplied
    # ``attempt_resume(..., resume_context=...)`` ingestion. Runtime composer
    # at §14.8.8.5 resumed-step gate-evaluation consumes via
    # ``ctx.resume_context_holder.consume_and_clear()`` (atomic one-shot
    # read-and-clear). NOT operator-supplied at RuntimeConfig — the holder is
    # a runtime-loop carrier, not deployment-time configuration. Per spec
    # v1.25 §4 C-RT-04 + §14.8.8.9.
    resume_context_holder: ResumeContextHolder

    # Stage 5 LOOP_INIT.
    override_evaluator: PerStepOverrideEvaluator
    topology_dispatcher: TopologyDispatcher
    lifecycle_emitter: LifecycleEventEmitter
    llm_dispatcher: LLMDispatcher
    """Per-step LLM-dispatch composer (C-RT-15 §14.5).

    Materialized at stage 5 LOOP_INIT alongside the override evaluator,
    topology dispatcher, and lifecycle emitter. Satisfies the
    `harness_cp.workflow_driver.StepDispatcher` Protocol — run-loop
    callers can pass `ctx.llm_dispatcher` to the CP `workflow_driver`
    as the per-step dispatch site. Concretized by
    `harness_runtime.lifecycle.llm_dispatch.RuntimeLLMDispatcher`.
    """

    sub_agent_dispatcher: Any
    """Per-step sub-agent dispatch composer (U-RT-59; C-RT-17 §14.7) +
    HITL gate composer wrap layer (U-RT-60; C-RT-18 §14.8).

    Materialized at stage 5 LOOP_INIT. v1.6–v1.10 lineage: concretized by
    `harness_runtime.lifecycle.sub_agent_dispatch.RuntimeSubAgentDispatcher`
    (sync; satisfies `harness_cp.workflow_driver.StepDispatcher` Protocol).

    v1.11 (post-U-RT-60 wrap-asymmetry fork APPLIED): bound to
    `harness_runtime.lifecycle.hitl_gate_composer.RuntimeHITLGateComposer`
    with `applicable_placements={SUB_AGENT_BOUNDARY}` wrapping the
    sub-agent dispatcher. Composer is **async** per spec §14.8.1 item 1;
    field type widened from `_CpStepDispatcher` (sync Protocol) to `Any`
    to admit the async composer. The sync `StepDispatcher` Protocol
    satisfaction at the registry boundary is carried by `SyncDispatcherFacade`
    per the U-RT-59 Path B precedent (see `ctx.step_dispatchers`).
    """

    ask_user_question_surface: Any
    """H_E `AskUserQuestion` delivery surface (U-RT-60 AC #2; C-RT-18 §14.8.3
    v1.11 binding pin).

    Materialized at stage 5 LOOP_INIT. Concretized by
    `harness_runtime.lifecycle.mcp_backed_ask_user_question_surface.MCPBackedAskUserQuestionSurface`.
    Satisfies the `AskUserQuestionSurface` Protocol declared at
    `harness_runtime.lifecycle.ask_user_question_surface`. Bound into both
    HITL composers (`PRE_ACTION` + `SUB_AGENT_BOUNDARY`) so the composer
    body step 4f can `await self.ask_user_question_surface.ask(...)`.

    Typed `Any` per the C-RT-04 Protocol-vs-concrete-narrowing pattern
    (mirrors `sub_agent_dispatcher` + `tracer_provider`)."""

    # Stage 3a CP_CLIENTS (extended at U-RT-72 per spec v1.16 §4 C-RT-04;
    # reshaped singular→mapping at U-RT-125 per spec v1.51 §14.9.10 D1).
    mcp_client_hosts: dict[ServerName, Any]
    """H_T-as-MCP-client hosts, keyed by `server_name` (U-RT-63/64/65/66 — each
    value concretized by
    `harness_runtime.lifecycle.mcp_client_host.MCPClientHost`).

    Materialized at stage 3a by `materialize_mcp_client_host_stage` (U-RT-73 /
    U-RT-126). Reshaped from the singular `mcp_client_host: MCPClientHost` to
    this `dict[ServerName, MCPClientHost]` mapping at U-RT-125 (runtime spec
    v1.51 §14.9.10 D1), sibling-mirroring `mcp_clients: dict[ClientName,
    MCPClient]` (`:1650`). The key is `server_name` (the per-deployment registry
    ID the dispatcher / trust gate / spans already read), NOT `client_name`.

    Distinct primitive from `ctx.mcp_host` (the H_T-as-MCP-server stage-2
    surface per U-RT-15) — `mcp_host` hosts H_T's tools for external MCP
    clients; `mcp_client_hosts` consume external MCP servers' tools at H_T's
    `TOOL_STEP` dispatch site. Spec §14.9.2 inv 4: ctx.mcp_host ≠ ctx.mcp_client_hosts.

    Carrier-shape reshape (U-RT-125/126, B2-impl-2a): a single-configured-host
    bootstrap yields a 1-entry dict; the dispatcher still resolves a single host
    (cross-host routing + per-host sandbox are U-RT-127/128/130, B2-impl-2b).

    Value typed `Any` per the C-RT-04 Protocol-vs-concrete-narrowing pattern to
    avoid the `lifecycle.mcp_client_host → types` import cycle (matches
    existing `sub_agent_dispatcher` precedent at v1.11).
    """

    # Stage 5 LOOP_INIT (extended at U-RT-72 per spec v1.16 §4 C-RT-04).
    tool_dispatcher: Any
    """`TOOL_STEP` dispatch entry point — the C-RT-21 §14.11 retry-wrap
    wrapper around the bare C-RT-19 `RuntimeToolDispatcher` (concretized by
    `harness_runtime.lifecycle.retry_breaker_tool.RetryBreakerToolDispatcher`).

    Materialized at stage 5 by `materialize_runtime_tool_dispatcher_stage`
    (U-RT-75). The bare `RuntimeToolDispatcher` is private to the wrapper
    (constructor arg per spec §14.9.6 invariant 6); not surfaced here.
    Mirrors the §14.6 wrap-binding pattern at `ctx.llm_dispatcher`.

    Typed `Any` per the C-RT-04 Protocol-vs-concrete-narrowing pattern to
    avoid the `lifecycle.retry_breaker_tool → types` import cycle.
    """

    hitl_tool_loop: Any
    """R-CXA-2 model-driven HITL tool-loop producer.

    Materialized at stage 5 LOOP_INIT by
    `harness_runtime.bootstrap.factories.r_cxa_2_producer_loop_factory
    .materialize_r_cxa_2_producer_loop_stage`. Exposes the runtime primitive
    that rewrites model-emitted tool calls through C-CP-17 and emits the
    `cp.hitl-tool-call-rewriting` CP→IS state-ledger row before dispatch.
    Typed `Any` to keep the schema layer free of lifecycle-module imports.
    """

    engine_recovery_loop: Any
    """R-CXA-2 engine-layer pause/resume recovery-loop producer.

    Materialized at stage 5 LOOP_INIT by the same R-CXA-2 producer factory.
    Exposes the runtime primitive that binds an engine-layer substrate and
    emits `cp.pause-captured` / `cp.resume-attempted` through CP→IS wiring.
    Typed `Any` to keep the schema layer free of lifecycle-module imports.
    """

    per_server_trust_evaluator: Any
    """CP-axis per-server trust evaluator (U-CP-68 — concretized by
    `harness_cp.per_server_trust_evaluator.PerServerTrustEvaluator`).

    Materialized at stage 5 within
    `materialize_runtime_tool_dispatcher_stage` (U-RT-75) step 1 from
    `config.trust_policy` (or `TrustPolicy.default()` if `None`). Consumed
    by the bare `RuntimeToolDispatcher.dispatch` step 2 per-server-trust
    gate per spec §14.9.2 inv 2.

    Typed `Any` per the C-RT-04 narrowing pattern (consistent with the
    other stage-5 sibling fields).
    """

    memory_tool_registry: Any
    """Memory tool storage-backend registry (U-RT-78
    `MemoryToolRegistry` — concretized by
    `harness_runtime.lifecycle.memory_tool_registry.MemoryToolRegistry`).

    Materialized at stage 5 LOOP_INIT by
    `materialize_memory_tool_registry_stage` (U-RT-80) per spec v1.17
    §14.12.3. Resolves a `MemoryToolStorageBackendProtocol` implementation
    per `RuntimeConfig.deployment_surface` + optional
    `RuntimeConfig.memory_tool_backend_config` override per §14.12.1.
    Consumed by C-RT-15 §14.5.1 callback-injection composer-step
    (U-RT-81) when `step.step_payload.tools` contains the Anthropic
    Memory tool definition (`tool type "memory_20250818"` per ADR-D3
    v1.2 §1.1 #11).

    Added at U-RT-79 per `Spec_Harness_Runtime_v1.md` v1.17 §4 C-RT-04
    field-table extension. Typed `Any` per the C-RT-04 Protocol-vs-
    concrete-narrowing pattern (mirrors `tool_dispatcher` +
    `mcp_client_hosts`) to avoid the `lifecycle.memory_tool_registry →
    types` import cycle.
    """

    mcp_namespace_emitter: Any
    """CP-axis MCP namespace emitter (U-CP-69 — concretized by
    `harness_cp.mcp_client_namespace_emitter.MCPClientNamespaceEmitter`).

    Materialized at stage 5 within
    `materialize_runtime_tool_dispatcher_stage` (U-RT-75) step 2 from
    the resolved MCP host's `tool_registry` (`ctx.mcp_client_hosts`; the sole
    host at B2-impl-2a, the routed host at U-RT-128). Consumed by the bare
    `RuntimeToolDispatcher` mid-dispatch to emit the canonical 7-attribute
    `mcp.*` namespace on the `mcp.tool.call` span per C-AS-14 §14.3.

    Typed `Any` per the C-RT-04 narrowing pattern (consistent with the
    other stage-5 sibling fields).
    """

    step_dispatchers: _CpStepDispatcherRegistry
    """Step-kind routing registry (U-RT-59; C-RT-17 §14.7.1 + §14.7.7).

    Materialized at stage 5 LOOP_INIT. Concretized by
    `harness_runtime.lifecycle.step_dispatchers.StepKindDispatcherRegistry`.
    The CP driver consumes via `step_dispatchers.lookup(step.kind).
    dispatch(...)` at every per-step dispatch site.

    v1.6 MVP binds 1 of 5 `StepKind` values: `SUB_AGENT_DISPATCH →
    sub_agent_dispatcher`. The `INFERENCE_STEP` binding is deferred behind
    a Class 1 fork on the U-RT-58 wrapper async/sync surface — the
    async wrapper does not compose with the sync driver as a registry
    binding. Tool / HITL / validator step kinds remain unbound at v1.6;
    `lookup` raises `StepKindDispatcherNotBoundError` (driver maps to
    `RT-FAIL-STEP-KIND-DISPATCHER-NOT-BOUND`).
    """

    @computed_field  # type: ignore[prop-decorator]
    @property
    def tenant_id(self) -> str | None:
        """Multi-tenant scoping key surfaced from ``RuntimeConfig.tenant_id``.

        Exposed as a computed property so the CP-side ``DriverContext``
        Protocol (``harness_cp.workflow_driver.DriverContext.tenant_id``) is
        structurally satisfied without duplicating storage. The CP driver
        reads ``ctx.tenant_id`` at the ``StepExecutionContext`` composition
        site (``workflow_driver.py`` per spec C-CP-25 §25.2.1) instead of
        the v1.6 MVP hardcoded ``None``. ``None`` = single-tenant (preserved
        at audit-writer via the ``_SINGLE_TENANT_TAG`` sentinel); operator-
        supplied non-None values flow through the 4-substep audit
        composition unchanged (``StepExecutionContext.tenant_id`` propagates
        to ``audit_writer.append(tenant_id, ...)`` at sub_agent_dispatch /
        hitl_gate_composer / llm_dispatch composition sites).

        Per ``workflow_driver_types.py`` deferral comment at the v1.6 MVP
        baseline: this is the v1.7+ extension that lifts the deferred-to-
        implementation-discretion hardcode as a binding fix (NOT a
        WorkflowManifestEntry schema extension — tenant_id is per-deployment
        scoping sourced from RuntimeConfig, not per-workflow operator-
        surfaced like ``default_gate_level`` at CP spec v1.20 §6.1.Y).
        """
        return self.config.tenant_id

    def activate_skill(self, skill_id: SkillID, workflow_id: str = "") -> None:
        """Operator-explicit Skill activation hook (hook-3 per spec §14.17.2).

        Emits one ``skill.activation`` span with ``activation_mode =
        FILESYSTEM_READ``. Silent-skip when ``skill_activation_emitter is
        None`` (operator opt-out path per §14.17.5 invariant 3). Raises
        ``UnknownSkillError`` when ``skill_id`` is not loaded.

        Per ``Spec_Harness_Runtime_v1.md`` v1.32 §14.17.2 hook-3 (plan v2.28
        U-RT-101). Co-located with ``HarnessContext`` per Q4=(q) NEW module
        ratification at the fork: the activation surface (this method) lives
        on the context; the emitter carrier lives at
        ``harness_runtime/lifecycle/skill_activation.py``.

        Parameters
        ----------
        skill_id :
            Identifier of the loaded Skill to activate.
        workflow_id :
            Operator-supplied workflow correlation key per spec §14.17.7
            deferred-discretion option (a). Defaults to empty string when
            invoked outside an active workflow; operator code with workflow
            scope SHOULD pass the current workflow_id explicitly.
        """
        if self.skill_activation_emitter is None:
            # Silent-skip per §14.17.5 invariant 3 (operator opt-out path).
            return
        if skill_id not in self.skills:
            raise UnknownSkillError(skill_id)
        from harness_runtime.lifecycle.skill_activation import SkillActivationMode

        self.skill_activation_emitter.emit(
            skill_id=skill_id,
            mode=SkillActivationMode.FILESYSTEM_READ,
            workflow_id=workflow_id,
            skill=self.skills[skill_id],
        )
