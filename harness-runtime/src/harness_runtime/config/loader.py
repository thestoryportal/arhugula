"""U-RT-04 — `RuntimeConfig` precedence resolver.

Per `Spec_Harness_Runtime_v1.md` v1.1 §3 (C-RT-03):

> Precedence at construction: kwargs to `run()` > environment variables >
> defaults. (No file-loading; that is Track B.)

This module materializes a `RuntimeConfig` from three sources, deterministically:

1. Per-field default (when the field has one).
2. Environment-variable lookup (env > defaults).
3. Caller-provided kwargs (kwargs > env > defaults).

C-RT-03 "Deferred to implementation discretion" surfaces filled here:

- **Env-var naming.** `HARNESS_*` prefix per the spec suggestion. Each
  scalar top-level field has one env-var key; sub-config fields (path
  bindings, provider secrets, OTel, collector) come via kwargs only at L1
  entry. Sub-units (U-RT-05..U-RT-08) extend the env-keying surface as
  their sub-config fields land.
- **kwargs-vs-env precedence resolver implementation.** Dict-merge with
  kwargs last so kwargs win for any field both sources provide.

Field validators (path existence, keyring allowlist) live on the schema
itself (C-RT-03 invariants); the resolver layer only handles precedence +
type coercion of env strings.

NOTE: `RuntimeConfig.extra='forbid'` automatically rejects unknown keys; we
do not duplicate that check here.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from harness_core.deployment_surface import DeploymentSurface
from harness_cp.topology_pattern import TopologyPattern

from harness_runtime.types import RuntimeConfig

__all__ = [
    "ENV_PREFIX",
    "materialize_runtime_config",
]


ENV_PREFIX = "HARNESS_"


def _parse_bool(raw: str) -> bool:
    """Coerce an env-var string to bool.

    `bool("False")` is `True` in Python; we need an explicit parser.
    Accepts the common truthy spellings; everything else is False.
    """
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# Top-level scalar field map: field name -> (env var key, coercion callable).
# Sub-config fields are not in this map at L1 entry; they come in via kwargs.
#
# When adding a new scalar field to RuntimeConfig, add it here too — the
# loader does NOT auto-iterate model_fields. Without an entry, the env-var
# precedence path silently ignores the field.
_ENV_SCALAR_FIELDS: dict[str, tuple[str, Any]] = {
    "deployment_surface": (f"{ENV_PREFIX}DEPLOYMENT_SURFACE", DeploymentSurface),
    "repository_root": (f"{ENV_PREFIX}REPOSITORY_ROOT", Path),
    "default_topology": (f"{ENV_PREFIX}DEFAULT_TOPOLOGY", TopologyPattern),
    "tenant_id": (f"{ENV_PREFIX}TENANT_ID", str),
    "ollama_host": (f"{ENV_PREFIX}OLLAMA_HOST", str),
    "ollama_optional": (f"{ENV_PREFIX}OLLAMA_OPTIONAL", _parse_bool),
    # B-EFFECT-FENCE (§14.22 C-RT-31) — env-keyed because it gates a CORRECTNESS
    # property (at-most-once execution): an operator who sets HARNESS_EFFECT_FENCING
    # must NOT be silently left without the fence (the no-silent-failure discipline).
    # NOTE: the recent opt-in flags `inter_step_data_flow` / `anthropic_optional` /
    # `openai_optional` are NOT env-keyed here (file/CLI only) — a known env gap, a
    # separate config-hygiene item, intentionally not folded into this arc.
    "effect_fencing": (f"{ENV_PREFIX}EFFECT_FENCING", _parse_bool),
    # B-L2-EMBEDDING-ACTIVATION: env-keyed for the SAME reason as effect_fencing —
    # it gates a behavior-changing property (which model serves a workload), so an
    # operator who sets HARNESS_ROUTING_ACTIVATION must NOT be silently dropped
    # ([[runtimeconfig-scalar-needs-both-env-loaders]]; mirrored in config_source).
    "routing_activation": (f"{ENV_PREFIX}ROUTING_ACTIVATION", _parse_bool),
}


def materialize_runtime_config(
    *,
    env: Mapping[str, str] | None = None,
    **kwargs: Any,
) -> RuntimeConfig:
    """Build a `RuntimeConfig` with precedence: kwargs > env > defaults.

    Parameters
    ----------
    env :
        Environment-variable map. Defaults to `os.environ` if `None`; pass an
        explicit empty mapping in tests to assert env-independence.
    **kwargs :
        Per-field overrides. Wins over both env and defaults. Sub-config
        fields (`path_bindings`, `provider_secrets`, `otel`, `collector`,
        `mcp_clients`) come via kwargs only at L1 entry.

    Returns
    -------
    RuntimeConfig
        The materialized, frozen, validated config.

    Raises
    ------
    pydantic.ValidationError
        - Required field missing across all three precedence levels.
        - Unknown kwarg (`extra='forbid'` on `RuntimeConfig`).
        - Type mismatch survives precedence merge.
    """
    resolved_env = env if env is not None else os.environ

    materialized: dict[str, Any] = {}

    # Lower precedence: env vars.
    for field_name, (env_key, coerce) in _ENV_SCALAR_FIELDS.items():
        raw = resolved_env.get(env_key)
        if raw is not None:
            materialized[field_name] = coerce(raw)

    # Higher precedence: kwargs.
    materialized.update(kwargs)

    return RuntimeConfig(**materialized)
