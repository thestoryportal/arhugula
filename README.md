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

Then edit `.env` and set `ANTHROPIC_API_KEY` for the shipped minimal workflow.

Run the smoke workflow:

```sh
just run examples/minimal.toml
```

The first run writes runtime state to `.harness/state.jsonl` and uses the paths
created by `just init-local`.

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
