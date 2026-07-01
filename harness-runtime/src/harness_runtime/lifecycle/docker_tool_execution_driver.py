"""Docker-backed TOOL_STEP execution drivers for R-410/R-411.

The dispatcher remains responsible for trust, sandbox tier-floor enforcement,
span emission, and schema validation. This module supplies the concrete
TIER_2_CONTAINER and TIER_3_MICROVM mechanisms: run a tool-runner command in a
local Docker-compatible runtime and exchange a single JSON request/response
over stdin/stdout. The drivers are local-only: they resolve the configured
image with ``docker inspect`` and run the immutable image ID so Docker never
pulls by tag.

Authority: C-RT-19 ToolExecutionDriver tier binding and C-RT-21 TOOL_STEP
retry wrapper.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from harness_as.sandbox_tier import SandboxTier

from harness_runtime.lifecycle.mcp_client_host import MCPClientHost
from harness_runtime.lifecycle.runtime_tool_dispatcher import (
    SandboxDispatchDecision,
    ToolInvocationProtocolError,
    ToolInvocationTimeoutError,
)

__all__ = ["DockerToolRunnerExecutionDriver", "GVisorRunscToolRunnerExecutionDriver"]


@dataclass(frozen=True)
class DockerToolRunnerExecutionDriver:
    """Execute one TOOL_STEP through a Docker-hosted JSON tool runner.

    The configured container command must read JSON from stdin and write a JSON
    object to stdout. The request shape is intentionally small and stable:
    `tool_id`, `tool_args`, `idempotency_key`, and the resolved sandbox fields.
    """

    image: str
    command: Sequence[str]
    docker_binary: str = "docker"
    docker_command: Sequence[str] | None = None
    timeout_seconds: float = 30.0
    network: str = "none"
    runtime: str | None = None
    required_tier: SandboxTier = SandboxTier.TIER_2_CONTAINER

    def __post_init__(self) -> None:
        if not self.image.strip():
            raise ValueError("DockerToolRunnerExecutionDriver.image must be non-empty")
        if not tuple(self.command):
            raise ValueError("DockerToolRunnerExecutionDriver.command must be non-empty")
        if self.docker_command is not None and not tuple(self.docker_command):
            raise ValueError("DockerToolRunnerExecutionDriver.docker_command must be non-empty")
        if self.timeout_seconds <= 0:
            raise ValueError("DockerToolRunnerExecutionDriver.timeout_seconds must be > 0")

    async def call_tool(
        self,
        *,
        mcp_client_host: MCPClientHost,
        sandbox_decision: SandboxDispatchDecision,
        tool_id: str,
        tool_args: Mapping[str, Any],
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        """Run the configured tool runner in Docker and parse its JSON response."""
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
        image_id = await self._resolve_local_image_id()
        argv = [
            *self._docker_command(),
            "run",
            "--rm",
            "--network",
            self.network,
            *self._runtime_args(),
            "-i",
            image_id,
            *self.command,
        ]
        stdout, stderr, returncode = await self._communicate(
            argv=argv,
            stdin=json.dumps(payload).encode("utf-8"),
            timeout_message=f"Docker tool runner timed out after {self.timeout_seconds:.1f}s",
        )

        if returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()
            raise ToolInvocationProtocolError(f"Docker tool runner exited {returncode}: {err}")

        try:
            parsed = json.loads(stdout.decode("utf-8"))
        except json.JSONDecodeError as exc:
            text = stdout.decode("utf-8", errors="replace").strip()
            raise ToolInvocationProtocolError(
                f"Docker tool runner returned non-JSON stdout: {text!r}"
            ) from exc
        if not isinstance(parsed, dict):
            raise ToolInvocationProtocolError("Docker tool runner must return a JSON object")
        return cast(Mapping[str, Any], parsed)

    async def _resolve_local_image_id(self) -> str:
        argv = [
            *self._docker_command(),
            "inspect",
            "--format",
            "{{.Id}}",
            self.image,
        ]
        stdout, stderr, returncode = await self._communicate(
            argv=argv,
            stdin=b"",
            timeout_message=(
                f"Docker image inspection timed out after {self.timeout_seconds:.1f}s"
            ),
        )
        if returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()
            listed_id = await self._resolve_local_image_id_from_listing(inspect_error=err)
            if listed_id:
                return listed_id
            raise ToolInvocationProtocolError(
                f"Docker image {self.image!r} is not available locally: {err}"
            )

        image_id = stdout.decode("utf-8", errors="replace").strip()
        if not image_id:
            raise ToolInvocationProtocolError(
                f"Docker image {self.image!r} resolved to an empty image id"
            )
        return image_id

    async def _resolve_local_image_id_from_listing(self, *, inspect_error: str) -> str | None:
        argv = [
            *self._docker_command(),
            "images",
            "--format",
            "{{.Repository}}:{{.Tag}} {{.ID}}",
        ]
        stdout, stderr, returncode = await self._communicate(
            argv=argv,
            stdin=b"",
            timeout_message=(f"Docker image listing timed out after {self.timeout_seconds:.1f}s"),
        )
        if returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()
            raise ToolInvocationProtocolError(
                f"Docker image {self.image!r} is not available locally: "
                f"{inspect_error}; docker images failed: {err}"
            )
        for line in stdout.decode("utf-8", errors="replace").splitlines():
            reference, _sep, image_id = line.partition(" ")
            if reference == self.image and image_id.strip():
                return image_id.strip()
        return None

    async def _communicate(
        self,
        *,
        argv: Sequence[str],
        stdin: bytes,
        timeout_message: str,
    ) -> tuple[bytes, bytes, int]:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(stdin),
                timeout=self.timeout_seconds,
            )
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise ToolInvocationTimeoutError(timeout_message) from exc
        return stdout, stderr, proc.returncode or 0

    def _docker_command(self) -> tuple[str, ...]:
        if self.docker_command is not None:
            return tuple(self.docker_command)
        return (self.docker_binary,)

    def _runtime_args(self) -> tuple[str, ...]:
        if self.runtime is None:
            return ()
        return ("--runtime", self.runtime)


@dataclass(frozen=True)
class GVisorRunscToolRunnerExecutionDriver(DockerToolRunnerExecutionDriver):
    """Execute a TOOL_STEP through Docker's gVisor ``runsc`` runtime.

    This is the R-411 provider-class addition on the R-410 Docker runner
    substrate. Docker still supplies image/runtime orchestration, but the
    container body executes under the configured ``runsc`` runtime and the
    dispatcher must resolve a Tier-3 sandbox decision.
    """

    runtime: str | None = "runsc"
    required_tier: SandboxTier = SandboxTier.TIER_3_MICROVM
