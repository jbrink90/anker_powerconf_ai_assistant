import { useEffect, useRef, useState, type FormEvent } from "react";

interface AssistantEvent {
	type: "you" | "assistant" | "tool" | "status" | "interrupt" | "error" | string;
	text: string;
	ts: string;
}

interface RuntimeSettings {
	system_prompt: string;
	wakeword_threshold: number;
	wakeword_cooldown_s: number;
	followup_s: number;
	wake_ack_tone: boolean;
	utterance_ack_tone: boolean;
	barge_in: boolean;
	barge_in_min_rms: number;
	barge_in_factor: number;
	noise_reduce: boolean;
	save_audio: boolean;
	streaming: boolean;
	echo_mode: boolean;
	vad_speech_ms: number;
	vad_silence_ms: number;
	vad_max_utterance_s: number;
	vad_threshold_factor: number;
	vad_min_rms: number;
}

type NumericSetting = Exclude<keyof RuntimeSettings, "system_prompt" | "wake_ack_tone" | "utterance_ack_tone" | "barge_in" | "noise_reduce" | "save_audio" | "streaming" | "echo_mode">;
type BooleanSetting = "wake_ack_tone" | "utterance_ack_tone" | "barge_in" | "noise_reduce" | "save_audio" | "streaming" | "echo_mode";

const SETTING_HELP: Record<keyof RuntimeSettings, string> = {
	system_prompt: "Sets the assistant's personality, response style, and instructions for every new reply.",
	wakeword_threshold: "The minimum model score needed to accept a wakeword. Raise it to reduce false activations; lower it if the assistant often misses you.",
	wakeword_cooldown_s: "After a wakeword is detected, ignore further wakeword detections for this many seconds.",
	followup_s: "How long the assistant stays ready for another request after replying without requiring the wakeword again. Set to 0 to disable.",
	wake_ack_tone: "Play a short tone when the wakeword is detected so you know the assistant is listening.",
	utterance_ack_tone: "Play a short tone after a complete utterance is captured, to indicate that the assistant has received it and started processing.",
	barge_in: "Let voice activity interrupt the assistant while it is speaking.",
	barge_in_min_rms: "Minimum microphone loudness needed to interrupt speech. Lower values make interruption easier; higher values help reject noise.",
	barge_in_factor: "Multiplier on the room-calibrated VAD threshold for interruption. Higher values make interruption less sensitive.",
	noise_reduce: "Filter background noise from captured speech before sending it to speech recognition. This uses extra CPU and may alter the voice.",
	save_audio: "Save captured input and generated output audio clips in the recordings folder.",
	streaming: "Start speaking each completed sentence while the rest of the reply is still being generated.",
	echo_mode: "Skip the LLM and speak back the recognized transcript. Useful for checking speech capture and playback.",
	vad_speech_ms: "How much continuous sound above the VAD threshold starts an utterance. Longer values reject brief noises but can delay capture.",
	vad_silence_ms: "How long silence must last before the assistant considers your utterance complete. Longer values allow pauses but delay replies.",
	vad_max_utterance_s: "Maximum duration for one captured utterance before it is sent for transcription.",
	vad_threshold_factor: "Multiplier applied to the calibrated room noise level to decide what counts as speech. Higher values reject more background noise.",
	vad_min_rms: "Minimum microphone loudness treated as speech, even in a quiet room. Raise it to reject noise; lower it for a quieter voice.",
};

export default function App() {
	const [events, setEvents] = useState<AssistantEvent[]>([]);
	const [connected, setConnected] = useState(false);
	const [status, setStatus] = useState("connecting…");
	const [settingsOpen, setSettingsOpen] = useState(false);
	const [settings, setSettings] = useState<RuntimeSettings | null>(null);
	const [settingsMessage, setSettingsMessage] = useState("");
	const [savingSettings, setSavingSettings] = useState(false);
	const feedRef = useRef<HTMLDivElement>(null);

	useEffect(() => {
		const source = new EventSource("/events");
		source.onopen = () => setConnected(true);
		source.onerror = () => setConnected(false);
		source.onmessage = (message) => {
			const ev = JSON.parse(message.data) as AssistantEvent;
			if (ev.type === "status") {
				setStatus(ev.text);
				return;
			}
			setEvents((prev) => [...prev.slice(-199), ev]);
		};
		return () => source.close();
	}, []);

	useEffect(() => {
		feedRef.current?.scrollTo({ top: feedRef.current.scrollHeight });
	}, [events, settingsOpen]);

	useEffect(() => {
		if (!settingsOpen) return;
		setSettingsMessage("Loading settings…");
		fetch("/api/settings")
			.then(async (response) => {
				const body = await response.json();
				if (!response.ok) throw new Error(body.error ?? "Could not load settings.");
				setSettings(body.values as RuntimeSettings);
				setSettingsMessage("");
			})
			.catch((error: Error) => setSettingsMessage(error.message));
	}, [settingsOpen]);

	const changeNumber = (key: NumericSetting, value: string) => {
		setSettings((current) => current ? { ...current, [key]: Number(value) } : current);
	};

	const changeBoolean = (key: BooleanSetting, value: boolean) => {
		setSettings((current) => current ? { ...current, [key]: value } : current);
	};

	const saveSettings = async (event: FormEvent<HTMLFormElement>) => {
		event.preventDefault();
		if (!settings) return;
		setSavingSettings(true);
		setSettingsMessage("");
		try {
			const response = await fetch("/api/settings", {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ settings }),
			});
			const body = await response.json();
			if (!response.ok) throw new Error(body.error ?? "Could not save settings.");
			setSettings(body.values as RuntimeSettings);
			setSettingsMessage("Saved. Changes apply now or on the next relevant turn; no restart needed.");
		} catch (error) {
			setSettingsMessage(error instanceof Error ? error.message : "Could not save settings.");
		} finally {
			setSavingSettings(false);
		}
	};

	const interrupt = () => {
		fetch("/action", {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({ action: "interrupt" }),
		});
	};

	return (
		<div className="app">
			<header>
				<h1>Voice Assistant</h1>
				<div className="pills">
					<button className="pill btn" onClick={() => setSettingsOpen((open) => !open)}>
						{settingsOpen ? "Chat" : "Settings"}
					</button>
					<button className="pill btn" onClick={interrupt}>
						Interrupt
					</button>
					<span className="pill status">{status}</span>
					<span className={`pill conn ${connected ? "on" : "off"}`}>
						{connected ? "connected" : "disconnected"}
					</span>
				</div>
			</header>
			{settingsOpen ? (
				<section className="settings-view">
					{settings ? (
						<form className="settings-form" onSubmit={saveSettings}>
							<p className="settings-intro">Changes save to .env and apply without restarting, either now or on the next relevant turn. Provider, device, wakeword model, and MCP server changes still require a restart.</p>
							<label className="setting wide" data-tooltip={SETTING_HELP.system_prompt}>
								<span>Assistant personality and instructions</span>
								<textarea aria-description={SETTING_HELP.system_prompt} value={settings.system_prompt} rows={7} onChange={(event) => setSettings({ ...settings, system_prompt: event.target.value })} />
							</label>
							<div className="settings-grid">
								<label className="setting" data-tooltip={SETTING_HELP.wakeword_threshold}><span>Wakeword threshold</span><input aria-description={SETTING_HELP.wakeword_threshold} type="number" min="0" max="1" step="0.01" value={settings.wakeword_threshold} onChange={(event) => changeNumber("wakeword_threshold", event.target.value)} /></label>
								<label className="setting" data-tooltip={SETTING_HELP.wakeword_cooldown_s}>
									<span>Wakeword cooldown (seconds)</span>
									<input aria-description={SETTING_HELP.wakeword_cooldown_s} type="number" min="0" max="60" step="0.1" aria-describedby="wakeword-cooldown-help" value={settings.wakeword_cooldown_s} onChange={(event) => changeNumber("wakeword_cooldown_s", event.target.value)} />
									<small id="wakeword-cooldown-help">Repeat detections are ignored for this long after a wakeword. Default: 4 seconds.</small>
								</label>
								<label className="setting" data-tooltip={SETTING_HELP.followup_s}><span>Follow-up listening (seconds)</span><input aria-description={SETTING_HELP.followup_s} type="number" min="0" max="120" step="1" value={settings.followup_s} onChange={(event) => changeNumber("followup_s", event.target.value)} /></label>
								<label className="setting" data-tooltip={SETTING_HELP.barge_in_min_rms}><span>Barge-in minimum RMS</span><input aria-description={SETTING_HELP.barge_in_min_rms} type="number" min="0" max="10000" step="25" value={settings.barge_in_min_rms} onChange={(event) => changeNumber("barge_in_min_rms", event.target.value)} /></label>
								<label className="setting" data-tooltip={SETTING_HELP.barge_in_factor}><span>Barge-in sensitivity</span><input aria-description={SETTING_HELP.barge_in_factor} type="number" min="0.1" max="10" step="0.1" value={settings.barge_in_factor} onChange={(event) => changeNumber("barge_in_factor", event.target.value)} /></label>
								<label className="setting" data-tooltip={SETTING_HELP.vad_speech_ms}><span>VAD speech start (ms)</span><input aria-description={SETTING_HELP.vad_speech_ms} type="number" min="50" max="2000" step="50" value={settings.vad_speech_ms} onChange={(event) => changeNumber("vad_speech_ms", event.target.value)} /></label>
								<label className="setting" data-tooltip={SETTING_HELP.vad_silence_ms}><span>VAD silence end (ms)</span><input aria-description={SETTING_HELP.vad_silence_ms} type="number" min="100" max="5000" step="50" value={settings.vad_silence_ms} onChange={(event) => changeNumber("vad_silence_ms", event.target.value)} /></label>
								<label className="setting" data-tooltip={SETTING_HELP.vad_max_utterance_s}><span>Maximum utterance (seconds)</span><input aria-description={SETTING_HELP.vad_max_utterance_s} type="number" min="3" max="120" step="1" value={settings.vad_max_utterance_s} onChange={(event) => changeNumber("vad_max_utterance_s", event.target.value)} /></label>
								<label className="setting" data-tooltip={SETTING_HELP.vad_threshold_factor}><span>VAD threshold factor</span><input aria-description={SETTING_HELP.vad_threshold_factor} type="number" min="1" max="20" step="0.1" value={settings.vad_threshold_factor} onChange={(event) => changeNumber("vad_threshold_factor", event.target.value)} /></label>
								<label className="setting" data-tooltip={SETTING_HELP.vad_min_rms}><span>VAD minimum RMS</span><input aria-description={SETTING_HELP.vad_min_rms} type="number" min="0" max="10000" step="25" value={settings.vad_min_rms} onChange={(event) => changeNumber("vad_min_rms", event.target.value)} /></label>
							</div>
							<div className="settings-toggles">
								<label data-tooltip={SETTING_HELP.wake_ack_tone}><input aria-description={SETTING_HELP.wake_ack_tone} type="checkbox" checked={settings.wake_ack_tone} onChange={(event) => changeBoolean("wake_ack_tone", event.target.checked)} /> Wake acknowledgment tone</label>
								<label data-tooltip={SETTING_HELP.utterance_ack_tone}><input aria-description={SETTING_HELP.utterance_ack_tone} type="checkbox" checked={settings.utterance_ack_tone} onChange={(event) => changeBoolean("utterance_ack_tone", event.target.checked)} /> Utterance received tone</label>
								<label data-tooltip={SETTING_HELP.barge_in}><input aria-description={SETTING_HELP.barge_in} type="checkbox" checked={settings.barge_in} onChange={(event) => changeBoolean("barge_in", event.target.checked)} /> Allow interruption while speaking</label>
								<label data-tooltip={SETTING_HELP.noise_reduce}><input aria-description={SETTING_HELP.noise_reduce} type="checkbox" checked={settings.noise_reduce} onChange={(event) => changeBoolean("noise_reduce", event.target.checked)} /> Noise reduction</label>
								<label data-tooltip={SETTING_HELP.save_audio}><input aria-description={SETTING_HELP.save_audio} type="checkbox" checked={settings.save_audio} onChange={(event) => changeBoolean("save_audio", event.target.checked)} /> Save recordings</label>
								<label data-tooltip={SETTING_HELP.streaming}><input aria-description={SETTING_HELP.streaming} type="checkbox" checked={settings.streaming} onChange={(event) => changeBoolean("streaming", event.target.checked)} /> Stream replies to speech</label>
								<label data-tooltip={SETTING_HELP.echo_mode}><input aria-description={SETTING_HELP.echo_mode} type="checkbox" checked={settings.echo_mode} onChange={(event) => changeBoolean("echo_mode", event.target.checked)} /> Echo mode</label>
							</div>
							<div className="settings-actions">
								<button className="pill btn save" type="submit" disabled={savingSettings}>{savingSettings ? "Saving…" : "Save changes"}</button>
								<span aria-live="polite">{settingsMessage}</span>
							</div>
						</form>
					) : <p className="settings-message">{settingsMessage || "Loading settings…"}</p>}
				</section>
			) : (
				<div className="feed" ref={feedRef}>
					{events.map((ev, i) => (
						<div key={i} className={`row ${ev.type}`}>
							<span className="ts">{ev.ts}</span>
							<span className="text">{ev.text}</span>
						</div>
					))}
					{events.length === 0 && (
						<div className="row status">
							<span className="text">Waiting for activity…</span>
						</div>
					)}
				</div>
			)}
		</div>
	);
}
