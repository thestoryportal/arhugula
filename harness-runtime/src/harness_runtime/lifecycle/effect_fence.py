"""Effect-boundary fence — at-most-once EXECUTION of non-idempotent step effects.

R-FS-1 standalone arc ``B-EFFECT-FENCE`` (runtime spec §14.22 C-RT-31, new at
v1.60). The durable engine classes (E sub-program) guarantee at-most-once *claim
of a revision* (the U-RT-123 reconciler CAS), NOT at-most-once *execution* of the
workflow steps a resume re-runs. A non-idempotent external effect (``git_push`` /
``send_email`` / any side-effecting MCP tool) fires *inside* the re-executed step,
and the per-step ledger COMMIT (`workflow_driver.py` `_append_step_ledger_entry`)
lands AFTER the dispatch returns — so a crash anywhere in the window

    dispatch(step) → call_tool() [EFFECT FIRES] → … → _append_step_ledger_entry()

leaves the effect fired but the step *uncommitted*. On resume, the driver's
``_determine_resume_at`` (shared by all four durable engine classes) finds the
step absent from the contiguous-materialized prefix → ``resume_at`` = that step →
the loop re-dispatches it → the same external effect fires a SECOND time. The
prefix-skip protects only COMMITTED steps; this window is precisely the
effected-but-uncommitted step nothing else covers, and is DISTINCT from the
reconciler's *revision-claim* fail-close.

This module is the hand-rolled (I-6 — no vendored Temporal/DBOS activity-dedup)
**per-effect fence at the tool sink**. It mirrors the U-RT-123 reconciler's
crash-atomic POSIX ``O_EXCL``/``os.link`` claim, applied at the
``RuntimeToolDispatcher`` ``call_tool`` sink keyed on the per-(run, step, tool)
composed ``idempotency_key``:

  * ``try_reserve(key)`` does the atomic claim. The FIRST caller wins (``True`` →
    fire the effect). ANY later caller of the SAME key loses (``False``) — both
    the cross-process RESUME re-dispatch AND an in-process RETRY (the
    ``RetryBreakerToolDispatcher`` re-calls the bare dispatcher) re-reach the sink
    with the same key. The sink SPLITS a lost claim on the captured output (see
    Semantic below) — suppress-and-continue when present, PAUSE/FAILED when not.
  * ``capture_output(key, payload)`` persists the tool's validated output AFTER
    the effect fires but BEFORE the step commits, keyed on the SAME
    ``idempotency_key`` and using the SAME crash-atomic claim primitive.
  * COMMIT = the EXISTING per-step ledger entry (one source of truth); the fence
    adds the RESERVE (pre-fire) marker + the OUTPUT (post-fire) record. No second
    commit record.

**Semantic = at-most-once, NOT exactly-once.** A re-dispatch of a lost-reserve
effect (the fire→commit window the prefix-skip does not cover) splits on the
captured output (B-EFFECT-FENCE-HITL-ROUTE):

  * **output present** (``read_output`` returns it) ⟹ the effect demonstrably
    completed AND its result is in hand ⟹ *suppress-and-continue*: the sink
    returns the captured output, the resumed step proceeds as if it ran, and the
    effect is NOT re-fired.
  * **output absent or corrupt** ⟹ the crash fell in the fire→capture window, so
    whether the effect fired is genuinely ambiguous ⟹ fail to the operator: the
    sink raises ``EffectFenceAmbiguousUncommittedError``, which the workflow
    driver routes to a §26.2 ``WorkflowPauseReason.EFFECT_FENCE_AMBIGUOUS`` PAUSE
    when a ``PauseResumeProtocol`` is bound, else FAILED (the conservative opt-out
    default). NEVER an auto-re-fire — auto-proceed happens ONLY on
    proof-of-completion, which IS the at-most-once guarantee.

This mirrors the reconciler's fail-closed posture on the ambiguous window while
adding the auto-recover path the captured output makes safe. Output capture uses
the same crash-atomic ``O_EXCL``/``os.link`` primitive as the reserve, so a torn
write can only leave an orphan temp, never a half-published output (present ⟹
complete-and-valid); a present-but-corrupt output (defensive — the atomic link
makes it impossible) fail-closes to PAUSE, never a valid suppress source.

**Operationally surprising (documented, not hidden):** a transient ``call_tool``
failure of a non-idempotent effect does NOT capture an output (capture is
post-validation), so its retry re-reaches the sink with the reserve held and no
output ⟹ PAUSE (or FAILED when unbound) rather than a blind retry. That is the
*correct* conservative behavior for a non-idempotent effect (you cannot safely
retry an effect that may already have fired); an idempotent tool does not need
the fence (per-tool fence opt-in is the ``B-EFFECT-FENCE-PER-TOOL`` follow-on,
landed at AS spec v1.12).

**Single-host** (the reconciler's bound posture): the default ``LOCAL_SINGLE_HOST``
flock-free claim is atomic on a local filesystem. Cross-host effect-fencing is
distributed-impossible under {I-6 ∧ no-unsafe-TTL} and folds into the deferred
F-CC multi-host recovery item — exactly as for the reconciler.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

__all__ = [
    "EffectFenceAbortedError",
    "EffectFenceAmbiguousUncommittedError",
    "EffectFenceProtocol",
    "RuntimeEffectFence",
]


class EffectFenceAmbiguousUncommittedError(Exception):
    """A re-dispatch lost the reserve AND no captured output proves completion.

    The effect's ``idempotency_key`` was already claimed by a prior attempt that
    did NOT commit (else the step would be prefix-skipped on resume and never
    re-reach the sink), AND ``read_output`` found no valid captured output. The
    crash therefore fell in the fire→capture window, so whether the external
    effect actually fired is genuinely ambiguous: re-firing risks an at-least-once
    double-execution, and there is no captured result to suppress-and-continue
    with. The sink raises this to hand the decision to the operator — the workflow
    driver routes it to a §26.2 ``WorkflowPauseReason.EFFECT_FENCE_AMBIGUOUS``
    PAUSE when a ``PauseResumeProtocol`` is bound, else FAILED (the conservative
    opt-out default). NEVER an auto-re-fire. NOT a transient class: a retry always
    re-loses the claim, so the ``RetryBreakerToolDispatcher`` treats it as
    permanent (re-raised verbatim).

    (Distinct from the auto-recover path: a lost reserve WITH a present captured
    output suppresses-and-continues at the sink and never raises.)
    """

    def __init__(self, *, idempotency_key: str) -> None:
        self.idempotency_key = idempotency_key
        super().__init__(
            "effect-fence: idempotency_key reserved by a prior uncommitted "
            "attempt with NO captured output; whether the non-idempotent effect "
            "fired is ambiguous — fail to operator (PAUSE/FAILED), never "
            f"auto-re-fire (at-most-once) (key={idempotency_key!r})"
        )


class EffectFenceAbortedError(Exception):
    """The operator resolved an ambiguous effect-fence pause with ABORT.

    B-EFFECT-FENCE-PAUSE-RESOLUTION: on resume of a §26.2 EFFECT_FENCE_AMBIGUOUS
    pause, the operator chose ABORT (cannot determine whether the effect fired, or
    declines to proceed). The dispatcher raises this terminal marker; the workflow
    driver name-matches it at the step-dispatch boundary and maps it to FAILED (the
    conservative terminal — never re-fire, never proceed-with-empty). Distinct from
    ``EffectFenceAmbiguousUncommittedError`` (which the driver routes to a resumable
    PAUSE): ABORT is the operator's terminal decision, so it does NOT re-pause. NOT a
    transient class — the ``RetryBreakerToolDispatcher`` re-raises it verbatim.
    """

    def __init__(self, *, idempotency_key: str) -> None:
        self.idempotency_key = idempotency_key
        super().__init__(
            "effect-fence: operator resolved the ambiguous pause with ABORT — "
            f"fail the run terminally, never re-fire (key={idempotency_key!r})"
        )


@runtime_checkable
class EffectFenceProtocol(Protocol):
    """The reserve-before-fire + capture-after-fire surface the tool dispatcher
    consults at the sink."""

    def try_reserve(self, idempotency_key: str) -> bool:
        """Atomically claim the effect. ``True`` = won (fresh) → fire; ``False`` =
        already reserved by a prior attempt → the caller splits on
        ``read_output``."""
        ...

    def capture_output(self, idempotency_key: str, payload: Mapping[str, Any]) -> None:
        """Durably + atomically record the effect's validated output, keyed on the
        same ``idempotency_key`` as the reserve. Called post-fire / pre-commit so a
        captured output always denotes a complete, valid success."""
        ...

    def read_output(self, idempotency_key: str) -> dict[str, Any] | None:
        """Return the captured output iff a complete, valid capture exists; ``None``
        iff ABSENT or CORRUPT (both → the ambiguous case, fail-closed)."""
        ...

    def clear_claim(self, idempotency_key: str) -> None:
        """Remove the held claim (+ any captured output) so a subsequent
        ``try_reserve`` wins and the effect re-fires fresh. The RE_FIRE resolution
        path. Missing-ok (idempotent)."""
        ...

    def try_consume_refire(self, idempotency_key: str) -> bool:
        """Atomically claim the ONE re-fire for this effect. ``True`` = THIS call won
        the re-fire latch (the FIRST RE_FIRE attempt → clear the stale claim + fire
        fresh); ``False`` = the latch was already taken (a RETRY of the re-fire, or a
        crash-then-resume after a re-fire began) → do NOT clear, fall through to the
        normal fence flow (the re-fire's own claim is held → suppress/ambiguous), so a
        retryable error during the re-fire can NEVER double-fire the effect."""
        ...


class RuntimeEffectFence:
    """Durable single-host effect fence — crash-atomic ``O_EXCL``/``os.link`` claim.

    ``harness_runtime``-private; never leaks into a cleared CP Protocol (a runtime
    substrate within §14.9 impl-discretion, not a new cross-axis contract). The
    claim mechanism is the U-RT-123 ``_claim_resume_revision`` pattern verbatim,
    re-keyed from ``(workflow, resource_version)`` to the per-effect
    ``idempotency_key``.
    """

    def __init__(self, *, fence_dir: Path) -> None:
        self._fence_dir = fence_dir

    @property
    def fence_dir(self) -> Path:
        """The durable claim-file directory."""
        return self._fence_dir

    def _claim_file(self, idempotency_key: str) -> Path:
        """The atomic claim file for one effect ``idempotency_key``.

        The key is already per-(run, step, tool) scoped (the run-scoped
        ``_compute_step_idempotency_key`` composed with ``step_id``/``tool_id`` at
        the dispatcher), so a flat directory keyed by its digest is collision-free
        ACROSS runs — a fresh run derives a different ``run_idempotency_key`` → a
        disjoint claim namespace (the U-RT-123 finding O-E3b-1 run-scoping lesson).
        """
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        return self._fence_dir / f"{digest}.claim"

    def _output_file(self, idempotency_key: str) -> Path:
        """The atomic output file for one effect ``idempotency_key``.

        Same per-(run, step, tool) digest keying as ``_claim_file`` (so claim +
        output share a run-scoped namespace), a distinct ``.output`` suffix.
        """
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        return self._fence_dir / f"{digest}.output"

    def _refire_file(self, idempotency_key: str) -> Path:
        """The atomic re-fire latch for one effect ``idempotency_key``.

        Same per-(run, step, tool) digest keying as ``_claim_file`` (so the latch
        shares the run-scoped namespace), a distinct ``.refire`` suffix. A one-way
        latch: once set, RE_FIRE is consumed for this key — a retry / crash-resume of
        the re-fire can never clear-and-re-fire again (the double-fire Codex [P1]).
        """
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        return self._fence_dir / f"{digest}.refire"

    def try_reserve(self, idempotency_key: str) -> bool:
        """Crash-atomic claim of the effect ``idempotency_key``.

        Returns ``True`` iff THIS call won the right to fire the effect; ``False``
        iff the key was already reserved (a resume re-dispatch or an in-process
        retry of the same effect). Hand-rolled (I-6) via the POSIX atomic
        create-exclusive primitive: write a best-effort incarnation stamp
        (``host:pid`` — for observability, NOT the win/lose discriminator) to a
        uuid-unique temp (``fsync``-ed), then ``os.link`` it into place. ``link``
        is atomic and raises ``FileExistsError`` if the claim exists, so a crash
        can only ever leave an orphan temp, never a half-published claim; the won
        claim's dirent is ``fsync``-ed so a crash cannot lose it and let a second
        dispatch re-create the claim and double-fire.
        """
        path = self._claim_file(idempotency_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = f"{socket.gethostname()}:{os.getpid()}".encode()
        tmp = path.parent / f"{path.name}.{uuid.uuid4().hex}.tmp"
        try:
            fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(fd, stamp)
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                os.link(tmp, path)
            except FileExistsError:
                return False  # the effect is already reserved → lose the claim
            self._fsync_dir(path.parent)
            return True
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def try_consume_refire(self, idempotency_key: str) -> bool:
        """Atomically claim the ONE re-fire for this effect (the same crash-atomic
        ``O_EXCL``/``os.link`` primitive as ``try_reserve``, on the ``.refire`` latch).

        Returns ``True`` iff THIS call won the latch (the FIRST RE_FIRE attempt → the
        caller clears the stale claim + fires fresh); ``False`` iff the latch was
        already taken (a ``RetryBreakerToolDispatcher`` retry of the re-fire reusing
        the same ``step_context``, or a crash-then-resume after a re-fire began) → the
        caller does NOT clear, so ``try_reserve`` loses to the re-fire's own held claim
        and the normal split (suppress-if-captured / ambiguous-PAUSE) applies. This is
        the durable consume-once that stops a retryable error during the re-fire from
        double-firing the non-idempotent effect (Codex [P1]).
        """
        path = self._refire_file(idempotency_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / f"{path.name}.{uuid.uuid4().hex}.tmp"
        try:
            fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(fd, f"{socket.gethostname()}:{os.getpid()}".encode())
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                os.link(tmp, path)
            except FileExistsError:
                return False  # the re-fire latch is already taken → do NOT re-clear
            self._fsync_dir(path.parent)
            return True
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def capture_output(self, idempotency_key: str, payload: Mapping[str, Any]) -> None:
        """Crash-atomically persist the effect's validated output (post-fire).

        Mirrors ``try_reserve``'s ``O_EXCL``/``os.link`` crash-atomicity: serialize
        the payload to a uuid-unique temp (``fsync``-ed), then ``os.link`` it into
        ``<digest>.output``. A torn write can only leave an orphan temp, never a
        half-published output, so ``read_output``'s "present" ⟹ "complete-and-valid".
        The dispatcher calls this AFTER the effect fired and its response passed
        ``output_schema`` validation, so a captured output is always a valid success
        result — a re-dispatch may safely return it (suppress-and-continue) without
        re-validating. Capturing twice (defensive — only the single reserve-winner
        captures) is a no-op: the first published output wins (``os.link`` raises
        ``FileExistsError``).
        """
        path = self._output_file(idempotency_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(dict(payload), sort_keys=True).encode("utf-8")
        tmp = path.parent / f"{path.name}.{uuid.uuid4().hex}.tmp"
        try:
            fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(fd, serialized)
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                os.link(tmp, path)
            except FileExistsError:
                return  # output already published by this winner → first publish wins
            self._fsync_dir(path.parent)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def read_output(self, idempotency_key: str) -> dict[str, Any] | None:
        """Read back the captured output for a reserved effect.

        Returns the deserialized output dict iff a complete, valid capture exists;
        ``None`` iff ABSENT (no output file — the crash fell before the
        ``capture_output`` fsync) OR CORRUPT (un-parseable / non-object — defensive;
        the atomic link makes a half-published output impossible). The dispatcher's
        two-case split treats ``None`` (absent-or-corrupt) as the ambiguous case →
        §26.2 PAUSE/FAILED, and a non-``None`` as proof-of-completion →
        suppress-and-continue. Fail-closed on corrupt: a torn/garbage output is
        NEVER a valid suppress source.
        """
        path = self._output_file(idempotency_key)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None  # absent → ambiguous
        try:
            loaded: Any = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return None  # corrupt → ambiguous (fail-closed)
        if not isinstance(loaded, dict):
            return None  # defensive — a non-object payload is not a valid tool response
        # A JSON object always has string keys; `capture_output` round-trips the
        # validated tool response, so this is the same `dict[str, Any]` shape.
        return cast("dict[str, Any]", loaded)

    def clear_claim(self, idempotency_key: str) -> None:
        """Remove the held claim + any captured output for an effect.

        The RE_FIRE resolution path (B-EFFECT-FENCE-PAUSE-RESOLUTION): the operator
        asserts the effect did NOT fire (the prior attempt claimed the reserve, then
        crashed before firing), so clearing the held reserve lets the resumed dispatch's
        ``try_reserve`` WIN and fire the effect as a FIRST-and-only execution (still
        at-most-once from the true state of the world). The inverse of ``try_reserve``:
        unlink both the ``<digest>.claim`` and any ``<digest>.output`` (an ambiguous
        pause has no valid output, but a corrupt one may exist — clear it too so the
        fresh dispatch's ``capture_output`` is not shadowed). Missing-ok (idempotent):
        a re-resume that already cleared + re-fired is a no-op here.
        """
        self._claim_file(idempotency_key).unlink(missing_ok=True)
        self._output_file(idempotency_key).unlink(missing_ok=True)

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        """``fsync`` the directory so the won claim's dirent survives a crash."""
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        except OSError:
            # Some filesystems reject directory fsync; the link itself is durable
            # enough on those (best-effort, mirroring the reconciler substrate).
            pass
        finally:
            os.close(dir_fd)
