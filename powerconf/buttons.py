import logging
import queue
import threading

import selectors

import evdev
from evdev import InputDevice, ecodes

logger = logging.getLogger(__name__)

KEY_ACTIONS = {
	ecodes.KEY_PLAYPAUSE: "interrupt",
	ecodes.KEY_PLAYCD: "interrupt",
	ecodes.KEY_PAUSECD: "interrupt",
	ecodes.KEY_VOICECOMMAND: "interrupt",
	ecodes.KEY_PHONE: "interrupt",
	ecodes.KEY_VOLUMEUP: "volume_up",
	ecodes.KEY_VOLUMEDOWN: "volume_down",
	ecodes.KEY_MUTE: "mute",
	ecodes.KEY_MICMUTE: "mute",
	ecodes.KEY_NEXTSONG: "play_file",
	ecodes.KEY_PREVIOUSSONG: "play_file",
}


def find_button_device(name_substring: str, allow_fallback: bool = True) -> InputDevice | None:
	"""Prefers a name match; optionally falls back to any device with AVRCP-like keys."""
	candidates = []
	for path in evdev.list_devices():
		try:
			device = InputDevice(path)
		except OSError:
			continue
		key_caps = device.capabilities().get(ecodes.EV_KEY, [])
		if name_substring and name_substring.lower() in device.name.lower():
			return device
		if allow_fallback and any(
			key in key_caps for key in (ecodes.KEY_PLAYPAUSE, ecodes.KEY_PLAYCD, ecodes.KEY_VOICECOMMAND)
		):
			candidates.append(device)
	if candidates:
		return candidates[0]
	return None


def is_name_match(device: InputDevice, name_substring: str) -> bool:
	return bool(name_substring) and name_substring.lower() in device.name.lower()


class ButtonListener:
	def __init__(self, name_substring: str, actions: queue.Queue[str], allow_fallback: bool = True) -> None:
		self._name_substring = name_substring
		self._actions = actions
		self._allow_fallback = allow_fallback
		self._stop = threading.Event()
		self._thread = threading.Thread(target=self._run, daemon=True)

	def start(self) -> None:
		self._thread.start()

	def stop(self) -> None:
		self._stop.set()
		self._thread.join(timeout=1)

	def _run(self) -> None:
		while not self._stop.is_set():
			device = find_button_device(self._name_substring, self._allow_fallback)
			if device is None:
				if self._stop.wait(3.0):
					return
				continue
			logger.info(
				"Button device%s: %s (%s)",
				"" if is_name_match(device, self._name_substring) else " (fallback)",
				device.name,
				device.path,
			)
			try:
				self._read_events(device)
			except OSError:
				logger.warning("Button device %s lost; rescanning", device.name)
			finally:
				device.close()
			if self._stop.wait(1.0):
				return

	def _read_events(self, device: InputDevice) -> None:
		selector = selectors.DefaultSelector()
		selector.register(device, selectors.EVENT_READ)
		try:
			while not self._stop.is_set():
				if not selector.select(timeout=3.0):
					# parked on a fallback device? keep looking for the named one
					if is_name_match(device, self._name_substring):
						continue
					replacement = find_button_device(self._name_substring, self._allow_fallback)
					if replacement is not None and is_name_match(replacement, self._name_substring):
						replacement.close()
						return
					if replacement is not None:
						replacement.close()
					continue
				for event in device.read():
					if event.type != ecodes.EV_KEY or event.value != 1:
						continue
					action = KEY_ACTIONS.get(event.code)
					logger.debug("Key %s -> %s", ecodes.KEY.get(event.code, event.code), action)
					if action:
						logger.info("Button: %s", action)
						self._actions.put(action)
		finally:
			selector.close()


def dump_events() -> None:
	"""Print every key event from every input device. For discovering the PowerConf's key codes."""
	devices = []
	for path in evdev.list_devices():
		try:
			device = InputDevice(path)
		except OSError:
			continue
		if ecodes.EV_KEY in device.capabilities():
			devices.append(device)
			print(f"{device.path}: {device.name}")
	if not devices:
		print("No input devices found (check permissions for /dev/input/*)")
		return
	print("\nPress buttons on the PowerConf. Ctrl+C to quit.\n")

	selector = selectors.DefaultSelector()
	for device in devices:
		selector.register(device, selectors.EVENT_READ)
	while True:
		for key, _ in selector.select():
			device = key.fileobj
			for event in device.read():
				if event.type == ecodes.EV_KEY:
					name = ecodes.KEY.get(event.code, event.code)
					state = {0: "up", 1: "down", 2: "hold"}.get(event.value, event.value)
					print(f"{device.name}: {name} {state}")


if __name__ == "__main__":
	dump_events()
