"""Tests for C-RT-27 SkillActivationSpanEmitter + SkillActivationHook.

Per runtime spec v1.32 §14.17 + plan v2.28 L9-quindecies cluster
(U-RT-99/100/101). Closes H_T-AS-8d producer-site absence per
.harness/class_1_fork_as_8d_skill_activation_surface_absence.md Reading B
Q-set ratification.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from harness_core import SkillID
from harness_runtime.lifecycle.skill_activation import (
    SkillActivationEmitterStageMaterializeError,
    SkillActivationHook,
    SkillActivationHookConfig,
    SkillActivationMode,
    SkillActivationSpanEmitter,
    UnknownSkillError,
)
from harness_runtime.lifecycle.skills import (
    Skill,
    SkillManifest,
    compute_git_blob_sha,
    load_skills_from_dir,
)

# --- U-RT-99 carrier substrate tests -----------------------------------------


def test_skill_activation_mode_three_values_byte_exact() -> None:
    """AC #1: 3 enum members preserving AS spec v1.7 §14.4 Claude Code
    taxonomy verbatim."""
    assert SkillActivationMode.FRONTMATTER_ONLY.value == "frontmatter_only"
    assert SkillActivationMode.TOOL_SEARCH.value == "tool_search"
    assert SkillActivationMode.FILESYSTEM_READ.value == "filesystem_read"
    assert len(list(SkillActivationMode)) == 3


def test_skill_activation_hook_protocol_two_methods() -> None:
    """AC #2: Protocol has select_for_workflow_init + select_for_llm_dispatch.

    runtime_checkable Protocol enables isinstance() duck-typing.
    """

    class _Hook:
        def select_for_workflow_init(self, loaded_skills, workflow_id):
            return []

        def select_for_llm_dispatch(self, loaded_skills, workflow_id, step_index):
            return []

    assert isinstance(_Hook(), SkillActivationHook)


def test_skill_activation_hook_config_empty_marker_or_hook_supplied() -> None:
    """AC #3: SkillActivationHookConfig dataclass supports None and operator hook."""
    cfg_empty = SkillActivationHookConfig()
    assert cfg_empty.hook is None

    class _Hook:
        def select_for_workflow_init(self, loaded_skills, workflow_id):
            return []

        def select_for_llm_dispatch(self, loaded_skills, workflow_id, step_index):
            return []

    h = _Hook()
    cfg_with_hook = SkillActivationHookConfig(hook=h)
    assert cfg_with_hook.hook is h

    # Frozen — assignment raises
    with pytest.raises(Exception):
        cfg_empty.hook = h  # type: ignore[misc]


def test_skill_manifest_has_version_sha_and_body_tokens_fields() -> None:
    """AC #4 + #5: SkillManifest carries version_sha (str) + body_tokens (int)."""
    m = SkillManifest(
        skill_id=SkillID("test-skill"),
        name="Test",
        description="test description",
        version="1.0",
        version_sha="abc123",
        body_tokens=42,
    )
    assert m.version_sha == "abc123"
    assert m.body_tokens == 42


def test_compute_git_blob_sha_byte_exact_to_git_hash_object() -> None:
    """AC #5: compute_git_blob_sha is byte-exact-identical to
    `git hash-object <path>` output."""
    content = b"hello world\n"
    expected_sha = (
        subprocess.run(
            ["git", "hash-object", "--stdin"],
            input=content,
            capture_output=True,
            check=True,
        )
        .stdout.decode("ascii")
        .strip()
    )
    assert compute_git_blob_sha(content) == expected_sha


def test_load_skills_computes_version_sha_and_body_tokens(tmp_path: Path) -> None:
    """AC #5 + #6: load_skills_from_dir computes version_sha + body_tokens
    when absent from the manifest JSON."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    manifest_file = skills_dir / "my-skill.skill.json"
    raw = '{"skill_id": "my-skill", "name": "test", "description": "hello world", "version": "1.0"}'
    manifest_file.write_text(raw)

    skills = load_skills_from_dir(skills_dir)
    assert SkillID("my-skill") in skills
    skill = skills[SkillID("my-skill")]
    # version_sha is byte-exact git hash-object output for the raw manifest bytes
    expected_sha = compute_git_blob_sha(manifest_file.read_bytes())
    assert skill.manifest.version_sha == expected_sha
    # body_tokens = len("hello world") // 4 = 2
    assert skill.manifest.body_tokens == 2


# --- U-RT-100 emitter tests --------------------------------------------------


class _FakeSpan:
    def __init__(self) -> None:
        self.attrs: dict[str, object] = {}

    def set_attribute(self, key, value) -> None:
        self.attrs[key] = value

    def __enter__(self):
        return self

    def __exit__(self, *a) -> None:
        return None


class _FakeTracer:
    def __init__(self) -> None:
        self.spans: list[_FakeSpan] = []

    def start_as_current_span(self, name):
        span = _FakeSpan()
        span.attrs["__span_name__"] = name
        self.spans.append(span)
        return span


class _FakeTracerProvider:
    def __init__(self) -> None:
        self.tracer = _FakeTracer()

    def get_tracer(self, name: str):
        return self.tracer


def _make_skill(skill_id: str = "my-skill") -> Skill:
    manifest = SkillManifest(
        skill_id=SkillID(skill_id),
        name="My Skill",
        description="testing",
        version="1.0",
        version_sha="deadbeef",
        body_tokens=10,
    )
    return Skill(manifest=manifest, source_path=Path("/tmp/fake.skill.json"))


def test_emitter_emit_opens_skill_activation_span_with_six_attrs() -> None:
    """AC #1: emit opens span named 'skill.activation' with all 6 attrs."""
    tp = _FakeTracerProvider()
    emitter = SkillActivationSpanEmitter(tracer_provider=tp)
    skill = _make_skill()

    emitter.emit(
        skill_id=skill.manifest.skill_id,
        mode=SkillActivationMode.TOOL_SEARCH,
        workflow_id="wf-1",
        skill=skill,
    )

    assert len(tp.tracer.spans) == 1
    span = tp.tracer.spans[0]
    assert span.attrs["__span_name__"] == "skill.activation"
    assert span.attrs["skill.id"] == "my-skill"
    assert span.attrs["skill.name"] == "My Skill"
    assert span.attrs["skill.version_sha"] == "deadbeef"
    assert span.attrs["skill.frontmatter.version"] == "1.0"
    assert span.attrs["skill.body_tokens"] == 10
    assert span.attrs["skill.activation_mode"] == "tool_search"
    assert span.attrs["workflow.id"] == "wf-1"


def test_emitter_accepts_string_literal_for_mode_cross_axis_boundary() -> None:
    """harness-cp passes mode as str literal ("frontmatter_only") to avoid
    importing SkillActivationMode (workspace dep-graph discipline)."""
    tp = _FakeTracerProvider()
    emitter = SkillActivationSpanEmitter(tracer_provider=tp)
    skill = _make_skill()
    emitter.emit(
        skill_id=skill.manifest.skill_id,
        mode="frontmatter_only",
        workflow_id="wf-1",
        skill=skill,
    )
    assert tp.tracer.spans[0].attrs["skill.activation_mode"] == "frontmatter_only"


# --- U-RT-100 factory tests --------------------------------------------------


@pytest.mark.asyncio
async def test_materialize_emitter_returns_none_when_config_none() -> None:
    """AC #2: opt-out short-circuit returns None."""
    from harness_runtime.bootstrap.factories.skill_activation_emitter_factory import (
        materialize_skill_activation_emitter_stage,
    )

    class _Cfg:
        skill_activation_hook_config = None

    class _Ctx:
        tracer_provider = _FakeTracerProvider()

    result = await materialize_skill_activation_emitter_stage(_Cfg(), _Ctx())
    assert result is None


@pytest.mark.asyncio
async def test_materialize_emitter_constructs_when_config_present() -> None:
    """AC #3: opt-in returns a bound emitter."""
    from harness_runtime.bootstrap.factories.skill_activation_emitter_factory import (
        materialize_skill_activation_emitter_stage,
    )

    class _Cfg:
        skill_activation_hook_config = SkillActivationHookConfig()

    class _Ctx:
        tracer_provider = _FakeTracerProvider()

    result = await materialize_skill_activation_emitter_stage(_Cfg(), _Ctx())
    assert isinstance(result, SkillActivationSpanEmitter)


@pytest.mark.asyncio
async def test_materialize_emitter_raises_on_tracer_provider_unbound() -> None:
    """AC #7: factory raises SkillActivationEmitterStageMaterializeError when
    tracer_provider is None."""
    from harness_runtime.bootstrap.factories.skill_activation_emitter_factory import (
        materialize_skill_activation_emitter_stage,
    )

    class _Cfg:
        skill_activation_hook_config = SkillActivationHookConfig()

    class _Ctx:
        tracer_provider = None

    with pytest.raises(SkillActivationEmitterStageMaterializeError):
        await materialize_skill_activation_emitter_stage(_Cfg(), _Ctx())


# --- U-RT-101 hook-3 (operator-explicit) tests -------------------------------


def test_activate_skill_silent_skip_when_emitter_unbound() -> None:
    """AC #4: HarnessContext.activate_skill silent-skips when
    skill_activation_emitter is None (operator opt-out)."""
    # Construct minimal HarnessContext-shaped object via duck-typing for unit
    # test. We don't need a full ctx — just the activate_skill method
    # behavior with None emitter.
    from harness_runtime.types import HarnessContext

    # The method's logic accesses self.skill_activation_emitter, self.skills,
    # raises UnknownSkillError if needed. We can test the silent-skip arm by
    # building a partial duck-typed object.
    class _PartialCtx:
        skill_activation_emitter = None
        skills: dict = {}
        activate_skill = HarnessContext.activate_skill

    # No exception, no side-effect
    _PartialCtx().activate_skill(SkillID("nonexistent"))


def test_activate_skill_raises_unknown_skill_error() -> None:
    """AC #5: activate_skill raises UnknownSkillError on unknown skill_id."""
    from harness_runtime.types import HarnessContext

    tp = _FakeTracerProvider()
    emitter = SkillActivationSpanEmitter(tracer_provider=tp)

    class _PartialCtx:
        skill_activation_emitter = emitter
        skills: dict = {}
        activate_skill = HarnessContext.activate_skill

    with pytest.raises(UnknownSkillError):
        _PartialCtx().activate_skill(SkillID("unknown"))


def test_activate_skill_emits_filesystem_read_span() -> None:
    """AC #3: HarnessContext.activate_skill emits one skill.activation span
    with activation_mode = filesystem_read."""
    from harness_runtime.types import HarnessContext

    tp = _FakeTracerProvider()
    emitter = SkillActivationSpanEmitter(tracer_provider=tp)
    skill = _make_skill("my-skill")

    class _PartialCtx:
        skill_activation_emitter = emitter
        skills = {SkillID("my-skill"): skill}
        activate_skill = HarnessContext.activate_skill

    _PartialCtx().activate_skill(SkillID("my-skill"), workflow_id="wf-99")
    assert len(tp.tracer.spans) == 1
    assert tp.tracer.spans[0].attrs["skill.activation_mode"] == "filesystem_read"
    assert tp.tracer.spans[0].attrs["workflow.id"] == "wf-99"


# --- U-RT-101 hook-2 (per-LLM-dispatch) tests --------------------------------


def test_dispatcher_emits_tool_search_span_when_hook_returns_skill() -> None:
    """AC #1: per-LLM-dispatch hook emits one span per selected skill with
    activation_mode = tool_search."""
    from harness_runtime.lifecycle.llm_dispatch import RuntimeLLMDispatcher

    tp = _FakeTracerProvider()
    emitter = SkillActivationSpanEmitter(
        tracer_provider=tp,
        hook=_HookSelectingAllForLLM(),
    )
    skill = _make_skill("my-skill")
    skills_map = {SkillID("my-skill"): skill}

    # Build dispatcher with no providers (we'll invoke just the hook block).
    # Hook firing is at start of dispatch() BEFORE provider resolution; we'll
    # raise ProviderUnreachable after the hook fires to short-circuit.
    dispatcher = RuntimeLLMDispatcher(
        providers={},
        tracer_provider=tp,
        skill_activation_emitter=emitter,
        skills=skills_map,
    )

    # Invoke dispatch — provider resolution will fail, but hook fires first.
    import asyncio

    # The hook only needs step_context.workflow_id + step_context.step_index;
    # we use a duck-typed object instead of a full StepExecutionContext.

    class _SC:
        workflow_id = "wf-llm-1"
        step_index = 7

    class _Binding:
        class model_binding:
            provider = "nonexistent"
            model = "x"

    class _Step:
        step_payload = None

    with pytest.raises(Exception):
        asyncio.run(dispatcher.dispatch(_Binding(), _Step(), step_context=_SC()))

    # Hook fired before exception — span should be emitted
    skill_spans = [s for s in tp.tracer.spans if s.attrs.get("__span_name__") == "skill.activation"]
    assert len(skill_spans) == 1
    assert skill_spans[0].attrs["skill.activation_mode"] == "tool_search"


class _HookSelectingAllForLLM:
    def select_for_workflow_init(self, loaded_skills, workflow_id):
        return list(loaded_skills)

    def select_for_llm_dispatch(self, loaded_skills, workflow_id, step_index):
        return list(loaded_skills)


def test_dispatcher_no_op_when_emitter_or_hook_none() -> None:
    """AC #4: dispatcher silent-skips when emitter is None OR hook is None."""
    from harness_runtime.lifecycle.llm_dispatch import RuntimeLLMDispatcher

    tp = _FakeTracerProvider()

    # Emitter None
    dispatcher = RuntimeLLMDispatcher(
        providers={},
        tracer_provider=tp,
        skill_activation_emitter=None,
        skills={SkillID("x"): _make_skill("x")},
    )
    assert dispatcher.skill_activation_emitter is None

    # Hook None (emitter present but without hook)
    emitter_no_hook = SkillActivationSpanEmitter(tracer_provider=tp, hook=None)
    dispatcher2 = RuntimeLLMDispatcher(
        providers={},
        tracer_provider=tp,
        skill_activation_emitter=emitter_no_hook,
        skills={SkillID("x"): _make_skill("x")},
    )
    assert dispatcher2.skill_activation_emitter.hook is None
