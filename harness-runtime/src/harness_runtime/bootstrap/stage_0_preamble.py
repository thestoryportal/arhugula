"""Stage 0 PREAMBLE — populate `ctx.config` + sub-config-derived resources.

Per `Spec_Harness_Runtime_v1.md` v1.1 §2 stage 0 post-conditions:
`ctx.config: RuntimeConfig` populated; sub-configs (path bindings, secrets,
OTel, collector) materialized; `drained_flag: asyncio.Event` initialized.

Also constructs orchestrator-internal handles used downstream:
- provider-secret resolver for stage 3a provider construction.
- `Actor` for stage 1 state-ledger writer construction (runtime identity =
  `Actor(actor_class=AGENT, actor_id="harness-runtime")`; per
  `[[u-rt-43-implementation-plan]]` §10 — using AGENT avoids an IS spec
  change that would otherwise require adding a `RUNTIME` actor class).
"""

from __future__ import annotations

import asyncio

from harness_core.workload_class import WorkloadClass
from harness_is.state_ledger_entry_schema import Actor, ActorClass

from harness_runtime.bootstrap.mutable_context import _MutableHarnessContext
from harness_runtime.config.provider_secrets import make_provider_secret_resolver
from harness_runtime.lifecycle.prompt_selection import (
    enforce_prompt_version_approval,
    reconcile_active_prompt_via_selection,
    resolve_per_role_system_prompts,
)
from harness_runtime.types import RuntimeConfig

__all__ = ["execute"]


async def execute(
    ctx: _MutableHarnessContext,
    config: RuntimeConfig,
    workload_class: WorkloadClass,
) -> None:
    """Populate the stage 0 PREAMBLE fields on `ctx`."""
    ctx.config = config
    ctx.drained_flag = asyncio.Event()
    # U-RT-87 — `pause_requested_flag` sibling-pattern to `drained_flag` per
    # runtime spec v1.21 §4 + §14.14.3. Caller-side pause-signaling primitive
    # consumed at workflow_driver per-step pre-entry detection.
    ctx.pause_requested_flag = asyncio.Event()
    ctx.keyring_resolver = make_provider_secret_resolver(config.provider_secrets)
    ctx.actor = Actor(actor_class=ActorClass.AGENT, actor_id="harness-runtime")
    # R-CL-P4 — operator-supplied prompts-management carrier (IS spec v1.5 §5.2
    # third procedural-tier hash component). Ambient substrate copied from
    # `RuntimeConfig.prompt_manifest` here at stage 0 (mirroring how
    # `routing_manifest` flows config → ctx, but without a dedicated
    # materialization stage — no enrichment is needed). Set before the first
    # `resolve_procedural_tier_snapshot` call (stage 3b producer sites) so the
    # snapshot reflects an operator-selected prompt version when one is supplied;
    # defaults to the empty manifest otherwise.
    ctx.prompt_manifest = config.prompt_manifest
    # R-PM-1 cascade PR #3 — reconcile the effective active prompt via the CP
    # prompt-selection layer HERE at stage 0, BEFORE the first procedural-tier
    # snapshot is computed (the stage-3b CP producer sites) and before the stage-5
    # injection reader. Reconciling at stage 0 (not stage 5) guarantees EVERY
    # downstream procedural-tier emission AND the injection reader read the SAME
    # selected version — coherent audit hashes across ALL stages (Codex P2-1).
    # Per-workload selection keys on the REAL run `workload_class`; per-role on the
    # MVP-default role. None / no-match → unchanged (the #496/PR-#1 inline active
    # prompt). An invalid manifest or a binding to an unauthored sha is fail-loud.
    # Per CP spec v1.31 §29.4.
    ctx.prompt_manifest = reconcile_active_prompt_via_selection(
        ctx.prompt_manifest,
        config.prompt_selection_manifest,
        workload_class=workload_class,
    )
    # R-PM-1 cascade PR #4 — per-persona-tier prompt governance (OD spec C-OD-34).
    # AFTER reconciliation (so the manifest is structurally valid + the selected sha
    # is already a verified store member): at a binding persona tier
    # (team-binding / multi-tenant-compliance) a selection-DRIVEN active prompt
    # version is a governed artifact whose sha must be operator-approved
    # (`approved_prompt_version_shas`), else fail-loud RT-FAIL-PROMPT-VERSION-
    # UNAPPROVED. Inert at solo-developer (local-first) + for inline-only / no-match
    # deployments (nothing selection-driven). The OD posture is consumed here per
    # the PER_PERSONA_TIER_REDACTION ⊳ RedactionSpanProcessor posture/consumer split.
    enforce_prompt_version_approval(
        persona_tier=config.persona_tier,
        selection_manifest=config.prompt_selection_manifest,
        approved_prompt_version_shas=config.approved_prompt_version_shas,
        workload_class=workload_class,
    )
    # R-FS-1 arc B4 — per-role PROMPT threading (runtime spec §14.5.3). Pre-resolve
    # each per-role-bound AgentRole's system-prompt content into
    # `ctx.per_role_system_prompts` — HERE at stage 0 so the same fail-loud
    # store-membership + binding-tier governance checks the default-role path runs
    # apply per role (BootstrapFailure before the dispatcher is constructed). The
    # stage-5 LLM-dispatcher factory binds the map; the dispatcher indexes it
    # per-branch at dispatch. Empty / no per-role bindings → `{}` → byte-identical
    # to pre-B4 dispatch. (The selection manifest's own hash visibility for the
    # procedural-tier snapshot is handled in the resolver, which reads
    # `config.prompt_selection_manifest` directly — IS spec §5.2 v1.9.)
    ctx.per_role_system_prompts = resolve_per_role_system_prompts(
        ctx.prompt_manifest,
        config.prompt_selection_manifest,
        workload_class=workload_class,
        persona_tier=config.persona_tier,
        approved_prompt_version_shas=config.approved_prompt_version_shas,
    )
