"""U-RT-104 + U-RT-105 — `WorkflowManifestLoader` for YAML / TOML manifests.

Implements C-RT-30 per runtime spec v1.36 §14.19 + SF-1 §3 + §4. Lands the
loader skeleton + projection layer:

- U-RT-104 ``load(path) -> WorkflowManifest`` — file-extension dispatch
  (``.yaml`` / ``.yml`` → strictyaml, ``.toml`` → tomllib stdlib), 8 typed
  exceptions, intermediate Pydantic carrier projecting to the SF-1 §3.1
  schema body, eager validation per spec §14.19.4 invariants 1-4 + 7.

- U-RT-105 ``load_workflow(path) -> LoadedWorkflow`` — sibling method that
  composes ``load()`` + projects the intermediate carrier to a
  ``WorkflowObject`` Protocol-conformant value (via ``LoadedWorkflow`` frozen
  Pydantic BaseModel). Adds step_payload JSON-serializability check
  (invariant 9) + YAML↔TOML round-trip equivalence (invariant 8).

The U-RT-105 plan §1.5 wording is "EXTEND load(path)"; implemented as a
sibling method to preserve U-RT-104's intermediate-carrier surface (used by
callers needing the parsed shape without forcing full WME projection — e.g.,
operator-facing tooling that surfaces validation errors at the carrier layer).

Per the v1.36 canonical-reading amendment, deployment-surface-keyed
engine_class admissibility defers to the runtime caller (U-RT-106).
U-CP-22 workload-keyed topology admissibility runs at load-time per AC #12.
Per plan v2.32 §1.2, U-RT-104 AC #11 reframes to enum-validity only.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any, ClassVar, cast

import yaml
from harness_core.identity import StepID
from harness_core.persona_tier import PersonaTier
from harness_core.workload_class import WorkloadClass
from harness_cp.cp_shared_types import ModelBinding
from harness_cp.engine_class import EngineClass
from harness_cp.gate_level_rule import GateLevel
from harness_cp.topology_pattern import TopologyPattern
from harness_cp.workflow_driver_types import StepKind, WorkflowStep
from harness_cp.workflow_manifest_entry import (
    FanoutTimeoutDisposition,
    WorkflowManifestEntry,
)
from pydantic import BaseModel, ConfigDict, ValidationError

from harness_runtime.lifecycle.strict_safe_loader import strict_safe_load

__all__ = [
    "MANIFEST_VERSION_V1",
    "SUPPORTED_MANIFEST_EXTENSIONS",
    "LoadedWorkflow",
    "ManifestAdmissibilityError",
    "ManifestEnumValueError",
    "ManifestParseError",
    "ManifestSchemaError",
    "ManifestStepIDCollisionError",
    "UnsupportedManifestFormatError",
    "UnsupportedManifestVersionError",
    "WorkflowManifest",
    "WorkflowManifestLoadError",
    "WorkflowManifestLoader",
]


MANIFEST_VERSION_V1 = 1
SUPPORTED_MANIFEST_EXTENSIONS: frozenset[str] = frozenset({".yaml", ".yml", ".toml"})


# ---------------------------------------------------------------------------
# Typed exception taxonomy (spec v1.36 §14.19.2)
# ---------------------------------------------------------------------------


class WorkflowManifestLoadError(Exception):
    """Base for the 7 typed exceptions; all CLI errors map at §14.18.4."""

    FAIL_CLASS: ClassVar[str] = "RT-FAIL-CLI-MANIFEST-LOAD"

    def __init__(self, reason: str, *, source: str | None = None) -> None:
        self.reason = reason
        self.source = source
        suffix = f" [source={source}]" if source else ""
        super().__init__(f"{self.FAIL_CLASS}: {reason}{suffix}")


class UnsupportedManifestFormatError(WorkflowManifestLoadError):
    FAIL_CLASS: ClassVar[str] = "RT-FAIL-CLI-MANIFEST-FORMAT-UNSUPPORTED"


class UnsupportedManifestVersionError(WorkflowManifestLoadError):
    FAIL_CLASS: ClassVar[str] = "RT-FAIL-CLI-MANIFEST-VERSION-UNSUPPORTED"


class ManifestParseError(WorkflowManifestLoadError):
    FAIL_CLASS: ClassVar[str] = "RT-FAIL-CLI-MANIFEST-PARSE"


class ManifestSchemaError(WorkflowManifestLoadError):
    FAIL_CLASS: ClassVar[str] = "RT-FAIL-CLI-MANIFEST-SCHEMA"


class ManifestEnumValueError(WorkflowManifestLoadError):
    FAIL_CLASS: ClassVar[str] = "RT-FAIL-CLI-MANIFEST-ENUM-VALUE"


class ManifestStepIDCollisionError(WorkflowManifestLoadError):
    FAIL_CLASS: ClassVar[str] = "RT-FAIL-CLI-MANIFEST-STEP-ID-COLLISION"


class ManifestAdmissibilityError(WorkflowManifestLoadError):
    """Topology admissibility per U-CP-22; engine_class admissibility per
    U-RT-106 caller (NOT raised by this loader at v1.36, per fork doc β)."""

    FAIL_CLASS: ClassVar[str] = "RT-FAIL-CLI-MANIFEST-ADMISSIBILITY"


# ---------------------------------------------------------------------------
# Intermediate Pydantic carrier (SF-1 §3.1 shape; not WorkflowManifestEntry)
# ---------------------------------------------------------------------------


class _ModelBindingSection(BaseModel):
    """`default_model_binding` table from SF-1 §3.1."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    provider: str
    model: str


class _StepEntry(BaseModel):
    """Single `steps[]` entry from SF-1 §3.1."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str
    step_kind: StepKind
    step_payload: dict[str, Any]


class _WorkflowSection(BaseModel):
    """`workflow` table from SF-1 §3.1.

    Optional structured fields (`layer_budgets`, `fallback_chain`,
    `hitl_placements`, `sub_agent_briefs`, `per_step_overrides`) are kept
    opaque at the intermediate-carrier layer — full projection to the CP
    `WorkflowManifestEntry` shape lands at U-RT-105. Enum fields validate
    eagerly per spec §14.19.4 invariants 2 + 3.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_id: str
    workload_class: WorkloadClass
    persona_tier: PersonaTier
    engine_class: EngineClass
    topology_pattern: TopologyPattern
    entry_version: int = 1
    default_gate_level: GateLevel | None = None
    fanout_timeout_disposition: FanoutTimeoutDisposition = FanoutTimeoutDisposition.FAIL_CLOSED
    layer_budgets: list[Any] = []
    fallback_chain: dict[str, Any] | None = None
    hitl_placements: list[Any] = []
    sub_agent_briefs: list[Any] | None = None
    per_step_overrides: dict[str, Any] = {}


class WorkflowManifest(BaseModel):
    """Intermediate loader-internal carrier projecting the SF-1 §3.1 schema.

    Frozen + ``extra=forbid`` gives closed-schema (invariant 1), eager
    validation (invariant 2), and idempotency (invariant 7) — equal inputs
    produce ``__eq__``-equal carriers.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    workflow: _WorkflowSection
    default_model_binding: _ModelBindingSection
    steps: list[_StepEntry]


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


class WorkflowManifestLoader:
    """Loads a YAML / TOML workflow manifest into a :class:`WorkflowManifest`.

    U-RT-105 will extend ``load()`` to return a full ``WorkflowObject``
    Protocol-conformant value; U-RT-104 lands the intermediate carrier shape
    and the 7 typed exception surface.
    """

    @classmethod
    def load(cls, path: Path) -> WorkflowManifest:
        """Parse + validate the manifest at ``path``.

        Raises one of the 7 :class:`WorkflowManifestLoadError` subclasses.
        On success, returns a frozen :class:`WorkflowManifest` carrier
        satisfying spec §14.19.4 invariants 1-9 except for engine_class
        deployment-surface admissibility (deferred to U-RT-106 per fork
        doc β).
        """
        document = cls._parse(path)
        cls._check_version(document, source=str(path))
        carrier = cls._build_carrier(document, source=str(path))
        cls._check_step_id_uniqueness(carrier, source=str(path))
        # Topology admissibility deferred to runtime sub-agent-dispatch site per
        # spec v1.38 Reading A (PR #80). Workflows that never dispatch sub-agents
        # (single-step inference workflows) escape the check entirely — by design.
        # Runtime authority: harness-runtime/.../sub_agent_dispatch.py:585.
        return carrier

    # ---------- parse ----------

    @classmethod
    def _parse(cls, path: Path) -> dict[str, Any]:
        suffix = path.suffix.lower()
        if suffix not in SUPPORTED_MANIFEST_EXTENSIONS:
            raise UnsupportedManifestFormatError(
                f"unsupported extension {suffix!r}; expected one of "
                f"{sorted(SUPPORTED_MANIFEST_EXTENSIONS)}",
                source=str(path),
            )
        try:
            raw_bytes = path.read_bytes()
        except OSError as exc:
            raise ManifestParseError(
                f"manifest file read failed: {exc}",
                source=str(path),
            ) from exc
        if suffix == ".toml":
            return cls._parse_toml(raw_bytes, source=str(path))
        return cls._parse_yaml(raw_bytes, source=str(path))

    @staticmethod
    def _parse_yaml(raw_bytes: bytes, source: str) -> dict[str, Any]:
        # Per runtime spec v1.39 §14.19 Reading A: strictyaml.dirty_load was
        # replaced with `strict_safe_load` (pyyaml SafeLoader subclass) to
        # preserve YAML 1.1 native scalar typing (int/float/bool). Strictness
        # preserved: duplicate-key detection + flow-style ban + anchor/alias
        # ban. Closes probe finding #16/#17 (PR #79).
        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ManifestParseError(
                f"manifest is not valid UTF-8: {exc}",
                source=source,
            ) from exc
        try:
            data = strict_safe_load(text)
        except yaml.YAMLError as exc:
            raise ManifestParseError(
                f"YAML parse error: {exc}",
                source=source,
            ) from exc
        if not isinstance(data, dict):
            raise ManifestSchemaError(
                "top-level YAML value must be a mapping",
                source=source,
            )
        return cast(dict[str, Any], data)

    @staticmethod
    def _parse_toml(raw_bytes: bytes, source: str) -> dict[str, Any]:
        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ManifestParseError(
                f"manifest is not valid UTF-8: {exc}",
                source=source,
            ) from exc
        try:
            return tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ManifestParseError(
                f"TOML parse error: {exc}",
                source=source,
            ) from exc

    # ---------- version check ----------

    @classmethod
    def _check_version(cls, document: dict[str, Any], *, source: str) -> None:
        if "version" not in document:
            raise UnsupportedManifestVersionError(
                "required field 'version' is absent at top level",
                source=source,
            )
        raw_version = document["version"]
        # YAML + TOML loaders both preserve native int scalars per spec v1.39
        # Reading A (pyyaml StrictSafeLoader replaces strictyaml.dirty_load).
        # Operator-string-typed `version: "1"` still rejected — version MUST
        # be the literal int 1 per the contract.
        if not isinstance(raw_version, int) or isinstance(raw_version, bool):
            raise UnsupportedManifestVersionError(
                f"'version' must be the integer {MANIFEST_VERSION_V1}; got {raw_version!r}",
                source=source,
            )
        if raw_version != MANIFEST_VERSION_V1:
            raise UnsupportedManifestVersionError(
                f"'version' must equal {MANIFEST_VERSION_V1}; got {raw_version}",
                source=source,
            )

    # ---------- carrier construction ----------

    @classmethod
    def _build_carrier(cls, document: dict[str, Any], *, source: str) -> WorkflowManifest:
        # Per runtime spec v1.39 §14.19 Reading A: both YAML and TOML loaders
        # preserve native scalar types; no boundary coercion needed. The
        # `_coerce_int_fields` helper present pre-v1.39 was retired with the
        # strictyaml.dirty_load → strict_safe_load swap.
        try:
            return WorkflowManifest.model_validate(document)
        except ValidationError as exc:
            raise cls._project_validation_error(exc, source=source) from exc

    @staticmethod
    def _project_validation_error(
        exc: ValidationError, *, source: str
    ) -> WorkflowManifestLoadError:
        """Discriminate Pydantic errors → typed taxonomy.

        StrEnum coercion failures map to ``ManifestEnumValueError`` (AC #9);
        any other validation failure (missing field, unknown field, type
        mismatch) maps to ``ManifestSchemaError`` (AC #6 / #7 / #8).
        """
        for err in exc.errors():
            err_type = err.get("type", "")
            if "enum" in err_type:
                location = ".".join(str(loc) for loc in err.get("loc", ()))
                raise ManifestEnumValueError(
                    f"invalid enum value at '{location}': {err.get('msg', '')}",
                    source=source,
                )
        return ManifestSchemaError(
            f"schema validation failed: {exc.errors()}",
            source=source,
        )

    # ---------- step-id uniqueness (invariant 4) ----------

    @staticmethod
    def _check_step_id_uniqueness(manifest: WorkflowManifest, *, source: str) -> None:
        seen: dict[str, int] = {}
        for idx, step in enumerate(manifest.steps):
            if step.step_id in seen:
                raise ManifestStepIDCollisionError(
                    f"step_id {step.step_id!r} appears at steps[{seen[step.step_id]}] "
                    f"and steps[{idx}]; step_ids MUST be unique within a workflow",
                    source=source,
                )
            seen[step.step_id] = idx

    # ---------- topology admissibility ----------
    # Per spec v1.38 §14.19.4 invariant 2 Reading A (PR #80): topology_pattern
    # admissibility is deferred from load-time to runtime sub-agent-dispatch
    # site at sub_agent_dispatch.py:585 (`topology_dispatcher.is_topology_permitted`).
    # Workflows that never dispatch sub-agents (single-step inference workflows)
    # escape the check by design. Pattern-consistent with v1.36 Reading β which
    # moved engine_class admissibility loader → U-RT-106. The load-time check
    # method previously here is RETIRED at v1.38; ManifestAdmissibilityError
    # itself stays in the taxonomy for engine_class admissibility per v1.36.

    # =============================================================
    # U-RT-105 — projection to WorkflowObject Protocol
    # =============================================================

    @classmethod
    def load_workflow(cls, path: Path) -> LoadedWorkflow:
        """Load + project the manifest at ``path`` into a :class:`LoadedWorkflow`.

        Composes :py:meth:`load` (U-RT-104 intermediate carrier) + projection
        to a ``WorkflowObject``-Protocol-conformant value (U-RT-105). Adds
        spec §14.19.4 invariant 9 (``step_payload`` JSON-serializability) on
        top of the U-RT-104 validation surface.

        Raises the same typed-exception taxonomy as :py:meth:`load`, plus
        :class:`ManifestSchemaError` when any ``step_payload`` is not
        ``json.dumps`` round-trippable.
        """
        manifest = cls.load(path)
        cls._check_step_payloads_json_serializable(manifest, source=str(path))
        return cls._project_to_loaded_workflow(manifest, source=str(path))

    @staticmethod
    def _check_step_payloads_json_serializable(manifest: WorkflowManifest, *, source: str) -> None:
        for idx, step in enumerate(manifest.steps):
            try:
                json.dumps(step.step_payload)
            except (TypeError, ValueError) as exc:
                raise ManifestSchemaError(
                    f"steps[{idx}].step_payload ({step.step_id!r}) is not JSON-serializable: {exc}",
                    source=source,
                ) from exc

    @classmethod
    def _project_to_loaded_workflow(
        cls, manifest: WorkflowManifest, *, source: str
    ) -> LoadedWorkflow:
        try:
            manifest_entry = WorkflowManifestEntry.model_validate(
                {
                    "workflow_id": manifest.workflow.workflow_id,
                    "workload_class": manifest.workflow.workload_class,
                    "persona_tier": manifest.workflow.persona_tier,
                    "engine_class": manifest.workflow.engine_class,
                    "topology_pattern": manifest.workflow.topology_pattern,
                    "layer_budgets": manifest.workflow.layer_budgets,
                    "fallback_chain": manifest.workflow.fallback_chain,
                    "hitl_placements": manifest.workflow.hitl_placements,
                    "sub_agent_briefs": manifest.workflow.sub_agent_briefs,
                    "per_step_overrides": manifest.workflow.per_step_overrides,
                    "entry_version": manifest.workflow.entry_version,
                    "default_gate_level": manifest.workflow.default_gate_level,
                    "fanout_timeout_disposition": (manifest.workflow.fanout_timeout_disposition),
                }
            )
        except ValidationError as exc:
            raise cls._project_validation_error(exc, source=source) from exc
        steps = tuple(
            WorkflowStep(
                step_id=StepID(s.step_id),
                step_kind=s.step_kind,
                step_payload=s.step_payload,
            )
            for s in manifest.steps
        )
        default_model_binding = ModelBinding(
            provider=manifest.default_model_binding.provider,
            model=manifest.default_model_binding.model,
        )
        return LoadedWorkflow(
            workflow_id=manifest.workflow.workflow_id,
            workload_class=manifest.workflow.workload_class,
            manifest_entry=manifest_entry,
            steps=steps,
            default_model_binding=default_model_binding,
        )


# ---------------------------------------------------------------------------
# LoadedWorkflow — WorkflowObject Protocol-conformant value (U-RT-105)
# ---------------------------------------------------------------------------


class LoadedWorkflow(BaseModel):
    """Frozen Pydantic model satisfying the :class:`WorkflowObject` Protocol.

    Five Pydantic fields directly satisfy the 5-property Protocol surface
    (``workflow_id`` / ``workload_class`` / ``manifest_entry`` / ``steps`` /
    ``default_model_binding``). ``runtime_checkable`` Protocols accept either
    ``@property`` or plain attributes; Pydantic frozen fields satisfy both.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_id: str
    workload_class: WorkloadClass
    manifest_entry: WorkflowManifestEntry
    steps: tuple[WorkflowStep, ...]
    default_model_binding: ModelBinding
