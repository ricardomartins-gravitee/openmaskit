"""E2E fixtures: boot a real OpenMaskit subprocess and drive its UI with Playwright.

The suite is opt-in. Run with ``uv run --group e2e pytest tests/e2e -m e2e``.
The Postgres-using tests skip unless ``OM_E2E_PG_URI`` is set; stub-based
tests run unconditionally and require no external services.

Each test gets a fresh OpenMaskit subprocess against a tmp store dir — slower
than a session-scoped server (~2-3s startup per test) but eliminates an
entire class of inter-test state-leak bugs without writing per-test cleanup
boilerplate.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from playwright.sync_api import Page

# Dedicated ports so the suite doesn't clobber a dashboard the user has open.
E2E_WEB_PORT = 19473
E2E_MCP_PORT = 19474

E2E_BASE_URL = f"http://127.0.0.1:{E2E_WEB_PORT}"

# Tiny MCP stdio server used by the custom-target tests.
STUB_SERVER_PATH = Path(__file__).parent / "fixtures" / "stub_mcp_server.py"

_STARTUP_TIMEOUT_S = 45
_PORT_TEARDOWN_WAIT_S = 5


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) != 0


def _wait_for_port_free(port: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_is_free(port):
            return
        time.sleep(0.1)


def _spawn_openmaskit(store_dir: Path) -> subprocess.Popen:
    key_path = store_dir / "test-encryption.key"
    if not key_path.exists():
        key_path.write_bytes(Fernet.generate_key())
        key_path.chmod(0o600)
    config_yaml = store_dir / "openmaskit.yaml"
    config_yaml.write_text(
        f'store_path: "{store_dir / "store.db"}"\n'
        f"web_port: {E2E_WEB_PORT}\n"
        f"mcp_port: {E2E_MCP_PORT}\n"
    )
    env = {
        **os.environ,
        "HOME": str(store_dir),
        "OPENMASKIT_ENCRYPTION_KEY": key_path.read_text(),
        "OPENMASKIT_HOST": "127.0.0.1",
        "OPENMASKIT_TRAFFIC_DB_PATH": str(store_dir / "traffic.db"),
    }
    # Preserve the selected daemon when HOME is isolated. Do not copy Docker
    # credentials/config into the test home. Only the context's socket is needed.
    if not env.get("DOCKER_HOST") and shutil.which("docker"):
        context = subprocess.run(["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
            capture_output=True, text=True, timeout=10)
        if context.returncode == 0 and context.stdout.strip():
            env["DOCKER_HOST"] = context.stdout.strip()
    env.pop("DOCKER_CONTEXT", None)
    if env.get("DOCKER_HOST") and shutil.which("docker"):
        # MCP's stdio environment intentionally drops arbitrary parent env vars.
        # Give Docker a credential-free context inside this disposable HOME too.
        docker_config = store_dir / ".docker"
        if not docker_config.exists():
            subprocess.run(["docker", "--config", str(docker_config), "context", "create",
                "acceptance", "--docker", "host=" + env["DOCKER_HOST"]],
                capture_output=True, text=True, check=True, timeout=10)
            subprocess.run(["docker", "--config", str(docker_config), "context", "use", "acceptance"],
                capture_output=True, text=True, check=True, timeout=10)
        env["DOCKER_CONFIG"] = str(docker_config)
    proc = subprocess.Popen(
        [sys.executable, "-m", "openmaskit", str(config_yaml)],
        env=env,
        stdout=(store_dir / "server.stdout.log").open("w"),
        stderr=(store_dir / "server.stderr.log").open("w"),
        cwd=Path(__file__).resolve().parents[2],
    )
    proc.test_log_path = store_dir / "server.stderr.log"
    deadline = time.monotonic() + _STARTUP_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            stderr = (store_dir / "server.stderr.log").read_text()
            raise RuntimeError(
                f"OpenMaskit exited during startup (rc={proc.returncode}).\n--- stderr ---\n{stderr}"
            )
        try:
            r = httpx.get(f"{E2E_BASE_URL}/health", timeout=1.0)
            if r.status_code == 200 and all(t.get("initialized") for t in r.json().get("targets", [])):
                return proc
        except httpx.HTTPError:
            pass
        time.sleep(0.4)
    _terminate(proc)
    raise RuntimeError(f"OpenMaskit did not become reachable at {E2E_BASE_URL} within {_STARTUP_TIMEOUT_S}s")


def _terminate(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    if os.environ.get("OM_E2E_DUMP_LOGS") and hasattr(proc, "test_log_path"):
        sys.stderr.write(proc.test_log_path.read_text())


@pytest.fixture
def pg_uri() -> str:
    uri = os.environ.get("OM_E2E_PG_URI")
    if not uri:
        pytest.skip("OM_E2E_PG_URI not set; skipping postgres e2e (set it to a Postgres URI to run)")
    return uri


@pytest.fixture
def openmaskit_server(tmp_path: Path) -> str:
    """Fresh OpenMaskit instance per test on dedicated ports + tmp store."""
    _wait_for_port_free(E2E_WEB_PORT, _PORT_TEARDOWN_WAIT_S)
    _wait_for_port_free(E2E_MCP_PORT, _PORT_TEARDOWN_WAIT_S)
    proc = _spawn_openmaskit(tmp_path)
    yield E2E_BASE_URL
    _terminate(proc)


@pytest.fixture
def dashboard_page(openmaskit_server: str, page: Page) -> Page:
    """Pre-navigate to the dashboard and dismiss the welcome modal."""
    page.set_default_timeout(15_000)
    page.goto(openmaskit_server)
    skip = page.get_by_role("button", name="Skip for now")
    if skip.is_visible():
        skip.click()
    return page


def api_client(server_url: str = E2E_BASE_URL) -> httpx.Client:
    """Return an httpx Client pre-loaded with a CSRF token + Origin header.

    The dashboard API's CSRF middleware rejects mutating requests without an
    ``X-CSRF-Token`` header; the Origin middleware rejects cross-origin
    mutations. Tests that bypass the dashboard JS must replay both.

    The returned client is NOT yet opened — use it with ``with api_client() as c:``.
    """
    token = httpx.get(f"{server_url}/api/csrf", timeout=5.0).json()["token"]
    return httpx.Client(
        base_url=server_url,
        timeout=15.0,
        headers={"X-CSRF-Token": token, "Origin": server_url},
    )


def install_stub_via_api(target_id: str, *, server_url: str = E2E_BASE_URL) -> None:
    """Install the stub MCP server as a custom stdio target via the JSON API.

    Skips the Add Server modal — for tests that want a target to exist but
    aren't testing the modal itself. Waits for the target to come online
    (tools/list completes) before returning.
    """
    payload = {
        "name": target_id,
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(STUB_SERVER_PATH)],
        "env": {},
    }
    with api_client(server_url) as client:
        r = client.post("/api/targets/custom", json=payload)
        r.raise_for_status()

    # Wait for the upstream session to initialize so tool schemas are available.
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        r = httpx.get(f"{server_url}/api/targets/{target_id}/tools", timeout=5.0)
        if r.status_code == 200 and r.json().get("tools"):
            return
        time.sleep(0.2)
    raise RuntimeError(f"Stub target {target_id!r} did not initialize within 15s")


@pytest.fixture
def real_presidio(tmp_path):
    """Real NLP integration, opt-in so normal browser tests stay lightweight."""
    python = os.environ.get('PRESIDIO_TEST_PYTHON')
    if not python:
        pytest.skip('Set PRESIDIO_TEST_PYTHON to an environment with Presidio and en_core_web_sm')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    script = Path(__file__).parent / 'fixtures' / 'presidio_service.py'
    log_path = tmp_path / 'presidio.log'
    with log_path.open('w') as log:
        proc = subprocess.Popen([python, str(script), str(port)], stdout=log, stderr=log,
            env={**os.environ, 'HOME': str(tmp_path)})
        try:
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    pytest.fail('Real Presidio failed to start: ' + log_path.read_text()[-3000:])
                try:
                    httpx.get(f'http://127.0.0.1:{port}/health', timeout=.5).raise_for_status()
                    break
                except httpx.HTTPError:
                    time.sleep(.2)
            else:
                pytest.fail('Real Presidio startup timed out')
            yield f'http://127.0.0.1:{port}', proc
        finally:
            _terminate(proc)


@pytest.fixture(scope='session')
def acceptance_catalog():
    """Deterministic catalog contract; the installed Postgres MCP is still real.

    The public catalog can change independently (and currently rejects its own
    client's headers). Keep external availability separate from app acceptance.
    Set OM_E2E_LIVE_CATALOG=1 to exercise the public catalog instead.
    """
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    entry = {
        'id': 'postgres-acceptance', 'handle': 'postgres', 'name': 'PostgreSQL',
        'description': 'Synthetic acceptance database', 'requires_oauth': False,
        'transport_type': 'stdio', 'official': False, 'tags': ['database'],
        'meta': {'command': 'docker', 'args': ['run', '-i', '--rm', '-e', 'DATABASE_URI',
            'crystaldba/postgres-mcp', '--access-mode=unrestricted'],
            'env': {'DATABASE_URI': {'label': 'Database URI', 'type': 'secret', 'required': True}}},
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path.startswith('/api/marketplace/catalog'):
                data = {'data': [entry], 'meta': {'total': 1, 'page': 1, 'size': 12, 'total_pages': 1}}
            elif self.path == '/api/marketplace/servers/postgres-acceptance':
                data = entry
            elif self.path == '/api/version_check':
                data = {'update_required': False, 'supported': True}
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(autouse=True)
def isolated_catalog(monkeypatch, acceptance_catalog):
    if os.environ.get('OM_E2E_LIVE_CATALOG') != '1':
        monkeypatch.setenv('OPENMASKIT_MARKETPLACE_API_URL', acceptance_catalog)
