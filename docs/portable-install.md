# How To Create And Install A Portable Harness Repo

Use this guide when you want to test Arhugula outside the original build repo.
It creates a runtime-first source repo that a new user can clone, initialize,
configure, and run without inheriting the build-operator history.

## What You Will Produce

```text
arhugula-harness-portable/
  README.md
  AGENTS.md
  justfile
  pyproject.toml
  uv.lock
  harness.toml.example
  .env.example
  docs/
  examples/
  deploy/
  harness-core/
  harness-is/
  harness-as/
  harness-cp/
  harness-od/
  harness-cxa/
  harness-runtime/
  tools/
  .harness/
  skills/
  prompts/
  routing_manifest/
```

## Build The Portable Source Repo

From the original build repo, run:

```sh
just portable-source dist/arhugula-harness-portable
```

The command reads `packaging/portable-source.toml`, copies only the allowlisted
runtime source files, applies the portable `README.md`, `AGENTS.md`, `justfile`,
and `.gitignore` overlays, then creates empty local runtime directories.

If the target directory already contains files, the command fails instead of
overwriting anything.

## Publish It As A New Git Repo

Create the first commit in the generated package:

```sh
cd dist/arhugula-harness-portable
git init
git add .
git commit -m "chore: import Arhugula harness portable runtime"
git branch -M main
git remote add origin <portable-repo-url>
git push -u origin main
```

The generated repo is now the clean pull target for external testing.

## New User Pull And Install

On another machine or in another directory:

```sh
git clone <portable-repo-url> arhugula-harness
cd arhugula-harness
uv sync --all-packages
just init-local
```

`just init-local` creates:

- `.harness/`
- `skills/`
- `prompts/`
- `routing_manifest/`
- `harness.toml`
- `.env`

It also rewrites the placeholder workspace path in `harness.toml` to the actual
clone path. It does not overwrite an existing `.env`.

## Configure The First Run

Authenticate at least one local provider CLI:

```sh
codex login status
agy models
claude auth status --json
```

Use the CLI's own login/onboarding flow if the status command reports that it is
not authenticated. Hosted SDK/API keys in `.env` remain available as secondary
fallbacks. For provider-free install verification, run only `just check-local`.

Run the first workflow:

```sh
just run examples/minimal.toml
```

Expected successful text output:

```text
status:    completed
workflow:  example-minimal
ledger:    <64-hex audit-ledger head hash>
```

## Included

| Path | Why it ships |
| --- | --- |
| `harness-*` packages | Runtime source, tests, and package metadata. |
| `pyproject.toml`, `uv.lock` | Reproducible local install with `uv`. |
| `harness.toml.example`, `.env.example` | Safe local config and secret templates. |
| `docs/`, `examples/`, `deploy/` | User setup, runtime operation, workflow examples, and deployment templates. |
| `tools/portable_init.py` | One-command local config initialization. |
| readiness and package tools under `tools/` | Static deploy/package checks used by the portable `justfile`. |
| `.harness/`, `skills/`, `prompts/`, `routing_manifest/` | Empty local runtime residence paths expected by the default config. |

## Excluded

| Path | Why it is excluded |
| --- | --- |
| `.git/`, local worktrees, `.venv/`, `dist/`, `build/` | Machine-local state or generated artifacts. |
| `harness.toml`, `.env` | User-local config and secrets. |
| build-governance ledgers and roadmap history | They manage the original harness build, not external runtime use. |
| Claude/Codex build hooks and local skills | They depend on build-repo governance state and are not required to run the runtime. |
| design documents, research corpora, scaffolding, dashboard scratch | Useful historical context, not required for install, config, or runtime execution. |

## Verification

Build-package verification:

```sh
just portable-source /tmp/arhugula-harness-portable-check
```

Generated-repo verification:

```sh
cd /tmp/arhugula-harness-portable-check
uv sync --all-packages
just init-local
just check-local
```

`just check-local` is provider-free. It syncs the workspace, runs lint,
typecheck, and non-e2e tests. Live provider workflows still require explicit
credentials in `.env`.

## Source Grounding

This guide is grounded in `packaging/portable-source.toml`,
`tools/portable_source_package.py`, `tools/portable_init.py`,
`packaging/portable/README.md`, `packaging/portable/justfile`,
`harness.toml.example`, `examples/README.md`, and `docs/tutorial-first-workflow.md`.
