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

## Run the Ollama recovery pair

`recovery-ollama-fallback.toml` pairs with
`recovery-ollama-fallback.runtime-overlay.toml.example`. This is a recovery
example, not a beginner happy-path smoke: the primary model is intentionally
unavailable, the reserved `llm_dispatch` retry policy exhausts it, and the
runtime fallback chain recovers to local `llama3.2:3b`.

Use it after `llama3.2:3b` is pulled locally and Ollama is listening on
`127.0.0.1:11434`. Apply the runtime overlay to a temp copy of `harness.toml`,
then run with hosted credentials disabled:

```sh
env -u ANTHROPIC_API_KEY -u OPENAI_API_KEY \
  PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring \
  uv run harness run examples/recovery-ollama-fallback.toml --config <temp-config>
```

With the self-hosted observability stack running, the expected trace contains
failed `chat llama-nonexistent-model-r300-fallback-probe` spans followed by a
successful `chat llama3.2:3b` span, all with `gen_ai.provider.name = "ollama"`.

## Run the Ollama parallelization pair

`topology-parallelization-ollama.toml` pairs with
`topology-parallelization-ollama.runtime-overlay.toml.example`. This example
proves topology fan-out: two `inference-step` branches run under
`topology_pattern = "parallelization"` and then drain through the state ledger
in branch order.

Use it after `llama3.2:3b` is pulled locally and Ollama is listening on
`127.0.0.1:11434`. If you want Tempo verification, start the local
self-hosted observability stack first:

```sh
just r420-self-hosted-stack-up
```

Create a temp config from `harness.toml`, apply the runtime overlay, replace
`/absolute/path/to/arhugula` with this checkout's absolute path, then run with
hosted credentials disabled:

```sh
env -u ANTHROPIC_API_KEY -u OPENAI_API_KEY \
  PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring \
  uv run harness run examples/topology-parallelization-ollama.toml --config <temp-config>
```

The expected state-ledger workflow rows are branch rows, not linear
`step:0`/`step:1` rows:

```text
workflow:example-topology-parallelization-ollama:fanout:branch:0:step:0
workflow:example-topology-parallelization-ollama:fanout:branch:0:terminal
workflow:example-topology-parallelization-ollama:fanout:branch:1:step:0
workflow:example-topology-parallelization-ollama:fanout:branch:1:terminal
```

With the self-hosted observability stack running, the expected Tempo proof is a
`workflow.envelope` trace with `workflow.id =
"example-topology-parallelization-ollama"` and at least two successful
`chat llama3.2:3b` spans, all with `gen_ai.provider.name = "ollama"` and no
Anthropic/OpenAI GenAI spans.

## Notes

- **TOML, not YAML.** `tomllib` preserves native scalar types, so
  `max_tokens = 8` reaches the LLM SDK as an int. The YAML loader path is
  gated on a strictyaml scalar-coercion Class 1 fork and is not yet runnable.
- **workflow_class ↔ path-binding match.** Bootstrap stage IS-1 looks up a
  path binding by `(path_class, workflow_class, deployment_surface)`. If you
  write your own manifest with a different `workload_class`, add matching
  `path_bindings.raw_entries` to `harness.toml`.
- **Beginner smoke shape.** The first-run smoke path is still
  `(pipeline-automation, single-threaded-linear)` with
  `pure-pattern-no-engine`. Other workload/topology pairs need paired runtime
  config, as shown by the routing, recovery, and topology examples above.
- **Model routing authority.** For the current one-shot dispatch path,
  `harness.toml`'s `[runtime.routing_manifest].fallback_chains[0]` is the
  effective model-control surface. A workflow file's `default_model_binding`
  documents the manifest default, but it does not override a runtime fallback
  chain that routes the step elsewhere.
