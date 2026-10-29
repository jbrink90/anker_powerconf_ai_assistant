import logging
from collections import deque

import numpy as np

logger = logging.getLogger(__name__)


class EnergyVad:
	def __init__(
		self,
		samplerate: int,
		speech_ms: int = 250,
		silence_ms: int = 900,
		max_utterance_s: float = 30.0,
		threshold_factor: float = 3.0,
		min_rms: float = 300.0,
	) -> None:
		self._samplerate = samplerate
		self._speech_frames = int(samplerate * speech_ms / 1000)
		self._silence_frames = int(samplerate * silence_ms / 1000)
		self._max_frames = int(samplerate * max_utterance_s)
		self._threshold_factor = threshold_factor
		self._min_rms = min_rms
		self._threshold = min_rms
		self._noise_rms = 0.0
		self._in_speech = False
		self._speech_run = 0
		self._silence_run = 0
		self._buffer: list[np.ndarray] = []
		self._prebuffer: deque[np.ndarray] = deque()
		self._prebuffer_frames = int(samplerate * max(speech_ms, 600) / 1000)

	def calibrate(self, noise_pcm: np.ndarray) -> None:
		rms = _rms(noise_pcm)
		self._noise_rms = rms
		self._threshold = max(rms * self._threshold_factor, self._min_rms)
		logger.info("VAD calibrated: noise floor %.0f, threshold %.0f", rms, self._threshold)

	def configure(
		self,
		*,
		speech_ms: int,
		silence_ms: int,
		max_utterance_s: float,
		threshold_factor: float,
		min_rms: float,
	) -> None:
		"""Apply VAD tuning live while retaining any utterance currently in progress."""
		self._speech_frames = int(self._samplerate * speech_ms / 1000)
		self._silence_frames = int(self._samplerate * silence_ms / 1000)
		self._max_frames = int(self._samplerate * max_utterance_s)
		self._threshold_factor = threshold_factor
		self._min_rms = min_rms
		self._prebuffer_frames = int(self._samplerate * max(speech_ms, 600) / 1000)
		self._threshold = max(self._noise_rms * self._threshold_factor, self._min_rms)

	@property
	def in_speech(self) -> bool:
		return self._in_speech

	@property
	def threshold(self) -> float:
		return self._threshold

	def reset(self) -> None:
		self._in_speech = False
		self._speech_run = 0
		self._silence_run = 0
		self._buffer.clear()
		self._prebuffer.clear()

	def feed(self, chunk: np.ndarray) -> np.ndarray | None:
		"""Feed one chunk. Returns the completed utterance (int16 samples) or None."""
		rms = _rms(chunk)
		if rms > self._threshold:
			self._speech_run += len(chunk)
			self._silence_run = 0
			if not self._in_speech and self._speech_run >= self._speech_frames:
				self._in_speech = True
				self._buffer = list(self._prebuffer)
				self._prebuffer.clear()
				logger.debug("Speech started")
		elif self._in_speech:
			self._silence_run += len(chunk)
		else:
			self._speech_run = 0

		if not self._in_speech:
			self._prebuffer.append(chunk)
			while sum(len(block) for block in self._prebuffer) > self._prebuffer_frames:
				self._prebuffer.popleft()

		if self._in_speech:
			self._buffer.append(chunk)
			total = sum(len(block) for block in self._buffer)
			if self._silence_run >= self._silence_frames or total >= self._max_frames:
				self._in_speech = False
				self._speech_run = 0
				self._silence_run = 0
				logger.debug("Speech ended after %.1fs", total / self._samplerate)
				return np.concatenate(self._buffer)
		return None


def _rms(chunk: np.ndarray) -> float:
	return float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))
