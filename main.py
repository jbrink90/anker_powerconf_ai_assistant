import logging
import os
import sys
import time
from datetime import date
from pathlib import Path

from powerconf.assistant import Assistant
from powerconf.buttons import dump_events
from powerconf.config import load_settings


class _DailyLogHandler(logging.Handler):
	"""Append log records to logs/YYYY-MM-DD.log, switching files at midnight."""

	def __init__(self, directory: Path) -> None:
		super().__init__()
		self.directory = directory
		self._day = ""
		self._stream = None

	def emit(self, record: logging.LogRecord) -> None:
		day = date.today().isoformat()
		if day != self._day:
			if self._stream is not None:
				self._stream.close()
			self.directory.mkdir(parents=True, exist_ok=True)
			path = self.directory / f"{day}.log"
			fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
			os.chmod(path, 0o600)
			self._stream = os.fdopen(fd, "a", encoding="utf-8")
			self._day = day
			self._stream.write(f"Log started {time.strftime('%H:%M:%S')}\n")
			self._stream.flush()
		self._stream.write(self.format(record) + "\n")
		self._stream.flush()

	def close(self) -> None:
		if self._stream is not None:
			self._stream.close()
			self._stream = None
		super().close()


class _WebAddressOnly(logging.Filter):
	def filter(self, record: logging.LogRecord) -> bool:
		return record.name == "powerconf.webui" and record.getMessage().startswith("Web UI on ")


def _configure_logging() -> None:
	root = logging.getLogger()
	root.setLevel(logging.INFO)
	root.handlers.clear()
	logging.captureWarnings(True)
	file_handler = _DailyLogHandler(Path(__file__).resolve().parent / "logs")
	file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
	root.addHandler(file_handler)
	console_handler = logging.StreamHandler(sys.__stderr__)
	console_handler.setLevel(logging.INFO)
	console_handler.addFilter(_WebAddressOnly())
	console_handler.setFormatter(logging.Formatter("%(message)s"))
	root.addHandler(console_handler)


def main() -> None:
	_configure_logging()
	logging.getLogger("primp").setLevel(logging.WARNING)
	logging.getLogger("httpx").setLevel(logging.WARNING)
	if len(sys.argv) > 1 and sys.argv[1] == "buttons":
		dump_events()
		return
	if len(sys.argv) > 1 and sys.argv[1] == "audio":
		from powerconf.audio import dump_audio_devices

		dump_audio_devices()
		return
	settings = load_settings()
	if len(sys.argv) > 1 and sys.argv[1] == "wakecal":
		from powerconf.audio import MicStream, find_source_name
		from powerconf.wakeword import WakeWordDetector, calibrate_wakeword

		mic = MicStream(find_source_name(settings.audio_input_device), settings.sample_rate)
		calibrate_wakeword(WakeWordDetector(settings.wakeword_model), mic)
		return
	try:
		Assistant(settings).run()
	except KeyboardInterrupt:
		pass


if __name__ == "__main__":
	main()
