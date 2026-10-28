"""Runtime target manager: hot-add and remove MCP server targets."""

from __future__ import annotations

# Modified in the unofficial fork: attach the shared privacy-check registry.

import logging
import sys
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING

import anyio
from anyio.streams.memory import MemoryObjectSendStream

from mcp.shared.message import SessionMessage

from openmaskit.config_serde import load_runtime_config
from openmaskit.container import stop_container
from openmaskit.masking.engine import MaskingEngine
from openmaskit.models import UpstreamHttpConfig, UpstreamStdioConfig
from openmaskit.proxy.core import TargetState, _bootstrap_upstream, run_proxy_for_target
from openmaskit.proxy.upstream import connect_upstream

if TYPE_CHECKING:
    from openmaskit.masking.store import MaskingStore
    from openmaskit.proxy.core import ProxyState

logger = logging.getLogger(__name__)


def _merge_user_args(base_args: list[str], config: dict) -> list[str]:
    """Merge base args with user-configured args from config.meta.user_args."""
    merged = base_args.copy()

    user_args = config.get("meta", {}).get("user_args", {})

    for arg_name, arg_config in user_args.items():
        values = arg_config.get("values", [])
        arg_format = arg_config.get("arg_format", "")

        if not arg_format:
            logger.warning(f"Missing arg_format for {arg_name}, skipping")
            continue

        for value in values:
            # Format: "--flag {value}" becomes ["--flag", "/path"]
            formatted = arg_format.replace("{value}", str(value))
            merged.extend(formatted.split())

    return merged


def _build_upstream_config(config: dict) -> UpstreamStdioConfig | UpstreamHttpConfig:
    transport = config.get("transport", "stdio")
    if transport == "stdio":
        base_args = config.get("args", [])
        merged_args = _merge_user_args(base_args, config)

        return UpstreamStdioConfig(
            command=config["command"],
            args=merged_args,
            env=config.get("env", {}),
        )
    else:
        oauth = config.get("oauth")
        return UpstreamHttpConfig(
            url=config["url"],
            oauth=oauth,
            headers=config.get("headers") or {},
        )


class TargetManager:
    """Manages hot-adding and removing MCP server targets at runtime."""

    def __init__(self, state: ProxyState, store: MaskingStore, store_path: str, container_runtime: str | None = None):
        self._state = state
        self._store = store
        self._store_path = store_path
        self._container_runtime = container_runtime
        self._exit_stacks: dict[str, AsyncExitStack] = {}
        self._target_locks: dict[str, anyio.Lock] = {}
        self._failed_flushes: dict[str, TargetState] = {}
        self._task_group: anyio.abc.TaskGroup | None = None
        self._shutdown_event: anyio.Event | None = None

    def set_task_group(self, tg: anyio.abc.TaskGroup, shutdown_event: anyio.Event):
        self._task_group = tg
        self._shutdown_event = shutdown_event
        # Start refresh monitor only if we have a task group AND backend client
        # (backend_client is needed for token refresh, and is only set in production)
        if tg is not None and hasattr(self._state, 'backend_client'):
            tg.start_soon(self._monitor_token_refresh)

    async def add_target(self, server_id: str, config: dict) -> TargetState:
        # A departed engine must finish persisting aliases before a retry loads
        # its counters. The same lock also serializes concurrent activations.
        async with self._target_locks.setdefault(server_id, anyio.Lock()):
            if server_id in self._state.targets:
                raise ValueError("Target is already connected")
            return await self._add_target(server_id, config)

    async def _add_target(self, server_id: str, config: dict) -> TargetState:
        """Hot-add a new target: create state, connect upstream, start relay.

        Cleans up after itself on failure — callers should NOT call remove_target
        if this raises.
        """
        departed = self._failed_flushes.get(server_id)
        if departed is not None:
            await self._flush_departed(departed)
        engine = MaskingEngine([], self._store, target_name=server_id)
        await engine.load_rules()
        await engine.load_aliases()
        await engine.load_mappers()
        await engine.load_guardrails()
        await engine.load_injections()

        hidden = await self._store.get_hidden_tools(target_name=server_id)

        ds_read_send, ds_read_recv = anyio.create_memory_object_stream[SessionMessage | Exception](32)

        target = TargetState(
            name=server_id,
            engine=engine,
            privacy_checks=getattr(self._state, "privacy_checks", None),
            hidden_tools=set(hidden),
            ds_read_send=ds_read_send,
            ds_read_recv=ds_read_recv,
            server_id=server_id,  # Set server_id for OAuth refresh
            traffic_buffer=self._state.traffic_buffer,
        )

        stack = AsyncExitStack()
        await stack.__aenter__()

        try:
            upstream = _build_upstream_config(config)
            us_read, us_write, container_info = await stack.enter_async_context(
                connect_upstream(upstream, self._store_path, errlog=sys.stderr,
                               server_id=server_id,
                               container_runtime=self._container_runtime)
            )

            target.container_info = container_info
            self._exit_stacks[server_id] = stack
            self._state.targets[server_id] = target

            with anyio.fail_after(120):
                await _bootstrap_upstream(us_read, us_write, target)
            if not target.initialized:
                raise ConnectionError("Upstream closed before initialization completed")
        except BaseException:
            if self._state.targets.get(server_id) is target:
                self._state.targets.pop(server_id, None)
            if self._exit_stacks.get(server_id) is stack:
                self._exit_stacks.pop(server_id, None)
            with anyio.CancelScope(shield=True):
                await stack.aclose()
            raise

        if self._task_group:
            self._task_group.start_soon(self.run_target, target, us_read, us_write)

        return target

    async def run_target(self, target: TargetState, us_read, us_write) -> None:
        """Own the relay and its background tasks until failure, EOF or removal."""
        if self._state.targets.get(target.name) is not target:
            return
        try:
            async with anyio.create_task_group() as tasks:
                target.lifecycle_scope = tasks.cancel_scope
                tasks.start_soon(self._flush_loop, target.engine, target.flush_lock)
                tasks.start_soon(target.response_dispatcher.start_background_eviction, self._shutdown_event)
                try:
                    await run_proxy_for_target(target, us_read, us_write)
                finally:
                    tasks.cancel_scope.cancel()
                    with anyio.CancelScope(shield=True):
                        await self.remove_target(target.name, expected=target)
        except Exception as exc:
            # Include nursery exit/background/final-flush failures, without
            # swallowing external cancellation (a BaseException).
            logger.warning("Target %s disconnected (%s)", target.name, type(exc).__name__)

    async def _flush_departed(self, target: TargetState) -> None:
        try:
            async with target.flush_lock:
                if target.engine.has_pending_writes:
                    await target.engine.flush_pending()
        except BaseException:
            self._failed_flushes[target.name] = target
            raise
        if self._failed_flushes.get(target.name) is target:
            self._failed_flushes.pop(target.name, None)

    async def remove_target(self, server_id: str, *, expected: TargetState | None = None) -> None:
        with anyio.CancelScope(shield=True):
            async with self._target_locks.setdefault(server_id, anyio.Lock()):
                await self._remove_target(server_id, expected=expected)

    async def _remove_target(self, server_id: str, *, expected: TargetState | None = None) -> None:
        """Teardown: close upstream connection, stop relay tasks, remove from state.

        Explicitly stops the upstream container before closing the exit stack.
        The transport's owner task closes its own SDK contexts, including when
        removal is requested from a different HTTP-handler task. The explicit
        container stop also handles older/incomplete runtime state.
        """
        target = self._state.targets.get(server_id)
        if expected is not None and target is not expected:
            return
        # Detach atomically before any await. An old relay's finalizer must
        # never pop or close a replacement target's resources.
        self._state.targets.pop(server_id, None)
        stack = self._exit_stacks.pop(server_id, None)
        if target is not None:
            target.initialized = False
            target.response_dispatcher.shutdown()
            if target.lifecycle_scope is not None:
                target.lifecycle_scope.cancel()

        # Explicit container stop, shielded so an outer cancellation can't
        # skip it. stop_container has its own internal timeout.
        if target is not None and target.container_info is not None:
            runtime, container_name = target.container_info
            try:
                await stop_container(runtime, container_name)
            except Exception as exc:
                logger.warning("Error stopping target container (%s)", type(exc).__name__)

        if target and target.ds_read_send:
            try:
                await target.ds_read_send.aclose()
            except (anyio.ClosedResourceError, anyio.EndOfStream):
                pass

        if stack:
            try:
                await stack.aclose()
            except Exception as exc:
                logger.warning("Error closing exit stack for %s: %s", server_id, exc)

        if target is not None:
            await self._flush_departed(target)

    async def _flush_loop(self, engine: MaskingEngine, flush_lock: anyio.Lock | None = None) -> None:
        flush_lock = flush_lock or anyio.Lock()
        while self._shutdown_event and not self._shutdown_event.is_set():
            await anyio.sleep(1.0)
            with anyio.CancelScope(shield=True):
                async with flush_lock:
                    try:
                        if engine.has_pending_writes:
                            await engine.flush_pending()
                    except Exception:
                        logger.exception("Failed to flush aliases for hot-added target")
        with anyio.CancelScope(shield=True):
            async with flush_lock:
                if engine.has_pending_writes:
                    await engine.flush_pending()

    async def _monitor_token_refresh(self) -> None:
        """Background task that monitors targets for refresh needs."""
        from openmaskit.proxy.upstream import refresh_backend_oauth_token

        # Use shorter sleep for tests
        check_interval = 0.1 if getattr(self, '_test_mode', False) else 5.0

        while self._shutdown_event and not self._shutdown_event.is_set():
            await anyio.sleep(check_interval)  # Will be cancelled by task group exit

            for server_id, target in list(self._state.targets.items()):
                if target.needs_token_refresh and target.server_id:
                    logger.info(f"Attempting automatic token refresh for {server_id}")

                    try:
                        # Get backend client from state
                        backend_client = getattr(self._state, 'backend_client', None)
                        if not backend_client:
                            logger.error("No backend client available for refresh")
                            target.needs_token_refresh = False
                            continue

                        # Call refresh
                        new_token = await refresh_backend_oauth_token(
                            target.server_id,
                            self._store_path,
                            backend_client,
                        )

                        if new_token:
                            # Success! Reconnect the target
                            logger.info(f"Token refreshed, reconnecting {server_id}")
                            record = await self._store.get_server(server_id)
                            try:
                                config = (
                                    load_runtime_config(record["config_json"])
                                    if record
                                    else None
                                )
                            except Exception:
                                logger.exception(
                                    f"Cannot reconnect {server_id}: stored config "
                                    f"not loadable; uninstall and re-add the server."
                                )
                                return
                            if config is None:
                                logger.error(
                                    f"Cannot reconnect {server_id}: no stored config; "
                                    f"uninstall and re-add the server."
                                )
                                return

                            # Remove old connection
                            await self.remove_target(server_id)

                            # Re-add with new token
                            await self.add_target(server_id, config)

                            logger.info(f"Successfully reconnected {server_id} with refreshed token")
                        else:
                            # Refresh failed - user must re-auth
                            logger.error(
                                f"Token refresh failed for {server_id} - user must re-authenticate"
                            )
                            target.needs_token_refresh = False  # Don't retry

                    except Exception as e:
                        logger.error(f"Error during token refresh for {server_id}: {e}")
                        target.needs_token_refresh = False
