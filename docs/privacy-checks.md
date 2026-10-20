# Reusable privacy checks

The dashboard's **Analyzers** page groups check definitions under the analyzer that runs them. Each analyzer card shows its type, local URL, connection status and checks, with **Add Check** directly on the card. A check's analyzer is selected by that context and remains fixed when editing the check.

Each MCP server's tools page has a **Privacy Checks** panel beside the existing rules and mappers. **Add Check** opens an analyzer/check picker and a choice of all tools or a specific tool. Assignments can be edited, paused, enabled or removed on that server without editing the shared definition. The analyzer cards on that page also offer **Use on this Server** and **Create MCP-only Check**. New server-only checks are saved and enabled for the chosen tools; a failed assignment can be retried without creating a duplicate definition.

## Dashboard workflow

1. Open **Analyzers**, add your locally running analyzer and check its connection.
2. On its card, choose **Add Check** and configure detection, action and optional structured fields.
3. Open an MCP server's tools page, choose **Add Check**, select that analyzer and check, then select the tools it should cover.
4. To configure something just for that server, use **Create MCP-only Check** under the appropriate analyzer. **Add MCP-only Analyzer** registers a connection visible only in that server's scope.

Shared definitions are edited in the analyzer library. Server pages show them for reuse, while their own analyzer/check definitions remain editable only within that server.

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

### Start Presidio with Docker Compose

Open Docker Desktop, then run these commands from the root of this fork:

```sh
docker compose -f compose.presidio.yaml up -d --wait --wait-timeout 300
```

The optional [Compose file](../compose.presidio.yaml) downloads the official
`ghcr.io/data-privacy-stack/presidio-analyzer:2.2.364` image and waits for its
`/health` endpoint. It publishes only `127.0.0.1:5002`, uses the image's default
English model, and has no automatic restart policy. No local maintenance job
is installed. The first image download can take several minutes. This runs
Presidio beside the existing OpenMaskit service; no OpenMaskit restart is needed.
Only the Analyzer is needed: OpenMaskit performs aliasing and redaction itself.

In **Analyzers → Add Analyzer**, enter:

| Field | Value |
|---|---|
| Name | Local Presidio |
| Type | Presidio |
| Local service URL | `http://127.0.0.1:5002` |

Save, then choose **Check Connection**. On the analyzer card, choose **Add Check**:
name `People's names`, Detect `PERSON`, language `en`, minimum confidence `0.7`,
action **Replace with reversible aliases**, and leave structured fields empty.
Use **Preview** with `John Smith met Sarah Johnson yesterday.`, then save.
Enable the saved check from the relevant MCP server's **Privacy Checks → Add Check**.

To stop and remove only this Compose setup:

```sh
docker compose -f compose.presidio.yaml down
```

Run the `up` command again to start it later. If an assigned analyzer is stopped,
OpenMaskit withholds responses requiring that check until it becomes available
or you pause that assignment.

If port 5002 is occupied by the standalone Presidio container from the earlier
instructions, stop that container first with `docker stop presidio-analyzer`.
The Compose setup does not replace or delete an existing standalone container.

The image and API are documented in the [official installation guide](https://presidio.dataprivacystack.org/installation/)
and [Presidio Analyzer source](https://github.com/data-privacy-stack/presidio/blob/2.2.364/presidio-analyzer/app.py).

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

The initial bounds are 100,000 characters per text, 256 analyzed strings and 2,000,000 characters per response across checks, 40 nested containers, 10,000 structured nodes per check and 10,000 content blocks, 1 MiB per analyzer response, a five-second HTTP timeout and a fifteen-second overall response-check deadline. Required check failures, invalid findings, exceeded limits and blocking matches return a new error result containing no original content. No partial result is released.

Preview uses transient aliases and does not write samples or alias mappings to SQLite. A failed preview does not mean the text is safe. Detection quality depends on the configured models, language, entity types and confidence threshold; false positives and missed entities remain possible.

Images, audio, binary attachments, resource URIs, protocol metadata, tool schemas, requests/arguments and intermediate streaming notifications are not inspected by these text checks. Output checks do not provide protection for data already forwarded outside the proxy or for data printed by upstream software. The normal encrypted local traffic audit behavior is unchanged.

## Delivery

Local installation remains manual, and there are no local scheduled jobs. GitHub alone maintains the source fork on its existing schedule. Regression tests cover scope, persistence, aliases, Unicode and overlapping spans, response formats, invalid settings, analyzer failures and limits. Browser acceptance uses real Presidio, its English spaCy model, and synthetic MCP/database data; see [the acceptance instructions](../tests/e2e/README.md). Text detection is probabilistic: passing these tests does not guarantee that a model will recognize every real name. Unsupported structured value types and malformed or unknown content blocks are withheld when a check is required.
