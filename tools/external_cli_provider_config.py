#!/usr/bin/env python3
"""Materialize a temp harness config for a local external CLI provider."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple

ROOT = Path(__file__).resolve().parents[1]


class ProviderPreset(NamedTuple):
    provider_name: str
    kind: str
    command: str
    model: str
    family: str
    auth_check: bool


PROVIDER_PRESETS: dict[str, ProviderPreset] = {
    "claude_code": ProviderPreset(
        provider_name="claude_code",
        kind="claude-code",
        command="claude",
        model="sonnet",
        family="anthropic",
        auth_check=True,
    ),
    "claude-code": ProviderPreset(
        provider_name="claude_code",
        kind="claude-code",
        command="claude",
        model="sonnet",
        family="anthropic",
        auth_check=True,
    ),
    "codex": ProviderPreset(
        provider_name="codex",
        kind="codex",
        command="codex",
        model="gpt-5",
        family="openai",
        auth_check=True,
    ),
    "antigravity": ProviderPreset(
        provider_name="antigravity",
        kind="antigravity",
        command="agy",
        model="Gemini 3.5 Flash (Low)",
        family="google",
        auth_check=True,
    ),
    "gemini": ProviderPreset(
        provider_name="gemini",
        kind="gemini",
        command="gemini",
        model="gemini-2.5-flash",
        family="google",
        auth_check=False,
    ),
}


def _load_toml(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _merge_config(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = _merge_config(current, value)
        else:
            merged[key] = value
    return merged


def _format_value(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, Mapping):
        items = ", ".join(f"{key} = {_format_value(item)}" for key, item in value.items())
        return f"{{ {items} }}"
    if isinstance(value, list | tuple):
        return "[" + ", ".join(_format_value(item) for item in value) + "]"
    raise TypeError(f"unsupported TOML value type: {type(value).__name__}")


def _render_table(path: tuple[str, ...], table: Mapping[str, Any]) -> list[str]:
    scalars: list[tuple[str, Any]] = []
    subtables: list[tuple[str, Mapping[str, Any]]] = []
    for key, value in table.items():
        if isinstance(value, Mapping):
            subtables.append((key, value))
        else:
            scalars.append((key, value))

    lines: list[str] = []
    if path:
        lines.append(f"[{'.'.join(path)}]")
    lines.extend(f"{key} = {_format_value(value)}" for key, value in scalars)
    if path and scalars:
        lines.append("")

    for key, value in subtables:
        lines.extend(_render_table((*path, key), value))
    return lines


def _render_toml(data: Mapping[str, Any]) -> str:
    lines: list[str] = []
    root_scalars = {key: value for key, value in data.items() if not isinstance(value, Mapping)}
    if root_scalars:
        lines.extend(f"{key} = {_format_value(value)}" for key, value in root_scalars.items())
        lines.append("")

    for key, value in data.items():
        if isinstance(value, Mapping):
            lines.extend(_render_table((key,), value))
    return "\n".join(lines).rstrip() + "\n"


def _default_output_path() -> Path:
    fd, name = tempfile.mkstemp(prefix="arhugula-external-cli-", suffix=".toml")
    os.close(fd)
    return Path(name)


def _preset_for(provider: str) -> ProviderPreset | None:
    return PROVIDER_PRESETS.get(provider)


def _build_provider_entry(
    *,
    preset: ProviderPreset | None,
    provider: str,
    provider_name: str | None,
    command: str | None,
    args: Sequence[str],
    auth_args: Sequence[str],
    response_format: str,
    prompt_transport: str,
    timeout_seconds: float,
    auth_check: bool | None,
) -> dict[str, Any]:
    if preset is None and provider != "generic-command":
        raise ValueError(
            "provider must be one of claude_code, codex, gemini, antigravity, "
            "or generic-command"
        )
    resolved_provider = provider_name or (preset.provider_name if preset else None)
    resolved_command = command or (preset.command if preset else None)
    if resolved_provider is None:
        raise ValueError("generic-command requires --provider-name")
    if resolved_command is None:
        raise ValueError("generic-command requires --command")

    kind = preset.kind if preset is not None else "generic-command"
    resolved_auth_check = (
        auth_check
        if auth_check is not None
        else (preset.auth_check if preset is not None else bool(auth_args))
    )

    entry: dict[str, Any] = {
        "provider": resolved_provider,
        "kind": kind,
        "command": resolved_command,
        "timeout_seconds": timeout_seconds,
        "auth_check": resolved_auth_check,
        "optional": False,
    }
    if args:
        entry["args"] = list(args)
    if auth_args:
        entry["auth_args"] = list(auth_args)
    if response_format != "text":
        entry["response_format"] = response_format
    if prompt_transport != "stdin":
        entry["prompt_transport"] = prompt_transport
    return entry


def materialize_external_cli_config(
    *,
    provider: str,
    base_config: Path,
    repo_root: Path,
    output: Path | None = None,
    model: str | None = None,
    provider_name: str | None = None,
    command: str | None = None,
    args: Sequence[str] = (),
    auth_args: Sequence[str] = (),
    response_format: str = "text",
    prompt_transport: str = "stdin",
    family: str | None = None,
    timeout_seconds: float = 120.0,
    auth_check: bool | None = None,
) -> Path:
    """Apply an external-CLI provider overlay to a temp copy of ``base_config``."""
    _ = repo_root
    preset = _preset_for(provider)
    entry = _build_provider_entry(
        preset=preset,
        provider=provider,
        provider_name=provider_name,
        command=command,
        args=args,
        auth_args=auth_args,
        response_format=response_format,
        prompt_transport=prompt_transport,
        timeout_seconds=timeout_seconds,
        auth_check=auth_check,
    )
    resolved_model = model or (preset.model if preset is not None else None)
    resolved_family = family or (preset.family if preset is not None else None)
    if resolved_model is None:
        raise ValueError("generic-command requires --model")
    if resolved_family is None:
        raise ValueError("generic-command requires --family")

    provider_key = str(entry["provider"])
    overlay: dict[str, Any] = {
        "runtime": {
            "enabled_provider_names": [provider_key],
            "external_cli_providers": [entry],
            "routing_manifest": {
                "manifest_version": 1,
                "per_role_bindings": {},
                "per_workload_overrides": {},
                "retry_policies": {},
                "fallback_chains": [
                    {
                        "primary": {
                            "provider": provider_key,
                            "model": resolved_model,
                            "family": resolved_family,
                        },
                        "same_family": [],
                        "cross_family": [],
                    }
                ],
            },
        }
    }
    output_path = output or _default_output_path()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        _render_toml(_merge_config(_load_toml(base_config), overlay)),
        encoding="utf-8",
    )
    return output_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "provider",
        help=(
            "claude_code, codex, legacy gemini, antigravity, or generic-command"
        ),
    )
    parser.add_argument("--provider-name", default=None)
    parser.add_argument("--command", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--family", default=None)
    parser.add_argument("--arg", action="append", default=[])
    parser.add_argument("--auth-arg", action="append", default=[])
    parser.add_argument(
        "--response-format",
        choices=("text", "json", "jsonl"),
        default="text",
    )
    parser.add_argument(
        "--prompt-transport",
        choices=("stdin", "arg"),
        default="stdin",
    )
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--auth-check", dest="auth_check", action="store_true", default=None)
    parser.add_argument("--no-auth-check", dest="auth_check", action="store_false")
    parser.add_argument("--base", type=Path, default=ROOT / "harness.toml")
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    try:
        output = materialize_external_cli_config(
            provider=args.provider,
            provider_name=args.provider_name,
            command=args.command,
            model=args.model,
            family=args.family,
            args=tuple(args.arg),
            auth_args=tuple(args.auth_arg),
            response_format=args.response_format,
            prompt_transport=args.prompt_transport,
            timeout_seconds=args.timeout_seconds,
            auth_check=args.auth_check,
            base_config=args.base,
            repo_root=args.repo_root,
            output=args.output,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(output.as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
