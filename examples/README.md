# Examples — running the harness CLI

This directory holds operator-runnable workflow manifests and paired config
snippets. `minimal.toml` is the smallest workflow that reaches a real LLM
dispatch — the MVP smoke test.

## One-time setup

1. **Runtime config.** Copy the template and edit the absolute paths:

   ```sh
   cp harness.toml.example harness.toml
   ```

   At minimum, set `repository_root` and the four `path_bindings.raw_entries`
   paths to real directories on your machine. The template's `workflow_class`
   values are `pipeline-automation` to match `examples/minimal.toml`.

2. **API key.** Copy the secrets template and set your Anthropic key:

   ```sh
   cp .env.example .env
   chmod 600 .env
   # edit .env: ANTHROPIC_API_KEY=sk-ant-...
   ```

   The `justfile` loads `.env` automatically (`set dotenv-load := true`). The
   harness resolves the key via ADR-F5 tier-aware keyring with env-var fallback
   at the `local-development` tier, so the `.env` value is picked up. Secrets
   are never read from `harness.toml`.

## Run the smoke

```sh
just run examples/minimal.toml
```

(`just run` passes `--config harness.toml` for you. To invoke the CLI directly:
`uv run harness run examples/minimal.toml --config harness.toml`.)

### Expected output

A successful run prints (text mode):

```
status:    completed
workflow:  example-minimal
ledger:    <64-hex audit-ledger head hash>
```

and writes the state-ledger entries to the `STATE_LEDGER` path bound in
`harness.toml` (e.g. `<repository_root>/.harness/state.jsonl`). The single
`inference-step` dispatches a one-token reply ("ok") from `claude-haiku-4-5`.

Exit codes: `0` success · `1` workflow failure · `2` manifest error ·
`3` config error · `4` bootstrap error.

## Run the Sonnet routing pair

`minimal-routing-model.toml` pairs with
`minimal-routing-model.runtime-routing.toml.example`. The workflow manifest
records the Sonnet intent; the runtime routing snippet shows the
`[runtime.routing_manifest]` table that must replace the matching table in
local `harness.toml` for the current one-shot path to dispatch Sonnet.

After applying that runtime routing table, run:

```sh
just run examples/minimal-routing-model.toml
```

The expected LLM span is `chat claude-sonnet-4-6`.

## Notes

- **TOML, not YAML.** `tomllib` preserves native scalar types, so
  `max_tokens = 8` reaches the LLM SDK as an int. The YAML loader path is
  gated on a strictyaml scalar-coercion Class 1 fork and is not yet runnable.
- **workflow_class ↔ path-binding match.** Bootstrap stage IS-1 looks up a
  path binding by `(path_class, workflow_class, deployment_surface)`. If you
  write your own manifest with a different `workload_class`, add matching
  `path_bindings.raw_entries` to `harness.toml`.
- **MVP-runnable shape.** Only `(pipeline-automation, single-threaded-linear)`
  with `pure-pattern-no-engine` is materialized end-to-end at the v1 MVP.
- **Model routing authority.** For the current one-shot dispatch path,
  `harness.toml`'s `[runtime.routing_manifest].fallback_chains[0]` is the
  effective model-control surface. A workflow file's `default_model_binding`
  documents the manifest default, but it does not override a runtime fallback
  chain that routes the step elsewhere.
