# OpenMaskit

An unofficial maintained fork of [OpenMaskit](https://github.com/MaskitMCP/openmaskit), with its Apache-2.0 license, notices and attribution preserved. It is not endorsed by the upstream project. The Python module, storage directory and environment variable names remain compatible so existing local configuration can be reused.

## Changes

- Empty header strings remain empty; legacy empty aliases cannot expand text.
- Tool results requiring field parsing are withheld when a text block exceeds the parsing limit.
- Saved rules reload for database-managed servers during startup.
- Failed upstream connections stay isolated from the dashboard and other servers;
  transport failures retain saved activation and privacy configuration for retry.
  Disconnects stop per-server background tasks and flush pending aliases before
  reconnecting; failed writes remain queued and block stale alias-counter reloads.
- The dashboard, distribution and command retain the original OpenMaskit name and icon. This repository remains an unofficial fork with its provenance documented here.
- Dependencies are locked, including the existing MCP major-version compatibility constraint.
- Reusable analyzer-backed text checks support shared definitions and server-only definitions, with local Presidio and compatible HTTP adapters. See [privacy-checks.md](docs/privacy-checks.md).

The parsing limit is still a resource guard. Names in message bodies, subjects, previews, raw source and non-text media still need separate protection.

## Updating

`bin/maintain` checks upstream every scheduled run. It prepares a separate candidate, merges upstream, updates the visible fork version, validates the complete non-browser test suite and a real synthetic startup/MCP check, then advances the maintained source only if validation passes. Locked dependencies are refreshed at least weekly.

The GitHub workflow checks upstream every six hours and publishes tested source updates independently of the user's computer. There is no local recurring maintenance task: the local automation was deleted at the user's request. Local updates are installed manually when the user requests them. Do not create local scheduled jobs or automatic installations. A conflict, failed check or incompatible change requires attention; it does not replace the live release.

## Deployment

An optional [container deployment](docs/containers.md) packages OpenMaskit and Presidio with persistent storage. GitHub builds and validates native amd64/arm64 images before publishing versioned GHCR manifests. Local container startup and updates remain manual.

The service runs from a Git commit snapshot under `.runtime/releases/`, installed normally from the frozen lockfile without editable source. `.runtime/current` points to the selected release. Source edits in this checkout cannot mutate the installed release. Before replacement, the service shuts down gracefully and the application database is backed up locally. The new service must report the expected package version and restore every saved rule. A failed rollout restores the previous service and database automatically.

The first migration uses `scripts/deploy.py --adopt-pid <current-service-pid>`. Later deployments use `scripts/deploy.py`. `bin/openmaskit` starts the selected release manually. A different process at the recorded PID is never stopped.

## Local state and credentials

`.runtime/` is ignored by Git and contains environments, caches, logs, maintenance status and private database backups. Existing mailbox configuration and credentials remain in `~/.openmaskit/`. Never publish either directory. Repository workflows use synthetic fixtures and do not need any mailbox access or personal secrets.

The user authorized publishing this as a public native GitHub fork, including the privacy fixes and synthetic regression tests. The fork retains upstream history and attribution, with an `upstream` remote for synchronization. Real application data remains local and excluded from Git.

## Reducing maintenance

When upstream ships equivalent protections, keep the regression checks and remove redundant fork changes only after the full suite and startup/MCP checks pass. Each update records the upstream commit in `fork.json`; every deployed version has an exact Git commit and a retained previous release.

## Shared design values

The fork consumes pinned, locally bundled theme assets from the independently
maintained OpenMaskit Design System. Existing OpenMaskit identity and upstream
attribution are preserved. See [design-system.md](docs/design-system.md) for
integrity checks and deliberate updates shared with the native macOS Companion.
