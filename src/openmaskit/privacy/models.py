"""Dashboard configuration for shared and server-scoped text checks."""
from __future__ import annotations

import ipaddress
import os
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


ALLOWED_ANALYZER_ORIGINS_ENV = "OPENMASKIT_ANALYZER_ALLOWED_ORIGINS"


def _analyzer_origin(value: str, *, origin_only: bool = False):
    if any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise ValueError("Analyzer URLs cannot contain whitespace or control characters")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise ValueError("Invalid analyzer URL") from exc
    if (parts.scheme not in ("http", "https") or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment or port == 0 or "*" in parts.hostname):
        raise ValueError("Use an HTTP(S) analyzer URL without credentials, query or fragment")
    if origin_only and parts.path not in ("", "/"):
        raise ValueError("Allowed analyzer origins cannot include a path")
    return parts, (parts.scheme, parts.hostname, port or (443 if parts.scheme == "https" else 80))


def validate_analyzer_endpoint(value: str) -> str:
    """Loopback by default; operators may explicitly allow exact service origins.

    The allowlist is deployment configuration, never writable by dashboard/MCP
    callers. No wildcard, private-network-wide or redirect exception is added.
    """
    parts, origin = _analyzer_origin(value)
    try:
        local = parts.hostname == "localhost" or ipaddress.ip_address(parts.hostname).is_loopback
    except ValueError:
        local = False
    if local:
        return value
    configured = os.environ.get(ALLOWED_ANALYZER_ORIGINS_ENV, "")
    try:
        allowed = {_analyzer_origin(item.strip(), origin_only=True)[1]
                   for item in configured.split(",") if item.strip()}
    except ValueError as exc:
        raise ValueError("Invalid analyzer origin allowlist configuration") from exc
    if origin not in allowed:
        raise ValueError("Use localhost, a loopback IP, or an explicitly allowed analyzer service origin")
    return value


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1, max_length=120)
    adapter: Literal["presidio", "http_spans"] = "presidio"
    endpoint: str = Field(min_length=1, max_length=500)

    @field_validator("endpoint")
    @classmethod
    def local_endpoint(cls, value: str) -> str:
        return validate_analyzer_endpoint(value)


class CheckConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1, max_length=120)
    provider_id: str = Field(min_length=1, max_length=64)
    language: str = Field(default="en", pattern=r"^[a-z]{2,3}(?:-[A-Za-z]{2,8})?$")
    entities: list[str] = Field(default_factory=lambda: ["PERSON"], min_length=1, max_length=32)
    threshold: float = Field(default=0.7, ge=0, le=1, allow_inf_nan=False)
    action: Literal["mask", "redact", "block"] = "mask"
    field_paths: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("entities")
    @classmethod
    def entity_names(cls, values: list[str]) -> list[str]:
        if any(not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", v) for v in values):
            raise ValueError("Entity types must use uppercase letters, digits and underscores")
        return list(dict.fromkeys(values))

    @field_validator("field_paths")
    @classmethod
    def paths(cls, values: list[str]) -> list[str]:
        if any(len(v) > 256 or not re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*", v) for v in values):
            raise ValueError("Use dot-separated field names, with implicit list traversal")
        return list(dict.fromkeys(values))


class AssignmentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    check_id: str = Field(min_length=1, max_length=64)
    tool_name: str = Field(default="*", min_length=1, max_length=256)
    active: bool = True
