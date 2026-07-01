"""C-RT-27 — Skill activation span emitter + activation hook Protocol.

Per ``Spec_Harness_Runtime_v1.md`` v1.32 §14.17 — H_T-AS-8d producer-site
absence resolution (Reading B operator-opt-in MVP per
``.harness/class_1_fork_as_8d_skill_activation_surface_absence.md``).

This module owns:

* ``SkillActivationMode`` — 3-value StrEnum preserving AS spec v1.7 §14.4
  Claude Code taxonomy verbatim per Q3=(i) ratification.
* ``SkillActivationHook`` — operator-supplied policy Protocol with 2 query
  methods (one per automatic-activation hook site).
* ``SkillActivationHookConfig`` — RuntimeConfig sub-model. ``None`` at the
  config layer = operator opt-out. Non-``None`` carries the operator-supplied
  ``hook`` Protocol implementation. Field-extension over the empty-marker
  precedent per spec §14.17.5 invariant 4 + §14.17.7 deferred-to-discretion
  (mechanism (a) — config-supplied; alternatives (b) singleton accessor /
  (c) future-version spec extension preserved as FM-2 follow-on options).
* ``SkillActivationSpanEmitter`` — producer carrier; ``emit(skill_id, mode,
  workflow_id, skill)`` opens a ``skill.activation`` span, sets all 6
  AS spec §14.4 attributes, closes synchronously.
* ``SkillActivationEmitterStageMaterializeError`` — typed exception mapped to
  fail class ``RT-FAIL-SKILL-ACTIVATION-STAGE-MATERIALIZE`` per §14.17.4.
* ``UnknownSkillError`` — raised by ``HarnessContext.activate_skill`` on
  skill-id miss per §14.17.2 hook-3 step 3.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from harness_core import SkillID

if TYPE_CHECKING:
    from harness_runtime.lifecycle.skills import Skill

__all__ = [
    "SkillActivationEmitterStageMaterializeError",
    "SkillActivationHook",
    "SkillActivationHookConfig",
    "SkillActivationMode",
    "SkillActivationSpanEmitter",
    "UnknownSkillError",
]


class SkillActivationMode(StrEnum):
    """Activation-mode discriminator for ``skill.activation`` span emission.

    Per AS spec v1.7 §14.4 + Q3=(i) ratification (preserve Claude Code
    taxonomy verbatim). H_T-runtime hook-to-enum mapping per Q2=(d) hybrid:

    * ``FRONTMATTER_ONLY`` — per-workflow-init hook fired (Skill selected via
      frontmatter match at workflow init).
    * ``TOOL_SEARCH`` — per-LLM-dispatch hook fired (LLM dispatch's tool
      selection moment activated this Skill).
    * ``FILESYSTEM_READ`` — operator-explicit ``ctx.activate_skill(...)``
      invoked (operator points at filesystem path = explicit filesystem read
      intent).
    """

    FRONTMATTER_ONLY = "frontmatter_only"
    TOOL_SEARCH = "tool_search"
    FILESYSTEM_READ = "filesystem_read"


@runtime_checkable
class SkillActivationHook(Protocol):
    """Operator-supplied activation policy.

    Two query methods for the two automatic-activation hook sites; the
    operator-explicit ``HarnessContext.activate_skill(...)`` site bypasses the
    hook entirely (operator-supplied ``skill_id`` directly).

    Per spec §14.17.1 + §14.17.2.
    """

    def select_for_workflow_init(
        self,
        loaded_skills: Iterable[SkillID],
        workflow_id: str,
    ) -> Iterable[SkillID]:
        """Per-workflow-init activation policy.

        Returns the subset of loaded skills the operator wants activated at
        workflow startup (emitted with ``activation_mode = FRONTMATTER_ONLY``).
        """
        ...

    def select_for_llm_dispatch(
        self,
        loaded_skills: Iterable[SkillID],
        workflow_id: str,
        step_index: int,
    ) -> Iterable[SkillID]:
        """Per-LLM-dispatch activation policy.

        Returns the subset of loaded skills the operator wants activated
        before this LLM dispatch (emitted with ``activation_mode =
        TOOL_SEARCH``).
        """
        ...


@dataclass(frozen=True)
class SkillActivationHookConfig:
    """Operator-supplied Skill activation hook policy + opt-in marker.

    Field-extension over the empty-marker precedent per spec §14.17.7
    deferred-to-discretion (mechanism (a) — config-supplied; alternatives
    (b)/(c) preserved as FM-2 follow-on options).

    Presence (non-``None`` at ``RuntimeConfig.skill_activation_hook_config``)
    signals operator opt-in. ``hook`` is the concrete ``SkillActivationHook``
    Protocol implementation consumed at hook firing time. ``None`` at the
    field layer is permitted to support emitter-without-hook deployment
    (e.g., emit only via operator-explicit ``ctx.activate_skill``) per
    §14.17.5 invariant 3 silent-skip discipline at automatic-hook sites.

    Typed ``Any`` (instead of the structural ``SkillActivationHook | None``)
    so Pydantic v2 schema-generation at ``RuntimeConfig.skill_activation_hook_config``
    introspection succeeds (the runtime_checkable Protocol is duck-typed at
    hook firing sites, not validated at dataclass layer).
    """

    hook: Any = None


class UnknownSkillError(KeyError):
    """``HarnessContext.activate_skill(skill_id)`` invoked with a skill_id
    not present in ``ctx.skills``.

    Per spec §14.17.2 hook-3 step 3. Raised to caller scope (NOT a fail-class
    propagating to the workflow driver); operator code that invokes
    ``ctx.activate_skill`` owns recovery (catch + log / catch + retry / etc.).
    """

    def __init__(self, skill_id: SkillID) -> None:
        super().__init__(skill_id)
        self.skill_id = skill_id


class SkillActivationEmitterStageMaterializeError(Exception):
    """Stage-5 ``materialize_skill_activation_emitter_stage`` cannot construct
    the emitter (e.g., tracer provider unbound; emitter construction raises).

    Per spec §14.17.4 + plan v2.28 U-RT-100 AC #7. Maps to fail class
    ``RT-FAIL-SKILL-ACTIVATION-STAGE-MATERIALIZE``; bootstrap aborts
    (fail-closed per ADR-F4 v1.1 §Consequences (c)).
    """


class SkillActivationSpanEmitter:
    """Producer-site emitter for the ``skill.activation`` span.

    Per spec §14.17.1 + §14.17.5 invariants 1+2+5+6+7.

    Constructed at bootstrap stage 5 by
    ``materialize_skill_activation_emitter_stage`` per §14.17.3. Bound to
    ``ctx.skill_activation_emitter`` (or ``None`` when operator opts out via
    ``RuntimeConfig.skill_activation_hook_config is None``).

    The ``emit`` method opens a short-lived ``skill.activation`` span carrying
    the AS spec v1.7 §14.4 6-attribute namespace. No nested scope; the span
    closes synchronously at method return.
    """

    def __init__(
        self,
        tracer_provider: Any,
        hook: SkillActivationHook | None = None,
    ) -> None:
        """Construct the emitter bound to a tracer provider + optional hook.

        Parameters
        ----------
        tracer_provider :
            OpenTelemetry ``TracerProvider`` per OD spec §C-OD-04. Sourced
            from ``ctx.tracer_provider`` at stage-5 materialization. Per
            ``lifecycle.llm_dispatch.RuntimeLLMDispatcher`` precedent, the
            tracer is acquired lazily at ``emit(...)`` time (NOT at
            construction) so the emitter tolerates fake-tracer substrates
            at integration-test stage-5 boundary.
        hook :
            Operator-supplied ``SkillActivationHook`` Protocol implementation
            consumed at the per-workflow-init + per-LLM-dispatch hook sites.
            ``None`` means automatic-hook sites silently no-op; the
            operator-explicit ``ctx.activate_skill`` path remains functional.
        """
        self._tracer_provider = tracer_provider
        self._hook = hook

    @property
    def hook(self) -> SkillActivationHook | None:
        """Bound activation hook (or ``None`` if not supplied)."""
        return self._hook

    def emit(
        self,
        skill_id: SkillID,
        mode: SkillActivationMode | str,
        workflow_id: str,
        skill: Skill,
    ) -> None:
        """Open + close a ``skill.activation`` span carrying the 6 attributes.

        Per AS spec v1.7 §14.4 namespace declaration. Span name format per
        OD spec §C-OD-04 §4.1 — short-lived span; ``skill.activation`` is
        the canonical span name (preserved at AS spec §14.1 verbatim per
        the 2026-05-26 GenAI fork resolution scope-statement that excludes
        non-LLM-inference parent-span anchors from rename).

        Parameters
        ----------
        skill_id, mode, workflow_id, skill :
            Sourced from the hook binding site. ``skill`` is the loaded
            ``Skill`` instance (from ``ctx.skills[skill_id]``); the v1.32
            ``SkillManifest`` extension carries ``version_sha`` +
            ``body_tokens`` computed at load.
        """
        tracer = self._tracer_provider.get_tracer("harness_runtime.skill_activation")
        with tracer.start_as_current_span("skill.activation") as span:
            span.set_attribute("skill.id", str(skill_id))
            span.set_attribute("skill.name", skill.manifest.name)
            span.set_attribute("skill.version_sha", skill.manifest.version_sha)
            span.set_attribute("skill.frontmatter.version", skill.manifest.version)
            span.set_attribute("skill.body_tokens", skill.manifest.body_tokens)
            span.set_attribute("skill.activation_mode", str(mode))
            # workflow_id is workflow-scope correlation; not an AS spec §14.4
            # attribute but useful for span hierarchy. Set as a non-namespace
            # attribute per OD spec §C-OD-04 §4.4 trace-context primitive.
            span.set_attribute("workflow.id", workflow_id)
