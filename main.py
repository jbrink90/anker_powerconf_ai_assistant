import logging
import sys

from powerconf.assistant import Assistant
from powerconf.buttons import dump_events
from powerconf.config import load_settings


def main() -> None:
	logging.basicConfig(
		level=logging.INFO,
		format="%(asctime)s %(levelname)s %(name)s: %(message)s",
	)
	logging.getLogger("primp").setLevel(logging.WARNING)
	logging.getLogger("httpx").setLevel(logging.WARNING)
	if len(sys.argv) > 1 and sys.argv[1] == "buttons":
		dump_events()
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
