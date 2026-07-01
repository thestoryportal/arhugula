# How To Operate The Runtime

Use this page for routine CLI and daemon operation. It avoids live-provider
setup details except where a workflow dispatch actually requires provider
credentials.

## Run One Workflow

Use one-shot mode when you want the CLI process to load config, load one
workflow manifest, dispatch it, print the result, and exit:

```sh
uv run harness run examples/minimal.toml --config harness.toml
```

Useful flags:

| Flag | Purpose |
| --- | --- |
| `--config <path>` | Use a specific TOML runtime config instead of CWD discovery. |
| `--output text|json` | Select text or JSON result output. |
| `--provider <name>` | Override the workflow default provider for this invocation. |
| `--model <name>` | Override the workflow default model for this invocation. |
| `--tenant-id <id>` | Override `RuntimeConfig.tenant_id`. |
| `--daemon` | Dispatch through a running daemon instead of one-shot bootstrap. |
| `--socket-path <path>` | Select the daemon Unix socket for daemon-client mode. |

## Run The Daemon

Start the FastMCP-backed daemon over a Unix socket:

```sh
uv run harness daemon --config harness.toml
```

The `just daemon` recipe runs the same command with `--config harness.toml`.
Daemon mode loads runtime config once, binds the runtime server, and serves
until interrupted or shut down.

Dispatch to a running daemon:

```sh
uv run harness run examples/minimal.toml --daemon
```

The `just run-daemon examples/minimal.toml` recipe uses this client path.

## Inspect And Shut Down

The parent CLI exposes pass-through admin commands:

```sh
uv run harness inspect --help
uv run harness shutdown --help
```

The package also preserves standalone admin scripts:

```sh
uv run harness-inspect --help
uv run harness-shutdown --help
```

Use `inspect` for read-only state and trace summaries. Use `shutdown` to signal
a running harness process to drain and exit through the admin shutdown path.

## Exit Codes

| Code | Meaning |
| ---: | --- |
| `0` | Successful CLI command or completed workflow. |
| `1` | Workflow-level failure or unknown daemon workflow status. |
| `2` | Workflow manifest load or admissibility error. |
| `3` | Runtime config load error. |
| `4` | Bootstrap or daemon connection/startup error. |

## Config Practices

- Keep `harness.toml` local and gitignored.
- Keep secrets out of TOML. Provider secret selectors belong in
  `[runtime.provider_secrets]`; actual secret values come from the configured
  backend.
- Use `harness.toml.example` as the full operator template for required fields,
  path bindings, OTel endpoint, routing manifest, and provider optionality.

## Source Grounding

This page is grounded in `harness-runtime/src/harness_runtime/cli/app.py`,
`harness-runtime/pyproject.toml`, `harness-runtime/src/harness_runtime/admin/inspect.py`,
`harness-runtime/src/harness_runtime/admin/shutdown_cli.py`,
`harness-runtime/src/harness_runtime/config_source.py`, `harness.toml.example`,
and `justfile`.
