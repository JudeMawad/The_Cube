# Cube architecture

Cube runs as three separate services over HTTP. The Pi records speech and controls audio and hardware. The backend handles commands, integrations and persistent state. The optional AI node runs speech recognition, language-model interpretation and speech synthesis. Each service has its own environment and assets and does not import another service's code. See [deployment layout](DEPLOYMENT_LAYOUT.md) for file locations and [project status](PROJECT_STATUS.md) for testing limits.

## Raspberry Pi client

`client/app/cube.py:main` continuously reads one `arecord` microphone stream: 16 kHz mono signed 16-bit PCM in 80 ms frames. Every frame reaches the user-supplied ONNX openWakeWord model (VAD 0.30, `hey_cube` threshold 0.50), including during capture, processing, and speech. `client/app/interaction.py` manages an incremental RMS recorder and assigns each interaction a generation ID. A wake replaces the current interaction immediately; a three-below-threshold-frame latch prevents repeated detections of one wake. Four-frame pre-roll retains continuous-command onset. Wake-marked uploads allow the backend to strip a leading wake prefix before existing command routing.

`client/app/voice_runtime.py` runs a background asyncio I/O loop for `/voice`, notifications, progress cues, and speech. `client/app/audio/async_speech.py` preserves remote Kokoro then local Piper fallback, using the existing speech settings and `pw-play`. Cancellation closes owned HTTP exchanges and terminates/reaps only owned audio subprocesses. Completion events are generation-checked by the frame coordinator before changing LEDs or follow-up state. Power commands require completed acknowledgment playback; hardware operations run outside the capture loop. Local speaker controls retain their independent Bluetooth/PipeWire behavior; a wake does not mute a sink or stop unrelated media.

Backend and AI-node inference routes monitor HTTP disconnects after reading the request body, using separate cancellation helpers in each service. `X-Cube-Interaction-ID` propagates as a header without entering the LLM prompt. Native workers hold resources until cleanup is safe; cancellation checkpoints skip queued inference, stop Whisper segment consumption, prevent fallback, and suppress stale results/history. Commands already sent to external devices finish recording their results. See [voice barge-in](VOICE_BARGE_IN.md) for implementation limits, tests, deployment, and required AEC/LLM physical verification.

`client/app/notifications/worker.py:Notifications` long-polls backend `/events/next`, then validates and acknowledges an event around main-loop playback. `client/app/control/worker.py:Controls` independently registers/polls an ephemeral backend control session, validates typed commands, and calls `client/app/control/hardware.py:Hardware`. That class sends renderer commands over a local abstract Unix datagram socket, delegates volume changes to the existing Pi speaker controller, displays successful live volume readback through the native content transition, and uses fixed `busctl`/`systemctl` operations for shutdown and reboot. Both workers use the backend base URL derived from `CUBE_VOICE_ENDPOINT` and read the event token from the runtime user's `~/.config/cube/events.token`. `/voice` receives `X-Cube-Client-ID` and, when available, authenticated control session/request headers.

Speech and cues use `cube.assistant`; assistant volume changes only that sink. Soloist plays music through `cube.spotify` with a separate ducking gain. Both outputs follow the selected physical speaker. The audio graph, gain worker and receiver observer run independently of voice.

The backend handles Spotify authorization, Web API calls and music state through `MusicController`. Authenticated `/music/*` routes accept expiring receiver status from the Pi. The backend recognizes supported music commands before AI interpretation and uses the same controller for all music tools. Music tools are not advertised to AI; recognized unsupported requests get a backend clarification. Ambiguous bare names can still reach normal conversation. Only explicit `play track` requests select the top search result; qualified tracks, artists and playlists require exact resolution. Trusted client identity and request IDs prevent duplicate execution. Spotify data stays out of AI context and history. See [Spotify](SPOTIFY.md) for grammar, authentication, audio timing, recovery and setup.

## Backend and HTTP routes

`server/server.py` creates the FastAPI application and composes `server/core/`, feature packages, and integrations. Its installed command uses one Uvicorn worker on port 8765. Main routes:

| Route | Implementation | Purpose |
| --- | --- | --- |
| `GET /health`, `POST /transcribe` | `server/server.py` | Basic status; raw local CPU Whisper transcription for comparison. |
| `POST /voice`, `POST /tts` | `server/server.py` | Main recorded-voice path; WAV speech synthesis for the Pi. |
| `POST /webhooks/radarr`, `POST /webhooks/sonarr` | `server/features/media/event_routes.py` | Authenticated Arr media events. |
| `GET /events/next`, `POST /events/{event_id}/validate`, `POST /events/{event_id}/ack` | `server/features/media/event_routes.py` | Authenticated notification delivery and acknowledgement. |
| `POST /cube/control/session`, `GET /cube/control/next`, `POST /cube/control/{command_id}/result` | `server/features/cube/routes.py` | Authenticated Pi control session, long poll, and result. |

`server/core/http_auth.py:authenticate` checks the event token for webhooks, notification, and control routes. `server/core/client_identity.py:client_identity` identifies clients. The backend does not expose a general hardware command submission endpoint; `server/features/cube/channel.py:ControlChannel` holds short-lived in-memory control requests. `server/features/media/media_events.py:EventStore` persists media requests and notification state in SQLite at `~/.local/state/cube/media-events.sqlite3`. `server/features/media/movie_commands.py:MovieCommands` handles movie requests, clarification, status, and follow-up; `movie_cancellation.py` handles guarded cancellation. `server/features/lights/`, `server/features/plugs/`, and `server/integrations/` connect Govee, Tuya/Smart Life, Overseerr, Radarr, and Sonarr.

## STT, assistant, tools, and TTS

`server/server.py:voice` saves the upload temporarily, calls `server/core/transcription.py:select_transcription`, removes the upload, then calls `server/core/ai_turn.py:process_turn`. With a valid `CUBE_AI_NODE_URL`, transcription first calls AI-node `/transcribe` through `server/core/ai_client.py:AIClient`; transport, timeout, status, or schema failures fall back once to `server/speech/stt.py:transcribe_audio`. Without that URL, local faster-whisper is used. The local model is loaded on backend import with `base.en`, CPU/int8, beam size 5, and VAD. The backend raw `/transcribe` always uses the local model.

For a complete catalog volume phrase, `process_turn` validates and executes one Cube tool intent through the authenticated Pi control channel before any LLM interpretation. The Pi reads the currently selected sink and returns its live channel levels and mute state. A short-lived direction in `ClientStateManager` resolves “a little more” only after a successful relative adjustment.

For other nonempty transcripts, `process_turn` sends bounded client history and executable tool definitions to AI-node `/process` when configured. `server/core/client_state.py:ClientStateManager` serializes turns per client and stores bounded in-memory history. The node's `ai_node/process.py:Processor` sends a single non-streaming request to an OpenAI-compatible `/chat/completions` endpoint and validates one strict JSON conversation answer or tool intent; it does not execute tools.

The backend validates the tool name and arguments in `server/core/tool_registry.py:ToolRegistry`, executes one synchronous handler through `server/core/tool_execution.py:execute_tool`, and preserves the backend result fields. Tools are registered from `server/features/{media,lights,plugs,cube}/tools.py`; feature state changes stay on the backend or are relayed to the Pi for Cube hardware. The current backend path uses the backend tool reply after execution. The AI node also implements a constrained `respond` phase, but `process_turn` does not currently call it.

If interpretation is unavailable, `server/core/voice_pipeline.py:process_transcription` uses the existing media-first, then light-command path. The server and AI node keep separate wire models in `server/core/schemas.py` and `ai_node/schemas.py`.

`server/server.py:synthesize_speech` asynchronously orchestrates TTS and caches up to 64 successful WAV responses. When AI-node URL is configured, it tries node `/tts`; on failure it uses local `server/tts/kokoro_tts.py:KokoroTTS`, with voice `bm_lewis`, speed 1.3, language `en-gb`.

`ai_node/app.py:create_app` exposes `GET /health`, `POST /transcribe`, `POST /process`, and `POST /tts` on the separate port 8766. `ai_node/stt.py:SpeechToText` loads faster-whisper in a background startup task and serializes inference; its defaults are `base.en`, CUDA, and float16. `ai_node/tts.py:SpeechSynthesizer` loads Kokoro independently. AI-node health reports STT model load readiness and whether LLM settings are configured; it does not prove first inference or upstream availability.

## LED matrix

`client/native/cube-display/cube-display.cc:main` drives the Pi 5 HUB75 64×64 panel through the pinned `client/third_party/rpi-rgb-led-matrix` submodule and an RP1 PIO patch applied by `prepare-matrix.py`. It runs a roughly 33 ms frame loop and swaps frames at VSync. `ReadControlMessages` receives `idle`, `wake`, `listening`, `thinking`, `followup`, `speech <level>`, and strict `control` datagrams on abstract socket `@cube-display`, accepting the user named by `CUBE_DISPLAY_USER` or root. `display-control.h:DisplayControl` owns display enable and the single direct master brightness.

On a 64×64 display, `display-frame.h` defines shared pixels and frames; `DisplayRuntime` returns an active app frame or black; a pure overlay marks current interaction state; and `ContentRenderer` uses a hardware-independent dissolve for explicit solid-white text/icons. `renderer-control.py` is a manual Pi-local socket helper. The Pi volume adapter shows live readback, refreshes it at playback, and clears it after two seconds. `Makefile` builds a candidate at `client/native/cube-display/build/cube-display`; the systemd unit points to the separately installed executable. See [display architecture](DISPLAY_ARCHITECTURE.md) and the [native guide](../client/native/cube-display/README.md).

## Configuration and startup

Each service reads its own environment settings; model/cache path overrides resolve relative to its service directory. Important names are:

| Service | Settings |
| --- | --- |
| Pi | `CUBE_CAPTURE_DEVICE`, `CUBE_VOICE_ENDPOINT`, `CUBE_TTS_ENDPOINT`, `CUBE_CLIENT_ID`, `CUBE_WAKE_MODEL_PATH`, `CUBE_PIPER_VOICE_PATH`, `CUBE_SPEAKER_MAC`; defaults and behavior in `client/app/cube.py`, `client/app/config/paths.py`, and `client/app/audio/`. |
| Backend | `CUBE_AI_NODE_URL`, `CUBE_AI_TRANSCRIPTION_TIMEOUT`, `CUBE_AI_PROCESS_TIMEOUT`, `CUBE_WHISPER_MODEL`, `CUBE_WHISPER_CACHE_DIR`, `CUBE_KOKORO_MODEL_PATH`, `CUBE_KOKORO_VOICES_PATH`; parsed in `server/core/config.py`. |
| AI node | `CUBE_AI_WHISPER_MODEL`, `CUBE_AI_WHISPER_DEVICE`, `CUBE_AI_WHISPER_COMPUTE_TYPE`, `CUBE_AI_WHISPER_CACHE_DIR`, `CUBE_AI_KOKORO_MODEL_PATH`, `CUBE_AI_KOKORO_VOICES_PATH`, `CUBE_AI_LLM_BASE_URL`, `CUBE_AI_LLM_MODEL`, `CUBE_AI_LLM_TIMEOUT`; parsed in `ai_node/config.py`. |
| Renderer | `CUBE_RENDERER_STATS=1` enables opt-in frame statistics in `cube-display.cc`. |

Credential file names are defined in `server/integrations/`: `~/.config/cube/govee_api_key`, `overseerr.env`, `radarr.env`, `sonarr.env`, and `tuya.env`; webhook/control credentials use `events.token`. Do not copy their contents into documentation. Model weights and caches are local assets; see [asset guidance](DEPLOYMENT_LAYOUT.md).

Pi unit templates `client/deploy/cube-display.service` and `client/deploy/cube-voice.service` start the renderer as root and voice client as the rendered service account; the voice unit orders itself after Cube Display, network, sound, and user service. `server/deploy/cube-server.service` starts `uvicorn server:app --host 0.0.0.0 --port 8765` from `server/`. These unit files are repository templates; installation and restart instructions are in [Pi deployment](../client/deploy/README.md) and [backend deployment](../server/deploy/README.md). The AI node uses `./ai_node/start.sh` from the repository root (or by absolute path), launching one Uvicorn worker on port 8766 after locating CUDA libraries. `ai_node/deploy/windows/` contains optional scheduled-task and WSL orchestration scripts; see its [README](../ai_node/deploy/windows/README.md).

For local checks, use the component commands in [tests/README.md](../tests/README.md), `server/scripts/compare_transcription.py` for a measured backend/node recording comparison, and the native guide's candidate build and manual panel procedure. A running service or passing mocked test does not confirm that hardware or live integrations work.
