"""U-RT-05 — `PathBindingConfig` → `PathBinding` materialization.

Per `Spec_Harness_Runtime_v1.md` v1.1 §3 (C-RT-03 `path_bindings` field) and
Phase 2 Session 3 plan v2.1 §2 L1, this module:

- Builds a validated `harness_is.PathBinding` from a `PathBindingConfig`'s
  raw entry records via `harness_is.load_path_binding`.
- Surfaces the workload-manifest opt-in declaration that gates shadow-Git
  checkpoint cadence + worktree-isolation concurrency.

The materialized `PathBinding` is consumed at U-RT-10 (`PathResolver(binding)`)
during bootstrap stage 1 IS. The `WorkloadManifestOptIns` is consumed at
U-RT-11 (worktree isolation + shadow-Git supervisor binding).

Failure modes:
- Duplicate `(path_class, workflow_class, deployment_surface)` triple →
  `harness_is.PathBindingDuplicateError` (raised by `load_path_binding`).
- Invalid raw entry shape → `pydantic.ValidationError` (raised by
  `PathBindingEntry.model_validate`).
- `WorkloadManifestOptIns` invariant violation (`shadow_git_enabled` set
  without `shadow_git_cadence`) → `pydantic.ValidationError` at config
  construction time, not at build time.
"""

from __future__ import annotations

from harness_is.path_binding import PathBinding, load_path_binding
from harness_is.workload_manifest_opt_in_schema import WorkloadManifestOptIns

from harness_runtime.types import PathBindingConfig

__all__ = [
    "build_path_binding",
    "resolve_opt_ins",
]


def build_path_binding(config: PathBindingConfig) -> PathBinding:
    """Build a validated `PathBinding` from a `PathBindingConfig`.

    Delegates to `harness_is.load_path_binding`, which validates each raw
    record into a `PathBindingEntry` and rejects duplicate triples.

    Parameters
    ----------
    config :
        The runtime-validated path-binding config.

    Returns
    -------
    PathBinding
        Frozen, deduplicated binding suitable for `PathResolver(binding)`.

    Raises
    ------
    pydantic.ValidationError
        Raw entry shape invalid.
    harness_is.PathBindingDuplicateError
        Two raw entries share a `(path_class, workflow_class, deployment_surface)`
        triple.
    """
    return load_path_binding(config.raw_entries)


def resolve_opt_ins(config: PathBindingConfig) -> WorkloadManifestOptIns:
    """Return the validated opt-in declaration carried by `config`.

    The opt-in invariant (`shadow_git_enabled` requires `shadow_git_cadence`)
    is enforced at `PathBindingConfig.opt_ins` construction time; this
    function is a typed accessor for downstream stage 1 units (U-RT-11) and
    a hook for future opt-in enrichment without touching call sites.
    """
    return config.opt_ins
