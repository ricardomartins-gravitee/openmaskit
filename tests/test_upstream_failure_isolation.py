"""Exercise the real MCP HTTP SDK with synthetic transports, never mailbox tools."""
import asyncio
import json
import sqlite3
import sys
import time

import anyio
import httpx
import pytest

from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCRequest

from openmaskit.masking.store import MaskingStore
from openmaskit.masking.engine import MaskingEngine
from openmaskit.proxy.core import ProxyState
from openmaskit.proxy.manager import TargetManager
from openmaskit.proxy import upstream
from openmaskit.web.app import create_app


@pytest.fixture
def synthetic_http(monkeypatch):
    calls = []
    failed = anyio.Event()
    real_factory = upstream.create_mcp_http_client

    async def handle(request):
        if request.method != "POST":
            if request.method == "DELETE" and request.url.host == "cleanup.test":
                failed.set()
                await anyio.sleep_forever()
            return httpx.Response(405)
        message = json.loads(request.content)
        method = message["method"]
        calls.append((request.url.host, method))
        assert method != "tools/call", "Fixture must never submit tools"
        if request.url.host == "waiting.test" and method == "initialize":
            failed.set()  # Barrier: initialization really reached the SDK HTTP task.
            await anyio.sleep_forever()
        if (request.url.host == "offline.test" and calls.count(("offline.test", "initialize")) == 1) or (
            request.url.host == "later.test" and method == "ping"
        ) or (
            request.url.host == "tools-timeout.test" and method == "tools/list"
        ):
            failed.set()
            raise httpx.ReadTimeout("Synthetic upstream timeout", request=request)
        if "id" not in message:
            return httpx.Response(202)
        result = {}
        if method == "initialize":
            result = {"protocolVersion": "2025-03-26", "capabilities": {},
                      "serverInfo": {"name": "synthetic", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": []}
        headers = {"Mcp-Session-Id": "synthetic-cleanup"} if request.url.host == "cleanup.test" else {}
        return httpx.Response(200, headers=headers,
                              json={"jsonrpc": "2.0", "id": message["id"], "result": result})

    def factory(**kwargs):
        # Preserve the actual SDK factory/defaults, swapping only the network.
        client = real_factory(**kwargs)
        client._transport = httpx.MockTransport(handle)
        return client

    monkeypatch.setattr(upstream, "create_mcp_http_client", factory)
    return calls, failed


async def wait_until(predicate):
    with anyio.fail_after(3):
        while not predicate():
            await anyio.sleep(0.01)


def owners():
    return {task for task in asyncio.all_tasks() if task.get_name() == "openmaskit-upstream-owner"}


@pytest.mark.anyio
@pytest.mark.parametrize("host", ["offline.test", "tools-timeout.test"])
async def test_sdk_initialize_timeout_does_not_cancel_api_task(tmp_path, synthetic_http, host):
    calls, failed = synthetic_http
    baseline = owners()
    store = await MaskingStore.create(tmp_path / "store.db")
    try:
        config = {"transport": "http", "url": f"http://{host}/mcp"}
        await store.install_server("offline", "Synthetic offline", source="custom", backend_id=None, config=config)
        state = ProxyState()
        state.store = store
        manager = TargetManager(state, store, str(tmp_path / "store.db"))
        with anyio.fail_after(3):
            with pytest.raises(Exception):
                await manager.add_target("offline", config)
            await anyio.sleep(0)  # A leaked SDK cancellation hits the caller here.
        assert failed.is_set()
        assert "offline" not in state.targets
        assert not manager._exit_stacks
        assert (await store.get_server("offline"))["active"]
        assert calls[0] == (host, "initialize")
        assert calls.count((host, "initialize")) == 1
        assert owners() == baseline
    finally:
        await store.close()


@pytest.mark.anyio
@pytest.mark.parametrize("source", ["custom", "marketplace"])
async def test_sdk_late_timeout_preserves_other_target_and_cross_task_remove(tmp_path, monkeypatch, synthetic_http, source):
    calls, failed = synthetic_http
    baseline = owners()
    store = await MaskingStore.create(tmp_path / "store.db")
    state = ProxyState()
    state.store = store
    manager = TargetManager(state, store, str(tmp_path / "store.db"))
    state.target_manager = manager
    active_flushers = set()
    real_flush_loop = manager._flush_loop

    async def track_flush_lifetime(engine, lock=None):
        active_flushers.add(engine)
        try:
            await real_flush_loop(engine, lock)
        finally:
            active_flushers.discard(engine)

    monkeypatch.setattr(manager, "_flush_loop", track_flush_lifetime)
    config = {"transport": "http", "url": "http://later.test/mcp"}
    await store.install_server("later", "Synthetic reconnect", source=source, backend_id=None, config=config)
    original = await store.get_server("later")
    app = create_app(state, csrf_token="synthetic-csrf")
    shutdown = anyio.Event()
    try:
        async with anyio.create_task_group() as tg:
            manager.set_task_group(tg, shutdown)
            good = await manager.add_target("good", {"transport": "http", "url": "http://good.test/mcp"})
            later = await manager.add_target("later", {"transport": "http", "url": "http://later.test/mcp"})
            alias = later.engine._get_or_create_alias("Synthetic One", "synthetic", "field", "value")
            await later.ds_read_send.send(SessionMessage(JSONRPCMessage(root=JSONRPCRequest(
                jsonrpc="2.0", id="synthetic-ping", method="ping"))))
            await wait_until(lambda: failed.is_set() and "later" not in state.targets)
            await wait_until(lambda: later.engine not in active_flushers)
            assert good.initialized
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost",
                headers={"X-CSRF-Token": "synthetic-csrf", "Origin": "http://127.0.0.1:9473"}) as client:
                path = "/api/targets/custom/later/activate" if source == "custom" else "/api/marketplace/activate"
                response = await client.post(path, json={"server_id": "later"})
            assert response.status_code == 200 and response.json()["connected"]
            replacement = state.targets["later"]
            assert replacement is not later and replacement.initialized
            assert replacement.engine.alias_cache[alias] == "Synthetic One"
            assert await store.get_server("later") == original
            await manager.remove_target("later", expected=later)
            assert state.targets["later"] is replacement
            await anyio.sleep(0)
            removed = anyio.Event()

            async def remove_from_api_task():
                await manager.remove_target("later")
                await manager.remove_target("good")
                removed.set()

            tg.start_soon(remove_from_api_task)
            with anyio.fail_after(3):
                await removed.wait()
            assert not state.targets and not manager._exit_stacks
            await wait_until(lambda: not active_flushers)
            assert owners() == baseline
            assert calls.count(("later.test", "ping")) == 1
            shutdown.set()
            tg.cancel_scope.cancel()
    finally:
        await store.close()


@pytest.mark.anyio
async def test_caller_cancellation_propagates_and_closes_owner(synthetic_http):
    baseline = owners()
    entered = anyio.Event()
    exited = anyio.Event()

    async def caller():
        try:
            async with upstream.connect_upstream(upstream.UpstreamHttpConfig(url="http://good.test/mcp")):
                entered.set()
                await anyio.sleep_forever()
        finally:
            exited.set()

    async with anyio.create_task_group() as tg:
        tg.start_soon(caller)
        await entered.wait()
        tg.cancel_scope.cancel()
    assert exited.is_set() and owners() == baseline


@pytest.mark.anyio
@pytest.mark.parametrize("malformed_saved", [False, True])
async def test_dashboard_survives_real_sdk_startup_timeout(tmp_path, monkeypatch, synthetic_http, malformed_saved):
    """Run async_main with real ASGI dashboard/DB and synthetic HTTP upstreams."""
    import openmaskit.__main__ as main

    calls, failed = synthetic_http
    baseline = owners()
    config = tmp_path / "config.yaml"
    config.write_text("targets:\n  good:\n    upstream:\n      transport: http\n      url: http://good.test/mcp\nstore_path: " + str(tmp_path / "store.db") + "\n")
    store = await MaskingStore.create(tmp_path / "store.db")
    try:
        await store.install_server("offline", "Synthetic offline", source="custom", backend_id=None,
                                   config={"transport": "http", "url": "http://offline.test/mcp"})
        if malformed_saved:
            await store.install_server("malformed", "Synthetic malformed", source="custom",
                                       backend_id=None, config={"transport": "http"})
    finally:
        await store.close()
    monkeypatch.setattr("sys.argv", ["openmaskit", str(config)])
    monkeypatch.setenv("OPENMASKIT_DISABLE_MARKETPLACE", "1")
    monkeypatch.setenv("OPENMASKIT_TRAFFIC_DB_PATH", str(tmp_path / "traffic.db"))
    monkeypatch.setattr(main, "_load_installation_id", lambda: "synthetic-startup-isolation")
    checked = anyio.Event()

    class StopSyntheticServer(Exception):
        pass

    async def serve(server):
        if server.config.port != 9473:
            await anyio.sleep_forever()
        await failed.wait()
        await wait_until(lambda: server.config.app.state.proxy_state.targets["good"].initialized)
        await wait_until(lambda: "offline" not in server.config.app.state.proxy_state.targets)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.config.app), base_url="http://localhost",
            headers={"X-CSRF-Token": server.config.app.state.csrf_token, "Origin": "http://127.0.0.1:9473"}) as client:
            response = await client.get("/health")
            retry = await client.post("/api/targets/custom/offline/activate")
        assert response.status_code == 200
        assert response.json()["database"]["connected"]
        assert retry.status_code == 200 and retry.json()["connected"]
        checked.set()
        raise StopSyntheticServer()

    monkeypatch.setattr(main.uvicorn.Server, "serve", serve)
    with anyio.fail_after(5):
        await main.async_main()
    assert checked.is_set(), "Dashboard task was cancelled by the SDK startup timeout"
    assert calls.count(("offline.test", "initialize")) == 2
    assert owners() == baseline


@pytest.mark.anyio
async def test_cancellation_during_sdk_initialization_cleans_pending_add(tmp_path, synthetic_http):
    calls, started = synthetic_http
    baseline = owners()
    store = await MaskingStore.create(tmp_path / "store.db")
    state = ProxyState()
    state.store = store
    manager = TargetManager(state, store, str(tmp_path / "store.db"))
    cancelled = anyio.Event()

    async def add():
        try:
            await manager.add_target("waiting", {"transport": "http", "url": "http://waiting.test/mcp"})
        except asyncio.CancelledError:
            cancelled.set()
            raise

    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(add)
            with anyio.fail_after(3):
                await started.wait()
            tg.cancel_scope.cancel()
        assert cancelled.is_set(), "Initialization cancellation was swallowed"
        assert not state.targets and not manager._exit_stacks
        assert owners() == baseline
        assert calls == [("waiting.test", "initialize")]
        await anyio.sleep(0)
    finally:
        await store.close()


@pytest.mark.anyio
async def test_hanging_sdk_session_delete_is_cancelled_within_cleanup_bound(synthetic_http):
    calls, deleting = synthetic_http
    baseline = owners()
    context = upstream.connect_upstream(upstream.UpstreamHttpConfig(url="http://cleanup.test/mcp"))
    read, write, _ = await context.__aenter__()
    await write.send(SessionMessage(JSONRPCMessage(root=JSONRPCRequest(
        jsonrpc="2.0", id="synthetic-init", method="initialize", params={
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "synthetic", "version": "1"}}))))
    with anyio.fail_after(3):
        await read.receive()
    started = time.monotonic()
    close_task = asyncio.create_task(context.__aexit__(None, None, None))
    try:
        await asyncio.wait_for(asyncio.shield(close_task), timeout=15)
    finally:
        if not close_task.done():
            close_task.cancel()
            await asyncio.gather(close_task, return_exceptions=True)
    assert deleting.is_set(), "Fixture did not reach SDK session termination"
    assert time.monotonic() - started < 15
    assert owners() == baseline
    assert calls == [("cleanup.test", "initialize")]


@pytest.mark.anyio
async def test_real_stdio_early_eof_is_isolated_and_leaves_no_owner(tmp_path):
    baseline = owners()
    store = await MaskingStore.create(tmp_path / "store.db")
    state = ProxyState()
    state.store = store
    manager = TargetManager(state, store, str(tmp_path / "store.db"))
    try:
        with anyio.fail_after(3):
            with pytest.raises(Exception):
                await manager.add_target("early-eof", {"transport": "stdio", "command": sys.executable,
                    "args": ["-c", "import sys; sys.stdin.buffer.readline()"]})
        await anyio.sleep(0)
        assert not state.targets and not manager._exit_stacks
        assert owners() == baseline
    finally:
        await store.close()


@pytest.mark.anyio
async def test_failed_final_flush_is_isolated_and_retry_preserves_alias_counters(tmp_path, monkeypatch, synthetic_http):
    calls, failed = synthetic_http
    baseline = owners()
    store = await MaskingStore.create(tmp_path / "store.db")
    state = ProxyState()
    state.store = store
    manager = TargetManager(state, store, str(tmp_path / "store.db"))
    state.target_manager = manager
    config = {"transport": "http", "url": "http://later.test/mcp"}
    await store.install_server("later", "Synthetic flush retry", source="custom", backend_id=None, config=config)
    shutdown = anyio.Event()
    app = create_app(state, csrf_token="synthetic-csrf")
    real_persist = store.persist_alias
    allow_retry = False
    added_during_flush = False
    try:
        async with anyio.create_task_group() as tg:
            manager.set_task_group(tg, shutdown)
            good = await manager.add_target("good", {"transport": "http", "url": "http://good.test/mcp"})
            later = await manager.add_target("later", config)
            first = later.engine._get_or_create_alias("Synthetic One", "synthetic", "field", "value")
            second = later.engine._get_or_create_alias("Synthetic Two", "synthetic", "field", "value")

            async def failing_persist(target_name, alias, *args):
                nonlocal added_during_flush
                if alias == second and not allow_retry:
                    if not added_during_flush:
                        later.engine._get_or_create_alias("Synthetic Three", "synthetic", "field", "value")
                        added_during_flush = True
                    raise sqlite3.OperationalError("Synthetic persistence failure")
                await real_persist(target_name, alias, *args)

            monkeypatch.setattr(store, "persist_alias", failing_persist)
            await later.ds_read_send.send(SessionMessage(JSONRPCMessage(root=JSONRPCRequest(
                jsonrpc="2.0", id="synthetic-ping", method="ping"))))
            await wait_until(lambda: "later" in manager._failed_flushes)
            assert good.initialized and "later" not in state.targets
            assert (await store.get_all_aliases(target_name="later")) == {first: "Synthetic One"}
            assert [row[0] for row in later.engine._pending_writes] == [second, "value_3"]
            assert (await store.get_server("later"))["active"]
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost",
                headers={"X-CSRF-Token": "synthetic-csrf", "Origin": "http://127.0.0.1:9473"}) as client:
                health = await client.get("/health")
                assert health.status_code == 200 and health.json()["database"]["connected"]
                blocked = await client.post("/api/targets/custom/later/activate")
                assert blocked.status_code == 200 and not blocked.json()["connected"]
                assert "later" not in state.targets
                allow_retry = True
                restored = await client.post("/api/targets/custom/later/activate")
            assert restored.status_code == 200 and restored.json()["connected"]
            replacement = state.targets["later"]
            assert replacement.engine.alias_cache == {
                "value_1": "Synthetic One", "value_2": "Synthetic Two", "value_3": "Synthetic Three"}
            assert replacement.engine._get_or_create_alias("Synthetic Four", "synthetic", "field", "value") == "value_4"
            assert not later.engine.has_pending_writes and not manager._failed_flushes
            assert calls.count(("later.test", "ping")) == 1
            await manager.remove_target("later")
            await manager.remove_target("good")
            await wait_until(lambda: owners() == baseline)
            assert (await store.get_all_aliases(target_name="later"))["value_4"] == "Synthetic Four"
            shutdown.set()
            tg.cancel_scope.cancel()
    finally:
        await store.close()


@pytest.mark.anyio
async def test_cancelled_partial_alias_flush_retains_remaining_writes(tmp_path, monkeypatch):
    store = await MaskingStore.create(tmp_path / "store.db")
    engine = MaskingEngine([], store, target_name="synthetic")
    first = engine._get_or_create_alias("Synthetic One", "synthetic", "field", "value")
    second = engine._get_or_create_alias("Synthetic Two", "synthetic", "field", "value")
    real_persist = store.persist_alias

    async def cancel_second(target, alias, *args):
        if alias == second:
            raise asyncio.CancelledError()
        await real_persist(target, alias, *args)

    monkeypatch.setattr(store, "persist_alias", cancel_second)
    try:
        with pytest.raises(asyncio.CancelledError):
            await engine.flush_pending()
        assert await store.get_all_aliases(target_name="synthetic") == {first: "Synthetic One"}
        assert [row[0] for row in engine._pending_writes] == [second]
        monkeypatch.setattr(store, "persist_alias", real_persist)
        await engine.flush_pending()
        replacement = MaskingEngine([], store, target_name="synthetic")
        await replacement.load_aliases()
        assert replacement.alias_cache == {first: "Synthetic One", second: "Synthetic Two"}
        assert replacement._get_or_create_alias("Synthetic Three", "synthetic", "field", "value") == "value_3"
    finally:
        await store.close()
