"""Runtime consumer of the CP prompt-selection surface (R-PM-1 cascade PR #3).

The CP ``PromptSelectionManifest`` resolver (``harness_cp.prompt_selection_manifest``)
yields a selected prompt ``version_sha`` for a ``(role, workload)``; the IS
``PromptManifest.versions`` content-addressed store (IS spec v1.7 §5.3) resolves
that sha to its content; the runtime translate-time injection seam (PR #1,
runtime spec v1.44 §14.5.2) places the content as a system prompt. **This module
is the runtime CONSUMER site that composes the two** — selection (CP) → sha →
store member (IS) → effective active prompt. It is where the PR #2 store gains
its consumer (the CP→IS store consultation is the CXA seam registered at
cascade PR #5; runtime is the consumer endpoint).

**Dimension honesty.** Reconciliation reads the run's REAL ``workload_class``
(threaded into ``run_bootstrap``) and the MVP-default agent role
(``_MVP_DEFAULT_AGENT_ROLE`` — the runtime has no per-step role at MVP; routing's
own ``per_role_bindings`` is likewise role-keyed only at R-300-second-provider).
So per-workload selection (``per_workload_overrides``) is behavior-driving
end-to-end, while per-role bindings are carried faithfully but resolved against
the default role until real per-role dispatch lands. This mirrors the routing
precedent exactly (`[[r-cxa-seam-wiring-is-producer-discovery]]` — don't build
the hollow per-role runtime indexer).

**Hash/injection coherence (the load-bearing correctness property).** The
selected version is reconciled ONTO ``active_prompt_version`` (a ``model_copy``
to the selected store member), NOT merely redirected at the injection reader. So
BOTH the runtime stage-5 injection reader (``active_prompt_version.content``) and
the C-IS-05 §5.2 procedural-tier hash reader (``active_prompt_version.version_sha``)
read the SAME selected version — consistent by construction. (Redirecting only
injection would reintroduce the content↔hash drift that PR #1's
``version_sha == digest(content)`` derive-invariant closed, one layer up.) The
``model_copy`` skips ``PromptManifest``'s ``mode="after"`` store-invariant
validator, but the selected member is already an authored store member satisfying
content↔sha + membership, so the copy is invariant-preserving.

**Cross-axis membership = fail-loud.** A bound sha that is not an authored member
of the store is a CP↔IS check declared runtime-deferred at the CP spec (§29.3,
mirroring ``validate_routing_manifest``'s runtime-deferred per-role
model-presence check); it is enforced HERE, detect-then-refuse
(``PromptSelectionUnauthoredError`` → ``RT-FAIL-PROMPT-SELECTION-UNAUTHORED``).

Authority: CP spec v1.31 §29 (the prompt-selection contract — incl. §29.4 the
runtime-consumer-site obligation + RT-FAIL-PROMPT-SELECTION-UNAUTHORED); runtime
spec v1.44 §14.5.2 (the translate-time injection seam this composes with); IS
spec v1.7 §5.3 (the `versions` store this consumes); R-PM-1 design §4.2.
"""

from __future__ import annotations

from harness_core import PersonaTier, WorkloadClass
from harness_cp.cp_shared_types import AgentRole
from harness_cp.prompt_selection_manifest import (
    PromptSelectionManifest,
    resolve_active_prompt_version_sha,
    validate_prompt_selection_manifest,
)
from harness_is.prompt_manifest import PromptManifest
from harness_od.prompt_governance_gradient import resolve_prompt_governance

__all__ = [
    "InvalidPromptSelectionManifestError",
    "PromptSelectionUnauthoredError",
    "PromptVersionUnapprovedError",
    "enforce_prompt_version_approval",
    "reconcile_active_prompt_via_selection",
    "resolve_per_role_system_prompts",
]

# Mirrors ``llm_dispatch._MVP_DEFAULT_AGENT_ROLE`` — the runtime has no per-step
# agent role at MVP, so per-role prompt selection resolves against this default
# until real per-role dispatch (R-300-second-provider). Per-workload selection
# keys on the genuine run workload and is behavior-driving today.
_MVP_DEFAULT_AGENT_ROLE = AgentRole("default")


class PromptSelectionUnauthoredError(Exception):
    """Raised when a prompt-selection binding names a ``version_sha`` that is not
    an authored member of the IS ``PromptManifest.versions`` store (R-PM-1 PR #3).

    The store-membership check is a cross-axis (CP selection ↔ IS store) check,
    declared runtime-deferred at the CP spec (§29.3, mirroring
    ``validate_routing_manifest``'s runtime-deferred model-presence check) and
    enforced here at the runtime consumer site: **fail-loud / detect-then-refuse**,
    never silently fall through to the inline active prompt (consistent with the
    arc-#1 ``RT-FAIL-SANDBOX-DRIVER-UNAVAILABLE`` + PR-#1
    ``RT-FAIL-PROMPT-INJECTION-CONFLICT`` postures + ``[[conformance-validator-disciplines]]``).

    Maps to ``RT-FAIL-PROMPT-SELECTION-UNAUTHORED`` per CP spec v1.31 §29.4.
    Raised at bootstrap stage 0 reconciliation (before any procedural-tier
    snapshot is computed + before the dispatcher is constructed) → surfaces as a
    ``BootstrapFailure`` — a config/authoring error the operator must correct (you
    cannot select a version that was never authored), unlike the per-dispatch
    step-level ``RT-FAIL-PROMPT-INJECTION-CONFLICT``.
    """

    def __init__(self, version_sha: str) -> None:
        self.version_sha = version_sha
        super().__init__(
            "RT-FAIL-PROMPT-SELECTION-UNAUTHORED: prompt-selection binding names "
            f"version_sha={version_sha!r} which is not an authored member of the "
            "PromptManifest.versions store; fail-loud (a version must be authored "
            "in the store before it can be selected)"
        )


class InvalidPromptSelectionManifestError(Exception):
    """Raised when the operator-supplied ``PromptSelectionManifest`` fails the CP
    structural validator (``validate_prompt_selection_manifest``) at the runtime
    consumer site (R-PM-1 PR #3).

    Mirrors ``build_routing_manifest``'s ``InvalidRoutingManifestError`` — the CP
    structural manifest contract (e.g. ``manifest_version >= 1``) is enforced at
    bootstrap rather than silently bypassed (Codex P2-2). Raised at stage 0
    reconciliation → surfaces as a ``BootstrapFailure``. Maps to CP spec v1.31
    §29.5 (the ``manifest_version < 1`` row).
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"invalid prompt-selection manifest: {reason}")


def reconcile_active_prompt_via_selection(
    prompt_manifest: PromptManifest,
    selection_manifest: PromptSelectionManifest | None,
    *,
    workload_class: WorkloadClass,
    role: AgentRole = _MVP_DEFAULT_AGENT_ROLE,
) -> PromptManifest:
    """Return ``prompt_manifest`` with ``active_prompt_version`` reconciled to the
    version the CP selection layer chooses for ``(role, workload)``, or unchanged.

    Fall-through (returns the manifest unchanged → the #496/PR-#1 standing inline
    active prompt, zero behavior change):

    * ``selection_manifest is None`` (no selection configured — the default), or
    * the manifest selects nothing for ``(role, workload)`` (``resolve`` → ``None``).

    Otherwise the selected ``version_sha`` MUST resolve to an authored member of
    ``prompt_manifest.versions``; the manifest is returned with
    ``active_prompt_version`` set to that member (so injection + the §5.2 hash
    both read it). A selected sha with no store member raises
    :class:`PromptSelectionUnauthoredError` (fail-loud). An operator-supplied
    manifest that fails the CP structural validator raises
    :class:`InvalidPromptSelectionManifestError` (fail-loud — parity with
    ``build_routing_manifest``'s bootstrap validation; Codex P2-2)."""
    if selection_manifest is None:
        return prompt_manifest
    validation_error = validate_prompt_selection_manifest(selection_manifest)
    if validation_error is not None:
        raise InvalidPromptSelectionManifestError(validation_error.reason)
    selected_sha = resolve_active_prompt_version_sha(
        selection_manifest, role=role, workload=workload_class
    )
    if selected_sha is None:
        return prompt_manifest
    for version in prompt_manifest.versions:
        if version.version_sha == selected_sha:
            return prompt_manifest.model_copy(update={"active_prompt_version": version})
    raise PromptSelectionUnauthoredError(selected_sha)


class PromptVersionUnapprovedError(Exception):
    """Raised when a binding-tier deployment activates a *selection-driven* prompt
    version whose ``version_sha`` the operator has not approved (R-PM-1 PR #4).

    The per-persona-tier prompt-governance posture (OD spec C-OD-34,
    ``harness_od.prompt_governance_gradient.resolve_prompt_governance``) requires
    approval at team-binding + multi-tenant-compliance: a shared / tenant prompt is
    a governed artifact. When a supplied ``PromptSelectionManifest`` *drives* an
    active version at such a tier, that version's ``version_sha`` MUST be a member
    of ``RuntimeConfig.approved_prompt_version_shas``; otherwise this fail-loud /
    detect-then-refuse error fires — never silently activate an unapproved prompt
    version at a binding tier (consistent with the PR-#3
    ``RT-FAIL-PROMPT-SELECTION-UNAUTHORED`` + PR-#1 ``RT-FAIL-PROMPT-INJECTION-CONFLICT``
    + arc-#1 ``RT-FAIL-SANDBOX-DRIVER-UNAVAILABLE`` postures;
    ``[[conformance-validator-disciplines]]``).

    Maps to ``RT-FAIL-PROMPT-VERSION-UNAPPROVED`` per OD spec C-OD-34. Raised at
    bootstrap stage 0 (after selection reconciliation, before any procedural-tier
    snapshot) → surfaces as a ``BootstrapFailure`` — a governance/config error the
    operator corrects by attesting the version (add its sha to
    ``approved_prompt_version_shas``) or not selecting it at a binding tier.
    """

    def __init__(self, *, persona_tier: PersonaTier, version_sha: str) -> None:
        self.persona_tier = persona_tier
        self.version_sha = version_sha
        super().__init__(
            "RT-FAIL-PROMPT-VERSION-UNAPPROVED: selection drove prompt "
            f"version_sha={version_sha!r} active at persona_tier="
            f"{persona_tier.value!r} (a binding tier requiring approval), but it is "
            "not a member of RuntimeConfig.approved_prompt_version_shas; fail-loud "
            "(a binding-tier prompt version must be operator-approved before it can "
            "be activated via selection)"
        )


def enforce_prompt_version_approval(
    *,
    persona_tier: PersonaTier,
    selection_manifest: PromptSelectionManifest | None,
    approved_prompt_version_shas: frozenset[str],
    workload_class: WorkloadClass,
    role: AgentRole = _MVP_DEFAULT_AGENT_ROLE,
) -> None:
    """Fail-loud if a binding-tier deployment activates an unapproved
    *selection-driven* prompt version (OD spec C-OD-34 per-tier prompt governance).

    No-op (the gate is inert) when any of:

    * the tier's posture does not require approval
      (``resolve_prompt_governance(persona_tier).approval_required`` is ``False`` —
      the solo-developer / local-first tier), or
    * no selection manifest is configured (``selection_manifest is None`` — an
      inline-only deployment has nothing *selection-driven* to govern), or
    * selection falls through for the run's ``(role, workload)``
      (``resolve_active_prompt_version_sha`` → ``None`` — the active prompt is the
      inline default, not selection-driven).

    Otherwise the selection-driven ``version_sha`` MUST be a member of
    ``approved_prompt_version_shas``; if not, raises
    :class:`PromptVersionUnapprovedError` (``RT-FAIL-PROMPT-VERSION-UNAPPROVED``).

    Called at bootstrap stage 0 *after* :func:`reconcile_active_prompt_via_selection`
    (so the manifest is already structurally validated + the selected sha is already
    a verified store member); this re-resolves the same pure CP resolver to identify
    the selection-driven sha without depending on the reconciler's internals. The
    posture is owned by OD (``harness_od``); this runtime site is its enforcement
    consumer — mirroring the ``PER_PERSONA_TIER_REDACTION`` ⊳ ``RedactionSpanProcessor``
    posture-vs-consumer split.
    """
    if not resolve_prompt_governance(persona_tier).approval_required:
        return
    if selection_manifest is None:
        return
    selected_sha = resolve_active_prompt_version_sha(
        selection_manifest, role=role, workload=workload_class
    )
    if selected_sha is None:
        return
    if selected_sha not in approved_prompt_version_shas:
        raise PromptVersionUnapprovedError(persona_tier=persona_tier, version_sha=selected_sha)


def resolve_per_role_system_prompts(
    prompt_manifest: PromptManifest,
    selection_manifest: PromptSelectionManifest | None,
    *,
    workload_class: WorkloadClass,
    persona_tier: PersonaTier,
    approved_prompt_version_shas: frozenset[str],
) -> dict[AgentRole, str]:
    """Resolve each per-role-bound ``AgentRole`` → its effective system-prompt
    content for ``workload_class`` (R-FS-1 arc B4 — per-role prompt threading,
    runtime spec §14.5.3).

    Returns the per-role injection map the LLM dispatcher indexes at dispatch on
    the branch ``step_context.agent_role`` (the §14.5.2 translate seam injects the
    looked-up content; an unbound role falls through to the stage-0 default-role
    ``active_system_prompt``). Built at bootstrap stage 0 — *before* the dispatcher
    is constructed — so the SAME fail-loud checks the default-role path runs apply
    per role: :func:`reconcile_active_prompt_via_selection` raises
    :class:`PromptSelectionUnauthoredError` / :class:`InvalidPromptSelectionManifestError`,
    and :func:`enforce_prompt_version_approval` raises
    :class:`PromptVersionUnapprovedError` at a binding persona tier — all surfacing
    as a ``BootstrapFailure``.

    The DEFAULT role (``_MVP_DEFAULT_AGENT_ROLE``) is EXCLUDED — it is already the
    stage-0-resolved ``active_prompt_version`` (the dispatcher's
    ``active_system_prompt``), and excluding it keeps the ``SINGLE_THREADED_LINEAR``
    / unbound-branch dispatch path falling through unchanged (§14.5.3 invariant:
    linear path untouched). ``selection_manifest is None`` / no per-role bindings →
    ``{}`` → every dispatch falls through (byte-identical to pre-B4).
    """
    if selection_manifest is None:
        return {}
    result: dict[AgentRole, str] = {}
    for role in selection_manifest.per_role_bindings:
        if role == _MVP_DEFAULT_AGENT_ROLE:
            continue
        # Reuse the SAME fail-loud store-membership + structural-validation
        # machinery as the default-role path; raises surface as BootstrapFailure.
        reconciled = reconcile_active_prompt_via_selection(
            prompt_manifest,
            selection_manifest,
            workload_class=workload_class,
            role=role,
        )
        # Per-role binding-tier governance (parity with the default-role gate).
        enforce_prompt_version_approval(
            persona_tier=persona_tier,
            selection_manifest=selection_manifest,
            approved_prompt_version_shas=approved_prompt_version_shas,
            workload_class=workload_class,
            role=role,
        )
        result[role] = reconciled.active_prompt_version.content
    return result
