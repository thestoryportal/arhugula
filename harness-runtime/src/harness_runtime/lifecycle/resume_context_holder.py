"""Runtime-internal sidecar carrier for one-shot ResumeContext delivery.

Authored at runtime spec v1.25 §14.8.8.9 (D10 operator-ratified 2026-05-24)
to close the v1.24 §14.8.8.8 implementer-discretion deferral on
``ctx.resume_context`` binding-site shape.

The canonical ``HarnessContext`` per ``types.py`` is a frozen Pydantic v2
BaseModel (``ConfigDict(frozen=True)``); direct field mutation
(``ctx.resume_context = None`` after one-shot consume) raises
``FrozenInstanceError``. The sidecar pattern resolves the tension: the
``ResumeContextHolder`` is itself frozen at the outer (Pydantic) layer, but
carries a single mutable internal state field via ``PrivateAttr``. Mutation
goes through the public ``set()`` and ``consume_and_clear()`` methods, NOT
direct field assignment.

Sibling pattern: ``ValidatorFrameworkConfig`` (v1.18 §14.13) +
``PauseResumeProtocolConfig`` (v1.21 §14.14) — empty-marker sub-model
precedent. ``ResumeContextHolder`` differs by carrying mutable internal
state via ``PrivateAttr`` (the marker subs are field-less).
"""

from __future__ import annotations

from harness_cp.pause_resume_protocol_types import ResumeContext
from pydantic import BaseModel, ConfigDict, PrivateAttr

__all__ = ["ResumeContextHolder"]


class ResumeContextHolder(BaseModel):
    """Frozen-outer / mutable-internal sidecar for one-shot ResumeContext.

    Lifecycle:
      1. Initialized at stage 5 LOOP_INIT to empty holder
         (``_current_context = None``).
      2. Driver-side resume entry-point per CP spec v1.16 §26.8.5: when caller
         invokes ``attempt_resume(snapshot, *, material_diff_policy,
         resume_context=ResumeContext(hitl_response=...))``, the driver
         receives the ``resume_context`` arg and calls
         ``ctx.resume_context_holder.set(resume_context)`` BEFORE driver
         hands control to the resumed-step inner loop.
      3. Runtime composer at §14.8.8.5 resumed-step gate-evaluation calls
         ``holder_state = ctx.resume_context_holder.consume_and_clear()``
         (atomic — returns current value AND clears to None in one step).
         If ``holder_state is not None and holder_state.hitl_response is not
         None``, the gate-evaluation consumes the operator response as the
         ``gate_result`` per §14.8.8.5 one-shot delivery; otherwise the
         gate-evaluation re-fires sync (or durable-async per cell synchrony).
    """

    model_config = ConfigDict(frozen=True)

    _current_context: ResumeContext | None = PrivateAttr(default=None)

    def set(self, resume_context: ResumeContext) -> None:
        """Set the current resume context.

        Called at driver-side resume entry-point per CP spec v1.16 §26.8.5.
        Last-write-wins semantic per runtime spec v1.25 change-note adjacent
        defect (ii): if called twice between ``consume_and_clear()`` calls,
        the second call overrides the first.
        """
        self._current_context = resume_context

    def peek(self) -> ResumeContext | None:
        """Return the current resume context WITHOUT clearing it.

        B-EFFECT-FENCE-PAUSE-RESOLUTION: the CP driver peeks (does NOT consume) to
        extract `effect_fence_resolution` for an effect-fence-ambiguous-pause resume,
        leaving the holder intact so the runtime HITL composer's one-shot
        ``consume_and_clear()`` is unaffected (a step that has both a HITL gate and a
        fenced tool dispatch still delivers its HITL response). For an effect-fence
        pause the `hitl_response` is None anyway (a pause has one reason), so the two
        readers never contend for the same field. The driver only peeks when the
        snapshot carries `effect_fence_resume` (an effect-fence pause), so a HITL-only
        resume never reaches this path.
        """
        return self._current_context

    def consume_and_clear(self) -> ResumeContext | None:
        """Atomically return the current resume context AND clear to None.

        Called at runtime composer §14.8.8.5 resumed-step gate-evaluation.
        Enforces §14.8.8.7 invariant 3 one-shot semantic — subsequent calls
        return None until ``set()`` is invoked again.

        Atomicity is at the level of the Python interpreter's GIL + the
        sequential nature of asyncio (within a single asyncio task, the
        read-and-clear sequence is uninterruptible by another resume
        invocation in the same task). Cross-task atomicity is NOT
        guaranteed; the holder is scoped to a single workflow execution
        per ``HarnessContext`` lifecycle per C-RT-04.
        """
        current = self._current_context
        self._current_context = None
        return current
