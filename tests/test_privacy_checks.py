"""Regression and security acceptance for reusable analyzer-backed privacy checks."""
import copy
import json
from unittest.mock import Mock

import httpx
import pytest
import pytest_asyncio
import respx
from pydantic import ValidationError
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCResponse

from openmaskit.masking.engine import MaskingEngine
from openmaskit.masking.rules import MaskingRule
from openmaskit.masking.store import MaskingStore
from openmaskit.privacy.adapters import HttpSpanAdapter, CheckFailure
from openmaskit.privacy.models import ProviderConfig, CheckConfig
from openmaskit.privacy.registry import PrivacyChecks
from openmaskit.proxy.core import ProxyState, TargetState, _intercept_response_with_checks
from openmaskit.web.app import create_app

URL = "http://127.0.0.1:15002"
NAME = "Sarah Johnson"


def findings(text, entity="PERSON", score=.95):
    rows, pos = [], 0
    while (pos := text.find(NAME, pos)) >= 0:
        rows.append(dict(start=pos, end=pos + len(NAME), entity_type=entity, score=score))
        pos += len(NAME)
    return rows


@pytest_asyncio.fixture
async def setup(tmp_path):
    store = await MaskingStore.create(tmp_path / "checks.db")
    registry = await PrivacyChecks.create(store)
    provider = await registry.save("providers", None, dict(name="Local", endpoint=URL))
    check = await registry.save("checks", None, dict(name="People", provider_id=provider["id"]))
    state = ProxyState()
    state.store, state.privacy_checks = store, registry
    for name in ("mail", "calendar"):
        engine = MaskingEngine([], store, target_name=name)
        await engine.load_aliases()
        state.targets[name] = TargetState(name, engine, privacy_checks=registry)
    yield store, registry, state, provider, check
    await store.close()


async def scan(setup, result, tool="read", target="mail"):
    _, registry, state, _, check = setup
    if not registry.effective(target, tool):
        await registry.assign(target, dict(check_id=check["id"]))
    engine = state.targets[target].engine
    return await registry.apply(target, tool, result,
        lambda value, prefix, path: engine._get_or_create_alias(value, tool, path, prefix), engine.alias_spans)


@pytest.fixture
def analyzer():
    with respx.mock as router:
        endpoint = router.post(URL + "/analyze").mock(side_effect=lambda req:
            httpx.Response(200, json=findings(json.loads(req.content)["text"])))
        yield endpoint


@pytest.mark.parametrize("url", ["http://localhost:5002", "http://127.0.0.1:5002/analyze", "http://[::1]:5002"])
def test_local_provider_addresses(url):
    assert ProviderConfig(name="Local", endpoint=url).endpoint == url


@pytest.mark.parametrize("url", ["https://example.com", "http://192.168.1.4:5002", "http://169.254.169.254",
    "http://127.0.0.1.evil.test", "http://user:secret@127.0.0.1", "http://127.0.0.1?token=a",
    "http://127.0.0.1#fragment", "file:///tmp/model", "http://127.0.0.1:0", "http://127.0.0.1:bad"])
def test_provider_rejects_remote_or_ambiguous_endpoints(url):
    with pytest.raises(ValidationError):
        ProviderConfig(name="No", endpoint=url)


@pytest.mark.parametrize("change", [dict(entities=[]), dict(entities=["person"]), dict(entities=["<script>"]),
    dict(threshold=float("nan")), dict(threshold=1.1), dict(language=""), dict(action="execute"),
    dict(field_paths=["messages.*.body"]), dict(field_paths=["" ]), dict(scope="other")])
def test_invalid_check_settings(change):
    with pytest.raises(ValidationError):
        CheckConfig.model_validate(dict(name="Test", provider_id="provider", **change))


@pytest.mark.asyncio
async def test_scope_visibility_and_reference_boundaries(setup):
    _, registry, _, provider, shared = setup
    local_provider = await registry.save("providers", "mail", dict(name="Mail local", endpoint=URL))
    local = await registry.save("checks", "mail", dict(name="Mail private", provider_id=local_provider["id"]))
    assert {x["id"] for x in registry.visible("checks", None)} == {shared["id"]}
    assert local["id"] not in {x["id"] for x in registry.visible("checks", "calendar")}
    assert local_provider["id"] not in {x["id"] for x in registry.visible("providers", None)}
    for operation in (
        registry.assign("calendar", dict(check_id=local["id"])),
        registry.save("checks", None, dict(name="Bad", provider_id=local_provider["id"])),
        registry.save("checks", "calendar", dict(name="Bad", provider_id=local_provider["id"])),
        registry.save("checks", "calendar", dict(name="Bad", provider_id=provider["id"]), local["id"]),
        registry.delete("checks", "calendar", local["id"]),
    ):
        with pytest.raises(ValueError):
            await operation


@pytest.mark.asyncio
async def test_assignments_deduplicate_and_isolate_edits(setup):
    _, registry, _, _, check = setup
    first = await registry.assign("mail", dict(check_id=check["id"]))
    await registry.assign("mail", dict(check_id=check["id"], tool_name="read"))
    assert registry.effective("mail", "read") == [check["id"]]
    assert registry.effective("calendar", "read") == []
    with pytest.raises(ValueError):
        await registry.assign("mail", dict(check_id=check["id"]))
    with pytest.raises(ValueError):
        await registry.unassign("calendar", first["id"])
    await registry.assign("mail", dict(check_id=check["id"], active=False), first["id"])
    assert registry.effective("mail", "write") == []
    assert registry.effective("mail", "read") == [check["id"]]


@pytest.mark.asyncio
async def test_delete_in_use_definitions_is_refused(setup):
    _, registry, _, provider, check = setup
    with pytest.raises(ValueError):
        await registry.delete("providers", None, provider["id"])
    binding = await registry.assign("mail", dict(check_id=check["id"]))
    with pytest.raises(ValueError):
        await registry.delete("checks", None, check["id"])
    await registry.unassign("mail", binding["id"])
    await registry.delete("checks", None, check["id"])
    await registry.delete("providers", None, provider["id"])
    assert not registry.definitions


@pytest.mark.asyncio
async def test_persistence_restart_and_alias_roundtrip(tmp_path, analyzer):
    path = tmp_path / "restart.db"
    store = await MaskingStore.create(path)
    registry = await PrivacyChecks.create(store)
    provider = await registry.save("providers", "mail", dict(name="Local", endpoint=URL))
    check = await registry.save("checks", "mail", dict(name="Private", provider_id=provider["id"]))
    await registry.assign("mail", dict(check_id=check["id"]))
    engine = MaskingEngine([], store, target_name="mail")
    result = await registry.apply("mail", "read", {"content": [{"type": "text", "text": NAME}]},
        lambda value, prefix, path: engine._get_or_create_alias(value, "read", path, prefix), engine.alias_spans)
    alias = result["content"][0]["text"]
    await engine.flush_pending()
    await store.close()
    store = await MaskingStore.create(path)
    try:
        restored = await PrivacyChecks.create(store)
        engine = MaskingEngine([], store, target_name="mail")
        await engine.load_aliases()
        assert restored.effective("mail", "read") == [check["id"]]
        assert engine.unmask_arguments("send", {"to": alias}) == {"to": NAME}
        assert restored.visible("checks", None) == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_response_shapes_aliases_unicode_and_types(setup, analyzer):
    nested = {"messages": [{"body": "😀 Olá " + NAME, "count": 7, "flags": [False, None, ""]}]}
    source = {"structuredContent": copy.deepcopy(nested), "content": [
        {"type": "text", "text": json.dumps(nested)},
        {"type": "text", "text": repr(nested)},
        {"type": "text", "text": NAME + " then " + NAME},
        {"type": "resource", "resource": {"uri": "test://example", "text": NAME}},
    ]}
    original = copy.deepcopy(source)
    result = await scan(setup, source)
    assert source == original
    assert NAME not in json.dumps(result)
    assert result["structuredContent"]["messages"][0]["count"] == 7
    assert result["structuredContent"]["messages"][0]["flags"] == [False, None, ""]
    assert result["content"][2]["text"] == "person_1 then person_1"
    assert "😀 Olá person_1" == result["structuredContent"]["messages"][0]["body"]
    assert setup[2].targets["mail"].engine.unmask_arguments("write", {"person": "person_1"}) == {"person": NAME}


@pytest.mark.asyncio
async def test_field_selection_embedded_json_and_plain_text(setup, analyzer):
    _, registry, _, _, check = setup
    config = dict(name="Selected", provider_id=check["provider_id"], field_paths=["messages.body"])
    await registry.save("checks", None, config, check["id"])
    result = await scan(setup, {"structuredContent": {"messages": [{"body": NAME, "id": NAME}],
        "description": NAME}, "content": [{"type": "text", "text": NAME}]})
    assert result["structuredContent"]["messages"][0] == {"body": "person_1", "id": NAME}
    assert result["structuredContent"]["description"] == NAME
    assert result["content"][0]["text"] == "person_1"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["mask", "redact", "block"])
async def test_actions(setup, analyzer, action):
    _, registry, _, _, check = setup
    await registry.save("checks", None, dict(name="Action", provider_id=check["provider_id"], action=action), check["id"])
    result = await scan(setup, {"content": [{"type": "text", "text": NAME}]})
    assert NAME not in json.dumps(result)
    assert (result.get("isError") is True) == (action == "block")
    if action != "block":
        assert result["content"][0]["text"] == ("person_1" if action == "mask" else "[REDACTED]")


@pytest.mark.asyncio
async def test_aliases_do_not_cross_servers(setup, analyzer):
    first = await scan(setup, {"content": [{"type": "text", "text": NAME}]})
    other = setup[2].targets["calendar"].engine
    assert other.unmask_arguments("write", {"name": "person_1"}) == {"name": "person_1"}
    second = await scan(setup, {"content": [{"type": "text", "text": NAME}]}, target="calendar")
    assert first == second
    assert other.alias_cache == {"person_1": NAME}


@pytest.mark.asyncio
@pytest.mark.parametrize("rows", [None, {}, [None], [{"start": -1, "end": 2, "entity_type": "PERSON", "score": .9}],
    [{"start": True, "end": 2, "entity_type": "PERSON", "score": .9}],
    [{"start": 0, "end": 99, "entity_type": "PERSON", "score": .9}],
    [{"start": 0, "end": 2, "entity_type": "PERSON", "score": float("nan")}],
    [{"start": 0, "end": 2, "entity_type": "PERSON", "score": True}],
    [{"start": 2, "end": 2, "entity_type": "PERSON", "score": .9}]])
async def test_invalid_provider_response_never_releases_original(setup, rows):
    with respx.mock:
        respx.post(URL + "/analyze").mock(return_value=httpx.Response(200, text=json.dumps(rows)))
        result = await scan(setup, {"content": [{"type": "text", "text": NAME}]})
    assert result["isError"]
    assert NAME not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "server", "invalid_json", "redirect", "oversize"])
async def test_http_failures_withhold_all_parallel_surfaces(setup, monkeypatch, failure):
    with respx.mock as router:
        route = router.post(URL + "/analyze")
        if failure == "timeout":
            route.mock(side_effect=httpx.ReadTimeout("sensitive analyzer diagnostic " + NAME))
        else:
            route.mock(return_value={"server": httpx.Response(500, text=NAME),
                "invalid_json": httpx.Response(200, text=NAME),
                "redirect": httpx.Response(307, headers={"location": "https://example.com/analyze"}),
                "oversize": httpx.Response(200, text="x" * 200)}[failure])
        if failure == "oversize":
            monkeypatch.setattr("openmaskit.privacy.adapters.MAX_RESPONSE", 100)
        result = await scan(setup, {"structuredContent": {"name": NAME}, "content": [
            {"type": "text", "text": NAME}, {"type": "text", "text": "Also " + NAME}]})
        assert len(router.calls) == 1
    assert result["isError"]
    assert NAME not in json.dumps(result)
    assert "structuredContent" not in result


@pytest.mark.asyncio
async def test_compatible_adapter_exact_endpoint_threshold_and_entity_filter():
    provider = ProviderConfig(name="Compatible", adapter="http_spans", endpoint=URL + "/detect")
    check = CheckConfig(name="Test", provider_id="id", threshold=.8)
    with respx.mock as router:
        route = router.post(URL + "/detect").mock(return_value=httpx.Response(200, json={"findings": [
            dict(start=0, end=2, entity_type="PERSON", score=.9),
            dict(start=2, end=4, entity_type="PERSON", score=.6),
            dict(start=4, end=6, entity_type="LOCATION", score=.99)]}))
        async with httpx.AsyncClient() as client:
            found = await HttpSpanAdapter().analyze(client, provider, check, "abcdef")
        assert [(f.start, f.end) for f in found] == [(0, 2)]
        assert json.loads(route.calls[0].request.content)["score_threshold"] == .8


@pytest.mark.asyncio
async def test_overlap_coverage_does_not_expose_longer_name(setup):
    with respx.mock:
        respx.post(URL + "/analyze").mock(return_value=httpx.Response(200, json=[
            dict(start=0, end=5, entity_type="PERSON", score=.99),
            dict(start=0, end=len(NAME), entity_type="PERSON", score=.8)]))
        result = await scan(setup, {"content": [{"type": "text", "text": NAME}]})
    assert result["content"][0]["text"] == "person_1"
    assert setup[2].targets["mail"].engine.alias_cache["person_1"] == NAME


@pytest.mark.asyncio
async def test_known_alias_is_preserved_and_crossing_span_is_withheld(setup):
    engine = setup[2].targets["mail"].engine
    alias = engine._get_or_create_alias(NAME, "read", "name", "person")
    with respx.mock as router:
        route = router.post(URL + "/analyze").mock(return_value=httpx.Response(200, json=[
            dict(start=0, end=len(alias), entity_type="PERSON", score=.99)]))
        result = await scan(setup, {"content": [{"type": "text", "text": alias}]})
        assert result["content"][0]["text"] == alias
        route.mock(return_value=httpx.Response(200, json=[dict(start=0, end=len(alias) + 4, entity_type="PERSON", score=.99)]))
        result = await scan(setup, {"content": [{"type": "text", "text": alias + " " + NAME}]})
    assert result["isError"] and NAME not in json.dumps(result)


@pytest.mark.asyncio
async def test_size_limits_and_partial_failure(setup, analyzer):
    result = await scan(setup, {"content": [{"type": "text", "text": NAME}, {"type": "text", "text": "x" * 100001}]})
    assert result["isError"] and NAME not in json.dumps(result)
    value = NAME
    for _ in range(45):
        value = [value]
    result = await scan(setup, {"structuredContent": {"names": value}})
    assert result["isError"]
    result = await scan(setup, {"structuredContent": {"names": [NAME] * 257}})
    assert result["isError"]


@pytest.mark.asyncio
async def test_preview_is_ephemeral(setup, analyzer):
    store, registry, state, provider, _ = setup
    before = dict(state.targets["mail"].engine.alias_cache)
    result = await registry.preview(None, dict(check=dict(name="Draft", provider_id=provider["id"]), text=NAME))
    assert result == dict(text="person_1", blocked=False)
    assert state.targets["mail"].engine.alias_cache == before
    async with store._db.execute("SELECT COUNT(*) FROM mappings") as cursor:
        assert (await cursor.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_real_proxy_interceptor_masks_and_records_blocked_status(setup, analyzer):
    _, registry, state, _, check = setup
    target = state.targets["mail"]
    target._emit_traffic = emitted = Mock()
    await registry.assign("mail", dict(check_id=check["id"]))
    target.pending_tool_calls[1] = dict(tool_name="read", request_id="1", timestamp=0)
    message = SessionMessage(message=JSONRPCMessage(root=JSONRPCResponse(jsonrpc="2.0", id=1,
        result={"content": [{"type": "text", "text": NAME}]})))
    result = await _intercept_response_with_checks(message, target)
    assert result.message.root.result["content"][0]["text"] == "person_1"
    await registry.save("checks", None, dict(name="Block", provider_id=check["provider_id"], action="block"), check["id"])
    target.pending_tool_calls[2] = dict(tool_name="read", request_id="2", timestamp=0)
    message = SessionMessage(message=JSONRPCMessage(root=JSONRPCResponse(jsonrpc="2.0", id=2,
        result={"content": [{"type": "text", "text": NAME}]})))
    result = await _intercept_response_with_checks(message, target)
    assert result.message.root.result["isError"]
    assert emitted.call_args.args[0].status == "error"


@pytest_asyncio.fixture
async def client(setup):
    app = create_app(setup[2], csrf_token="acceptance-token")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:9473",
        headers={"Origin": "http://127.0.0.1:9473", "X-CSRF-Token": "acceptance-token"}) as client:
        yield client


@pytest.mark.asyncio
async def test_dashboard_routes_scoped_crud_assignment_and_preview(client, setup, analyzer):
    for path in ("/privacy", "/targets/mail/tools"):
        response = await client.get(path)
        assert response.status_code == 200
        assert "<!-- PRIVACY_CHECKS_PANEL -->" not in response.text
        assert 'x-data="privacyChecksPanel()"' in response.text
    body = dict(name="Private", provider_id=setup[3]["id"])
    response = await client.post("/api/targets/mail/privacy/checks/create", json=body)
    assert response.status_code == 201
    identifier = response.json()["id"]
    response = await client.post("/api/targets/mail/privacy/assignments/create", json=dict(check_id=identifier))
    assert response.status_code == 200
    binding = response.json()
    assert identifier not in json.dumps((await client.get("/api/privacy")).json())
    assert identifier not in json.dumps((await client.get("/api/targets/calendar/privacy")).json())
    assert (await client.post("/api/targets/calendar/privacy/assignments/create", json=dict(check_id=identifier))).status_code == 400
    assert (await client.post("/api/targets/mail/privacy/assignments/" + binding["id"] + "/update",
        json=dict(check_id=identifier, active=False))).status_code == 200
    assert (await client.post("/api/targets/mail/privacy/preview", json=dict(check=body, text=NAME))).json()["text"] == "person_1"
    assert (await client.post("/api/targets/mail/privacy/checks/" + identifier + "/delete", json={})).status_code == 400
    assert (await client.post("/api/targets/mail/privacy/assignments/" + binding["id"] + "/delete", json={})).status_code == 200
    assert (await client.post("/api/targets/mail/privacy/checks/" + identifier + "/delete", json={})).status_code == 200


@pytest.mark.asyncio
async def test_api_origin_csrf_and_invalid_settings(client):
    body = dict(name="Test", endpoint=URL)
    assert (await client.post("/api/privacy/providers/create", json=body, headers={"Origin": "https://evil.example"})).status_code == 403
    assert (await client.post("/api/privacy/providers/create", json=body, headers={"X-CSRF-Token": "wrong"})).status_code == 403
    response = await client.post("/api/privacy/providers/create", json=dict(name="Bad", endpoint="https://remote.example"))
    assert response.status_code == 400 and response.json()["details"]
    assert (await client.get("/api/targets/missing/privacy")).status_code == 400
    assert (await client.post("/api/privacy/unknown/create", json={})).status_code == 400


@pytest.mark.parametrize('result', [
    {'content': None}, {'content': ['Sarah Johnson']},
    {'content': [{'type': 'text', 'text': {'name': NAME}}]},
    {'content': [{'type': 'text'}]},
    {'content': [{'type': 'resource', 'resource': None}]},
    {'content': [{'type': 'resource', 'resource': {'text': [NAME]}}]},
    {'content': [{'type': 'text', 'text': repr({'names': {NAME}})}]},
    {'content': [{'type': 'text', 'text': repr({'name': NAME.encode()})}]},
])
async def test_uninspectable_results_fail_closed(setup, analyzer, result):
    masked = await scan(setup, result)
    assert masked['isError']
    assert NAME not in json.dumps(masked)


async def test_embedded_and_nested_json_obey_field_paths_preserve_keys(setup, analyzer):
    _, registry, _, _, check = setup
    await registry.save('checks', None, dict(name='Selected', provider_id=check['provider_id'],
        field_paths=['messages.body']), check['id'])
    data = {'messages': [{'body': NAME, 'id': NAME}], 'name': NAME}
    result = await scan(setup, {'structuredContent': {'messages': json.dumps(data['messages'])},
        'content': [{'type': 'resource', 'resource': {'uri': 'fixture://local', 'text': json.dumps(data)}}]})
    assert json.loads(result['structuredContent']['messages'])[0] == {'body': 'person_1', 'id': NAME}
    embedded = json.loads(result['content'][0]['resource']['text'])
    assert embedded == {'messages': [{'body': 'person_1', 'id': NAME}], 'name': NAME}
    await registry.save('checks', None, dict(name='All', provider_id=check['provider_id']), check['id'])
    result = await scan(setup, {'content': [{'type': 'text', 'text': repr({1: NAME})}]})
    assert result['content'][0]['text'] == "{1: 'person_1'}"


async def test_traversal_and_total_character_budgets(setup, analyzer):
    assert (await scan(setup, {'structuredContent': {'values': [None] * 10001}}))['isError']
    assert (await scan(setup, {'structuredContent': {'values': ['x' * 100000] * 21}}))['isError']


async def test_paused_unassigned_and_unselected_tools_do_not_call_analyzer(setup):
    _, registry, state, _, check = setup
    binding = await registry.assign('mail', dict(check_id=check['id'], tool_name='read'))
    result = {'content': [{'type': 'text', 'text': NAME}]}
    with respx.mock as router:
        engine = state.targets['mail'].engine
        for scope, tool in [('calendar', 'read'), ('mail', 'send')]:
            assert await registry.apply(scope, tool, result, None, engine.alias_spans) is result
        await registry.assign('mail', dict(check_id=check['id'], tool_name='read', active=False), binding['id'])
        assert await registry.apply('mail', 'read', result, None, engine.alias_spans) is result
        assert not router.calls


async def test_missing_required_definition_or_provider_is_withheld(setup, analyzer):
    _, registry, _, provider, check = setup
    await registry.assign('mail', dict(check_id=check['id']))
    registry.definitions.pop(provider['id'])
    assert (await scan(setup, {'content': [{'type': 'text', 'text': NAME}]}))['isError']
    registry.definitions.pop(check['id'])
    assert (await scan(setup, {'content': [{'type': 'text', 'text': NAME}]}))['isError']
    assert not analyzer.calls


async def test_shared_update_reaches_both_servers_local_update_is_isolated(setup, analyzer):
    _, registry, _, _, check = setup
    await scan(setup, {'content': [{'type': 'text', 'text': NAME}]}, target='mail')
    await scan(setup, {'content': [{'type': 'text', 'text': NAME}]}, target='calendar')
    await registry.save('checks', None, dict(name='Redact', provider_id=check['provider_id'], action='redact'), check['id'])
    for server in ('mail', 'calendar'):
        assert (await scan(setup, {'content': [{'type': 'text', 'text': NAME}]}, target=server))['content'][0]['text'] == '[REDACTED]'
    local = await registry.save('checks', 'mail', dict(name='Mail block', provider_id=check['provider_id'], action='block'))
    await registry.assign('mail', dict(check_id=local['id']))
    # Redaction runs first, so the later check sees only already redacted text.
    assert not (await scan(setup, {'content': [{'type': 'text', 'text': NAME}]})).get('isError')
    assert local['id'] not in registry.effective('calendar', 'read')


async def test_binary_media_and_resource_links_are_preserved(setup, analyzer):
    source = {'content': [{'type': 'image', 'data': 'c3ludGhldGlj', 'mimeType': 'image/png'},
        {'type': 'audio', 'data': 'c3ludGhldGlj', 'mimeType': 'audio/wav'},
        {'type': 'resource_link', 'uri': 'fixture://local', 'name': 'link'},
        {'type': 'resource', 'resource': {'uri': 'fixture://blob', 'blob': 'c3ludGhldGlj'}},
        {'type': 'text', 'text': NAME}]}
    result = await scan(setup, source)
    assert result['content'][:-1] == source['content'][:-1]
    assert result['content'][-1]['text'] == 'person_1'


async def test_existing_rules_and_check_aliases_work_together(setup, analyzer):
    _, registry, state, _, check = setup
    target = state.targets['mail']
    target.engine.rules.append(MaskingRule('*', 'email', 'email'))
    await registry.assign('mail', dict(check_id=check['id']))
    target.pending_tool_calls[3] = dict(tool_name='read', request_id='3', timestamp=0)
    data = {'email': 'alice@example.test', 'body': NAME}
    message = SessionMessage(message=JSONRPCMessage(root=JSONRPCResponse(jsonrpc='2.0', id=3,
        result={'content': [{'type': 'text', 'text': json.dumps(data)}]})))
    result = await _intercept_response_with_checks(message, target)
    assert json.loads(result.message.root.result['content'][0]['text']) == {'email': 'email_1', 'body': 'person_1'}
    assert target.engine.unmask_arguments('send', {'to': 'email_1', 'name': 'person_1'}) == {'to': 'alice@example.test', 'name': NAME}


async def test_malformed_content_cannot_break_proxy_error_delivery(setup, analyzer):
    _, registry, state, _, check = setup
    target = state.targets['mail']
    await registry.assign('mail', dict(check_id=check['id']))
    target.pending_tool_calls[9] = dict(tool_name='read', request_id='9', timestamp=0)
    message = SessionMessage(message=JSONRPCMessage(root=JSONRPCResponse(jsonrpc='2.0', id=9,
        result={'content': None, 'structuredContent': {'name': NAME}})))
    result = await _intercept_response_with_checks(message, target)
    assert result.message.root.result['isError']
    assert NAME not in json.dumps(result.message.root.result)


@pytest.mark.parametrize('content', [
    [{'type': 'unknown', 'text': NAME}],
    [{'type': 'resource', 'resource': {'uri': 'fixture://local'}}],
    [{'type': 'text', 'text': ''}] * 10001,
])
async def test_unknown_or_excessive_content_is_withheld(setup, analyzer, content):
    result = await scan(setup, {'content': content})
    assert result['isError']


async def test_preview_failures_limits_and_block_results(setup, analyzer):
    _, registry, _, provider, check = setup
    with pytest.raises(ValueError):
        await registry.preview(None, dict(check=dict(name='Draft', provider_id=provider['id']), text='x' * 100001))
    result = await registry.preview(None, dict(check=dict(name='Draft', provider_id=provider['id'], action='block'), text=NAME))
    assert result['blocked'] and NAME not in result['text']
    analyzer.mock(return_value=httpx.Response(500, text=NAME))
    with pytest.raises(CheckFailure):
        await registry.preview(None, dict(check=dict(name='Draft', provider_id=provider['id']), text=NAME))
    with pytest.raises(ValueError):
        await registry.assign('mail', dict(check_id=check['id']), 'missing')


async def test_adapter_limit_is_enforced_before_http():
    async with httpx.AsyncClient() as client:
        with pytest.raises(CheckFailure):
            await HttpSpanAdapter().analyze(client, ProviderConfig(name='Local', endpoint=URL),
                CheckConfig(name='Draft', provider_id='id'), 'x' * 100001)


async def test_oversized_structured_leaf_is_withheld_even_outside_selected_paths(setup, analyzer):
    _, registry, _, _, check = setup
    await registry.save('checks', None, dict(name='Selected', provider_id=check['provider_id'],
        field_paths=['body']), check['id'])
    result = await scan(setup, {'structuredContent': {'other': 'x' * 100001}})
    assert result['isError']


async def test_overall_deadline_withholds_slow_successful_analyzer(setup, monkeypatch):
    import anyio
    original = anyio.fail_after
    monkeypatch.setattr('openmaskit.privacy.registry.anyio.fail_after', lambda *args: original(.02))
    async def slow(request):
        await anyio.sleep(.2)
        return httpx.Response(200, json=[])
    with respx.mock:
        respx.post(URL + '/analyze').mock(side_effect=slow)
        result = await scan(setup, {'content': [{'type': 'text', 'text': NAME}]})
    assert result['isError'] and NAME not in json.dumps(result)


async def test_multiple_assignments_run_once_and_provider_explicit_analyze_url(setup, analyzer):
    _, registry, _, provider, check = setup
    await registry.save('providers', None, dict(name='Local', endpoint=URL + '/analyze'), provider['id'])
    await registry.assign('mail', dict(check_id=check['id']))
    await registry.assign('mail', dict(check_id=check['id'], tool_name='read'))
    result = await scan(setup, {'content': [{'type': 'text', 'text': NAME}]})
    assert result['content'][0]['text'] == 'person_1'
    assert len(analyzer.calls) == 1


async def test_field_path_segments_do_not_match_literal_dotted_keys(setup, analyzer):
    _, registry, _, _, check = setup
    await registry.save('checks', None, dict(name='Selected', provider_id=check['provider_id'],
        field_paths=['messages.body']), check['id'])
    result = await scan(setup, {'structuredContent': {'messages.body': NAME,
        'messages': [{'body': NAME, 'body.suffix': NAME}]}})
    assert result['structuredContent'] == {'messages.body': NAME,
        'messages': [{'body': 'person_1', 'body.suffix': NAME}]}


@pytest.mark.parametrize('adapter, configured, requested', [
    ('presidio', URL + '/', URL + '/analyze'),
    ('presidio', URL + '/analyze/', URL + '/analyze'),
    ('http_spans', URL + '/detect/', URL + '/detect/'),
])
async def test_adapter_preserves_meaningful_endpoint_paths(adapter, configured, requested):
    provider = ProviderConfig(name='Local', adapter=adapter, endpoint=configured)
    rows = findings(NAME)
    with respx.mock as router:
        route = router.post(requested).mock(return_value=httpx.Response(200,
            json={'findings': rows} if adapter == 'http_spans' else rows))
        async with httpx.AsyncClient() as client:
            result = await HttpSpanAdapter().analyze(client, provider,
                CheckConfig(name='Draft', provider_id='id'), NAME)
        assert len(result) == 1
        assert len(route.calls) == 1
