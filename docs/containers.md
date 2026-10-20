# OpenMaskit containers

This fork publishes OpenMaskit for Linux amd64 and arm64 at
`ghcr.io/ricardomartins-gravitee/openmaskit`. Docker Desktop selects the appropriate
architecture on Intel and Apple Silicon Macs.

The root `compose.yaml` runs OpenMaskit and Presidio Analyzer together. The existing
`compose.presidio.yaml` remains the Presidio-only option for a native OpenMaskit
installation. Run only the Compose file appropriate to the deployment you choose.

## Start a separate container installation

Open Docker Desktop. From this repository:

```sh
mkdir -p .runtime
cp .env.container.example .runtime/container.env
docker compose --env-file .runtime/container.env pull
docker compose --env-file .runtime/container.env up -d --wait --wait-timeout 300
```

Open **http://127.0.0.1:9483**. The example environment uses MCP port **9484**.
It deliberately uses separate ports and the Compose project name
`openmaskit-container` for testing alongside an existing native installation
and its Presidio-only stack. Marketplace requests are disabled by default;
set `OPENMASKIT_DISABLE_MARKETPLACE=0` to enable them. Without the example environment, Compose defaults to ports 9473/9474.

This creates a fresh container installation with its own settings, aliases and key.
It does not import the native installation's configuration or attach to its files.
Host-dependent MCP connections, including the existing Thunderbird launcher, need
a separate connection design before migration. HTTP MCP servers and programs
actually installed inside the image can be used by the container.

## Connect Presidio

In this container dashboard, open **Analyzers → Add Analyzer**:

| Field | Value |
|---|---|
| Name | Presidio |
| Type | Presidio |
| Local service URL | `http://presidio-analyzer:3000` |

Choose **Check Connection**, create a check under the analyzer, and assign it from
the desired MCP server's **Privacy Checks** panel. The private service address is
for the combined container stack; the native installation still uses its own
loopback endpoint such as `http://127.0.0.1:5002`.

Presidio is not published on a host port in the combined stack. It only joins an
internal Docker network shared with OpenMaskit. OpenMaskit also has a separate
network for its outbound MCP/OAuth connections.

The image keeps the native loopback-only analyzer policy by default. Compose
explicitly sets `OPENMASKIT_ANALYZER_ALLOWED_ORIGINS=http://presidio-analyzer:3000`.
This deployment-only, comma-separated list accepts exact HTTP(S) origins, including
scheme and port; paths, credentials, queries, fragments and wildcards are rejected.
It is not editable through the dashboard or MCP. Other private-network addresses
are not implicitly allowed. Redirects and environment HTTP proxies remain disabled
for analyzer requests, and required-check failures still withhold responses.

## Persistence and manual operation

The named `openmaskit-data` volume, scoped by the Compose project name, is mounted at
`/home/openmaskit/.openmaskit`. It contains the settings database, aliases, encryption
key, installation identifier, encrypted audit log and OAuth/configuration state.
The image runs as UID/GID 10001, not root. Host ports bind only to `127.0.0.1`.

Stop containers while retaining their data:

```sh
docker compose --env-file .runtime/container.env down
```

Start again using the same environment file and Compose project/directory.
Do not use `down --volumes` for a real installation: it deletes the stored state
and encryption key. No updater, automatic restart policy, host Docker socket
mount or local scheduled task is included.

### Back up before an update

Create a consistent backup while OpenMaskit is stopped:

```sh
mkdir -p .runtime/backups
container_backup=".runtime/backups/openmaskit-container-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"
docker compose --env-file .runtime/container.env stop openmaskit
docker compose --env-file .runtime/container.env run --rm -T --no-deps --entrypoint python openmaskit -c 'import sys,tarfile; a=tarfile.open(fileobj=sys.stdout.buffer,mode="w|gz"); a.add("/home/openmaskit/.openmaskit",arcname=".openmaskit"); a.close()' > "$container_backup"
docker compose --env-file .runtime/container.env up -d --wait --wait-timeout 300
```

Keep this backup private: it contains the encryption key and configuration.
Retain a separate backup for each upgrade rather than overwriting the last
known-good backup. Restore the complete state, including the key, when recovering.

### Update explicitly

Set `OPENMASKIT_IMAGE` in `.runtime/container.env` to the desired published tag,
then run:

```sh
docker compose --env-file .runtime/container.env pull openmaskit
docker compose --env-file .runtime/container.env up -d --wait --wait-timeout 300
```

Each validated source commit has an immutable `sha-<full-commit>` tag. A version
tag such as `0.6.1-privacy.9` points to the first validated build of that version
and is never overwritten. For an exact update, use the full commit tag from the
Containers workflow summary. Keep the previous image reference and the backup
for rollback; data migrations may require restoring the backup as well.

## Build from source

```sh
docker build -t openmaskit:local .
OPENMASKIT_IMAGE=openmaskit:local docker compose --env-file .runtime/container.env up -d --wait --wait-timeout 300
```

The image installs from the frozen `uv.lock`, copies the installed package rather
than editable source, and excludes local environments, credentials, databases
and backups from its build context. Python and uv base images are pinned by digest.

## Publishing and acceptance

The Containers workflow builds on native amd64 and arm64 GitHub runners. Both
architectures must pass the regression suite, native startup/MCP protections and
`scripts/container_smoke.py` against the actual image and real Presidio. The smoke
check uses a disposable Compose project and synthetic data. It checks private
networking, local ports, non-root operation, real text detection and MCP alias
reversal, persistent keys/settings/aliases/audit encryption, container recreation,
and analyzer-outage withholding.

Only trusted main-branch runs publish to GHCR. Pull requests validate images
without publishing. Successful Tests and hosted fork-maintenance runs trigger
publication, including source updates made by the maintenance workflow. Tagged
architecture images are combined only after both native validation jobs succeed.
Version/commit manifests are not replaced on retries. A new GHCR package must be
made public by the repository owner for anonymous pulls.

GitHub Dependabot checks Docker bases and workflow actions weekly. These checks
run on GitHub; local image updates remain manual.

Sources: [uv container integration](https://docs.astral.sh/uv/guides/integration/docker/),
[Compose networking](https://docs.docker.com/compose/how-tos/networking/),
[GitHub container publishing](https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images).
