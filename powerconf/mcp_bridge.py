import asyncio
import logging
import threading
from contextlib import AsyncExitStack
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)


class McpBridge:
	"""Holds stdio connections to MCP servers on a dedicated event-loop thread,
	exposing their tools as plain Ollama tool defs + a synchronous call()."""

	def __init__(self) -> None:
		self._loop = asyncio.new_event_loop()
		self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
		self._thread.start()
		self._stack = AsyncExitStack()
		self._sessions: dict[str, object] = {}
		self._defs: list[dict] = []
		self._errlogs = []

	def start(self, servers: list[dict], timeout: float = 90.0) -> None:
		future = asyncio.run_coroutine_threadsafe(self._connect_all(servers), self._loop)
		try:
			future.result(timeout=timeout)
		except TimeoutError:
			# cold uvx/npx installs can outlast the timeout — keep the tools that
			# connected in time; the rest still register in the background
			logger.warning(
				"MCP startup timed out — %d tool(s) connected so far, remaining servers will join late",
				len(self._defs),
			)

	async def _connect_all(self, servers: list[dict]) -> None:
		for config in servers:
			try:
				await self._connect(config)
			except Exception:
				logger.exception("MCP server %r failed to start", config.get("name", config.get("command")))

	async def _connect(self, config: dict) -> None:
		from mcp import ClientSession, StdioServerParameters
		from mcp.client.stdio import stdio_client

		params = StdioServerParameters(
			command=config["command"],
			args=config.get("args", []),
			env=config.get("env"),
		)
		log_dir = Path(__file__).resolve().parents[1] / "logs"
		log_dir.mkdir(parents=True, exist_ok=True)
		errlog = (log_dir / f"{date.today().isoformat()}.log").open("a", encoding="utf-8", buffering=1)
		try:
			read, write = await self._stack.enter_async_context(stdio_client(params, errlog=errlog))
		except Exception:
			errlog.close()
			raise
		self._errlogs.append(errlog)
		session = await self._stack.enter_async_context(ClientSession(read, write))
		await session.initialize()
		tools = await session.list_tools()
		name = config.get("name", config["command"])
		for tool in tools.tools:
			if tool.name in self._sessions:
				logger.warning("MCP tool %r from %s shadows an earlier tool — skipped", tool.name, name)
				continue
			self._sessions[tool.name] = session
			self._defs.append({
				"type": "function",
				"function": {
					"name": tool.name,
					"description": tool.description or "",
					"parameters": tool.input_schema,
				},
			})
		logger.info("MCP server %s connected: %d tools", name, len(tools.tools))

	def tool_names(self) -> set[str]:
		return set(self._sessions)

	def ollama_tools(self) -> list[dict]:
		return self._defs

	def call(self, name: str, arguments: dict, timeout: float = 60.0) -> str:
		session = self._sessions[name]
		future = asyncio.run_coroutine_threadsafe(session.call_tool(name, arguments), self._loop)
		result = future.result(timeout=timeout)
		parts = [getattr(block, "text", str(block)) for block in result.content]
		return "\n".join(p for p in parts if p)

	def close(self) -> None:
		asyncio.run_coroutine_threadsafe(self._stack.aclose(), self._loop).result(timeout=10)
		for errlog in self._errlogs:
			errlog.close()
		self._errlogs.clear()
		self._loop.call_soon_threadsafe(self._loop.stop)
