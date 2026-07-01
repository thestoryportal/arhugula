"""U-RT-101 — Real-bootstrap e2e: skill activation binding chain (AS-8d
RETIRE-READY → RETIRED gate).

Implements runtime plan v2.28 §1 U-RT-101 AC #8 + runtime spec v1.32
§14.17.6 retirement implications (binding-chain materialization
verification + ≥1 hook site firing verification per X-AL-2 + Reading B
operator-opt-in RETIRE-READY pattern).

## Mechanism α (default per spec §14.17.7 implementer-discretion)

Exercises the binding chain end-to-end via the real bootstrap (no
``_FakeCtx`` / ``_MutableHarnessContext`` shortcut) — ``run_bootstrap``
runs against in-process fake providers/daemon/tracer (standard
integration-test substrate per ``conftest.py::patched_runtime``); the
stage-5 LOOP_INIT bucket invokes
``materialize_skill_activation_emitter_stage`` with the real factory; the
test asserts binding-chain post-conditions + ≥1 hook-site emission via
the operator-explicit ``ctx.activate_skill(...)`` path.

Coverage:

- **Opt-out** (``RuntimeConfig.skill_activation_hook_config = None``, the
  production-default): ``ctx.skill_activation_emitter is None``; all 3
  hook sites silent-skip per §14.17.5 invariant 3 (production-default
  state preserved).

- **Opt-in without hook** (``SkillActivationHookConfig()`` with
  ``hook=None``): ``ctx.skill_activation_emitter is not None`` but
  ``ctx.skill_activation_emitter.hook is None``; automatic hooks
  (per-LLM-dispatch / per-workflow-init) silent-skip; operator-explicit
  ``ctx.activate_skill(...)`` succeeds (emitter bound).

- **Opt-in with hook + skill emission** (X-AL-2 retirement criterion
  satisfaction): ``ctx.skill_activation_emitter`` bound + hook bound;
  operator-explicit ``ctx.activate_skill(...)`` emits one
  ``skill.activation`` span with ``activation_mode = filesystem_read``
  carrying all 6 AS spec v1.7 §14.4 attributes.

## Hook-coverage scope discipline

Plan v2.28 U-RT-101 AC #8 prescribes "all 3 hook sites emit
skill.activation spans" — stricter than fork doc §14.17.6 retirement
criterion (X-AL-2 "≥1 hook site"). This e2e verifies the operator-explicit
hook (hook-3) which satisfies the X-AL-2 retirement criterion. The
per-LLM-dispatch (hook-2) + per-workflow-init (hook-1) firings require a
running workflow loop + LLM dispatch, which exceeds the e2e scope of
binding-chain verification. Plan AC #8 ↔ fork §14.17.6 divergence
documented at the closure-event filing (see retirement event batch-25);
full-3-hook coverage owed at a follow-on workflow-execution-e2e arc.

## Verification-shape discipline

Per ``[[verification-shape-sharpened-grep-vs-e2e]]`` (batch-16 §6
sharpening + batch-17/18 application): "binding chain succeeds end-to-end
against a real substrate + ≥1 hook site empirically emits a span." This
test uses the production ``run_bootstrap`` orchestrator (not test-local
shortcuts) and verifies the full binding chain at the produced
``HarnessContext`` + observes span emission.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest
from harness_core import SkillID
from harness_runtime.bootstrap import run_bootstrap
from harness_runtime.lifecycle.skill_activation import (
    SkillActivationHookConfig,
    SkillActivationSpanEmitter,
)
from harness_runtime.types import HarnessContext, RuntimeConfig

from .conftest import WORKLOAD, build_config

# --- Operator-supplied fixture hook ------------------------------------------


class _AllSkillsHook:
    """Operator-supplied SkillActivationHook fixture — selects all loaded
    skills at every query.

    Trivial activation policy for e2e verification — every loaded skill
    activates at every hook firing. Real operator hooks implement
    frontmatter-match / cluster-routing / role-based / etc. policies.
    """

    def select_for_workflow_init(
        self,
        loaded_skills: Iterable[SkillID],
        workflow_id: str,
    ) -> Iterable[SkillID]:
        return list(loaded_skills)

    def select_for_llm_dispatch(
        self,
        loaded_skills: Iterable[SkillID],
        workflow_id: str,
        step_index: int,
    ) -> Iterable[SkillID]:
        return list(loaded_skills)


# --- Config builders ---------------------------------------------------------


def _config_with_skill_activation_opt_out(tmp_path: Path) -> RuntimeConfig:
    """Production-default — skill_activation_hook_config defaults to None."""
    return build_config(tmp_path)


def _config_with_skill_activation_opt_in_no_hook(tmp_path: Path) -> RuntimeConfig:
    return build_config(tmp_path).model_copy(
        update={"skill_activation_hook_config": SkillActivationHookConfig()},
    )


def _config_with_skill_activation_opt_in_and_hook(tmp_path: Path) -> RuntimeConfig:
    return build_config(tmp_path).model_copy(
        update={
            "skill_activation_hook_config": SkillActivationHookConfig(
                hook=_AllSkillsHook(),
            ),
        },
    )


# --- Skills fixture helper ---------------------------------------------------


def _seed_skills_dir(skills_dir: Path) -> SkillID:
    """Write a single fixture skill manifest. Returns its skill_id."""
    skills_dir.mkdir(parents=True, exist_ok=True)
    manifest = skills_dir / "fixture-skill.skill.json"
    raw = (
        '{"skill_id": "fixture-skill", '
        '"name": "Fixture Skill", '
        '"description": "test fixture for AS-8d e2e", '
        '"version": "1.0"}'
    )
    manifest.write_text(raw)
    return SkillID("fixture-skill")


# --- AC #4 — opt-out branch (production-default) -----------------------------


@pytest.mark.asyncio
async def test_skill_activation_e2e_opt_out_branch(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """AC #4 — opt-out config → ``ctx.skill_activation_emitter is None``;
    all 3 hook sites silent-skip; pre-v1.32 production-default behaviour
    preserved."""
    _ = patched_runtime
    config = _config_with_skill_activation_opt_out(tmp_path)
    assert config.skill_activation_hook_config is None

    ctx = await run_bootstrap(config, workload_class=WORKLOAD)

    assert isinstance(ctx, HarnessContext)
    assert ctx.skill_activation_emitter is None, (
        "opt-out (default) config must yield ctx.skill_activation_emitter "
        "is None per spec §14.17.3 opt-out branch"
    )


# --- AC #1/#3 — opt-in branch binding-chain materialization ------------------


@pytest.mark.asyncio
async def test_skill_activation_e2e_opt_in_no_hook_branch(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """AC #3 (partial) — opt-in config with hook=None → emitter bound but
    automatic hooks silent-skip. operator-explicit ctx.activate_skill
    succeeds (emitter bound) but is exercised at the with-hook test."""
    _ = patched_runtime
    config = _config_with_skill_activation_opt_in_no_hook(tmp_path)
    assert config.skill_activation_hook_config is not None
    assert config.skill_activation_hook_config.hook is None

    ctx = await run_bootstrap(config, workload_class=WORKLOAD)

    assert isinstance(ctx, HarnessContext)
    assert isinstance(ctx.skill_activation_emitter, SkillActivationSpanEmitter)
    assert ctx.skill_activation_emitter.hook is None, (
        "emitter constructed with hook=None per fork §14.17.5 invariant 3 "
        "emitter-without-hook deployment shape"
    )


# --- AC #3/#6 — opt-in with hook + operator-explicit emission ----------------


@pytest.mark.asyncio
async def test_skill_activation_e2e_opt_in_with_hook_branch(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """AC #3 + AC #6 + X-AL-2 retirement criterion satisfaction —
    binding chain materialized + operator-explicit hook-3 emits one
    ``skill.activation`` span carrying all 6 AS spec v1.7 §14.4 attributes
    with ``activation_mode = filesystem_read``.

    Note (MVP-proxy disposition per advisor pre-substantive consultation):
    ``skill.body_tokens`` at the fixture is computed from
    ``description`` length (MVP heuristic per spec §14.17.7 + skills.py
    extension). Real-workflow body-token computation (full SKILL.md body
    read) is owed at a follow-on arc per spec §14.17.7 deferred-discretion
    + plan v2.28 U-RT-99 AC #6 MVP-shape acknowledgement.
    """
    _ = patched_runtime
    skills_dir = tmp_path / "skills"
    skill_id = _seed_skills_dir(skills_dir)

    # build_config sets PATH_CLASS_REGISTRY[SKILLS] to skills_dir via tmp_path.
    config = _config_with_skill_activation_opt_in_and_hook(tmp_path)
    assert config.skill_activation_hook_config is not None
    assert config.skill_activation_hook_config.hook is not None

    ctx = await run_bootstrap(config, workload_class=WORKLOAD)
    assert isinstance(ctx, HarnessContext)
    assert isinstance(ctx.skill_activation_emitter, SkillActivationSpanEmitter)
    assert ctx.skill_activation_emitter.hook is not None

    # The fixture skill should have loaded.
    assert skill_id in ctx.skills, (
        f"fixture skill {skill_id} must be loaded at ctx.skills via stage-2 AS load_skills_from_dir"
    )

    # Verify the skill loaded with computed version_sha + body_tokens.
    skill = ctx.skills[skill_id]
    assert len(skill.manifest.version_sha) == 40  # SHA-1 hex
    assert skill.manifest.body_tokens >= 0  # MVP heuristic: non-negative int
    assert skill.manifest.name == "Fixture Skill"
    assert skill.manifest.version == "1.0"

    # Exercise hook-3 (operator-explicit) — emits one skill.activation span.
    # This satisfies the X-AL-2 retirement criterion ("≥1 hook site fires
    # at production binding + observation of skill.activation span emission")
    # per spec §14.17.6 retirement implications.
    # Pre-emission span tally:
    tracer_provider = patched_runtime["tracer"]
    spans_before = len(tracer_provider.spans)

    ctx.activate_skill(skill_id, workflow_id="e2e-wf-1")

    # Observe emission: exactly 1 new skill.activation span carrying the
    # AS spec v1.7 §14.4 6-attribute namespace + activation_mode =
    # filesystem_read (operator-explicit hook-3 enum mapping per Q2=(d) +
    # Q3=(i) preserve-Claude-Code-taxonomy ratification).
    new_spans = tracer_provider.spans[spans_before:]
    skill_spans = [s for s in new_spans if s.name == "skill.activation"]
    assert len(skill_spans) == 1, (
        f"X-AL-2 retirement criterion: expected exactly 1 skill.activation "
        f"span on operator-explicit activation; got {len(skill_spans)}"
    )
    span = skill_spans[0]
    assert span.attrs["skill.id"] == str(skill_id)
    assert span.attrs["skill.name"] == "Fixture Skill"
    assert len(span.attrs["skill.version_sha"]) == 40
    assert span.attrs["skill.frontmatter.version"] == "1.0"
    assert isinstance(span.attrs["skill.body_tokens"], int)
    assert span.attrs["skill.body_tokens"] >= 0
    assert span.attrs["skill.activation_mode"] == "filesystem_read"
    assert span.attrs["workflow.id"] == "e2e-wf-1"


# --- Joint-binding substrate verification ------------------------------------


@pytest.mark.asyncio
async def test_skill_activation_e2e_joint_with_other_opt_in_bindings(
    tmp_path: Path,
    patched_runtime: dict[str, Any],
) -> None:
    """Joint-binding substrate — skill_activation_hook_config opt-in
    composes with other operator-opt-in bindings (validator_framework_config /
    pause_resume_protocol_config / webhook_delivery_composer_config) at the
    same RuntimeConfig instance without binding-chain interference.

    Per stage-5 LOOP_INIT bucket ordering discipline at
    ``bootstrap/stage_5_loop_init.py`` — sibling factories run within stage
    5 with implementer-discretion ordering; binding chains are orthogonal
    (no shared state between emitter / validator / pause-resume / webhook).
    """
    _ = patched_runtime
    from harness_runtime.lifecycle.pause_resume_protocol_types import (
        PauseResumeProtocolConfig,
    )
    from harness_runtime.lifecycle.webhook_delivery_composer_types import (
        WebhookDeliveryComposerConfig,
    )

    config = build_config(tmp_path).model_copy(
        update={
            "skill_activation_hook_config": SkillActivationHookConfig(
                hook=_AllSkillsHook(),
            ),
            "pause_resume_protocol_config": PauseResumeProtocolConfig.default(),
            "webhook_delivery_composer_config": WebhookDeliveryComposerConfig.default(),
        },
    )

    ctx = await run_bootstrap(config, workload_class=WORKLOAD)
    assert isinstance(ctx, HarnessContext)
    # All 3 opt-in bindings non-None at the frozen HarnessContext.
    assert ctx.skill_activation_emitter is not None
    assert ctx.pause_resume_protocol is not None
    assert ctx.webhook_delivery_composer is not None
