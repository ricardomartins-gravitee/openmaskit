"""Dashboard configuration for shared and server-scoped text checks."""
from __future__ import annotations

import ipaddress
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1, max_length=120)
    adapter: Literal["presidio", "http_spans"] = "presidio"
    endpoint: str = Field(min_length=1, max_length=500)

    @field_validator("endpoint")
    @classmethod
    def local_endpoint(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or parts.username or parts.password:
            raise ValueError("Use a local HTTP(S) analyzer URL without credentials")
        if parts.query or parts.fragment or not parts.hostname:
            raise ValueError("Analyzer URL cannot contain a query or fragment")
        try:
            local = parts.hostname == "localhost" or ipaddress.ip_address(parts.hostname).is_loopback
            port = parts.port
        except ValueError as exc:
            raise ValueError("Use localhost or a loopback IP address") from exc
        if not local or port == 0:
            raise ValueError("Only local analyzers are supported")
        return value.rstrip("/")


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
