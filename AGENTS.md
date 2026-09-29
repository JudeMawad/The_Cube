# Cube project context

Cube is a voice-operated Raspberry Pi 5 device with a 64×64 LED matrix. Three independently deployable services cooperate over HTTP: the Pi client captures speech and owns physical output; the backend owns voice orchestration, integrations, and persistent media state; the optional AI node provides remote STT, LLM interpretation, and TTS. Each service has its own Python environment and assets. Runtime modules do not import code from another service.

## Where to work

- `client/app/`: Pi entrypoint `cube.py`; `audio/` playback, Piper fallback, and Bluetooth speaker control; `control/` Pi hardware command worker; `notifications/` media event worker; `config/` asset paths.
- `client/native/cube-display/`: C++ LED renderer, content masks and transitions, manual socket control, build helper. `client/deploy/` has Pi systemd units; `client/assets/` documents user-supplied wake/voice assets and holds original animation samples; `client/third_party/` is the pinned matrix dependency.
- `server/server.py`: FastAPI entrypoint. `server/core/` owns voice routing, AI HTTP contracts, client history, and tool validation/execution. `server/features/` owns media, lighting, plugs, and Cube controls; `server/integrations/` owns external API adapters; `server/speech/` and `server/tts/` own local inference. `server/deploy/` has the backend unit.
- `ai_node/`: independent FastAPI inference service, model settings and assets; `deploy/windows/` has optional Windows/WSL startup orchestration.
- `tests/`: client, server, AI-node, and cross-service integration tests. `docs/`: architecture, status, deployment, and release validation.

## Working rules

- Before modifying code, identify the relevant component. Read only the files needed for the requested task; do not explore the entire repository unless necessary or repeatedly read files already inspected.
- Prefer modifying existing implementations over duplicate functionality. Avoid unrelated refactoring and unrelated file changes. Preserve working behavior and the HTTP contracts between services.
- Keep service imports, assets, and environments independent. Maintain strict wire schemas, backend-owned tool execution and state, Pi-owned hardware effects, and the existing local fallback behavior where applicable.
- Do not change hardware configuration or system services unless explicitly required. Native builds create a candidate under `client/native/cube-display/build/`; do not run a second renderer against the panel.
- Run targeted tests when appropriate instead of the entire suite. Tests mock many external systems; passing tests do not prove physical Pi, GPU, Bluetooth, or live integration behavior.
- Keep responses concise. Do not give lengthy explanations for routine changes. Ask for clarification only when essential.

## Tests and references

From the repository root, select the relevant component (using its own environment):

```sh
PYTHONPATH=client/app client/app/.venv/bin/python -m unittest discover -s tests/client -t . -v
PYTHONPATH=server CUBE_AI_NODE_URL= server/.venv/bin/python -m unittest discover -s tests/server -t . -v
ai_node/.venv/bin/python -m unittest discover -s tests/ai_node -t . -v
PYTHONPATH=server python -m unittest discover -s tests/integration -t . -v
```

See [architecture](docs/ARCHITECTURE.md) for code paths and contracts, [project status](docs/PROJECT_STATUS.md) for verification limits, [test guidance](tests/README.md) for dependencies and alternatives, and [deployment layout](docs/DEPLOYMENT_LAYOUT.md) before changing installed paths.
