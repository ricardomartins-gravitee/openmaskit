"""Exercise real startup and the MCP proxy using synthetic data only."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

PATHS = ['author', 'recipients', 'ccList', 'messages.author',
         'messages.recipients', 'messages.ccList', 'identities.name']


async def upstream():
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server
    from mcp import types
    server = Server('synthetic-mail')

    @server.list_tools()
    async def tools():
        return [types.Tool(name='getMessage', description='Synthetic privacy fixture',
                           inputSchema={'type': 'object', 'properties': {'oversized': {'type': 'boolean'}}})]

    @server.call_tool()
    async def call(name, arguments):
        data = {'author': 'Alice Synthetic <alice@example.test>',
                'recipients': 'Bob Synthetic <bob@example.test>', 'ccList': '',
                'body': 'x' * (1_048_577 if arguments.get('oversized') else 20)}
        return [types.TextContent(type='text', text=json.dumps(data))]

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def proxy():
    import openmaskit.__main__ as main
    # Isolate the fixture from the user's installation identifier.
    main._load_installation_id = lambda: 'synthetic-privacy-regression'
    sys.argv = ['mcp-privacy-proxy', *sys.argv[2:]]
    main.main()


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


async def probe(web, mcp):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    with urllib.request.urlopen(f'http://127.0.0.1:{web}/api/targets/synthetic/rules') as response:
        assert len(json.load(response)['rules']) == 7, 'Saved rules were not restored at startup'
    async with streamable_http_client(f'http://127.0.0.1:{mcp}/synthetic/mcp') as (read, write, _):
        async with ClientSession(read, write) as client:
            await client.initialize()
            ordinary = await client.call_tool('getMessage', {})
            data = json.loads(ordinary.content[0].text)
            assert data['author'].startswith('header_'), 'Sender was exposed'
            assert data['recipients'].startswith('header_'), 'Recipient was exposed'
            assert data['ccList'] == '', 'Empty header received an alias'
            large = await client.call_tool('getMessage', {'oversized': True})
            assert large.isError, 'Oversized response was not withheld'
            assert 'Alice Synthetic' not in str(large), 'Original response escaped the size guard'


async def initialize_store(path):
    from openmaskit.masking.rules import MaskingRule
    from openmaskit.masking.store import MaskingStore
    store = await MaskingStore.create(path)
    try:
        await store.install_server('synthetic', 'Synthetic mail', 'custom', None,
            {'transport': 'stdio', 'command': sys.executable, 'args': [str(Path(__file__).resolve()), '--upstream']})
        for field in PATHS:
            await store.add_rule(MaskingRule('*', field, 'header'), target_name='synthetic')
    finally:
        await store.close()


def run():
    from cryptography.fernet import Fernet
    state = Path(os.environ.get('PRIVACY_SMOKE_STATE', '.runtime/smoke')).resolve()
    state.mkdir(parents=True, exist_ok=True)
    os.environ['OPENMASKIT_ENCRYPTION_KEY'] = Fernet.generate_key().decode()
    os.environ['OPENMASKIT_DISABLE_MARKETPLACE'] = '1'
    os.environ['OPENMASKIT_HOST'] = '127.0.0.1'
    os.environ['OPENMASKIT_LOG_LEVEL'] = 'ERROR'
    with tempfile.TemporaryDirectory(dir=state) as directory:
        directory = Path(directory)
        db = directory / 'store.db'
        asyncio.run(initialize_store(db))
        web, mcp = free_port(), free_port()
        config = directory / 'empty.yaml'
        config.write_text('')
        env = {**os.environ, 'OPENMASKIT_TRAFFIC_DB_PATH': str(directory / 'traffic.db')}
        with (directory / 'proxy.log').open('wb') as log:
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--proxy',
                '--config', str(config), '--store-path', str(db),
                '--web-port', str(web), '--mcp-port', str(mcp)],
                env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 30
                while True:
                    if process.poll() is not None:
                        raise RuntimeError('Synthetic proxy exited: ' + (directory / 'proxy.log').read_text()[-2500:])
                    try:
                        with urllib.request.urlopen(f'http://127.0.0.1:{web}/api/config', timeout=1):
                            break
                    except Exception:
                        if time.monotonic() > deadline:
                            raise RuntimeError('Synthetic proxy did not start')
                        time.sleep(0.1)
                asyncio.run(probe(web, mcp))
            finally:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=35)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    print('PASS: real startup restored seven rules; MCP headers masked; empty Cc preserved; oversized result withheld.')


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--upstream':
        asyncio.run(upstream())
    elif len(sys.argv) > 1 and sys.argv[1] == '--proxy':
        proxy()
    else:
        run()
