# Maintaining MCP Privacy Proxy

This repository is an unofficial maintained fork of MaskitMCP/openmaskit.

- Preserve sender/recipient masking, empty-string handling, size-limit withholding and startup rule restoration.
- Keep all actual mailbox data, credentials, logs, databases, runtime environments and backups under the ignored `.runtime/` directory or the existing user application data directory. Never commit them or upload them.
- Keep `LICENSE`, `NOTICE` and upstream attribution. Use the MCP Privacy Proxy name and distinct icon for this modified build.
- The user explicitly authorized publishing these privacy fixes and regression tests in a public native GitHub fork on 19 October 2026. GitHub-hosted upstream synchronization, maintenance commits, pushes and validation are authorized. Sending email and changing access permissions beyond this publication are not authorized.
- Scheduled maintenance runs only on GitHub. Do not create or enable local scheduled jobs, recurring Codex tasks or automatic local installations. The local maintenance automation was deleted at the user's request on 19 October 2026. Local installation and deployment are manual and require a user request.
- Use `scripts/maintain.py` in the GitHub maintenance workflow, which publishes only after successful validation. It prepares candidates separately and runs the complete non-browser suite plus a real synthetic startup/MCP check before advancing the maintained branch. The `--deploy` option is for a user-requested local installation only.
- Keep the native GitHub fork named `openmaskit` and preserve its upstream relationship. Do not introduce further unnecessary naming changes.
- Refresh locked dependencies at least weekly. Never remove the MCP major-version constraint without an intentional compatibility migration.
- On merge conflicts, test failures, unexpected privilege changes or breaking configuration/schema changes, keep the last working release running and report the exact action needed.
- Validate every deployment by comparing live saved rules with the database and checking service health. Keep a database backup and the previous immutable release for rollback.
- Once upstream implements equivalent fixes, reduce the fork changes only after the privacy and startup tests still pass.
- Stay quiet on scheduled runs when no change occurred. Notify on a completed update, failed validation, failed rollout, upstream fixes becoming available, or a required user action.
