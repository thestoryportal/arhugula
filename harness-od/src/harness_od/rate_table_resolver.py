"""U-OD-48 — PRICE_TABLE_REF resolution discipline.

Materializes OD spec v1.8 §C-OD-28.2 (resolution discipline) + §28.4 invariant 4
(provider-then-model resolution). Resolves a (provider, model) pair against a
`RateTable` into a `ProviderRates` set, preferring per-model overrides over
provider-level defaults.

Per AC #3 — caching at workflow scope (immutable post-resolution) — is the
caller's concern (workflow_driver per §C-OD-28.2 "ctx.rate_table.resolve_for(...)").
This module exposes the resolution function as a pure surface; callers
memoize the result for the workflow's lifetime.

Per AC #4 — Decimal arithmetic throughout — is preserved trivially: this
module returns the `ProviderRates` shape unchanged (Decimal fields preserved).

Authority:
- `Spec_Operational_Discipline_v1_8.md` §C-OD-28.2 + §28.4 invariant 4
- `Implementation_Plan_Operational_Discipline_v2_14.md` U-OD-48
"""

from __future__ import annotations

from harness_od.rate_table_types import ProviderRates, RateTable


class RateTableMissingError(LookupError):
    """`CP-FAIL-RATE-TABLE-MISSING` — provider not in rate table.

    Per §C-OD-28.2 — resolution failure (rate-table missing OR provider/model
    not in table) raises this typed error. Operator may flip to fail-open
    (`cost_chain_noop`) via bootstrap config; default is fail-closed (raise).

    Fail-class name follows the CP-FAIL-* taxonomy convention per
    `Spec_Control_Plane_v1_10.md` §B failure-mode catalog; the error class
    is homed at OD axis (its emission site is OD cost-attribution per
    U-OD-39 / U-OD-40 / U-OD-41).
    """


def resolve_for(
    rate_table: RateTable,
    provider: str,
    model: str | None,
) -> ProviderRates:
    """Resolve (provider, model) → ProviderRates per §C-OD-28.2.

    Resolution order per §C-OD-28.4 invariant 4:
      1. Look up `provider` in `rate_table.providers`. Missing → raise
         `RateTableMissingError`.
      2. If `model is not None` AND `model in provider_rates.per_model_overrides`:
         return the per-model override.
      3. Otherwise: return the provider-level `ProviderRates`.

    Parameters
    ----------
    rate_table
        The resolved `RateTable` for this workflow's execution (per
        §C-OD-28.4 invariant 1 — immutable for the workflow's lifetime).
    provider
        Provider name (e.g., "anthropic"). Must be a key in
        `rate_table.providers`.
    model
        Model identifier (e.g., "claude-sonnet-4-6") or None for
        provider-level resolution. Per-model overrides resolve before
        provider-level fallback.

    Returns
    -------
    ProviderRates
        The resolved per-(provider, model) rate set.

    Raises
    ------
    RateTableMissingError
        `provider` is not present in `rate_table.providers`. Maps to
        `CP-FAIL-RATE-TABLE-MISSING` per §C-OD-28.2.
    """
    base = rate_table.providers.get(provider)
    if base is None:
        raise RateTableMissingError(
            f"CP-FAIL-RATE-TABLE-MISSING: provider {provider!r} not in rate "
            f"table version {rate_table.version!r}"
        )

    # Per-model override resolution (§C-OD-28.4 invariant 4 — model resolves
    # before falling back to provider-level).
    if model is not None and base.per_model_overrides is not None:
        override = base.per_model_overrides.get(model)
        if override is not None:
            return override

    return base
