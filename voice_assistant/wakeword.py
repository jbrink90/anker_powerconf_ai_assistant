import logging
import inspect
import time
from pathlib import Path

import numpy as np

from voice_assistant.audio import MicStream

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
		model_path = Path(model_name).expanduser()
		custom_model = model_path.suffix.lower() in (".onnx", ".tflite")
		if custom_model and not model_path.is_file():
			raise FileNotFoundError(f"Custom wake-word model file not found: {model_path.resolve()}")
		if model_path.is_file() and not custom_model:
			raise ValueError("Custom wake-word models must use .onnx or .tflite format")
		try:
			if custom_model:
				model_kwargs = {"wakeword_model_paths": [str(model_path.resolve())]}
				# openwakeword 0.4.x infers the runtime and rejects this argument;
				# newer releases expose it explicitly.
				if "inference_framework" in inspect.signature(Model.__init__).parameters:
					model_kwargs["inference_framework"] = (
						"onnx" if model_path.suffix.lower() == ".onnx" else "tflite"
					)
				self._model = Model(**model_kwargs)
			else:
				self._model = Model()
		except Exception as exc:
			if custom_model:
				raise RuntimeError(
					f"Could not load custom wake-word model {model_path}: {exc}"
				) from exc
			raise RuntimeError(
				"Wake word models unavailable — run: "
				'python -c "from openwakeword.utils import download_models; download_models()"'
			) from exc
		available = list(self._model.models)
		self._name = available[0] if custom_model and available else next(
			(m for m in available if m.startswith(model_name)), None
		)
		if self._name is None:
			raise RuntimeError(f"Wake word model {model_name!r} not loaded; available: {available}")
		logger.info("Loaded wake-word model %s", self._name)
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
	quiet_s = min(5.0, duration_s / 3)
	print(
		f"Stay quiet for {quiet_s:.0f}s, then say your wake phrase several times "
		f"for the remaining {duration_s - quiet_s:.0f}s. Ctrl+C to stop early.\n"
	)
	mic.start()
	mic.clear()
	quiet_peaks: list[float] = []
	phrase_peaks: list[float] = []
	active_bucket = -1
	bucket_peak = 0.0
	try:
		start = time.monotonic()
		deadline = start + duration_s
		while time.monotonic() < deadline:
			chunk = mic.read(timeout=0.2)
			if chunk is None:
				continue
			elapsed = time.monotonic() - start
			bucket = int(elapsed)
			if bucket != active_bucket:
				if active_bucket >= 0:
					(quiet_peaks if active_bucket < quiet_s else phrase_peaks).append(bucket_peak)
				if active_bucket < quiet_s <= bucket:
					print("Now say the wake phrase several times.")
					detector.reset()
				active_bucket = bucket
				bucket_peak = 0.0
			score = detector.feed(chunk)
			bucket_peak = max(bucket_peak, score)
	except KeyboardInterrupt:
		pass
	finally:
		if active_bucket >= 0:
			(quiet_peaks if active_bucket < quiet_s else phrase_peaks).append(bucket_peak)
		mic.stop()
	positive = [score for score in phrase_peaks if score > 0.1]
	if not positive:
		print("\nNo phrase detections — check the mic, model, and wake phrase pronunciation.")
		return
	quiet_floor = float(np.percentile(quiet_peaks, 95)) if quiet_peaks else 0.0
	phrase_floor = float(np.percentile(positive, 25))
	print(f"\nQuiet-room 95th percentile: {quiet_floor:.3f}")
	print(f"Phrase 25th percentile: {phrase_floor:.3f}")
	if phrase_floor <= quiet_floor:
		print("Scores overlap; lower thresholds may cause false triggers. Try a quieter room or closer speaking distance.")
		return
	suggested = (quiet_floor + phrase_floor) / 2
	print(f"Suggested WAKEWORD_THRESHOLD={suggested:.2f} (recheck for false triggers in normal use)")
