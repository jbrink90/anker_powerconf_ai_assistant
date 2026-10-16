import logging
import time

import numpy as np

from powerconf.audio import MicStream

logger = logging.getLogger(__name__)

FRAME_SAMPLES = 1280  # 80ms of 16kHz audio — the frame size openwakeword expects


class WakeWordDetector:
	def __init__(self, model_name: str, samplerate: int = 16000) -> None:
		if samplerate != 16000:
			raise ValueError("openwakeword requires 16kHz audio")
		try:
			from openwakeword.model import Model
		except ImportError as exc:
			raise RuntimeError("openwakeword is not installed — run: pip install openwakeword") from exc
		try:
			self._model = Model()
		except Exception as exc:
			raise RuntimeError(
				"Wake word models unavailable — run: "
				'python -c "from openwakeword.utils import download_models; download_models()"'
			) from exc
		available = list(self._model.models)
		self._name = next((m for m in available if m.startswith(model_name)), None)
		if self._name is None:
			raise RuntimeError(f"Wake word model {model_name!r} not loaded; available: {available}")
		self._pending = np.empty(0, dtype=np.int16)

	def reset(self) -> None:
		self._pending = np.empty(0, dtype=np.int16)
		try:
			self._model.reset()
		except AttributeError:
			pass

	def feed(self, chunk: np.ndarray) -> float:
		"""Append audio; returns the highest wake-word score among frames completed this call."""
		self._pending = np.concatenate([self._pending, chunk])
		best = 0.0
		while len(self._pending) >= FRAME_SAMPLES:
			frame, self._pending = self._pending[:FRAME_SAMPLES], self._pending[FRAME_SAMPLES:]
			best = max(best, float(self._model.predict(frame)[self._name]))
		return best


def calibrate_wakeword(detector: WakeWordDetector, mic: MicStream, duration_s: float = 15.0) -> None:
	print(f"Say your wake phrase a few times over the next {duration_s:.0f}s. Ctrl+C to stop early.\n")
	mic.start()
	mic.clear()
	scores = []
	try:
		deadline = time.monotonic() + duration_s
		while time.monotonic() < deadline:
			chunk = mic.read(timeout=0.2)
			if chunk is None:
				continue
			score = detector.feed(chunk)
			if score > 0:
				scores.append(score)
				print(f"\rscore: {score:.3f}   peak: {max(scores):.3f}   ", end="", flush=True)
	except KeyboardInterrupt:
		pass
	finally:
		mic.stop()
	peaks = sorted((s for s in scores if s > 0.1), reverse=True)
	if not peaks:
		print("\nNo detections — check the mic or try a different model name.")
		return
	suggested = peaks[min(2, len(peaks) - 1)] * 0.75
	print(f"\nTop scores: {[round(p, 3) for p in peaks[:5]]}")
	print(f"Suggested WAKEWORD_THRESHOLD={suggested:.2f}")
