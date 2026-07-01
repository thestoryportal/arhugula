"""`harness_runtime.lifecycle` — runtime composition primitives.

Per Phase 2 Session 3 plan v2.1 §1 layout, this subpackage hosts the
composition primitives the bootstrap stages instantiate: path-class
registry materialization (U-RT-10), state-ledger writer wrapper (U-RT-12),
shadow-Git supervisor (U-RT-11), content-addressed index + semantic cache
(U-RT-09), provider SDK construction (U-RT-17..20), TracerProvider
construction (U-RT-27), collector daemon supervisor (U-RT-29), MCP host
(U-RT-15), and audit-ledger writer (U-RT-32).

Modules land lazily as the units that need them land.
"""

from __future__ import annotations
