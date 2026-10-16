import logging

from ddgs import DDGS

logger = logging.getLogger(__name__)

TOOLS = [
	{
		"type": "function",
		"function": {
			"name": "web_search",
			"description": "Search the web for current information, facts, news, or anything you don't know.",
			"parameters": {
				"type": "object",
				"properties": {
					"query": {"type": "string", "description": "The search query"},
				},
				"required": ["query"],
			},
		},
	},
]


def web_search(query: str, max_results: int = 5) -> str:
	results = DDGS().text(query, max_results=max_results)
	if not results:
		return "No results found."
	return "\n".join(f"{r['title']}: {r['body']}" for r in results)


_mcp = None


def init_mcp(servers: list[dict]) -> None:
	global _mcp
	from powerconf.mcp_bridge import McpBridge

	bridge = McpBridge()
	try:
		bridge.start(servers)
	except Exception:
		logger.exception("MCP startup failed")
		return
	if bridge.ollama_tools():
		_mcp = bridge
	else:
		logger.warning("No MCP tools discovered — check MCP_SERVERS config")


def all_tools() -> list[dict]:
	return TOOLS + (_mcp.ollama_tools() if _mcp else [])


def run_tool(name: str, arguments: dict) -> str:
	if name == "web_search":
		return web_search(arguments.get("query", ""))
	if _mcp and name in _mcp.tool_names():
		return _mcp.call(name, arguments)
	return f"Unknown tool: {name}"
