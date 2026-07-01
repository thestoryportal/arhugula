"""Tests for U-CP-69 — C-CP-27 MCPClientNamespaceEmitter.emit_mcp_call_span().

ACs from CP plan v2.15 §1 U-CP-69 (preserved at v2.17):
  AC #1 Mutates `mcp.tool.call` span with all 7 attributes per C-AS-14 §14.3
  AC #2 `mcp.transport` value populates correctly per per-server config
        (stdio / streamable_http / sse)
  AC #3 `mcp.auth_present` reflects actual auth state (False on STDIO;
        transport-config-driven elsewhere)
  AC #4 `mcp.primitive.signature.sha256` is content-addressable per-primitive
  AC #5 Unit test: emit + verify all 7 attributes via OTel test collector
"""

from __future__ import annotations

import pytest
from harness_cp.cp_shared_types import MCPTrustTier
from harness_cp.mcp_client_namespace_emitter import (
    ATTR_MCP_AUTH_PRESENT,
    ATTR_MCP_PRIMITIVE_KIND,
    ATTR_MCP_PRIMITIVE_SIGNATURE_SHA256,
    ATTR_MCP_PROTOCOL_VERSION,
    ATTR_MCP_SERVER_NAME,
    ATTR_MCP_SERVER_TRUST_TIER,
    ATTR_MCP_TRANSPORT,
    MCP_CALL_SPAN_NAME,
    MCPClientNamespaceEmitter,
    MCPServerInfo,
)
from harness_cp.per_server_trust_types import MCPPrimitive
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)


@pytest.fixture
def exporter_and_provider() -> tuple[InMemorySpanExporter, TracerProvider]:
    """Per-test isolated TracerProvider + in-memory exporter."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter, provider


def _info_lookup_factory(info: MCPServerInfo):
    def _lookup(server_name: str) -> MCPServerInfo:
        return info

    return _lookup


def _get_exported_attrs(
    exporter: InMemorySpanExporter,
) -> dict[str, object]:
    spans = exporter.get_finished_spans()
    assert len(spans) == 1, f"expected 1 span, got {len(spans)}"
    span = spans[0]
    assert isinstance(span, ReadableSpan)
    return dict(span.attributes or {})


# ---------------------------------------------------------------------------
# AC #1 + AC #5 — all 7 attributes mutate span via OTel test collector
# ---------------------------------------------------------------------------


def test_emit_writes_all_seven_attributes(
    exporter_and_provider: tuple[InMemorySpanExporter, TracerProvider],
) -> None:
    """AC #1 + AC #5 — span carries all 7 mcp.* attributes after emit."""
    exporter, provider = exporter_and_provider
    info = MCPServerInfo(
        transport="stdio",
        protocol_version="2025-06-18",
        auth_present=False,
        trust_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
    )
    emitter = MCPClientNamespaceEmitter(info_lookup=_info_lookup_factory(info))
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span(MCP_CALL_SPAN_NAME) as span:
        emitter.emit_mcp_call_span(
            span,
            server_name="srv-a",
            primitive=MCPPrimitive.TOOL,
            signature_hash="b" * 64,
        )
    attrs = _get_exported_attrs(exporter)
    assert attrs == {
        ATTR_MCP_SERVER_NAME: "srv-a",
        ATTR_MCP_SERVER_TRUST_TIER: "level-2-sandbox-all",
        ATTR_MCP_PROTOCOL_VERSION: "2025-06-18",
        ATTR_MCP_TRANSPORT: "stdio",
        ATTR_MCP_AUTH_PRESENT: False,
        ATTR_MCP_PRIMITIVE_KIND: "tool",
        ATTR_MCP_PRIMITIVE_SIGNATURE_SHA256: "b" * 64,
    }


def test_attribute_names_byte_exact_per_as_spec_14_3(
    exporter_and_provider: tuple[InMemorySpanExporter, TracerProvider],
) -> None:
    """AC #1 — attribute name constants match C-AS-14 §14.3 verbatim."""
    assert ATTR_MCP_SERVER_NAME == "mcp.server.name"
    assert ATTR_MCP_SERVER_TRUST_TIER == "mcp.server.trust_tier"
    assert ATTR_MCP_PROTOCOL_VERSION == "mcp.protocol_version"
    assert ATTR_MCP_TRANSPORT == "mcp.transport"
    assert ATTR_MCP_AUTH_PRESENT == "mcp.auth_present"
    assert ATTR_MCP_PRIMITIVE_KIND == "mcp.primitive.kind"
    assert ATTR_MCP_PRIMITIVE_SIGNATURE_SHA256 == "mcp.primitive.signature.sha256"


# ---------------------------------------------------------------------------
# AC #2 — transport values: stdio / streamable_http / sse
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("transport", ["stdio", "streamable_http", "sse"])
def test_transport_value_passes_through(
    transport: str,
    exporter_and_provider: tuple[InMemorySpanExporter, TracerProvider],
) -> None:
    """AC #2 — each of the 3 transports (per per-server config) emits verbatim."""
    exporter, provider = exporter_and_provider
    info = MCPServerInfo(
        transport=transport,
        protocol_version="2025-06-18",
        auth_present=transport != "stdio",
        trust_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
    )
    emitter = MCPClientNamespaceEmitter(info_lookup=_info_lookup_factory(info))
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span(MCP_CALL_SPAN_NAME) as span:
        emitter.emit_mcp_call_span(
            span,
            server_name="srv",
            primitive=MCPPrimitive.RESOURCE,
            signature_hash="c" * 64,
        )
    attrs = _get_exported_attrs(exporter)
    assert attrs[ATTR_MCP_TRANSPORT] == transport


# ---------------------------------------------------------------------------
# AC #3 — auth_present discipline (False on STDIO; config-driven elsewhere)
# ---------------------------------------------------------------------------


def test_auth_present_false_on_stdio(
    exporter_and_provider: tuple[InMemorySpanExporter, TracerProvider],
) -> None:
    """AC #3 — STDIO config emits auth_present=False."""
    exporter, provider = exporter_and_provider
    info = MCPServerInfo(
        transport="stdio",
        protocol_version="2025-06-18",
        auth_present=False,
        trust_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
    )
    emitter = MCPClientNamespaceEmitter(info_lookup=_info_lookup_factory(info))
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span(MCP_CALL_SPAN_NAME) as span:
        emitter.emit_mcp_call_span(
            span,
            server_name="local-stdio",
            primitive=MCPPrimitive.TOOL,
            signature_hash="d" * 64,
        )
    attrs = _get_exported_attrs(exporter)
    assert attrs[ATTR_MCP_AUTH_PRESENT] is False


def test_auth_present_true_on_authed_remote(
    exporter_and_provider: tuple[InMemorySpanExporter, TracerProvider],
) -> None:
    """AC #3 — non-STDIO transport with auth config emits auth_present=True."""
    exporter, provider = exporter_and_provider
    info = MCPServerInfo(
        transport="streamable_http",
        protocol_version="2025-06-18",
        auth_present=True,
        trust_tier=MCPTrustTier.LEVEL_3_ALLOW_WITH_AUDIT,
    )
    emitter = MCPClientNamespaceEmitter(info_lookup=_info_lookup_factory(info))
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span(MCP_CALL_SPAN_NAME) as span:
        emitter.emit_mcp_call_span(
            span,
            server_name="auth-srv",
            primitive=MCPPrimitive.TOOL,
            signature_hash="e" * 64,
        )
    attrs = _get_exported_attrs(exporter)
    assert attrs[ATTR_MCP_AUTH_PRESENT] is True


# ---------------------------------------------------------------------------
# AC #4 — signature.sha256 is per-primitive content-addressable
# ---------------------------------------------------------------------------


def test_signature_hash_emits_per_call_value(
    exporter_and_provider: tuple[InMemorySpanExporter, TracerProvider],
) -> None:
    """AC #4 — caller-supplied per-primitive sha256 emits verbatim."""
    exporter, provider = exporter_and_provider
    info = MCPServerInfo(
        transport="stdio",
        protocol_version="2025-06-18",
        auth_present=False,
        trust_tier=MCPTrustTier.LEVEL_2_SANDBOX_ALL,
    )
    emitter = MCPClientNamespaceEmitter(info_lookup=_info_lookup_factory(info))
    tracer = provider.get_tracer("test")
    sig = "f" * 64
    with tracer.start_as_current_span(MCP_CALL_SPAN_NAME) as span:
        emitter.emit_mcp_call_span(
            span,
            server_name="srv",
            primitive=MCPPrimitive.PROMPT,
            signature_hash=sig,
        )
    attrs = _get_exported_attrs(exporter)
    assert attrs[ATTR_MCP_PRIMITIVE_SIGNATURE_SHA256] == sig


# ---------------------------------------------------------------------------
# Default lookup discipline — raises when operator omits injection
# ---------------------------------------------------------------------------


def test_default_lookup_raises_lookup_error(
    exporter_and_provider: tuple[InMemorySpanExporter, TracerProvider],
) -> None:
    """Default lookup raises LookupError — operator MUST inject at bootstrap."""
    _exporter, provider = exporter_and_provider
    emitter = MCPClientNamespaceEmitter()  # no info_lookup injected
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span(MCP_CALL_SPAN_NAME) as span:
        with pytest.raises(LookupError, match="MCPServerInfoLookup"):
            emitter.emit_mcp_call_span(
                span,
                server_name="anyone",
                primitive=MCPPrimitive.TOOL,
                signature_hash="0" * 64,
            )
