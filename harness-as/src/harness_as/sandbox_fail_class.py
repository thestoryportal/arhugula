"""Sandbox-fail-class taxonomy + routing-posture metadata — U-AS-03.

Implements C-AS-04 §4.1 (sandbox.fail.class enum) + §4.2 (pre-HITL
escalation metadata) + §4.3 (sampling posture). Declares the 7-value
`SandboxFailClass` enum, the C5/C9 routing-posture enums, the per-class
metadata table, and the permanent-fail staircase-skip predicate.

Authority: Implementation_Plan_Action_Surface_v1.md §2 U-AS-03;
Spec_Action_Surface_v1.md §4 C-AS-04; ADR-D2 v1.1 §1.7.1 + §1.8.

C9 retry-posture is informational at this unit — the actual retry loop
lives in the CP-axis plan (acceptance #7).

v1.4 carrier-extension (per AS plan v1.4 §1) — additionally declares
`MCPInvocationFailClass` 4-value StrEnum per AS spec v1.6 §15.8 and the
`project_mcp_to_sandbox_fail_class` projection function per §15.10. The
MCP-protocol-layer taxonomy siblings the F4 process-execution-layer
taxonomy above; both compose on the `sandbox.violation` child span per
§15.9 dual-attribute emission discipline.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict


class SandboxFailClass(StrEnum):
    """The 7 sandbox-violation failure classes (C-AS-04 §4.1)."""

    ESCAPE_ATTEMPT = "escape_attempt"
    EGRESS_DENIED = "egress_denied"
    TIMEOUT = "timeout"
    OOM = "oom"
    SIGNAL = "signal"
    EXIT_NONZERO = "exit_nonzero"
    POLICY_OVERRIDE = "policy_override"


class C5FailClass(StrEnum):
    """C5 fail-classification of a sandbox failure (C-AS-04 §4.1)."""

    PERMANENT_FAIL = "PERMANENT_FAIL"
    TRANSIENT_FAIL = "TRANSIENT_FAIL"
    GATE_CONTRACT_DEPENDENT = "GATE_CONTRACT_DEPENDENT"
    INFORMATIONAL = "INFORMATIONAL"


class C9RetryPosture(StrEnum):
    """C9 retry posture for a sandbox failure (C-AS-04 §4.1; informational)."""

    NO_RETRY = "NO_RETRY"
    C9_BACKOFF_RETRY = "C9_BACKOFF_RETRY"
    PER_TOOL_RETRY_EXIT = "PER_TOOL_RETRY_EXIT"
    AUDIT_LEDGER_ONLY = "AUDIT_LEDGER_ONLY"


class SandboxFailClassMetadata(BaseModel):
    """Registered routing-posture metadata for one sandbox fail class."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fail_class: SandboxFailClass
    c5_classification: C5FailClass
    c9_retry_posture: C9RetryPosture
    skips_pre_hitl_staircase: bool
    always_sampled: bool
    tamper_evidence_relevant: bool


def _meta(
    fail_class: SandboxFailClass,
    c5: C5FailClass,
    c9: C9RetryPosture,
    *,
    skips: bool,
    tamper: bool,
) -> SandboxFailClassMetadata:
    # always_sampled is uniformly true across all classes (C-AS-04 §4.3).
    return SandboxFailClassMetadata(
        fail_class=fail_class,
        c5_classification=c5,
        c9_retry_posture=c9,
        skips_pre_hitl_staircase=skips,
        always_sampled=True,
        tamper_evidence_relevant=tamper,
    )


_FAIL_CLASS_METADATA: Mapping[SandboxFailClass, SandboxFailClassMetadata] = MappingProxyType(
    {
        SandboxFailClass.ESCAPE_ATTEMPT: _meta(
            SandboxFailClass.ESCAPE_ATTEMPT,
            C5FailClass.PERMANENT_FAIL,
            C9RetryPosture.NO_RETRY,
            skips=True,
            tamper=True,
        ),
        SandboxFailClass.EGRESS_DENIED: _meta(
            SandboxFailClass.EGRESS_DENIED,
            C5FailClass.PERMANENT_FAIL,
            C9RetryPosture.NO_RETRY,
            skips=True,
            tamper=False,
        ),
        SandboxFailClass.TIMEOUT: _meta(
            SandboxFailClass.TIMEOUT,
            C5FailClass.TRANSIENT_FAIL,
            C9RetryPosture.C9_BACKOFF_RETRY,
            skips=False,
            tamper=False,
        ),
        SandboxFailClass.OOM: _meta(
            SandboxFailClass.OOM,
            C5FailClass.TRANSIENT_FAIL,
            C9RetryPosture.C9_BACKOFF_RETRY,
            skips=False,
            tamper=False,
        ),
        SandboxFailClass.SIGNAL: _meta(
            SandboxFailClass.SIGNAL,
            C5FailClass.PERMANENT_FAIL,
            C9RetryPosture.NO_RETRY,
            skips=True,
            tamper=False,
        ),
        SandboxFailClass.EXIT_NONZERO: _meta(
            SandboxFailClass.EXIT_NONZERO,
            C5FailClass.GATE_CONTRACT_DEPENDENT,
            C9RetryPosture.PER_TOOL_RETRY_EXIT,
            skips=False,
            tamper=False,
        ),
        SandboxFailClass.POLICY_OVERRIDE: _meta(
            SandboxFailClass.POLICY_OVERRIDE,
            C5FailClass.INFORMATIONAL,
            C9RetryPosture.AUDIT_LEDGER_ONLY,
            skips=False,
            tamper=False,
        ),
    }
)


def fail_class_metadata(c: SandboxFailClass) -> SandboxFailClassMetadata:
    """Return the routing-posture metadata row for a sandbox fail class."""
    return _FAIL_CLASS_METADATA[c]


def permanent_fail_skips_staircase(c: SandboxFailClass) -> bool:
    """True for permanent-fail classes that skip the pre-HITL staircase.

    True exactly for ESCAPE_ATTEMPT, EGRESS_DENIED, SIGNAL (C-AS-04 §4.2).
    """
    return _FAIL_CLASS_METADATA[c].skips_pre_hitl_staircase


# --- MCP-protocol-layer fail-class (v1.4 carrier-extension; AS spec v1.6 §15.8) --


class MCPInvocationFailClass(StrEnum):
    """The 4 MCP-protocol-layer fail classes (C-AS-15 §15.8).

    Sibling to `SandboxFailClass` (F4 process-execution-layer at §4.1).
    Carries what FAILED at the MCP-protocol boundary; F4 carries what
    FAILED inside the sandboxed process. Both compose on the
    `sandbox.violation` child span per §15.9 dual-attribute emission.
    """

    TRANSPORT = "transport"
    PROTOCOL_ERROR = "protocol_error"
    SCHEMA_VIOLATION = "schema_violation"
    TIMEOUT = "timeout"


_MCP_TO_SANDBOX_PROJECTION: Mapping[MCPInvocationFailClass, SandboxFailClass] = MappingProxyType(
    {
        MCPInvocationFailClass.TRANSPORT: SandboxFailClass.EXIT_NONZERO,
        MCPInvocationFailClass.PROTOCOL_ERROR: SandboxFailClass.EXIT_NONZERO,
        MCPInvocationFailClass.SCHEMA_VIOLATION: SandboxFailClass.POLICY_OVERRIDE,
        MCPInvocationFailClass.TIMEOUT: SandboxFailClass.TIMEOUT,
    }
)


def project_mcp_to_sandbox_fail_class(
    mcp_fail_class: MCPInvocationFailClass,
) -> SandboxFailClass:
    """Best-effort projection MCP-shape → F4 process-shape per §15.10.

    Total over the 4-value MCPInvocationFailClass domain. Used at the
    dispatcher exception-handler binding to emit BOTH attributes from
    BOTH enums per §15.9 emission discipline (recommended default
    option (a) at §15.10).

    Projection is best-effort: §15.10 row 3
    (`schema_violation → policy_override`) is flagged HIGH semantic
    stretch in the spec itself. Future ADR-D2 / F4 enum revision arc
    MAY add a `contract_violation` value to absorb this projection
    cleanly.
    """
    return _MCP_TO_SANDBOX_PROJECTION[mcp_fail_class]
