# Arhugula Harness

This is the portable runtime source package for Arhugula. It contains the
runtime packages, examples, deployment templates, local initialization tool, and
operator documentation needed to install and run the harness from a clean git
clone.

## Quick Start

```sh
uv sync --all-packages
just init-local
```

Then edit `.env` and set `ANTHROPIC_API_KEY` for the shipped minimal workflow:

```sh
${EDITOR:-vi} .env
```

Run the smoke workflow:

```sh
just run examples/minimal.toml
```

The first run writes runtime state to `.harness/state.jsonl` and uses the paths
created by `just init-local`.

## Common Example Commands

Create a temp config from `harness.toml` plus an example runtime overlay. This
prints the temp config path and does not modify `harness.toml`:

```sh
SONNET_CONFIG="$(just example-config examples/minimal-routing-model.runtime-routing.toml.example)"
uv run harness run examples/minimal-routing-model.toml --config "$SONNET_CONFIG"
```

Set up local Ollama for the Ollama examples:

```sh
ollama pull llama3.2:3b
curl -sf http://127.0.0.1:11434/api/tags
```

Run the local Ollama recovery example:

```sh
RECOVERY_CONFIG="$(just example-config examples/recovery-ollama-fallback.runtime-overlay.toml.example)"
env -u ANTHROPIC_API_KEY -u OPENAI_API_KEY \
  PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring \
  uv run harness run examples/recovery-ollama-fallback.toml --config "$RECOVERY_CONFIG"
```

Start the local observability stack, then run the topology fan-out example:

```sh
just r420-self-hosted-stack-up
TOPOLOGY_CONFIG="$(just example-config examples/topology-parallelization-ollama.runtime-overlay.toml.example)"
env -u ANTHROPIC_API_KEY -u OPENAI_API_KEY \
  PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring \
  uv run harness run examples/topology-parallelization-ollama.toml --config "$TOPOLOGY_CONFIG"
```

Stop the observability stack:

```sh
just r420-self-hosted-stack-down
```

## What Is Included

- `harness-core/`, `harness-is/`, `harness-as/`, `harness-cp/`, `harness-od/`,
  `harness-cxa/`, and `harness-runtime/`
- `pyproject.toml` and `uv.lock`
- `harness.toml.example` and `.env.example`
- `examples/`, `docs/`, and `deploy/`
- readiness and packaging tools under `tools/`
- local runtime scaffold directories: `.harness/`, `skills/`, `prompts`, and
  `routing_manifest/`

## What Is Not Included

This portable package intentionally excludes the original build-operator
substrate: design documents, roadmap ledgers, closure artifacts, historical
fork files, local worktrees, Claude/Codex build hooks, nested `CLAUDE.md`
lineage notes, and generated dashboard scratch. Those are useful for developing
the harness, but they are not required to pull, install, configure, and run the
runtime package.

## Documentation

Start at [docs/README.md](docs/README.md). For the external clone workflow, use
[docs/portable-install.md](docs/portable-install.md).
