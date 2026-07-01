# AGENTS.md — OD Axis Codex Projection

Read this file before OD-axis work. `harness-od/CLAUDE.md` remains the canonical Claude-native lineage; consult it only for exact posture, substitution, and anti-leakage details.

## Scope

OD owns HITL audit primitives, audit-ledger schema, cost attribution, observability schemas, sampling, redaction, and operational gates.

## Codex Rules

- Ground `C-OD-*`, `U-OD-*`, and OD↔CXA claims with `just overlay-query` or direct source/test reads.
- Keep OTel namespace ownership, attribute tiers, and runtime emission sites distinct.
- Do not run live provider or credential-dependent observability paths without explicit operator authorization.
- Run targeted `harness-od` tests first, then runtime lifecycle tests when materializers or emitters are touched.
