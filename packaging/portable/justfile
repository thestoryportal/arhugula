set dotenv-load := true
set dotenv-required := false
set positional-arguments := true
export UV_CACHE_DIR := env_var_or_default("UV_CACHE_DIR", "/tmp/arhugula-uv-cache")

default:
    @just --list

# Install/sync the uv workspace.
sync:
    uv sync --all-packages

# Create local runtime directories plus gitignored harness.toml and .env files.
init-local:
    uv run python tools/portable_init.py

# Provider-free local verification for the portable source package.
test:
    env -u ANTHROPIC_API_KEY -u OPENAI_API_KEY -u E2B_API_KEY -u GOOGLE_APPLICATION_CREDENTIALS -u GOOGLE_CLOUD_PROJECT PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring uv run pytest -m "not e2e"

lint:
    uv run ruff check .

typecheck:
    uv run pyright

# Provider-free memory substrate docs and verification-matrix closeout.
memory-closeout-check:
    uv run python tools/memory_closeout_check.py --check

check-local: sync lint typecheck memory-closeout-check test

# One-shot workflow run. Example: just run examples/minimal.toml
run file:
    uv run harness run {{file}} --config harness.toml

# Materialize a temp harness config by applying an example runtime overlay.
example-config overlay:
    @uv run python tools/apply_example_runtime_overlay.py {{overlay}}

# Materialize a temp harness config for a local external CLI provider.
# Examples:
#   just external-cli-config codex
#   just external-cli-config antigravity
#   just external-cli-config gemini       # legacy/deprecated Gemini CLI
#   just external-cli-config generic-command --provider-name local_llm --command my-llm --model demo --family openai --arg=--model --arg={model}
external-cli-config provider *args:
    @uv run python tools/external_cli_provider_config.py {{provider}} {{args}}

# Start the daemon over its configured Unix socket.
daemon:
    uv run harness daemon --config harness.toml

# Dispatch through a running daemon.
run-daemon file:
    uv run harness run {{file}} --daemon

# Build workspace wheels, export locked third-party requirements, and validate
# portable runtime image/readiness artifacts.
q4-packaging-check:
    uv run python tools/q4_packaging_gate.py --build --check

# Rebuild this same portable source package into another empty directory.
portable-source target="dist/arhugula-harness-portable":
    uv run python tools/portable_source_package.py {{target}} --check

# Start/stop local self-hosted telemetry stack. Requires Docker.
r420-self-hosted-stack-up:
    docker compose -f deploy/self-hosted-local/compose.yaml up -d

r420-self-hosted-stack-down:
    docker compose -f deploy/self-hosted-local/compose.yaml down

r420-self-hosted-stack-status:
    docker compose -f deploy/self-hosted-local/compose.yaml ps

# Static readiness checks. These do not start providers or hosted sandboxes.
r420-self-hosted-readiness config:
    uv run python tools/self_hosted_readiness.py --config {{config}}

r421-managed-cloud-readiness config *args:
    uv run python tools/managed_cloud_readiness.py --config {{config}} {{args}}

sandbox-host-check provider='r411-gvisor':
    uv run python tools/sandbox_host_readiness.py --provider {{provider}}
