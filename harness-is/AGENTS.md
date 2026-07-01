# AGENTS.md — IS Axis Codex Projection

Read this file before IS-axis work. `harness-is/CLAUDE.md` remains the canonical Claude-native lineage; consult it only for exact posture, substitution, and anti-leakage details.

## Scope

IS owns state ledger entries, content-addressed indexing, semantic cache, and filesystem-path classification.

## Codex Rules

- Ground `C-IS-*`, `U-IS-*`, and IS↔CXA claims with `just overlay-query` or direct source/test reads.
- Do not widen ledger schemas or hash material silently; route schema changes through the design/back-flow discipline.
- Keep implementation edits in `harness-is/**` separate from design-substrate edits unless explicitly authorized.
- Run targeted `harness-is` tests first, then broader gates when behavior or shared contracts change.
