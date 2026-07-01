"""Tests for U-CP-74 shared canonicalization helper (CP spec v1.26 §16.5.5).

Acceptance-criterion coverage (U-CP-74 AC #8):
  sorted keys + deterministic   -> test_canonicalize_outcome_bytes_sorted_keys_deterministic
  rejects NaN / Infinity        -> test_canonicalize_outcome_bytes_rejects_nan_infinity
"""

from __future__ import annotations

import math

import pytest
from harness_cp.state_ledger_canonicalization import _canonicalize_outcome_bytes
from pydantic import BaseModel


def test_canonicalize_outcome_bytes_sorted_keys_deterministic() -> None:
    """Sorted keys + (',',':') separators → byte-identical output across key orders."""
    a = _canonicalize_outcome_bytes({"b": 2, "a": 1})
    b = _canonicalize_outcome_bytes({"a": 1, "b": 2})
    assert a == b
    assert a == b'{"a":1,"b":2}'


def test_canonicalize_outcome_bytes_rejects_nan_infinity() -> None:
    """ECMA-404: reject NaN / Infinity / -Infinity (allow_nan=False)."""
    with pytest.raises(ValueError):
        _canonicalize_outcome_bytes({"x": math.nan})
    with pytest.raises(ValueError):
        _canonicalize_outcome_bytes({"x": math.inf})
    with pytest.raises(ValueError):
        _canonicalize_outcome_bytes({"x": -math.inf})


def test_canonicalize_outcome_bytes_accepts_pydantic_basemodel() -> None:
    """BaseModel input is converted via model_dump before canonicalization."""

    class _M(BaseModel):
        b: int
        a: int

    bytes_from_model = _canonicalize_outcome_bytes(_M(b=2, a=1))
    bytes_from_mapping = _canonicalize_outcome_bytes({"a": 1, "b": 2})
    assert bytes_from_model == bytes_from_mapping


def test_canonicalize_outcome_bytes_nested_keys_sorted_recursively() -> None:
    """Nested mappings have their keys sorted at every level."""
    a = _canonicalize_outcome_bytes({"outer": {"b": 2, "a": 1}})
    b = _canonicalize_outcome_bytes({"outer": {"a": 1, "b": 2}})
    assert a == b
