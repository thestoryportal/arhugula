# AGENTS.md - Portable Arhugula Harness

This repository is the portable runtime package for Arhugula. Treat it as a
runtime/application repo, not the original harness build-governance repo.

## Startup

- Read `README.md`, `docs/README.md`, and `docs/portable-install.md` before
  changing install, config, or deployment behavior.
- Prefer `just` recipes for local commands. Run `just --list` to see the
  portable command surface.
- Keep secrets out of committed files. `.env` and `harness.toml` are local and
  gitignored.

## Change Discipline

- Keep runtime code changes in the `harness-*` packages.
- Keep operator documentation in `docs/`.
- Keep deployment templates in `deploy/`.
- Do not add original build-governance artifacts such as design-substrate,
  `.harness` roadmap ledgers, `.claude/`, `.codex/`, or local worktrees unless
  this repo is deliberately being converted back into a harness-development
  repo.

## Verification

- For install/config changes, run `just init-local` in a clean checkout or temp
  clone and verify `harness.toml` points at that checkout.
- For normal runtime changes, run `just check-local`.
- For image/package changes, run `just q4-packaging-check`.
