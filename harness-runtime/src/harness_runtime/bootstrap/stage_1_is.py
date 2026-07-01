"""Stage 1 IS — path registry, state ledger, shadow_git, index + cache.

Per `Spec_Harness_Runtime_v1.md` v1.1 §2 stage 1 post-conditions:
`ctx.path_resolver`, `ctx.worktree_manager`, `ctx.shadow_git`, `ctx.ledger_writer`,
`ctx.index`, `ctx.cache` all non-None; ledger chain reattached and verified.

Composer call sequence (DAG-ordered):
1. `materialize_path_registry(config.path_bindings, workflow_class, deployment_surface)`
2. `materialize_state_ledger(resolver, workflow_class, deployment_surface, actor)`
3. `materialize_isolation_stage(repository_root, worktree_base, opt_ins, ledger_writer)`
4. `materialize_index_cache(repository_root / '.harness' / 'index.json')`

Step 4's index path is a runtime convention (no `PathClass.INDEX`); the path
class registry's 4 members are SKILLS / PROMPTS / ROUTING_MANIFEST / STATE_LEDGER
per C-IS-01 §1.

**Class 1 fork — STATE_LEDGER path treatment (filed inline at U-RT-43 landing).**
`materialize_path_registry` (U-RT-10) creates EVERY resolved path as a
DIRECTORY via `Path.mkdir(parents=True, exist_ok=True)`. But
`materialize_state_ledger` (U-RT-12) → `initialize_jsonl_event_ledger`
expects the STATE_LEDGER path to be a FILE (writes `.jsonl` content). At
integration these two composers conflict: path_registry produces a dir
where state_ledger expects a file.

Workaround at U-RT-43: between steps 1 and 2, if the STATE_LEDGER
resolved path exists as an empty directory, remove it so state_ledger's
own touch logic can create the file. Class 1 file at
`.harness/class_1_tension_u_rt_43_state_ledger_path_dir_vs_file.md`;
resolution candidates:
- (A) IS spec amendment: STATE_LEDGER resolves to a DIRECTORY containing
  `state.jsonl` (state_ledger composer appends `/state.jsonl`); least
  schema change.
- (B) `materialize_path_registry` differentiates file-class vs dir-class
  members of PathClass; state_ledger remains file-typed.
- (C) Status quo + this workaround documented as canonical.
"""

from __future__ import annotations

from harness_core.workload_class import WorkloadClass

from harness_runtime.bootstrap.mutable_context import _MutableHarnessContext
from harness_runtime.lifecycle.index_cache import materialize_index_cache
from harness_runtime.lifecycle.path_registry import materialize_path_registry
from harness_runtime.lifecycle.shadow_git import materialize_isolation_stage
from harness_runtime.lifecycle.state_ledger import (
    materialize_state_ledger,
    materialize_state_ledger_reader,
)
from harness_runtime.types import RuntimeConfig

__all__ = ["execute"]


async def execute(
    ctx: _MutableHarnessContext,
    config: RuntimeConfig,
    workload_class: WorkloadClass,
) -> None:
    """Populate stage 1 IS fields on `ctx`."""
    assert ctx.config is not None, "stage 0 PREAMBLE must precede stage 1 IS"
    assert ctx.actor is not None, "stage 0 must construct ctx.actor"

    # 1. Path registry.
    registry = materialize_path_registry(
        config.path_bindings,
        workflow_class=workload_class,
        deployment_surface=config.deployment_surface,
    )
    ctx.path_resolver = registry.resolver

    # 2. State ledger writer (depends on path resolver).
    # Per IS spec v1.3 §1 amendment (2026-05-20): STATE_LEDGER + ROUTING_MANIFEST
    # PathClass members resolve to DIRECTORIES; the JSONL ledger lives at
    # `<dir>/state.jsonl`, manifest at `<dir>/routing.manifest.json`. The
    # earlier rmdir workaround (resolving fork-state-ledger-path-dir-vs-file
    # OPEN) is removed. path_registry's mkdir is now consistent with the
    # composer expectations.
    ctx.ledger_writer = materialize_state_ledger(
        registry.resolver,
        workflow_class=workload_class,
        deployment_surface=config.deployment_surface,
        actor=ctx.actor,
    )

    # 2.5. Ledger reader (CP plan v2.12 §0.5 — resolves
    # `[[fork-u-cp-56-resumption-underspec]]`). Reader shares the writer's
    # handle so it always sees the writer's latest appends.
    ctx.ledger_reader = materialize_state_ledger_reader(ctx.ledger_writer)

    # 3. Worktree isolation + shadow-Git supervisor.
    isolation = materialize_isolation_stage(
        repository_root=config.repository_root,
        worktree_base=config.repository_root / ".harness" / "worktrees",
        opt_ins=config.path_bindings.opt_ins,
        ledger_writer=ctx.ledger_writer,
    )
    ctx.worktree_manager = isolation.worktree_manager
    ctx.shadow_git = isolation.shadow_git

    # 4. Index + semantic cache (runtime-convention path under .harness/).
    index_path = config.repository_root / ".harness" / "index.json"
    index_stage = materialize_index_cache(index_path)
    ctx.index = index_stage.index
    ctx.cache = index_stage.cache
