# Optional AI node

This FastAPI service provides `/transcribe`, `/process`, `/tts`, and `/health`. It communicates with the backend over HTTP and does not import backend or Pi code. The backend executes tools and stores state; the Pi controls audio and hardware. Omit the node by leaving backend `CUBE_AI_NODE_URL` empty.

## GPU setup

The intended runtime uses Python 3.12, an NVIDIA GPU, CUDA 12 and cuDNN 9. Linux or WSL2 is supported by the launcher. In WSL, install a compatible NVIDIA Windows driver; do not install a Linux display driver inside WSL. Check `nvidia-smi` and the [NVIDIA WSL guide](https://docs.nvidia.com/cuda/wsl-user-guide/index.html).

```sh
python3.12 -m venv ai_node/.venv
ai_node/.venv/bin/python -m pip install -r ai_node/requirements.txt
ai_node/.venv/bin/python -m pip install --no-deps kokoro-onnx==0.6.1
ai_node/.venv/bin/python -m pip check
```

Kokoro's distribution declares CPU ONNX Runtime, so `pip check` can report that unmet metadata dependency when only `onnxruntime-gpu` is intentionally installed. Inspect other failures; do not “fix” that declaration by installing both ONNX Runtime distributions. Use a separate environment for [CPU-only tests](../tests/README.md).

Provision `ai_node/assets/models/kokoro/kokoro-v1.0.onnx` and `voices-v1.0.bin` from the model sources linked in [notices](../THIRD_PARTY_NOTICES.md), or set explicit paths. Whisper defaults to `base.en`, CUDA, float16 and its service-local cache. First load can download weights; provision/cache them before an offline deployment. Each service needs its own model setup.

## Configuration

Use `config/examples/ai-node.env.example` as a reference. Export edited variables in the launch environment; Python does not automatically source this file.

| Variable | Default / purpose |
| --- | --- |
| `CUBE_AI_WHISPER_MODEL` | `base.en` or a local converted model directory |
| `CUBE_AI_WHISPER_DEVICE` | `cuda`; explicit CPU override is for deliberate experiments, never automatic fallback |
| `CUBE_AI_WHISPER_COMPUTE_TYPE` | `float16` |
| `CUBE_AI_WHISPER_CACHE_DIR` | `ai_node/assets/models/whisper` |
| `CUBE_AI_KOKORO_MODEL_PATH` | `assets/models/kokoro/kokoro-v1.0.onnx` relative to this service |
| `CUBE_AI_KOKORO_VOICES_PATH` | `assets/models/kokoro/voices-v1.0.bin` |
| `CUBE_AI_LLM_BASE_URL` | Required for interpretation; OpenAI-compatible API base, including `/v1` if required by the host |
| `CUBE_AI_LLM_MODEL` | Exact installed model identifier |
| `CUBE_AI_LLM_TIMEOUT` | Positive seconds, default 20 |

Run `./ai_node/start.sh` from any working directory. It discovers pip-installed cuBLAS/cuDNN libraries before Python starts and launches one Uvicorn worker on port 8766. It binds all interfaces; restrict network access. `/health` reports load/configuration state, not proof of inference. Validate real STT, TTS and the upstream LLM separately. Missing models or invalid settings are not repaired by a health check.

## Interpretation contract

`/process` receives a strict request with transcript, bounded client history and backend-advertised tool definitions. It requests one non-streaming structured result from the LLM: either a conversation reply or one tool intent. The backend validates/executes that intent. Unknown fields, invalid tool arguments and malformed LLM results are rejected. Music tools are not advertised to AI.

The schema also supports a constrained `respond` phase for compatibility. The current backend uses its own tool result wording and does not call that phase. Keep both service-local schemas and the cross-service contract tests; do not introduce runtime cross-imports.

Disconnect cancellation closes owned HTTP work, skips queued inference and suppresses stale completions. Running native inference keeps its resources locked until cleanup is safe. LLM HTTP cancellation does not prove the upstream GPU stopped computing. See [voice validation](../docs/VOICE_BARGE_IN.md).

## Development and deployment

[Tests](../tests/README.md) use fake inference without a GPU or model download. `server/scripts/compare_transcription.py --help` describes a manual comparison using an explicit recording; do not commit personal recordings. Optional [Windows scheduled tasks](deploy/windows/README.md) require local settings and deliberate installation. They are not needed for ordinary Linux deployment.
