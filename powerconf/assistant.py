import logging
import queue
import shlex
import subprocess
import threading
import time
from pathlib import Path

import numpy as np

from powerconf.audio import (
	MicStream,
	bluetooth_audio_ready,
	load_audio,
	play_samples,
	play_wav,
	reduce_noise,
	wav_bytes,
)
from powerconf.buttons import ButtonListener, find_button_device
from powerconf.clients import OllamaClient, SpeachesClient
from powerconf.config import Settings
from powerconf.vad import EnergyVad

logger = logging.getLogger(__name__)

SAVE_DIR = Path("recordings")


class Assistant:
	def __init__(self, settings: Settings) -> None:
		self.settings = settings
		self.speaches = SpeachesClient(
			settings.speaches_base_url,
			settings.stt_model,
			settings.tts_model,
			settings.tts_voice,
		)
		if settings.tts_provider == "fish":
			from powerconf.clients import FishAudioClient

			self.tts = FishAudioClient(
				settings.fish_audio_base_url,
				settings.fish_audio_api_key,
				settings.fish_audio_model,
				settings.fish_audio_voice,
			)
		else:
			self.tts = self.speaches
		self.ollama = OllamaClient(settings.ollama_base_url, settings.ollama_model)
		self.messages: list[dict[str, str]] = [{"role": "system", "content": settings.system_prompt}]
		self.actions: queue.Queue[str] = queue.Queue()
		self._speaking = threading.Event()
		self._followup_until = 0.0

	def run(self) -> None:
		from powerconf.audio import find_device, find_source_name

		self._wait_for_speaker()
		source_name = find_source_name(self.settings.audio_input_device)
		if source_name:
			logger.info("Mic source: %s", source_name)
		mic = MicStream(source_name, self.settings.sample_rate)
		self._mic = mic
		self._out_device = find_device(self.settings.audio_output_device, "output")
		listener = self._start_buttons()

		vad = EnergyVad(
			self.settings.sample_rate,
			speech_ms=self.settings.vad_speech_ms,
			silence_ms=self.settings.vad_silence_ms,
			max_utterance_s=self.settings.vad_max_utterance_s,
			threshold_factor=self.settings.vad_threshold_factor,
			min_rms=self.settings.vad_min_rms,
		)
		self._vad = vad

		detector = None
		if self.settings.wakeword_model:
			from powerconf.wakeword import WakeWordDetector

			detector = WakeWordDetector(self.settings.wakeword_model, self.settings.sample_rate)
			logger.info(
				"Wake word %r armed (threshold %.2f)",
				self.settings.wakeword_model,
				self.settings.wakeword_threshold,
			)

		if self.settings.mcp_servers:
			import json as _json

			from powerconf.tools import init_mcp

			try:
				init_mcp(_json.loads(self.settings.mcp_servers))
			except _json.JSONDecodeError:
				logger.warning("MCP_SERVERS is not valid JSON — skipping MCP setup")

		mic.start()
		logger.info("Listening")
		self._calibrate(mic, vad)

		awake_until = 0.0
		self._detector = detector
		self._wake_cooldown = 0.0
		try:
			while True:
				self._handle_actions(mic)
				chunk = mic.read(timeout=0.05)
				if chunk is None or self._speaking.is_set():
					continue
				now = time.monotonic()
				if detector is not None and now > max(awake_until, self._followup_until) and now > self._wake_cooldown:
					score = detector.feed(chunk)
					if score >= self.settings.wakeword_threshold:
						logger.info("Wake word detected (score %.2f) — listening", score)
						awake_until = time.monotonic() + 6.0
				utterance = vad.feed(chunk)
				if utterance is not None:
					if detector is None or now <= max(awake_until, self._followup_until):
						self._process(utterance)
					awake_until = 0.0
					self._followup_until = 0.0
				elif vad.in_speech and now <= max(awake_until, self._followup_until):
					# keep the window open while a command is still being spoken
					awake_until = now + 2.0
					self._followup_until = now + 2.0
		finally:
			mic.stop()
			listener.stop()

	def _wait_for_speaker(self) -> None:
		while True:
			has_sink, has_source = bluetooth_audio_ready()
			has_buttons = find_button_device(self.settings.button_device_name)
			if has_buttons is not None:
				has_buttons.close()
			missing = []
			if not has_sink:
				missing.append("speaker sink")
			if not has_source:
				missing.append("mic source")
			if has_buttons is None:
				missing.append("button input")
			if not missing:
				logger.info("PowerConf connected: sink, source, and buttons present")
				return
			logger.warning("Waiting for PowerConf — missing: %s", ", ".join(missing))
			time.sleep(3.0)

	def _start_buttons(self) -> ButtonListener:
		device = find_button_device(self.settings.button_device_name)
		if device is not None:
			device.close()
		else:
			logger.warning(
				"No button input device found matching %r yet; will keep rescanning",
				self.settings.button_device_name,
			)
		listener = ButtonListener(self.settings.button_device_name, self.actions)
		listener.start()
		return listener

	def _handle_actions(self, mic: MicStream) -> None:
		while True:
			try:
				action = self.actions.get_nowait()
			except queue.Empty:
				return
			# "interrupt" is a no-op here — its only effect is checked during playback
			if action == "play_file":
				self._play_file(mic)
			elif action == "volume_up":
				self._run_command(self.settings.volume_command.format(op="+5%"))
			elif action == "volume_down":
				self._run_command(self.settings.volume_command.format(op="-5%"))
			elif action == "mute":
				self._run_command(self.settings.mute_command)

	def _play_file(self, mic: MicStream) -> None:
		path = Path(self.settings.double_tap_wav)
		if not path.is_file():
			logger.warning("Double-tap wav not found: %s", path)
			return
		try:
			samples, samplerate = load_audio(path)
		except Exception:
			logger.exception("Could not decode %s", path)
			return
		logger.info("Playing %s", path)
		self._drop_pending_interrupts()
		self._speaking.set()
		try:
			play_samples(samples, samplerate, device=self._out_device, should_stop=self._playback_interrupted)
		finally:
			self._speaking.clear()
			mic.clear()
			self._vad.reset()
			self._suppress_wake_echo()

	def _calibrate(self, mic: MicStream, vad: EnergyVad) -> None:
		logger.info("Calibrating noise floor...")
		mic.clear()
		chunks = []
		deadline = time.monotonic() + 1.0
		while time.monotonic() < deadline:
			chunk = mic.read(timeout=0.1)
			if chunk is not None:
				chunks.append(chunk)
		if not chunks:
			logger.warning("No audio during calibration — mic may be suspended or disconnected")
			return
		vad.calibrate(np.concatenate(chunks))
		if vad.threshold <= self.settings.vad_min_rms:
			logger.warning(
				"Noise floor near zero — mic may be muted or delivering silence; speech detection may false-trigger"
			)

	def _process(self, pcm: np.ndarray) -> None:
		timestamp = time.strftime("%Y%m%d-%H%M%S")
		if self.settings.save_audio:
			self._save(f"in_{timestamp}.wav", wav_bytes(pcm, self.settings.sample_rate))
		if self.settings.noise_reduce:
			pcm = reduce_noise(pcm, self.settings.sample_rate)
			if self.settings.save_audio:
				self._save(f"in_filtered_{timestamp}.wav", wav_bytes(pcm, self.settings.sample_rate))
		input_wav = wav_bytes(pcm, self.settings.sample_rate)
		try:
			text = self.speaches.transcribe(input_wav)
		except Exception:
			logger.exception("Speech-to-text failed")
			return
		if not text:
			logger.info("Empty transcript — check the mic isn't muted on the device")
			return
		logger.info("You: %s", text)
		if self.settings.echo_mode:
			reply = text
		else:
			self.messages.append({"role": "user", "content": text})
			try:
				reply = self._chat()
			except Exception:
				logger.exception("Ollama chat failed")
				return
		reply = _strip_markdown(_unwrap_json_reply(reply))
		logger.info("Assistant: %s", reply)
		if not reply.strip():
			logger.warning("Empty reply from model — nothing to speak")
			return
		try:
			audio = self.tts.synthesize(reply)
		except Exception:
			logger.exception("Text-to-speech failed")
			return
		if not audio.startswith(b"RIFF"):
			logger.error("TTS returned non-WAV data (%d bytes) — skipping playback", len(audio))
			return
		if self.settings.save_audio:
			self._save(f"out_{timestamp}.wav", audio)
		if self._cancel_pending("interrupt"):
			logger.info("Response cancelled")
			return
		try:
			self._play_with_barge_in(
				lambda should_stop: play_wav(audio, device=self._out_device, should_stop=should_stop)
			)
		except Exception:
			logger.exception("Playback failed")

	def _play_with_barge_in(self, play) -> None:
		self._mic.clear()  # drop audio queued during the STT/Ollama/TTS gap
		self._vad.reset()
		self._followup_until = 0.0
		self._speaking.set()
		watcher = threading.Thread(target=self._watch_barge_in, args=(self._mic,), daemon=True)
		if self.settings.barge_in:
			watcher.start()
		try:
			play(self._playback_interrupted)
		finally:
			self._speaking.clear()
			if watcher.is_alive():
				watcher.join(timeout=1)
			if not self._followup_until:
				# no barge-in: drop playback audio so it can't re-trigger the VAD
				self._mic.clear()
				self._vad.reset()
				# stay awake briefly for follow-ups — no wake word needed
				self._followup_until = time.monotonic() + self.settings.followup_s
			self._suppress_wake_echo()

	def _watch_barge_in(self, mic: MicStream) -> None:
		threshold = max(self.settings.barge_in_min_rms, self._vad.threshold * self.settings.barge_in_factor)
		speech_run = 0
		silence_run = 0
		speech_frames = int(self.settings.sample_rate * self.settings.vad_speech_ms / 1000)
		# tolerate short dips between syllables — continuous speech still dips below threshold
		silence_grace = int(self.settings.sample_rate * 0.4)
		max_rms = 0.0
		try:
			while self._speaking.is_set():
				chunk = mic.read(timeout=0.1)
				if chunk is None:
					continue
				rms = float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))
				max_rms = max(max_rms, rms)
				if rms > threshold:
					speech_run += len(chunk)
					silence_run = 0
					if speech_run >= speech_frames:
						logger.info("Barge-in: speech detected during playback (rms %.0f > %.0f) — interrupting", rms, threshold)
						self._followup_until = time.monotonic() + 8.0
						self.actions.put("interrupt")
						return
				elif speech_run > 0:
					silence_run += len(chunk)
					if silence_run >= silence_grace:
						speech_run = 0
						silence_run = 0
				# keep the real VAD fed so the barge-in utterance keeps its prebuffer
				self._vad.feed(chunk)
		finally:
			logger.info("Barge-in watcher done (peak mic rms %.0f, threshold %.0f)", max_rms, threshold)

	def _suppress_wake_echo(self) -> None:
		if self._detector is not None:
			self._detector.reset()
			# don't feed the detector for a moment — residual reverb of our own playback
			self._wake_cooldown = time.monotonic() + 1.5

	def _chat(self) -> str:
		from powerconf.tools import all_tools, run_tool

		for _ in range(4):
			message = self.ollama.chat(self.messages, tools=all_tools())
			self.messages.append(message)
			tool_calls = message.get("tool_calls") or []
			if not tool_calls:
				return message.get("content", "").strip()
			for call in tool_calls:
				function = call["function"]
				arguments = function.get("arguments") or {}
				logger.info("Tool call: %s(%s)", function["name"], arguments)
				try:
					result = run_tool(function["name"], arguments)
				except Exception:
					logger.exception("Tool %s failed", function["name"])
					result = f"{function['name']} failed"
				self.messages.append({"role": "tool", "content": str(result)})
		return "Sorry, I couldn't finish that research."

	def _save(self, name: str, data: bytes) -> None:
		path = SAVE_DIR / name
		SAVE_DIR.mkdir(exist_ok=True)
		path.write_bytes(data)
		logger.info("Saved %s", path)

	def _playback_interrupted(self) -> bool:
		interrupted = False
		held = []
		while True:
			try:
				action = self.actions.get_nowait()
			except queue.Empty:
				break
			if action == "interrupt":
				interrupted = True
			elif action == "play_file":
				interrupted = True
				held.append(action)
			else:
				held.append(action)
		for action in held:
			self.actions.put(action)
		if interrupted:
			logger.info("Playback interrupted")
		return interrupted

	def _cancel_pending(self, *actions: str) -> bool:
		held = []
		found = False
		while True:
			try:
				action = self.actions.get_nowait()
			except queue.Empty:
				break
			if action in actions:
				found = True
			else:
				held.append(action)
		for action in held:
			self.actions.put(action)
		return found

	def _drop_pending_interrupts(self) -> None:
		# a tap queued during STT/Ollama/TTS processing must not kill playback before it starts
		held = []
		while True:
			try:
				action = self.actions.get_nowait()
			except queue.Empty:
				break
			if action != "interrupt":
				held.append(action)
		for action in held:
			self.actions.put(action)

	def _run_command(self, command: str) -> None:
		try:
			subprocess.run(shlex.split(command), check=False, capture_output=True)
		except OSError:
			logger.warning("Command failed: %s", command)


def _strip_markdown(reply: str) -> str:
	# turn markdown-ish output into speakable prose
	import re

	text = re.sub(r"[*_`#]", "", reply)
	text = re.sub(r"^\s*[-•]\s*", "", text, flags=re.MULTILINE)
	text = re.sub(r"\n{2,}", ". ", text)
	text = re.sub(r"\n", ", ", text)
	return re.sub(r"\s{2,}", " ", text).strip()


def _unwrap_json_reply(reply: str) -> str:
	# small models sometimes emit a raw tool call as content — dig out the payload string
	import json

	text = reply.strip()
	if not text.startswith("{"):
		return reply
	try:
		data = json.loads(text)
	except json.JSONDecodeError:
		return reply
	while isinstance(data, dict):
		for key in ("parameters", "arguments", "content"):
			if isinstance(data.get(key), (dict, str)):
				data = data[key]
				break
		else:
			data = next((v for v in data.values() if isinstance(v, dict)), "")
	if isinstance(data, str) and data.strip():
		logger.warning("Model replied with a raw tool-call JSON — extracted payload")
		return data.strip()
	return reply
