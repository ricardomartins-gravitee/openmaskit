"""Browser and real-model acceptance of shared and server-only privacy checks."""
import asyncio
import json
import re

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from playwright.sync_api import expect

from tests.e2e.conftest import (E2E_BASE_URL, E2E_MCP_PORT, api_client, install_stub_via_api,
    _spawn_openmaskit, _terminate, _wait_for_port_free, E2E_WEB_PORT)

pytestmark = pytest.mark.e2e
NAME = 'Sarah Johnson'


def panel(page):
    return page.locator('.privacy-panel')


def modal(page):
    return panel(page).locator('.modal-target')


def create_provider(page, endpoint, name='Local Presidio'):
    panel(page).get_by_role('button', name='Add Shared Analyzer').click()
    modal(page).get_by_role('textbox', name='Name', exact=True).fill(name)
    modal(page).get_by_label('Local service URL').fill(endpoint)
    modal(page).get_by_role('button', name='Save', exact=True).click()
    expect(panel(page).get_by_role('status')).to_have_text('Analyzer saved')


def create_check(page, name, local=False):
    panel(page).get_by_role('button', name='Add Server Check' if local else 'Add Shared Check', exact=True).click()
    modal(page).get_by_role('textbox', name='Name', exact=True).fill(name)
    # Use the real preview before saving; no synthetic aliases should persist.
    modal(page).get_by_text('Preview', exact=True).click()
    modal(page).get_by_label('Sample text').fill(NAME + ' called yesterday.')
    modal(page).get_by_role('button', name='Preview Check', exact=True).click()
    expect(modal(page).locator('.privacy-preview')).to_contain_text('person_1')
    expect(modal(page).locator('.privacy-preview')).not_to_contain_text(NAME)
    modal(page).get_by_role('button', name='Save', exact=True).click()
    expect(modal(page)).not_to_be_visible()


def test_shared_and_local_dashboard_lifecycle(dashboard_page, real_presidio, tmp_path):
    page = dashboard_page
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    install_stub_via_api('mail')
    install_stub_via_api('calendar')
    page.goto(E2E_BASE_URL + '/privacy')
    create_provider(page, real_presidio[0])
    panel(page).get_by_role('button', name='Check Connection').click()
    expect(panel(page).get_by_role('cell', name='Connected', exact=True)).to_be_visible()
    create_check(page, 'People')
    for target in ('mail', 'calendar'):
        page.goto(E2E_BASE_URL + f'/targets/{target}/tools')
        panel(page).get_by_label(re.compile(r'^Shared check')).select_option(label='People')
        panel(page).get_by_role('button', name='Use Check', exact=True).click()
        expect(panel(page).get_by_role('cell', name='Enabled', exact=True)).to_be_visible()
    panel(page).get_by_role('button', name='Pause', exact=True).click()
    expect(panel(page).get_by_role('cell', name='Paused', exact=True)).to_be_visible()
    page.goto(E2E_BASE_URL + '/targets/mail/tools')
    expect(panel(page).get_by_role('cell', name='Enabled', exact=True)).to_be_visible()
    create_check(page, 'Mail only', local=True)
    expect(panel(page).get_by_role('cell', name='Only this server', exact=True)).to_be_visible()
    with api_client() as client:
        assert 'Mail only' not in client.get('/api/privacy').text
        assert 'Mail only' not in client.get('/api/targets/calendar/privacy').text
        # No alias mappings created by previews.
        assert client.get('/api/targets/mail/mappings').json()['mappings'] == []
    # Local edit and deletion, including the meaningful in-use refusal.
    row = panel(page).get_by_role('row').filter(has=page.get_by_role('cell', name='Mail only', exact=True)).last
    row.get_by_role('button', name='Edit', exact=True).click()
    modal(page).get_by_role('textbox', name='Name', exact=True).fill('Mail only revised')
    modal(page).get_by_role('button', name='Save', exact=True).click()
    expect(modal(page)).not_to_be_visible()
    page.on('dialog', lambda dialog: dialog.accept())
    row = panel(page).get_by_role('row').filter(has=page.get_by_role('cell', name='Mail only revised', exact=True)).last
    row.get_by_role('button', name='Delete', exact=True).click()
    expect(panel(page).get_by_role('alert').first).to_contain_text('Remove this check')
    binding = panel(page).get_by_role('row').filter(has=page.get_by_role('cell', name='Mail only revised', exact=True)).first
    binding.get_by_role('button', name='Remove', exact=True).click()
    expect(panel(page).get_by_role('status')).to_have_text('Check removed from this server')
    row.get_by_role('button', name='Delete', exact=True).click()
    expect(panel(page).get_by_role('cell', name='Mail only revised', exact=True)).to_have_count(0)
    page.set_viewport_size({'width': 390, 'height': 844})
    expect(panel(page).get_by_role('heading', name='Privacy Checks', exact=True)).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
    # Library links and secondary controls use the dashboard theme.
    expect(panel(page).get_by_role('button', name='Pause', exact=True)).to_have_class(re.compile(r'\bbtn-small\b'))
    link = panel(page).get_by_role('link', name='Open shared library')
    assert link.evaluate('(el) => getComputedStyle(el).color') == 'rgb(88, 166, 255)'
    page.evaluate('window.scrollTo(0, 0)')
    page.screenshot(path=str(tmp_path / 'privacy-mobile.png'), full_page=True)
    assert errors == []


async def _mcp_call(target, tool, arguments):
    async with streamable_http_client(f'http://127.0.0.1:{E2E_MCP_PORT}/{target}/mcp') as (read, write, _):
        async with ClientSession(read, write) as client:
            await client.initialize()
            return (await client.call_tool(tool, arguments)).model_dump(mode='json', exclude_none=True)


def mcp_call(target, tool, arguments):
    # Playwright's synchronous session owns a loop on the pytest thread.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(lambda: asyncio.run(_mcp_call(target, tool, arguments))).result(timeout=30)


def configure(endpoint):
    with api_client() as client:
        provider = client.post('/api/privacy/providers/create', json={'name': 'Real Presidio', 'endpoint': endpoint})
        provider.raise_for_status()
        check = client.post('/api/privacy/checks/create', json={
            'name': 'People', 'provider_id': provider.json()['id'], 'entities': ['PERSON'], 'threshold': .7})
        check.raise_for_status()
        for target in ('mail', 'calendar'):
            client.post(f'/api/targets/{target}/privacy/assignments/create', json={'check_id': check.json()['id']}).raise_for_status()
        return check.json()


def test_real_presidio_mcp_restart_alias_roundtrip_and_outage(real_presidio, tmp_path):
    proc = _spawn_openmaskit(tmp_path)
    try:
        install_stub_via_api('mail')
        install_stub_via_api('calendar')
        configure(real_presidio[0])
        for target in ('mail', 'calendar'):
            result = mcp_call(target, 'lookup_user', {'name': NAME})
            assert NAME not in json.dumps(result)
            assert 'structuredContent' in result, result
            assert result['structuredContent']['name'] == 'person_1'
            assert result['structuredContent']['id'] == 1
            assert result['structuredContent']['email'] == 'alice@example.com'
        body = mcp_call('mail', 'echo', {'text': '😀 Olá, I spoke with Sarah Johnson yesterday.'})
        assert NAME not in json.dumps(body)
        assert 'person_1' in body['content'][0]['text']
        # Actual aliases resolve when the next MCP call goes upstream. An unassigned
        # tool lets us inspect that resolution without re-masking its response.
        with api_client() as client:
            for target in ('mail', 'calendar'):
                bindings = client.get(f'/api/targets/{target}/privacy').json()['assignments']
                for binding in bindings:
                    client.post(f'/api/targets/{target}/privacy/assignments/{binding["id"]}/update',
                        json={'check_id': binding['check_id'], 'tool_name': 'lookup_user'}).raise_for_status()
        echoed = mcp_call('mail', 'echo', {'text': 'person_1'})
        assert json.loads(echoed['content'][0]['text'])['received'] == NAME
        # A distinct new person on calendar doesn't change mail's person_1.
        other = mcp_call('calendar', 'lookup_user', {'name': 'John Smith'})
        assert 'John Smith' not in json.dumps(other)
        assert other['structuredContent']['name'] == 'person_2'
        _terminate(proc)
        _wait_for_port_free(E2E_WEB_PORT, 5)
        proc = _spawn_openmaskit(tmp_path)
        result = mcp_call('mail', 'lookup_user', {'name': NAME})
        assert result['structuredContent']['name'] == 'person_1'
        echoed = mcp_call('mail', 'echo', {'text': 'person_1'})
        assert json.loads(echoed['content'][0]['text'])['received'] == NAME
        real_presidio[1].terminate()
        real_presidio[1].wait(timeout=10)
        result = mcp_call('mail', 'lookup_user', {'name': NAME})
        assert result['isError']
        assert NAME not in json.dumps(result)
        assert 'structuredContent' not in result
    finally:
        _terminate(proc)


def test_privacy_dialog_controls_match_theme(dashboard_page, tmp_path):
    """Dropdowns and preview textareas must not fall back to browser defaults."""
    page = dashboard_page
    page.goto(E2E_BASE_URL + '/privacy')
    panel(page).get_by_role('button', name='Add Shared Analyzer').click()

    def assert_controls():
        reference = modal(page).get_by_role('textbox', name='Name', exact=True)
        theme = reference.evaluate('(el) => { const s = getComputedStyle(el); return {background: s.backgroundColor, color: s.color, radius: s.borderRadius}; }')
        controls = modal(page).locator('select:visible, textarea:visible')
        assert controls.count() > 0
        for control in controls.all():
            style = control.evaluate('(el) => { const s = getComputedStyle(el); return {background: s.backgroundColor, color: s.color, radius: s.borderRadius, appearance: s.appearance, image: s.backgroundImage}; }')
            assert {k: style[k] for k in theme} == theme
            assert control.bounding_box()['height'] >= 34
            if control.evaluate('(el) => el.tagName') == 'SELECT':
                assert style['appearance'] == 'none'
                assert style['image'] != 'none'
                control.focus()
                expect(control).to_have_css('border-color', 'rgb(88, 166, 255)')
    assert_controls()
    kind = modal(page).get_by_label(re.compile(r'^Type'))
    kind.select_option('http_spans')
    expect(kind).to_have_value('http_spans')
    kind.select_option('presidio')
    modal(page).get_by_role('textbox', name='Name', exact=True).fill('Synthetic analyzer')
    modal(page).get_by_label('Local service URL').fill('http://127.0.0.1:5002')
    modal(page).screenshot(path=str(tmp_path / 'analyzer-dialog.png'))
    modal(page).get_by_role('button', name='Save', exact=True).click()
    expect(modal(page)).not_to_be_visible()
    for scope in ('shared', 'server'):
        if scope == 'server':
            install_stub_via_api('mail')
            page.goto(E2E_BASE_URL + '/targets/mail/tools')
            panel(page).get_by_role('button', name='Add Server Check', exact=True).click()
        else:
            panel(page).get_by_role('button', name='Add Shared Check', exact=True).click()
        modal(page).get_by_text('Preview', exact=True).click()
        assert_controls()
        action = modal(page).get_by_label(re.compile(r'^Action'))
        action.select_option('redact')
        expect(action).to_have_value('redact')
        page.set_viewport_size({'width': 390, 'height': 844})
        assert_controls()
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        modal(page).get_by_role('button', name='Cancel', exact=True).click()
