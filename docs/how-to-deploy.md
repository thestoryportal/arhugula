# How To Deploy

The repository currently exposes provider-free readiness checks and explicit
live/provisioned runbooks. Use the static gates first; run live commands only
after the required local services, cloud resources, and operator approvals are
in place.

## Build And Validate Runtime Packages

Run the Q4 packaging gate:

```sh
just q4-packaging-check
```

The gate builds all workspace wheels, exports a hashed third-party
`requirements.lock.txt` from `uv.lock`, verifies runtime CLI entry points, checks
the runtime Dockerfile targets, and confirms one-command readiness recipes are
present.

## Build Runtime Images

After producing the dist directory described by the Q4 gate, build image
targets from `deploy/images/harness-runtime.Dockerfile`:

```sh
docker build -f deploy/images/harness-runtime.Dockerfile --target self-hosted-daemon -t arhugula/harness:self-hosted dist
docker build -f deploy/images/harness-runtime.Dockerfile --target managed-cloud-daemon -t arhugula/harness:managed-cloud dist
docker build -f deploy/images/harness-runtime.Dockerfile --target sandbox-runner -t arhugula/harness:sandbox-runner dist
```

The image recipe installs third-party packages from the hashed requirements
export and then installs workspace wheels from the wheelhouse without resolving
new dependencies.

## Self-Hosted Local Readiness

The local self-hosted stack in `deploy/self-hosted-local/` provides OTel
Collector, Tempo, and Grafana while the harness daemon runs as a host process.

Static readiness:

```sh
just r420-self-hosted-readiness harness.selfhosted.local.toml
```

Local stack lifecycle:

```sh
just r420-self-hosted-stack-up
just r420-self-hosted-stack-down
```

Live self-hosted e2e commands in that runbook require Docker and local service
setup. They do not make hosted-provider inference calls when using the documented
local Ollama/sentinel path, but they do start local services and dispatch through
the daemon.

## Managed-Cloud Readiness

The managed-cloud runbook in `deploy/managed-cloud/` validates the non-mutating
shape before any hosted sandbox or cloud trace proof:

```sh
just r421-managed-cloud-readiness harness.managed-cloud.toml --hosted-sandbox-provider e2b
```

Resolve-only E2B secret validation fetches from GCP Secret Manager but does not
create a hosted sandbox:

```sh
uv run python tools/r421_e2b_live_probe.py --config harness.managed-cloud.toml --resolve-only
```

The full managed-cloud live e2e creates a hosted E2B sandbox, emits OTLP to the
managed collector, and polls Cloud Trace. Treat that command as usage-billed and
operator-approved only.

## Deployment Boundary

Static readiness checks are safe to run as provider-free CI/local gates. Live
deployment proofs can start daemons, contact collectors, fetch secrets, create
hosted sandboxes, or call providers; keep those on explicit operator approval.

## Source Grounding

This page is grounded in `deploy/images/README.md`,
`deploy/images/harness-runtime.Dockerfile`, `tools/q4_packaging_gate.py`,
`deploy/self-hosted-local/README.md`, `tools/self_hosted_readiness.py`,
`deploy/managed-cloud/README.md`, `tools/managed_cloud_readiness.py`,
`tools/r421_e2b_live_probe.py`, and `justfile`.
