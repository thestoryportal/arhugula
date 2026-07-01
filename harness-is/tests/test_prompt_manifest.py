"""Tests for the prompts-management carrier — `PromptManifest` + `PromptVersion`.

Implements IS spec v1.5 §C-IS-05 §5.2 (third procedural-tier hash-component
carrier; post-MVP closure R-CL-P4). Mirrors the `RoutingManifest` shape
(frozen + `extra="forbid"`); empty-defaultable at the runtime carrier.

R-PM-1 cascade PR #1 (IS spec v1.6 §C-IS-05 §5.2 provenance-tightening) adds
the inline `content` carrier + the `version_sha == prompt_version_sha(content)`
derive-invariant (detect-then-refuse).

R-PM-1 cascade PR #2 (IS spec v1.7 §C-IS-05 §5.3) adds the `versions` authoring
store + its internal-coherence invariants (entries authored + content-addressed-
unique; a non-empty active selection is a store member) + the `from_contents`
authoring builder. The store has no runtime consumer in PR #2 (selection is the
PR #3 CP arc), so these are carrier-coherence unit tests — the appropriate
verification shape for an additive carrier with no behavioral path yet.
"""

from __future__ import annotations

import hashlib

import pytest
from harness_is.prompt_manifest import (
    PromptManifest,
    PromptVersion,
    prompt_version_sha,
)
from pydantic import ValidationError


def test_prompt_version_construct_and_frozen() -> None:
    """`PromptVersion` carries a content-derived `version_sha` and is frozen +
    extra-forbid."""
    pv = PromptVersion.from_content("hello prompt")
    assert pv.content == "hello prompt"
    assert pv.version_sha == prompt_version_sha("hello prompt")
    with pytest.raises(ValidationError):
        pv.version_sha = "mutated"  # type: ignore[misc]  # frozen
    with pytest.raises(ValidationError):
        PromptVersion(version_sha="", unexpected="y")  # type: ignore[call-arg]  # extra-forbid


def test_prompt_version_sha_helper_empty_is_sentinel() -> None:
    """`prompt_version_sha("")` is the empty-carrier sentinel; non-empty is a
    hex SHA-256 digest of the content."""
    assert prompt_version_sha("") == ""
    expected = hashlib.sha256(b"some content").hexdigest()
    assert prompt_version_sha("some content") == expected


def test_prompt_version_derive_invariant_rejects_mismatched_sha() -> None:
    """The `version_sha == digest(content)` invariant is enforced at
    construction (detect-then-refuse) — a non-empty content with a wrong sha
    raises, and a non-empty sha with no content raises (the #496 identity-only
    pattern is superseded)."""
    with pytest.raises(ValidationError):
        PromptVersion(version_sha="not-the-digest", content="real content")
    with pytest.raises(ValidationError):
        PromptVersion(version_sha="orphan-sha", content="")


def test_prompt_version_content_only_derives_sha() -> None:
    """The operator-supplied declarative path (Codex review): supplying only
    ``content`` (e.g. from TOML/JSON `RuntimeConfig.prompt_manifest`) derives
    ``version_sha`` — the operator does not precompute the digest."""
    pv = PromptVersion(content="declarative prompt body")
    assert pv.version_sha == prompt_version_sha("declarative prompt body")
    assert pv.content == "declarative prompt body"
    # Mirrors the explicit from_content helper.
    assert pv == PromptVersion.from_content("declarative prompt body")
    # Empty / fully-defaulted construction stays the empty sentinel.
    assert PromptVersion().version_sha == ""
    assert PromptVersion(content="").version_sha == ""


def test_prompt_version_empty_carrier_sentinel() -> None:
    """The empty-carrier shape (`version_sha=""`, `content=""`) is the
    no-active-prompt default and satisfies the invariant."""
    pv = PromptVersion(version_sha="")
    assert pv.version_sha == ""
    assert pv.content == ""
    assert PromptVersion.from_content("") == pv


def test_prompt_manifest_construct_and_frozen() -> None:
    """`PromptManifest` mirrors `RoutingManifest` (frozen + extra-forbid)."""
    pm = PromptManifest(
        manifest_version=1,
        active_prompt_version=PromptVersion.from_content("v-content"),
    )
    assert pm.manifest_version == 1
    assert pm.active_prompt_version.content == "v-content"
    with pytest.raises(ValidationError):
        pm.manifest_version = 2  # type: ignore[misc]  # frozen
    with pytest.raises(ValidationError):
        PromptManifest(  # type: ignore[call-arg]  # extra-forbid
            manifest_version=1,
            active_prompt_version=PromptVersion(version_sha=""),
            extra="nope",
        )


def test_prompt_manifest_empty_carrier_sentinel() -> None:
    """The empty-carrier shape (`version_sha=""`) is the no-active-prompt default."""
    empty = PromptManifest(
        manifest_version=1,
        active_prompt_version=PromptVersion(version_sha=""),
    )
    assert empty.active_prompt_version.version_sha == ""


def test_prompt_manifest_equality_by_value() -> None:
    """Two logically-identical manifests compare equal (value semantics for the
    resolver's cross-instance determinism)."""
    a = PromptManifest(manifest_version=1, active_prompt_version=PromptVersion.from_content("s"))
    b = PromptManifest(manifest_version=1, active_prompt_version=PromptVersion.from_content("s"))
    assert a == b


# --- R-PM-1 PR #2: the `versions` authoring store (IS spec v1.7 §5.3) ---------


def test_prompt_manifest_versions_default_empty_preserves_pr1_behavior() -> None:
    """Default `versions=()` preserves the #496/PR-#1 behavior verbatim: the
    active version stands alone with no store and no membership obligation."""
    pm = PromptManifest(
        manifest_version=1,
        active_prompt_version=PromptVersion.from_content("inline-only"),
    )
    assert pm.versions == ()
    # An inline active with no store is valid (the #506 config shape).
    assert pm.active_prompt_version.content == "inline-only"


def test_prompt_manifest_versions_store_is_content_addressed() -> None:
    """A multi-version store carries content-addressed versions; each entry's
    `version_sha` is the digest of its content (PR #1 derive-invariant per entry)."""
    pm = PromptManifest(
        manifest_version=1,
        active_prompt_version=PromptVersion(version_sha=""),
        versions=(
            PromptVersion.from_content("system prompt A"),
            PromptVersion.from_content("system prompt B"),
        ),
    )
    assert len(pm.versions) == 2
    assert {v.version_sha for v in pm.versions} == {
        prompt_version_sha("system prompt A"),
        prompt_version_sha("system prompt B"),
    }


def test_prompt_manifest_versions_rejects_empty_sentinel_in_store() -> None:
    """The empty-carrier sentinel (`version_sha=""`) is the no-active-prompt marker,
    not a stored authored version — refused in the store."""
    with pytest.raises(ValidationError, match="only authored versions"):
        PromptManifest(
            manifest_version=1,
            active_prompt_version=PromptVersion(version_sha=""),
            versions=(PromptVersion.from_content("real"), PromptVersion(version_sha="")),
        )


def test_prompt_manifest_versions_rejects_duplicate_content() -> None:
    """Content-addressed uniqueness: duplicate content is one version, so two
    entries with the same `version_sha` are refused."""
    dup = PromptVersion.from_content("same body")
    with pytest.raises(ValidationError, match="content-addressed-unique"):
        PromptManifest(
            manifest_version=1,
            active_prompt_version=PromptVersion(version_sha=""),
            versions=(dup, PromptVersion.from_content("same body")),
        )


def test_prompt_manifest_active_must_be_member_of_store() -> None:
    """A non-empty active selection must be an authored member of a non-empty store."""
    authored = PromptVersion.from_content("authored body")
    # Member → valid.
    pm = PromptManifest(
        manifest_version=1,
        active_prompt_version=PromptVersion.from_content("authored body"),
        versions=(authored, PromptVersion.from_content("other body")),
    )
    assert pm.active_prompt_version.version_sha == authored.version_sha
    # Non-member → refused.
    with pytest.raises(ValidationError, match="must be a member of versions"):
        PromptManifest(
            manifest_version=1,
            active_prompt_version=PromptVersion.from_content("unauthored body"),
            versions=(authored,),
        )


def test_prompt_manifest_authored_but_none_selected() -> None:
    """A non-empty store with an empty active selection is the authored-but-none-
    selected state (selection is the PR #3 CP arc) — valid, no membership check."""
    pm = PromptManifest(
        manifest_version=1,
        active_prompt_version=PromptVersion(version_sha=""),
        versions=(PromptVersion.from_content("a"), PromptVersion.from_content("b")),
    )
    assert pm.active_prompt_version.version_sha == ""
    assert len(pm.versions) == 2


def test_prompt_manifest_versions_is_frozen() -> None:
    """`versions` is part of the frozen carrier (mirrors RoutingManifest immutability)."""
    pm = PromptManifest(
        manifest_version=1,
        active_prompt_version=PromptVersion(version_sha=""),
        versions=(PromptVersion.from_content("x"),),
    )
    with pytest.raises(ValidationError):
        pm.versions = ()  # type: ignore[misc]  # frozen


def test_prompt_manifest_from_contents_authoring_helper() -> None:
    """`from_contents` content-addresses a store and selects an active member."""
    pm = PromptManifest.from_contents(
        manifest_version=2,
        contents=["prompt one", "prompt two", "prompt three"],
        active="prompt two",
    )
    assert pm.manifest_version == 2
    assert len(pm.versions) == 3
    assert pm.active_prompt_version.version_sha == prompt_version_sha("prompt two")
    assert pm.active_prompt_version.content == "prompt two"
    assert pm.active_prompt_version.version_sha in {v.version_sha for v in pm.versions}


def test_prompt_manifest_from_contents_none_active_is_unselected() -> None:
    """`from_contents(active=None)` authors a store with no active selection."""
    pm = PromptManifest.from_contents(
        manifest_version=1,
        contents=["only prompt"],
        active=None,
    )
    assert pm.active_prompt_version.version_sha == ""
    assert len(pm.versions) == 1


def test_prompt_manifest_from_contents_active_not_in_contents_refused() -> None:
    """`from_contents` with an `active` outside `contents` is refused by the
    builder's explicit `active ∈ contents` guard (you cannot activate an
    unauthored version)."""
    with pytest.raises(ValueError, match="active must be one of contents"):
        PromptManifest.from_contents(
            manifest_version=1,
            contents=["authored"],
            active="never authored",
        )


def test_prompt_manifest_from_contents_empty_contents_with_active_refused() -> None:
    """The empty-`contents` builder edge (adversarial F-01): an `active` cannot
    slip through via the empty-store short-circuit — the builder's explicit guard
    catches it even when the manifest validator's membership check would not run."""
    with pytest.raises(ValueError, match="active must be one of contents"):
        PromptManifest.from_contents(manifest_version=1, contents=[], active="foo")


def test_prompt_manifest_from_contents_empty_string_active_refused() -> None:
    """`active=""` is not the no-selection sentinel (that is `active=None`); an
    empty string is content-addressed and must be a member — but empty content is
    never an authored version, so it is refused (adversarial F-03)."""
    with pytest.raises(ValueError, match="active must be one of contents"):
        PromptManifest.from_contents(manifest_version=1, contents=["real"], active="")
