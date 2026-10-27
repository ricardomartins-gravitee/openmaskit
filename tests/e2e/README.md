# OpenMaskit E2E tests

Browser-driven end-to-end tests that boot a real OpenMaskit subprocess on dedicated ports, install marketplace servers, and drive the dashboard with Playwright.

## What's covered

- `test_postgres_flow.py` — install PostgreSQL from the marketplace, hide a tool, run a SQL query, mask `email` (via the hover-to-Mask tree button) and `phone_number` (via the manual path entry), verify both fields render as aliases, add an argument guardrail on `sql`, and confirm a dangerous query is blocked.

## Prerequisites

- A running container runtime (Docker / Podman / nerdctl / Finch) so the `crystaldba/postgres-mcp` image can be pulled and launched by the proxy.
- A Postgres database with a `users` table containing `email` and `phone_number` columns.
  ```sql
  CREATE TABLE users (
      id INTEGER PRIMARY KEY,
      name TEXT,
      email TEXT,
      phone_number TEXT
  );
  INSERT INTO users VALUES
      (1, 'John Krasinsky', 'user1@yahoo.com', NULL),
      (2, 'John Doe', 'john.doe@gmail.com', '+31611119987');
  ```
- Reachable from the container — on macOS that means `host.containers.internal` (Podman) or `host.docker.internal` (Docker Desktop), not `localhost`.
- Network access for the initial dependency, NLP model, browser and container downloads. Marketplace tests use a local catalog fixture by default, while running the real PostgreSQL MCP container. Set `OM_E2E_LIVE_CATALOG=1` to probe the public catalog separately.
- Ports `19473` / `19474` free.

## Run

```bash
# Install Playwright deps + the browser (one-time)
uv sync --group e2e
uv run --group e2e playwright install chromium

# Run the e2e suite
OM_E2E_PG_URI=postgresql://user:pass@host.containers.internal:5432/dbname \
    uv run --group e2e pytest tests/e2e -m e2e -v

# Headed (watch it click through):
OM_E2E_PG_URI=... uv run --group e2e pytest tests/e2e -m e2e -v --headed --slowmo 200

# Dump the OpenMaskit subprocess stderr on shutdown (debugging startup):
OM_E2E_PG_URI=... OM_E2E_DUMP_LOGS=1 uv run --group e2e pytest tests/e2e -m e2e -v
```

E2E tests are excluded from the default `uv run pytest tests/` run via the `-m 'not e2e'` default in `pyproject.toml`, so they never run accidentally in CI's unit-test stage.

## Notes

- Each test spawns a fresh proxy with an isolated home, database, encryption key, installation identifier, logs and separate dashboard/MCP ports. The selected Docker daemon socket is preserved without copying Docker credentials. The normal running proxy and user data are not used.
- The proxy must download the postgres-mcp container image on first run — the install step's timeout is set to 60s to accommodate this; if you're on a slow link, pre-pull with `docker pull crystaldba/postgres-mcp` (or your runtime's equivalent).

## Real Presidio acceptance

The privacy acceptance tests cover the shared library, server-only checks,
analyzer-owned definitions, filtered server/tool assignments, preview, editing, removal, pause isolation, retry without duplicate definitions, mobile layout and browser errors;
and real MCP masking of both JSON text and structured results, per-server alias
isolation and reversal, full process restart, and withholding during an analyzer
outage. They use only synthetic data. Presidio runs as a disposable test process,
not an installed or scheduled service.

```bash
uv venv --python 3.12 .runtime/presidio-test
uv pip install --python .runtime/presidio-test/bin/python -r tests/e2e/requirements-presidio.txt
PRESIDIO_TEST_PYTHON="$PWD/.runtime/presidio-test/bin/python" \
  OM_E2E_PG_URI=postgresql://user:pass@host.docker.internal:5432/dbname \
  uv run --frozen --group e2e pytest tests/e2e -m e2e -v \
  --basetemp=.runtime/acceptance --junitxml=.runtime/acceptance.xml \
  --screenshot only-on-failure --output .runtime/browser-results
```

Without `PRESIDIO_TEST_PYTHON`, the real-model tests skip. Without
`OM_E2E_PG_URI`, the four PostgreSQL tests skip. GitHub's acceptance job provisions
both and runs the complete suite; the unit job runs all non-browser regressions
and the existing real startup/MCP smoke check. These jobs run on pull requests
and main-branch pushes, without local scheduling or automatic installation.

`test_privacy_resilience.py` separately exercises UI save/preview failures,
retry without duplicate definitions, disabled controls while a request is pending,
Escape cancellation, analyzer reconnection and escaped markup in names. These
tests deliberately intercept HTTP responses to isolate browser error handling;
they supplement the real Presidio tests rather than standing in for model checks.
All in-process backend tests use a fresh temporary encryption key per test, even
when an individual test restores its own monkeypatches.

The public catalog returned HTTP 400 ("Missing required headers") during the
19 October 2026 acceptance run, including with the original upstream version
header. Its live availability is independent of this change. The local catalog
fixture preserves the tested API contract without making acceptance depend on
that external service. It does not simulate the MCP server or database.
