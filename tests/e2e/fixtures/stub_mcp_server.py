"""Tiny stdio MCP server for e2e tests.

Two tools:

- ``lookup_user(name)`` returns a fixed record with ``email``, ``host``, and
  ``phone_number`` fields. Used to exercise masking and strip rules.
- ``echo(text)`` returns the input verbatim. Used to verify argument
  injections — the test asserts that the echoed value reflects the
  injected one, not the value the test sent.

Spawned by OpenMaskit as ``<python> stub_mcp_server.py`` from the e2e
custom-target tests. Uses the ``mcp`` runtime dep that's already in
[project] dependencies.
"""

from __future__ import annotations

import json
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent

mcp = FastMCP("stub")


@mcp.tool()
def lookup_user(name: str) -> CallToolResult:
    """Return a fixed user record. The ``name`` argument is echoed back."""
    data = {
        "id": 1,
        "name": name,
        "email": "alice@example.com",
        "host": "prod-db.internal.net",
        "phone_number": "+15551234567",
    }
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(data))], structuredContent=data)


@mcp.tool()
def echo(text: str) -> dict:
    """Return what the caller sent — used to verify injections."""
    return {"received": text}


if __name__ == "__main__":
    mcp.run()
