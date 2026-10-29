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
	{
		"type": "function",
		"function": {
			"name": "clear_history",
			"description": "Erase all conversation history and the persistent memory log. Use when the user asks to clear, wipe, or reset history/memory.",
			"parameters": {"type": "object", "properties": {}},
		},
	},
	{
		"type": "function",
		"function": {
			"name": "forget_last",
			"description": "Delete the most recent exchange from conversation history. Use when the user asks to forget, delete, or undo the last thing they said or the assistant's last reply.",
			"parameters": {"type": "object", "properties": {}},
		},
	},
]


def web_search(query: str, max_results: int = 5) -> str:
	results = DDGS().text(query, max_results=max_results)
	if not results:
		return "No results found."
	return "\n".join(f"{r['title']}: {r['body']}" for r in results)


_mcp = None
_history_ops: dict = {}


def register_history_ops(clear, forget) -> None:
	_history_ops["clear_history"] = clear
	_history_ops["forget_last"] = forget


def init_mcp(servers: list[dict]) -> None:
	global _mcp
	from powerconf.mcp_bridge import McpBridge

	bridge = McpBridge()
	try:
		bridge.start(servers)
	except Exception:
		logger.exception("MCP startup failed")
		bridge.close()
		return
	if bridge.ollama_tools():
		_mcp = bridge
	else:
		logger.warning("No MCP tools discovered — check MCP_SERVERS config")
		bridge.close()


def all_tools() -> list[dict]:
	return TOOLS + (_mcp.ollama_tools() if _mcp else [])


def run_tool(name: str, arguments: dict) -> str:
	if name == "web_search":
		return web_search(arguments.get("query", ""))
	if name in _history_ops:
		return _history_ops[name]()
	if _mcp and name in _mcp.tool_names():
		return _mcp.call(name, arguments)
	return f"Unknown tool: {name}"
