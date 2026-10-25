# Anker PowerConf Voice Assistant

Turns an Anker PowerConf speakerphone into a voice assistant: an
always-listening VAD captures speech from the speakerphone's mic, audio goes to
[Speaches](https://github.com/speaches-ai/speaches) for STT, the transcript to
Ollama for a reply, and Speaches TTS audio plays back through the speaker.

Runs on a Raspberry Pi (or any Linux host) paired to the PowerConf over
Bluetooth. Speaches and Ollama run on a separate LAN host — the Pi only does
audio I/O and button handling.

```
VAD ─► mic capture ─► Speaches STT ─► Ollama chat ─► Speaches TTS ─► speaker
```

## Pi setup

```sh
sudo apt install python3-pip python3-venv libportaudio2
# evdev needs headers to build from source if no wheel is available:
sudo apt install python3-dev   # or: sudo apt install python3-evdev and skip the pip dep

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Add your user to the `input` group so `evdev` can read `/dev/input/*`:

```sh
sudo usermod -aG input $USER   # log out/in afterwards
```

## Pairing the PowerConf

```sh
bluetoothctl
> power on
> agent on
> scan on            # put the PowerConf in pairing mode (hold BT button)
> pair <MAC>
> trust <MAC>
> connect <MAC>
> quit
```

PipeWire/PulseAudio should pick it up as a speakerphone profile (HFP) once the
mic is opened. Verify with `pactl list sources short` / `pactl list sinks
short`. If it shows up as A2DP-only, set the headset profile in your audio
control (e.g. `pavucontrol` or `wpctl`).

## Configure

```sh
cp .env.example .env
```

Set `SPEACHES_BASE_URL` / `OLLAMA_BASE_URL` to your LAN host and pick models.

## Run

```sh
source .venv/bin/activate
python main.py
```

The assistant listens continuously; an energy VAD segments speech. The noise
floor is measured for 1 s at startup; tune `VAD_*` vars if needed.

- **tap play** — interrupts TTS playback (barge-in).
- **double-tap play** — plays the wav file named by `DOUBLE_TAP_WAV`
  (default `not_found.wav` in the project dir).
- Note: the PowerConf only exposes AVRCP events for the play button
  (tap = `KEY_PLAYCD`/`KEY_PAUSECD` alternating, double-tap = `KEY_NEXTSONG`).
  Volume and mute buttons are handled internally by the device and emit no
  events.
- Set `ECHO_MODE=1` to skip Ollama and speak the transcript back (loopback
  test). Set `SAVE_AUDIO=1` to write `recordings/in_*.wav` (mic capture) and
  `recordings/out_*.wav` (TTS output) — pull them off the Pi to judge fidelity
  on proper speakers.

### Discovering button key codes

Button mappings vary by firmware. To see exactly what each physical button
emits:

```sh
python main.py buttons
```

then update `KEY_ACTIONS` in `powerconf/buttons.py` if a button isn't mapped.

## Notes

- While the mic is open the device uses the HFP profile, so playback is call
  quality, not A2DP music quality — that's a Bluetooth limitation, not a bug.
- Conversation history: the last few exchanges are sent verbatim; older turns
  are compressed into one-line entries (user request, tools used, reply) in
  `history.md`, which persists across restarts and is injected as context.
  Delete `history.md` to start fresh.

## Web UI

A lightweight React app shows the live feed — transcripts, replies, tool
calls, status, errors — in a browser instead of the terminal.

Build it on a dev machine with Node installed, then copy the output to the Pi:

```sh
cd web
npm install
npm run build          # outputs web/dist/
scp -r dist jordan@<pi>:~/.../anker_powerconf_speaker/web/
```

Set `WEB_UI=1` and `WEB_UI_PORT=8765` in `.env`, start the assistant, then open
`http://<pi>:8765`. The page subscribes to `/events` (server-sent events);
clients that connect late get the last 100 events replayed. `npm run dev`
proxies `/events` to the Pi so you can iterate on the UI without redeploying.

## Custom tools

Tools are how the assistant acts on the world — the model picks one from the
advertised schemas, the tool runs, and its result goes back to the model and
gets spoken. Two extension paths:

### Built-in tools (`powerconf/tools.py`)

For anything that can run in-process. Three steps:

1. Write a function that takes the model's arguments and returns a short string
   — the result is spoken aloud, so keep it brief.
2. Add its JSON schema to `TOOLS` (name, description, `parameters`). The
   description is what the model reads to decide when to call it — say *when*,
   not just what.
3. Add a dispatch branch in `run_tool`.

`web_search` is the template. For tools that need assistant internals
(history, playback state, volume), the function can't live in `tools.py` —
instead write a method on `Assistant`, register it from `Assistant.__init__`
via a small callback registry like `register_history_ops`, and dispatch by
name the same way. `clear_history` / `forget_last` show that pattern.

Notes:

- Tools run synchronously in the chat loop — a slow tool stalls the reply.
  Wrap anything blocking >1 s in a timeout or defer it.
- Every schema in `TOOLS` + discovered MCP tools is sent to the model on every
  turn; each schema costs prompt tokens and latency, so keep them tight and
  trim tools you don't use.
- If a tool's result shouldn't be spoken verbatim, return a status string
  ("Light turned off") rather than raw data — the model paraphrases it.

### MCP servers (`.env`)

For anything that already exists as an MCP server, or needs isolation in its
own process — filesystem access, OAuth-backed services, Node/Python tools with
their own deps. Configure once in `MCP_SERVERS`; tools are discovered and
routed automatically at startup. Details below.

## MCP tool servers

The assistant can use [MCP](https://modelcontextprotocol.io) servers as plugins —
each server's tools are advertised to Ollama alongside the built-in
`web_search`, and tool calls are routed back to the server over stdio.

Configure in `.env` as a one-line JSON array:

```sh
MCP_SERVERS=[{"name":"filesystem","command":"npx","args":["-y","@modelcontextprotocol/server-filesystem","/home/jordan"]}]
```

- Requires `pip install mcp` on the Pi (in `requirements.txt`).
- Most community servers run via `npx` or `uvx` — install Node.js or uv on the
  Pi first. `apt install nodejs npm` works for npx-based servers.
- A failing server logs a warning and is skipped; the assistant still runs.
- For Gmail and other OAuth-backed servers, run the server's own auth setup
  once on the Pi (or copy its token file over) before enabling it here.
