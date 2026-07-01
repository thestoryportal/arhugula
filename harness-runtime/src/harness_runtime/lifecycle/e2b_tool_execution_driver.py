"""E2B-backed TOOL_STEP execution driver for R-412.

This driver supplies the managed-cloud Tier-4 execution mechanism selected for
R-412: a short-lived E2B hosted sandbox executes a JSON tool-runner command and
returns the runner's JSON response to ``RuntimeToolDispatcher``. The dispatcher
continues to own trust checks, tier-floor enforcement, telemetry spans, and
output-schema validation.

Authority: C-RT-19 ToolExecutionDriver tier binding and C-RT-21 TOOL_STEP
retry wrapper.
"""

from __future__ import annotations

import importlib
import json
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from harness_as.sandbox_tier import SandboxTier

from harness_runtime.lifecycle.mcp_client_host import MCPClientHost
from harness_runtime.lifecycle.runtime_tool_dispatcher import (
    SandboxDispatchDecision,
    ToolInvocationProtocolError,
)

__all__ = ["E2BManagedFullVMToolRunnerExecutionDriver"]


def _empty_metadata() -> Mapping[str, str]:
    return {}


def _load_e2b_sandbox_class() -> Any:
    try:
        module = importlib.import_module("e2b")
    except ImportError as exc:
        raise ToolInvocationProtocolError(
            "Python module 'e2b' is not importable; install it explicitly for "
            "live R-412 runs, e.g. `uv run --with e2b ...`"
        ) from exc
    sandbox_cls = getattr(module, "Sandbox", None)
    if sandbox_cls is None:
        raise ToolInvocationProtocolError("Python module 'e2b' does not expose Sandbox")
    return sandbox_cls


@dataclass(frozen=True)
class E2BManagedFullVMToolRunnerExecutionDriver:
    """Execute one TOOL_STEP through an E2B managed full-VM sandbox.

    The configured command must read one JSON object from stdin and write one
    JSON object to stdout. The request shape matches the local Docker/gVisor
    runners so tool-runner images/scripts can be shared across provider
    classes.
    """

    command: Sequence[str]
    sandbox_cls: Any | None = None
    timeout_seconds: int = 30
    sandbox_timeout_seconds: int = 60
    allow_internet_access: bool = False
    metadata: Mapping[str, str] = field(default_factory=_empty_metadata)
    required_tier: SandboxTier = SandboxTier.TIER_4_FULL_VM

    def __post_init__(self) -> None:
        if not tuple(self.command):
            raise ValueError("E2BManagedFullVMToolRunnerExecutionDriver.command must be non-empty")
        if self.timeout_seconds <= 0:
            raise ValueError(
                "E2BManagedFullVMToolRunnerExecutionDriver.timeout_seconds must be > 0"
            )
        if self.sandbox_timeout_seconds <= 0:
            raise ValueError(
                "E2BManagedFullVMToolRunnerExecutionDriver.sandbox_timeout_seconds must be > 0"
            )

    async def call_tool(
        self,
        *,
        mcp_client_host: MCPClientHost,
        sandbox_decision: SandboxDispatchDecision,
        tool_id: str,
        tool_args: Mapping[str, Any],
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        """Run the configured JSON runner in a managed E2B sandbox."""
        _ = mcp_client_host
        if sandbox_decision.tier is not self.required_tier:
            raise ToolInvocationProtocolError(
                f"{type(self).__name__} requires resolved "
                f"{self.required_tier.value}, got {sandbox_decision.tier.value!r}"
            )

        payload = {
            "tool_id": tool_id,
            "tool_args": dict(tool_args),
            "idempotency_key": idempotency_key,
            "sandbox": {
                "tier": sandbox_decision.tier.value,
                "tech": sandbox_decision.tech,
                "provider": sandbox_decision.provider,
                "assigned_tier_reason": sandbox_decision.assigned_tier_reason,
            },
        }
        sandbox_cls = self.sandbox_cls or _load_e2b_sandbox_class()
        command = self._command_with_stdin(json.dumps(payload))
        metadata = {
            "roadmap_item": "R-412-sandbox-tier-4-full-vm-execution",
            "sandbox_tier": self.required_tier.value,
            **dict(self.metadata),
        }
        with sandbox_cls.create(
            timeout=self.sandbox_timeout_seconds,
            allow_internet_access=self.allow_internet_access,
            metadata=metadata,
        ) as sandbox:
            result = sandbox.commands.run(command, timeout=self.timeout_seconds)
        return self._parse_result(result)

    def _command_with_stdin(self, payload_json: str) -> str:
        return f"printf %s {shlex.quote(payload_json)} | {shlex.join(tuple(self.command))}"

    def _parse_result(self, result: Any) -> Mapping[str, Any]:
        exit_code = getattr(result, "exit_code", None)
        if exit_code is None:
            exit_code = getattr(result, "return_code", None)
        if isinstance(exit_code, int) and exit_code != 0:
            stderr = getattr(result, "stderr", "")
            if not isinstance(stderr, str):
                stderr = repr(stderr)
            raise ToolInvocationProtocolError(
                f"E2B tool runner exited {exit_code}: {stderr.strip()}"
            )

        stdout = getattr(result, "stdout", None)
        if not isinstance(stdout, str):
            raise ToolInvocationProtocolError("E2B command result did not expose string stdout")
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise ToolInvocationProtocolError(
                f"E2B tool runner returned non-JSON stdout: {stdout.strip()!r}"
            ) from exc
        if not isinstance(parsed, dict):
            raise ToolInvocationProtocolError("E2B tool runner must return a JSON object")
        return cast(Mapping[str, Any], parsed)
