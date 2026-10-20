# Maintaining OpenMaskit

This repository is an unofficial maintained fork of MaskitMCP/openmaskit.

## Required project guidance

Before investigating, reviewing or changing this repository, read these files in full once per conversation:

1. [CLAUDE.md](CLAUDE.md) — shared project architecture, local deployment/threat model, terminology, protocol invariants, persistence and development guidance. These instructions apply to every coding agent working on this fork.
2. [.claude/CLAUDE.md](.claude/CLAUDE.md) — CodeGraph workflow. Use it when the CodeGraph tools and an initialized index are actually available. Otherwise use native source search/read tools; do not install tools or initialize an index unless the user requests it.

Keep the detailed guidance in those existing files rather than copying it here, so upstream documentation updates remain effective. Read each file once; following links back here does not require another reading cycle.

For fork maintenance and deployment, also read [FORK.md](FORK.md). For analyzer/check work, read [docs/privacy-checks.md](docs/privacy-checks.md). Resolve stale architectural descriptions and example routes against the current source before relying on them.

The fork-specific instructions below take precedence where they differ from those shared documents. User instructions and system/developer instructions retain their normal priority. In particular, upstream development commands do not authorize production deployment, publication to upstream package registries, local scheduled jobs or changes to real mailbox data.

## Fork maintenance instructions

- Preserve sender/recipient masking, empty-string handling, size-limit withholding and startup rule restoration.
- Keep all actual mailbox data, credentials, logs, databases, runtime environments and backups under the ignored `.runtime/` directory or the existing user application data directory. Never commit them or upload them.
- Keep `LICENSE`, `NOTICE` and upstream attribution. The user explicitly requested restoring the original OpenMaskit name and icon on 20 October 2026. Keep that name in the UI, package, CLI, documentation and maintenance checks; do not rename the project or app without an explicit user request.
- The user explicitly authorized publishing these privacy fixes and regression tests in a public native GitHub fork on 19 October 2026. GitHub-hosted upstream synchronization, maintenance commits, pushes and validation are authorized. Sending email and changing access permissions beyond this publication are not authorized.
- Scheduled maintenance runs only on GitHub. Do not create or enable local scheduled jobs, recurring Codex tasks or automatic local installations. The local maintenance automation was deleted at the user's request on 19 October 2026. Local installation and deployment are manual and require a user request.
- Use `scripts/maintain.py` in the GitHub maintenance workflow, which publishes only after successful validation. It prepares candidates separately and runs the complete non-browser suite plus a real synthetic startup/MCP check before advancing the maintained branch. The `--deploy` option is for a user-requested local installation only.
- Keep the native GitHub fork named `openmaskit` and preserve its upstream relationship. Do not introduce further unnecessary naming changes.
- Refresh locked dependencies at least weekly. Never remove the MCP major-version constraint without an intentional compatibility migration.
- On merge conflicts, test failures, unexpected privilege changes or breaking configuration/schema changes, keep the last working release running and report the exact action needed.
- Validate every deployment by comparing live saved rules with the database and checking service health. Keep a database backup and the previous immutable release for rollback.
- Once upstream implements equivalent fixes, reduce the fork changes only after the privacy and startup tests still pass.
- Stay quiet on scheduled runs when no change occurred. Notify on a completed update, failed validation, failed rollout, upstream fixes becoming available, or a required user action.
