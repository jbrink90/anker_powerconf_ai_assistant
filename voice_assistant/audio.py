import io
import logging
import queue
import subprocess
import threading
import wave

import numpy as np
import sounddevice as sd

logger = logging.getLogger(__name__)

BLOCK_MS = 30


def find_device(name_substring: str, kind: str) -> int | None:
	try:
		devices = list(enumerate(sd.query_devices()))
	except Exception as exc:
		logger.warning("Could not enumerate audio devices: %s", exc)
		return None
	for index, info in devices:
		channels = info["max_input_channels"] if kind == "input" else info["max_output_channels"]
		if name_substring and channels > 0 and name_substring.lower() in info["name"].lower():
			logger.debug("Using %s device %s", kind, info["name"])
			return index
	if kind == "output" and not name_substring:
		preferred = ("powerconf", "anker", "bluez", "bluetooth")
		for index, info in devices:
			if info["max_output_channels"] > 0 and any(token in info["name"].lower() for token in preferred):
				logger.debug("Auto-selected likely Bluetooth output %s", info["name"])
				return index
		try:
			logger.debug("Using default output device %s", sd.query_devices(kind="output")["name"])
		except Exception:
			logger.warning("No default output device is available")
	elif name_substring:
		logger.debug("No %s device matching %r", kind, name_substring)
	return None


def bluetooth_audio_ready() -> tuple[bool, bool]:
	"""Returns (has_sink, has_source) for bluez PipeWire nodes."""
	try:
		sinks = subprocess.check_output(["pactl", "list", "sinks", "short"], text=True)
		sources = subprocess.check_output(["pactl", "list", "sources", "short"], text=True)
	except (OSError, subprocess.CalledProcessError):
		return False, False
	return "bluez_output" in sinks, "bluez_input" in sources


def find_source_name(substring: str) -> str | None:
	"""Resolve a substring (e.g. 'bluez') to a PipeWire source name for parecord --device."""
	if not substring:
		return None
	try:
		sources = subprocess.check_output(["pactl", "list", "sources", "short"], text=True)
	except (OSError, subprocess.CalledProcessError):
		return None
	for line in sources.splitlines():
		parts = line.split("\t")
		if len(parts) > 1 and substring.lower() in parts[1].lower() and not parts[1].endswith(".monitor"):
			return parts[1]
	logger.warning("No source matching %r, using default", substring)
	return None


class MicStream:
	"""Captures via parecord (PipeWire-native) — PortAudio input goes silent during playback on bluez."""
	def __init__(self, source_name: str | None, samplerate: int) -> None:
		self.samplerate = samplerate
		self._source_name = source_name
		self.block_frames = int(samplerate * BLOCK_MS / 1000)
		self._queue: queue.Queue[np.ndarray] = queue.Queue()
		self._proc: subprocess.Popen | None = None
		self._reader: threading.Thread | None = None

	def start(self) -> None:
		cmd = [
			"pacat",
			"--record",
			f"--rate={self.samplerate}",
			"--channels=1",
			"--file-format=raw",
			"--latency-msec=30",
		]
		if self._source_name:
			cmd.append(f"--device={self._source_name}")
		self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
		self._reader = threading.Thread(target=self._read, daemon=True)
		self._reader.start()

	def _read(self) -> None:
		assert self._proc is not None and self._proc.stdout is not None
		block_bytes = self.block_frames * 2
		while True:
			data = self._proc.stdout.read(block_bytes)
			if not data:
				return
			self._queue.put(np.frombuffer(data, dtype=np.int16))

	def stop(self) -> None:
		if self._proc is not None:
			self._proc.terminate()
			self._proc = None
		if self._reader is not None:
			self._reader.join(timeout=1)
			self._reader = None

	def read(self, timeout: float = 0.1) -> np.ndarray | None:
		try:
			return self._queue.get(timeout=timeout)
		except queue.Empty:
			return None

	def clear(self) -> None:
		while True:
			try:
				self._queue.get_nowait()
			except queue.Empty:
				return


def reduce_noise(pcm: np.ndarray, samplerate: int, gain_db: float = 6.0) -> np.ndarray:
	import noisereduce as nr

	filtered = nr.reduce_noise(y=pcm.astype(np.float32) / 32768.0, sr=samplerate, stationary=False)
	filtered *= 10 ** (gain_db / 20)
	return np.clip(filtered * 32768.0, -32768, 32767).astype(np.int16)


def wav_bytes(pcm: np.ndarray, samplerate: int) -> bytes:
	buffer = io.BytesIO()
	with wave.open(buffer, "wb") as wav:
		wav.setnchannels(1)
		wav.setsampwidth(2)
		wav.setframerate(samplerate)
		wav.writeframes(pcm.astype(np.int16).tobytes())
	return buffer.getvalue()


def load_audio(path) -> tuple[np.ndarray, int]:
	"""Decode an audio file to int16 samples (n, channels) + samplerate. Handles wav/flac/ogg/mp3."""
	try:
		import soundfile as sf

		return sf.read(str(path), dtype="int16", always_2d=True)
	except ImportError:
		with wave.open(str(path), "rb") as wav:
			frames = wav.readframes(wav.getnframes())
			samplerate = wav.getframerate()
			if wav.getsampwidth() != 2:
				raise ValueError(f"Unsupported sample width: {wav.getsampwidth()} (install soundfile for other formats)")
			return np.frombuffer(frames, dtype=np.int16).reshape(-1, wav.getnchannels()), samplerate


def play_samples(samples: np.ndarray, samplerate: int, device: int | None = None, should_stop=None) -> None:
	channels = samples.shape[1] if samples.ndim > 1 else 1
	chunk = samplerate // 10
	with sd.OutputStream(samplerate=samplerate, channels=channels, dtype="int16", device=device) as stream:
		index = 0
		while index < len(samples):
			if should_stop is not None and should_stop():
				break
			end = min(index + chunk, len(samples))
			stream.write(samples[index:end])
			index = end


def play_wav(data: bytes, device: int | None = None, should_stop=None) -> None:
	with wave.open(io.BytesIO(data), "rb") as wav:
		frames = wav.readframes(wav.getnframes())
		samplerate = wav.getframerate()
		channels = wav.getnchannels()
		if wav.getsampwidth() != 2:
			raise ValueError(f"Unsupported sample width: {wav.getsampwidth()}")
	samples = np.frombuffer(frames, dtype=np.int16).reshape(-1, channels)
	play_samples(samples, samplerate, device, should_stop)


def play_wake_ack(device: int | None = None) -> None:
	"""Play a short, soft rising two-note cue when the wake word is recognized."""
	try:
		output_info = sd.query_devices(device, "output") if device is not None else sd.query_devices(kind="output")
		samplerate = int(output_info["default_samplerate"])
	except Exception:
		samplerate = 48000
	duration = 0.12
	t = np.arange(int(samplerate * duration), dtype=np.float32) / samplerate
	frequencies = np.where(t < duration / 2, 660.0, 880.0)
	envelope = np.minimum(1.0, np.minimum(t / 0.008, (duration - t) / 0.015))
	samples_float = np.sin(2 * np.pi * frequencies * t) * envelope * 0.12
	samples = np.clip(samples_float * 32767, -32768, 32767).astype(np.int16)
	play_samples(samples, samplerate, device=device)


def dump_audio_devices() -> None:
	"""Print PipeWire/PulseAudio routes and PortAudio devices for troubleshooting."""
	for label, args in (
		("default sink", ["pactl", "get-default-sink"]),
		("default source", ["pactl", "get-default-source"]),
		("sinks", ["pactl", "list", "sinks", "short"]),
		("sources", ["pactl", "list", "sources", "short"]),
	):
		try:
			output = subprocess.check_output(args, text=True, stderr=subprocess.STDOUT).strip()
		except (OSError, subprocess.CalledProcessError) as exc:
			output = str(exc)
		print(f"\n{label}:\n{output or '(none)'}")
	print("\nPortAudio devices:")
	try:
		default_input, default_output = sd.default.device
		for index, info in enumerate(sd.query_devices()):
			if info["max_input_channels"] or info["max_output_channels"]:
				flags = []
				if index == default_input:
					flags.append("default input")
				if index == default_output:
					flags.append("default output")
				print(
					f"  {index}: {info['name']} "
					f"(in={info['max_input_channels']}, out={info['max_output_channels']})"
					+ (f" [{' / '.join(flags)}]" if flags else "")
				)
	except Exception as exc:
		print(f"  Could not enumerate PortAudio devices: {exc}")
