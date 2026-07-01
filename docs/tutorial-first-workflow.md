# Tutorial: First Workflow

This tutorial gets a local operator from a clean checkout to the shipped
minimal workflow. It uses the one-shot CLI path and the example manifest already
in the repository.

## Prerequisites

- Python dependencies are synchronized with `uv`.
- You have a provider credential for the model selected by
  `examples/minimal.toml`. The shipped example and `harness.toml.example`
  route to Anthropic `claude-haiku-4-5`.
- You can edit a local, gitignored `harness.toml`.

## 1. Create Local Config

Copy the template:

```sh
cp harness.toml.example harness.toml
```

Edit `harness.toml`:

- Set `runtime.repository_root` to the absolute path of this checkout.
- Replace the four `runtime.path_bindings.raw_entries` paths with absolute
  paths on your machine.
- Keep the template `workflow_class = "pipeline-automation"` entries when
  running `examples/minimal.toml`.

The runtime config loader composes environment values, the config file, and CLI
overrides in deterministic order. `harness.toml` is for selectors and paths, not
secret values.

## 2. Provide The Provider Credential

For local development, the example guide uses a repo-local `.env` loaded by the
`justfile`:

```sh
cp .env.example .env
chmod 600 .env
```

Set `ANTHROPIC_API_KEY` in `.env`. The runtime resolves provider secrets through
the configured tier-aware provider-secret backend; `harness.toml.example` keeps
secret values out of the TOML file.

## 3. Run The Minimal Workflow

Run:

```sh
just run examples/minimal.toml
```

The `just run` recipe expands to:

```sh
uv run harness run examples/minimal.toml --config harness.toml
```

On success, text output includes a completed status, the workflow id, and the
state ledger head hash. Manifest errors exit with code `2`, config errors with
code `3`, and bootstrap errors with code `4`.

## 4. Confirm The State Ledger

The runtime writes state-ledger entries to the path bound as `STATE_LEDGER` in
`harness.toml`. For the template, that is intended to live under
`<repository_root>/.harness/`.

## Source Grounding

This tutorial is grounded in `examples/README.md`, `harness.toml.example`,
`justfile`, `harness-runtime/src/harness_runtime/cli/app.py`,
`harness-runtime/src/harness_runtime/config_source.py`, and
`harness-runtime/src/harness_runtime/types.py`.
