# Pre-commit notes — lane id on the widget emitter

## What changed

`tools/widget_log.py emit` records a `lane_id` on every row. When `HARNESS_LANE_ID` is not set
in the environment it falls back to a synthesized `<host>-nolane` id, and agent shell exports
do not survive between tool calls — so every row written through the skills has carried the
fallback. The fix is documentation only: both carriers now show the prefixed command.

`.claude/skills/widget/SKILL.md`, line 88 (before → after):

```
- `just widget-emit --pr <PR#> --arc-id <arc-id>`
+ `HARNESS_LANE_ID=<lane-id> just widget-emit --pr <PR#> --arc-id <arc-id>`
```

`.agents/skills/widget/SKILL.md`, line 41 (before → after):

```
- `just widget-emit --pr <N> --arc-id <arc-id>`
+ `HARNESS_LANE_ID=<lane-id> just widget-emit --pr <N> --arc-id <arc-id>`
```

## Tests

No test was added or changed. The existing carrier test still passes:

```python
def test_widget_carriers_document_emit() -> None:
    for path in [".claude/skills/widget/SKILL.md", ".agents/skills/widget/SKILL.md"]:
        section = (ROOT / path).read_text(encoding="utf-8")
        assert "just widget-emit" in section, path
```
