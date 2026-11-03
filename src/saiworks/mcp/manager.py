from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable
from threading import RLock

from .transport import MCPTransportClosed

from .client import MCPClient
from .config import MCPServerConfig
from .types import MCPToolCallResult


ClientFactory = Callable[[MCPServerConfig], MCPClient]
CLIENT_INVALIDATION_ERROR_CODES = {
    "mcp_transport_closed",
    "mcp_transport_connect_failed",
    "mcp_transport_timeout",
    "mcp_transport_read_timeout",
    "mcp_protocol_error",
}


@dataclass(slots=True)
class MCPManager:
    configs: dict[str, MCPServerConfig]
    client_factory: ClientFactory
    _clients: dict[str, MCPClient] = field(default_factory=dict)
    logger: object | None = None

    _lock: object = field(default_factory=RLock, init=False, repr=False)
    _generation: int = field(default=0, init=False)
    _closing: bool = field(default=False, init=False)
    _starting: dict = field(default_factory=dict, init=False)
    _retired: list = field(default_factory=list, init=False)

    def capture_generation(self) -> int:
        with self._lock:
            return self._generation

    def get_client(self, server_name: str, *, expected_generation: int | None = None) -> MCPClient:
        with self._lock:
            if expected_generation is not None and expected_generation != self._generation:
                raise MCPTransportClosed("MCP request belongs to a retired resource generation")
            if self._closing or any(name == server_name for name, _ in self._retired):
                raise MCPTransportClosed("MCP server resources are awaiting cleanup")
            existing = self._clients.get(server_name)
            if existing is not None:
                return existing
            if server_name in self._starting:
                raise MCPTransportClosed("MCP server initialization is already in progress")
            generation = self._generation
            config = self.configs[server_name]
            client = self.client_factory(config)
            self._starting[server_name] = client
        try:
            if self.logger is not None:
                self.logger.record("mcp.server.start", {"server_name": server_name, "transport": config.transport})
            client.initialize()
        except BaseException:
            try:
                client.close()
            except Exception:
                with self._lock:
                    self._retain_failed(server_name, client)
            finally:
                with self._lock:
                    if self._starting.get(server_name) is client:
                        self._starting.pop(server_name)
            raise
        with self._lock:
            if self._starting.get(server_name) is client:
                self._starting.pop(server_name)
            stale = generation != self._generation or self._closing
            if not stale:
                self._clients[server_name] = client
        if stale:
            try:
                client.close()
            except Exception:
                with self._lock:
                    self._retain_failed(server_name, client)
            raise MCPTransportClosed("MCP initialization belongs to a retired generation")
        if self.logger is not None:
            self.logger.record("mcp.server.ready", {"server_name": server_name, "transport": config.transport})
        return client

    def _retain_failed(self, name, client):
        if not any(existing is client for _, existing in self._retired):
            self._retired.append((name, client))

    def invalidate(self, server_name: str, expected_client=None) -> None:
        with self._lock:
            client = self._clients.get(server_name)
            if client is None or (expected_client is not None and client is not expected_client):
                return
            self._clients.pop(server_name)
            try:
                client.close()
            except Exception:
                self._retain_failed(server_name, client)

    def call_tool(self, server_name: str, tool_name: str, arguments: dict, *, expected_generation: int | None = None) -> MCPToolCallResult:
        client = self.get_client(server_name, expected_generation=expected_generation)
        result = client.call_tool(tool_name, arguments)
        if result.is_error and result.error_code in CLIENT_INVALIDATION_ERROR_CODES:
            # Once tools/call has started, a transport failure cannot tell us
            # whether a remote side effect already happened. Drop the broken
            # connection, but never replay the call implicitly.
            self.invalidate(server_name, client)
            if self.logger is not None:
                self.logger.record("mcp.server.reconnect_deferred", {
                    "server_name": server_name,
                    "tool_name": tool_name,
                    "error_code": result.error_code,
                    "reason": "tool_call_outcome_unknown",
                })
        return result

    def read_resource(self, server_name: str, resource_id: str) -> str:
        client = self.get_client(server_name)
        try:
            return client.read_resource(resource_id)
        except Exception as exc:
            error_code = getattr(exc, "error_code", "")
            if error_code in CLIENT_INVALIDATION_ERROR_CODES:
                self.invalidate(server_name, client)
                if self.logger is not None:
                    self.logger.record("mcp.server.reconnect_deferred", {
                        "server_name": server_name,
                        "resource_id": resource_id,
                        "error_code": error_code,
                        "reason": "resource_read_failed",
                    })
            raise

    def close(self) -> None:
        with self._lock:
            self._closing = True
            self._generation += 1
            pending = [*self._clients.items(), *self._starting.items(), *self._retired]
            self._clients.clear()
            self._starting.clear()
            self._retired.clear()
            errors = []
            try:
                for name, client in pending:
                    try:
                        client.close()
                    except Exception as error:
                        self._retain_failed(name, client)
                        errors.append(error)
            finally:
                self._closing = False
            if errors:
                raise ExceptionGroup("MCP client cleanup failed", errors)
