import json
import logging
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

BACKLOG = 100  # events replayed to clients that connect mid-session
_CONTENT_TYPES = {
	".html": "text/html; charset=utf-8",
	".js": "text/javascript",
	".css": "text/css",
	".svg": "image/svg+xml",
	".png": "image/png",
	".ico": "image/x-icon",
	".json": "application/json",
}


class WebUI:
	"""Serves the built web app plus an /events SSE feed the UI subscribes to."""

	def __init__(self, static_dir: Path, port: int, on_action=None) -> None:
		self._static_dir = static_dir
		self._port = port
		self._on_action = on_action
		self._backlog: list[str] = []
		self._listeners: list[queue.Queue] = []
		self._lock = threading.Lock()

	def start(self) -> None:
		ui = self

		class Handler(BaseHTTPRequestHandler):
			def do_GET(self) -> None:
				path = urlparse(self.path).path
				if path == "/events":
					self._events()
				else:
					self._static(path)

			def do_POST(self) -> None:
				if urlparse(self.path).path != "/action" or ui._on_action is None:
					self.send_error(404)
					return
				length = int(self.headers.get("Content-Length") or 0)
				try:
					body = json.loads(self.rfile.read(length) or b"{}")
					action = body["action"]
				except (json.JSONDecodeError, KeyError):
					self.send_error(400, "expected {\"action\": \"...\"}")
					return
				ui._on_action(action)
				self.send_response(204)
				self.end_headers()

			def _events(self) -> None:
				self.send_response(200)
				self.send_header("Content-Type", "text/event-stream")
				self.send_header("Cache-Control", "no-cache")
				self.send_header("Connection", "keep-alive")
				self.end_headers()
				q: queue.Queue = queue.Queue()
				with ui._lock:
					ui._listeners.append(q)
					backlog = list(ui._backlog)
				try:
					for line in backlog:
						self._sse(line)
					self._sse(": ready\n\n")
					while True:
						try:
							line = q.get(timeout=15)
						except queue.Empty:
							line = ": ping\n\n"
						self._sse(line)
				except (BrokenPipeError, ConnectionResetError):
					pass
				finally:
					with ui._lock:
						ui._listeners.remove(q)

			def _sse(self, payload: str) -> None:
				self.wfile.write(payload.encode("utf-8"))
				self.wfile.flush()

			def _static(self, path: str) -> None:
				if path in ("", "/"):
					path = "/index.html"
				target = (ui._static_dir / path.lstrip("/")).resolve()
				if not str(target).startswith(str(ui._static_dir.resolve())) or not target.is_file():
					# SPA fallback — unknown paths get the app shell
					target = ui._static_dir / "index.html"
					if not target.is_file():
						self.send_error(404, "web/dist not built")
						return
				body = target.read_bytes()
				self.send_response(200)
				self.send_header("Content-Type", _CONTENT_TYPES.get(target.suffix, "application/octet-stream"))
				self.send_header("Content-Length", str(len(body)))
				self.end_headers()
				self.wfile.write(body)

			def log_message(self, format, *args) -> None:
				pass  # quiet — request noise isn't useful in the log feed

		self._server = ThreadingHTTPServer(("0.0.0.0", self._port), Handler)
		threading.Thread(target=self._server.serve_forever, daemon=True).start()
		logger.info("Web UI on http://0.0.0.0:%d", self._port)

	def emit(self, kind: str, text: str) -> None:
		line = "data: " + json.dumps({"type": kind, "text": text, "ts": time.strftime("%H:%M:%S")}) + "\n\n"
		with self._lock:
			self._backlog.append(line)
			del self._backlog[:-BACKLOG]
			for listener in self._listeners:
				listener.put(line)
