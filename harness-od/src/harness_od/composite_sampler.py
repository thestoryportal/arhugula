"""C-OD-09 + C-OD-10 composite sampler — OTel SDK `Sampler` subclass.

H_T-OD-3 retirement substrate. Closes the project-authored-sampler gap at
`harness-runtime/lifecycle/tracer_provider.py:_DEFAULT_SAMPLER` (previously
stock `ParentBased(ALWAYS_ON)` per the module docstring's defense-in-depth
deferral) by honoring the OD spec v1.2 §9.2 always-sampled set at the SDK
boundary.

**Composition** (per OTel canonical pattern):

    ParentBased(root=HarnessCompositeSampler(base_rate=...))

ParentBased wrapping preserves trace consistency — children of a sampled
parent always sample; children of an unsampled parent never sample. The
inner `HarnessCompositeSampler` resolves the root-span decision via:

  1. If `name` (+ `attributes`) matches the §9.2 always-sampled set (via
     `is_always_sampled`): return `RECORD_AND_SAMPLE`.
  2. Else: delegate to `TraceIdRatioBased(base_rate)`.

**§9.2 conditional rows (B7 over-sampling refinement).** Four §9.2 rows are
conditional, not name-only:

  - `files.operation` always-sampled only at `kind ∈ {upload, delete}`
  - `memory.operation` always-sampled only at `kind ∈ {write, update, delete}`
  - `validator.fail.*` always-sampled only at `permanence=permanent`
  - `subagent.span` always-sampled only at the root

The first three are resolved by passing `attributes` through to
`is_always_sampled` (the non-mutation / transient complements fall to the
§10.1 `TraceIdRatioBased(base_rate)` branch); `subagent.span` is root-
conditional, delivered by the `ParentBased` composition below. Conservative-
absent: a missing discriminating attribute always-samples (never under-sample
the §9.3 floor). See `sampling_mode._conditional_always_sampled` for the SSOT.

**Enforcement boundary (honest scope).** `ParentBased(root=...)` consults this
inner sampler ONLY for root spans — non-root spans inherit the parent decision
and never reach `should_sample`. The runtime producers (`files_api.py`,
`memory_tool_dispatch.py`) emit `files.operation` / `memory.operation` as
NON-ROOT spans and set `*.kind` AFTER span creation, so the attribute is not
visible to this head sampler at decision time. The §9.2-conditional refinement
here is therefore correct + tested for the root-span + attribute-at-creation
case, but its production effect is bounded: full §9.2-conditional enforcement
for non-root + production-tail spans is a TAIL-keep concern (attributes read at
trace completion), wired by the in-process collector arc (R-420 / R-421);
tracked as the `B-TAIL-CONDITIONAL-SAMPLING` forward arc. `is_always_sampled`
is the SSOT the tail-keep processor will consume there.

**Base-rate sourcing.** Per §10.3 the base-rate envelope is keyed by
(persona_tier × deployment_surface). At HEAD the production binding site
(`lifecycle/tracer_provider.py:compose_tracer_provider`) resolves the per-cell
rate from `PER_CELL_BASE_RATE_ENVELOPE` (U-OD-12) and threads it as
`base_rate`; `build_default_sampler`'s `1.0` default is the standalone /
solo-developer × local-development convenience, not the production value.
"""

from __future__ import annotations

from collections.abc import Sequence

from opentelemetry.context import Context
from opentelemetry.sdk.trace.sampling import (
    Decision,
    ParentBased,
    Sampler,
    SamplingResult,
    TraceIdRatioBased,
)
from opentelemetry.trace import Link, SpanKind
from opentelemetry.trace.span import TraceState
from opentelemetry.util.types import Attributes

from harness_od.sampling_mode import is_always_sampled

__all__ = [
    "HarnessCompositeSampler",
    "build_default_sampler",
]


class HarnessCompositeSampler(Sampler):
    """Composite sampler honoring §9.2 always-sampled set + §10.1 base-rate.

    Designed to sit inside `ParentBased(root=...)` per OTel canonical pattern;
    standalone use samples root spans correctly but does not propagate the
    parent decision to children.
    """

    def __init__(self, base_rate: float = 1.0) -> None:
        if not (0.0 <= base_rate <= 1.0):
            raise ValueError(f"base_rate must be in [0.0, 1.0]; got {base_rate}")
        self._base_rate = base_rate
        self._ratio_sampler = TraceIdRatioBased(base_rate)

    @property
    def base_rate(self) -> float:
        """Per §10.1 base-rate set sampling probability (cell-tunable)."""
        return self._base_rate

    def should_sample(
        self,
        parent_context: Context | None,
        trace_id: int,
        name: str,
        kind: SpanKind | None = None,
        attributes: Attributes = None,
        links: Sequence[Link] | None = None,
        trace_state: TraceState | None = None,
    ) -> SamplingResult:
        if is_always_sampled(name, attributes):
            return SamplingResult(
                decision=Decision.RECORD_AND_SAMPLE,
                attributes=attributes,
                trace_state=trace_state,
            )
        return self._ratio_sampler.should_sample(
            parent_context=parent_context,
            trace_id=trace_id,
            name=name,
            kind=kind,
            attributes=attributes,
            links=links,
            trace_state=trace_state,
        )

    def get_description(self) -> str:
        return (
            f"HarnessCompositeSampler(always_sampled_per_C-OD-09_§9.2, base_rate={self._base_rate})"
        )


def build_default_sampler(base_rate: float = 1.0) -> Sampler:
    """Build the canonical `ParentBased(root=HarnessCompositeSampler(...))`.

    `base_rate` defaults to 1.0 (the §10.3 solo-developer × local-development
    row + standalone convenience). The production binding site
    (`lifecycle/tracer_provider.py:compose_tracer_provider`) already threads the
    per-cell `PER_CELL_BASE_RATE_ENVELOPE` default (U-OD-12) resolved from
    (persona_tier × deployment_surface), so non-default rates are live at HEAD.
    """
    return ParentBased(root=HarnessCompositeSampler(base_rate=base_rate))
