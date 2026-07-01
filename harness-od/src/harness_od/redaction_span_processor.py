"""C-OD-12 + C-OD-13 pre-collector redaction SpanProcessor.

H_T-OD-4 retirement substrate. Closes the redaction-discipline gap at
`harness-runtime/lifecycle/span_processor.py:materialize_span_processor_stage`
(previously stock `BatchSpanProcessor` only; zero redaction references) by
stripping content-bearing attributes from span attributes at OTel SpanProcessor
`on_end` BEFORE the `BatchSpanProcessor` buffer receives them.

**Composition** (per OTel canonical multi-processor pattern):

    provider.add_span_processor(RedactionSpanProcessor())   # fires first
    provider.add_span_processor(BatchSpanProcessor(...))    # fires after

`TracerProvider` invokes registered processors in registration order via its
internal `MultiSpanProcessor`. The redaction processor mutates the span's
attribute bag in place; the BSP then enqueues the (now-redacted) span for
export.

**Spec authority.** OD spec v1.2 §C-OD-12 §12.1 declares the 3-class attribute
partition (`DEFAULT_OFF_CONTENT` / `DEFAULT_ON_STRUCTURE` / `HASH_DIGEST_OF_CONTENT`);
content-bearing attributes are default-off at all 9 cells. OD spec v1.2
§C-OD-13 §13.2 mandates pre-collector redaction at the SDK / wrapper boundary
"BEFORE the BatchSpanProcessor buffer" at multi-tenant-compliance cells; this
processor is the canonical wiring of that mandate at the substrate layer.

**Default-off, with a solo-developer per-session toggle (§13.1 gate (a)).** The
processor enforces the §12.1 default-off discipline at all 3 persona tiers —
content-bearing attributes are stripped at `on_end` unless an override is in
scope. The §C-OD-13 §13.1 per-persona-tier override gradient (solo-developer
toggleable per session; team-binding non-toggleable; multi-tenant-compliance
pre-collector eval-grade pipeline) is captured at
`harness-od/src/harness_od/redaction_gradient.py:PER_PERSONA_TIER_REDACTION`.
The **solo-developer per-session toggle** (§13.1 "Per-session toggle at the
in-process collector configuration; default-off") is wired here via the
module-level `_SESSION_CONTENT_CAPTURE` ContextVar + the `session_content_capture()`
context manager: at `PersonaTier.SOLO_DEVELOPER` (`toggleable=True`) an operator
enables raw content capture for a session by wrapping the run; `on_end` then
skips the strip for spans ended in that context. team-binding +
multi-tenant-compliance (`toggleable=False`) IGNORE the override per §13.3
monotonic-tightening / downgrade-rejection — a stray enable can never relax
redaction at a tightened tier. The deployment-binding-time `persona_tier`
threads through `RuntimeConfig` to this processor's ctor (runtime spec v1.37 §3
C-RT-03); the per-session toggle is runtime/session-scoped, distinct from the
deployment-binding persona_tier per OD spec v1.26 §1.1. The specific toggle UX
(CLI flag vs config-file) is deferred to implementation discretion per §13.3.

**Strip-not-tokenize (MVP scope-lock).** OD spec v1.2 §C-OD-13 §13.2 declares
"any span emitted from the harness at a multi-tenant-compliance cell MUST have
content attributes either omitted entirely OR redacted to opaque tokens
BEFORE the span hands off to the BatchSpanProcessor." The MVP implements the
"omitted entirely" arm — keys in the redaction set are deleted from the span's
attribute bag. The opaque-token arm (e.g., `[REDACTED:PII]` / `[REDACTED:MCP_ARG]`)
is the eval-grade pipeline shape at multi-tenant-compliance cells per §C-OD-13
§13.2; eval-grade tokenization is a follow-on arc.

**Empirical posture at HEAD.** A grep of harness-{runtime,cp,as,od}/src
returns ZERO production `span.set_attribute(...)` calls against the 13 keys in
`DEFAULT_OFF_CONTENT_ATTRIBUTES` (the OTel GenAI semconv 1.41.0 Opt-In set
plus `mcp.tool.call.{arguments,result}` + `skill.body_content` + `memory.content`
+ `files.content`). This processor is therefore **defense-in-depth at HEAD**:
no observable behavior change on the spans currently emitted by the runtime;
any future producer that sets one of the 13 content-bearing keys will be
silently stripped by this processor before reaching the BSP buffer. The
substrate carrier `DEFAULT_OFF_CONTENT_ATTRIBUTES` is exported from
`harness-od/src/harness_od/content_structure_discipline.py` per C-OD-12 §12.1.

**OTel SpanProcessor lifecycle.** Per `opentelemetry.sdk.trace.SpanProcessor`:

  - `on_start(span, parent_context)` — fires at `Tracer.start_span`. No-op
    at this processor (redaction is at end-of-span; pre-start attributes are
    not yet set).
  - `on_end(span: ReadableSpan)` — fires at `span.end()`. Reads + mutates
    `span._attributes` (the OTel-Python `BoundedAttributes` carrier; mutable
    when the span is non-immutable — empirically `_immutable=False` at the
    on_end boundary).
  - `force_flush(timeout_millis)` — no-op (returns True). The BSP downstream
    is the only force-flushable processor in the chain.
  - `shutdown()` — no-op.

**Attribute-bag mutation discipline.** `span._attributes` is a Python OTel
SDK private API (no public mutation surface on `ReadableSpan`). The
implementation uses `del span._attributes[key]` per the canonical
OTel-Python redaction pattern (cf. opentelemetry-instrumentation contrib
sanitization processors). The deletion is wrapped in `try/except` to tolerate
unexpected immutable-bag states without raising at the SpanProcessor boundary
(an exception at `on_end` would propagate into the TracerProvider's span-end
path and disrupt downstream processors).

Authority anchors: OD spec v1.2 §C-OD-12 §12.1 (default-off content attribute
discipline) + §C-OD-13 §13.1 (per-persona-tier override gradient — captured
at `redaction_gradient.py`, NOT consumed at MVP) + §13.2 (pre-collector
redaction at SDK / wrapper boundary BEFORE BatchSpanProcessor buffer). Cross-
reference: `harness-od/src/harness_od/content_structure_discipline.py`
canonical declaration site of `DEFAULT_OFF_CONTENT_ATTRIBUTES`.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import TYPE_CHECKING

from harness_core import PersonaTier
from opentelemetry.context import Context
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor

from harness_od.content_structure_discipline import DEFAULT_OFF_CONTENT_ATTRIBUTES
from harness_od.redaction_gradient import PER_PERSONA_TIER_REDACTION
from harness_od.redaction_tokenizer import RedactionAttributeTokenizer

if TYPE_CHECKING:
    from collections.abc import Generator

__all__ = [
    "MultiTenantOverrideRefusedError",
    "RedactionSpanProcessor",
    "session_content_capture",
    "session_content_capture_enabled",
]


#: Per-session content-capture override (C-OD-13 §13.1 solo-developer row).
#: `default=False` encodes the §13.1 "default-off" posture — content capture is
#: OFF by default, so redaction applies. An operator ENABLES content capture for
#: a session by setting this `True` (via `session_content_capture`), honored
#: ONLY at the solo-developer tier (`toggleable=True`); team-binding +
#: multi-tenant-compliance IGNORE it (non-toggleable per §13.3 monotonic-
#: tightening / downgrade-rejection). Mirrors the per-session ContextVar
#: isolation idiom at `harness-runtime/.../lifecycle/mcp_server.py`
#: (`_CURRENT_TOOL_CTX`); propagates into the span-emitting task/context where
#: `on_end` fires synchronously at `span.end()`.
_SESSION_CONTENT_CAPTURE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "harness.od.session_content_capture",
    default=False,
)


@contextmanager
def session_content_capture(*, enabled: bool = True) -> Generator[None]:
    """Per-session content-capture toggle (C-OD-13 §13.1 solo-developer).

    The operator-facing mechanism for §13.1's "Per-session toggle at the
    in-process collector configuration; default-off" override at the
    solo-developer tier. Wrap a workflow run to ENABLE raw content capture
    (skip the §12.1 default-off strip) for that session::

        with session_content_capture():
            api.run(workflow)  # spans this run keep content at solo-developer

    On exit the prior value is restored (`ContextVar.reset`). The toggle is
    honored ONLY at `PersonaTier.SOLO_DEVELOPER`
    (`PER_PERSONA_TIER_REDACTION[...].toggleable=True`); team-binding +
    multi-tenant-compliance deployments IGNORE it and always redact, so a stray
    enable can never relax redaction at a non-toggleable tier (§13.3
    downgrade-rejection).

    The set propagates into the span-emitting execution context (same task, or
    a context-copied child task / `asyncio.to_thread`), so `on_end` — which
    fires synchronously at `span.end()` in that context — observes it.

    The specific surfacing of this toggle (CLI flag vs config-file) is the
    §13.3-deferred toggle UX; this context manager is the underlying mechanism
    over which any such UX composes.
    """
    token = _SESSION_CONTENT_CAPTURE.set(enabled)
    try:
        yield
    finally:
        _SESSION_CONTENT_CAPTURE.reset(token)


def session_content_capture_enabled() -> bool:
    """Return the current session's content-capture override (default `False`).

    Introspection helper reading the same `ContextVar` the processor consults
    at `on_end`. `True` only inside a `session_content_capture()` scope.
    """
    return _SESSION_CONTENT_CAPTURE.get()


class MultiTenantOverrideRefusedError(Exception):
    """Raised when an empty `redacted_attributes` is supplied at multi-tenant.

    Per OD spec §C-OD-13 §13.1 row 3 +
    `PER_PERSONA_TIER_REDACTION[MULTI_TENANT_COMPLIANCE].toggleable=False`:
    multi-tenant-compliance is non-toggleable; operator cannot disable
    redaction at this tier. Surfaces at processor construction; never at
    runtime.
    """


class RedactionSpanProcessor(SpanProcessor):
    """OTel SpanProcessor stripping content-bearing attributes at on_end.

    Registered BEFORE `BatchSpanProcessor` on the TracerProvider so that the
    BSP's buffered exporter receives the (already-redacted) span. The strip
    set defaults to OD spec C-OD-12 §12.1 13-attribute
    `DEFAULT_OFF_CONTENT_ATTRIBUTES` frozenset.

    Operator override at construction: pass `redacted_attributes=` with a
    custom frozenset to widen or narrow the strip surface. The default is
    the spec-canonical set; non-default constructions belong at deployment-
    binding-time per ADR-D6 v1.2 §1.4 redaction discipline.
    """

    def __init__(
        self,
        *,
        persona_tier: PersonaTier = PersonaTier.SOLO_DEVELOPER,
        redacted_attributes: frozenset[str] = DEFAULT_OFF_CONTENT_ATTRIBUTES,
        tokenizer: RedactionAttributeTokenizer | None = None,
    ) -> None:
        # OD spec §C-OD-13 §13.1 row 3 — multi-tenant-compliance is
        # non-toggleable; operator cannot disable redaction at this tier.
        # An empty `redacted_attributes` at multi-tenant is a disable attempt.
        posture = PER_PERSONA_TIER_REDACTION[persona_tier]
        if persona_tier == PersonaTier.MULTI_TENANT_COMPLIANCE and len(redacted_attributes) == 0:
            raise MultiTenantOverrideRefusedError(
                f"persona_tier={persona_tier.value} is non-toggleable per "
                f"PER_PERSONA_TIER_REDACTION[{persona_tier.value}].toggleable={posture.toggleable} "
                f"(OD spec §C-OD-13 §13.1 row 3); empty redacted_attributes "
                f"frozenset is rejected at construction. Re-pass the spec-canonical "
                f"DEFAULT_OFF_CONTENT_ATTRIBUTES (default) or a non-empty operator-tuned set."
            )
        self._persona_tier: PersonaTier = persona_tier
        self._redacted: frozenset[str] = redacted_attributes
        self._tokenizer = tokenizer

    @property
    def redacted_attributes(self) -> frozenset[str]:
        """The frozenset of attribute keys this processor strips at on_end."""
        return self._redacted

    @property
    def persona_tier(self) -> PersonaTier:
        """The deployment's persona tier, gating §13.1 toggleability semantics.

        Solo-developer + team-binding tiers permit operator override via the
        ctor `redacted_attributes` keyword (including the empty-frozenset
        disable path). Multi-tenant-compliance refuses the empty-frozenset
        disable at construction per `MultiTenantOverrideRefusedError`.
        """
        return self._persona_tier

    @property
    def tokenizer_enabled(self) -> bool:
        """Whether content attributes are tokenized instead of deleted."""
        return self._tokenizer is not None

    def on_start(
        self,
        span: Span,
        parent_context: Context | None = None,
    ) -> None:
        """No-op. Redaction fires at on_end; pre-start attrs are not yet set."""
        return None

    def on_end(self, span: ReadableSpan) -> None:
        """Strip content-bearing attributes from `span._attributes` in place.

        Mutates the OTel-Python `BoundedAttributes` carrier backing the
        span's attribute bag. Tolerates unexpected immutable-bag states via
        try/except to avoid raising at the SpanProcessor boundary (an
        exception here would disrupt downstream processors in the
        MultiSpanProcessor chain).
        """
        # §C-OD-13 §13.1 per-session content-capture toggle (solo-developer
        # ONLY). `PER_PERSONA_TIER_REDACTION[SOLO_DEVELOPER].toggleable=True`:
        # an operator may enable raw content capture per session via
        # `session_content_capture()`. team-binding + multi-tenant-compliance
        # are non-toggleable (§13.3 monotonic-tightening / downgrade-rejection)
        # — the override is IGNORED at those tiers, so a stray enable can never
        # relax redaction at a tightened tier. Default-off (`ContextVar`
        # default `False`) preserves the strip-at-all-tiers MVP behavior when
        # no override is in scope.
        if self._persona_tier == PersonaTier.SOLO_DEVELOPER and _SESSION_CONTENT_CAPTURE.get():
            return
        # OTel-Python exposes the span's attribute bag as `_attributes`, a
        # `BoundedAttributes` (`MutableMapping`) that is mutable when the span
        # is non-immutable. There is no public mutation surface on
        # `ReadableSpan`; the canonical OTel-Python redaction idiom mutates
        # this private carrier directly. Pyright sees only the `Mapping`
        # base type, not the `MutableMapping` runtime shape — suppress the
        # reportPrivateUsage + reportIndexIssue checks at the access site.
        attrs = span._attributes  # pyright: ignore[reportPrivateUsage]
        if attrs is None:
            return
        span_context = span.context
        trace_id = span_context.trace_id.to_bytes(16, "big").hex() if span_context else None
        span_id = span_context.span_id.to_bytes(8, "big").hex() if span_context else None
        for key in list(attrs.keys()):
            if key in self._redacted:
                try:
                    if self._tokenizer is None:
                        del attrs[key]  # pyright: ignore[reportIndexIssue]
                    else:
                        attrs[key] = self._tokenizer.tokenize(  # pyright: ignore[reportIndexIssue]
                            attribute_key=key,
                            raw_value=attrs[key],
                            trace_id=trace_id,
                            span_id=span_id,
                        )
                except (KeyError, TypeError):
                    # Immutable-bag fallback: span already frozen; skip
                    # silently rather than raise into the TracerProvider
                    # span-end path.
                    pass

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        """No-op. Returns True — there is no buffer at this processor.

        The downstream BatchSpanProcessor owns the export buffer; calling
        `TracerProvider.force_flush(...)` reaches BSP through the
        MultiSpanProcessor chain.
        """
        del timeout_millis
        return True

    def shutdown(self) -> None:
        """No-op. No resources held by this processor."""
        return None
