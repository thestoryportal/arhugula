"""Shared outcome-canonical-bytes helper for §16.5 CP→IS composers — U-CP-74.

Implements CP spec v1.26 §16.5.5 implementation note + §16.5.4 idempotency-key
suffix discipline per Q-β.i-1(a). Declares `_canonicalize_outcome_bytes` —
the producer of outcome-canonical-bytes consumed by per-composer idempotency-key
derivation across U-CP-74..79.

**Canonicalization scheme.** Mirror v1.7 §13.5.1 `cp_audit_to_od_audit`
converter JSON-canonicalization: sorted keys, `(",", ":")` separators, UTF-8
encode, NaN/Infinity rejection per ECMA-404. The bytes are SHA-256-hashed at
the caller site (§16.5.4 suffix segment).

Authority: Implementation_Plan_Control_Plane_v2_29.md §1 U-CP-74 AC #8;
Spec_Control_Plane_v1_26.md §16.5.5 implementation note + §16.5.4 Q-β.i-1(a)
formula extension.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

__all__ = ["_canonicalize_outcome_bytes"]
"""Intentional public surface despite leading underscore: the spec-mandated
helper name at CP spec v1.26 §16.5.5 (the underscore signals 'intra-harness-cp
axis' helper, consumed by §16.5.2 composers at U-CP-74..79; not exported beyond
harness-cp)."""


def _canonicalize_outcome_bytes(payload: BaseModel | Mapping[str, Any]) -> bytes:
    """Produce canonical-form bytes of a composer outcome payload (§16.5.5).

    Sorted keys, `(",", ":")` separators, UTF-8 encode. Rejects NaN / Infinity
    per ECMA-404 (`allow_nan=False` raises `ValueError`). Accepts a Pydantic
    `BaseModel` or a plain mapping; `BaseModel` is converted to its dump form
    first.
    """
    if isinstance(payload, BaseModel):
        as_mapping = payload.model_dump(mode="json")
    else:
        as_mapping = dict(payload)
    return json.dumps(
        as_mapping,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
