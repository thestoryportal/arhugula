"""R-008 OD-4 opaque redaction token substrate.

This module provides the provider-free OD substrate for the tokenization arm of
C-OD-13 §13.2: content-bearing span attributes can be replaced with opaque
tokens while the raw value is held behind a token-map sink interface.

The default tokenizer remains category-neutral (`CONTENT`). Runtime callers may
opt into the provider-free eval-grade classifier for category labels such as
`PII`, `MCP_ARG`, and `GENAI_PROMPT` without making provider calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import count
from threading import Lock
from typing import Protocol

__all__ = [
    "DeterministicRedactionClassifier",
    "EvalGradeSemanticRedactionClassifier",
    "InMemoryRedactionTokenMap",
    "OpaqueRedactionTokenizer",
    "RedactionAttributeClassification",
    "RedactionAttributeClassifier",
    "RedactionAttributeTokenizer",
    "RedactionTokenMap",
    "RedactionTokenRecord",
]

_CATEGORY_SAFE_CHARS = re.compile(r"[^A-Z0-9_]+")
_EMAIL_PATTERN = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_SSN_PATTERN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_SECRET_PATTERN = re.compile(
    r"\b(?:api[_-]?key|access[_-]?token|secret|password)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class RedactionTokenRecord:
    """One token-to-raw-value mapping captured before span export."""

    token: str
    attribute_key: str
    raw_value: object
    trace_id: str | None
    span_id: str | None
    semantic_category: str = "CONTENT"


@dataclass(frozen=True, slots=True)
class RedactionAttributeClassification:
    """Semantic category for one content attribute before tokenization."""

    category: str


class RedactionAttributeClassifier(Protocol):
    """Classify one content attribute for category-specific token labels."""

    def classify(
        self,
        *,
        attribute_key: str,
        raw_value: object,
    ) -> RedactionAttributeClassification:
        """Return the category to encode in the redaction token."""
        ...


def _normalize_category(category: str) -> str:
    normalized = _CATEGORY_SAFE_CHARS.sub("_", category.upper()).strip("_")
    return normalized or "CONTENT"


class DeterministicRedactionClassifier:
    """Backward-compatible alias for the provider-free eval-grade classifier."""

    def __init__(self) -> None:
        self._classifier = EvalGradeSemanticRedactionClassifier()

    def classify(
        self,
        *,
        attribute_key: str,
        raw_value: object,
    ) -> RedactionAttributeClassification:
        """Classify by explicit attribute shape first, then content cues."""
        return self._classifier.classify(attribute_key=attribute_key, raw_value=raw_value)


class EvalGradeSemanticRedactionClassifier:
    """Provider-free eval-grade classifier for category-specific tokens.

    This is deliberately small and auditable. It is not a replacement for a
    provider model; it is the runtime-safe eval classifier used at the
    pre-collector redaction boundary. Attribute semantics win over raw-content
    cues so sensitive tool/file/memory surfaces keep stable category labels
    even when the raw payload is unstructured.
    """

    def classify(
        self,
        *,
        attribute_key: str,
        raw_value: object,
    ) -> RedactionAttributeClassification:
        """Classify by explicit attribute shape first, then simple content cues."""
        if attribute_key in {"mcp.tool.call.arguments", "gen_ai.tool.call.arguments"}:
            return RedactionAttributeClassification("MCP_ARG")
        if attribute_key in {"mcp.tool.call.result", "gen_ai.tool.call.result"}:
            return RedactionAttributeClassification("TOOL_RESULT")
        if attribute_key == "files.content":
            return RedactionAttributeClassification("FILE")
        if attribute_key == "memory.content":
            return RedactionAttributeClassification("MEMORY")
        if attribute_key == "skill.body_content":
            return RedactionAttributeClassification("SKILL")
        if attribute_key in {"gen_ai.input.messages", "gen_ai.system_instructions"}:
            return self._classify_text(raw_value, fallback="GENAI_PROMPT")
        if attribute_key == "gen_ai.output.messages":
            return self._classify_text(raw_value, fallback="GENAI_RESPONSE")
        if attribute_key in {"gen_ai.retrieval.documents", "gen_ai.retrieval.query.text"}:
            return self._classify_text(raw_value, fallback="RETRIEVAL_CONTENT")
        return self._classify_text(raw_value, fallback="CONTENT")

    def _classify_text(
        self,
        raw_value: object,
        *,
        fallback: str,
    ) -> RedactionAttributeClassification:
        """Classify raw text cues while preserving a semantic fallback."""
        raw_text = str(raw_value)
        if _SECRET_PATTERN.search(raw_text):
            return RedactionAttributeClassification("SECRET")
        if _SSN_PATTERN.search(raw_text) or _EMAIL_PATTERN.search(raw_text):
            return RedactionAttributeClassification("PII")
        return RedactionAttributeClassification(fallback)


class RedactionTokenMap(Protocol):
    """Sink for token-to-raw-value mappings.

    Production can back this with a durable audit-ledger writer. Tests and
    provider-free local flows can use `InMemoryRedactionTokenMap`.
    """

    def append(self, record: RedactionTokenRecord) -> None:
        """Persist one token mapping."""


class RedactionAttributeTokenizer(Protocol):
    """Protocol consumed by `RedactionSpanProcessor` token mode."""

    def tokenize(
        self,
        *,
        attribute_key: str,
        raw_value: object,
        trace_id: str | None,
        span_id: str | None,
    ) -> str:
        """Return an opaque replacement token for a content attribute."""
        ...


class InMemoryRedactionTokenMap:
    """Provider-free token-map sink for tests and local wiring probes."""

    def __init__(self) -> None:
        self._records: list[RedactionTokenRecord] = []

    @property
    def records(self) -> tuple[RedactionTokenRecord, ...]:
        """Captured token-map records in append order."""
        return tuple(self._records)

    def append(self, record: RedactionTokenRecord) -> None:
        """Capture one token-map record."""
        self._records.append(record)


class OpaqueRedactionTokenizer:
    """Replace raw content with opaque, non-semantic placeholders.

    Tokens are intentionally per-record unique. The placeholder does not encode
    the raw value, attribute key, trace id, or span id; those details live only
    in the configured `RedactionTokenMap`.
    """

    def __init__(
        self,
        *,
        token_map: RedactionTokenMap,
        token_prefix: str = "CONTENT",
        classifier: RedactionAttributeClassifier | None = None,
    ) -> None:
        self._token_map = token_map
        self._token_prefix = token_prefix
        self._classifier = classifier
        self._counter = count(1)
        self._lock = Lock()

    def tokenize(
        self,
        *,
        attribute_key: str,
        raw_value: object,
        trace_id: str | None,
        span_id: str | None,
    ) -> str:
        """Create an opaque token and append its raw mapping to the sink."""
        category = self._token_prefix
        if self._classifier is not None:
            category = self._classifier.classify(
                attribute_key=attribute_key,
                raw_value=raw_value,
            ).category
        category = _normalize_category(category)
        with self._lock:
            token_ordinal = next(self._counter)
        token = f"[REDACTED:{category}:{token_ordinal:012x}]"
        self._token_map.append(
            RedactionTokenRecord(
                token=token,
                semantic_category=category,
                attribute_key=attribute_key,
                raw_value=raw_value,
                trace_id=trace_id,
                span_id=span_id,
            )
        )
        return token
