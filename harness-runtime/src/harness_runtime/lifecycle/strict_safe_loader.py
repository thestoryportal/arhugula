"""Strict YAML SafeLoader for harness manifest parsing.

Authority: C-RT-30 WorkflowManifestLoader.

Per runtime spec v1.39 §14.19 Reading (A) re-litigation of Q-H=b (Phase 2a
G2): replaces `strictyaml.dirty_load` at the WorkflowManifestLoader. The
strictness features the original Q-H=b decision selected for (closed schema,
no anchors/aliases, no flow style, duplicate-key detection) are preserved
via a thin `yaml.SafeLoader` subclass. The behavior that Q-H=b did NOT
consider — strictyaml's `dirty_load` stringifies every scalar — is replaced
with YAML 1.1's native scalar typing (int / float / bool / null / str).

Bans:
  - duplicate mapping keys (manifest correctness; strictyaml ban preserved)
  - non-empty flow-style mappings + sequences (strictyaml ban preserved;
    empty `{}` / `[]` allowed since they are the only practical way to
    express empty mapping / sequence in YAML)
  - anchors (&foo) + aliases (*foo) (strictyaml ban preserved)

YAML 1.1 booleans for ambiguous scalars (yes / no / on / off / true / false)
are accepted per native pyyaml SafeLoader behavior. Operators wanting a
string-typed identifier whose value parses as a bool should quote per YAML
1.2 native rule (`tenant_id: "yes"` preserves str; `tenant_id: yes` becomes
bool). Documented at runtime spec v1.39 §14.19 operator-facing guidance.

Closes use-the-product probe finding #16/#17 (PR #79): strictyaml.dirty_load
stringified `max_tokens: 8` → `"8"`; Anthropic SDK rejected. v1.39 absorbs
the gap structurally — YAML and TOML manifests now reach the SDK with
identical native-typed shapes.
"""

from __future__ import annotations

# This module is a thin subclass over pyyaml's loosely-typed Loader internals
# (`construct_object` / `check_event` / `peek_event` return untyped values, and
# the overridden constructors receive already-typed node args). Suppress the
# yaml-boundary "unknown"/"unnecessary-isinstance" noise file-wide; real issues
# surface via the other strict rules, which stay enabled.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnnecessaryIsInstance=false
import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode, SequenceNode


class StrictSafeLoader(yaml.SafeLoader):
    """SafeLoader with manifest-safety bans; preserves native scalar typing."""

    def construct_mapping(self, node, deep=False):  # type: ignore[override]
        if not isinstance(node, MappingNode):
            raise ConstructorError(
                None,
                None,
                f"expected a mapping node, got {type(node).__name__}",
                node.start_mark,
            )
        if node.flow_style and node.value:
            # Empty flow mapping `{}` is the canonical YAML way to express an
            # empty mapping; allowed. Non-empty flow style mapping is banned.
            raise ConstructorError(
                None,
                None,
                "non-empty flow-style mappings not permitted in harness manifests",
                node.start_mark,
            )
        seen: set[object] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=True)
            if key in seen:
                raise ConstructorError(
                    None,
                    None,
                    f"duplicate mapping key: {key!r}",
                    key_node.start_mark,
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)

    def construct_sequence(self, node, deep=False):  # type: ignore[override]
        if isinstance(node, SequenceNode) and node.flow_style and node.value:
            # Empty flow sequence `[]` is the canonical YAML empty sequence;
            # allowed. Non-empty flow sequence is banned.
            raise ConstructorError(
                None,
                None,
                "non-empty flow-style sequences not permitted in harness manifests",
                node.start_mark,
            )
        return super().construct_sequence(node, deep=deep)

    def compose_node(self, parent, index):  # type: ignore[override]
        from yaml.events import AliasEvent

        if self.check_event(AliasEvent):
            event = self.peek_event()
            raise ConstructorError(
                None,
                None,
                "anchors/aliases not permitted in harness manifests",
                event.start_mark,
            )
        node = super().compose_node(parent, index)
        if node is not None and getattr(node, "anchor", None):
            raise ConstructorError(
                None,
                None,
                "anchors not permitted in harness manifests",
                node.start_mark,
            )
        return node


def strict_safe_load(stream: str | bytes) -> object:
    """Load a single YAML document under :class:`StrictSafeLoader`.

    Raises :exc:`yaml.YAMLError` (or subclass) on parse failure; callers at
    the WorkflowManifestLoader translate to typed
    :class:`ManifestParseError`.
    """
    return yaml.load(stream, Loader=StrictSafeLoader)
