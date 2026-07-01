# Architecture

The harness runtime is the executable layer over the design axes. It keeps the
axis packages separate while the runtime composes config, bootstrap, dispatch,
observability, and shutdown into an operator-facing system.

## Axis Packages

The workspace is organized around four axis packages and a cross-axis package:

| Axis | Package | Runtime role |
| --- | --- | --- |
| IS | `harness-is` | Path binding and information substrate contracts. |
| AS | `harness-as` | Agent substrate carriers and posture concepts. |
| CP | `harness-cp` | Control-plane routing, model binding, retry, and workflow concepts. |
| OD | `harness-od` | Observability, durability, tracing, sampling, and audit concepts. |
| CXA | `harness-cxa` | Cross-axis composition contracts and seams. |

`harness-runtime` imports those packages and owns the executable runtime surface.

## Runtime Flow

One-shot `harness run` follows this shape:

1. Load `RuntimeConfig` from environment, optional TOML, and CLI overrides.
2. Load the workflow manifest into a closed-schema carrier.
3. Apply workflow-level provider/model overrides.
4. Check the workflow engine class against the active deployment surface.
5. Bootstrap runtime context and dispatch through the API run path.
6. Emit text or JSON output and map terminal status to the CLI exit code.

Daemon mode loads config, bootstraps runtime context, binds the FastMCP server
to a Unix socket, and serves daemon-client workflow dispatches until shutdown.

## Config And Boundaries

`RuntimeConfig` is frozen and forbids unknown fields. Operator-supplied TOML
contains paths, selectors, routing manifests, and non-secret provider backend
metadata. Provider secret values are resolved through configured secret
backends, not read from TOML.

Deployment surface is a primary discriminator. It drives behavior such as
provider-secret backend choice, collector placement, sampler defaults, sandbox
tier expectations, and readiness/live-run gates.

## Observability And State

Runtime config requires an OTLP endpoint. The OD wiring builds tracing,
collector, sampling, and state-ledger behavior from runtime config and package
contracts. State ledger paths are resolved through IS path bindings, and the
CLI/admin surfaces expose read-only inspection and graceful shutdown paths.

## Deployment Surfaces

| Surface | Current proof path |
| --- | --- |
| Local development | Example workflow, one-shot CLI, daemon/client tests, provider-free test lane. |
| Self-hosted server | Static readiness plus local Docker Compose OTel/Tempo/Grafana runbook. |
| Managed cloud | Static readiness plus explicit GCP Secret Manager, E2B, Cloud Run collector, and Cloud Trace live e2e path. |
| Portable images | Q4 package/image gate and Docker targets for self-hosted daemon, managed-cloud daemon, and sandbox runner. |

## Closure Track

The roadmap close track has completed Q1 through Q4. D1 adds this documentation
suite and a provider-free completeness gate. C1 remains the downstream
full-spec closure certification and ship arc.

## Source Grounding

This architecture page is grounded in `pyproject.toml`,
`harness-runtime/src/harness_runtime/cli/app.py`,
`harness-runtime/src/harness_runtime/api.py`,
`harness-runtime/src/harness_runtime/bootstrap/__init__.py`,
`harness-runtime/src/harness_runtime/config_source.py`,
`harness-runtime/src/harness_runtime/types.py`,
`harness-runtime/src/harness_runtime/lifecycle/workflow_manifest_loader.py`,
`deploy/self-hosted-local/README.md`, `deploy/managed-cloud/README.md`,
`deploy/images/README.md`, `.harness/roadmap_status.md`, and
`.harness/arc-ledger.yaml`.
