"""U-RT-103 — `RuntimeConfigSource` 3-source layered precedence loader.

Implements runtime spec v1.35 §3.7 (NEW). Composes a :class:`RuntimeConfig`
from three sources in strict ascending priority:

1. Environment variables (``HARNESS_*`` prefix, Pydantic-settings env source)
2. TOML config file (``harness.toml`` at workspace root by default; overridable)
3. CLI overrides (per-invocation dict; highest priority)

Sibling to the legacy U-RT-04 ``materialize_runtime_config()`` (env + kwargs
only); U-RT-103 adds the config-file layer and the secrets-exclusion guard
per Q-L=(b) ratification.

Failure surface (spec v1.35 §14.18.4):
    All load-side errors raise :class:`RuntimeConfigLoadError`, mapped at the
    CLI layer to fail-class ``RT-FAIL-CLI-CONFIG-LOAD`` → exit code 3.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any, ClassVar, cast

from harness_core.deployment_surface import DeploymentSurface
from harness_cp.topology_pattern import TopologyPattern
from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from harness_runtime.types import RuntimeConfig

__all__ = [
    "DEFAULT_CONFIG_FILE_NAME",
    "ENV_PREFIX",
    "RUNTIME_CONFIG_LOAD_FAIL_CLASS",
    "RuntimeConfigLoadError",
    "RuntimeConfigSource",
]


ENV_PREFIX = "HARNESS_"
DEFAULT_CONFIG_FILE_NAME = "harness.toml"
RUNTIME_CONFIG_LOAD_FAIL_CLASS = "RT-FAIL-CLI-CONFIG-LOAD"

# Heuristic for plaintext secret detection in TOML. Per spec v1.35 §3.7
# "Secrets exclusion" + Q-L=(b): operator-supplied secrets must flow through
# ADR-F5 keyring, not plaintext config files. The patterns catch the obvious
# mistakes (an LLM provider API key dropped into harness.toml). The match is
# case-insensitive and applies at any nesting depth of the TOML document.
_SECRET_KEY_PATTERN = re.compile(
    r"(?:^|_)(api_?key|secret|password|passphrase|token|credential)s?$",
    re.IGNORECASE,
)

EXAMPLE_CONFIG_FILE_NAME = "harness.toml.example"


def _format_validation_error(exc: ValidationError) -> str:
    """Render a Pydantic ``ValidationError`` as a human-readable message.

    Pre-fix the loader raised ``f"Pydantic validation failed: {exc.errors()}"``
    which emits Python ``list[dict].__repr__()`` (e.g. ``[{'type': 'missing',
    'loc': ('deployment_surface',), 'msg': 'Field required', 'input': {},
    'url': 'https://errors.pydantic.dev/2.13/v/missing'}, ...]``). Operators
    parsing that at the terminal had to mentally decode the dict-repr to
    discover which fields were missing or invalid.

    Per probe-v4 adjacent finding (a) at
    ``.harness/class_1_fork_daemon_default_socket_path_pid_mismatch.md`` §4
    (operator-routed β-scope apply 2026-05-29): split errors into
    ``missing`` vs ``invalid`` buckets, render each as a dotted-path
    bullet list, and append a pointer to the workspace-root
    ``harness.toml.example`` template.

    Within X-AL-3 boundary: ZERO new operator-facing subcommand; ZERO new
    spec surface; the fail-class identifier ``RT-FAIL-CLI-CONFIG-LOAD`` is
    PRESERVED VERBATIM per runtime spec v1.39 §14.18.4. Only the payload
    text is reformatted. ``harness init`` template-generator subcommand
    (the third pain point at the same finding) remains foreclosed as
    spec-extension shape per CLAUDE.md §4.4.
    """
    missing: list[str] = []
    invalid: list[tuple[str, str]] = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err.get("loc", ()))
        if not loc:
            loc = "<root>"
        if err.get("type") == "missing":
            missing.append(loc)
        else:
            invalid.append((loc, str(err.get("msg", ""))))

    lines: list[str] = []
    if missing:
        lines.append("Missing required fields:")
        for field in missing:
            lines.append(f"  - {field}")
    if invalid:
        if lines:
            lines.append("")
        lines.append("Invalid fields:")
        for field, msg in invalid:
            lines.append(f"  - {field}: {msg}")
    lines.append("")
    lines.append(
        f"See {EXAMPLE_CONFIG_FILE_NAME} at the workspace root for a "
        f"template covering all required fields."
    )
    return "\n".join(lines)


class RuntimeConfigLoadError(Exception):
    """Typed exception for any 3-source loader failure.

    Maps to CLI fail-class ``RT-FAIL-CLI-CONFIG-LOAD`` at the
    :mod:`harness_runtime.cli` layer (spec v1.35 §14.18.4 → exit code 3).
    """

    FAIL_CLASS: ClassVar[str] = RUNTIME_CONFIG_LOAD_FAIL_CLASS

    def __init__(self, reason: str, *, source: str | None = None) -> None:
        self.reason = reason
        self.source = source
        suffix = f" [source={source}]" if source else ""
        super().__init__(f"{self.FAIL_CLASS}: {reason}{suffix}")


class _RuntimeEnvSettings(BaseSettings):
    """Sidecar :class:`BaseSettings` reading scalar ``HARNESS_*`` env vars.

    Mirrors the existing U-RT-04 ``_ENV_SCALAR_FIELDS`` set. Sub-config
    fields (path_bindings, provider_secrets, otel, collector) are NOT
    env-keyed at the v1 source layer; operators provide them via config
    file or CLI overrides.
    """

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        extra="ignore",
        case_sensitive=False,
    )

    deployment_surface: DeploymentSurface | None = None
    repository_root: Path | None = None
    default_topology: TopologyPattern | None = None
    tenant_id: str | None = None
    ollama_host: str | None = None
    ollama_optional: bool | None = None
    # B-EFFECT-FENCE (§14.22 C-RT-31) — env-keyed: it gates a CORRECTNESS property
    # (at-most-once execution), so HARNESS_EFFECT_FENCING must not be silently
    # dropped. (The recent `inter_step_data_flow` / `*_optional` flags remain
    # file/CLI-only — a known env gap, separate config-hygiene item.)
    effect_fencing: bool | None = None
    # B-L2-EMBEDDING-ACTIVATION (C-CP-02 §2.2) — env-keyed for the SAME reason: it
    # gates a behavior-changing property (which model serves a workload), so
    # HARNESS_ROUTING_ACTIVATION must not be silently dropped.
    routing_activation: bool | None = None


class RuntimeConfigSource:
    """3-source layered loader composing :class:`RuntimeConfig` from env, file, CLI.

    See module docstring for precedence.

    Single public entrypoint :py:meth:`load` is a classmethod by design — the
    source itself is stateless; the per-invocation state lives in the
    ``config_file`` + ``cli_overrides`` arguments.
    """

    @classmethod
    def load(
        cls,
        config_file: Path | None = None,
        cli_overrides: dict[str, Any] | None = None,
    ) -> RuntimeConfig:
        """Compose a :class:`RuntimeConfig` from the 3 sources.

        Parameters
        ----------
        config_file:
            Path to a TOML config file. When ``None``, auto-discover
            ``DEFAULT_CONFIG_FILE_NAME`` (``harness.toml``) in the process CWD per
            spec §3.7 (Reading A); if absent, the config-file layer contributes
            nothing (precedence reduces to env + CLI). When set explicitly, the
            file MUST exist and parse as TOML.
        cli_overrides:
            Per-invocation dict of CLI-flag overrides. Highest priority.

        Raises
        ------
        RuntimeConfigLoadError
            * TOML parse failure (``ManifestParseError``-shaped).
            * Plaintext-secret detected at any TOML nesting level.
            * Pydantic validation failure on the composed kwargs (e.g., type
              mismatch, missing required field, unknown field).
        """
        env_values = cls._load_env_values()
        # §3.7 auto-discovery (Reading A): an omitted --config falls back to a
        # CWD-local harness.toml; absent → env+CLI-only (today's no-file behavior).
        resolved_config_file = (
            config_file if config_file is not None else cls._discover_default_config()
        )
        file_values = (
            cls._load_file_values(resolved_config_file) if resolved_config_file is not None else {}
        )
        cli_values = dict(cli_overrides or {})

        merged: dict[str, Any] = {}
        merged.update(env_values)
        merged.update(file_values)
        merged.update(cli_values)

        try:
            return RuntimeConfig(**merged)
        except ValidationError as exc:
            raise RuntimeConfigLoadError(
                _format_validation_error(exc),
                source="merged",
            ) from exc

    @staticmethod
    def _discover_default_config() -> Path | None:
        """§3.7 auto-discovery (Reading A — CWD).

        When ``--config`` is omitted, look for ``DEFAULT_CONFIG_FILE_NAME``
        (``harness.toml``) in the process working directory. Returns the path if
        it is an existing file, else ``None`` (preserving the env+CLI-only
        behavior for the no-file case). This wires the previously-dead
        ``DEFAULT_CONFIG_FILE_NAME`` constant and closes the spec §3.7 gap
        (declared-but-unimplemented). "Workspace root" is read as the CWD: the
        true ``repository_root`` lives *inside* harness.toml, so discovery cannot
        key on it without circularity.
        """
        candidate = Path.cwd() / DEFAULT_CONFIG_FILE_NAME
        return candidate if candidate.is_file() else None

    @staticmethod
    def _load_env_values() -> dict[str, Any]:
        """Read ``HARNESS_*`` env vars via pydantic-settings; return non-None values."""
        try:
            sidecar = _RuntimeEnvSettings()
        except ValidationError as exc:
            raise RuntimeConfigLoadError(
                f"env-var coercion failed:\n{_format_validation_error(exc)}",
                source="env",
            ) from exc
        return {k: v for k, v in sidecar.model_dump().items() if v is not None}

    @classmethod
    def _load_file_values(cls, config_file: Path) -> dict[str, Any]:
        """Read + parse a TOML config file; raise on plaintext-secret presence."""
        try:
            raw_bytes = config_file.read_bytes()
        except OSError as exc:
            raise RuntimeConfigLoadError(
                f"config file read failed: {exc}",
                source=str(config_file),
            ) from exc

        try:
            document = tomllib.loads(raw_bytes.decode("utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise RuntimeConfigLoadError(
                f"TOML parse error: {exc}",
                source=str(config_file),
            ) from exc
        except UnicodeDecodeError as exc:
            raise RuntimeConfigLoadError(
                f"config file is not valid UTF-8: {exc}",
                source=str(config_file),
            ) from exc

        cls._reject_plaintext_secrets(document, str(config_file))

        # Project TOML structure to RuntimeConfig field-set. Spec §3.7
        # declares section examples like `[runtime] tenant_id = "..."`;
        # accept either flat top-level keys OR a `[runtime]` table.
        runtime_section = document.get("runtime")
        if isinstance(runtime_section, dict):
            return dict(cast(dict[str, Any], runtime_section))
        return dict(document)

    @classmethod
    def _reject_plaintext_secrets(cls, node: object, source: str, path: str = "") -> None:
        """Walk a parsed TOML document; raise on any LEAF key matching the
        secret pattern.

        The detector targets plaintext-secret VALUES being placed in config
        files (e.g., ``api_key = "sk-..."``), NOT schema table names that
        happen to match the regex. Per
        ``[[finding-runtime-config-loader-unreachable-sub-configs]]`` fix
        (B): before this change, the SCHEMA FIELD NAME ``provider_secrets``
        itself matched the regex (ends in ``secrets``), so any
        ``[runtime.provider_secrets]`` sub-table — even empty — raised the
        detector. This made the file-loader pathway unreachable in
        conjunction with the missing sub-config defaults (the matching
        ``types.py`` fix (A) restores those defaults).

        Discrimination: skip the key-check when the value is a dict (i.e., a
        TOML sub-table). Sub-tables can only carry name structure, not
        secret values; their leaf descendants are still recursively checked.
        Net: the detector still fires on real plaintext secrets at any
        nesting depth (because the recursion proceeds into dict values and
        eventually hits a leaf with a matching key) but no longer false-
        matches on schema field names whose values are themselves tables.
        """
        if isinstance(node, dict):
            for key, value in cast(dict[object, object], node).items():
                if not isinstance(key, str):
                    continue
                if not isinstance(value, dict) and _SECRET_KEY_PATTERN.search(key):
                    full_path = f"{path}.{key}" if path else key
                    raise RuntimeConfigLoadError(
                        f"plaintext secret detected at '{full_path}': "
                        "secrets must be sourced via ADR-F5 keyring "
                        "(see RuntimeConfig.provider_secrets)",
                        source=source,
                    )
                cls._reject_plaintext_secrets(
                    cast(object, value), source, f"{path}.{key}" if path else key
                )
        elif isinstance(node, list):
            for idx, item in enumerate(cast(list[Any], node)):
                cls._reject_plaintext_secrets(item, source, f"{path}[{idx}]")
