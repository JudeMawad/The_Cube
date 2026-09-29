# Deployment layout

Each service has its own Python environment and assets. Template rendering and repository checks do not install files or restart services. The [client restart script](../client/deploy/README.md#rebuild-and-restart-the-client) explicitly rebuilds and installs the renderer and restarts client services when you run it.

| Component | Source / environment | Assets | Installed entrypoint |
| --- | --- | --- | --- |
| Pi voice | `client/app/`, `client/app/.venv/` | `client/assets/` | `client/app/cube.py` |
| Matrix | `client/native/cube-display/` | `client/assets/animations/` | separately installed `client/native/cube-display/cube-display` |
| Backend | `server/`, `server/.venv/` | `server/assets/models/` | `uvicorn server:app` from `server/` |
| AI node | `ai_node/`, `ai_node/.venv/` | `ai_node/assets/models/` | `ai_node/start.sh` |

Model weights and Piper voice JSON are not distributed; see [wake provisioning](../client/assets/models/README.md) and [voice provisioning](../client/assets/voices/README.md). Model path overrides resolve against the owning service (`client/`, `server/`, or `ai_node/`), not the shell working directory. The Pi Python environment lives under `client/app/`; the Piper executable is resolved there. See [setup](SETUP.md) for provisioning.

## Templates and installed files

Repository systemd/polkit files contain `@CUBE_USER@`, `@CUBE_HOME@`, `@CUBE_REPO@`, and `@CUBE_UID@`. Render to a new staging directory:

```sh
python3 scripts/render_deployment.py client --user "$(id -un)" --uid "$(id -u)"   --home "$HOME" --repo "$PWD" --output /tmp/cube-client-units
```

Use `server` for backend units. The helper rejects root service users, unsafe paths and an existing output directory. It never installs, enables, reloads or restarts anything. Review the generated files and use [Pi](../client/deploy/README.md) or [backend](../server/deploy/README.md) deployment instructions. These templates assume the user's primary group has the same name; adjust `Group=` if your account differs.

The native build writes only `client/native/cube-display/build/`. Preserve the installed executable for rollback before replacement, and stop the existing renderer during an intentional installation. Hardware flags remain installation-specific.

## Private and generated data

Credentials live under the service user's `~/.config/cube/`. The backend stores media state at `~/.local/state/cube/media-events.sqlite3`. Pi Soloist data/cache is under `~/.local/share/cube/soloist/` and `~/.cache/cube/soloist/`; user-runtime snapshots are under `$XDG_RUNTIME_DIR/cube-audio/`. None belongs in Git.

Each service includes a tracked `voice_commands.json` generated from `config/voice_commands.toml`. Each service can deploy without loading the repository-level catalog. The cat and alien `.cubeanim` files are tracked so the renderer does not require conversion tools at runtime. Regeneration commands are documented in [the catalog guide](VOICE_CATALOG.md) and [display architecture](DISPLAY_ARCHITECTURE.md).

Ignored local model caches, recordings, old installed binaries and rollback copies may remain on a development machine. These files are not part of a public clone; keep any still needed for local operation or rollback.
