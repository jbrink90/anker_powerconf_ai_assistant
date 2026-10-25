import logging
import queue
import re
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
HISTORY_LOG_PATH = Path("history.md")

RECENT_MESSAGES = 10   # verbatim tail sent alongside the compressed log
HISTORY_LOG_ENTRIES = 30  # compressed entries injected as a system message
STORED_MESSAGES = 40   # cap on self.messages in RAM — older turns live in the log
_SENTENCE_END = re.compile(r'[.!?]+["\')\]]*\s')


class _SentenceSplitter:
	def __init__(self, emit) -> None:
		self._buf = ""
		self._emit = emit

	def feed(self, delta: str) -> None:
		self._buf += delta
		while True:
			match = _SENTENCE_END.search(self._buf)
			if match is None:
				return
			sentence = self._buf[: match.end()].strip()
			if len(sentence) < 12:  # too short — probably "Mr." etc., keep accumulating
				return
			self._buf = self._buf[match.end() :]
			self._emit(sentence)

	def flush(self) -> None:
		sentence = self._buf.strip()
		self._buf = ""
		if sentence:
			self._emit(sentence)


class _SpeechPipeline:
	"""Synthesizes reply sentences on parallel worker threads; playback drains .audio in order.

	Each queued item carries its sequence number and the generation at enqueue time
	so output from a round that ended in tool calls can be dropped even if synthesis
	already finished.
	"""

	_WORKERS = 3  # concurrent TTS calls — sentence playback is faster than one HTTP round-trip

	def __init__(self, tts, emit=None) -> None:
		self._tts = tts
		self._emit = emit or (lambda kind, text: None)
		self._sentences: queue.Queue = queue.Queue()
		self.audio: queue.Queue = queue.Queue()
		self._generation = 0
		self._next_seq = 0
		self._lock = threading.Lock()
		self._threads = [threading.Thread(target=self._run, daemon=True) for _ in range(self._WORKERS)]

	def start(self) -> None:
		for thread in self._threads:
			thread.start()

	def say(self, text: str) -> None:
		text = _strip_markdown(text)
		if text:
			with self._lock:
				self._sentences.put((self._next_seq, self._generation, text))
				self._next_seq += 1

	def discard_pending(self) -> None:
		with self._lock:
			self._generation += 1
			self._next_seq = 0  # sequence restarts per generation so playback can't stall
		for pending in (self._sentences, self.audio):
			while True:
				try:
					pending.get_nowait()
				except queue.Empty:
					break

	def finish(self) -> None:
		for _ in self._threads:
			self._sentences.put((-1, self._generation, None))

	def abort(self) -> None:
		self.discard_pending()
		self.finish()

	def _run(self) -> None:
		try:
			while True:
				seq, gen, sentence = self._sentences.get()
				if sentence is None:
					return
				logger.info("TTS segment %d: %r", seq, sentence)
				self._emit("tool", f"tts: {sentence}")
				try:
					payload = self._tts.synthesize(sentence)
				except Exception as exc:
					payload = exc
				self.audio.put((seq, gen, payload))
		finally:
			self.audio.put(None)


class Assistant:
	def __init__(self, settings: Settings) -> None:
		self.settings = settings
		self.speaches = SpeachesClient(
			settings.speaches_base_url,
			settings.stt_model,
			settings.tts_model,
			settings.tts_voice,
		)
		self.stt = self.speaches
		if settings.stt_provider == "openai" or settings.tts_provider == "openai":
			from powerconf.clients import OpenAIAudioClient

			openai_audio = OpenAIAudioClient(
				settings.openai_base_url,
				settings.openai_api_key,
				settings.stt_model,
				settings.tts_model,
				settings.tts_voice,
			)
			if settings.stt_provider == "openai":
				self.stt = openai_audio
			if settings.tts_provider == "openai":
				self.tts = openai_audio
		if settings.tts_provider == "fish":
			from powerconf.clients import FishAudioClient

			self.tts = FishAudioClient(
				settings.fish_audio_base_url,
				settings.fish_audio_api_key,
				settings.fish_audio_model,
				settings.fish_audio_voice,
			)
		elif not hasattr(self, "tts"):
			self.tts = self.speaches
		if settings.chat_provider == "openai":
			from powerconf.clients import OpenAIClient

			self.ollama = OpenAIClient(
				settings.openai_base_url,
				settings.openai_api_key,
				settings.openai_model,
			)
		else:
			self.ollama = OllamaClient(settings.ollama_base_url, settings.ollama_model)
		self.messages: list[dict[str, str]] = [{"role": "system", "content": settings.system_prompt}]
		self._log = self._load_log()
		self._turn_tools: list[str] = []
		from powerconf.tools import register_history_ops

		register_history_ops(self._clear_history, self._forget_last)
		self.actions: queue.Queue[str] = queue.Queue()
		self._speaking = threading.Event()
		self._interrupted = False
		self._followup_until = 0.0
		self.ui = None
		if settings.web_ui:
			from powerconf.webui import WebUI

			self.ui = WebUI(Path("web/dist"), settings.web_ui_port, on_action=self.actions.put)
			self.ui.start()

	def run(self) -> None:
		from powerconf.audio import find_device, find_source_name

		self._wait_for_speaker()
		if self.settings.startup_volume:
			self._run_command(self.settings.volume_command.format(op=self.settings.startup_volume))
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
		self._emit("status", "listening")
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
						self._emit("status", "wake word — listening")
						awake_until = time.monotonic() + 6.0
						self._wake_cooldown = time.monotonic() + self.settings.wakeword_cooldown_s
						detector.reset()
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

	def _emit(self, kind: str, text: str) -> None:
		if self.ui is not None:
			self.ui.emit(kind, text)

	def _wait_for_speaker(self) -> None:
		while True:
			has_sink, has_source = bluetooth_audio_ready()
			has_buttons = find_button_device(
				self.settings.button_device_name, self.settings.button_fallback
			)
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
		device = find_button_device(self.settings.button_device_name, self.settings.button_fallback)
		if device is not None:
			device.close()
		else:
			logger.warning(
				"No button input device found matching %r yet; will keep rescanning",
				self.settings.button_device_name,
			)
		listener = ButtonListener(
			self.settings.button_device_name, self.actions, self.settings.button_fallback
		)
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
		self._interrupted = False
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
			text = self.stt.transcribe(input_wav)
		except Exception:
			logger.exception("Speech-to-text failed")
			return
		if not text:
			logger.info("Empty transcript — check the mic isn't muted on the device")
			return
		logger.info("You: %s", text)
		self._emit("you", text)
		pipeline = _SpeechPipeline(self.tts, self._emit)
		pipeline.start()
		if self.settings.echo_mode:
			reply = text
			pipeline.say(text)
		else:
			self.messages.append({"role": "user", "content": text})
			try:
				reply = self._chat(pipeline)
			except Exception:
				pipeline.abort()
				logger.exception("Chat failed")
				self._emit("error", "chat failed — see terminal log")
				return
		pipeline.finish()
		reply = _unwrap_json_reply(reply)
		logger.info("Assistant: %s", reply)
		self._emit("assistant", reply)
		self._log_turn(text, reply)
		if not reply.strip():
			logger.warning("Empty reply from model — nothing to speak")
			return
		if self._cancel_pending("interrupt"):
			logger.info("Response cancelled")
			return
		try:
			save_prefix = f"out_{timestamp}" if self.settings.save_audio else None
			self._play_speech(pipeline, save_prefix)
		except Exception:
			logger.exception("Playback failed")

	def _play_speech(self, pipeline: _SpeechPipeline, save_prefix: str | None) -> None:
		self._mic.clear()  # drop audio queued during the STT/Ollama/TTS gap
		self._vad.reset()
		self._followup_until = 0.0
		self._interrupted = False
		self._speaking.set()
		self._emit("status", "speaking")
		watcher = threading.Thread(target=self._watch_barge_in, args=(self._mic,), daemon=True)
		if self.settings.barge_in:
			watcher.start()
		index = 0
		expected = 0
		pending: dict[int, object] = {}
		workers_done = 0
		try:
			while workers_done < len(pipeline._threads):
				item = pipeline.audio.get()
				if item is None:
					workers_done += 1
					continue
				seq, gen, payload = item
				if gen != pipeline._generation:
					continue
				pending[seq] = payload
				while expected in pending and not self._interrupted:
					segment = pending.pop(expected)
					expected += 1
					if isinstance(segment, Exception):
						raise segment
					if not segment.startswith(b"RIFF"):
						logger.error("TTS returned non-WAV data (%d bytes) — skipping segment", len(segment))
						continue
					if save_prefix:
						index += 1
						self._save(f"{save_prefix}_{index}.wav", segment)
					play_wav(segment, device=self._out_device, should_stop=self._playback_interrupted)
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
			self._emit("status", "listening")
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
						self._emit("interrupt", "voice barge-in — interrupted")
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
			self._wake_cooldown = time.monotonic() + self.settings.wakeword_cooldown_s

	def _load_log(self) -> list[str]:
		try:
			lines = HISTORY_LOG_PATH.read_text(encoding="utf-8").splitlines()
		except OSError:
			return []
		if len(lines) > HISTORY_LOG_ENTRIES:
			lines = lines[-HISTORY_LOG_ENTRIES:]
			try:
				HISTORY_LOG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
			except OSError:
				pass
		logger.info("Loaded %d history log entries from %s", len(lines), HISTORY_LOG_PATH)
		return lines

	def _log_turn(self, user_text: str, reply: str) -> None:
		entry = f"- [{time.strftime('%H:%M')}] user: {_squash(user_text)!r}"
		if self._turn_tools:
			entry += f" | tools: {', '.join(self._turn_tools)}"
		entry += f" | reply: {_squash(reply)!r}"
		self._log.append(entry)
		try:
			with HISTORY_LOG_PATH.open("a", encoding="utf-8") as f:
				f.write(entry + "\n")
		except OSError:
			logger.warning("Could not write %s", HISTORY_LOG_PATH, exc_info=True)

	def _write_log(self) -> None:
		try:
			HISTORY_LOG_PATH.write_text(
				"".join(line + "\n" for line in self._log), encoding="utf-8"
			)
		except OSError:
			logger.warning("Could not write %s", HISTORY_LOG_PATH, exc_info=True)

	def _clear_history(self) -> str:
		self._log.clear()
		self.messages = self.messages[:1]
		self._write_log()
		logger.info("History cleared on request")
		return "Conversation history cleared."

	def _forget_last(self) -> str:
		# the current turn's user message is last in self.messages and its log
		# entry isn't written yet — "that" means the exchange before it
		forgot = False
		if self._log:
			self._log.pop()
			self._write_log()
			forgot = True
		user_idx = [i for i, m in enumerate(self.messages) if m["role"] == "user"]
		if len(user_idx) >= 2:
			del self.messages[user_idx[-2] : user_idx[-1]]
			forgot = True
		if not forgot:
			return "There's nothing earlier to forget."
		logger.info("Forgot last exchange on request")
		return "Forgot the last exchange."

	def _request_messages(self) -> list[dict]:
		request = [self.messages[0]]
		tail = self.messages[1:][-RECENT_MESSAGES:]
		while tail and tail[0]["role"] == "tool":
			tail = tail[1:]  # orphaned tool results break OpenAI's pairing rule
		# skip log entries already covered verbatim in the tail
		overlap = sum(1 for m in tail if m["role"] == "user")
		older = self._log[: len(self._log) - overlap] if overlap else self._log
		if older:
			entries = "\n".join(older[-HISTORY_LOG_ENTRIES:])
			request.append({"role": "system", "content": "Earlier in this session (oldest first):\n" + entries})
		return request + tail

	def _chat(self, speech: _SpeechPipeline) -> str:
		from powerconf.tools import all_tools, run_tool

		self._turn_tools = []
		if len(self.messages) > STORED_MESSAGES:
			self.messages = [self.messages[0]] + self.messages[-STORED_MESSAGES:]
		tools = all_tools()
		for _ in range(4):
			request = self._request_messages()
			splitter = _SentenceSplitter(speech.say) if self.settings.streaming else None
			chat_stream = getattr(self.ollama, "chat_stream", None) if splitter else None
			if chat_stream is not None:
				message = chat_stream(request, tools, splitter.feed)
				splitter.flush()
			else:
				message = self.ollama.chat(request, tools)
				if splitter is not None:
					splitter.feed(message.get("content") or "")
					splitter.flush()
				else:
					speech.say(message.get("content") or "")
			self.messages.append(message)
			tool_calls = message.get("tool_calls") or []
			if not tool_calls:
				return (message.get("content") or "").strip()
			speech.discard_pending()  # narration from a tool-calling round isn't spoken
			for call in tool_calls:
				function = call["function"]
				self._turn_tools.append(function["name"])
				arguments = function.get("arguments") or {}
				if isinstance(arguments, str):  # OpenAI sends a JSON string, Ollama a dict
					import json

					try:
						arguments = json.loads(arguments)
					except json.JSONDecodeError:
						arguments = {}
				logger.info("Tool call: %s(%s)", function["name"], arguments)
				args = ", ".join(f"{k}={v!r}" for k, v in arguments.items())
				self._emit("tool", f"{function['name']}({args})")
				try:
					result = run_tool(function["name"], arguments)
				except Exception:
					logger.exception("Tool %s failed", function["name"])
					result = f"{function['name']} failed"
				tool_msg = {"role": "tool", "content": str(result)}
				if call.get("id"):  # OpenAI requires matching tool_call_id
					tool_msg["tool_call_id"] = call["id"]
				self.messages.append(tool_msg)
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
			self._interrupted = True
			logger.info("Playback interrupted")
			self._emit("status", "interrupted")
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


def _squash(text: str, limit: int = 100) -> str:
	return " ".join(text.split())[:limit]


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
