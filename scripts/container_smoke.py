"""Exercise the built image and real Presidio using a disposable Compose project."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[1]
DOCKER = os.environ.get("DOCKER_BIN") or shutil.which("docker") or str(Path.home() / ".docker/bin/docker")
NAME = "Sarah Johnson"
FIXTURE = """
from mcp.server.fastmcp import FastMCP
import hashlib
app = FastMCP("synthetic-container")
@app.tool()
def read_message() -> str:
    return "Sarah Johnson"
@app.tool()
def echo_digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()
app.run()
"""
STATE_PROBE = """
import hashlib,json,os,sqlite3
from pathlib import Path
from openmaskit.security import TokenEncryption
data = Path.home() / '.openmaskit'
key = data / '.key'
assert os.getuid() == 10001
assert key.stat().st_mode & 0o777 == 0o600
db = sqlite3.connect(str(data / 'store.db'))
assert 'synthetic-secret-only' not in db.execute('SELECT config_json FROM mcp_servers').fetchone()[0]
traffic = sqlite3.connect(str(data / 'traffic.db'))
payloads = traffic.execute('SELECT response_enc FROM traffic WHERE target_name=?', ('fixture',)).fetchall()
assert any(b'Sarah Johnson' in TokenEncryption().decrypt_bytes(row[0]) for row in payloads if row[0])
assert all(b'Sarah Johnson' not in row[0] for row in payloads if row[0])
print(json.dumps({
    'key': hashlib.sha256(key.read_bytes()).hexdigest(),
    'installation_id': hashlib.sha256((data / '.installation_id').read_bytes()).hexdigest(),
    'rules': db.execute('SELECT count(*) FROM rules').fetchone()[0],
    'definitions': db.execute('SELECT count(*) FROM privacy_definitions').fetchone()[0],
    'assignments': db.execute('SELECT count(*) FROM privacy_assignments').fetchone()[0],
    'mappings': db.execute('SELECT count(*) FROM mappings').fetchone()[0],
}))
"""


def ports():
    with socket.socket() as first, socket.socket() as second:
        first.bind(("127.0.0.1", 0))
        second.bind(("127.0.0.1", 0))
        return first.getsockname()[1], second.getsockname()[1]


def run(args, env, *, capture=False):
    result = subprocess.run([str(a) for a in args], cwd=ROOT, env=env, check=True,
        text=True, stdout=subprocess.PIPE if capture else None, timeout=360)
    return result.stdout.strip() if capture else ""


def client(base):
    token = httpx.get(base + "/api/csrf", trust_env=False, timeout=5).json()["token"]
    return httpx.Client(base_url=base, trust_env=False, timeout=20,
        headers={"Origin": base, "X-CSRF-Token": token})


def post(api, path, body):
    response = api.post(path, json=body)
    response.raise_for_status()
    return response.json()


def ready(base):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        with httpx.Client(base_url=base, trust_env=False, timeout=3) as api:
            if len(api.get("/api/targets/fixture/tools").json().get("tools", [])) == 2:
                return
        time.sleep(.2)
    raise RuntimeError("Synthetic MCP did not initialize")


async def mcp_call(port, tool, arguments):
    async with streamable_http_client(f"http://127.0.0.1:{port}/fixture/mcp") as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return (await session.call_tool(tool, arguments)).model_dump(mode="json", exclude_none=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="openmaskit:container-candidate")
    args = parser.parse_args()
    web, mcp = ports()
    env = {**os.environ, "OPENMASKIT_IMAGE": args.image, "OPENMASKIT_WEB_PORT": str(web),
           "OPENMASKIT_MCP_PORT": str(mcp), "OPENMASKIT_DISABLE_MARKETPLACE": "1"}
    base = f"http://127.0.0.1:{web}"
    compose = [DOCKER, "compose", "-f", str(ROOT / "compose.yaml"),
               "--project-name", f"openmaskit-smoke-{os.getpid()}"]

    def stack(*args, capture=False):
        return run([*compose, *args], env, capture=capture)

    try:
        stack("up", "-d", "--wait", "--wait-timeout", "300")
        cid = stack("ps", "-q", "openmaskit", capture=True)
        spec = json.loads(run([DOCKER, "inspect", cid], env, capture=True))[0]
        assert spec["Config"]["User"] == "10001:10001"
        assert spec["HostConfig"]["RestartPolicy"]["Name"] == "no"
        assert all(port["HostIp"] == "127.0.0.1" for bindings in spec["NetworkSettings"]["Ports"].values()
                   if bindings for port in bindings)
        analyzer = stack("ps", "-q", "presidio-analyzer", capture=True)
        analyzer_spec = json.loads(run([DOCKER, "inspect", analyzer], env, capture=True))[0]
        assert not analyzer_spec["HostConfig"]["PortBindings"]
        networks = analyzer_spec["NetworkSettings"]["Networks"]
        assert len(networks) == 1
        network = json.loads(run([DOCKER, "network", "inspect", next(iter(networks))], env, capture=True))[0]
        assert network["Internal"]

        with client(base) as api:
            assert api.get("/health").json()["status"] == "healthy"
            assert api.get("/api/config").json()["mcp_port"] == mcp
            assert api.post("/api/privacy/providers/create",
                json={"name": "Forbidden", "endpoint": "http://other-analyzer:3000"}).status_code == 400
            provider = post(api, "/api/privacy/providers/create",
                {"name": "Container Presidio", "endpoint": "http://presidio-analyzer:3000"})
            check = post(api, "/api/privacy/checks/create",
                {"name": "Names", "provider_id": provider["id"], "entities": ["PERSON"]})
            preview = post(api, "/api/privacy/preview", {"check": {
                k: v for k, v in check.items() if k not in {"id", "kind", "scope", "used_by"}}, "text": NAME})
            assert preview["text"] == "person_1" and NAME not in preview["text"]
            created = post(api, "/api/targets/custom", {"name": "fixture", "transport": "stdio",
                "command": "/opt/venv/bin/python", "args": ["-c", FIXTURE],
                "env": {"SYNTHETIC_SECRET": {"value": "synthetic-secret-only", "type": "secret"}}})
            assert created["connected"], "Synthetic MCP connection failed"
            ready(base)
            post(api, "/api/targets/fixture/rules/create", {"field_path": "ccList", "alias_prefix": "header"})
            post(api, "/api/targets/fixture/privacy/assignments/create", {"check_id": check["id"]})

        first = asyncio.run(mcp_call(mcp, "read_message", {}))
        alias = first["content"][0]["text"]
        assert alias == "person_1" and NAME not in json.dumps(first)
        digest = asyncio.run(mcp_call(mcp, "echo_digest", {"text": alias}))
        assert digest["content"][0]["text"] == hashlib.sha256(NAME.encode()).hexdigest()

        with client(base) as api:
            api.get("/api/targets/fixture/traffic").raise_for_status()
        # Wait only for the normal asynchronous alias flush.
        deadline = time.monotonic() + 5
        while True:
            before = json.loads(stack("exec", "-T", "openmaskit", "python", "-c", STATE_PROBE, capture=True))
            if before["mappings"] > 0:
                break
            assert time.monotonic() < deadline
            time.sleep(.2)
        stack("up", "-d", "--force-recreate", "--wait", "--wait-timeout", "300")
        ready(base)
        after = json.loads(stack("exec", "-T", "openmaskit", "python", "-c", STATE_PROBE, capture=True))
        assert before == after, "Persistent configuration, keys or aliases changed"
        assert asyncio.run(mcp_call(mcp, "read_message", {}))["content"][0]["text"] == alias
        stack("stop", "presidio-analyzer")
        blocked = asyncio.run(mcp_call(mcp, "read_message", {}))
        assert blocked.get("isError") and NAME not in json.dumps(blocked)
        stack("up", "-d", "--wait", "--wait-timeout", "300", "presidio-analyzer")
        assert asyncio.run(mcp_call(mcp, "read_message", {}))["content"][0]["text"] == alias
        print("PASS: non-root image, local ports, private Presidio network, real detection/MCP alias roundtrip,")
        print("persistent settings/key/aliases/audit encryption, container recreation and outage withholding.")
    except BaseException:
        stack("logs", "--no-color", "--tail", "120")
        raise
    finally:
        # This project and volume contain only synthetic fixtures created above.
        stack("down", "--volumes", "--remove-orphans")


if __name__ == "__main__":
    main()
