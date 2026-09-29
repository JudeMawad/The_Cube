# Setup

Run commands from the repository root unless stated otherwise. Choose only the services you need. Use a trusted LAN or private encrypted network; do not forward the HTTP ports to the internet.

## Clone and test

```sh
git clone --recurse-submodules YOUR_PUBLIC_REPOSITORY_URL cube-public
cd cube-public
```

If already cloned, run `git submodule update --init --recursive`. The matrix dependency is pinned; do not update it as part of setup. A normal workstation can run the [CPU-only tests](../tests/README.md) and native matrix-facade tests without any private configuration or hardware.

## Backend

The recorded production environment uses Python 3.12. Install your OS's Python venv support, libsndfile and eSpeak NG packages. On Debian/Ubuntu these include `python3-venv libsndfile1 espeak-ng`.

```sh
python3.12 -m venv server/.venv
server/.venv/bin/python -m pip install -r server/requirements.txt
server/.venv/bin/python -m pip check
server/.venv/bin/python server/scripts/setup_kokoro.py
```

The setup helper downloads Kokoro model and voice data to `server/assets/models/kokoro/`. It skips existing nonempty files; this is not checksum verification. Review the upstream model sources/terms in [notices](../THIRD_PARTY_NOTICES.md). Local faster-whisper loads on backend import and may download `base.en` on first startup. An offline deployment must pre-provision its model/cache.

Copy `config/examples/server.env.example` to `~/.config/cube/server.env`, edit it and restrict access. Leave `CUBE_AI_NODE_URL` blank to use backend-local inference and deterministic integrations. For a manual launch, export the edited variables in your shell; a `.env` file is not loaded automatically by Python.

```sh
cd server
.venv/bin/uvicorn server:app --host 127.0.0.1 --port 8765 --workers 1
```

Bind to a trusted interface for Pi access. Use one worker: control sessions, conversation state and music command receipts live in that process. `/health` is a readiness aid, not proof of successful model inference. Missing Kokoro leaves TTS unavailable while other routes remain; failure to load local Whisper can prevent backend startup.

## Pi client

Use a 64-bit Raspberry Pi OS installation with Pi 5 support. Install Python venv support, ALSA tools (`arecord`), PipeWire/Pulse compatibility, WirePlumber, Bluetooth tools if needed, `g++`, `make`, and `patch`. The reference microphone is ReSpeaker; verify the actual capture device with `arecord -l` and configure `CUBE_CAPTURE_DEVICE`. Do not assume ALSA device numbering is stable.

```sh
python3 -m venv client/app/.venv
client/app/.venv/bin/python -m pip install -r client/requirements.txt
client/app/.venv/bin/python -m pip check
make -C client/native/cube-display -j2
```

The build produces a candidate only. See [Pi deployment](../client/deploy/README.md) before installing or starting it. Never run two renderers against the panel.

Wake models and speech weights are not included. Supply a compatible openWakeWord ONNX at `client/assets/models/hey_cube.onnx`, or set `CUBE_WAKE_MODEL_PATH`. Provision its supporting embedding/melspectrogram assets separately using upstream guidance. Read [wake provisioning](../client/assets/models/README.md) for prediction-name/threshold considerations; Cube does not retrain or calibrate a model automatically.

For local Piper fallback, obtain **both** `en_US-lessac-medium.onnx` and its matching `.onnx.json`, review the voice model card, and place them in `client/assets/voices/`, or configure `CUBE_PIPER_VOICE_PATH` with the matching JSON alongside the weights. See [voice provisioning](../client/assets/voices/README.md). All these files remain ignored. Missing wake weights prevent normal voice startup; missing Piper assets prevent fallback speech. The [hardware-free tests](../tests/README.md) mock model loading.

Copy `config/examples/client.env.example` to `~/.config/cube/client.env` and edit the backend endpoints, microphone and weather location. Replace `YOUR_LATITUDE`, `YOUR_LONGITUDE` and `YOUR_TIMEZONE` with your chosen forecast location; unedited placeholders leave weather unavailable. Source defaults are a documented public city reference, not private location configuration. Set `CUBE_CLIENT_ID` consistently on the Pi and backend Spotify configuration. The default hostname `server` is a development convention; a fresh clone does not supply DNS for it.

The audio graph is required even without Spotify: speech targets `cube.assistant`. Provision the user PipeWire session and `cube-audio` services before launching voice. Pair/select the physical speaker separately; review [Spotify and audio routing](SPOTIFY.md) and [Pi deployment](../client/deploy/README.md).

## Private configuration and optional integrations

Create `~/.config/cube` with mode 0700. Keep configuration/token files mode 0600 and owned by the service account. Never paste credentials into commands, issues, screenshots or Git. Example files contain placeholders and must be edited; copying an example does not provision an integration.

| Feature | Service/user location | Required configuration |
| --- | --- | --- |
| Govee | Backend `~/.config/cube/` | `govee_api_key` and `~/.config/cube/govee.json`; device/group mapping example is provided |
| Tuya / Smart Life | Backend `~/.config/cube/tuya.env` | Cloud region endpoint, Access ID/Secret, optional lamp/mirror/mushroom device IDs |
| Overseerr | Backend `~/.config/cube/overseerr.env` | `OVERSEERR_URL`, `OVERSEERR_API_KEY` |
| Radarr / Sonarr | Backend `~/.config/cube/radarr.env`, `sonarr.env` | Service URL/API key; needed for media verification and cancellation |
| Pi controls, events, music | Both users `~/.config/cube/events.token` | Same privately transferred random token, at least 32 ASCII non-whitespace characters |
| Spotify Web API | Backend `~/.config/cube/spotify-web/` | PKCE helper creates private config/tokens; see [Spotify](SPOTIFY.md) |
| Soloist | Pi private config/data/cache directories | Separately obtained receiver and own API key; see [Spotify](SPOTIFY.md) |

[Media events](../server/MEDIA_EVENTS.md) includes a token generator that refuses overwrite and webhook setup. Missing integration configuration returns an error when that feature is used; it does not provision devices automatically. Mixed “all lights” commands can report partial success across configured/unconfigured groups. The Pi can display clock/original animations without backend credentials. Weather needs internet access. Music/artwork is optional and expires safely when unavailable.

## Optional AI node

Follow [AI-node setup](../ai_node/README.md) for its separate GPU environment and LLM endpoint. Do not install CPU and GPU ONNX Runtime distributions together. The backend calls the node over HTTP; it never imports its code. Keep the local backend speech fallback provisioned.

## Deployment templates

Render templates for the actual service account/UID and checkout using [deployment layout](DEPLOYMENT_LAYOUT.md). The renderer requires `CUBE_DISPLAY_USER`, supplied by the rendered unit. Review the generated units before installation. Rendering templates and running repository checks do not install or restart services.
