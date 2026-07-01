"""U-RT-23 — fallback chain materialization + advancement tests.

ACs per Phase 2 Session 3 Track A atomic decomposition §L5 U-RT-23:
  #1 degenerate (all-down) case surfaces typed
     -> test_advance_or_raise_at_terminal_raises_exhausted
     -> test_advance_or_raise_at_single_primary_chain_raises_exhausted
     -> test_exhausted_error_carries_failed_candidate
     -> test_empty_manifest_chains_raises_bind_error
  #2 downgrade rule auditable
     -> test_stage_surfaces_default_downgrade_rule
     -> test_downgrade_rule_carries_audit_fields

Test convention notes:
- Fallback chains are constructed via `compose_fallback_chain` (the CP
  landed constructor); the runtime tests don't re-test CP advancement
  logic — that lives at `harness-cp/tests/test_cross_family_fallback_chain.py`.
- Cross-family attribution flags are spot-checked at one transition (anthropic
  → openai); exhaustive flag coverage stays at the CP test suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from harness_as import BlastRadiusTier
from harness_core import DeploymentSurface
from harness_cp.cross_family_fallback_chain import (
    FallbackChain,
    ProviderCandidate,
    ProviderFamily,
    compose_fallback_chain,
)
from harness_cp.default_downgrade_rule import DEFAULT_DOWNGRADE_RULE
from harness_cp.routing_manifest_residence import RoutingManifest
from harness_cp.topology_pattern import TopologyPattern
from harness_runtime.lifecycle.fallback_chain import (
    FallbackChainBindError,
    FallbackChainExhaustedError,
    FallbackChainStage,
    advance_or_raise,
    materialize_fallback_chain_stage,
)
from harness_runtime.types import (
    CollectorConfig,
    OTelConfig,
    PathBindingConfig,
    ProviderSecretsConfig,
    RuntimeConfig,
)

# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


def _candidate(provider: str, model: str, family: ProviderFamily) -> ProviderCandidate:
    return ProviderCandidate(provider=provider, model=model, family=family)


_ANTHROPIC_OPUS = _candidate("anthropic", "opus", ProviderFamily.ANTHROPIC)
_ANTHROPIC_SONNET = _candidate("anthropic", "sonnet", ProviderFamily.ANTHROPIC)
_OPENAI_GPT5 = _candidate("openai", "gpt-5", ProviderFamily.OPENAI)
_OPENAI_GPT4 = _candidate("openai", "gpt-4", ProviderFamily.OPENAI)
_OLLAMA_LLAMA = _candidate("ollama", "llama", ProviderFamily.LOCAL_OPEN_WEIGHT)


def _full_chain() -> FallbackChain:
    """A populated chain — primary + same-family + cross-family + terminal."""
    return compose_fallback_chain(
        primary=_ANTHROPIC_OPUS,
        same_family=(_ANTHROPIC_SONNET,),
        cross_family=(_OPENAI_GPT5, _OPENAI_GPT4),
        terminal=_OLLAMA_LLAMA,
    )


def _single_primary_chain() -> FallbackChain:
    """A degenerate chain — primary only; no successors anywhere."""
    return compose_fallback_chain(
        primary=_ANTHROPIC_OPUS,
        same_family=(),
        cross_family=(),
        terminal=None,
    )


def _manifest_with_chain(chain: FallbackChain) -> RoutingManifest:
    return RoutingManifest(
        manifest_version=1,
        per_role_bindings={},
        per_workload_overrides={},
        fallback_chains=(chain,),
        retry_policies={},
    )


def _config(
    tmp_path: Path,
    *,
    manifest: RoutingManifest | None = None,
) -> RuntimeConfig:
    if manifest is None:
        return RuntimeConfig(
            deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
            repository_root=tmp_path,
            path_bindings=PathBindingConfig(),
            provider_secrets=ProviderSecretsConfig(),
            otel=OTelConfig(otlp_endpoint="http://localhost:4317"),
            collector=CollectorConfig(),
            default_topology=TopologyPattern.SINGLE_THREADED_LINEAR,
        )
    return RuntimeConfig(
        deployment_surface=DeploymentSurface.LOCAL_DEVELOPMENT,
        repository_root=tmp_path,
        path_bindings=PathBindingConfig(),
        provider_secrets=ProviderSecretsConfig(),
        otel=OTelConfig(otlp_endpoint="http://localhost:4317"),
        collector=CollectorConfig(),
        default_topology=TopologyPattern.SINGLE_THREADED_LINEAR,
        routing_manifest=manifest,
    )


# ---------------------------------------------------------------------------
# materialize_fallback_chain_stage — happy path + bind-failure path.
# ---------------------------------------------------------------------------


def test_materialize_binds_first_manifest_chain(tmp_path: Path) -> None:
    """The first entry in `manifest.fallback_chains` becomes the stage chain."""
    chain = _full_chain()
    stage = materialize_fallback_chain_stage(
        _config(tmp_path, manifest=_manifest_with_chain(chain))
    )
    assert isinstance(stage, FallbackChainStage)
    assert stage.chain is chain


def test_empty_manifest_chains_raises_bind_error(tmp_path: Path) -> None:
    """When the manifest carries no fallback chain, materialization raises
    `FallbackChainBindError` at bootstrap (the default empty manifest has
    `fallback_chains=()`). AC #1 boundary — typed at bootstrap."""
    with pytest.raises(FallbackChainBindError):
        materialize_fallback_chain_stage(_config(tmp_path))


# ---------------------------------------------------------------------------
# AC #1 — degenerate (all-down) case surfaces typed.
# ---------------------------------------------------------------------------


def test_advance_or_raise_returns_next_candidate(tmp_path: Path) -> None:
    """Within-chain advancement returns the next candidate + the §4.3
    OnFailureResult attribution flags."""
    chain = _full_chain()
    nxt, result = advance_or_raise(chain, _ANTHROPIC_OPUS)
    assert nxt == _ANTHROPIC_SONNET
    assert result.cross_family_triggered is False
    assert result.cache_state_lost is False


def test_advance_or_raise_marks_cross_family_at_boundary(tmp_path: Path) -> None:
    """Crossing the family boundary (anthropic → openai) sets the §4.3
    attribution flags."""
    chain = _full_chain()
    nxt, result = advance_or_raise(chain, _ANTHROPIC_SONNET)
    assert nxt == _OPENAI_GPT5
    assert result.cross_family_triggered is True
    assert result.cache_state_lost is True


def test_advance_or_raise_at_terminal_raises_exhausted(tmp_path: Path) -> None:
    """Advancement past the terminal candidate raises
    `FallbackChainExhaustedError`. AC #1."""
    chain = _full_chain()
    with pytest.raises(FallbackChainExhaustedError):
        advance_or_raise(chain, _OLLAMA_LLAMA)


def test_advance_or_raise_at_single_primary_chain_raises_exhausted(
    tmp_path: Path,
) -> None:
    """A primary-only chain exhausts at the first failure. AC #1 — the
    all-down case is byte-1 of the traversal."""
    chain = _single_primary_chain()
    with pytest.raises(FallbackChainExhaustedError):
        advance_or_raise(chain, _ANTHROPIC_OPUS)


def test_exhausted_error_carries_failed_candidate(tmp_path: Path) -> None:
    """The exhausted-error carries the `failed` candidate so the audit-ledger
    consumer can attribute the `fallback.exhausted` event."""
    chain = _single_primary_chain()
    with pytest.raises(FallbackChainExhaustedError) as excinfo:
        advance_or_raise(chain, _ANTHROPIC_OPUS)
    assert excinfo.value.failed == _ANTHROPIC_OPUS


def test_advance_or_raise_with_off_chain_candidate_raises_exhausted(
    tmp_path: Path,
) -> None:
    """A `failed` candidate not in the chain has no successor (per
    `on_provider_failure`'s ValueError-handling branch) — exhausted."""
    chain = _full_chain()
    off_chain = _candidate("google", "gemini", ProviderFamily.GOOGLE)
    with pytest.raises(FallbackChainExhaustedError):
        advance_or_raise(chain, off_chain)


# ---------------------------------------------------------------------------
# AC #2 — downgrade rule auditable.
# ---------------------------------------------------------------------------


def test_stage_surfaces_default_downgrade_rule(tmp_path: Path) -> None:
    """`FallbackChainStage.downgrade_rule` is the canonical
    `DEFAULT_DOWNGRADE_RULE` (identity-equal so an audit consumer can match
    via `is`). AC #2."""
    chain = _full_chain()
    stage = materialize_fallback_chain_stage(
        _config(tmp_path, manifest=_manifest_with_chain(chain))
    )
    assert stage.downgrade_rule is DEFAULT_DOWNGRADE_RULE


def test_downgrade_rule_carries_audit_fields(tmp_path: Path) -> None:
    """The downgrade rule exposes the three fields required for audit:
    `parent_blast_radius` (Tier-3), `child_ceiling` (Tier-1 READ_ONLY), and
    `rationale` (C-CP-12 §12.1 narrative). AC #2."""
    chain = _full_chain()
    stage = materialize_fallback_chain_stage(
        _config(tmp_path, manifest=_manifest_with_chain(chain))
    )
    assert stage.downgrade_rule.parent_blast_radius is BlastRadiusTier.EXTERNAL_REVERSIBLE
    assert stage.downgrade_rule.child_ceiling is BlastRadiusTier.READ_ONLY
    assert "C-CP-12 §12.1" in stage.downgrade_rule.rationale


def test_stage_is_frozen(tmp_path: Path) -> None:
    """`FallbackChainStage` is a frozen dataclass — mutation rejected."""
    from dataclasses import FrozenInstanceError

    chain = _full_chain()
    stage = materialize_fallback_chain_stage(
        _config(tmp_path, manifest=_manifest_with_chain(chain))
    )
    with pytest.raises(FrozenInstanceError):
        stage.chain = chain  # type: ignore[misc]
