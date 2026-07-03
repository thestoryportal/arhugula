"""U-RT-51 — Pattern P1 identity-equality verification (Phase 2 close gate).

Per session-3 atomic decomposition L11 U-RT-51 + CXA v2.3 §2.3 + §3:

> Scope: test asserts the 22 genuine typed CXA seams (per CXA v2.3 §3)
> realize Pattern P1 byte-exact — for each (producer-export, consumer-import)
> pair, `consumer_module.SYMBOL is producer_module.SYMBOL` returns `True`.
> AC: 22 identity-equality assertions pass; missing seam fails with typed
> error naming the (producer, consumer) pair.

Two tests:

1. `test_pattern_p1_identity_per_seam` — parametrized over the 22 genuine
   typed seams. For each row asserts the producer's defining-module
   symbol IS the consumer's bound name. Per-row failure includes both
   module names + symbol + CXA edge label in the assertion message.

2. `test_cross_axis_imports_match_enumerated_seams` — discovery
   completeness check. AST-walks every `.py` in harness-{as,cp,od}/src/
   and asserts every cross-axis `from harness_X import` symbol appears
   in the 22-row enumeration (modulo a per-symbol allowlist for the
   convention/runtime/spurious edges that import but don't define
   Pattern P1 byte-exact obligations).

Background
----------
Pattern P1 byte-exact namespace alignment: a consumer's reference to a
producer's exported symbol is the same Python object — not a copy, not
a local redefinition. CXA v2.3 §3 governs this invariant. The 22
seams enumerated at CXA v2.3 §2.3 are the typed-import subset; the
other 70 canonical cross-axis relationships (46 convention-level + 24
phase-2-runtime) are NOT Python imports and are not exercised here.

Audit evidence per seam: `.harness/cxa_7c_audit_cp_buckets.md`,
`.harness/cxa_7c_audit_od_buckets.md`,
`.harness/class_1_tension_cxa_as_is_untyped_edges.md`.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# The 22 genuine typed CXA seams enumerated at CXA v2.3 §2.3.
# Each row: (cxa_edge_label, consumer_module, producer_canonical_module, symbol).
# ---------------------------------------------------------------------------


PATTERN_P1_SEAMS: tuple[tuple[str, str, str, str], ...] = (
    # ---- §2.3.1 AS → IS (7 G) -------------------------------------------
    (
        "U-AS-19→U-IS-07",
        "harness_as.sandbox_event_idempotency",
        "harness_is.state_ledger_entry_schema",
        "Identifier",
    ),
    (
        "U-AS-19→U-IS-12",
        "harness_as.sandbox_event_idempotency",
        "harness_is.state_ledger_entry_schema",
        "Identifier",
    ),
    (
        "U-AS-26→U-IS-07",
        "harness_as.secret_fetch_audit",
        "harness_is.state_ledger_entry_schema",
        "StateLedgerEntry",
    ),
    (
        "U-AS-26→U-IS-09",
        "harness_as.secret_fetch_audit",
        "harness_is.chain_link_construction",
        "construct_prior_event_hash",
    ),
    (
        "U-AS-26→U-IS-10",
        "harness_as.secret_fetch_audit",
        "harness_is.chain_verification",
        "verify_chain",
    ),
    (
        "U-AS-28→U-IS-01",
        "harness_as.anthropic_primitive_adoption",
        "harness_is.path_class_registry",
        "PathClass",
    ),
    (
        "U-AS-28→U-IS-02",
        "harness_as.anthropic_primitive_adoption",
        "harness_is.path_class_registry",
        "PATH_CLASS_REGISTRY",
    ),
    # ---- §2.3.2 CP → IS (9 G) -------------------------------------------
    (
        "U-CP-04→U-IS-01",
        "harness_cp.routing_manifest_residence",
        "harness_is.path_class_registry",
        "PathClass",
    ),
    (
        "U-CP-04→U-IS-02",
        "harness_cp.routing_manifest_residence",
        "harness_is.path_resolver",
        "PathResolver",
    ),
    (
        "U-CP-30→U-IS-12",
        "harness_cp.handoff_context",
        "harness_is.state_ledger_entry_schema",
        "Identifier",
    ),
    (
        "U-CP-33→U-IS-01",
        "harness_cp.concurrent_prompt_cache_warmup",
        "harness_is.path_class_registry",
        "PathClass",
    ),
    (
        "U-CP-33→U-IS-02",
        "harness_cp.concurrent_prompt_cache_warmup",
        "harness_is.path_resolver",
        "PathResolver",
    ),
    (
        "U-CP-34→U-IS-07",
        "harness_cp.sibling_ledger_entry_composition",
        "harness_is.state_ledger_entry_schema",
        "StateLedgerEntry",
    ),
    (
        "U-CP-34→U-IS-11",
        "harness_cp.sibling_ledger_entry_composition",
        "harness_is.state_ledger_write",
        "append_ledger_entry",
    ),
    (
        "U-CP-35→U-IS-12",
        "harness_cp.parent_fanout_close_entry",
        "harness_is.state_ledger_read",
        "LedgerNavigationPrimitive",
    ),
    (
        "U-CP-52→U-IS-12",
        "harness_cp.hitl_timeout_degradation",
        "harness_is.state_ledger_entry_schema",
        "Identifier",
    ),
    # ---- §2.3.3 CP → AS (5 G) -------------------------------------------
    (
        "U-CP-26→U-AS-01",
        "harness_cp.default_downgrade_rule",
        "harness_as.sandbox_tier",
        "BlastRadiusTier",
    ),
    (
        "U-CP-44→U-AS-20",
        "harness_cp.f5_signing_key_resolution",
        "harness_as.secret_fetch",
        "fetch_secret",
    ),
    (
        "U-CP-27→U-AS-01",
        "harness_cp.sub_agent_gate_level_descent",
        "harness_as.sandbox_tier",
        "BlastRadiusTier",
    ),
    (
        "U-CP-43→U-AS-01",
        "harness_cp.gate_level_rule",
        "harness_as.sandbox_tier",
        "BlastRadiusTier",
    ),
    (
        "U-CP-45→U-AS-01",
        "harness_cp.five_axis_composition",
        "harness_as.sandbox_tier",
        "BlastRadiusTier",
    ),
    # NEW at cluster 10-CP-C close (2026-05-21): U-CP-68
    # PerServerTrustEvaluator.evaluate() signature requires
    # `tool_contract: ToolContract | None` per CP spec v1.10 §27.1; physical
    # import of ToolContract from harness_as creates a genuine typed
    # CP→AS Pattern-P1 seam. Routes to next CXA amendment (CXA v2.5 §2.3.3
    # CP→AS bucket: 5 → 6 entries; aggregate 24 → 25 typed seams).
    (
        "U-CP-68→U-AS-03",
        "harness_cp.per_server_trust_evaluator",
        "harness_as.tool_contract",
        "ToolContract",
    ),
    # ---- §2.3.5 OD → AS (1 G) -------------------------------------------
    (
        "U-OD-29→U-AS-15",
        "harness_od.per_sandbox_tier_otlp_reachability",
        "harness_as.sandbox_tier",
        "SandboxTier",
    ),
    # ---- §2.3.7 CP → OD (2 G; NEW v2.4 + v2.5) --------------------------
    # First seam added at CXA v2.4 §2.3.7 (sub-agent dispatch audit-write per
    # U-RT-59 Fork 2 Path D); second seam added at CXA v2.5 §2.3.7 (HITL gate
    # response audit-write per U-RT-60 Q3 ratification). Both seams share the
    # `cp_audit_to_od_audit` converter at `harness-cxa/`. Both classify as G
    # per the converter-output-type precedent (CXA v2.4 §0.3 + v2.5 §0.3).
    (
        "U-CP-28→U-OD-00",
        "harness_cxa.cp_audit_conversion",
        "harness_od.audit_ledger_types",
        "AuditLedgerEntry",
    ),
    (
        "U-CP-46→U-OD-00",
        "harness_cxa.cp_audit_conversion",
        "harness_od.audit_ledger_types",
        "AuditLedgerEntry",
    ),
    # ---- §2.3.2 CP → IS — 6 NEW v2.17 §16.5 absorption rows --------------
    # Six NEW genuine typed seams added at CXA v2.17 §2.3.2 rows 38-43
    # absorbing the 6 PENDING §0.4 forward-tracking marker entries
    # (U-CP-74..U-CP-79 §16.5 CP→IS composer atomic-unit LANDED events
    # at PRs #39-#44 2026-05-28..29). Each composer constructs
    # `EntryPayload` from `harness_is.state_ledger_write` per CP spec
    # v1.25 §16.5.3 contract — vanilla Pattern-P1 symbol-equality
    # CP→IS edge. Rows 40 + 42 + 43 share consumer module
    # `pause_resume_protocol` (1 workflow-layer class method + 2
    # engine-layer free functions per CP spec v1.11 §26 NOTE
    # disjoint-primitives declaration); the (consumer_mod, producer_mod)
    # pair enforcement at this test allows duplicate triples at different
    # edge-names.
    (
        "U-CP-74→U-IS-11",
        "harness_cp.per_step_override_evaluator",
        "harness_is.state_ledger_write",
        "EntryPayload",
    ),
    (
        "U-CP-75→U-IS-11",
        "harness_cp.workload_binding_engine_class_selection",
        "harness_is.state_ledger_write",
        "EntryPayload",
    ),
    (
        "U-CP-76→U-IS-11",
        "harness_cp.pause_resume_protocol",
        "harness_is.state_ledger_write",
        "EntryPayload",
    ),
    (
        "U-CP-77→U-IS-11",
        "harness_cp.hitl_as_tool_call_rewriting",
        "harness_is.state_ledger_write",
        "EntryPayload",
    ),
    (
        "U-CP-78→U-IS-11",
        "harness_cp.pause_resume_protocol",
        "harness_is.state_ledger_write",
        "EntryPayload",
    ),
    (
        "U-CP-79→U-IS-11",
        "harness_cp.pause_resume_protocol",
        "harness_is.state_ledger_write",
        "EntryPayload",
    ),
)


# ---------------------------------------------------------------------------
# Sanity: count.
# ---------------------------------------------------------------------------


def test_seam_count_is_31() -> None:
    """CXA v2.17 §2.3 enumerates 31 genuine typed seams.

    v2.3 baseline: 22 seams. v2.4 added §2.3.7 with 1 seam (U-CP-28 → U-OD-00
    sub-agent dispatch audit-write). v2.5 grew §2.3.7 to 2 seams (added
    U-CP-46 → U-OD-00 HITL gate response audit-write per Q3 ratification —
    shared `cp_audit_to_od_audit` converter at `harness-cxa/`).

    Cluster 10-CP-C close (2026-05-21) added a 25th seam: U-CP-68 → U-AS-03
    (PerServerTrustEvaluator.evaluate() signature consumes ToolContract per CP
    spec v1.10 §27.1). CXA v2.15 §2.3.3 CP→AS amendment landed (5 → 6
    in-bucket; aggregate 24 → 25) absorbing the seam.

    CXA v2.17 (2026-05-31) absorbs 6 NEW §2.3.2 rows at U-CP-74..U-CP-79
    §16.5 CP→IS composer atomic-unit LANDED events from PRs #39-#44
    (2026-05-28..29) closing the v2.16 §0.4 forward-tracking marker
    6-PENDING transit to 6-ABSORBED. CP→IS bucket grows 37 → 43 canonical;
    9 → 15 genuine. Aggregate genuine 25 → 31.
    """
    assert len(PATTERN_P1_SEAMS) == 31


# ---------------------------------------------------------------------------
# Per-seam identity-equality test (the 22 assertions per AC).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("edge", "consumer_module", "producer_module", "symbol"),
    PATTERN_P1_SEAMS,
    ids=[row[0] for row in PATTERN_P1_SEAMS],
)
def test_pattern_p1_identity_per_seam(
    edge: str,
    consumer_module: str,
    producer_module: str,
    symbol: str,
) -> None:
    """For each genuine typed seam: consumer's bound symbol IS producer's export.

    Failure message names the CXA edge label + both module names + symbol so
    a CI log reader can diagnose without the edge taxonomy in front of them.
    """
    consumer = importlib.import_module(consumer_module)
    producer = importlib.import_module(producer_module)

    consumer_value = getattr(consumer, symbol, None)
    producer_value = getattr(producer, symbol, None)

    assert producer_value is not None, (
        f"Pattern P1 violation ({edge}): producer module "
        f"{producer_module!r} does not export {symbol!r}"
    )
    assert consumer_value is not None, (
        f"Pattern P1 violation ({edge}): consumer module "
        f"{consumer_module!r} does not bind {symbol!r}"
    )
    assert consumer_value is producer_value, (
        f"Pattern P1 violation ({edge}): "
        f"{consumer_module}.{symbol} ({consumer_value!r}) "
        f"is NOT {producer_module}.{symbol} ({producer_value!r}) — "
        f"identity-equality broken (local redefinition or shadowing)"
    )


# ---------------------------------------------------------------------------
# Discovery completeness — find all cross-axis imports and check coverage.
# ---------------------------------------------------------------------------


_AXES = ("harness_as", "harness_cp", "harness_od", "harness_is")


def _axis_of(module_path: str) -> str | None:
    """Return the top-level axis name (`harness_X`) of a dotted module path."""
    parts = module_path.split(".")
    if parts[0] in _AXES:
        return parts[0]
    return None


def _walk_cross_axis_imports(src_root: Path, owner_axis: str) -> list[tuple[str, str, str]]:
    """AST-walk `src_root/*.py` for `from harness_X import Y` where X != owner.

    Returns a list of (consumer_module, producer_module_or_axis, symbol).
    """
    results: list[tuple[str, str, str]] = []
    for py in sorted(src_root.rglob("*.py")):
        if "__pycache__" in py.parts:
            continue
        try:
            tree = ast.parse(py.read_text(), filename=str(py))
        except SyntaxError:
            continue

        # Reconstruct the consumer module's dotted name relative to src_root.
        rel = py.relative_to(src_root.parent)
        # e.g. src/harness_cp/foo.py → harness_cp.foo
        mod_parts = list(rel.with_suffix("").parts)
        if mod_parts and mod_parts[0] == "src":
            mod_parts = mod_parts[1:]
        if mod_parts and mod_parts[-1] == "__init__":
            mod_parts = mod_parts[:-1]
        consumer_mod = ".".join(mod_parts)

        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module is None:
                continue
            target_axis = _axis_of(node.module)
            if target_axis is None or target_axis == owner_axis:
                continue
            for alias in node.names:
                results.append((consumer_mod, node.module, alias.name))
    return results


# Symbols that ARE imported across axis boundaries but aren't in the 22-seam
# enumeration because they're allowlisted convention/runtime per CXA v2.3.
# Includes anything the discovery walk finds that maps to a non-genuine class.
_ALLOWLISTED_CROSS_AXIS_SYMBOLS: frozenset[tuple[str, str]] = frozenset(
    {
        # (producer_module, symbol) pairs that ARE cross-axis Python imports
        # but NOT in the 22-row genuine-seam set per CXA v2.3 §2.3.
        #
        # Re-exports through axis package roots — the canonical seam targets
        # the producer's defining module; the package-root binding is the
        # same object by identity.
        ("harness_is", "Identifier"),  # re-export of state_ledger_entry_schema.Identifier
        ("harness_is", "PathClass"),  # re-export
        ("harness_is", "PathClassMetadata"),  # re-export
        ("harness_as", "GateLevel"),
        ("harness_as", "SandboxTier"),
        ("harness_as", "SecretRef"),
        ("harness_as", "SecretScope"),
        ("harness_as", "BlastRadiusTier"),
        ("harness_as", "fetch_secret"),
        # Symbols on already-enumerated seams beyond the canonical one
        # (U-AS-26 / U-CP-34 → U-IS-07 covers StateLedgerEntry + neighbors).
        ("harness_is.state_ledger_entry_schema", "Actor"),
        ("harness_is.state_ledger_entry_schema", "ActorClass"),
        ("harness_is.state_ledger_entry_schema", "Timestamp"),  # U-AS-26 secondary
        # U-CP-84 producer consumes the U-IS-19 `BranchMetadata` sidecar carrier
        # (a StateLedgerEntry neighbor on the §10.1 entry-shape export) to compose
        # the branch_metadata write-cadence — same CP→IS U-CP-34 → U-IS-07 seam.
        ("harness_is.state_ledger_entry_schema", "BranchMetadata"),
        ("harness_is.state_ledger_write", "EntryPayload"),
        ("harness_is.state_ledger_write", "WriteKey"),
        ("harness_is.state_ledger_write", "WriteResult"),
        ("harness_is.chain_verification", "ChainVerificationResult"),
        # U-CP-34 → U-IS-11 picks `append_ledger_entry` as the canonical seam
        # symbol; the JsonlLedgerHandle import is the carrier type per C-IS-07.
        ("harness_is.jsonl_event_ledger_lifecycle", "JsonlLedgerHandle"),
        # CP plan v2.12 — U-CP-56 selective replay-resumption consumes the
        # IS BoundedWindow shape per C-IS-07 §7.2 in `_determine_resume_at`.
        # Composes against the same U-CP-56 → U-IS-07 seam axis (state-ledger
        # read substrate via the §7.4 implementation-discretion clause).
        ("harness_is.state_ledger_read", "BoundedWindow"),
        # U-MEM access-mode selection consumes existing IS memory
        # policy/profile/scope carrier records to choose CP-side provider
        # access posture. These imports classify as convention/runtime carriers,
        # not new Pattern-P1 byte-exact seam obligations.
        ("harness_is.cli_profile", "CliProfile"),
        ("harness_is.memory_policy", "AccessDecision"),
        ("harness_is.memory_policy", "MemoryPolicyDocument"),
        ("harness_is.memory_record_envelope", "MemoryScope"),
        # CLASS 3 DRIFT: U-OD-20 → CP `ReplayDisposition` is a genuine typed
        # seam that CXA v2.3 §2.3.6 missed (audit visited only 12 OD→CP
        # rows, all classified convention/runtime). Drift filed at
        # `.harness/class_3_drift_u_rt_51_od_cp_replay_disposition_seam.md`.
        # Future CXA revision pass bumps genuine count 22 → 23.
        ("harness_cp.engine_namespace", "ReplayDisposition"),
        # Pause/resume engine-layer projection-helper imports — consumed by
        # `harness_od.pause_resume_namespace._project_pause_event_to_audit_payload`
        # + `_project_resume_outcome_to_audit_payload` per OD spec v1.11
        # §C-OD-30.4 (helper-contract landing at fork §10 commit `10129c8`,
        # 2026-05-24). CP→OD pause/resume audit-write seam IS enumerated at
        # CXA v2.9 §2.3.7 + §0.3 8-prefix discriminator (`pause:` / `resume:`);
        # these carrier symbols are the projection inputs at the CXA-homed
        # converter dispatch. Engine-layer (§22 / C-CP-22 / U-CP-49) carriers
        # only — workflow-layer (§26 / C-CP-26) carriers PauseSnapshot /
        # ResumeResult NOT imported here per fork §11 won't-fix closure
        # (commit `1b7bcb0`, 2026-05-24; lossy projection foreclosed by
        # CP spec v1.11 §26 NOTE disjoint-primitives declaration).
        ("harness_cp.pause_resume_protocol", "PauseEvent"),
        ("harness_cp.pause_resume_protocol", "ResumeAttempt"),
        ("harness_cp.pause_resume_protocol", "ResumeOutcome"),
        ("harness_cp.pause_resume_protocol", "ResumeOutcomeKind"),
    }
)


def test_cross_axis_imports_match_enumerated_seams() -> None:
    """Completeness: every cross-axis import is either in the 22 seams or allowlisted.

    Guards against new genuine typed seams landing without CXA v2.3 + this
    test being updated. If this test fails on a new symbol, either:
    - Add the symbol to PATTERN_P1_SEAMS (it's a new genuine seam → CXA needs revision), OR
    - Add to _ALLOWLISTED_CROSS_AXIS_SYMBOLS (it's a convention/runtime import we've classified).
    """
    repo_root = Path(__file__).resolve().parents[3]
    consumers = {
        "harness_as": repo_root / "harness-as" / "src" / "harness_as",
        "harness_cp": repo_root / "harness-cp" / "src" / "harness_cp",
        "harness_od": repo_root / "harness-od" / "src" / "harness_od",
    }

    found_triples: list[tuple[str, str, str]] = []
    for owner_axis, src_root in consumers.items():
        assert src_root.is_dir(), f"missing source dir: {src_root}"
        found_triples.extend(_walk_cross_axis_imports(src_root, owner_axis))

    # Build the enumerated-seam set keyed by (producer_module, symbol).
    enumerated: set[tuple[str, str]] = {(row[2], row[3]) for row in PATTERN_P1_SEAMS}

    # Symbols permitted to cross axes that aren't in the genuine-seam set.
    permitted = enumerated | _ALLOWLISTED_CROSS_AXIS_SYMBOLS

    unaccounted: list[tuple[str, str, str]] = []
    for consumer_mod, producer_mod, symbol in found_triples:
        if (producer_mod, symbol) in permitted:
            continue
        unaccounted.append((consumer_mod, producer_mod, symbol))

    assert not unaccounted, (
        "Found cross-axis imports not enumerated in PATTERN_P1_SEAMS or "
        "_ALLOWLISTED_CROSS_AXIS_SYMBOLS — update CXA v2.3 + this test:\n  "
        + "\n  ".join(f"{c} imports {s!r} from {p}" for c, p, s in unaccounted)
    )


def test_all_enumerated_seams_have_consumer_import() -> None:
    """Inverse completeness: every enumerated seam corresponds to a real import.

    Catches enumeration drift — a CXA row that's claimed to be a typed seam
    but has no actual Python import in the landed code.
    """
    repo_root = Path(__file__).resolve().parents[3]
    consumers = {
        "harness_as": repo_root / "harness-as" / "src" / "harness_as",
        "harness_cp": repo_root / "harness-cp" / "src" / "harness_cp",
        "harness_od": repo_root / "harness-od" / "src" / "harness_od",
    }

    found_triples: set[tuple[str, str]] = set()
    for owner_axis, src_root in consumers.items():
        for consumer_mod, producer_mod, symbol in _walk_cross_axis_imports(src_root, owner_axis):
            _ = consumer_mod
            found_triples.add((producer_mod, symbol))

    # Each enumerated seam's symbol should be importable from SOME consumer.
    # Allow producer-module-canonical OR axis-package re-export.
    missing: list[str] = []
    for edge, consumer_mod, producer_mod, symbol in PATTERN_P1_SEAMS:
        # Either the canonical module or the axis re-export must appear.
        producer_axis = producer_mod.split(".")[0]
        if (producer_mod, symbol) in found_triples:
            continue
        if (producer_axis, symbol) in found_triples:
            continue
        # The consumer module itself imports it — check via importlib.
        try:
            consumer = importlib.import_module(consumer_mod)
        except ImportError:
            missing.append(f"{edge}: cannot import consumer {consumer_mod}")
            continue
        if getattr(consumer, symbol, None) is None:
            missing.append(
                f"{edge}: {consumer_mod} does not bind {symbol!r} (producer {producer_mod})"
            )

    assert not missing, "Enumerated seams without landed imports:\n  " + "\n  ".join(missing)
