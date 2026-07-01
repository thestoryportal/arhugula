"""U-RT-112 — Procedural-tier snapshot resolver primitive.

Per ``Spec_Information_Substrate_v1.md`` v1.3 §C-IS-05 §5.2 (NEW resolver
contract). Plan-unit ownership at ``Implementation_Plan_Harness_Runtime_v2_42.md``
§1 U-RT-112 (residence-pinned per Q-γ=(γ-2) operator ratification 2026-05-30 at
`.harness/architect_recommendation_h_t_is_2_artifact_tier_registry_wiring.md`
§11.4). Supersedes IS plan v2.4 U-IS-18 residence-deferred placeholder; IS plan
v2.5 retires U-IS-18 (RELOCATED-TO-U-RT-112).

The resolver implements the spec §5.2 content-hash recipe over the four
procedural-tier components (`active_prompt_version` + `active_skills_versions`
+ `prompt_selection_manifest_sha` + `routing_manifest_sha`). The
`prompt_selection_manifest_sha` component is NEW at **v1.9** (R-FS-1 arc B4 —
per-role prompt coherence): a SHA-256 over the whole `PromptSelectionManifest`
canonical-JSON bytes (`""` when `None`), mirroring `routing_manifest_sha`, so a
fan-out branch's per-role prompt selection is hash-visible (forward-only rebase;
`None`/empty selection is byte-identical to the v1.8 3-component hash). The
prompts component (`active_prompt_version`) was deferred at v1.3 per spec §5.2
Deferral footer and is bound at **v1.5**
(post-MVP closure R-CL-P4; fork
`.harness/class_1_fork_prompts_management_surface_active_prompt_version.md`,
operator-ratified 2026-06-11): the runtime ``HarnessContext.prompt_manifest:
PromptManifest`` carrier (``harness_is.prompt_manifest``) homes the active
prompt version, mirroring the ``routing_manifest`` precedent (empty-defaultable
operator-supplied frozen carrier read at write-time). Hash rebasing at v1.5 is
expected per spec §5.2 (any recipe-component change yields different output over
identical procedural state; snapshot-ref equality is scoped within a single
recipe-version generation; forward-only — no migration of historical entries).

Recipe (v1.9 — 4-component scope):

    sha256(canonical_json({
        "active_prompt_version": ctx.prompt_manifest.active_prompt_version.version_sha,
        "active_skills_versions": sorted(
            set(skill.manifest.version_sha for skill in ctx.skills.values())
        ),
        "prompt_selection_manifest_sha": (
            "" if ctx.config.prompt_selection_manifest is None else sha256(
                canonical_json(ctx.config.prompt_selection_manifest).encode("utf-8")
            ).hexdigest()
        ),
        "routing_manifest_sha": sha256(
            ctx.routing_manifest.model_dump_json(by_alias=False).encode("utf-8")
        ).hexdigest(),
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

Direct-compute discipline per spec §5.2: NO separate snapshot-keyed registry
persists at H_T; resolver re-computes from current ``HarnessContext`` state at
every call. Procedural artifacts themselves persist at filesystem+git per IS
spec §C-IS-02 line 163 (working-tier ``HarnessContext`` is the read-side surface;
durable persistence is at the substrate residence column row 4).

Producer-site lift at composers is DEFERRED at v2.42 scope per Q2=narrow
ratification (~13 sites across CP / runtime / AS). The ``make_procedural_tier_snapshot_resolver``
factory is authored at v2.42 for future producer-site lift arcs per CP spec
v1.25 §16.5.7 + §16.5.8 ``ledger_writer`` kw-only-callable-bound-at-runtime-wiring
precedent.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import TYPE_CHECKING

from harness_is.state_ledger_entry_schema import Identifier

if TYPE_CHECKING:
    from harness_runtime.types import HarnessContext


__all__ = [
    "make_procedural_tier_snapshot_resolver",
    "resolve_procedural_tier_snapshot",
]


def _canonicalize_procedural_tier_payload(
    active_prompt_version: str,
    active_skills_versions: list[str],
    prompt_selection_manifest_sha: str,
    routing_manifest_sha: str,
) -> bytes:
    """Canonicalize the procedural-tier payload to bytes for hashing.

    Internal helper exposed for testing per U-RT-112 AC #3 (alphabetical key
    ordering) + AC #4 (skills-versions list canonicalization).

    The payload dict is built with the four v1.9 components ordered
    alphabetically by key (``active_prompt_version`` NEW at IS spec v1.5 §5.2;
    ``prompt_selection_manifest_sha`` NEW at IS spec v1.9 §5.2 — R-FS-1 arc B4
    per-role prompt coherence); ``json.dumps`` is invoked with ``sort_keys=True``
    + ``separators=(",", ":")`` to produce a deterministic canonical byte form
    independent of Python dict insertion order.
    """
    payload = {
        "active_prompt_version": active_prompt_version,
        "active_skills_versions": active_skills_versions,
        "prompt_selection_manifest_sha": prompt_selection_manifest_sha,
        "routing_manifest_sha": routing_manifest_sha,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
        "utf-8",
    )


def resolve_procedural_tier_snapshot(
    harness_context: HarnessContext,
) -> Identifier:
    """Compute the procedural-tier snapshot content-hash for ``harness_context``.

    Pure function per U-RT-112 AC #1 + AC #8 (direct-compute discipline; no
    module-level state; same input yields identical output across calls). No
    ``harness_context`` mutation per AC #9.

    Returns a lowercase 64-character hex SHA-256 digest per AC #2 (content-hash
    recipe byte-exact). Four-component scope at v1.9: the prompts component
    (``active_prompt_version``) was deferred at v1.3 per spec §5.2 Deferral
    footer, bound at v1.5 (post-MVP closure R-CL-P4) — read from
    ``harness_context.prompt_manifest.active_prompt_version.version_sha`` — and the
    ``prompt_selection_manifest_sha`` component is bound at v1.9 (R-FS-1 arc B4),
    read from ``harness_context.config.prompt_selection_manifest`` (``""`` when
    ``None`` — no dedicated carrier; the config field is its spec'd home).
    """
    # 0. Extract the active prompt version (NEW v1.5 third component) from the
    #    PromptManifest carrier on harness_context. Empty-defaultable
    #    (`version_sha=""` when no active prompt); mirrors routing_manifest as
    #    an operator-supplied frozen carrier read at write-time per spec §5.2.
    active_prompt_version = harness_context.prompt_manifest.active_prompt_version.version_sha

    # 1. Extract active skills versions from harness_context.skills.
    #    Each Skill's version_sha lives at skill.manifest.version_sha per
    #    harness-runtime/lifecycle/skills.py:60 (NEW at U-RT-99 / v1.32).
    #    ``HarnessContext.skills`` is typed as ``dict[SkillID, Skill]`` where
    # 2. Sort ascending lexicographic + dedup per AC #4.
    active_skills_versions = sorted(
        {skill.manifest.version_sha for skill in harness_context.skills.values()},
    )

    # 3. Derive routing_manifest_sha via canonical-JSON-bytes SHA-256 per
    #    AC #12 (RoutingManifest exposes no .sha method at HEAD; canonicalize-
    #    at-resolver per spec §5.2 implementer-discretion footer).
    #
    #    IMPORTANT: ``model_dump_json`` preserves Pydantic v2's dict insertion
    #    order, which is NOT deterministic across logically-identical instances
    #    constructed with different mapping insertion orders. To honor AC #6's
    #    cross-instance determinism guarantee, canonicalize via ``model_dump``
    #    + ``json.dumps(sort_keys=True)``. Empirical verification at adversarial
    #    review of PR #89 (2026-05-30) confirmed insertion-order-dependence;
    #    sort_keys=True closes the determinism gap.
    routing_manifest_bytes = json.dumps(
        harness_context.routing_manifest.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    routing_manifest_sha = hashlib.sha256(routing_manifest_bytes).hexdigest()

    # 3b. Derive prompt_selection_manifest_sha (NEW v1.9 — IS spec §5.2 4th
    #     component; R-FS-1 arc B4 per-role prompt coherence). Mirrors
    #     routing_manifest_sha exactly: SHA-256 over the WHOLE
    #     PromptSelectionManifest canonical-JSON bytes (model_dump(mode="json")
    #     + sort_keys for cross-instance determinism, per the routing rationale
    #     above). This makes per-role prompt-selection bindings hash-visible — a
    #     `per_role_bindings` flip changes a branch's injected content (B4
    #     §14.5.3) AND this hash, closing the §14.5.2 invariant for the per-role
    #     dimension. Read from the operator-supplied RuntimeConfig (the selection
    #     manifest's spec'd home — `RuntimeConfig.prompt_selection_manifest`, NOT
    #     stage-enriched, so no dedicated HarnessContext carrier / C-RT-04 row is
    #     owed, unlike the reconciled `prompt_manifest`/`routing_manifest`).
    #     `None` (no selection manifest) → "" sentinel. The v1.9 widening rebases
    #     the hash forward for ALL runs (the documented forward-only rebase); a
    #     None-selection run is byte-identical to any other None-selection run.
    selection_manifest = harness_context.config.prompt_selection_manifest
    if selection_manifest is None:
        prompt_selection_manifest_sha = ""
    else:
        prompt_selection_manifest_bytes = json.dumps(
            selection_manifest.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        prompt_selection_manifest_sha = hashlib.sha256(prompt_selection_manifest_bytes).hexdigest()

    # 4 + 5. Build canonical payload + serialize via helper (alphabetical key
    #        ordering + sort_keys + compact separators per AC #2 + #3).
    canonical_bytes = _canonicalize_procedural_tier_payload(
        active_prompt_version=active_prompt_version,
        active_skills_versions=active_skills_versions,
        prompt_selection_manifest_sha=prompt_selection_manifest_sha,
        routing_manifest_sha=routing_manifest_sha,
    )

    # 6. Return SHA-256 hex digest as Identifier (str alias per AC #7).
    return Identifier(hashlib.sha256(canonical_bytes).hexdigest())


def make_procedural_tier_snapshot_resolver(
    harness_context: HarnessContext,
) -> Callable[[], Identifier]:
    """Build a zero-arg resolver closure capturing ``harness_context``.

    For engine-layer composers without ``HarnessContext`` access at firing time
    per CP spec v1.25 §16.5.7 + §16.5.8 ``ledger_writer`` kw-only-callable
    precedent. The returned closure re-computes from the captured ctx at every
    call (no caching at v2.42 per AC #8 direct-compute discipline; same-input
    memoization is implementer-discretion at consumer-site).

    Producer-site lift at composers is DEFERRED at v2.42 scope per Q2=narrow;
    this factory is authored at v2.42 for future per-axis cascade arcs.
    """

    def _resolve() -> Identifier:
        return resolve_procedural_tier_snapshot(harness_context)

    return _resolve
