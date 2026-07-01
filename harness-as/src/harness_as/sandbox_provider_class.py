"""Sandbox provider-class enumeration + per-class metadata — U-AS-11.

Implements C-AS-09 §9.2 (closed six-class sandbox-provider taxonomy). Declares
the `SandboxProviderClass` enum, the per-class metadata table, and the
`ClassCardinality` marker.

Authority: Implementation_Plan_Action_Surface_v1.md §2 U-AS-11;
Spec_Action_Surface_v1.md §9.2 C-AS-09; ADR-D2 v1.1 §1.2.

The taxonomy is closed at six classes (acceptance #2) — a seventh requires a
Workflow §4.1.2 Class-2 ADR-D2 revision. Each class is itself `OPEN`
cardinality (acceptance #4): new candidates *within* an existing class are
permitted at deployment-binding time.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict

from harness_as.sandbox_tier import SandboxTier


class SandboxProviderClass(StrEnum):
    """The 6 sandbox provider classes (C-AS-09 §9.2). Cardinality fixed at 6."""

    LANGUAGE_LEVEL = "language-level"
    FILESYSTEM_OVERLAY_WORKTREE = "filesystem-overlay-worktree"
    PROCESS_ULIMIT_BUBBLEWRAP_SEATBELT = "process-ulimit-bubblewrap-seatbelt"
    CONTAINER = "container"
    MICROVM_FIRECRACKER = "microvm-firecracker"
    FULL_VM = "full-vm"


class ClassCardinality(StrEnum):
    """Per-class cardinality marker (C-AS-09 §9.2).

    Every provider class is `OPEN` — new candidates within the class are
    permitted at deployment-binding time without an ADR revision.
    """

    OPEN = "OPEN"


class ProviderClassMetadata(BaseModel):
    """Registered metadata for one sandbox provider class (C-AS-09 §9.2)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_class: SandboxProviderClass
    mechanism_description: str
    tier_mapping: frozenset[SandboxTier]
    """Sandbox tiers the provider class can back (C-AS-09 §9.2 column 4)."""
    cardinality: ClassCardinality


_PROVIDER_CLASS_METADATA: Mapping[SandboxProviderClass, ProviderClassMetadata] = MappingProxyType(
    {
        SandboxProviderClass.LANGUAGE_LEVEL: ProviderClassMetadata(
            provider_class=SandboxProviderClass.LANGUAGE_LEVEL,
            mechanism_description="In-process language sandbox (Pyodide / Starlark / ulimit)",
            tier_mapping=frozenset({SandboxTier.TIER_1_PROCESS, SandboxTier.TIER_2_CONTAINER}),
            cardinality=ClassCardinality.OPEN,
        ),
        SandboxProviderClass.FILESYSTEM_OVERLAY_WORKTREE: ProviderClassMetadata(
            provider_class=SandboxProviderClass.FILESYSTEM_OVERLAY_WORKTREE,
            mechanism_description=(
                "Git-worktree isolation (kilocode pattern); "
                "fuse-overlay / fuse-projfs (oh-my-pi pattern)"
            ),
            tier_mapping=frozenset({SandboxTier.TIER_2_CONTAINER}),
            cardinality=ClassCardinality.OPEN,
        ),
        SandboxProviderClass.PROCESS_ULIMIT_BUBBLEWRAP_SEATBELT: ProviderClassMetadata(
            provider_class=SandboxProviderClass.PROCESS_ULIMIT_BUBBLEWRAP_SEATBELT,
            mechanism_description=(
                "OS-level process isolation with seccomp / namespacing / sandbox-exec"
            ),
            tier_mapping=frozenset({SandboxTier.TIER_2_CONTAINER}),
            cardinality=ClassCardinality.OPEN,
        ),
        SandboxProviderClass.CONTAINER: ProviderClassMetadata(
            provider_class=SandboxProviderClass.CONTAINER,
            mechanism_description=(
                "Shared-kernel container (Docker / Podman) OR user-space kernel "
                "(gVisor) OR microVM-backed container (Kata)"
            ),
            tier_mapping=frozenset({SandboxTier.TIER_3_MICROVM}),
            cardinality=ClassCardinality.OPEN,
        ),
        SandboxProviderClass.MICROVM_FIRECRACKER: ProviderClassMetadata(
            provider_class=SandboxProviderClass.MICROVM_FIRECRACKER,
            mechanism_description="Hardware-virt microVM with KVM",
            tier_mapping=frozenset({SandboxTier.TIER_4_FULL_VM}),
            cardinality=ClassCardinality.OPEN,
        ),
        SandboxProviderClass.FULL_VM: ProviderClassMetadata(
            provider_class=SandboxProviderClass.FULL_VM,
            mechanism_description=("Hardware-virt full VM; ephemeral; network-egress-restricted"),
            tier_mapping=frozenset({SandboxTier.TIER_4_FULL_VM}),
            cardinality=ClassCardinality.OPEN,
        ),
    }
)


def provider_class_metadata(c: SandboxProviderClass) -> ProviderClassMetadata:
    """Return the metadata row for a sandbox provider class (C-AS-09 §9.2).

    Total over `SandboxProviderClass` (acceptance #5).
    """
    return _PROVIDER_CLASS_METADATA[c]
