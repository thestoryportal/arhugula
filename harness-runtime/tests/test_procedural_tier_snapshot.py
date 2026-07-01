"""Tests for U-RT-112 — procedural-tier snapshot resolver primitive.

Implements IS spec v1.3 §C-IS-05 §5.2 resolver contract; residence pinned
at harness-runtime per Q-γ=(γ-2) operator ratification 2026-05-30.

14 tests cover U-RT-112 ACs #1-#14 per Implementation_Plan_Harness_Runtime_v2_42.md
§1 acceptance criteria. Uses minimal duck-typed context fixture; ZERO bootstrap
or full HarnessContext construction (mirror pattern at
test_lifecycle_skill_activation.py _PartialCtx idiom).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from harness_core import SkillID
from harness_cp.cp_shared_types import AgentRole
from harness_cp.prompt_selection_manifest import PromptBinding, PromptSelectionManifest
from harness_cp.routing_manifest_residence import RoutingManifest
from harness_is.prompt_manifest import PromptManifest, PromptVersion
from harness_runtime.lifecycle.procedural_tier_snapshot import (
    _canonicalize_procedural_tier_payload,
    make_procedural_tier_snapshot_resolver,
    resolve_procedural_tier_snapshot,
)
from harness_runtime.lifecycle.skills import Skill, SkillManifest


def _skill(skill_id: str, version_sha: str = "v-sha-default") -> Skill:
    """Construct a minimal Skill at-rest carrier."""
    manifest = SkillManifest(
        skill_id=SkillID(skill_id),
        name=f"name-{skill_id}",
        description=f"desc-{skill_id}",
        version="1.0",
        version_sha=version_sha,
        body_tokens=1,
    )
    return Skill(manifest=manifest, source_path=Path("/dev/null"))


def _routing_manifest(manifest_version: int = 1) -> RoutingManifest:
    """Minimal RoutingManifest fixture."""
    return RoutingManifest(
        manifest_version=manifest_version,
        per_role_bindings={},
        per_workload_overrides={},
        fallback_chains=(),
        retry_policies={},
    )


def _prompt_manifest(content: str = "") -> PromptManifest:
    """Minimal PromptManifest fixture (empty-defaultable; ``content=""`` →
    ``version_sha=""`` → no active prompt, the empty-carrier default).

    R-PM-1 PR #1 — the ``version_sha`` is content-derived (``from_content``);
    distinct content → distinct sha → distinct snapshot (the test intent)."""
    return PromptManifest(
        manifest_version=1,
        active_prompt_version=PromptVersion.from_content(content),
    )


def _ctx(
    skills: dict[SkillID, Skill] | None = None,
    routing_manifest: RoutingManifest | None = None,
    prompt_manifest: PromptManifest | None = None,
    prompt_selection_manifest: PromptSelectionManifest | None = None,
) -> SimpleNamespace:
    """Build a minimal duck-typed HarnessContext-shape exposing just the
    fields the resolver reads (``skills`` + ``routing_manifest`` +
    ``prompt_manifest`` + ``config.prompt_selection_manifest`` — the NEW v1.9
    4th recipe component, read from config (its spec'd home), default ``None``
    → ``""`` sentinel)."""
    return SimpleNamespace(
        skills=skills if skills is not None else {},
        routing_manifest=routing_manifest if routing_manifest is not None else _routing_manifest(),
        prompt_manifest=prompt_manifest if prompt_manifest is not None else _prompt_manifest(),
        config=SimpleNamespace(prompt_selection_manifest=prompt_selection_manifest),
    )


# ---------------------------------------------------------------------------
# AC #2 — content-hash recipe byte-exact (64-char lowercase hex SHA-256).
# ---------------------------------------------------------------------------


def test_resolve_returns_64_char_lowercase_hex() -> None:
    """AC #2: Output is 64-char lowercase hex SHA-256."""
    result = resolve_procedural_tier_snapshot(_ctx())  # type: ignore[arg-type]
    assert len(result) == 64
    assert result == result.lower()
    assert all(c in "0123456789abcdef" for c in result)


# ---------------------------------------------------------------------------
# AC #3 — alphabetical key ordering (3 components at v1.5).
# ---------------------------------------------------------------------------


def test_resolve_canonical_payload_alphabetical_keys_4_components_at_v1_9() -> None:
    """AC #3: Canonical payload has 4 keys alphabetically ordered (v1.9 —
    ``prompt_selection_manifest_sha`` NEW, R-FS-1 arc B4 per-role coherence)."""
    payload_bytes = _canonicalize_procedural_tier_payload(
        active_prompt_version="p" * 64,
        active_skills_versions=["a"],
        prompt_selection_manifest_sha="c" * 64,
        routing_manifest_sha="b" * 64,
    )
    payload = json.loads(payload_bytes.decode("utf-8"))
    assert list(payload.keys()) == [
        "active_prompt_version",
        "active_skills_versions",
        "prompt_selection_manifest_sha",
        "routing_manifest_sha",
    ]


# ---------------------------------------------------------------------------
# AC #4 — skills-versions list canonicalization.
# ---------------------------------------------------------------------------


def test_resolve_skills_versions_sorted_ascending() -> None:
    """AC #4: ``active_skills_versions`` sorted ascending."""
    skill_a = _skill("skill-a", version_sha="v-sha-z")
    skill_b = _skill("skill-b", version_sha="v-sha-a")
    ctx = _ctx(skills={SkillID("a"): skill_a, SkillID("b"): skill_b})
    # Construct an oracle by manually invoking the same recipe in sorted order.
    # Use the canonical-JSON derivation per PR #89 Finding A fix (2026-05-30).
    expected_versions = sorted(["v-sha-z", "v-sha-a"])
    routing_sha = hashlib.sha256(
        json.dumps(
            ctx.routing_manifest.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
    ).hexdigest()
    oracle = hashlib.sha256(
        _canonicalize_procedural_tier_payload(
            active_prompt_version="",
            active_skills_versions=expected_versions,
            prompt_selection_manifest_sha="",
            routing_manifest_sha=routing_sha,
        ),
    ).hexdigest()
    assert resolve_procedural_tier_snapshot(ctx) == oracle  # type: ignore[arg-type]


def test_resolve_skills_versions_dedup_before_serialize() -> None:
    """AC #4: duplicate ``version_sha`` values deduplicated before serialization."""
    skill_a = _skill("skill-a", version_sha="v-shared")
    skill_b = _skill("skill-b", version_sha="v-shared")
    ctx_dup = _ctx(skills={SkillID("a"): skill_a, SkillID("b"): skill_b})
    skill_single = _skill("skill-c", version_sha="v-shared")
    ctx_single = _ctx(skills={SkillID("c"): skill_single})
    assert resolve_procedural_tier_snapshot(ctx_dup) == resolve_procedural_tier_snapshot(  # type: ignore[arg-type]
        ctx_single,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# AC #5 + #6 — input-differential discipline.
# ---------------------------------------------------------------------------


def test_resolve_different_skills_set_different_hash() -> None:
    """AC #5: differing skills sets produce different hashes."""
    ctx_a = _ctx(skills={SkillID("x"): _skill("x", version_sha="v-1")})
    ctx_b = _ctx(skills={SkillID("y"): _skill("y", version_sha="v-2")})
    assert resolve_procedural_tier_snapshot(ctx_a) != resolve_procedural_tier_snapshot(  # type: ignore[arg-type]
        ctx_b,  # type: ignore[arg-type]
    )


def test_resolve_different_routing_manifest_different_hash() -> None:
    """AC #5: differing routing manifests produce different hashes."""
    ctx_a = _ctx(routing_manifest=_routing_manifest(manifest_version=1))
    ctx_b = _ctx(routing_manifest=_routing_manifest(manifest_version=2))
    assert resolve_procedural_tier_snapshot(ctx_a) != resolve_procedural_tier_snapshot(  # type: ignore[arg-type]
        ctx_b,  # type: ignore[arg-type]
    )


def test_resolve_different_prompt_version_different_hash() -> None:
    """AC #5 (v1.5): the 3rd component PARTICIPATES — differing
    ``active_prompt_version`` produces different hashes (proves the prompts
    component is wired into the recipe, not merely declared)."""
    ctx_a = _ctx(prompt_manifest=_prompt_manifest(content="prompt-v-1"))
    ctx_b = _ctx(prompt_manifest=_prompt_manifest(content="prompt-v-2"))
    assert resolve_procedural_tier_snapshot(ctx_a) != resolve_procedural_tier_snapshot(  # type: ignore[arg-type]
        ctx_b,  # type: ignore[arg-type]
    )


def test_resolve_same_prompt_version_same_hash() -> None:
    """AC #6 (v1.5): identical ``active_prompt_version`` ⇒ identical hash
    (the prompts component holds the snapshot constant when unchanged)."""
    ctx_a = _ctx(prompt_manifest=_prompt_manifest(content="prompt-v-stable"))
    ctx_b = _ctx(prompt_manifest=_prompt_manifest(content="prompt-v-stable"))
    assert resolve_procedural_tier_snapshot(ctx_a) == resolve_procedural_tier_snapshot(  # type: ignore[arg-type]
        ctx_b,  # type: ignore[arg-type]
    )


def test_resolve_same_state_same_hash_across_calls() -> None:
    """AC #6: identical state ⇒ identical hash (cross-instance + cross-call determinism)."""
    skill = _skill("k", version_sha="v-determ")
    ctx_a = _ctx(skills={SkillID("k"): skill})
    ctx_b = _ctx(skills={SkillID("k"): skill})
    result_a1 = resolve_procedural_tier_snapshot(ctx_a)  # type: ignore[arg-type]
    result_a2 = resolve_procedural_tier_snapshot(ctx_a)  # type: ignore[arg-type]
    result_b = resolve_procedural_tier_snapshot(ctx_b)  # type: ignore[arg-type]
    assert result_a1 == result_a2 == result_b


# ---------------------------------------------------------------------------
# AC #7 — return type ``Identifier`` (str alias).
# ---------------------------------------------------------------------------


def test_resolve_return_type_is_identifier_alias() -> None:
    """AC #7: return value is an Identifier (str subtype at runtime)."""
    result = resolve_procedural_tier_snapshot(_ctx())  # type: ignore[arg-type]
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# AC #1 + #8 — pure-function discipline (no side effects; no module state).
# ---------------------------------------------------------------------------


def test_resolve_pure_function_no_side_effects() -> None:
    """AC #1 + #8: no global state; deterministic for fixed input."""
    ctx = _ctx()
    snapshot_1 = resolve_procedural_tier_snapshot(ctx)  # type: ignore[arg-type]
    # Invoke an unrelated function that has no shared state.
    _ = resolve_procedural_tier_snapshot(_ctx(skills={SkillID("x"): _skill("x")}))  # type: ignore[arg-type]
    snapshot_2 = resolve_procedural_tier_snapshot(ctx)  # type: ignore[arg-type]
    assert snapshot_1 == snapshot_2


# ---------------------------------------------------------------------------
# AC #9 — no HarnessContext mutation.
# ---------------------------------------------------------------------------


def test_resolve_no_harness_context_mutation() -> None:
    """AC #9: resolver does not mutate ctx.skills or ctx.routing_manifest."""
    skills_before = {SkillID("a"): _skill("a", version_sha="v-1")}
    skills_snapshot = dict(skills_before)
    routing_before = _routing_manifest(manifest_version=42)
    ctx = _ctx(skills=skills_before, routing_manifest=routing_before)
    resolve_procedural_tier_snapshot(ctx)  # type: ignore[arg-type]
    assert ctx.skills == skills_snapshot
    assert ctx.routing_manifest == routing_before


# ---------------------------------------------------------------------------
# AC #10 — empty skills set handled.
# ---------------------------------------------------------------------------


def test_resolve_empty_skills_set_handled() -> None:
    """AC #10: empty ``ctx.skills`` produces a deterministic non-error hash."""
    result_a = resolve_procedural_tier_snapshot(_ctx(skills={}))  # type: ignore[arg-type]
    result_b = resolve_procedural_tier_snapshot(_ctx(skills={}))  # type: ignore[arg-type]
    assert result_a == result_b
    assert len(result_a) == 64


# ---------------------------------------------------------------------------
# AC #11 (v1.5) — prompts component bound (was deferred at v1.3).
# ---------------------------------------------------------------------------


def test_resolve_canonical_payload_includes_prompts_key_at_v1_5() -> None:
    """AC #11: canonical payload contains exactly 4 keys (v1.9); prompts bound +
    prompt_selection_manifest_sha present (R-FS-1 arc B4)."""
    payload_bytes = _canonicalize_procedural_tier_payload(
        active_prompt_version="",
        active_skills_versions=[],
        prompt_selection_manifest_sha="",
        routing_manifest_sha="0" * 64,
    )
    payload = json.loads(payload_bytes.decode("utf-8"))
    assert set(payload.keys()) == {
        "active_prompt_version",
        "active_skills_versions",
        "prompt_selection_manifest_sha",
        "routing_manifest_sha",
    }
    assert len(payload) == 4


# ---------------------------------------------------------------------------
# AC #12 — RoutingManifest sha derivation byte-exact via model_dump_json.
# ---------------------------------------------------------------------------


def test_resolve_routing_manifest_sha_derivation_byte_exact() -> None:
    """AC #12: routing_manifest_sha derivation matches the documented recipe.

    Post-PR-#89 adversarial-review fix: derivation uses ``json.dumps`` over
    ``model_dump(mode="json")`` with ``sort_keys=True`` + compact separators
    to guarantee cross-instance determinism per AC #6 (Pydantic v2's
    ``model_dump_json`` preserves dict insertion order, which is non-canonical).
    """
    rm = _routing_manifest(manifest_version=7)
    ctx = _ctx(routing_manifest=rm)
    expected_routing_sha = hashlib.sha256(
        json.dumps(
            rm.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
    ).hexdigest()
    expected_payload = _canonicalize_procedural_tier_payload(
        active_prompt_version="",
        active_skills_versions=[],
        prompt_selection_manifest_sha="",
        routing_manifest_sha=expected_routing_sha,
    )
    expected_hash = hashlib.sha256(expected_payload).hexdigest()
    assert resolve_procedural_tier_snapshot(ctx) == expected_hash  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Discriminator for AC #6 cross-instance determinism — RoutingManifest dict
# insertion order MUST NOT affect resolver output (Finding A fix from PR #89
# adversarial review, 2026-05-30).
# ---------------------------------------------------------------------------


def test_resolve_routing_manifest_sha_invariant_under_dict_insertion_order() -> None:
    """AC #6 cross-instance determinism: two logically-identical RoutingManifests
    constructed with different mapping insertion orders MUST produce the same
    snapshot hash. Pre-Finding-A fix this test would FAIL because Pydantic v2's
    ``model_dump_json`` preserves dict insertion order; post-fix the
    ``sort_keys=True`` canonicalization closes the gap.
    """
    from harness_cp.cp_shared_types import ModelBinding
    from harness_cp.routing_manifest_residence import RoleRoutingBinding

    mb_a = ModelBinding(provider="anthropic", model="claude-opus-4-7")
    mb_b = ModelBinding(provider="openai", model="gpt-5")
    rb_a = RoleRoutingBinding(
        preferred_model_binding=mb_a,
        layer_budget_overrides={},
    )
    rb_b = RoleRoutingBinding(
        preferred_model_binding=mb_b,
        layer_budget_overrides={},
    )
    # Two logically-identical RoutingManifests, different mapping insertion order.
    rm_order_1 = RoutingManifest(
        manifest_version=1,
        per_role_bindings={
            AgentRole("orchestrator"): rb_a,
            AgentRole("worker"): rb_b,
        },
        per_workload_overrides={},
        fallback_chains=(),
        retry_policies={},
    )
    rm_order_2 = RoutingManifest(
        manifest_version=1,
        per_role_bindings={
            AgentRole("worker"): rb_b,
            AgentRole("orchestrator"): rb_a,
        },
        per_workload_overrides={},
        fallback_chains=(),
        retry_policies={},
    )
    ctx_1 = _ctx(routing_manifest=rm_order_1)
    ctx_2 = _ctx(routing_manifest=rm_order_2)
    snapshot_1 = resolve_procedural_tier_snapshot(ctx_1)  # type: ignore[arg-type]
    snapshot_2 = resolve_procedural_tier_snapshot(ctx_2)  # type: ignore[arg-type]
    assert snapshot_1 == snapshot_2, (
        f"Resolver MUST be invariant under RoutingManifest dict insertion order "
        f"per AC #6 cross-instance determinism; got {snapshot_1!r} vs {snapshot_2!r}"
    )


# ---------------------------------------------------------------------------
# AC #13 — factory function shape.
# ---------------------------------------------------------------------------


def test_make_resolver_factory_returns_callable_capturing_ctx() -> None:
    """AC #13: ``make_procedural_tier_snapshot_resolver`` returns a zero-arg
    callable that re-computes from the captured ctx state at each call."""
    ctx = _ctx(skills={SkillID("a"): _skill("a", version_sha="v-1")})
    closure = make_procedural_tier_snapshot_resolver(ctx)  # type: ignore[arg-type]
    direct = resolve_procedural_tier_snapshot(ctx)  # type: ignore[arg-type]
    assert callable(closure)
    assert closure() == direct
    # Closure is idempotent across calls (re-compute from captured ctx).
    assert closure() == closure()


# ---------------------------------------------------------------------------
# v1.9 (R-FS-1 arc B4) — prompt_selection_manifest_sha 4th component: per-role
# prompt-selection bindings are now hash-visible, closing the §14.5.2 coherence
# gap that per-role prompt injection would otherwise reintroduce.
# ---------------------------------------------------------------------------


def _selection_manifest(role: str, version_sha: str) -> PromptSelectionManifest:
    """Minimal PromptSelectionManifest with a single per-role binding."""
    return PromptSelectionManifest(
        manifest_version=1,
        per_role_bindings={AgentRole(role): PromptBinding(version_sha=version_sha)},
    )


def test_resolve_selection_manifest_present_differs_from_none() -> None:
    """v1.9: a configured prompt-selection manifest changes the procedural-tier
    hash vs no selection manifest — per-role bindings are now hash-visible
    (mirroring how the routing manifest's per-role bindings already were)."""
    none_ctx = _ctx(prompt_selection_manifest=None)
    sel_ctx = _ctx(prompt_selection_manifest=_selection_manifest("researcher", "a" * 64))
    assert resolve_procedural_tier_snapshot(none_ctx) != resolve_procedural_tier_snapshot(  # type: ignore[arg-type]
        sel_ctx  # type: ignore[arg-type]
    )


def test_resolve_per_role_binding_flip_changes_hash() -> None:
    """v1.9 — THE load-bearing coherence property: flipping a per_role_bindings
    entry's version_sha (which changes that branch's injected per-role prompt
    content at dispatch, B4 §14.5.3) MUST change the procedural-tier hash. This is
    exactly the §14.5.2 invariant the v1.8 3-component recipe violated for the
    per-role dimension (the gap this arc closes)."""
    ctx_a = _ctx(prompt_selection_manifest=_selection_manifest("researcher", "a" * 64))
    ctx_b = _ctx(prompt_selection_manifest=_selection_manifest("researcher", "b" * 64))
    assert resolve_procedural_tier_snapshot(ctx_a) != resolve_procedural_tier_snapshot(  # type: ignore[arg-type]
        ctx_b  # type: ignore[arg-type]
    )


def test_resolve_same_selection_manifest_same_hash() -> None:
    """v1.9: two logically-identical selection manifests → identical hash
    (determinism, mirroring the active_prompt_version / routing components)."""
    ctx_a = _ctx(prompt_selection_manifest=_selection_manifest("writer", "c" * 64))
    ctx_b = _ctx(prompt_selection_manifest=_selection_manifest("writer", "c" * 64))
    assert resolve_procedural_tier_snapshot(ctx_a) == resolve_procedural_tier_snapshot(  # type: ignore[arg-type]
        ctx_b  # type: ignore[arg-type]
    )
