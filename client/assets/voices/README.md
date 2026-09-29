# User-provided Piper voice

Piper voice weights and voice JSON are not distributed here. For the default fallback, obtain **both** `en_US-lessac-medium.onnx` and its matching `en_US-lessac-medium.onnx.json` from the [upstream voice directory](https://huggingface.co/rhasspy/piper-voices/tree/main/en/en_US/lessac/medium), review its model card/terms, and place them together in this directory.

Alternatively set `CUBE_PIPER_VOICE_PATH` to the ONNX path; keep its matching `.onnx.json` alongside it. Relative paths resolve against `client/`. Install the client environment so the Piper executable exists at `client/app/.venv/bin/piper`. Local fallback speech requires both the model and its configuration. No speech weights, training checkpoints or synthetic training samples belong in Git.
