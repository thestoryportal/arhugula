"""Foundational discriminator enums — U-AS-04.

Implements C-AS-10 §10.1 (MCP transport-level set). Declares `MCPTransport`,
the AS-owned transport / remote-trust discriminator, and re-exports the two
cross-cutting discriminator enums (`DeploymentSurface`, `PersonaTier`) from
`harness-core` so the AS-axis discriminator surface resolves from one module.

Authority: Implementation_Plan_Action_Surface_v1_2.md §2 U-AS-04 (R3-revised
body canonical at v1.1 §5.3 — declaration-site conversion);
Spec_Action_Surface_v1.md C-AS-10 §10.1; ADR-D2 v1.2.

R3 declaration-site conversion (A-1): the landed source declared local
`DeploymentSurface` / `PersonaTier` `StrEnum`s because no carrier existed.
Their values matched U-CORE-01 byte-exact; per the carrier map they are
cross-axis shared types, so the local declarations are deleted and the types
imported from `harness-core` (U-CORE-01). `MCPTransport` stays AS-owned.
Per Q-R3-6, downstream AS consumers carry their own explicit `[U-CORE-01]`
edges; this module's re-export is a convenience surface, not the dependency
path.
"""

from __future__ import annotations

from enum import StrEnum

from harness_core import DeploymentSurface, PersonaTier

__all__ = ["DeploymentSurface", "MCPTransport", "PersonaTier"]


class MCPTransport(StrEnum):
    """MCP transport / remote-trust level (C-AS-10 §10.1).

    Closed at cardinality 5 — adding a value requires a Workflow §4.1.2
    Class-2 ADR-D2 revision. Identifier strings byte-exact spec-canonical.
    """

    STDIO = "stdio"
    STREAMABLE_HTTP_L0_REFUSE = "streamable_http_l0"
    STREAMABLE_HTTP_L1_PINNED = "streamable_http_l1"
    STREAMABLE_HTTP_L2_SANDBOX = "streamable_http_l2"
    STREAMABLE_HTTP_L3_AUDIT = "streamable_http_l3"
