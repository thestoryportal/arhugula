"""U-RT-100 — SkillActivationSpanEmitter stage-5 LOOP_INIT factory.

Implements runtime spec v1.32 §14.17.3 factory signature + stage-5 LOOP_INIT
placement + §14.17.4 failure-mode taxonomy + §14.17.6 operator-opt-in
RETIRE-READY pattern.

Reading B operator-opt-in MVP absorption of fork
``.harness/class_1_fork_as_8d_skill_activation_surface_absence.md``
(operator-ratified 2026-05-28):

- Opt-out branch (``config.skill_activation_hook_config is None``) returns
  ``None`` unconditionally — preserves the pre-v1.32 production-default
  behavior; all 3 hook binding sites (per-LLM-dispatch / per-workflow-init
  / operator-explicit) test ``ctx.skill_activation_emitter is not None``
  and silent-skip per §14.17.5 invariant 3.
- Opt-in branch (non-None config) constructs a ``SkillActivationSpanEmitter``
  bound to ``ctx.tracer_provider`` + the operator-supplied
  ``SkillActivationHook`` per ``config.skill_activation_hook_config.hook``.
  Hook MAY be ``None`` at the field layer (emitter-without-hook deployment
  pattern — operator-explicit ``ctx.activate_skill`` path remains
  functional; automatic hooks silently skip).
- Construction failure raises ``SkillActivationEmitterStageMaterializeError``
  (fail class ``RT-FAIL-SKILL-ACTIVATION-STAGE-MATERIALIZE``, permanent
  severity → bootstrap rollback per C-RT-02).

Mirrors L9-decies validator_framework_factory + L9-undecies
pause_resume_protocol_factory + L9-quaterdecies webhook_delivery_composer_factory
module-shape precedent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from harness_runtime.lifecycle.skill_activation import (
    SkillActivationEmitterStageMaterializeError,
    SkillActivationSpanEmitter,
)
from harness_runtime.types import RuntimeConfig

if TYPE_CHECKING:
    from harness_runtime.bootstrap.mutable_context import _MutableHarnessContext


async def materialize_skill_activation_emitter_stage(
    config: RuntimeConfig,
    ctx: _MutableHarnessContext,
) -> SkillActivationSpanEmitter | None:
    """Stage-5 LOOP_INIT factory for ``SkillActivationSpanEmitter``.

    Per runtime spec v1.32 §14.17.3.

    Opt-out short-circuit: ``config.skill_activation_hook_config is None`` →
    return ``None`` (operator opt-out path; production-default state
    preserved).

    Opt-in construction: non-None config → construct emitter bound to
    ``ctx.tracer_provider`` (sourced from stage-4 OD-bucket landing) +
    operator-supplied hook from ``config.skill_activation_hook_config.hook``.

    Raises
    ------
    SkillActivationEmitterStageMaterializeError
        Emitter construction fails (e.g., ``ctx.tracer_provider`` is None at
        stage-5 entry; ``SkillActivationSpanEmitter.__init__`` raises).
    """
    if config.skill_activation_hook_config is None:
        return None

    if ctx.tracer_provider is None:
        raise SkillActivationEmitterStageMaterializeError(
            "tracer_provider unbound at stage-5 entry — "
            "stage-4 OD-bucket landing must complete before "
            "materialize_skill_activation_emitter_stage runs"
        )

    try:
        emitter = SkillActivationSpanEmitter(
            tracer_provider=ctx.tracer_provider,
            hook=config.skill_activation_hook_config.hook,
        )
    except Exception as e:
        raise SkillActivationEmitterStageMaterializeError(
            f"SkillActivationSpanEmitter construction failed: {e!r}"
        ) from e

    return emitter
