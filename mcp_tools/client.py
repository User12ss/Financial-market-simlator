"""MCP (Model Context Protocol) client manager for external tool integration.

Connects to MCP servers that provide tools for:
- Market data (prices, indicators, historical data)
- Economic data (FRED, BLS, central bank releases)
- News and sentiment (real-time feeds)
- Database access (PostgreSQL, SQLite for internal data)

Usage:
    manager = MCPManager(config)
    manager.connect_all_sync()
    result = manager.call_tool_sync("market-data", "get_price", {"ticker": "SPY"})
    enrichment = manager.enrich_scenario_context("Fed raises rates 50bps")
"""

from __future__ import annotations
import asyncio
import threading
from dataclasses import dataclass, field
from typing import Any


@dataclass
class MCPServerConfig:
    """Configuration for a single MCP server."""
    name: str
    transport: str  # "stdio" or "sse"
    command: str | None = None  # for stdio: command to run
    args: list[str] = field(default_factory=list)
    url: str | None = None  # for sse: server URL
    env: dict[str, str] = field(default_factory=dict)
    enabled: bool = True


@dataclass
class ToolResult:
    """Result from an MCP tool call."""
    success: bool
    data: Any = None
    error: str | None = None


class MCPManager:
    """Manages connections to multiple MCP servers and routes tool calls.

    Uses a dedicated background event loop so that async MCP sessions
    stay alive across sync call_tool_sync / enrich_scenario_context calls.
    """

    def __init__(self, server_configs: list[MCPServerConfig] | None = None):
        self.configs = server_configs or []
        self._sessions: dict[str, Any] = {}
        self._tools: dict[str, dict] = {}
        self._connected = False
        self._contexts: list[Any] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        """Start a background event loop thread if not already running."""
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
            self._thread.start()
        return self._loop

    def _run_sync(self, coro):
        """Run an async coroutine on the background loop and wait for the result."""
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=30)

    @property
    def available_tools(self) -> dict[str, list[str]]:
        """Map of server_name → list of tool names."""
        return {name: list(tools.keys()) for name, tools in self._tools.items()}

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ── Async API ────────────────────────────────────────────────────────

    async def connect_all(self) -> None:
        """Connect to all enabled MCP servers."""
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError:
            print("  MCP SDK not available — running without external tools")
            return

        for config in self.configs:
            if not config.enabled:
                continue
            try:
                await self._connect_server(config)
            except Exception as e:
                print(f"  WARNING: Failed to connect to MCP server '{config.name}': {e}")

        self._connected = True

    async def _connect_server(self, config: MCPServerConfig) -> None:
        """Connect to a single MCP server and discover its tools."""
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        if config.transport == "stdio" and config.command:
            server_params = StdioServerParameters(
                command=config.command,
                args=config.args,
                env=config.env or None,
            )

            stdio_ctx = stdio_client(server_params)
            read, write = await stdio_ctx.__aenter__()
            self._contexts.append(stdio_ctx)

            session_ctx = ClientSession(read, write)
            session = await session_ctx.__aenter__()
            self._contexts.append(session_ctx)

            await session.initialize()
            tools_response = await session.list_tools()

            self._sessions[config.name] = session
            self._tools[config.name] = {
                tool.name: {
                    "description": tool.description,
                    "schema": tool.inputSchema,
                }
                for tool in tools_response.tools
            }

    async def call_tool(self, server_name: str, tool_name: str, arguments: dict) -> ToolResult:
        """Call a tool on a specific MCP server."""
        if server_name not in self._sessions:
            return ToolResult(success=False, error=f"Server '{server_name}' not connected")

        session = self._sessions[server_name]
        try:
            result = await session.call_tool(tool_name, arguments=arguments)
            content = result.content[0].text if result.content else ""
            return ToolResult(success=True, data=content)
        except Exception as e:
            return ToolResult(success=False, error=str(e))

    async def disconnect_all(self) -> None:
        """Disconnect from all MCP servers."""
        for ctx in reversed(self._contexts):
            try:
                await ctx.__aexit__(None, None, None)
            except Exception:
                pass
        self._contexts = []
        self._sessions.clear()
        self._tools.clear()
        self._connected = False

    # ── Sync API (used by orchestrator) ──────────────────────────────────

    def connect_all_sync(self) -> None:
        """Connect to all servers (sync — safe to call from main thread)."""
        self._run_sync(self.connect_all())

    def call_tool_sync(self, server_name: str, tool_name: str, arguments: dict) -> ToolResult:
        """Call a tool synchronously."""
        return self._run_sync(self.call_tool(server_name, tool_name, arguments))

    def disconnect_all_sync(self) -> None:
        """Disconnect all servers synchronously."""
        self._run_sync(self.disconnect_all())
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread:
                self._thread.join(timeout=5)
            self._loop.close()
            self._loop = None

    def enrich_scenario_context(self, scenario_description: str) -> str:
        """Call relevant MCP tools to gather real data for a scenario.

        This is the main integration point — called by the orchestrator's
        enrich_scenario node to pull live data before agents analyze.
        """
        if not self._connected or not self._tools:
            return ""

        enrichments = []

        for server_name, tools in self._tools.items():
            for tool_name, tool_info in tools.items():
                if self._is_tool_relevant(tool_name, tool_info, scenario_description):
                    result = self.call_tool_sync(server_name, tool_name, {
                        "query": scenario_description,
                    })
                    if result.success and result.data:
                        enrichments.append(f"[{server_name}/{tool_name}]: {result.data}")

        if enrichments:
            return "EXTERNAL DATA (from MCP tools):\n" + "\n".join(enrichments)
        return ""

    # ── Utility ──────────────────────────────────────────────────────────

    def get_tools_for_prompt(self) -> str:
        """Format available MCP tools as a string for LLM context."""
        if not self._tools:
            return ""

        lines = ["AVAILABLE EXTERNAL DATA TOOLS (via MCP):"]
        for server, tools in self._tools.items():
            lines.append(f"\n[{server}]")
            for name, info in tools.items():
                lines.append(f"  - {name}: {info['description']}")
        return "\n".join(lines)

    def _is_tool_relevant(self, tool_name: str, tool_info: dict, scenario: str) -> bool:
        """Simple relevance check — can be enhanced with LLM-based routing."""
        keywords = {
            "rate": ["interest", "fed", "central bank", "monetary"],
            "price": ["commodity", "oil", "gold", "stock"],
            "economic": ["gdp", "inflation", "employment", "fiscal"],
            "news": ["announcement", "policy", "geopolitical"],
        }
        scenario_lower = scenario.lower()
        description_lower = tool_info.get("description", "").lower()

        for category, terms in keywords.items():
            if any(t in scenario_lower for t in terms):
                if category in tool_name.lower() or category in description_lower:
                    return True
        return False
