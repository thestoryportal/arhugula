"""`HITLAutoApprovePolicy` — the CP §19.5 operator-policy floor-override schema.

Authority: C-RT-03, C-CP-19, and U-RT-116.

Per `Spec_Harness_Runtime_v1.md` §3.8 (NEW at v1.49) + `.harness/
class_1_fork_b3_1_hitl_auto_approve_policy_field.md` (F-B3-1; R-FS-1 B3-spec-1)
+ design `.harness/r-fs-1-b3-smart-hitl-design-v1.md` §3.3 D-cond.2 (Reading C
tunable-floor). Materializes the CP §19.5 *"operator-policy override of any
`max()` floor"* **authoring schema** that CP v1.2 §19.5 (line 1702) deferred to
implementation discretion.

**Reading C — tunable floor (in-`max()`, NOT a post-`max()` bypass).** A two-bool
**named-cell** override of the two §19.1-annotated overridable floor cells only:

- `solo_persona_floor_auto` → `persona_tier_floor[SOLO_DEVELOPER]` → `AUTO`
  (§19.1 line 1639 *"operator may override to auto for non-irreversible"*).
  Default **ON** ⇒ READ_ONLY auto-ON.
- `solo_local_mutation_floor_auto` → `blast_radius_floor[LOCAL_MUTATION]` → `AUTO`
  (§19.1 line 1634 *"configurable to auto at solo-developer"*). Default **OFF** ⇒
  operator opt-in.

The two-bool named-cell shape is **normative**: it can express **exactly** the two
§19.1-annotated overridable floor cells and nothing else. A Reading-D post-`max()`
bypass `Mapping[(persona_tier, blast_radius_tier), bool]` (rejected at design §3.3)
is **not representable**, and EXTERNAL_REVERSIBLE / EXTERNAL_IRREVERSIBLE override
is **structurally foreclosed** (no field expresses it — AC-2). Solo-scoped: the
composer applies the knobs only at `SOLO_DEVELOPER`, so multi-tenant-compliance is
structurally foreclosed (no override *attempt* to refuse) and team-binding override
is a registered owed follow-on (F-B3-1 §6), not deferred-and-dropped.

Leaf module (pydantic only, no `harness_runtime` imports) so both
`harness_runtime.types.RuntimeConfig` and `harness_runtime.lifecycle.
hitl_gate_composer` import it without a cycle.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

__all__ = ["HITLAutoApprovePolicy"]


class HITLAutoApprovePolicy(BaseModel):
    """Two-bool solo-scoped named-cell §19.5 floor-override (runtime spec §3.8)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    solo_persona_floor_auto: bool = True
    """§19.1 line 1639 — `persona_tier_floor[SOLO_DEVELOPER]` → `AUTO`
    (*"operator may override to auto for non-irreversible"*). Default ON ⇒
    READ_ONLY auto-ON (the READ_ONLY blast floor is already `AUTO` independent of
    the policy, so lowering only the persona cell makes `max()` = `AUTO`)."""

    solo_local_mutation_floor_auto: bool = False
    """§19.1 line 1634 — `blast_radius_floor[LOCAL_MUTATION]` → `AUTO`
    (*"configurable to auto at solo-developer"*). Default OFF ⇒ operator opt-in.
    Applied ONLY when the step's resolved `blast_radius_tier == LOCAL_MUTATION`
    (the named cell) — never to EXTERNAL_* (the hard-stop floor stays `ASK`)."""
