"""E2b — assemble one reviewer prompt: template + structural pack + PR diff (handoff-s2 §2 D).

The E2 template names the repository the reviewer may read. E2 pointed every reviewer at
the live checkout (README "Known defects" item 3); here the path is the worktree checked
out at the reviewed PR's head, so callers and tests come from the reviewed commit
([LAW:one-source-of-truth]: the tree the pack was computed from is the tree the reviewer
reads).

usage: e2b_assemble.py <template> <diff-file> <pack-file|none> <worktree-path> > prompt.txt
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

E2_REPO_PATH = "/Users/robertrhu/Projects/arhugula-v2"  # the literal the E2 template carries
NO_PACK = "(no structural context provided)"


def assemble(template: str, diff: str, pack: str | None, worktree: str) -> str:
    fills = {
        E2_REPO_PATH: worktree,
        "{{PACK}}": pack if pack is not None else NO_PACK,
        "{{DIFF}}": diff,
    }
    for token in fills:
        if token not in template:  # [LAW:no-silent-failure] a drifted template is a loud exit
            raise SystemExit(f"e2b_assemble: template lacks {token!r}")
    # One pass over the TEMPLATE only: a token that appears inside an inserted pack or
    # diff (a diff quoting the template, a pack quoting a diff) is never re-substituted
    # (codex r1 P3 on the e2-callers arc). [LAW:dataflow-not-control-flow]
    pattern = re.compile("|".join(re.escape(t) for t in fills))
    return pattern.sub(lambda m: fills[m.group(0)], template)


def main() -> int:
    if len(sys.argv) != 5:
        print(__doc__, file=sys.stderr)
        return 2
    template, diff_file, pack_file, worktree = sys.argv[1:]
    pack = None if pack_file == "none" else Path(pack_file).read_text()
    sys.stdout.write(
        assemble(Path(template).read_text(), Path(diff_file).read_text(), pack, worktree)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
