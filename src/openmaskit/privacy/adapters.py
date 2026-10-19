"""Local HTTP analyzer adapters. Providers detect spans; the proxy owns aliases."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass

import httpx

from openmaskit.privacy.models import CheckConfig, ProviderConfig

MAX_TEXT = 100_000
MAX_RESPONSE = 1_048_576


class CheckFailure(Exception):
    """A required check could not complete; original output must be withheld."""


@dataclass(frozen=True)
class Finding:
    start: int
    end: int
    entity_type: str
    score: float


class HttpSpanAdapter:
    """Presidio and a small compatible JSON span protocol, with bounded I/O.

    Presidio: POST /analyze with text, language, entities, score_threshold;
    response is a list of {start,end,entity_type,score} using Unicode code points.
    http_spans: POST to the exact configured URL with the same request; response
    is {findings: [the same objects]}. No redirected requests or environment proxy.
    """

    async def analyze(self, client: httpx.AsyncClient, provider: ProviderConfig,
                      check: CheckConfig, text: str) -> list[Finding]:
        if len(text) > MAX_TEXT:
            raise CheckFailure("Text exceeds analyzer limit")
        endpoint = provider.endpoint
        if provider.adapter == "presidio" and not endpoint.endswith("/analyze"):
            endpoint += "/analyze"
        payload = {"text": text, "language": check.language, "entities": check.entities,
                   "score_threshold": check.threshold}
        try:
            async with client.stream("POST", endpoint, json=payload) as response:
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_RESPONSE:
                        raise CheckFailure("Analyzer response exceeds limit")
            rows = json.loads(data)
            if provider.adapter == "http_spans":
                rows = rows["findings"]
            if not isinstance(rows, list) or len(rows) > 10_000:
                raise CheckFailure("Invalid analyzer response")
            findings = []
            for row in rows:
                start, end, entity, score = row["start"], row["end"], row["entity_type"], row["score"]
                if (type(start) is not int or type(end) is not int
                        or not 0 <= start < end <= len(text)
                        or type(score) not in (int, float) or not math.isfinite(score)
                        or not 0 <= score <= 1 or not isinstance(entity, str)):
                    raise CheckFailure("Invalid analyzer span")
                if entity in check.entities and score >= check.threshold:
                    findings.append(Finding(start, end, entity, score))
            return findings
        except CheckFailure:
            raise
        except (httpx.HTTPError, ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
            # Never return analyzer bodies or exception text containing submitted data.
            raise CheckFailure("Analyzer unavailable or returned invalid findings") from exc


ADAPTERS = {"presidio": HttpSpanAdapter(), "http_spans": HttpSpanAdapter()}
