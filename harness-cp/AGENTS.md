# AGENTS.md — CP Axis Codex Projection

Read this file before CP-axis work. `harness-cp/CLAUDE.md` remains the canonical Claude-native lineage; consult it only for exact posture, substitution, and anti-leakage details.

## Scope

CP owns routing, retry/breaker/idempotency, workflow lifecycle, topology, HITL placement, and CP-side composer contracts.

## Codex Rules

- Ground `C-CP-*`, `U-CP-*`, and CP↔CXA claims with `just overlay-query` or direct source/test reads.
- Treat composer signatures, idempotency-key formulas, and audit/state-ledger emissions as contract surfaces.
- Do not synthesize missing design fields or caller-site derivations; halt and route as back-flow when the substrate is absent.
- Run targeted `harness-cp` tests first, then runtime tests when workflow-driver or bootstrap wiring is involved.
