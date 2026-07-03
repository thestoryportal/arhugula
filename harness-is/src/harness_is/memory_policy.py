"""Memory policy model - U-MEM-04.

Implements C-MEM-09's provider-neutral policy vocabulary, default-disabled
policy document, and fail-closed resolver for capture, promotion, retrieval,
injection, native-provider memory, and standard memory-tool exposure.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from harness_is.memory_record_envelope import MemoryRecordKind, MemoryScope, MemoryVisibility


class CaptureDecision(StrEnum):
    """Capture fidelity decisions declared by C-MEM-09."""

    DENY = "deny"
    SUMMARIZE_ONLY = "summarize_only"
    CAPTURE_FULL = "capture_full"
    CAPTURE_REDACTED = "capture_redacted"


class PromotionDecision(StrEnum):
    """Promotion decisions declared by C-MEM-09."""

    DISCARD = "discard"
    KEEP_EPISODIC = "keep_episodic"
    PROPOSE_SEMANTIC = "propose_semantic"
    PROMOTE_SEMANTIC = "promote_semantic"
    PROPOSE_PROCEDURAL = "propose_procedural"
    PROMOTE_PROCEDURAL = "promote_procedural"


class AccessDecision(StrEnum):
    """Memory retrieval/injection/tool/native access decisions declared by C-MEM-09."""

    DENY = "deny"
    RETRIEVAL_ONLY = "retrieval_only"
    PROMPT_PACKET = "prompt_packet"
    STANDARD_TOOLS = "standard_tools"
    NATIVE_PROVIDER = "native_provider"


class ReviewMode(StrEnum):
    """Review modes declared by C-MEM-09."""

    AUTOMATIC = "automatic"
    OPERATOR_REQUIRED = "operator_required"
    FORBIDDEN = "forbidden"


class RetentionDecision(StrEnum):
    """Retention actions for policy-controlled memory records."""

    RETAIN = "retain"
    EXPIRE = "expire"
    PRUNE = "prune"
    TOMBSTONE = "tombstone"


class RedactionDecision(StrEnum):
    """Redaction actions for sensitive memory content."""

    NONE = "none"
    REDACT = "redact"
    TOMBSTONE = "tombstone"


def _empty_record_kind_filter() -> tuple[MemoryRecordKind, ...]:
    return ()


class MemoryPolicyDocument(BaseModel):
    """C-MEM-09 policy document.

    ``enabled=False`` is the compatibility-preserving default. The resolver
    treats disabled policies as deny-all for access/capture and discard for
    promotion even if a caller accidentally populates permissive fields.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["memory-policy/v1"] = "memory-policy/v1"
    policy_id: str
    enabled: bool = False
    capture_decision: CaptureDecision = CaptureDecision.DENY
    promotion_decision: PromotionDecision = PromotionDecision.DISCARD
    retrieval_access: AccessDecision = AccessDecision.DENY
    injection_access: AccessDecision = AccessDecision.DENY
    native_memory_access: AccessDecision = AccessDecision.DENY
    standard_tool_access: AccessDecision = AccessDecision.DENY
    review_mode: ReviewMode = ReviewMode.FORBIDDEN
    retention_decision: RetentionDecision = RetentionDecision.RETAIN
    redaction_decision: RedactionDecision = RedactionDecision.NONE
    eligible_record_kinds: tuple[MemoryRecordKind, ...] = Field(
        default_factory=_empty_record_kind_filter
    )


DEFAULT_DISABLED_MEMORY_POLICY = MemoryPolicyDocument(policy_id="default-disabled")
"""Default no-memory policy preserving existing runtime behavior."""


class MemoryCaptureResolution(BaseModel):
    """Resolved capture policy decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capture_decision: CaptureDecision
    review_mode: ReviewMode
    retention_decision: RetentionDecision
    redaction_decision: RedactionDecision
    failure_reason: str | None = None


class MemoryPromotionResolution(BaseModel):
    """Resolved promotion policy decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    promotion_decision: PromotionDecision
    review_mode: ReviewMode
    failure_reason: str | None = None


class MemoryAccessResolution(BaseModel):
    """Resolved retrieval/injection/tool/native access policy decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    access_decision: AccessDecision
    review_mode: ReviewMode
    failure_reason: str | None = None


MemoryPolicySource = Callable[[], MemoryPolicyDocument | dict[str, object]]
"""Lazy policy source used by the resolver boundary."""


_VISIBILITY_RANK = {
    MemoryVisibility.PRIVATE: 0,
    MemoryVisibility.WORKFLOW: 1,
    MemoryVisibility.PROJECT: 2,
    MemoryVisibility.TENANT: 3,
    MemoryVisibility.PUBLIC: 4,
}


class MemoryPolicyResolver:
    """Resolve C-MEM-09 decisions with default-off and fail-closed behavior."""

    def __init__(
        self,
        policy: MemoryPolicyDocument | None = None,
        *,
        policy_source: MemoryPolicySource | None = None,
    ) -> None:
        if policy is not None and policy_source is not None:
            raise ValueError("pass either policy or policy_source, not both")
        self._policy = policy or DEFAULT_DISABLED_MEMORY_POLICY
        self._policy_source = policy_source

    def resolve_capture(self) -> MemoryCaptureResolution:
        """Resolve capture fidelity; failures deny capture by default."""

        try:
            policy = self._load_policy()
        except Exception as exc:
            return MemoryCaptureResolution(
                capture_decision=CaptureDecision.DENY,
                review_mode=ReviewMode.FORBIDDEN,
                retention_decision=RetentionDecision.RETAIN,
                redaction_decision=RedactionDecision.NONE,
                failure_reason=_failure_reason(exc),
            )
        if not policy.enabled:
            return MemoryCaptureResolution(
                capture_decision=CaptureDecision.DENY,
                review_mode=ReviewMode.FORBIDDEN,
                retention_decision=policy.retention_decision,
                redaction_decision=policy.redaction_decision,
            )
        return MemoryCaptureResolution(
            capture_decision=policy.capture_decision,
            review_mode=policy.review_mode,
            retention_decision=policy.retention_decision,
            redaction_decision=policy.redaction_decision,
        )

    def resolve_promotion(self) -> MemoryPromotionResolution:
        """Resolve promotion; policy-source failures fail closed to discard."""

        try:
            policy = self._load_policy()
        except Exception as exc:
            return MemoryPromotionResolution(
                promotion_decision=PromotionDecision.DISCARD,
                review_mode=ReviewMode.FORBIDDEN,
                failure_reason=_failure_reason(exc),
            )
        if not policy.enabled:
            return MemoryPromotionResolution(
                promotion_decision=PromotionDecision.DISCARD,
                review_mode=ReviewMode.FORBIDDEN,
            )
        return MemoryPromotionResolution(
            promotion_decision=policy.promotion_decision,
            review_mode=policy.review_mode,
        )

    def resolve_retrieval(
        self,
        *,
        record_kind: MemoryRecordKind | None = None,
        record_scope: MemoryScope | None = None,
        requested_scope: MemoryScope | None = None,
    ) -> MemoryAccessResolution:
        """Resolve retrieval access for an optional record kind and scope."""

        return self._resolve_access(
            configured_access=lambda policy: policy.retrieval_access,
            record_kind=record_kind,
            record_scope=record_scope,
            requested_scope=requested_scope,
            enforce_scope=True,
        )

    def resolve_injection(
        self,
        *,
        record_kind: MemoryRecordKind | None = None,
        record_scope: MemoryScope | None = None,
        requested_scope: MemoryScope | None = None,
    ) -> MemoryAccessResolution:
        """Resolve injection access without permitting broader-than-record scope."""

        return self._resolve_access(
            configured_access=lambda policy: policy.injection_access,
            record_kind=record_kind,
            record_scope=record_scope,
            requested_scope=requested_scope,
            enforce_scope=True,
        )

    def resolve_native_memory(self) -> MemoryAccessResolution:
        """Resolve provider-native memory exposure; only native_provider can allow it."""

        result = self._resolve_access(
            configured_access=lambda policy: policy.native_memory_access,
            record_kind=None,
            record_scope=None,
            requested_scope=None,
            enforce_scope=False,
        )
        if result.access_decision is not AccessDecision.NATIVE_PROVIDER:
            return result.model_copy(update={"access_decision": AccessDecision.DENY})
        return result

    def resolve_standard_tools(self) -> MemoryAccessResolution:
        """Resolve standard memory-tool exposure; only standard_tools can allow it."""

        result = self._resolve_access(
            configured_access=lambda policy: policy.standard_tool_access,
            record_kind=None,
            record_scope=None,
            requested_scope=None,
            enforce_scope=False,
        )
        if result.access_decision is not AccessDecision.STANDARD_TOOLS:
            return result.model_copy(update={"access_decision": AccessDecision.DENY})
        return result

    def _resolve_access(
        self,
        *,
        configured_access: Callable[[MemoryPolicyDocument], AccessDecision],
        record_kind: MemoryRecordKind | None,
        record_scope: MemoryScope | None,
        requested_scope: MemoryScope | None,
        enforce_scope: bool,
    ) -> MemoryAccessResolution:
        try:
            policy = self._load_policy()
        except Exception as exc:
            return MemoryAccessResolution(
                access_decision=AccessDecision.DENY,
                review_mode=ReviewMode.FORBIDDEN,
                failure_reason=_failure_reason(exc),
            )
        if not policy.enabled:
            return MemoryAccessResolution(
                access_decision=AccessDecision.DENY,
                review_mode=ReviewMode.FORBIDDEN,
            )
        if not _record_kind_allowed(policy, record_kind):
            return MemoryAccessResolution(
                access_decision=AccessDecision.DENY,
                review_mode=policy.review_mode,
            )
        if enforce_scope and not _scope_not_broader(
            record_scope=record_scope,
            requested_scope=requested_scope,
        ):
            return MemoryAccessResolution(
                access_decision=AccessDecision.DENY,
                review_mode=policy.review_mode,
            )
        return MemoryAccessResolution(
            access_decision=configured_access(policy),
            review_mode=policy.review_mode,
        )

    def _load_policy(self) -> MemoryPolicyDocument:
        if self._policy_source is None:
            return self._policy
        raw_policy = self._policy_source()
        if isinstance(raw_policy, MemoryPolicyDocument):
            return raw_policy
        return MemoryPolicyDocument.model_validate(raw_policy)


def _record_kind_allowed(
    policy: MemoryPolicyDocument,
    record_kind: MemoryRecordKind | None,
) -> bool:
    if record_kind is None:
        return True
    return not policy.eligible_record_kinds or record_kind in policy.eligible_record_kinds


def _scope_not_broader(
    *,
    record_scope: MemoryScope | None,
    requested_scope: MemoryScope | None,
) -> bool:
    if record_scope is None and requested_scope is None:
        return False
    if record_scope is None or requested_scope is None:
        return False
    if _VISIBILITY_RANK[requested_scope.visibility] > _VISIBILITY_RANK[record_scope.visibility]:
        return False
    for field_name in (
        "project",
        "workflow",
        "workload_class",
        "provider_family",
        "cli_profile",
        "tenant",
    ):
        record_value = getattr(record_scope, field_name)
        requested_value = getattr(requested_scope, field_name)
        if record_value is not None and requested_value != record_value:
            return False
    return True


def _failure_reason(exc: Exception) -> str:
    message = str(exc)
    return message if message else type(exc).__name__


__all__ = [
    "DEFAULT_DISABLED_MEMORY_POLICY",
    "AccessDecision",
    "CaptureDecision",
    "MemoryAccessResolution",
    "MemoryCaptureResolution",
    "MemoryPolicyDocument",
    "MemoryPolicyResolver",
    "MemoryPolicySource",
    "MemoryPromotionResolution",
    "PromotionDecision",
    "RedactionDecision",
    "RetentionDecision",
    "ReviewMode",
]
