"""`harness_runtime.config` — bootstrap stage 0 PREAMBLE config materialization.

Per `Spec_Harness_Runtime_v1.md` v1.1 §3 (C-RT-03 `RuntimeConfig`) and Phase 2
Session 3 plan v2.1 §2 L1, this subpackage owns:

- The `RuntimeConfig` precedence resolver (kwargs > env > defaults) at
  `loader.materialize_runtime_config()` — U-RT-04.
- `PathBindingConfig` enrichment + `PathBinding` construction — U-RT-05.
- `ProviderSecretsConfig` enrichment + keyring resolver driver — U-RT-06.
- `OTelConfig` enrichment (endpoint, sampler, resource attrs) — U-RT-07.
- `CollectorConfig` enrichment (ring buffer, sqlite rotation, placement) — U-RT-08.

The L0 sub-config placeholders live at `harness_runtime.types`; L1 units enrich
those placeholders in place (preserving import compatibility) and add the
materialization logic here.
"""

from __future__ import annotations
