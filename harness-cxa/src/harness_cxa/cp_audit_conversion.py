"""CP→OD audit-write converter — production CXA seam (U-RT-59 Fork 2 close
+ U-CP-72 10-CP-D 6-prefix extension).

Production-grade converter materializing the typed CP→OD audit-write seam
declared at `Cross_Axis_Composition_Document_v2_8.md` §2.3.7 (extended via
the U-CP-72 6-prefix dispatch at 10-CP-D cluster). The converter contract
is at `Spec_Control_Plane_v1_7.md` §13.5.1 + the OD-side recognition
discipline at `Spec_Operational_Discipline_v1_5.md` C-OD-24.6. Wired at the
sub-agent dispatch composer per `Spec_Harness_Runtime_v1.md` v1.7 §14.7.2
step 8c + (future) the validator + webhook + operator-burden + per-server-
trust composers as they wire audit emission.

**U-CP-72 prefix-dispatch extension (10-CP-D, 2026-05-22).** Per the v1.16
spec contract surface + Implementation_Plan_Control_Plane_v2_15.md U-CP-72,
the converter signature widens from a single `CPAuditLedgerEntry` input to
a tagged union over 5 carrier types (the existing CP-side carrier plus 4
producer-specific AuditPayload subclasses). Dispatch routes on isinstance.
7 of 8 plan-mandated branches land post-Sub-arc-A (`dispatch:` + `hitl:` via
CPAuditLedgerEntry; `hitl_webhook:` + `operator_burden:` + `validator:` +
`mcp_trust:` + **`pause:`/`resume:` via PauseResumeAuditPayload (un-STRUCK at
U-OD-51 landing 2026-05-23 per Sub-arc A of `[[fork-u-cp-72-cost-and-pause-
resume-prefix-gap]]` §2.1 routing target (a))**). 1 prefix (`cost:`) remains
STRUCK per `[[halt-route-split-AC-pattern]]` — see
`.harness/class_1_fork_u_cp_72_cost_and_pause_resume_prefix_gap.md`
§2.2 (Sub-arc B: CostRecordAuditPayload authoring + CXA v2.9 amendment owed).

**Home rationale (Q5 ratification, 2026-05-20).** Lives at `harness-cxa/` per
the workspace `CLAUDE.md` §2.5 assignment ("harness-cxa/ hosts CXA seam
instantiation"). `harness-od/` was foreclosed by OD's "0 outbound cross-axis
edges" invariant; `harness-cp/` would have created a new CP→OD outbound
package-import edge. Moved here from the original prototype location at
`harness-runtime/src/harness_runtime/lifecycle/cp_audit_conversion.py` at the
implementation arc landing.

**Ratifications absorbed.**

- **Q1 (chain equivalence)** — CP `prior_event_hash` ≡ OD `prior_entry_hash`
  per C-IS-06 + C-IS-13 §13.5 (same SHA-256 chain). Direct pass-through.
- **Q2(a) (entry_core source semantic)** — caller (the dispatch composer)
  writes an F2 state-ledger entry recording the dispatch action FIRST,
  then passes the resulting `StateLedgerEntryRef` to the converter. The
  audit's `entry_core` therefore preserves the IS-anchor invariant.
- **Q3 (entry_hash canonicalization)** — SHA-256 over `payload.model_dump_json()`
  per ADR-D5 v1.4 §1.4.1 + OD spec v1.5 C-OD-24.5 canonical helper recipe.
  F2-04 absorption (OD spec v1.7, 2026-05-20): the recipe is now materialized
  at `harness_od.audit_ledger_types.compute_entry_hash`; this converter
  imports it (no local inline duplicate).
- **Q4 (namespace prefix)** — CP-sourced fields land under `audit.cp.*`
  within OD `audit_namespace_attrs` per C-OD-24.6 + the 15-namespace
  ingestion map at C-OD-05.
- **NOTE 3 cryptographic-payload-mismatch foreclosure** — converter signs
  the OD `AuditPayload` directly via `sign_audit_entry`; CP-side signatures
  are NOT re-projected (different bytes, different schemas).
"""

from __future__ import annotations

from harness_cp.per_step_override_evaluator import CPAuditLedgerEntry
from harness_od.audit_ledger_types import (
    AuditLedgerEntry,
    AuditPayload,
    SignatureAlgorithm,
    StateLedgerEntryRef,
    compute_entry_hash,
)
from harness_od.cost_namespace import CostRecordAuditPayload
from harness_od.hitl_operator_burden_namespace import OperatorBurdenAuditPayload
from harness_od.hitl_webhook_namespace import WebhookDeliveryAuditPayload
from harness_od.mcp_trust_namespace import TrustEvaluationAuditPayload
from harness_od.multi_tenant_trace_separation_and_audit_ledger import sign_audit_entry
from harness_od.pause_resume_namespace import PauseResumeAuditPayload
from harness_od.validator_namespace import ValidatorEscalationAuditPayload

#: Namespace prefix for CP-sourced fields landing in OD `audit_namespace_attrs`.
#: Ratified at OD spec v1.5 C-OD-24.6 (Q4 — `audit.cp.*` sub-namespace
#: extends OD-canonical `audit.*` per C-OD-05 §5.1).
CP_AUDIT_NAMESPACE_PREFIX = "audit.cp"

#: Producer-specific sub-namespace prefixes per U-CP-72 7-prefix dispatch
#: post-Sub-arc-A (initial 6-prefix landing at 10-CP-D 2026-05-22; pause_resume
#: added at U-OD-51 landing 2026-05-23). Each AuditPayload subclass projects
#: its producer-specific fields into the corresponding sub-namespace at write
#: time per OD spec v1.9 §C-OD-24.6 sub-namespace tagging discipline.
WEBHOOK_AUDIT_NAMESPACE_PREFIX = "audit.hitl_webhook"
OPERATOR_BURDEN_AUDIT_NAMESPACE_PREFIX = "audit.operator_burden"
VALIDATOR_AUDIT_NAMESPACE_PREFIX = "audit.validator"
MCP_TRUST_AUDIT_NAMESPACE_PREFIX = "audit.mcp_trust"
PAUSE_RESUME_AUDIT_NAMESPACE_PREFIX = "audit.pause_resume"
COST_AUDIT_NAMESPACE_PREFIX = "audit.cost"

#: Union of carrier types accepted by `cp_audit_to_od_audit` per U-CP-72
#: prefix dispatch. Full 8-prefix coverage post-Sub-arc-B (`cost:` branch
#: un-STRUCK at U-OD-41 landing per OD spec v1.10 §C-OD-26.6 +
#: CXA v2.9 §2.3.7 row 8 + `[[fork-u-cp-72-cost-and-pause-resume-prefix-gap]]`
#: §2.2 routing target). The `pause`/`resume` branch un-STRUCK at U-OD-51
#: landing 2026-05-23 per Sub-arc A of the same fork §2.1.
CpAuditCarrier = (
    CPAuditLedgerEntry
    | WebhookDeliveryAuditPayload
    | OperatorBurdenAuditPayload
    | ValidatorEscalationAuditPayload
    | TrustEvaluationAuditPayload
    | PauseResumeAuditPayload
    | CostRecordAuditPayload
)


def _project_namespace_attrs(cp_entry: CPAuditLedgerEntry) -> dict[str, str]:
    """Project CP fields → `audit_namespace_attrs` dict per the §3 field table.

    Conditional hash fields (`edited_proposal_hash` / `rejection_reason_hash` /
    `response_text_hash`) are included only when populated per C-CP-16 §16.2's
    response-conditional discipline.
    """
    attrs: dict[str, str] = {
        f"{CP_AUDIT_NAMESPACE_PREFIX}.action_id": str(cp_entry.action_id),
        f"{CP_AUDIT_NAMESPACE_PREFIX}.gate_level": cp_entry.gate_level.value,
        f"{CP_AUDIT_NAMESPACE_PREFIX}.response": cp_entry.response,
        f"{CP_AUDIT_NAMESPACE_PREFIX}.timestamp": cp_entry.timestamp,
    }
    if cp_entry.edited_proposal_hash is not None:
        attrs[f"{CP_AUDIT_NAMESPACE_PREFIX}.edited_proposal_hash"] = cp_entry.edited_proposal_hash
    if cp_entry.rejection_reason_hash is not None:
        attrs[f"{CP_AUDIT_NAMESPACE_PREFIX}.rejection_reason_hash"] = cp_entry.rejection_reason_hash
    if cp_entry.response_text_hash is not None:
        attrs[f"{CP_AUDIT_NAMESPACE_PREFIX}.response_text_hash"] = cp_entry.response_text_hash
    return attrs


def _project_producer_namespace_attrs(
    carrier: WebhookDeliveryAuditPayload
    | OperatorBurdenAuditPayload
    | ValidatorEscalationAuditPayload
    | TrustEvaluationAuditPayload
    | PauseResumeAuditPayload
    | CostRecordAuditPayload,
    producer_prefix: str,
) -> dict[str, str]:
    """Project a producer-specific AuditPayload subclass into the unified
    `audit_namespace_attrs` dict per OD spec v1.9 §C-OD-24.6 sub-namespace
    discipline.

    Common `audit_cp_*` fields land under `audit.cp.*`; producer-specific
    fields land under `audit.{producer_prefix}.*`. Optional fields are
    elided when `None` (per §C-OD-24.6 conditional-field discipline).
    """
    attrs: dict[str, str] = {}
    data = carrier.model_dump()
    for field_name, value in data.items():
        if value is None:
            continue
        rendered = str(value)
        if field_name.startswith("audit_cp_"):
            suffix = field_name[len("audit_cp_") :]
            attrs[f"{CP_AUDIT_NAMESPACE_PREFIX}.{suffix}"] = rendered
        else:
            attrs[f"{producer_prefix}.{field_name}"] = rendered
    return attrs


def _entry_core_or_default(
    entry_core: StateLedgerEntryRef | None, action_id: str
) -> StateLedgerEntryRef:
    """Resolve `entry_core` to the supplied value or a synthesized opaque
    marker per Q2(a) source-semantic discipline (production composer always
    supplies; tests + legacy callers may omit)."""
    if entry_core is not None:
        return entry_core
    return StateLedgerEntryRef(f"cp-audit:{action_id}")


def cp_audit_to_od_audit(
    cp_entry: CpAuditCarrier,
    *,
    key_id: str,
    algo: SignatureAlgorithm = SignatureAlgorithm.ED25519,
    entry_core: StateLedgerEntryRef | None = None,
) -> AuditLedgerEntry:
    """Convert any CP-side audit carrier to a signed OD `AuditLedgerEntry`.

    Production seam per CP spec v1.7 §13.5.1 + OD spec v1.5 C-OD-24.6 +
    U-CP-72 6-prefix extension (10-CP-D 2026-05-22).

    Dispatches on the carrier type:

    - `CPAuditLedgerEntry` → existing `dispatch:` + `hitl:` prefix paths
      (preserved verbatim from the U-RT-59 Fork 2 close).
    - `WebhookDeliveryAuditPayload` → `audit.hitl_webhook.*` sub-namespace
      composition (`hitl_webhook:` prefix).
    - `OperatorBurdenAuditPayload` → `audit.operator_burden.*` sub-namespace
      composition (`operator_burden:` prefix).
    - `ValidatorEscalationAuditPayload` → `audit.validator.*` sub-namespace
      composition (`validator:` prefix).
    - `TrustEvaluationAuditPayload` → `audit.mcp_trust.*` sub-namespace
      composition (`mcp_trust:` prefix).
    - `PauseResumeAuditPayload` → `audit.pause_resume.*` sub-namespace
      composition (`pause:` OR `resume:` prefix per
      `audit_cp_action_id` discriminator at the producer-side; converter
      isinstance branches once on the union type, prefix discrimination is
      authored at U-OD-51 producer-side per OD spec v1.9 §C-OD-30.2
      action_id pattern).
    - `CostRecordAuditPayload` → `audit.cost.*` sub-namespace
      composition (`cost:` prefix). Authored at U-OD-41 helper per OD spec
      v1.10 §C-OD-26.6 + CXA v2.9 §2.3.7 row 8.

    The `cost:` prefix un-STRUCK at U-OD-41 landing 2026-05-24 per Sub-arc B
    of `[[fork-u-cp-72-cost-and-pause-resume-prefix-gap]]` §2.2 routing
    target — CostRecordAuditPayload + COST_AUDIT_NAMESPACE_PREFIX + isinstance
    branch all land at this same impl arc. The `pause:`/`resume:` prefix
    un-STRUCK at U-OD-51 landing 2026-05-23 per Sub-arc A of the same fork
    §2.1. Full 8-prefix coverage now operational.

    Parameters:
        cp_entry: any CP-side audit carrier per `CpAuditCarrier` union.
        key_id: OD `audit.signature.key_id` per C-OD-21 §21.2 + ADR-D5
            v1.4 §1.4.1.
        algo: OD `audit.signature.algorithm`; defaults to Ed25519.
        entry_core: OD `payload.entry_core` IS reference per Q2(a). If
            None, synthesizes an opaque `cp-audit:<action_id>` marker.

    Returns:
        a fully-signed `AuditLedgerEntry` ready for `audit_writer.append`.

    Raises:
        ValueError: from `sign_audit_entry` when `key_id` is empty.
        TypeError: if `cp_entry` is not a member of `CpAuditCarrier`.
    """
    if isinstance(cp_entry, CPAuditLedgerEntry):
        # Existing dispatch / hitl path — preserved verbatim from U-RT-59
        # Fork 2 close. The CP-side action_id already encodes the
        # `dispatch:` or `hitl:` prefix per the producer-side composers.
        resolved_entry_core = _entry_core_or_default(entry_core, str(cp_entry.action_id))
        payload = AuditPayload(
            entry_core=resolved_entry_core,
            audit_namespace_attrs=_project_namespace_attrs(cp_entry),
            prior_entry_hash=cp_entry.prior_event_hash,
        )
    elif isinstance(cp_entry, WebhookDeliveryAuditPayload):
        resolved_entry_core = _entry_core_or_default(entry_core, cp_entry.audit_cp_action_id)
        payload = AuditPayload(
            entry_core=resolved_entry_core,
            audit_namespace_attrs=_project_producer_namespace_attrs(
                cp_entry, WEBHOOK_AUDIT_NAMESPACE_PREFIX
            ),
            prior_entry_hash=cp_entry.audit_cp_prior_event_hash,
        )
    elif isinstance(cp_entry, OperatorBurdenAuditPayload):
        resolved_entry_core = _entry_core_or_default(entry_core, cp_entry.audit_cp_action_id)
        payload = AuditPayload(
            entry_core=resolved_entry_core,
            audit_namespace_attrs=_project_producer_namespace_attrs(
                cp_entry, OPERATOR_BURDEN_AUDIT_NAMESPACE_PREFIX
            ),
            prior_entry_hash=cp_entry.audit_cp_prior_event_hash,
        )
    elif isinstance(cp_entry, ValidatorEscalationAuditPayload):
        resolved_entry_core = _entry_core_or_default(entry_core, cp_entry.audit_cp_action_id)
        payload = AuditPayload(
            entry_core=resolved_entry_core,
            audit_namespace_attrs=_project_producer_namespace_attrs(
                cp_entry, VALIDATOR_AUDIT_NAMESPACE_PREFIX
            ),
            prior_entry_hash=cp_entry.audit_cp_prior_event_hash,
        )
    elif isinstance(cp_entry, TrustEvaluationAuditPayload):
        resolved_entry_core = _entry_core_or_default(entry_core, cp_entry.audit_cp_action_id)
        payload = AuditPayload(
            entry_core=resolved_entry_core,
            audit_namespace_attrs=_project_producer_namespace_attrs(
                cp_entry, MCP_TRUST_AUDIT_NAMESPACE_PREFIX
            ),
            prior_entry_hash=cp_entry.audit_cp_prior_event_hash,
        )
    elif isinstance(cp_entry, PauseResumeAuditPayload):
        resolved_entry_core = _entry_core_or_default(entry_core, cp_entry.audit_cp_action_id)
        payload = AuditPayload(
            entry_core=resolved_entry_core,
            audit_namespace_attrs=_project_producer_namespace_attrs(
                cp_entry, PAUSE_RESUME_AUDIT_NAMESPACE_PREFIX
            ),
            prior_entry_hash=cp_entry.audit_cp_prior_event_hash,
        )
    elif isinstance(cp_entry, CostRecordAuditPayload):  # type: ignore[reportUnnecessaryIsInstance]
        resolved_entry_core = _entry_core_or_default(entry_core, cp_entry.audit_cp_action_id)
        payload = AuditPayload(
            entry_core=resolved_entry_core,
            audit_namespace_attrs=_project_producer_namespace_attrs(
                cp_entry, COST_AUDIT_NAMESPACE_PREFIX
            ),
            prior_entry_hash=cp_entry.audit_cp_prior_event_hash,
        )
    else:
        raise TypeError(
            f"cp_audit_to_od_audit: unsupported carrier type "
            f"{type(cp_entry).__name__}; expected one of "
            f"CPAuditLedgerEntry, WebhookDeliveryAuditPayload, "
            f"OperatorBurdenAuditPayload, ValidatorEscalationAuditPayload, "
            f"TrustEvaluationAuditPayload, PauseResumeAuditPayload, "
            f"CostRecordAuditPayload"
        )

    signature_attrs = sign_audit_entry(payload, key_id=key_id, algo=algo)
    entry_hash = compute_entry_hash(payload)

    return AuditLedgerEntry(
        payload=payload,
        signature_attrs=signature_attrs,
        entry_hash=entry_hash,
    )
