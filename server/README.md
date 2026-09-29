# Backend

The FastAPI entrypoint `server.py` connects voice processing, integrations and media state. Run one worker on a trusted interface. [Setup](../docs/SETUP.md) covers the environment and assets; [deployment](deploy/README.md) covers systemd.

## Code layout

- `core/`: HTTP contracts, cancellation, STT selection, deterministic/AI routing, per-client history, tool validation and execution.
- `features/`: Cube controls, lighting/plugs, media requests/events and Spotify `MusicController`.
- `integrations/`: external APIs, private configuration and credential handling.
- `speech/` and `tts/`: local CPU inference fallbacks.
- `scripts/`: explicit model setup and read-only diagnostics; these are not startup hooks.

The backend executes tools and sends hardware commands to the Pi through an authenticated control channel. Music commands are deterministic and excluded from AI-visible tools. A successful external acknowledgment is not proof of physical completion, and commands with an uncertain outcome are not retried. The backend uses its own tool reply after execution; the AI node's optional `respond` contract remains independently tested.

## Configuration and operation

See `config/examples/` at the repository root. Blank `CUBE_AI_NODE_URL` disables remote inference. Backend-local Whisper is still required; Kokoro failure leaves `/tts` unavailable without preventing other routes. `CUBE_LOG_TRANSCRIPTS=1` explicitly enables local fallback transcript diagnostics; leave it unset for normal operation. Do not export or share runtime journals without reviewing them.

Govee reads `~/.config/cube/govee.json` plus `govee_api_key`. Its CLI and feature wrapper share validation/control logic. Tuya reads `tuya.env` as data, never shell code. Its optional aliases are `lamp`, `mirror`, and `mushroom`; `tuya`, “smart life” and “small lights” select that group. “All lights” includes Govee plus those plugs and can return partial success. Device cloud reports can be stale.

```sh
PYTHONPATH=server server/.venv/bin/python -m integrations.tuya status all
```

This diagnostic only reads device state. Other integration CLIs can change devices; inspect their help before use.

## References

- [Architecture and HTTP routes](../docs/ARCHITECTURE.md)
- [Voice catalog](../docs/VOICE_CATALOG.md)
- [Movie commands and cancellation](OVERSEERR.md)
- [Media events and authentication](MEDIA_EVENTS.md)
- [Spotify setup and contracts](../docs/SPOTIFY.md)
- [Tests](../tests/README.md)
