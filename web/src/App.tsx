import { useEffect, useRef, useState } from "react";

interface AssistantEvent {
	type: "you" | "assistant" | "tool" | "status" | "interrupt" | "error" | string;
	text: string;
	ts: string;
}

export default function App() {
	const [events, setEvents] = useState<AssistantEvent[]>([]);
	const [connected, setConnected] = useState(false);
	const [status, setStatus] = useState("connecting…");
	const feedRef = useRef<HTMLDivElement>(null);

	useEffect(() => {
		const source = new EventSource("/events");
		source.onopen = () => setConnected(true);
		source.onerror = () => setConnected(false);
		source.onmessage = (message) => {
			const ev = JSON.parse(message.data) as AssistantEvent;
			if (ev.type === "status") {
				setStatus(ev.text);
			}
			setEvents((prev) => [...prev.slice(-199), ev]);
		};
		return () => source.close();
	}, []);

	useEffect(() => {
		feedRef.current?.scrollTo({ top: feedRef.current.scrollHeight });
	}, [events]);

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
				<h1>Jarvis</h1>
				<div className="pills">
					<button className="pill btn" onClick={interrupt}>
						Interrupt
					</button>
					<span className="pill status">{status}</span>
					<span className={`pill conn ${connected ? "on" : "off"}`}>
						{connected ? "connected" : "disconnected"}
					</span>
				</div>
			</header>
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
		</div>
	);
}
