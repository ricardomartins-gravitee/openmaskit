# Maintaining Mail Privacy Proxy

This repository is an unofficial maintained fork of MaskitMCP/openmaskit.

- Preserve sender/recipient masking, empty-string handling, size-limit withholding and startup rule restoration.
- Keep all actual mailbox data, credentials, logs, databases, runtime environments and backups under the ignored `.runtime/` directory or the existing user application data directory. Never commit them or upload them.
- Keep `LICENSE`, `NOTICE` and upstream attribution. Use the Mail Privacy Proxy name and distinct icon for this modified build.
- Keep the fork private while the confirmed privacy bugs await coordinated upstream handling. The user's authorization to maintain this fork includes ordinary upstream synchronization, commits, pushes, testing and deploying compatible updates. It does not authorize sending email or changing repository visibility.
- Use `scripts/maintain.py --push --deploy` for scheduled maintenance. It prepares candidates separately and runs the complete non-browser suite plus a real synthetic startup/MCP check before advancing the maintained branch.
- Refresh locked dependencies at least weekly. Never remove the MCP major-version constraint without an intentional compatibility migration.
- On merge conflicts, test failures, unexpected privilege changes or breaking configuration/schema changes, keep the last working release running and report the exact action needed.
- Validate every deployment by comparing live saved rules with the database and checking service health. Keep a database backup and the previous immutable release for rollback.
- Once upstream implements equivalent fixes, reduce the fork changes only after the privacy and startup tests still pass.
- Stay quiet on scheduled runs when no change occurred. Notify on a completed update, failed validation, failed rollout, upstream fixes becoming available, or a required user action.
