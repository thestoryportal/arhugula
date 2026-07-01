"""U-RT-83 — RuntimeConfig.validator_framework_config + ValidatorFrameworkConfig empty-marker tests.

Authority: C-RT-23.

ACs per runtime plan v2.17 §1 U-RT-83:

1. RuntimeConfig.validator_framework_config field appended at the
   established field-ordering position (after `memory_tool_backend_config`);
   type `ValidatorFrameworkConfig | None`; default `None`; frozen-model
   invariant preserved.
2. `ValidatorFrameworkConfig` empty-marker authored as `@dataclass(frozen=True)`
   with NO fields; `.default()` classmethod returns `cls()` empty instance.
3. Module organization under `lifecycle/` (true parallel to §14.12 precedent
   `memory_tool_types.py`).
4. `RuntimeConfig(validator_framework_config=None)` constructs successfully +
   `RuntimeConfig(validator_framework_config=ValidatorFrameworkConfig.default())`
   constructs successfully. RuntimeConfig Pydantic v2 frozen-model validation
   passes in both shapes.
5. Spec v1.18 §3 C-RT-02 RuntimeConfig table NEW row verbatim.
6. Importable; pyright strict mode passes.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest
from harness_core.deployment_surface import DeploymentSurface
from harness_cp.topology_pattern import TopologyPattern
from harness_runtime.lifecycle.validator_framework_types import (
    ValidatorFrameworkConfig,
)
from harness_runtime.types import (
    CollectorConfig,
    OTelConfig,
    PathBindingConfig,
    ProviderSecretsConfig,
    RuntimeConfig,
)
from pydantic import ValidationError


def _minimal_runtime_config_kwargs(tmp_path: Path) -> dict[str, Any]:
    return {
        "deployment_surface": DeploymentSurface.LOCAL_DEVELOPMENT,
        "repository_root": tmp_path,
        "path_bindings": PathBindingConfig(),
        "provider_secrets": ProviderSecretsConfig(),
        "otel": OTelConfig(otlp_endpoint="http://localhost:4318"),
        "collector": CollectorConfig(),
        "default_topology": TopologyPattern.SINGLE_THREADED_LINEAR,
    }


# AC #1 — field appended, defaults to None.


def test_runtime_config_accepts_explicit_none(tmp_path: Path) -> None:
    config = RuntimeConfig(
        **_minimal_runtime_config_kwargs(tmp_path),
        validator_framework_config=None,
    )
    assert config.validator_framework_config is None


def test_runtime_config_field_defaults_to_none_when_omitted(tmp_path: Path) -> None:
    """Backwards-compatibility — instantiating without the new field defaults
    to None (preserves v1.17-shape behaviour)."""
    config = RuntimeConfig(**_minimal_runtime_config_kwargs(tmp_path))
    assert config.validator_framework_config is None


def test_runtime_config_field_declared_in_schema() -> None:
    """AC #1 — Pydantic schema declares the field."""
    assert "validator_framework_config" in RuntimeConfig.model_fields


# AC #2 — empty-marker dataclass shape.


def test_validator_framework_config_is_frozen_dataclass() -> None:
    assert dataclasses.is_dataclass(ValidatorFrameworkConfig)
    fields = dataclasses.fields(ValidatorFrameworkConfig)
    assert fields == (), "empty-marker must declare zero fields at v1.18"
    # frozen=True: assignment raises.
    instance = ValidatorFrameworkConfig()
    with pytest.raises(dataclasses.FrozenInstanceError):
        instance.unknown = "x"  # type: ignore[attr-defined]


def test_validator_framework_config_default_factory() -> None:
    default = ValidatorFrameworkConfig.default()
    assert isinstance(default, ValidatorFrameworkConfig)
    # Empty-marker: all instances structurally equal.
    assert default == ValidatorFrameworkConfig()


# AC #3 — module organization under lifecycle/ (parallel to §14.12 precedent).


def test_module_lives_under_lifecycle_subdirectory() -> None:
    """AC #3 — true parallel to memory_tool_types.py per F1-03 absorption."""
    import harness_runtime.lifecycle.validator_framework_types as mod

    assert mod.__name__ == "harness_runtime.lifecycle.validator_framework_types"


# AC #4 — both shapes pass RuntimeConfig Pydantic validation.


def test_runtime_config_accepts_default_factory_instance(tmp_path: Path) -> None:
    config = RuntimeConfig(
        **_minimal_runtime_config_kwargs(tmp_path),
        validator_framework_config=ValidatorFrameworkConfig.default(),
    )
    assert isinstance(config.validator_framework_config, ValidatorFrameworkConfig)


def test_runtime_config_rejects_wrong_type(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        RuntimeConfig(
            **_minimal_runtime_config_kwargs(tmp_path),
            validator_framework_config="not_a_config",  # type: ignore[arg-type]
        )


def test_runtime_config_frozen_invariant_preserved(tmp_path: Path) -> None:
    """AC #1 — RuntimeConfig frozen-model invariant preserved post-addition."""
    config = RuntimeConfig(**_minimal_runtime_config_kwargs(tmp_path))
    with pytest.raises(ValidationError):
        config.validator_framework_config = ValidatorFrameworkConfig()  # type: ignore[misc]


# AC #6 — importable from the package surface.


def test_carriers_importable() -> None:
    # Imports at module top; assert schema-level field presence + default factory.
    assert "validator_framework_config" in RuntimeConfig.model_fields
    assert ValidatorFrameworkConfig.default() == ValidatorFrameworkConfig()
