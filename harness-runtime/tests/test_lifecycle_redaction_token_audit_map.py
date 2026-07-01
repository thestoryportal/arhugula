"""R-008 OD-4 redaction-token audit-map persistence tests."""

from __future__ import annotations

from harness_od.redaction_tokenizer import OpaqueRedactionTokenizer
from harness_runtime.lifecycle.redaction_token_audit_map import AuditLedgerRedactionTokenMap


class _RecordingAuditWriter:
    def __init__(self) -> None:
        self.appended: list[tuple[str | None, object]] = []

    def append(self, tenant_id: str | None, audit_entry: object) -> object:
        self.appended.append((tenant_id, audit_entry))
        return "appended"


def test_audit_ledger_redaction_token_map_writes_signed_audit_entry() -> None:
    audit_writer = _RecordingAuditWriter()
    token_map = AuditLedgerRedactionTokenMap(
        audit_writer=audit_writer,
        tenant_id="tenant-r008",
        signing_key_id="redaction-token-test-key",
    )
    tokenizer = OpaqueRedactionTokenizer(token_map=token_map)

    token = tokenizer.tokenize(
        attribute_key="mcp.tool.call.arguments",
        raw_value='{"secret":"value"}',
        trace_id="trace-r008",
        span_id="span-r008",
    )

    assert len(audit_writer.appended) == 1
    tenant_id, audit_entry = audit_writer.appended[0]
    assert tenant_id == "tenant-r008"
    attrs = audit_entry.payload.audit_namespace_attrs
    assert attrs["audit.redaction_token.token"] == token
    assert attrs["audit.redaction_token.attribute_key"] == "mcp.tool.call.arguments"
    assert attrs["audit.redaction_token.raw_value"] == '{"secret":"value"}'
    assert attrs["audit.redaction_token.trace_id"] == "trace-r008"
    assert attrs["audit.redaction_token.span_id"] == "span-r008"
    assert audit_entry.signature_attrs.audit_signature_key_id == "redaction-token-test-key"
