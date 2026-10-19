# Reusable privacy checks

The dashboard's **Checks** page contains shared analyzers and check definitions. Each MCP server's tools page has a **Privacy Checks** panel using the same tables, Add/Edit forms and modals as the existing rules and mappers.

## Scope and reuse

- A shared analyzer is registered once and can serve checks used by multiple MCP servers.
- A shared check defines entity types, language, confidence, action and optional field paths. It only runs on servers where it is explicitly assigned.
- A server can create its own analyzers and checks. These definitions are visible only in that server's dashboard scope and cannot be referenced by another server or by the shared library.
- Shared definitions can be edited in the library. Server pages can enable, pause or remove their own assignments without changing other servers.
- Assignments can cover all tools on one server or one selected tool. Duplicate effective assignments run the check once.
- MCP servers do not gain a tool or API credential allowing them to register or alter checks. This is dashboard configuration, protected by the existing Origin and CSRF controls.
- Definitions and bindings persist in additive local SQLite tables and reload at startup. Real text samples and detections are not stored in the shared definitions.

## First adapter: Presidio

Register an already running local Presidio Analyzer, for example `http://127.0.0.1:5002`. The adapter calls `/analyze`; a URL already ending in `/analyze` is also accepted. Use **Check Connection** for a synthetic request, then create a check, select `PERSON` or other supported entity types, and preview synthetic text.

Presidio installation and model provisioning are separate manual steps. See the [official installation guide](https://presidio.dataprivacystack.org/installation/). Registering an analyzer does not install packages, download models, start a process or create a scheduled task. The analyzer needs to be available when a configured MCP response is checked.

This initial implementation supports loopback HTTP(S) endpoints only. Raw response text is sent to the configured local analyzer. Environment HTTP proxies and HTTP redirects are disabled. Remote/cloud analyzers, endpoint credentials and managed analyzer startup are not included.

## Other analyzers

The adapter registry separates detection from alias generation. The **Compatible analyzer** option calls the exact configured local URL with:

```json
{"text":"example","language":"en","entities":["PERSON"],"score_threshold":0.7}
```

Its response must be:

```json
{"findings":[{"start":0,"end":7,"entity_type":"PERSON","score":0.95}]}
```

Offsets are Python/Unicode code-point offsets into the exact submitted text, with an exclusive end. Byte and UTF-16 offsets require an adapter conversion. Arbitrary products are not automatically compatible with this format. Add their translation in `privacy/adapters.py` and register their adapter type explicitly.

The analyzer returns positions and entity types. OpenMaskit's existing per-server alias store generates and resolves stable aliases, including aliases produced by existing rules. Shared configuration does not share identity mappings between MCP servers. Redaction is irreversible. Block action withholds the whole result when a match is detected.

## Processing and limits

Existing rules and mappers run first. Enabled checks then inspect text content, recursively nested structured string values, JSON/Python-repr text and embedded resource text. Dictionary keys and non-string scalar types are preserved. Field paths fan out through lists. Optional paths restrict structured values; unstructured text results are always scanned in full.

Returned findings are validated for bounds, scores and entity types. Overlapping accepted spans are combined so a shorter detection cannot expose the remainder of a longer accepted detection. Existing aliases are preserved; a detection crossing an alias boundary withholds the response.

The initial bounds are 100,000 characters per text, 256 analyzed strings and 2,000,000 characters per response across checks, 40 nested containers, 1 MiB per analyzer response, a five-second HTTP timeout and a fifteen-second overall response-check deadline. Required check failures, invalid findings, exceeded limits and blocking matches return a new error result containing no original content. No partial result is released.

Preview uses transient aliases and does not write samples or alias mappings to SQLite. A failed preview does not mean the text is safe. Detection quality depends on the configured models, language, entity types and confidence threshold; false positives and missed entities remain possible.

Images, audio, binary attachments, resource URIs, protocol metadata, tool schemas, requests/arguments and intermediate streaming notifications are not inspected by these text checks. Output checks do not provide protection for data already forwarded outside the proxy or for data printed by upstream software. The normal encrypted local traffic audit behavior is unchanged.

## Delivery

This feature is prepared on a separate branch for review. It is not installed into the running service. Local installation remains manual, and there are no local scheduled jobs. GitHub alone maintains the source fork on its existing schedule. Browser checks and feature regression tests have not been added or run for this change.
