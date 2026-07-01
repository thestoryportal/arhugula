# R-421 MANAGED_CLOUD Readiness

This directory is the operator-facing setup surface for R-421. It is not a
managed-cloud deployment by itself.

Use `harness.managed-cloud.e2b.example.toml` as the shape of the runtime config
once a managed collector endpoint, GCP Secret Manager project, and hosted
sandbox provider are selected. Static readiness validates the non-mutating
shape only; it does not fetch GCP secrets, create an E2B sandbox, or probe OTLP.
Set `runtime.provider_secrets.gcp_project_id` to the canonical Google Cloud
project ID or numeric project number, not the console display name. For the
operator-provisioned project from this session, that ID is
`project-ba535aa4-f08d-46b2-ba6`; the display name `My First Project` is not a
valid Secret Manager resource identifier.

The current infrastructure selection is recorded in
[`r411-r421-infrastructure-selection.md`](r411-r421-infrastructure-selection.md).
Recommended first R-421 closure path: E2B hosted sandbox, GCP Secret Manager as
the first managed-cloud provider-secret backend, and the Google-built
OpenTelemetry Collector on Cloud Run exporting to Google Cloud Observability.
R-411 remains a separate host/runtime gate; E2B is not counted as a local R-411
runtime under the current roadmap taxonomy.

The Cloud Run collector deployment surface is in
`cloud-run-otel-collector/`. It uses Google's collector image, mounts
`collector.yaml` from Secret Manager, enables HTTP/2 for OTLP gRPC, and keeps
`min-instances=0` for a low-idle-cost proof. The safer default is authenticated
Cloud Run ingress: the live e2e can fetch a short-lived identity token with
`gcloud auth print-identity-token` and attach it to the OTLP gRPC call as call
credentials.

After copying the template and replacing placeholders, run:

```bash
just r421-managed-cloud-readiness harness.managed-cloud.toml --hosted-sandbox-provider e2b
```

Credential placement for live GCP probes:

- Preferred operator ADC path: run `gcloud auth application-default login` and
  set the active project to `project-ba535aa4-f08d-46b2-ba6`. The Google SDK
  discovers `~/.config/gcloud/application_default_credentials.json`
  automatically; do not commit that file.
- Service-account path: place the JSON outside the repository, for example
  `/Users/robertrhu/.config/arhugula/gcp-secret-manager-accessor.json`, then
  expose only `GOOGLE_APPLICATION_CREDENTIALS=/Users/robertrhu/.config/arhugula/gcp-secret-manager-accessor.json`
  in the shell or local ignored env file used for the live probe. The service
  account needs Secret Manager Secret Accessor for `e2b-secret`.

To verify that the configured provider-secret backend can fetch the E2B key
without creating a hosted sandbox, run:

```bash
uv run python tools/r421_e2b_live_probe.py --config harness.managed-cloud.toml --resolve-only
```

Optional hosted-sandbox candidate probe after the resolve-only check:

```bash
uv run --with e2b python tools/r421_e2b_live_probe.py --config harness.managed-cloud.toml
```

The live E2B probe creates a hosted sandbox and is usage-billed by E2B. It
requires explicit operator approval before Codex runs it. The resolve-only
command performs a GCP Secret Manager access but does not create an E2B sandbox.

Full managed-cloud live e2e after the collector URL is in the config:

```bash
just r421-managed-cloud-live-e2e harness.managed-cloud.toml \
  --trace-query-project project-ba535aa4-f08d-46b2-ba6 \
  --cloud-run-auth-audience https://YOUR-COLLECTOR.run.app \
  --cloud-run-auth-impersonate-service-account 543404640214-compute@developer.gserviceaccount.com \
  --gcloud-bin "$HOME/google-cloud-sdk/bin/gcloud"
```

This command creates a hosted E2B sandbox, emits a `sandbox.violation` trace to
the managed OTLP endpoint, and polls Cloud Trace for the emitted trace ID. It
prints only redacted secret status and trace IDs.

Runtime bootstrap reachability is explicit in `[runtime.collector]`. The
default `bootstrap_sandbox_tier` is `tier-1-process`, which keeps local and
self-hosted host-process bootstrap constrained to `IN_PROCESS` or
`SELF_HOSTED_BACKEND_COLLECTOR` placements. The E2B/FULL_VM managed-cloud
template sets:

```toml
[runtime.collector]
placement = "VENDOR_PIPELINE"
bootstrap_sandbox_tier = "tier-4-full-vm"
```

The readiness and live e2e commands check this configured tier before creating
an E2B sandbox, so a stale Tier-1/VENDOR_PIPELINE binding fails without a paid
hosted-sandbox call.

R-421 closed on 2026-06-07 with the operator-provisioned E2B + GCP path. The
live command resolved `e2b-secret` through Secret Manager, created a hosted E2B
sandbox for the deterministic command, exported authenticated OTLP to the Cloud
Run collector, and observed Cloud Trace trace
`d848a4da6622f42407a5e58c507513c5` with spans
`r421.managed_cloud.root` and `sandbox.violation`.
