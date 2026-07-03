# Harness Runtime Documentation

This is the portable operator-facing documentation set for the harness runtime.
It covers external clone setup, first use, day-to-day operation, deployment
readiness, API/config reference, and architecture.

## Audience Map

| Audience | Start here | Outcome |
| --- | --- | --- |
| External tester | [Portable install](portable-install.md) | Pull a clean runtime repo, initialize local config, and run the first workflow. |
| New runtime operator | [Tutorial: first workflow](tutorial-first-workflow.md) | Run the shipped minimal workflow through `harness run`. |
| Daily operator or workflow author | [How to operate the runtime](how-to-operate-runtime.md) | Use one-shot CLI, daemon mode, admin inspection, and shutdown paths. |
| Deployment owner | [How to deploy](how-to-deploy.md) | Validate self-hosted, managed-cloud, and image packaging surfaces before live runs. |
| Maintainer or reviewer | [Reference](reference.md) and [Architecture](architecture.md) | Check public commands, config fields, workflow shape, package layout, and runtime flow. |
| Memory operator or reviewer | [Memory layer README](memory-layer-readme.md) and [Memory substrate](memory-substrate.md) | Review memory features, workflow, usage, policy, architecture, migration posture, and live gates. |

## Public Surfaces Covered

| Surface | Documentation |
| --- | --- |
| Portable source packaging | [Portable install](portable-install.md) |
| Runtime CLI | [How to operate the runtime](how-to-operate-runtime.md), [Reference](reference.md) |
| Runtime config | [Tutorial](tutorial-first-workflow.md), [Reference](reference.md) |
| Workflow manifests | [Tutorial](tutorial-first-workflow.md), [Reference](reference.md) |
| Example workflows | [Tutorial](tutorial-first-workflow.md) |
| Self-hosted readiness | [How to deploy](how-to-deploy.md) |
| Managed-cloud readiness | [How to deploy](how-to-deploy.md) |
| Runtime image packaging | [How to deploy](how-to-deploy.md), [Reference](reference.md) |
| Architecture/API | [Architecture](architecture.md), [Reference](reference.md) |
| Memory layer usage and workflow | [Memory layer README](memory-layer-readme.md) |
| Memory substrate policy and live gates | [Memory substrate](memory-substrate.md) |

## Source Grounding

This index is grounded in the runtime CLI, config loader, example workflow
guide, deployment runbooks, portable package manifest, and local init tooling:
`harness-runtime`, `examples/README.md`, `harness.toml.example`,
`deploy/self-hosted-local/README.md`, `deploy/managed-cloud/README.md`,
`deploy/images/README.md`, `packaging/portable-source.toml`,
`tools/portable_source_package.py`, `tools/portable_init.py`,
`docs/memory-layer-readme.md`, `docs/memory-substrate.md`,
`tools/memory_closeout_check.py`, and
`harness-runtime/src/harness_runtime/memory_verification_suite.py`.
