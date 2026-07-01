"""Per-role binding catalog surface — the B1↔B4 `step_id → AgentRole` contract.

R-FS-1 child-arc **B4 Slice 2** (the per-role binding *catalog*). CP spec v1.32
§25.14 (the B1↔B4 role seam) + runtime spec v1.48 §14.5.3 both name the per-role
binding **catalog** — operator-authored ``per_role_bindings`` content for both
``RoutingManifest`` (C-CP-01 §1.3, per-role *model*) and
``PromptSelectionManifest`` (C-CP-29 §29, per-role *prompt*) — as the distinct
B4 deliverable, **deferred to implementation discretion** (runtime spec §14.5.3
closing line). This module is that surface.

**What B1 + B4-Slice-1 already landed (the *mechanism*).** A fan-out branch's
child ``StepExecutionContext`` carries an ``AgentRole`` (CP-composed at
branch-spawn, CP §25.11/§25.14). The runtime dispatch seam reads
``step_context.agent_role`` to index the per-role **model** binding (B1 U-RT-114,
``retry_breaker_fallback._effective_chain``) and the per-role **prompt** (B4
Slice 1 #616, the §14.5.2 translate seam). Both index the structurally-present
``per_role_bindings`` maps; an unbound role falls through to the default (§14.5.3
+ §29 lookup-miss policy — intentional zero-config, the C11 local-first default).

**What this module adds (B4 Slice 2 — the *catalog* surface + validation).** The
mechanism keys on a role the **driver derives** from a fan-out worker's
``step_id`` (``AgentRole(str(step.step_id))``, the three driver sites at
``workflow_driver.py``). For an operator to author a catalog that *actually
binds* those workers, the derivation must be a **single shared contract** — not a
literal duplicated between the driver (the producer) and the operator's head (the
catalog author). ``derive_agent_role`` is that one source of truth; the driver
calls it, and an operator keys ``per_role_bindings`` on it. ``validate_per_role_
catalog`` is the deterministic authoring/validation aid: given the roles a
workflow's fan-out *derives* and the roles a catalog *binds*, it reports the
live bindings, the **dead** bindings (bound but underivable — the typo class),
and the unbound roles (derivable but bound to nothing — the intentional
fall-through). It is **not** a runtime gate: a dead binding never fires and an
unbound role is the committed fall-through, so neither is fail-loud — surfacing
them is an authoring aid, not an admissibility check (variability-in-values).

Authority: Spec_Control_Plane_v1_32.md §25.14 (B1↔B4 role seam, D2);
Spec_Harness_Runtime_v1.md v1.48 §14.5.3 (catalog deferred to impl discretion);
C-CP-01 §1.3 (``RoutingManifest.per_role_bindings``); C-CP-29 §29
(``PromptSelectionManifest.per_role_bindings``).
"""

from __future__ import annotations

from collections.abc import Iterable

from harness_core import StepID
from pydantic import BaseModel, ConfigDict

from harness_cp.cp_shared_types import AgentRole

__all__ = [
    "PerRoleCatalogCoherence",
    "derive_agent_role",
    "derive_fanout_roles",
    "validate_per_role_catalog",
]


def derive_agent_role(step_id: StepID) -> AgentRole:
    """Derive the ``AgentRole`` a fan-out worker dispatches under, from its ``step_id``.

    The **single source of truth** for the B1↔B4 role-derivation contract
    (§25.14 / §14.5.3). The fan-out drivers compose each worker's child
    ``StepExecutionContext`` with ``agent_role = derive_agent_role(step.step_id)``
    (``workflow_driver.py`` orchestrator-workers + decentralized/hierarchical
    sites); an operator authoring ``RoutingManifest.per_role_bindings`` /
    ``PromptSelectionManifest.per_role_bindings`` keys the binding on the same
    function for the same ``step_id``, so the two agree by construction (no
    duplicated literal to drift).

    ``StepID`` and ``AgentRole`` are both open ``str`` newtypes; the derivation
    is the identity on the underlying string (``AgentRole(str(step_id))``).
    Pure + deterministic."""
    return AgentRole(str(step_id))


def derive_fanout_roles(step_ids: Iterable[StepID]) -> frozenset[AgentRole]:
    """The set of ``AgentRole``s a fan-out over ``step_ids`` derives.

    A convenience over :func:`derive_agent_role` for the catalog-coherence
    caller — the operator's set of bindable worker roles for a given workflow's
    fan-out steps. Pure; order-independent (a set)."""
    return frozenset(derive_agent_role(step_id) for step_id in step_ids)


class PerRoleCatalogCoherence(BaseModel):
    """Coherence of an operator-authored per-role catalog against a fan-out's roles.

    The result of :func:`validate_per_role_catalog`. Frozen + ``extra="forbid"``.
    The three sets partition ``bound_roles ∪ derivable_roles``:

    - ``live_roles`` — bound **and** derivable: the catalog entries that index a
      real fan-out worker. The behaviour-driving subset.
    - ``dead_bindings`` — bound but **not** derivable: a binding no fan-out
      worker derives (the operator-typo / stale-catalog class). Never fires; not
      an error (a superset catalog reused across workflows binds roles unused by
      any one of them), but the actionable authoring diagnostic.
    - ``unbound_roles`` — derivable but **not** bound: a fan-out worker with no
      catalog entry. Falls through to the default binding per the committed
      §14.5.3 / §29 lookup-miss policy — intentional zero-config, informational."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    live_roles: frozenset[AgentRole]
    dead_bindings: frozenset[AgentRole]
    unbound_roles: frozenset[AgentRole]

    @property
    def has_dead_bindings(self) -> bool:
        """``True`` iff the catalog binds a role no fan-out worker derives.

        The actionable authoring signal — a ``True`` here usually means a typo in
        a ``per_role_bindings`` key or a catalog left stale against a renamed
        workflow step. Advisory, never fail-loud."""
        return bool(self.dead_bindings)


def validate_per_role_catalog(
    *,
    derivable_roles: Iterable[AgentRole],
    bound_roles: Iterable[AgentRole],
) -> PerRoleCatalogCoherence:
    """Report how an operator-authored per-role catalog lines up with a fan-out.

    Generic over the catalog kind — pass ``manifest.per_role_bindings.keys()`` as
    ``bound_roles`` for *either* ``RoutingManifest`` (per-role model) *or*
    ``PromptSelectionManifest`` (per-role prompt), and the roles a workflow's
    fan-out derives (via :func:`derive_fanout_roles`) as ``derivable_roles``.

    Pure set algebra (deterministic): ``live = bound ∩ derivable``,
    ``dead = bound − derivable``, ``unbound = derivable − bound``. Returns the
    breakdown; raises nothing — a dead binding and an unbound role are both
    benign under the committed semantics (the fall-through-to-default lookup-miss
    policy), so this is an authoring/validation aid, not an admissibility gate."""
    derivable = frozenset(derivable_roles)
    bound = frozenset(bound_roles)
    return PerRoleCatalogCoherence(
        live_roles=bound & derivable,
        dead_bindings=bound - derivable,
        unbound_roles=derivable - bound,
    )
