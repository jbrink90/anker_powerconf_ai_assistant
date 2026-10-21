import json
import logging
from collections.abc import Callable

import httpx

logger = logging.getLogger(__name__)


class SpeachesClient:
	def __init__(self, base_url: str, stt_model: str, tts_model: str, tts_voice: str) -> None:
		self._client = httpx.Client(base_url=base_url, timeout=httpx.Timeout(60.0))
		self.stt_model = stt_model
		self.tts_model = tts_model
		self.tts_voice = tts_voice

	def transcribe(self, wav_bytes: bytes) -> str:
		response = self._client.post(
			"/v1/audio/transcriptions",
			data={"model": self.stt_model, "response_format": "json"},
			files={"file": ("audio.wav", wav_bytes, "audio/wav")},
		)
		response.raise_for_status()
		return response.json()["text"].strip()

	def synthesize(self, text: str) -> bytes:
		response = self._client.post(
			"/v1/audio/speech",
			json={
				"model": self.tts_model,
				"input": text,
				"voice": self.tts_voice,
				"response_format": "wav",
			},
		)
		response.raise_for_status()
		return response.content


class OpenAIAudioClient:
	"""STT + TTS through OpenAI-compatible /v1/audio endpoints."""
	def __init__(self, base_url: str, api_key: str, stt_model: str, tts_model: str, tts_voice: str) -> None:
		headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
		self._client = httpx.Client(base_url=base_url, timeout=httpx.Timeout(60.0), headers=headers)
		self.stt_model = stt_model
		self.tts_model = tts_model
		self.tts_voice = tts_voice

	def transcribe(self, wav_bytes: bytes) -> str:
		response = self._client.post(
			"/audio/transcriptions",
			data={"model": self.stt_model, "response_format": "json"},
			files={"file": ("audio.wav", wav_bytes, "audio/wav")},
		)
		response.raise_for_status()
		return response.json()["text"].strip()

	def synthesize(self, text: str) -> bytes:
		response = self._client.post(
			"/audio/speech",
			json={"model": self.tts_model, "input": text, "voice": self.tts_voice, "response_format": "wav"},
		)
		response.raise_for_status()
		return response.content


class FishAudioClient:
	def __init__(self, base_url: str, api_key: str, model: str, voice: str) -> None:
		self._client = httpx.Client(
			base_url=base_url,
			timeout=httpx.Timeout(60.0),
			headers={"Authorization": f"Bearer {api_key}", "model": model} if api_key else {"model": model},
		)
		self.voice = voice

	def synthesize(self, text: str) -> bytes:
		body: dict = {"text": text, "format": "wav"}
		if self.voice:
			body["reference_id"] = self.voice
		response = self._client.post("/v1/tts", json=body)
		response.raise_for_status()
		return response.content


class OllamaClient:
	def __init__(self, base_url: str, model: str) -> None:
		self._client = httpx.Client(base_url=base_url, timeout=httpx.Timeout(120.0))
		self.model = model

	def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
		payload: dict = {"model": self.model, "messages": messages, "stream": False}
		if tools:
			payload["tools"] = tools
		response = self._client.post("/api/chat", json=payload)
		response.raise_for_status()
		return response.json()["message"]

	def chat_stream(
		self,
		messages: list[dict],
		tools: list[dict] | None = None,
		on_delta: Callable[[str], None] | None = None,
	) -> dict:
		payload: dict = {"model": self.model, "messages": messages, "stream": True}
		if tools:
			payload["tools"] = tools
		content: list[str] = []
		tool_calls: list[dict] = []
		with self._client.stream("POST", "/api/chat", json=payload) as response:
			response.raise_for_status()
			for line in response.iter_lines():
				if not line:
					continue
				data = json.loads(line)
				message = data.get("message") or {}
				delta = message.get("content")
				if delta:
					content.append(delta)
					if on_delta:
						on_delta(delta)
				tool_calls.extend(message.get("tool_calls") or [])
				if data.get("done"):
					break
		result: dict = {"role": "assistant", "content": "".join(content)}
		if tool_calls:
			result["tool_calls"] = tool_calls
		return result


class OpenAIClient:
	"""OpenAI-compatible /v1/chat/completions — works with OpenAI, Open WebUI, LM Studio, etc."""
	def __init__(self, base_url: str, api_key: str, model: str) -> None:
		headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
		self._client = httpx.Client(base_url=base_url, timeout=httpx.Timeout(120.0), headers=headers)
		self.model = model

	def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
		payload: dict = {"model": self.model, "messages": messages}
		if tools:
			payload["tools"] = tools
		response = self._client.post("/chat/completions", json=payload)
		response.raise_for_status()
		return response.json()["choices"][0]["message"]
