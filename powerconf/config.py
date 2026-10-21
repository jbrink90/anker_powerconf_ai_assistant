import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
	speaches_base_url: str
	ollama_base_url: str
	chat_provider: str
	stt_provider: str
	openai_base_url: str
	openai_api_key: str
	openai_model: str
	stt_model: str
	tts_model: str
	tts_voice: str
	tts_provider: str
	fish_audio_base_url: str
	fish_audio_api_key: str
	fish_audio_model: str
	fish_audio_voice: str
	ollama_model: str
	echo_mode: bool
	save_audio: bool
	noise_reduce: bool
	double_tap_wav: str
	system_prompt: str
	audio_input_device: str
	audio_output_device: str
	sample_rate: int
	button_device_name: str
	volume_command: str
	mute_command: str
	vad_speech_ms: int
	vad_silence_ms: int
	vad_max_utterance_s: float
	vad_threshold_factor: float
	vad_min_rms: float
	wakeword_model: str
	wakeword_threshold: float
	barge_in: bool
	barge_in_min_rms: float
	barge_in_factor: float
	followup_s: float
	mcp_servers: str
	startup_volume: str


def load_settings() -> Settings:
	load_dotenv()
	return Settings(
		speaches_base_url=os.getenv("SPEACHES_BASE_URL", "http://localhost:8000").rstrip("/"),
		ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/"),
		chat_provider=os.getenv("CHAT_PROVIDER", "ollama"),
		stt_provider=os.getenv("STT_PROVIDER", "speaches"),
		openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
		openai_api_key=os.getenv("OPENAI_API_KEY", ""),
		openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
		stt_model=os.getenv("STT_MODEL", "whisper-1"),
		tts_model=os.getenv("TTS_MODEL", "tts-1"),
		tts_voice=os.getenv("TTS_VOICE", "af_heart"),
		tts_provider=os.getenv("TTS_PROVIDER", "speaches"),
		fish_audio_base_url=os.getenv("FISH_AUDIO_BASE_URL", "https://api.fish.audio").rstrip("/"),
		fish_audio_api_key=os.getenv("FISH_AUDIO_API_KEY", ""),
		fish_audio_model=os.getenv("FISH_AUDIO_MODEL", "s2"),
		fish_audio_voice=os.getenv("FISH_AUDIO_VOICE", ""),
		ollama_model=os.getenv("OLLAMA_MODEL", "llama3.2"),
		echo_mode=os.getenv("ECHO_MODE", "0") in ("1", "true", "yes"),
		save_audio=os.getenv("SAVE_AUDIO", "0") in ("1", "true", "yes"),
		noise_reduce=os.getenv("NOISE_REDUCE", "0") in ("1", "true", "yes"),
		double_tap_wav=os.getenv("DOUBLE_TAP_WAV", "not_found.wav"),
		system_prompt=os.getenv(
			"SYSTEM_PROMPT",
			"You are Jarvis, a voice assistant. Reply in one or two short spoken sentences — "
			"plain text only, no lists or markdown. You have tools: web_search for current news "
			"and facts you're unsure of; fetch to read a URL the user gives you; wikipedia for "
			"general knowledge and \"who/what is\" questions; weather for weather or forecasts; "
			"time for the time or date; memory to remember things the user asks you to remember "
			"and to recall them later; shell for basic Pi info — use commands like "
			"[\"vcgencmd\",\"measure_temp\"] for temperature, [\"uptime\"], [\"df\",\"-h\"], or [\"free\",\"-h\"]; "
			"space; and filesystem for files under /home/jordan. Rules: never search or call "
			"tools for opinions, jokes, or small talk — answer directly. When a tool returns "
			"information, summarize it in your own words; never say you can't access something "
			"or tell the user to check a website. If a tool fails, say so briefly and move on.",
		),
		audio_input_device=os.getenv("AUDIO_INPUT_DEVICE", ""),
		audio_output_device=os.getenv("AUDIO_OUTPUT_DEVICE", ""),
		sample_rate=int(os.getenv("SAMPLE_RATE", "16000")),
		button_device_name=os.getenv("BUTTON_DEVICE_NAME", "AVRCP"),
		volume_command=os.getenv("VOLUME_COMMAND", "pactl set-sink-volume @DEFAULT_SINK@ {op}"),
		mute_command=os.getenv("MUTE_COMMAND", "pactl set-sink-mute @DEFAULT_SINK@ toggle"),
		vad_speech_ms=int(os.getenv("VAD_SPEECH_MS", "250")),
		vad_silence_ms=int(os.getenv("VAD_SILENCE_MS", "900")),
		vad_max_utterance_s=float(os.getenv("VAD_MAX_UTTERANCE_S", "30")),
		vad_threshold_factor=float(os.getenv("VAD_THRESHOLD_FACTOR", "3.0")),
		vad_min_rms=float(os.getenv("VAD_MIN_RMS", "300")),
		wakeword_model=os.getenv("WAKEWORD_MODEL", ""),
		wakeword_threshold=float(os.getenv("WAKEWORD_THRESHOLD", "0.5")),
		barge_in=os.getenv("BARGE_IN", "1") in ("1", "true", "yes"),
		barge_in_min_rms=float(os.getenv("BARGE_IN_MIN_RMS", "300")),
		barge_in_factor=float(os.getenv("BARGE_IN_FACTOR", "1.0")),
		followup_s=float(os.getenv("FOLLOWUP_S", "10")),
		mcp_servers=os.getenv("MCP_SERVERS", ""),
		startup_volume=os.getenv("STARTUP_VOLUME", ""),
	)
