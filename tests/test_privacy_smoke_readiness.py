"""A dashboard listener is insufficient evidence that an MCP target is ready."""
import importlib.util
import io
import json
from pathlib import Path

import pytest


@pytest.mark.parametrize('tools, ready', [([], False), ([{'name': 'other'}], False),
    ([{'name': 'getMessage'}], True), ([{'name': 'other'}, {'name': 'getMessage'}], True)])
def test_smoke_waits_for_expected_upstream_tool(monkeypatch, tools, ready):
    spec = importlib.util.spec_from_file_location('privacy_smoke_readiness',
        Path(__file__).resolve().parents[1] / 'scripts/privacy_smoke.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    def response(url, timeout):
        calls.append((url, timeout))
        return io.BytesIO(json.dumps({'tools': tools}).encode())
    monkeypatch.setattr(module.urllib.request, 'urlopen', response)
    assert module.target_ready(12345) is ready
    assert calls == [('http://127.0.0.1:12345/api/targets/synthetic/tools', 1)]
