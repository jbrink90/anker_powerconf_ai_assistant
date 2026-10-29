import json
import logging
import queue
import socket
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


class _BoundedHTTPServer(ThreadingHTTPServer):
	"""Keep slow or abandoned browser requests from exhausting Pi threads."""

	daemon_threads = True
	request_queue_size = 16

	def __init__(self, *args, max_clients: int = 32, **kwargs) -> None:
		self._client_slots = threading.BoundedSemaphore(max_clients)
		super().__init__(*args, **kwargs)

	def process_request(self, request, client_address) -> None:
		if not self._client_slots.acquire(blocking=False):
			self.shutdown_request(request)
			return
		try:
			super().process_request(request, client_address)
		except Exception:
			self._client_slots.release()
			raise

	def process_request_thread(self, request, client_address) -> None:
		try:
			super().process_request_thread(request, client_address)
		finally:
			self._client_slots.release()


class WebUI:
	"""Serves the built web app plus an /events SSE feed the UI subscribes to."""

	def __init__(
		self,
		static_dir: Path,
		port: int,
		on_action=None,
		settings_provider=None,
		on_settings_update=None,
	) -> None:
		self._static_dir = static_dir
		self._port = port
		self._on_action = on_action
		self._settings_provider = settings_provider
		self._on_settings_update = on_settings_update
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
				elif path == "/api/settings" and ui._settings_provider is not None:
					try:
						self._send_json(200, {"values": ui._settings_provider()})
					except Exception:
						logger.exception("Could not read runtime settings")
						self._send_json(500, {"error": "Could not read settings; see the server log."})
				else:
					self._static(path)

			def do_POST(self) -> None:
				path = urlparse(self.path).path
				if path == "/api/settings" and ui._on_settings_update is not None:
					if not self._same_origin():
						self.send_error(403, "settings updates must come from this site")
						return
					try:
						body = self._read_json()
						values = ui._on_settings_update(body.get("settings"))
					except ValueError as exc:
						self._send_json(400, {"error": str(exc)})
					except Exception:
						logger.exception("Could not update runtime settings")
						self._send_json(500, {"error": "Could not save settings; see the server log."})
					else:
						self._send_json(200, {"values": values})
					return
				if path != "/action" or ui._on_action is None:
					self.send_error(404)
					return
				try:
					body = self._read_json()
					action = body["action"]
				except (json.JSONDecodeError, KeyError, ValueError):
					self.send_error(400, "expected {\"action\": \"...\"}")
					return
				ui._on_action(action)
				self.send_response(204)
				self.end_headers()

			def _read_json(self) -> dict:
				try:
					length = int(self.headers.get("Content-Length") or 0)
				except ValueError as exc:
					raise ValueError("Invalid Content-Length header.") from exc
				if length < 0:
					raise ValueError("Invalid Content-Length header.")
				if length > 32000:
					raise ValueError("Request body is too large.")
				raw = self.rfile.read(length)
				if len(raw) != length:
					raise ValueError("Incomplete request body.")
				body = json.loads(raw or b"{}")
				if not isinstance(body, dict):
					raise ValueError("Request body must be a JSON object.")
				return body

			def _same_origin(self) -> bool:
				origin = self.headers.get("Origin")
				host = self.headers.get("Host")
				return bool(origin and host and urlparse(origin).netloc.lower() == host.lower())

			def _security_headers(self) -> None:
				self.send_header("X-Content-Type-Options", "nosniff")
				self.send_header("X-Frame-Options", "DENY")
				self.send_header("Referrer-Policy", "no-referrer")

			def _send_json(self, status: int, body: dict) -> None:
				data = json.dumps(body).encode("utf-8")
				self.send_response(status)
				self.send_header("Content-Type", "application/json; charset=utf-8")
				self.send_header("Content-Length", str(len(data)))
				self.send_header("Cache-Control", "no-store")
				self._security_headers()
				self.end_headers()
				self.wfile.write(data)

			def _events(self) -> None:
				self.connection.settimeout(45)
				self.send_response(200)
				self.send_header("Content-Type", "text/event-stream")
				self.send_header("Cache-Control", "no-cache")
				self.send_header("Connection", "keep-alive")
				self._security_headers()
				self.end_headers()
				q: queue.Queue = queue.Queue(maxsize=BACKLOG)
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
				except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError):
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
				root = ui._static_dir.resolve()
				target = (root / path.lstrip("/")).resolve()
				try:
					target.relative_to(root)
				except ValueError:
					self.send_error(404)
					return
				if not target.is_file():
					# SPA fallback — unknown paths get the app shell
					target = root / "index.html"
					if not target.is_file():
						self.send_error(404, "web/dist not built")
						return
				body = target.read_bytes()
				self.send_response(200)
				self.send_header("Content-Type", _CONTENT_TYPES.get(target.suffix, "application/octet-stream"))
				self.send_header("Content-Length", str(len(body)))
				self._security_headers()
				self.end_headers()
				self.wfile.write(body)

			def log_message(self, format, *args) -> None:
				pass  # quiet — request noise isn't useful in the log feed

		self._server = _BoundedHTTPServer(("0.0.0.0", self._port), Handler)
		threading.Thread(target=self._server.serve_forever, daemon=True).start()
		logger.info("Web UI on http://0.0.0.0:%d", self._port)

	def emit(self, kind: str, text: str) -> None:
		line = "data: " + json.dumps({"type": kind, "text": text, "ts": time.strftime("%H:%M:%S")}) + "\n\n"
		with self._lock:
			self._backlog.append(line)
			del self._backlog[:-BACKLOG]
			for listener in self._listeners:
				try:
					listener.put_nowait(line)
				except queue.Full:
					try:
						listener.get_nowait()
						listener.put_nowait(line)
					except (queue.Empty, queue.Full):
						pass
