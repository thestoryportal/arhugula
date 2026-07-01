"""`harness-runtime` — H_T composition root.

Per `CLAUDE.md` §3.3 and `design-substrate/Spec_Harness_Runtime_v1.md` v1.1,
this package owns runtime composition of the axis libraries:

- Bootstrap orchestration (9 stages per C-RT-01).
- Provider SDK lifecycle (F-P2-4; C-RT-05).
- TracerProvider lifecycle (F-P2-3; C-RT-06).
- In-process OTLP collector daemon supervision (F-P2-5; C-RT-07).
- Cross-axis seam wiring (C-RT-12; 24 phase-2-runtime edges).
- `run()` Python API (F-P2-2; C-RT-08).

Foundational package scaffold traceability lands here as U-RT-01: the runtime
workspace package is importable and exposes the composition-root public API.
Public API surface lands at L8 close (U-RT-42 — `run()` signature + `RunResult`
shape) and L9 (U-RT-43 — bootstrap orchestrator body).
"""

from __future__ import annotations

from harness_runtime.api import (
    ConcurrentRunNotSupported,
    FailureCause,
    HarnessDraining,
    InvalidWorkflowError,
    ResumeArgsError,
    ResumeHandleUnknownError,
    ResumeProtocolNotBoundError,
    ResumeStepIndexOutOfRangeError,
    ResumeWorkflowMismatchError,
    RunResult,
    WorkflowObject,
    resume,
    run,
)
from harness_runtime.bootstrap import (
    BootstrapFailure,
    BootstrapStageCompleteEvent,
    IncompleteBootstrapError,
    run_bootstrap,
)
from harness_runtime.drain import DrainPlatformError
from harness_runtime.shutdown import (
    AlreadyShutDown,
    FlushReport,
    FlushTimeoutError,
    ShutdownReport,
    ShutdownTimeout,
    flush_observability,
    shutdown,
)

__all__ = [
    "AlreadyShutDown",
    "BootstrapFailure",
    "BootstrapStageCompleteEvent",
    "ConcurrentRunNotSupported",
    "DrainPlatformError",
    "FailureCause",
    "FlushReport",
    "FlushTimeoutError",
    "HarnessDraining",
    "IncompleteBootstrapError",
    "InvalidWorkflowError",
    "ResumeArgsError",
    "ResumeHandleUnknownError",
    "ResumeProtocolNotBoundError",
    "ResumeStepIndexOutOfRangeError",
    "ResumeWorkflowMismatchError",
    "RunResult",
    "ShutdownReport",
    "ShutdownTimeout",
    "WorkflowObject",
    "flush_observability",
    "resume",
    "run",
    "run_bootstrap",
    "shutdown",
]
