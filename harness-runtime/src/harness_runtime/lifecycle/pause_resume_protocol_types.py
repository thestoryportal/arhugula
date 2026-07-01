"""U-RT-87 — Pause/resume protocol config carrier (empty-marker sub-model).

Implements runtime spec v1.21 §14.14.1 (architectural surfaces introduced):

- `PauseResumeProtocolConfig`: operator-supplied pause/resume protocol opt-in
  marker. Empty-marker `@dataclass(frozen=True)` per the `ValidatorFrameworkConfig`
  precedent (runtime spec v1.18 §14.13.1; `lifecycle/validator_framework_types.py`).
  Presence at `RuntimeConfig.pause_resume_protocol_config` signals operator
  opt-in to the pause/resume protocol; absence (`None` default at C-RT-02)
  signals operator opt-out and yields `ctx.pause_resume_protocol is None`.

Internal operator-supply shape (snapshot-storage substrate selection, pause-
trigger detection mechanism, resume-API-surface selection) is deferred to
implementation discretion at C-RT-24 landing arc per FM-2 no-extension
discipline (spec §14.14.7).

Module sits parallel to `validator_framework_types.py` + `memory_tool_types.py`
under `lifecycle/` per the §14.12 + §14.13 carrier-home precedent (RuntimeConfig
sub-models that pair with stage factories at runtime spec contracts live in
harness-runtime/lifecycle/).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PauseResumeProtocolConfig:
    """Operator-supplied pause/resume protocol opt-in marker.

    Empty-marker at v1.21 Reading A scope per spec §14.14.1. The carrier
    shape is intentionally empty; presence signals opt-in, absence (the
    `RuntimeConfig.pause_resume_protocol_config = None` default) signals
    opt-out.

    `durable` (NEW at runtime spec v1.46, R-CC-1 arc #3 cascade step 2) selects
    the **harness-owned durable** snapshot-storage substrate: when `True`, the
    stage-5 factory wraps the CP `PauseResumeProtocol` in a
    `DurablePauseResumeProtocol` backed by a `JournalWorkflowPauseStore`
    (co-located under the resolved `STATE_LEDGER` dir), so captured snapshots
    survive a process restart and `api.resume(..., resume_handle=...)` can read
    them back. `False` (the default) preserves the v1.21 behavior verbatim
    (the bare CP protocol; the caller persists the snapshot itself).
    """

    durable: bool = False
    """`True` → durable harness-owned snapshot persistence (cascade step 2)."""

    @classmethod
    def default(cls) -> PauseResumeProtocolConfig:
        """Return the empty-marker default instance.

        Equivalent to leaving `RuntimeConfig.pause_resume_protocol_config = None`
        at the opt-out shape (which is the production-default state); the
        explicit `.default()` factory provides the empty marker for opt-in
        callers who want the no-snapshot-substrate-config baseline at v1.21.
        """
        return cls()
