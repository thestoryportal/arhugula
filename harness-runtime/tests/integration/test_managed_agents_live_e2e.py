"""Arc M — live Managed Agents step-dispatch end-to-end (C-RT-28 §14.20).

Intentionally marked ``e2e`` + skip-gated on operator-provided Anthropic
Managed Agents credentials. It drives a REAL Anthropic Managed Agents session
through the production dispatch surface:

  AnthropicManagedAgentsClient (live SDK)
    → ManagedAgentsStepDispatcher.dispatch (C-RT-28 §14.20.2)
      → create_session → send_event → poll-to-terminal → managed_agents.runtime span
    → session-outcome mapping

This is the **surfaced vendor-gate** (fork doc Slice 6): a live run re-touches
ANTHROPIC_API_KEY (paid). It NEVER auto-fires — it skips unless the operator
supplies the credentials + agent/environment IDs and explicitly runs the e2e
lane (`[[feedback-background-agent-no-unilateral-paid-calls-or-secret-relocation]]`).

Required environment:

- ``ANTHROPIC_API_KEY``: a live Anthropic API key (paid; managed-agents beta).
- ``MANAGED_AGENTS_E2E_AGENT_ID``: a provisioned Managed Agents agent id.
- ``MANAGED_AGENTS_E2E_ENVIRONMENT_ID``: a provisioned environment id.
"""

from __future__ import annotations

import importlib.util
import os

import pytest
from harness_core import StepID
from harness_cp.workflow_driver_types import StepKind, WorkflowStep
from harness_runtime.lifecycle.managed_agents import (
    AnthropicManagedAgentsClient,
    ManagedAgentSessionStatus,
)
from harness_runtime.lifecycle.managed_agents_dispatch import ManagedAgentsStepDispatcher
from opentelemetry.sdk.trace import TracerProvider


def _require_live_managed_agents() -> dict[str, str]:
    if importlib.util.find_spec("anthropic") is None:
        pytest.skip("managed-agents live e2e requires the `anthropic` SDK")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    agent_id = os.environ.get("MANAGED_AGENTS_E2E_AGENT_ID", "").strip()
    environment_id = os.environ.get("MANAGED_AGENTS_E2E_ENVIRONMENT_ID", "").strip()
    if not api_key:
        pytest.skip("managed-agents live e2e requires ANTHROPIC_API_KEY (paid; vendor-gate)")
    if not agent_id or not environment_id:
        pytest.skip(
            "managed-agents live e2e requires MANAGED_AGENTS_E2E_AGENT_ID + "
            "MANAGED_AGENTS_E2E_ENVIRONMENT_ID"
        )
    return {"api_key": api_key, "agent_id": agent_id, "environment_id": environment_id}


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_managed_agents_step_dispatch_live_e2e() -> None:
    params = _require_live_managed_agents()

    from anthropic import Anthropic  # local import — only on the live path

    sdk_client = Anthropic(api_key=params["api_key"])
    client = AnthropicManagedAgentsClient(client=sdk_client)
    dispatcher = ManagedAgentsStepDispatcher(client=client, tracer_provider=TracerProvider())

    step = WorkflowStep(
        step_id=StepID("managed-agents-e2e-0"),
        step_kind=StepKind.MANAGED_AGENTS,
        step_payload={
            "agent_id": params["agent_id"],
            "environment_id": params["environment_id"],
            "event_type": "user.message",
            "event_payload": {"content": [{"type": "text", "text": "ping"}]},
            "poll_interval_seconds": 2.0,
            "max_poll_attempts": 60,
        },
    )

    out = await dispatcher.dispatch(
        binding=None,  # type: ignore[arg-type]  # unused by managed-agents dispatch
        step=step,
        step_context=None,  # type: ignore[arg-type]
    )

    assert out["session_id"]
    assert out["status"] in {
        ManagedAgentSessionStatus.IDLE.value,
        ManagedAgentSessionStatus.COMPLETED.value,
    }
    assert out["billable_seconds"] >= 0.0
