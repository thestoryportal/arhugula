"""Tests for U-CP-14 — per-step override evaluator + CP audit-ledger entries.

Acceptance-criterion coverage:
  #1 resolve_step_binding field-by-field -> test_resolve_step_binding_field_by_field_override
  #2 audit_ref populated on override     -> test_audit_ref_populated_on_override
  #2 action_id composition               -> test_audit_entry_action_id_composition
  #4 deterministic                       -> test_override_evaluator_deterministic
  #5 CPAuditLedgerEntry 8 fields          -> test_cp_audit_ledger_entry_eight_fields
  #5 response-conditional hash population -> test_cp_audit_entry_response_conditional_hash_population
  #6 CPSignedAuditLedgerEntry 5 sig fields -> test_cp_signed_audit_entry_five_signature_fields
  #6 distinct from OD AuditLedgerEntry    -> test_cp_audit_types_distinct_from_od
"""

from __future__ import annotations

from harness_as import GateLevel
from harness_core import PersonaTier, StepID, WorkloadClass
from harness_cp.cp_shared_types import AgentRole, ModelBinding
from harness_cp.cross_family_fallback_chain import (
    FallbackChain,
    ProviderCandidate,
    ProviderFamily,
)
from harness_cp.engine_class import EngineClass
from harness_cp.per_step_override_evaluator import (
    CPAuditLedgerEntry,
    CPSignedAuditLedgerEntry,
    emit_override_audit_entry,
    resolve_step_binding,
)
from harness_cp.topology_pattern import TopologyPattern
from harness_cp.workflow_manifest_entry import StepOverride, WorkflowManifestEntry

_DEFAULT_BINDING = ModelBinding(provider="anthropic", model="default-model")
_OVERRIDE_BINDING = ModelBinding(provider="anthropic", model="override-model")
_CHAIN = FallbackChain(
    primary=ProviderCandidate(provider="anthropic", model="m", family=ProviderFamily.ANTHROPIC),
    same_family=(),
    cross_family=(),
    terminal=None,
)


def _manifest(**over: object) -> WorkflowManifestEntry:
    base: dict[str, object] = {
        "workflow_id": "wf-1",
        "workload_class": WorkloadClass.PIPELINE_AUTOMATION,
        "persona_tier": PersonaTier.TEAM_BINDING,
        "engine_class": EngineClass.PURE_PATTERN_NO_ENGINE,
        "topology_pattern": TopologyPattern.SINGLE_THREADED_LINEAR,
        "layer_budgets": (),
        "fallback_chain": _CHAIN,
        "hitl_placements": (),
        "per_step_overrides": {},
    }
    base.update(over)
    return WorkflowManifestEntry(**base)  # type: ignore[arg-type]


def test_resolve_step_binding_field_by_field_override() -> None:
    manifest = _manifest(
        per_step_overrides={
            StepID("s1"): StepOverride(step_id=StepID("s1"), model_binding=_OVERRIDE_BINDING)
        }
    )
    binding = resolve_step_binding(
        manifest,
        "s1",
        default_model_binding=_DEFAULT_BINDING,
        persona_tier=PersonaTier.TEAM_BINDING,
    )
    # model_binding overridden; engine_class inherits the manifest default.
    assert binding.model_binding == _OVERRIDE_BINDING
    assert binding.engine_class is EngineClass.PURE_PATTERN_NO_ENGINE
    assert binding.persona_tier is PersonaTier.TEAM_BINDING


def test_resolve_step_binding_applies_prompt_version_sha() -> None:
    """B4 Slice 3 (CP spec v1.37 §6.2): resolve_step_binding carries a per-step
    StepOverride.prompt_version_sha onto the StepEffectiveBinding. An override
    carrying ONLY a prompt sha still applies (so the override state-ledger entry
    fires) while model/engine inherit the manifest defaults."""
    sha = "a" * 64
    manifest = _manifest(
        per_step_overrides={
            StepID("s1"): StepOverride(step_id=StepID("s1"), prompt_version_sha=sha)
        }
    )
    binding = resolve_step_binding(
        manifest,
        "s1",
        default_model_binding=_DEFAULT_BINDING,
        persona_tier=PersonaTier.TEAM_BINDING,
    )
    assert binding.prompt_version_sha == sha
    assert binding.override_applied is True
    assert binding.model_binding == _DEFAULT_BINDING
    assert binding.engine_class is EngineClass.PURE_PATTERN_NO_ENGINE


def test_resolve_step_binding_prompt_version_sha_none_without_prompt_override() -> None:
    """No per-step prompt dimension → prompt_version_sha is None (the runtime
    dispatch falls through to per-role / default): both for an override that omits
    the prompt field and for a step with no override at all."""
    manifest = _manifest(
        per_step_overrides={
            StepID("s1"): StepOverride(step_id=StepID("s1"), model_binding=_OVERRIDE_BINDING)
        }
    )
    overridden = resolve_step_binding(
        manifest,
        "s1",
        default_model_binding=_DEFAULT_BINDING,
        persona_tier=PersonaTier.TEAM_BINDING,
    )
    assert overridden.prompt_version_sha is None  # override present, no prompt dimension
    no_override = resolve_step_binding(
        manifest,
        "s2",
        default_model_binding=_DEFAULT_BINDING,
        persona_tier=PersonaTier.TEAM_BINDING,
    )
    assert no_override.prompt_version_sha is None


def test_prompt_version_sha_rides_binding_model_dump_for_provenance() -> None:
    """B4 Slice 3 provenance (CP spec v1.37 §6.6): binding.model_dump — which IS
    post_override_step_config at the WIRED per-step override state-ledger entry
    (workflow_driver.py) — carries prompt_version_sha, so a per-step prompt flip
    is captured in that entry's outcome-hash (live step-level provenance, NOT the
    run-level §5.2 procedural-tier hash)."""
    sha1, sha2 = "a" * 64, "b" * 64

    def _dump(sha: str) -> dict[str, object]:
        manifest = _manifest(
            per_step_overrides={
                StepID("s1"): StepOverride(step_id=StepID("s1"), prompt_version_sha=sha)
            }
        )
        binding = resolve_step_binding(
            manifest,
            "s1",
            default_model_binding=_DEFAULT_BINDING,
            persona_tier=PersonaTier.TEAM_BINDING,
        )
        return binding.model_dump(mode="json")

    d1, d2 = _dump(sha1), _dump(sha2)
    assert d1["prompt_version_sha"] == sha1
    assert d2["prompt_version_sha"] == sha2


def test_resolve_step_binding_applies_agent_role() -> None:
    """B4 Slice 4 (CP spec v1.38 §6.2): resolve_step_binding carries a per-step
    StepOverride.agent_role onto the StepEffectiveBinding. An override carrying
    ONLY a role still applies (so the override state-ledger entry fires) while
    model/engine inherit the manifest defaults."""
    role = AgentRole("specialist-reviewer")
    manifest = _manifest(
        per_step_overrides={StepID("s1"): StepOverride(step_id=StepID("s1"), agent_role=role)}
    )
    binding = resolve_step_binding(
        manifest,
        "s1",
        default_model_binding=_DEFAULT_BINDING,
        persona_tier=PersonaTier.TEAM_BINDING,
    )
    assert binding.agent_role == role
    assert binding.override_applied is True
    assert binding.model_binding == _DEFAULT_BINDING
    assert binding.engine_class is EngineClass.PURE_PATTERN_NO_ENGINE


def test_resolve_step_binding_agent_role_none_without_role_override() -> None:
    """No per-step role dimension → agent_role is None (the CP driver folds the
    fan-out-derived role / linear-path default instead): both for an override that
    omits the role field and for a step with no override at all."""
    manifest = _manifest(
        per_step_overrides={
            StepID("s1"): StepOverride(step_id=StepID("s1"), model_binding=_OVERRIDE_BINDING)
        }
    )
    overridden = resolve_step_binding(
        manifest,
        "s1",
        default_model_binding=_DEFAULT_BINDING,
        persona_tier=PersonaTier.TEAM_BINDING,
    )
    assert overridden.agent_role is None  # override present, no role dimension
    no_override = resolve_step_binding(
        manifest,
        "s2",
        default_model_binding=_DEFAULT_BINDING,
        persona_tier=PersonaTier.TEAM_BINDING,
    )
    assert no_override.agent_role is None


def test_agent_role_rides_binding_model_dump_for_provenance() -> None:
    """B4 Slice 4 provenance (CP spec v1.38 §6.6): binding.model_dump — which IS
    post_override_step_config at the WIRED per-step override state-ledger entry
    (workflow_driver.py) — carries agent_role, so a per-step role flip is captured
    in that entry's outcome-hash (live step-level provenance, NOT the run-level
    §5.2 procedural-tier hash)."""

    def _dump(role: str) -> dict[str, object]:
        manifest = _manifest(
            per_step_overrides={
                StepID("s1"): StepOverride(step_id=StepID("s1"), agent_role=AgentRole(role))
            }
        )
        binding = resolve_step_binding(
            manifest,
            "s1",
            default_model_binding=_DEFAULT_BINDING,
            persona_tier=PersonaTier.TEAM_BINDING,
        )
        return binding.model_dump(mode="json")

    d1, d2 = _dump("role-a"), _dump("role-b")
    assert d1["agent_role"] == "role-a"
    assert d2["agent_role"] == "role-b"


def test_resolve_step_binding_applies_model_binding_override() -> None:
    """B-MODEL-RESOLUTION-CONSOLIDATION (CP spec v1.50 §6.2): a per-step
    StepOverride.model_binding sets BOTH the concrete `model_binding` (override value)
    AND the `None`-or-override SIGNAL `model_binding_override` (so the C-RT-16 wrapper
    can distinguish a per-step model override from the manifest default)."""
    manifest = _manifest(
        per_step_overrides={
            StepID("s1"): StepOverride(step_id=StepID("s1"), model_binding=_OVERRIDE_BINDING)
        }
    )
    binding = resolve_step_binding(
        manifest,
        "s1",
        default_model_binding=_DEFAULT_BINDING,
        persona_tier=PersonaTier.TEAM_BINDING,
    )
    assert binding.model_binding == _OVERRIDE_BINDING  # concrete resolved value
    assert binding.model_binding_override == _OVERRIDE_BINDING  # the None-or-override signal
    assert binding.override_applied is True


def test_resolve_step_binding_model_binding_override_none_without_model_override() -> None:
    """No per-step MODEL dimension → model_binding_override is None (the wrapper falls
    through to per-role / per-workload / routed / default), while `model_binding` still
    resolves to the manifest default: both for an override that omits the model field
    and for a step with no override at all."""
    manifest = _manifest(
        per_step_overrides={
            StepID("s1"): StepOverride(step_id=StepID("s1"), prompt_version_sha="a" * 64)
        }
    )
    overridden = resolve_step_binding(
        manifest,
        "s1",
        default_model_binding=_DEFAULT_BINDING,
        persona_tier=PersonaTier.TEAM_BINDING,
    )
    assert overridden.model_binding_override is None  # override present, no model dimension
    assert overridden.model_binding == _DEFAULT_BINDING  # concrete value still the default
    no_override = resolve_step_binding(
        manifest,
        "s2",
        default_model_binding=_DEFAULT_BINDING,
        persona_tier=PersonaTier.TEAM_BINDING,
    )
    assert no_override.model_binding_override is None


def test_model_binding_override_rides_binding_model_dump_for_provenance() -> None:
    """B-MODEL-RESOLUTION-CONSOLIDATION provenance (CP spec v1.50 §6.6): the
    `model_binding_override` signal rides binding.model_dump — the
    post_override_step_config at the WIRED per-step override state-ledger entry — so a
    per-step model flip is captured in that entry's outcome-hash (live step-level
    provenance, like prompt_version_sha / agent_role)."""

    def _dump(model: str) -> dict[str, object]:
        manifest = _manifest(
            per_step_overrides={
                StepID("s1"): StepOverride(
                    step_id=StepID("s1"),
                    model_binding=ModelBinding(provider="anthropic", model=model),
                )
            }
        )
        binding = resolve_step_binding(
            manifest,
            "s1",
            default_model_binding=_DEFAULT_BINDING,
            persona_tier=PersonaTier.TEAM_BINDING,
        )
        return binding.model_dump(mode="json")

    d1, d2 = _dump("model-x"), _dump("model-y")
    assert d1["model_binding_override"] == {"provider": "anthropic", "model": "model-x"}
    assert d2["model_binding_override"] == {"provider": "anthropic", "model": "model-y"}


def test_audit_ref_populated_on_override() -> None:
    manifest = _manifest(
        per_step_overrides={
            StepID("s1"): StepOverride(
                step_id=StepID("s1"), engine_class=EngineClass.EVENT_SOURCED_REPLAY
            )
        }
    )
    binding = resolve_step_binding(
        manifest,
        "s1",
        default_model_binding=_DEFAULT_BINDING,
        persona_tier=PersonaTier.TEAM_BINDING,
    )
    assert binding.override_applied is True
    assert binding.override_audit_ref is not None

    no_override = resolve_step_binding(
        manifest,
        "s2",
        default_model_binding=_DEFAULT_BINDING,
        persona_tier=PersonaTier.TEAM_BINDING,
    )
    assert no_override.override_applied is False
    assert no_override.override_audit_ref is None
    assert no_override.persona_tier is PersonaTier.TEAM_BINDING


def test_audit_entry_action_id_composition() -> None:
    entry = emit_override_audit_entry(
        workflow_id="wf-1",
        step_id="s1",
        override=StepOverride(step_id=StepID("s1")),
        actor="ctl",  # type: ignore[arg-type]
    )
    assert entry.action_id == "wf-1||s1"


def test_audit_entry_timestamp_is_iso_8601_per_v1_28() -> None:
    """CP spec v1.28 §16.5.6.X — `timestamp` is non-tier-conditional per
    C-CP-16 §16.2 + ADR-D5 §1.4. v1.27 `timestamp=""` placeholder closed."""
    from datetime import datetime

    entry = emit_override_audit_entry(
        workflow_id="wf-1",
        step_id="s1",
        override=StepOverride(step_id=StepID("s1")),
        actor="ctl",  # type: ignore[arg-type]
    )
    assert entry.timestamp != ""
    # ISO-8601 parses round-trip
    parsed = datetime.fromisoformat(entry.timestamp)
    assert parsed.tzinfo is not None, "timestamp MUST carry UTC tzinfo"


def test_audit_entry_prior_event_hash_sentinel_canonical_at_solo_developer() -> None:
    """ADR-D5 §1.4 row 1: solo-developer tier requires no hash chain.
    `"0"*64` sentinel is canonical per CP spec v1.28 §16.5.6.X. Team-binding+
    tier wiring deferred per operator-deployment-time opt-in."""
    entry = emit_override_audit_entry(
        workflow_id="wf-1",
        step_id="s1",
        override=StepOverride(step_id=StepID("s1")),
        actor="ctl",  # type: ignore[arg-type]
    )
    assert entry.prior_event_hash == "0" * 64


def test_override_evaluator_deterministic() -> None:
    manifest = _manifest(
        per_step_overrides={
            StepID("s1"): StepOverride(step_id=StepID("s1"), model_binding=_OVERRIDE_BINDING)
        }
    )
    a = resolve_step_binding(
        manifest,
        "s1",
        default_model_binding=_DEFAULT_BINDING,
        persona_tier=PersonaTier.TEAM_BINDING,
    )
    b = resolve_step_binding(
        manifest,
        "s1",
        default_model_binding=_DEFAULT_BINDING,
        persona_tier=PersonaTier.TEAM_BINDING,
    )
    assert a == b


def test_cp_audit_ledger_entry_eight_fields() -> None:
    assert len(CPAuditLedgerEntry.model_fields) == 8
    assert set(CPAuditLedgerEntry.model_fields) == {
        "action_id",
        "gate_level",
        "response",
        "edited_proposal_hash",
        "rejection_reason_hash",
        "response_text_hash",
        "timestamp",
        "prior_event_hash",
    }


def test_cp_audit_entry_response_conditional_hash_population() -> None:
    approve = CPAuditLedgerEntry(
        action_id="a||s",  # type: ignore[arg-type]
        gate_level=GateLevel.AUTO,
        response="approve",
        timestamp="t",
        prior_event_hash="0" * 64,
    )
    assert approve.edited_proposal_hash is None
    assert approve.rejection_reason_hash is None
    assert approve.response_text_hash is None

    edit = CPAuditLedgerEntry(
        action_id="a||s",  # type: ignore[arg-type]
        gate_level=GateLevel.ASK,
        response="edit",
        edited_proposal_hash="f" * 64,
        timestamp="t",
        prior_event_hash="0" * 64,
    )
    assert edit.edited_proposal_hash == "f" * 64


def test_cp_signed_audit_entry_five_signature_fields() -> None:
    sig_fields = set(CPSignedAuditLedgerEntry.model_fields) - {"entry"}
    assert sig_fields == {
        "audit_signature_sha256",
        "audit_signature_value",
        "audit_signature_algorithm",
        "audit_signature_key_id",
        "audit_signature_key_period",
    }


def test_cp_audit_types_distinct_from_od() -> None:
    # The CP audit types are CP-spec-owned, nominally distinct from the OD
    # AuditLedgerEntry (U-OD-00). The CP names carry the `CP` prefix per the
    # v2.9 §0.5.1 name-collision resolution.
    assert CPAuditLedgerEntry.__name__ == "CPAuditLedgerEntry"
    assert CPSignedAuditLedgerEntry.__name__ == "CPSignedAuditLedgerEntry"


# --- B-HITL-PLACEMENT-PER-STEP-LOOSEN (CP spec v1.53 §6.2) — per-step SUB_AGENT_
# BOUNDARY gate removal carrier propagation + model_dump provenance. ----------


def test_resolve_step_binding_applies_removed_placements() -> None:
    """CP spec v1.53 §6.2: resolve_step_binding carries StepOverride.removed_placements
    onto the StepEffectiveBinding (the full CP-side half of the CP→runtime seam — the
    composer reads binding.removed_placements). An override carrying ONLY the removal
    still applies (the override state-ledger entry fires)."""
    from harness_cp.hitl_placement import LoosenablePlacementKind

    removed = frozenset({LoosenablePlacementKind.SUB_AGENT_BOUNDARY})
    manifest = _manifest(
        per_step_overrides={
            StepID("s1"): StepOverride(step_id=StepID("s1"), removed_placements=removed)
        }
    )
    binding = resolve_step_binding(
        manifest,
        "s1",
        default_model_binding=_DEFAULT_BINDING,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
    )
    assert binding.removed_placements == removed
    assert binding.override_applied is True
    assert binding.model_binding == _DEFAULT_BINDING


def test_resolve_step_binding_removed_placements_empty_default() -> None:
    """Default empty ⇒ byte-identical monotone path: an override without a removal
    dimension AND a step with no override at all both yield an empty set."""
    manifest = _manifest(
        per_step_overrides={
            StepID("s1"): StepOverride(step_id=StepID("s1"), model_binding=_OVERRIDE_BINDING)
        }
    )
    overridden = resolve_step_binding(
        manifest,
        "s1",
        default_model_binding=_DEFAULT_BINDING,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
    )
    assert overridden.removed_placements == frozenset()
    no_override = resolve_step_binding(
        manifest,
        "s2",
        default_model_binding=_DEFAULT_BINDING,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
    )
    assert no_override.removed_placements == frozenset()


def test_removed_placements_rides_binding_model_dump_for_provenance() -> None:
    """Provenance (CP spec v1.53 §6.6, advisor #3): binding.model_dump — the WIRED
    per-step override state-ledger entry payload — carries removed_placements, so a
    removal opt-in is captured in that entry's outcome-hash (live step-level
    provenance, NOT a new run-level §5.2 procedural-tier hash field)."""
    from harness_cp.hitl_placement import LoosenablePlacementKind

    manifest = _manifest(
        per_step_overrides={
            StepID("s1"): StepOverride(
                step_id=StepID("s1"),
                removed_placements=frozenset({LoosenablePlacementKind.SUB_AGENT_BOUNDARY}),
            )
        }
    )
    binding = resolve_step_binding(
        manifest,
        "s1",
        default_model_binding=_DEFAULT_BINDING,
        persona_tier=PersonaTier.SOLO_DEVELOPER,
    )
    dumped = binding.model_dump(mode="json")
    assert dumped["removed_placements"] == ["sub-agent-boundary"]
