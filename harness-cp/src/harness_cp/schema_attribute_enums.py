"""Schema-attribute utility enums — U-CP-00b (re-export surface).

`AttributeValueType` and `Cardinality` — the value-type and cardinality
discriminators the CP plan uses to type its `…AttributeSchema` records (the
per-namespace attribute-schema records at U-CP-01/07/11/21/31/37/46/47).

**Carrier-home re-home.** U-CP-00b originally *declared* these two enums here
in `harness-cp` under operator decision D3 ("all consumers are CP-axis units;
no cross-axis sharing"). The U-AS-31 Class 1 fork
(`.harness/class_1_tension_u_as_31_attribute_schema_enums.md`,
operator-ruled 2026-05-16) established that they are genuinely cross-axis:
U-AS-31 (AS axis, C-AS-14 namespace schemas) consumes them too, and an AS→CP
package edge cycles against the 24 declared CP→AS edges (CXA v2.1 §2.3.4).
The enums are re-homed to `harness-core` (the U-CORE-01 cross-axis shared-type
pattern); this module re-exports them so CP-side `harness_cp.schema_attribute_enums`
citations remain stable.

Authority: Implementation_Plan_Control_Plane_v2_7.md §2.0b U-CP-00b;
`.harness/class_1_tension_u_as_31_attribute_schema_enums.md` (re-home).
"""

from __future__ import annotations

from harness_core import AttributeValueType, Cardinality

__all__ = ["AttributeValueType", "Cardinality"]
