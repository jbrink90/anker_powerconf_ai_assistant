"""Allowlisted, persistent runtime settings for the web control panel."""

import math
import os
import shutil
import stat
import tempfile
from pathlib import Path


_FIELDS = {
	"system_prompt": ("SYSTEM_PROMPT", str, None, None),
	"wakeword_threshold": ("WAKEWORD_THRESHOLD", float, 0.0, 1.0),
	"wakeword_cooldown_s": ("WAKEWORD_COOLDOWN_S", float, 0.0, 60.0),
	"followup_s": ("FOLLOWUP_S", float, 0.0, 120.0),
	"wake_ack_tone": ("WAKE_ACK_TONE", bool, None, None),
	"barge_in": ("BARGE_IN", bool, None, None),
	"barge_in_min_rms": ("BARGE_IN_MIN_RMS", float, 0.0, 10000.0),
	"barge_in_factor": ("BARGE_IN_FACTOR", float, 0.1, 10.0),
	"noise_reduce": ("NOISE_REDUCE", bool, None, None),
	"save_audio": ("SAVE_AUDIO", bool, None, None),
	"streaming": ("STREAMING", bool, None, None),
	"echo_mode": ("ECHO_MODE", bool, None, None),
	"vad_speech_ms": ("VAD_SPEECH_MS", int, 50, 2000),
	"vad_silence_ms": ("VAD_SILENCE_MS", int, 100, 5000),
	"vad_max_utterance_s": ("VAD_MAX_UTTERANCE_S", float, 3.0, 120.0),
	"vad_threshold_factor": ("VAD_THRESHOLD_FACTOR", float, 1.0, 20.0),
	"vad_min_rms": ("VAD_MIN_RMS", float, 0.0, 10000.0),
}

_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


def public_values(settings) -> dict:
	"""Return only editable, non-secret settings to the browser."""
	return {name: getattr(settings, name) for name in _FIELDS}


def validate_updates(payload: object) -> dict:
	if not isinstance(payload, dict) or not payload:
		raise ValueError("Provide at least one setting to update.")
	unknown = set(payload) - _FIELDS.keys()
	if unknown:
		raise ValueError(f"Unsupported setting: {sorted(unknown)[0]}")
	if len(payload) > len(_FIELDS):
		raise ValueError("Too many settings in one update.")

	updates = {}
	for name, value in payload.items():
		env_name, kind, minimum, maximum = _FIELDS[name]
		if kind is bool:
			if not isinstance(value, bool):
				raise ValueError(f"{name} must be true or false.")
			updates[name] = value
		elif kind is str:
			if not isinstance(value, str) or not value.strip():
				raise ValueError(f"{name} must not be empty.")
			if len(value) > 12000:
				raise ValueError(f"{name} must be 12,000 characters or fewer.")
			updates[name] = value
		else:
			if isinstance(value, bool) or not isinstance(value, (int, float)):
				raise ValueError(f"{name} must be a number.")
			number = float(value)
			if not math.isfinite(number) or number < minimum or number > maximum:
				raise ValueError(f"{name} must be between {minimum} and {maximum}.")
			updates[name] = int(number) if kind is int else number
	return updates


def save_updates(updates: dict) -> None:
	"""Atomically update only allowlisted keys while preserving other .env content."""
	from dotenv import set_key

	_ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
	with tempfile.NamedTemporaryFile(
		mode="w", encoding="utf-8", dir=_ENV_PATH.parent, prefix=".env.", suffix=".tmp", delete=False
	) as temp:
		temp_path = Path(temp.name)
	try:
		if _ENV_PATH.exists():
			shutil.copyfile(_ENV_PATH, temp_path)
			mode = stat.S_IMODE(_ENV_PATH.stat().st_mode)
			os.chmod(temp_path, mode)
		for name, value in updates.items():
			env_name, kind, _, _ = _FIELDS[name]
			serialized = ("1" if value else "0") if kind is bool else str(value)
			set_key(temp_path, env_name, serialized, quote_mode="auto")
		os.replace(temp_path, _ENV_PATH)
	except Exception:
		temp_path.unlink(missing_ok=True)
		raise
