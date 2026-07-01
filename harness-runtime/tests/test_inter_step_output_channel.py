"""B-INTERSTEP — `InterStepOutputChannel` unit tests (runtime spec §14.21 C-RT-34).

Covers the channel's own semantics: append-ordered recording, the
`most_recent_output()` upstream-read (incl. the EVALUATOR_OPTIMIZER re-dispatch
case where append order — NOT a step_id dict — is load-bearing), the last-wins
`outputs_by_step_id()` view, and defensive copy-on-record.

The wiring (driver records / dispatcher injects) is exercised in
`harness-cp/tests/test_workflow_driver_evaluator_optimizer.py` (producer) and
`test_lifecycle_llm_dispatch.py` (the genuine LLM-dispatcher consumer).
"""

from __future__ import annotations

from harness_runtime.lifecycle.inter_step_output_channel import InterStepOutputChannel


def test_empty_channel_reads_none() -> None:
    channel = InterStepOutputChannel()
    assert len(channel) == 0
    assert channel.most_recent_output() is None
    assert channel.outputs_by_step_id() == {}


def test_record_then_most_recent_is_last_appended() -> None:
    channel = InterStepOutputChannel()
    channel.record("generate", {"draft": "v1"})
    channel.record("evaluate", {"feedback": "fix x"})
    assert len(channel) == 2
    assert channel.most_recent_output() == {"feedback": "fix x"}


def test_redispatched_step_id_most_recent_is_the_new_output_not_stale() -> None:
    """EVALUATOR_OPTIMIZER re-dispatches the same generate `step_id` across
    iterations. `most_recent_output()` MUST return the NEW generate output, not
    the (overwritten-in-place) prior value a step_id-keyed dict would surface —
    this is why the channel stores an append-ordered list."""
    channel = InterStepOutputChannel()
    channel.record("generate", {"draft": "v1"})
    channel.record("evaluate", {"feedback": "fix x"})
    channel.record("generate", {"draft": "v2"})  # regenerate
    assert channel.most_recent_output() == {"draft": "v2"}


def test_outputs_by_step_id_is_last_wins() -> None:
    channel = InterStepOutputChannel()
    channel.record("generate", {"draft": "v1"})
    channel.record("evaluate", {"feedback": "fix x"})
    channel.record("generate", {"draft": "v2"})
    assert channel.outputs_by_step_id() == {
        "generate": {"draft": "v2"},
        "evaluate": {"feedback": "fix x"},
    }


def test_record_copies_defensively() -> None:
    """A later mutation of the caller's mapping must not retroactively alter a
    recorded entry."""
    channel = InterStepOutputChannel()
    payload: dict[str, object] = {"draft": "v1"}
    channel.record("generate", payload)
    payload["draft"] = "mutated-after-record"
    assert channel.most_recent_output() == {"draft": "v1"}


def test_outputs_by_step_id_returns_a_copy() -> None:
    """Mutating the returned view must not corrupt channel state."""
    channel = InterStepOutputChannel()
    channel.record("generate", {"draft": "v1"})
    view = dict(channel.outputs_by_step_id())
    view["generate"] = {"draft": "tampered"}
    assert channel.outputs_by_step_id() == {"generate": {"draft": "v1"}}


def test_reset_clears_all_records() -> None:
    channel = InterStepOutputChannel()
    channel.record("generate", {"draft": "v1"})
    channel.record("evaluate", {"feedback": "fix"})
    assert len(channel) == 2
    channel.reset()
    assert len(channel) == 0
    assert channel.most_recent_output() is None
    assert channel.outputs_by_step_id() == {}


def test_reset_at_per_run_boundary_prevents_cross_run_leak() -> None:
    """The per-run boundary contract the `run_workflow` tool handler relies on
    (daemon-client mode, U-RT-108): resetting between runs on a REUSED channel
    means a later run's first read sees NONE of the prior run's outputs — no
    stale cross-run context leaks into a later model prompt."""
    channel = InterStepOutputChannel()
    # Run 1 records outputs.
    channel.record("generate", {"draft": "run1-secret"})
    channel.record("evaluate", {"feedback": "run1"})
    # Per-run boundary (the tool handler resets on a non-resume invocation).
    channel.reset()
    # Run 2's first dispatch reads an empty channel — no run-1 leak.
    assert channel.most_recent_output() is None
    channel.record("generate", {"draft": "run2"})
    assert channel.most_recent_output() == {"draft": "run2"}
