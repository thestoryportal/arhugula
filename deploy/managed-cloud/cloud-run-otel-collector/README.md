# R-421 Cloud Run OTel Collector

This directory contains the minimal managed-collector deployment surface for
the R-421 live gate. It uses Google's OpenTelemetry Collector image on Cloud
Run, receives OTLP gRPC on the Cloud Run service port, and exports traces to
Google Cloud Observability.

The collector config is committed as non-secret YAML. The recommended deploy
path stores that YAML in Secret Manager and mounts it into the collector
container, matching the Google Cloud collector guidance without adding a custom
image build step.

Default probe values:

- Project: `project-ba535aa4-f08d-46b2-ba6`
- Region: `us-central1`
- Cloud Run service: `arhugula-r421-otel-collector`
- Collector config secret: `r421-otel-collector-config`
- Runtime OTLP endpoint: the Cloud Run service URL returned by `gcloud run
  services describe`, without a `/v1/traces` suffix.

Prepare the project and runtime service account:

```bash
PROJECT_ID=project-ba535aa4-f08d-46b2-ba6
REGION=us-central1
SERVICE=arhugula-r421-otel-collector
CONFIG_SECRET=r421-otel-collector-config
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
RUN_SA="$PROJECT_NUMBER-compute@developer.gserviceaccount.com"

gcloud services enable run.googleapis.com secretmanager.googleapis.com cloudtrace.googleapis.com iamcredentials.googleapis.com \
  --project="$PROJECT_ID"
```

Provision or refresh the config secret without creating duplicate versions on a
first deploy:

```bash
if gcloud secrets describe "$CONFIG_SECRET" --project="$PROJECT_ID" >/dev/null 2>&1; then
  gcloud secrets versions add "$CONFIG_SECRET" \
    --data-file=deploy/managed-cloud/cloud-run-otel-collector/collector.yaml \
    --project="$PROJECT_ID"
else
  gcloud secrets create "$CONFIG_SECRET" \
    --data-file=deploy/managed-cloud/cloud-run-otel-collector/collector.yaml \
    --project="$PROJECT_ID"
fi

gcloud secrets add-iam-policy-binding "$CONFIG_SECRET" \
  --member="serviceAccount:$RUN_SA" \
  --role=roles/secretmanager.secretAccessor \
  --project="$PROJECT_ID"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$RUN_SA" \
  --role=roles/cloudtrace.agent

gcloud iam service-accounts add-iam-policy-binding "$RUN_SA" \
  --member=user:storyportalrobert@gmail.com \
  --role=roles/iam.serviceAccountTokenCreator \
  --project="$PROJECT_ID"
```

For local proof runs, treat the Service Account Token Creator binding as
temporary and remove it after the run if ongoing local impersonation is not
needed.

Deploy the authenticated collector:

```bash
gcloud run deploy "$SERVICE" \
  --image=us-docker.pkg.dev/cloud-ops-agents-artifacts/google-cloud-opentelemetry-collector/otelcol-google:0.151.0 \
  --args=--config=/etc/otelcol-google/config.yaml \
  --set-secrets="/etc/otelcol-google/config.yaml=$CONFIG_SECRET:latest" \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=$PROJECT_ID" \
  --service-account="$RUN_SA" \
  --port=4317 \
  --use-http2 \
  --no-allow-unauthenticated \
  --min-instances=0 \
  --max-instances=1 \
  --cpu=1 \
  --memory=512Mi \
  --region="$REGION" \
  --project="$PROJECT_ID"
```

Grant the active operator account and the impersonated runtime service account
permission to invoke the authenticated collector:

```bash
gcloud run services add-iam-policy-binding "$SERVICE" \
  --member=user:storyportalrobert@gmail.com \
  --role=roles/run.invoker \
  --region="$REGION" \
  --project="$PROJECT_ID"

gcloud run services add-iam-policy-binding "$SERVICE" \
  --member="serviceAccount:$RUN_SA" \
  --role=roles/run.invoker \
  --region="$REGION" \
  --project="$PROJECT_ID"
```

Fetch the service URL for the R-421 runtime config:

```bash
gcloud run services describe "$SERVICE" \
  --region="$REGION" \
  --project="$PROJECT_ID" \
  --format='value(status.url)'
```

This deployment is intentionally small and usage-billed. `min-instances=0`
keeps idle compute cost at zero. The authenticated Cloud Run service URL is
still internet-routable, but it requires a valid Cloud Run invoker identity
token for OTLP export.
