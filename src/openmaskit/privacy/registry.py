"""Local persistence, scope isolation and response scanning for privacy checks."""
from __future__ import annotations

import copy
import json
import re
from uuid import uuid4

import anyio
import httpx

from openmaskit.masking.parsing import serialize, try_parse_structured
from openmaskit.privacy.adapters import ADAPTERS, CheckFailure, Finding, MAX_TEXT
from openmaskit.privacy.models import AssignmentConfig, CheckConfig, ProviderConfig

SCHEMA = """
CREATE TABLE IF NOT EXISTS privacy_definitions (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, scope TEXT, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS privacy_assignments (
 id TEXT PRIMARY KEY, target_name TEXT NOT NULL, check_id TEXT NOT NULL,
 tool_name TEXT NOT NULL, active INTEGER NOT NULL,
 UNIQUE(target_name, check_id, tool_name)
);
"""
PREFIXES = {"PERSON": "person", "EMAIL_ADDRESS": "email", "PHONE_NUMBER": "phone",
            "LOCATION": "location", "IP_ADDRESS": "ip", "IBAN_CODE": "iban"}


def withheld():
    return {"isError": True, "content": [{"type": "text", "text":
        "A required privacy check could not release this response. "
        "Check the analyzer connection, check settings, or request a smaller result."}]}


class PrivacyChecks:
    def __init__(self, store):
        self.db = store._db
        self.definitions = {}
        self.assignments = {}
        self.lock = anyio.Lock()

    @classmethod
    async def create(cls, store):
        self = cls(store)
        await self.db.executescript(SCHEMA)
        async with self.db.execute("SELECT id,kind,scope,payload FROM privacy_definitions") as cursor:
            for identifier, kind, scope, payload in await cursor.fetchall():
                model = ProviderConfig if kind == "providers" else CheckConfig
                config = model.model_validate_json(payload).model_dump()
                self.definitions[identifier] = {"id": identifier, "kind": kind, "scope": scope, **config}
        async with self.db.execute("SELECT id,target_name,check_id,tool_name,active FROM privacy_assignments") as cursor:
            for identifier, target, check, tool, active in await cursor.fetchall():
                self.assignments[identifier] = {"id": identifier, "target_name": target,
                    "check_id": check, "tool_name": tool, "active": bool(active)}
        await self.db.commit()
        return self

    def visible(self, kind, scope):
        return [copy.deepcopy(v) for v in self.definitions.values()
                if v["kind"] == kind and v["scope"] in ({None, scope} if scope else {None})]

    def assigned(self, scope):
        return [dict(v) for v in self.assignments.values() if v["target_name"] == scope]

    def config(self, item):
        model = ProviderConfig if item["kind"] == "providers" else CheckConfig
        return model.model_validate({k: v for k, v in item.items() if k not in {"id", "kind", "scope"}})

    def provider_for(self, check, scope):
        provider = self.definitions.get(check.provider_id)
        if not provider or provider["kind"] != "providers" or provider["scope"] not in {None, scope}:
            raise ValueError("Select a shared analyzer or one belonging to this server")
        return self.config(provider)

    async def save(self, kind, scope, body, identifier=None):
        model = ProviderConfig if kind == "providers" else CheckConfig
        config = model.model_validate(body)
        async with self.lock:
            old = self.definitions.get(identifier) if identifier else None
            if identifier and (not old or old["scope"] != scope or old["kind"] != kind):
                raise ValueError("Definition not found in this scope")
            if kind == "checks":
                self.provider_for(config, scope)
            identifier = identifier or uuid4().hex
            row = {"id": identifier, "kind": kind, "scope": scope, **config.model_dump()}
            await self.db.execute("INSERT OR REPLACE INTO privacy_definitions VALUES (?,?,?,?)",
                                  (identifier, kind, scope, config.model_dump_json()))
            await self.db.commit()
            self.definitions[identifier] = row
            return copy.deepcopy(row)

    async def delete(self, kind, scope, identifier):
        async with self.lock:
            row = self.definitions.get(identifier)
            if not row or row["kind"] != kind or row["scope"] != scope:
                raise ValueError("Definition not found in this scope")
            if kind == "providers" and any(v.get("provider_id") == identifier for v in self.definitions.values()):
                raise ValueError("Remove checks using this analyzer first")
            if any(v["check_id"] == identifier for v in self.assignments.values()):
                raise ValueError("Remove this check from its servers before deleting it")
            await self.db.execute("DELETE FROM privacy_definitions WHERE id=?", (identifier,))
            await self.db.commit()
            self.definitions.pop(identifier)

    async def assign(self, scope, body, identifier=None):
        config = AssignmentConfig.model_validate(body)
        async with self.lock:
            definition = self.definitions.get(config.check_id)
            if not definition or definition["kind"] != "checks" or definition["scope"] not in {None, scope}:
                raise ValueError("Check not found in this scope")
            if identifier and (identifier not in self.assignments or self.assignments[identifier]["target_name"] != scope):
                raise ValueError("Assignment not found in this scope")
            if any(v["target_name"] == scope and v["check_id"] == config.check_id
                   and v["tool_name"] == config.tool_name and v["id"] != identifier
                   for v in self.assignments.values()):
                raise ValueError("This check is already assigned to that tool")
            identifier = identifier or uuid4().hex
            await self.db.execute("INSERT OR REPLACE INTO privacy_assignments VALUES (?,?,?,?,?)",
                (identifier, scope, config.check_id, config.tool_name, int(config.active)))
            await self.db.commit()
            row = {"id": identifier, "target_name": scope, **config.model_dump()}
            self.assignments[identifier] = row
            return dict(row)

    async def unassign(self, scope, identifier):
        async with self.lock:
            row = self.assignments.get(identifier)
            if not row or row["target_name"] != scope:
                raise ValueError("Assignment not found in this scope")
            await self.db.execute("DELETE FROM privacy_assignments WHERE id=?", (identifier,))
            await self.db.commit()
            self.assignments.pop(identifier)

    def effective(self, scope, tool_name):
        # One execution per check even when both all-tools and specific-tool bindings exist.
        return list(dict.fromkeys(v["check_id"] for v in self.assignments.values()
            if v["target_name"] == scope and v["active"] and v["tool_name"] in {"*", tool_name}))

    async def apply(self, scope, tool_name, result, mint, known_alias_spans):
        identifiers = self.effective(scope, tool_name)
        if not identifiers:
            return result
        try:
            with anyio.fail_after(15):
                async with httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False) as client:
                    result = copy.deepcopy(result)
                    budget = [0, 0]
                    for identifier in identifiers:
                        item = self.definitions.get(identifier)
                        if not item or item["scope"] not in {None, scope}:
                            raise CheckFailure("Required check missing")
                        config = self.config(item)
                        provider = self.provider_for(config, scope)
                        result = await self.scan_result(client, provider, config, result,
                            lambda value, prefix, path: mint(value, prefix, "privacy." + identifier + "." + path),
                            known_alias_spans, budget)
                    return result
        except (CheckFailure, TimeoutError, ValueError, TypeError, KeyError, RecursionError):
            # A partial copy, provider body or original response is never released on failure.
            return withheld()

    async def scan_result(self, client, provider, check, result, mint, known_alias_spans, budget):
        async def scan(text, path):
            if not text:
                return text
            budget[0] += 1
            budget[1] += len(text)
            if budget[0] > 256 or budget[1] > 2_000_000 or len(text) > MAX_TEXT:
                raise CheckFailure("Response exceeds scan budget")
            findings = await ADAPTERS[provider.adapter].analyze(client, provider, check, text)
            # Preserve coverage of every accepted detection, including overlapping spans.
            occupied = []
            alias_ranges = known_alias_spans(text)
            for finding in sorted(findings, key=lambda f: (f.start, f.end, f.entity_type)):
                if any(start <= finding.start and finding.end <= end for start, end in alias_ranges):
                    continue
                if any(finding.start < end and finding.end > start for start, end in alias_ranges):
                    raise CheckFailure("Detection overlaps an existing alias boundary")
                if occupied and finding.start < occupied[-1].end:
                    prior = occupied[-1]
                    winner = min((prior, finding), key=lambda f: (-f.score, -(f.end - f.start), f.entity_type))
                    occupied[-1] = Finding(prior.start, max(prior.end, finding.end), winner.entity_type, winner.score)
                else:
                    occupied.append(finding)
            if occupied and check.action == "block":
                raise CheckFailure("Check blocked the response")
            for finding in sorted(occupied, key=lambda f: f.start, reverse=True):
                value = text[finding.start:finding.end]
                prefix = PREFIXES.get(finding.entity_type, finding.entity_type.lower())
                alias = mint(value, prefix, path) if check.action == "mask" else "[REDACTED]"
                text = text[:finding.start] + alias + text[finding.end:]
            return text

        async def walk(value, path="", depth=0):
            if depth > 40:
                raise CheckFailure("Response nesting exceeds limit")
            if isinstance(value, dict):
                return {key: await walk(child, (path + "." if path else "") + key, depth + 1)
                        for key, child in value.items()}
            if isinstance(value, list):
                return [await walk(child, path, depth + 1) for child in value]
            if isinstance(value, str):
                if len(value) > MAX_TEXT:
                    raise CheckFailure("Text exceeds scan limit")
                parsed = try_parse_structured(value) if value.lstrip().startswith(("{", "[", "(")) else None
                if parsed:
                    return serialize(await walk(parsed.data, path, depth + 1), parsed.format)
                if check.field_paths and not any(path == p or path.startswith(p + ".") for p in check.field_paths):
                    return value
                return await scan(value, path)
            return value

        if "structuredContent" in result:
            result["structuredContent"] = await walk(result["structuredContent"])
        for block in result.get("content", []):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                text = block["text"]
                parsed = try_parse_structured(text) if len(text) <= MAX_TEXT else None
                # Field selection restricts structured values. Plain text is always scanned.
                block["text"] = serialize(await walk(parsed.data), parsed.format) if parsed else await scan(text, "text")
            elif block.get("type") == "resource" and isinstance(block.get("resource"), dict):
                resource = block["resource"]
                if isinstance(resource.get("text"), str):
                    resource["text"] = await scan(resource["text"], "resource.text")
        return result

    async def preview(self, scope, body):
        check = CheckConfig.model_validate(body["check"])
        provider = self.provider_for(check, scope)
        text = body.get("text", "")
        if not isinstance(text, str) or len(text) > MAX_TEXT:
            raise ValueError("Preview needs text of at most 100,000 characters")
        seen, counts = {}, {}
        def mint(value, prefix, path):
            key = (value, prefix)
            if key not in seen:
                counts[prefix] = counts.get(prefix, 0) + 1
                seen[key] = f"{prefix}_{counts[prefix]}"
            return seen[key]
        def alias_spans(value):
            return [(m.start(), m.end()) for alias in seen.values() for m in re.finditer(
                r"(?<![A-Za-z0-9_])" + re.escape(alias) + r"(?![A-Za-z0-9_])", value)]
        with anyio.fail_after(15):
            async with httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False) as client:
                try:
                    result = await self.scan_result(client, provider, check,
                        {"content": [{"type": "text", "text": text}]}, mint, alias_spans, [0, 0])
                    return {"text": result["content"][0]["text"], "blocked": False}
                except CheckFailure:
                    if check.action == "block":
                        return {"text": "Response withheld by this check or analyzer failure.", "blocked": True}
                    raise
