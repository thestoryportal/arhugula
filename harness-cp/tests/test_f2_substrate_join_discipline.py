"""Tests for U-CP-18 — F2 substrate join discipline (C-CP-08 §8.2).

Acceptance-criterion coverage:
  #1 F2JoinKind cardinality 3       -> test_f2_join_kind_cardinality_three
  #2 5 contracts, per-engine kinds  -> test_engine_f2_join_contracts_cardinality_five,
                                       test_per_engine_join_kind_match_spec
  #3 F2 shape preserved across kinds-> test_f2_shape_preserved_across_join_kinds
  #4 delegates to U-IS-07/09/12     -> test_delegates_to_u_is_07_09_12
  #5 R-CP-07-satisfying contract    -> structural (the 5-entry table is it)
"""

from __future__ import annotations

from harness_cp.engine_class import EngineClass
from harness_cp.f2_substrate_join_discipline import (
    ENGINE_F2_JOIN_CONTRACTS,
    EngineF2JoinContract,
    F2JoinKind,
    f2_join_contract,
)


def test_f2_join_kind_cardinality_three() -> None:
    """Acceptance #1 — `F2JoinKind` declares exactly three values."""
    assert len(F2JoinKind) == 3
    assert {k.name for k in F2JoinKind} == {
        "ENGINE_NATIVE_LEDGER",
        "HARNESS_OVERLAY_LEDGER",
        "CRD_RECONCILER_LEDGER",
    }


def test_engine_f2_join_contracts_cardinality_five() -> None:
    """Acceptance #2 — exactly 5 contracts, one per `EngineClass`."""
    assert len(ENGINE_F2_JOIN_CONTRACTS) == 5
    assert {c.engine_class for c in ENGINE_F2_JOIN_CONTRACTS} == set(EngineClass)
    assert all(isinstance(c, EngineF2JoinContract) for c in ENGINE_F2_JOIN_CONTRACTS)


def test_per_engine_join_kind_match_spec() -> None:
    """Acceptance #2 — per-engine join kind matches C-CP-08 §8.2 verbatim."""
    expected = {
        EngineClass.EVENT_SOURCED_REPLAY: F2JoinKind.ENGINE_NATIVE_LEDGER,
        EngineClass.SAVE_POINT_CHECKPOINT: F2JoinKind.HARNESS_OVERLAY_LEDGER,
        EngineClass.PURE_PATTERN_NO_ENGINE: F2JoinKind.HARNESS_OVERLAY_LEDGER,
        EngineClass.RECONCILER_LOOP: F2JoinKind.CRD_RECONCILER_LEDGER,
        EngineClass.WAL_SEGMENT: F2JoinKind.HARNESS_OVERLAY_LEDGER,
    }
    for engine, kind in expected.items():
        assert f2_join_contract(engine).join_kind == kind


def test_f2_shape_preserved_across_join_kinds() -> None:
    """Acceptance #3 — every join kind exposes the same F2 read/write contract.

    The F2 six-field shape is preserved regardless of join kind: every
    contract delegates to the same U-IS-07 read/write primitive.
    """
    read_contracts = {c.read_contract for c in ENGINE_F2_JOIN_CONTRACTS}
    write_contracts = {c.write_contract for c in ENGINE_F2_JOIN_CONTRACTS}
    assert len(read_contracts) == 1
    assert len(write_contracts) == 1


def test_delegates_to_u_is_07_09_12() -> None:
    """Acceptance #4 — delegation pointers cite U-IS-07 / U-IS-09 / U-IS-12."""
    for c in ENGINE_F2_JOIN_CONTRACTS:
        assert "U-IS-07" in c.read_contract
        assert "U-IS-07" in c.write_contract and "U-IS-09" in c.write_contract
        assert "U-IS-09" in c.chain_construction
        assert "U-IS-12" in c.idempotency_key_path


def test_f2_join_contract_total_over_engine_class() -> None:
    """Acceptance #5 — `f2_join_contract` resolves for every engine class."""
    for engine in EngineClass:
        contract = f2_join_contract(engine)
        assert contract.engine_class == engine
