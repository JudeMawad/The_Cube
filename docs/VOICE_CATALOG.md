# Voice command catalog

`config/voice_commands.toml` defines the command vocabulary. It generates independent snapshots in `client/app/config/voice_commands.json` and `server/core/voice_commands.json`. The generated JSON files are tracked so each service can run without the root catalog.

```sh
python3 scripts/build_voice_catalog.py
python3 scripts/build_voice_catalog.py --check
```

Examples document a pattern; they do not add aliases. Changing a regex, exact phrase, capture group or eligibility flag changes behavior and needs routing/safety tests. The generator checks command identities, example matches and fast-rule ambiguity.

Named Cube/plug/light commands and explicit music controls use local tool calls validated by the backend. “Small lights” and “smart life” normalize to the Tuya group. Bare colors are not complete light commands. Negative, quoted, hypothetical and compound requests must not become executable by stripping their safety context.

Music commands and private playlist aliases are described in [Spotify](SPOTIFY.md). Volume without the music qualifier controls assistant volume; music volume controls Spotify. The optional AI route is not allowed to execute the local-only music tools.

Keep both generated snapshots synchronized and run affected component tests plus cross-service tests when a wire contract changes. Do not edit generated JSON directly.
