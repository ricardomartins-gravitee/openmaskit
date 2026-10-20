"""Exact deployment allowlists for container analyzer service origins."""
import httpx
import pytest
from pydantic import ValidationError

from openmaskit.privacy.adapters import CheckFailure, HttpSpanAdapter
from openmaskit.privacy.models import ProviderConfig, CheckConfig

ENV = "OPENMASKIT_ANALYZER_ALLOWED_ORIGINS"
ORIGIN = "http://presidio-analyzer:3000"


@pytest.mark.parametrize("url", [ORIGIN, ORIGIN + "/analyze", "http://PRESIDIO-ANALYZER:3000/analyze"])
def test_exact_container_origin_allowed_only_by_operator(monkeypatch, url):
    monkeypatch.delenv(ENV, raising=False)
    with pytest.raises(ValidationError):
        ProviderConfig(name="Container", endpoint=url)
    monkeypatch.setenv(ENV, ORIGIN)
    assert ProviderConfig(name="Container", endpoint=url).endpoint == url


@pytest.mark.parametrize("url", ["http://presidio-analyzer:3001", "https://presidio-analyzer:3000",
    "http://presidio-analyzer.evil:3000", "http://192.168.1.1:3000", "http://169.254.169.254",
    "http://user@presidio-analyzer:3000", "http://@presidio-analyzer:3000",
    ORIGIN + "?token=secret", ORIGIN + "#fragment", ORIGIN + "\n", "http://*:3000"])
def test_container_allowlist_does_not_allow_other_destinations(monkeypatch, url):
    monkeypatch.setenv(ENV, ORIGIN)
    with pytest.raises(ValidationError):
        ProviderConfig(name="Denied", endpoint=url)


@pytest.mark.parametrize("value", ["*", "http://*", ORIGIN + "/analyze", ORIGIN + "?x=y", "http://user@presidio-analyzer:3000"])
def test_invalid_operator_allowlist_fails_closed(monkeypatch, value):
    monkeypatch.setenv(ENV, value)
    with pytest.raises(ValidationError):
        ProviderConfig(name="Denied", endpoint=ORIGIN)


def test_allowlist_normalizes_default_ports_and_accepts_multiple_origins(monkeypatch):
    monkeypatch.setenv(ENV, "http://presidio-analyzer:80/, https://other-analyzer:443")
    assert ProviderConfig(name="One", endpoint="http://presidio-analyzer/analyze")
    assert ProviderConfig(name="Two", endpoint="https://other-analyzer/detect")
    assert ProviderConfig(name="Loopback", endpoint="http://127.0.0.1:5002")


@pytest.mark.asyncio
async def test_revoked_origin_is_rejected_before_http(monkeypatch):
    monkeypatch.setenv(ENV, ORIGIN)
    provider = ProviderConfig(name="Allowed initially", endpoint=ORIGIN)
    monkeypatch.delenv(ENV)
    calls = []
    async def record(request):
        calls.append(request)
        return httpx.Response(200, json=[])
    async with httpx.AsyncClient(transport=httpx.MockTransport(record)) as client:
        with pytest.raises(CheckFailure):
            await HttpSpanAdapter().analyze(client, provider, CheckConfig(name="Names", provider_id="p"), "Synthetic name")
    assert not calls
