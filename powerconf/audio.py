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
	if not name_substring:
		return None
	for index, info in enumerate(sd.query_devices()):
		channels = info["max_input_channels"] if kind == "input" else info["max_output_channels"]
		if channels > 0 and name_substring.lower() in info["name"].lower():
			logger.info("Using %s device %s", kind, info["name"])
			return index
	logger.warning("No %s device matching %r, using default", kind, name_substring)
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
