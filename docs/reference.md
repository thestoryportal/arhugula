# Reference

This reference lists the public runtime surfaces that operators and maintainers
use directly.

## Workspace Packages

| Package | Role |
| --- | --- |
| `harness-core` | Shared core types. |
| `harness-is` | Information substrate axis package. |
| `harness-as` | Agent substrate axis package. |
| `harness-cp` | Control plane axis package. |
| `harness-od` | Observability/durability axis package. |
| `harness-cxa` | Cross-axis composition package. |
| `harness-runtime` | Runtime config, bootstrap, CLI, daemon, workflow loading, and lifecycle wiring. |

## Console Scripts

| Script | Target |
| --- | --- |
| `harness` | `harness_runtime.cli:main` |
| `harness-inspect` | `harness_runtime.admin.inspect:main` |
| `harness-shutdown` | `harness_runtime.admin.shutdown_cli:main` |

## Parent CLI Commands

| Command | Summary |
| --- | --- |
| `harness run <workflow>` | One-shot workflow dispatch, or daemon-client dispatch with `--daemon`. |
| `harness daemon` | Start the runtime daemon over a Unix socket. |
| `harness inspect ...` | Pass through to the read-only admin inspector. |
| `harness shutdown ...` | Pass through to the admin shutdown CLI. |

## RuntimeConfig Required Fields

`RuntimeConfig` is a frozen Pydantic model with `extra="forbid"`. The operator
must provide:

| Field | Meaning |
| --- | --- |
| `deployment_surface` | Local, self-hosted, or managed-cloud surface selector. |
| `repository_root` | Absolute repository root path. |
| `otel.otlp_endpoint` | OTLP endpoint URL with a scheme. |
| `default_topology` | Fallback topology when the workflow does not specify one. |

The template also declares path bindings, provider-secret selector metadata,
provider optionality flags, and a routing manifest fallback chain. Optional
runtime features are exposed as explicit `RuntimeConfig` fields and default off
unless their type declares a default sub-config.

## Config Load Order

The runtime config source loads environment values, then TOML file values, then
CLI overrides. If `--config` is omitted, the loader looks for `harness.toml` in
the process current working directory; if it is absent, the file layer
contributes nothing.

## Workflow Manifest Shape

Workflow manifests are parsed from TOML/YAML into a frozen loader carrier with:

| Field | Meaning |
| --- | --- |
| `version` | Manifest schema version. |
| `workflow` | Workflow metadata, class, topology, and engine class. |
| `default_model_binding` | Provider/model default for inference dispatch. |
| `steps` | Ordered workflow step entries. |

The loader validates version, schema shape, and step id uniqueness. Deployment
surface admissibility is checked at runtime dispatch.

## Provider-Free Gates

| Recipe | Purpose |
| --- | --- |
| `just codex-preflight` | Write/check deterministic local context before work. |
| `just overlay-check` | Re-derive semantic overlay and fail on hard CXA/stale-artifact drift. |
| `just q4-packaging-check` | Build package artifacts and validate deploy image/readiness surface. |
| `just docs-completeness-check` | Validate the D1 documentation suite. |
| `just check` | Workspace sync, lint, typecheck, and provider-free tests. |
| `just codex-closeout` | Write a pre-closeout checkpoint and enforce closeout obligations. |

## Source Grounding

This reference is grounded in `pyproject.toml`, `harness-runtime/pyproject.toml`,
`harness-runtime/src/harness_runtime/cli/app.py`,
`harness-runtime/src/harness_runtime/config_source.py`,
`harness-runtime/src/harness_runtime/types.py`,
`harness-runtime/src/harness_runtime/lifecycle/workflow_manifest_loader.py`,
`tools/semantic_overlay/overlay.py`, `tools/q4_packaging_gate.py`, and `justfile`.
