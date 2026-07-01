# Examples — running the harness CLI

This directory holds operator-runnable workflow manifests and paired config
snippets. `minimal.toml` is the smallest workflow that reaches a real LLM
dispatch — the MVP smoke test.

## One-time setup

1. **Install and initialize.** From the repository root:

   ```sh
   uv sync --all-packages
   just init-local
   ```

   `just init-local` creates local runtime directories plus gitignored
   `harness.toml` and `.env` files. It preserves existing local files unless
   explicitly forced.

2. **Provider auth.** Authenticate at least one local provider CLI as the same
   OS user that runs Arhugula:

   ```sh
   codex login status
   agy models
   claude auth status --json
   ```

   Use the CLI's own login/onboarding flow if the status command reports that it
   is not authenticated. The `justfile` still loads `.env` automatically
   (`set dotenv-load := true`) so hosted SDK/API keys can be used as secondary
   fallbacks. Secrets are never read from `harness.toml`.

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
`inference-step` dispatches a one-token reply ("ok") from the first available
provider in the default chain: `claude_code`, `codex`, `antigravity`, then SDK
fallbacks.

Exit codes: `0` success · `1` workflow failure · `2` manifest error ·
`3` config error · `4` bootstrap error.

## Run the Sonnet routing pair

`minimal-routing-model.toml` pairs with
`minimal-routing-model.runtime-routing.toml.example`. The workflow manifest
records the Sonnet intent; the runtime routing snippet shows the
`[runtime.routing_manifest]` table that must replace the matching table in
local `harness.toml` for the current one-shot path to dispatch Sonnet.
The `example-config` helper applies that runtime routing table to a temp copy
of `harness.toml`, prints the temp path, and leaves `harness.toml` unchanged.

Run:

```sh
SONNET_CONFIG="$(just example-config examples/minimal-routing-model.runtime-routing.toml.example)"
uv run harness run examples/minimal-routing-model.toml --config "$SONNET_CONFIG"
```

The expected LLM span is `chat claude-sonnet-4-6`.

## Route through local Claude Code

`claude-code.runtime-overlay.toml.example` routes `examples/minimal.toml`
through an already-authenticated local Claude Code CLI. Arhugula does not read
or store Claude OAuth/session tokens; it only runs the official `claude` CLI
with stdin/stdout. The easiest path is the temp-config helper:

First verify local CLI auth:

```sh
claude auth status --json
```

Only run the live example if that reports `"loggedIn": true`.

```sh
CLAUDE_CODE_CONFIG="$(just external-cli-config claude_code)"
uv run harness run examples/minimal.toml --config "$CLAUDE_CODE_CONFIG"
```

The expected LLM span is `chat sonnet` with
`gen_ai.provider.name = "claude_code"` and
`external_cli.provider.kind = "claude-code"`.

## Route through local Codex

Codex routing uses the already-authenticated local `codex` CLI. Arhugula runs
`codex login status` at construction and dispatches through
`codex exec --json --ephemeral --sandbox read-only` with the prompt on stdin.

```sh
codex login status
CODEX_CONFIG="$(just external-cli-config codex)"
uv run harness run examples/minimal.toml --config "$CODEX_CONFIG"
```

The expected LLM span is `chat gpt-5` with
`gen_ai.provider.name = "codex"` and `external_cli.provider.kind = "codex"`.

## Route through local Antigravity

Google's replacement for Gemini CLI installs the `agy` binary. Arhugula runs
`agy models` at construction as a token-free auth check and dispatches through
`agy --print` with sandboxing enabled:

```sh
curl -fsSL https://antigravity.google/cli/install.sh | bash
agy
```

Complete browser sign-in and first-launch setup in the TUI, then exit and
verify the authenticated model list:

```sh
agy models
ANTIGRAVITY_CONFIG="$(just external-cli-config antigravity)"
uv run harness run examples/minimal.toml --config "$ANTIGRAVITY_CONFIG"
```

The expected LLM span is `chat Gemini 3.5 Flash (Low)` with
`gen_ai.provider.name = "antigravity"` and
`external_cli.provider.kind = "antigravity"`.

## Route through legacy Gemini

Gemini CLI is deprecated, but Arhugula keeps the adapter for existing legacy
installs that still have the local `gemini` binary and its headless text prompt
mode. The adapter passes `--skip-trust` for the current session so a fresh
checkout does not block on Gemini's folder-trust prompt. It does not read
token/session files. The default helper config does not run a construction-time
auth probe because Gemini's non-live auth status surface is not stable across
installs; the official CLI surfaces auth errors during inference.

```sh
GEMINI_CONFIG="$(just external-cli-config gemini)"
uv run harness run examples/minimal.toml --config "$GEMINI_CONFIG"
```

The expected LLM span is `chat gemini-2.5-flash` with
`gen_ai.provider.name = "gemini"` and `external_cli.provider.kind = "gemini"`.

## Route through another local CLI

For CLIs without a built-in adapter, generate a temp config with
`generic-command`. The command is argv-only, no shell is used, and prompts go to
stdin by default:

```sh
CUSTOM_CONFIG="$(just external-cli-config generic-command \
  --provider-name local_llm \
  --command my-llm \
  --model demo-model \
  --family openai \
  --arg=--model \
  '--arg={model}' \
  --arg=--json \
  --auth-arg=auth \
  --auth-arg=status \
  --response-format json)"
uv run harness run examples/minimal.toml --config "$CUSTOM_CONFIG"
```

Use `--prompt-transport arg` plus an `--arg='{prompt}'` template only for CLIs
that cannot read stdin; that can expose prompt text through process argv.

## Set up local Ollama

The Ollama examples require Ollama listening on `127.0.0.1:11434` with
`llama3.2:3b` pulled locally. If the readiness check fails, start the Ollama app
or run `ollama serve` in another terminal.

```sh
ollama pull llama3.2:3b
curl -sf http://127.0.0.1:11434/api/tags
```

## Run the Ollama recovery pair

`recovery-ollama-fallback.toml` pairs with
`recovery-ollama-fallback.runtime-overlay.toml.example`. This is a recovery
example, not a beginner happy-path smoke: the primary model is intentionally
unavailable, the reserved `llm_dispatch` retry policy exhausts it, and the
runtime fallback chain recovers to local `llama3.2:3b`.

Use it after `llama3.2:3b` is pulled locally and Ollama is listening on
`127.0.0.1:11434`. Materialize a temp config from the runtime overlay, then run
with hosted credentials disabled:

```sh
RECOVERY_CONFIG="$(just example-config examples/recovery-ollama-fallback.runtime-overlay.toml.example)"
env -u ANTHROPIC_API_KEY -u OPENAI_API_KEY \
  PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring \
  uv run harness run examples/recovery-ollama-fallback.toml --config "$RECOVERY_CONFIG"
```

With the self-hosted observability stack running, the expected trace contains
failed `chat llama-nonexistent-model-r300-fallback-probe` spans followed by a
successful `chat llama3.2:3b` span, all with `gen_ai.provider.name = "ollama"`.

## Start and stop local observability

The local self-hosted observability stack exposes OTLP gRPC on
`127.0.0.1:4317`, OTLP HTTP on `127.0.0.1:4318`, Tempo on
`127.0.0.1:3200`, and Grafana on `127.0.0.1:3000`.

```sh
just r420-self-hosted-stack-up
just r420-self-hosted-stack-status
curl -sf http://127.0.0.1:3200/ready
```

Stop it with:

```sh
just r420-self-hosted-stack-down
```

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

Materialize a temp config from the runtime overlay, then run with hosted
credentials disabled:

```sh
TOPOLOGY_CONFIG="$(just example-config examples/topology-parallelization-ollama.runtime-overlay.toml.example)"
env -u ANTHROPIC_API_KEY -u OPENAI_API_KEY \
  PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring \
  uv run harness run examples/topology-parallelization-ollama.toml --config "$TOPOLOGY_CONFIG"
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
