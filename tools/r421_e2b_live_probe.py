#!/usr/bin/env python3
"""Live E2B hosted-sandbox probe for the R-421 managed-cloud candidate.

This command is intentionally not part of CI. It creates a short-lived E2B
cloud sandbox, runs one deterministic shell command, and tears the sandbox down
through the SDK context manager. It performs no LLM/provider inference.
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from harness_runtime.config.provider_secrets import (
    SecretResolutionError,
    make_provider_secret_resolver,
)
from harness_runtime.config_source import RuntimeConfigLoadError, RuntimeConfigSource

E2B_SECRET_NAME = "e2b-secret"


class LiveProbeError(RuntimeError):
    """Raised for a failed R-421 E2B live probe."""


def _print_step(message: str) -> None:
    print(f"[r421-e2b-live] {message}", flush=True)


def _load_sandbox_class() -> Any:
    try:
        module = importlib.import_module("e2b")
    except ImportError as exc:
        raise LiveProbeError(
            "Python module 'e2b' is not importable; install it explicitly for "
            "the live probe, e.g. `uv run --with e2b python tools/r421_e2b_live_probe.py`"
        ) from exc
    sandbox_cls = getattr(module, "Sandbox", None)
    if sandbox_cls is None:
        raise LiveProbeError("Python module 'e2b' does not expose Sandbox")
    return sandbox_cls


def resolve_e2b_secret_from_config(config_file: Path, *, secret_name: str) -> str:
    """Resolve the E2B API key through the configured provider-secret backend."""
    try:
        config = RuntimeConfigSource.load(config_file=config_file)
        resolver = make_provider_secret_resolver(config.provider_secrets)
        value = resolver.resolve_bootstrap_value(secret_name)
    except RuntimeConfigLoadError as exc:
        raise LiveProbeError(f"runtime config load failed: {exc}") from exc
    except SecretResolutionError as exc:
        raise LiveProbeError(f"provider-secret resolution failed: {exc}") from exc
    except Exception as exc:  # pragma: no cover - backend SDK/runtime boundary
        raise LiveProbeError(
            f"provider-secret backend failed for {secret_name!r}: {type(exc).__name__}"
        ) from exc

    if not value:
        raise LiveProbeError(f"provider-secret backend returned an empty value for {secret_name!r}")
    return value


def _result_stdout(result: Any) -> str:
    stdout = getattr(result, "stdout", None)
    if not isinstance(stdout, str):
        raise LiveProbeError("E2B command result did not expose string stdout")
    return stdout


def run_probe(
    *,
    sandbox_cls: Any,
    command: str,
    sandbox_timeout_seconds: int,
    command_timeout_seconds: int,
) -> str:
    """Create an E2B sandbox, run the command, and return stdout."""
    with sandbox_cls.create(
        timeout=sandbox_timeout_seconds,
        allow_internet_access=False,
        metadata={"roadmap_item": "R-421-managed-cloud-deployment-e2e"},
    ) as sandbox:
        result = sandbox.commands.run(command, timeout=command_timeout_seconds)
    return _result_stdout(result)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--command",
        default="printf r421-e2b-ok",
        help="Deterministic command to run inside the E2B sandbox.",
    )
    parser.add_argument("--sandbox-timeout", type=int, default=60)
    parser.add_argument("--command-timeout", type=int, default=15)
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=(
            "Runtime config whose provider_secrets backend should supply E2B_API_KEY "
            "when the environment variable is absent."
        ),
    )
    parser.add_argument(
        "--secret-name",
        default=E2B_SECRET_NAME,
        help=f"Provider-secret name to resolve from --config. Defaults to {E2B_SECRET_NAME}.",
    )
    parser.add_argument(
        "--resolve-only",
        action="store_true",
        help=(
            "Resolve the E2B API key from the configured backend, then exit "
            "without creating an E2B sandbox."
        ),
    )
    args = parser.parse_args(argv)

    if not os.environ.get("E2B_API_KEY"):
        if args.config is None:
            print(
                "R-421 E2B live probe failed: E2B_API_KEY is not set and --config was not provided",
                file=sys.stderr,
            )
            return 1
        try:
            os.environ["E2B_API_KEY"] = resolve_e2b_secret_from_config(
                args.config,
                secret_name=args.secret_name,
            )
        except LiveProbeError as exc:
            print(f"R-421 E2B live probe failed: {exc}", file=sys.stderr)
            return 1
        _print_step(
            f"resolved {args.secret_name} from configured provider-secret backend (redacted)"
        )
    else:
        _print_step("using E2B_API_KEY from environment")

    if args.resolve_only:
        _print_step("resolve-only completed: no hosted E2B sandbox was created")
        return 0

    try:
        sandbox_cls = _load_sandbox_class()
        _print_step("creating hosted E2B sandbox and running deterministic command")
        stdout = run_probe(
            sandbox_cls=sandbox_cls,
            command=args.command,
            sandbox_timeout_seconds=args.sandbox_timeout,
            command_timeout_seconds=args.command_timeout,
        )
    except LiveProbeError as exc:
        print(f"R-421 E2B live probe failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover - provider SDK/runtime boundary
        print(f"R-421 E2B live probe failed: {exc}", file=sys.stderr)
        return 1

    if stdout != "r421-e2b-ok":
        print(
            f"R-421 E2B live probe failed: unexpected stdout {stdout!r}; expected 'r421-e2b-ok'",
            file=sys.stderr,
        )
        return 1

    _print_step("completed: stdout=r421-e2b-ok hosted-provider-calls=1 cost=usage-billed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
