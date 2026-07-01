"""Allowlist-intersection access control — U-AS-22.

Implements C-AS-06 §6.1 (allowlist entry signature — consumed), §6.2 (access-
control composition). Declares the `AllowlistDecision` enum and the
`check_secret_allowlist` intersection function.

Authority: Implementation_Plan_Action_Surface_v1_1.md §5.3 U-AS-22 (CONFORM
unit — single Q-R3-3 option (a) micro-edit: `SecretAllowlistEntry` is consumed
from U-AS-07, not declared here; v1 base body at
Implementation_Plan_Action_Surface_v1.md §2 U-AS-22); Spec_Action_Surface_v1.md
§6 C-AS-06; ADR-F5 v1.1 §"Permanent tensions engaged".

Depends on: U-AS-07 (`SecretAllowlistEntry`, `ToolContract` — the
carrier-ordering fix homes `SecretAllowlistEntry` at U-AS-07; this unit
consumes it); U-AS-20 (`SecretScope`).

`required_secrets` is an orthogonal access-control dimension (§6.2 row 2): it
is NOT a fifth `max()` floor and does not enter the C-AS-02 sandbox-tier
composition — this module carries no `SandboxTier` logic.
"""

from __future__ import annotations

from enum import StrEnum

from harness_as.secret_fetch import SecretScope
from harness_as.tool_contract import SecretAllowlistEntry, ToolContract


class AllowlistDecision(StrEnum):
    """Secret-allowlist access-control verdict (C-AS-06 §6.2)."""

    PERMITTED = "PERMITTED"
    DENIED_NOT_IN_TOOL_ALLOWLIST = "DENIED_NOT_IN_TOOL_ALLOWLIST"
    DENIED_NOT_IN_OPERATOR_POLICY_OVERRIDE = "DENIED_NOT_IN_OPERATOR_POLICY_OVERRIDE"


def check_secret_allowlist(
    tool: ToolContract,
    requested_name: str,
    requested_scope: SecretScope,
    operator_policy_override: frozenset[SecretAllowlistEntry],
) -> AllowlistDecision:
    """Resolve a secret request against the tool x operator-policy allowlist.

    Per C-AS-06 §6.2 row 1: a `fetch_secret(name, scope, tier)` call succeeds
    only if `(name, scope)` is in `tool.required_secrets` **intersected with**
    the operator-policy override. The denial verdict identifies which set the
    request fell out of — the tool allowlist is checked first.
    """
    requested = SecretAllowlistEntry(name=requested_name, scope=requested_scope)
    if requested not in set(tool.required_secrets):
        return AllowlistDecision.DENIED_NOT_IN_TOOL_ALLOWLIST
    if requested not in operator_policy_override:
        return AllowlistDecision.DENIED_NOT_IN_OPERATOR_POLICY_OVERRIDE
    return AllowlistDecision.PERMITTED
