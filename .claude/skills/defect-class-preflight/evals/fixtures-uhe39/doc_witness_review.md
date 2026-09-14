# Pre-commit notes — doc witness for the review-policy skill

The arc adds four policy dispositions to `review-policy/SKILL.md` and a shell witness that
is supposed to turn red if any of them goes missing or is changed. The witness is below,
followed by the skill excerpt it checks. It passes today (PASS=7 FAIL=0).

## `tools/hooks/test_review_policy_docs.sh` (excerpt)

```bash
RP=.claude/skills/review-policy/SKILL.md
has() { if grep -qF -- "$3" "$1"; then ok "$2"; else bad "$2 missing"; fi; }

has "$RP" "policy heading" '## Review dispositions'
for k in P1 P2 P4; do
  has "$RP" "the $k disposition" "- **$k —"
done
has "$RP" "P2 no-self-suppression rule" \
  'dropped: a reviewer never suppresses its own finding.'
has "$RP" "P3 routing deferred" \
  'routing by finding class is deferred until its predictiveness is'
has "$RP" "checkpoint statement" 'recorded-decision checkpoint'
```

## `.claude/skills/review-policy/SKILL.md` (excerpt)

```markdown
## Review dispositions

- **P1 —** alternatives in a finding are optional, not mandated.
- **P2 —** dropped: a reviewer never suppresses its own finding.
- **P3 —** routing by finding class is deferred until its predictiveness is measured; shadow
  mode only, if run at all.
- **P4 —** a blocking post-edit hook is admitted only for fast deterministic checks.

Every ten rounds is a recorded-decision checkpoint, not a cap: continuation is unbounded.
```
