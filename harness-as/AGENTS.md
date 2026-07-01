# AGENTS.md — AS Axis Codex Projection

Read this file before AS-axis work. `harness-as/CLAUDE.md` remains the canonical Claude-native lineage; consult it only for exact posture, substitution, and anti-leakage details.

## Scope

AS owns tool contracts, MCP integration surfaces, sandbox tier contracts, secret-fetch abstractions, and skill filesystem contracts.

## Codex Rules

- Ground `C-AS-*`, `U-AS-*`, and AS↔CXA claims with `just overlay-query` or direct source/test reads.
- Do not silently mix MCP protocol fail classes, sandbox process fail classes, and OD telemetry namespaces; preserve layer boundaries.
- Do not run credential or paid-provider paths without explicit operator authorization.
- Run targeted `harness-as` tests first, then runtime integration tests when dispatcher or MCP behavior is touched.
