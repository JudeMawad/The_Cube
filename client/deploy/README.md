# Pi deployment

Complete [setup](../../docs/SETUP.md), review wiring and select the physical audio device first. Templates target a non-root voice/audio account; only the panel renderer runs as root. Polkit permits that specific account's Cube power operations. Review the rule before installation.

## Render and review

From the checkout root as the intended Pi service user:

```sh
python3 scripts/render_deployment.py client --user "$(id -un)" --uid "$(id -u)"   --home "$HOME" --repo "$PWD" --output /tmp/cube-client-units
systemd-analyze verify /tmp/cube-client-units/*.service /tmp/cube-client-units/*.timer
```

Warnings about missing executables or configuration mean setup is incomplete. Rendering itself performs no installation. Verify `User`, `Group`, `HOME`, `WorkingDirectory`, `XDG_RUNTIME_DIR`, every `ExecStart`, matrix geometry/rotation/PIO flags, and the display account setting. The renderer requires `CUBE_DISPLAY_USER`; the template supplies the same user as voice. Local socket access remains restricted to that UID or root.

## Audio prerequisite

Voice targets `cube.assistant`; Soloist targets `cube.spotify`. Both buses follow the selected physical speaker. The standalone filter is a client of the user's existing PipeWire server, not a replacement server configuration.

Install the current graph into the path used by the audio unit:

```sh
sudo install -d -o root -g root -m 755 /opt/cube/soloist/config
sudo install -o root -g root -m 644 client/deploy/pipewire/cube-audio.conf /opt/cube/soloist/config/
```

The path retains the established installed layout even when Spotify is disabled. Provision a persistent user PipeWire/WirePlumber session; on a headless Pi, enable lingering for that user if required by your OS. `user@UID.service` alone does not prove the audio server is running. Verify `wpctl status`, the selected default sink and access to `/run/user/UID/pipewire-0`. Do not load the retired `cube-spotify-audio-proof` graph alongside the current buses.

## Install during maintenance

Build with `make -C client/native/cube-display -j2`. Preserve the previous installed binary/unit for rollback. Stop an existing renderer before copying the candidate; never run a second instance against HUB75. Install `build/cube-display` as `client/native/cube-display/cube-display` with root ownership and mode 0755. Ensure the service account cannot replace a root-executed binary without your intended deployment permissions.

For a first installation after prerequisites are complete:

```sh
sudo install -o root -g root -m 644 /tmp/cube-client-units/cube-display.service   /tmp/cube-client-units/cube-voice.service /tmp/cube-client-units/cube-audio.service   /tmp/cube-client-units/cube-audio-control.service /etc/systemd/system/
sudo install -o root -g root -m 644 /tmp/cube-client-units/60-cube-power.rules /etc/polkit-1/rules.d/
sudo systemctl daemon-reload
sudo systemctl enable --now cube-audio.service cube-audio-control.service cube-display.service cube-voice.service
```

Restart existing services to load new binaries or environment settings; `enable --now` does not update an already running process.

`~/.config/cube/client.env` is the optional voice EnvironmentFile. Use the example, edit endpoints/weather/capture and protect it. The former `CUBE_HOME_WEATHER_*` drop-in names are not consumed; use `CUBE_WEATHER_LATITUDE`, `CUBE_WEATHER_LONGITUDE`, and `CUBE_WEATHER_TIMEZONE`.

## Optional Spotify services

Follow [Spotify](../../docs/SPOTIFY.md) to obtain a private receiver/key and configure backend authorization. Then review/install the rendered receiver, bridge, status, age-check service/timer files. Do not enable them before their required directories and `spotify-web-bridge.env` exist. They have independent lifetimes from voice/display.

## Rebuild and restart the client

After the first installation, run this from the checkout root as the Pi voice-service user, without putting `sudo` before Python:

```sh
python3 scripts/restart_client.py
```

The [restart script](../../scripts/restart_client.py) builds the renderer with `make -j2` while services keep running, then requests sudo access, stops the client, backs up the installed renderer to `client/native/cube-display/cube-display.backup`, and installs the candidate as root with mode 0755. It starts the audio graph, gain worker and display, followed by selected Spotify services and voice. Expect speech and music to be interrupted. It checks service stability for ten seconds and queries the renderer's control socket; physical output still needs checking on the Pi.

These options skip the build or preview the operation:

```sh
python3 scripts/restart_client.py --restart-only
python3 scripts/restart_client.py --dry-run
```

The script finds the checkout from its own location, so an absolute script path also works from another directory. Run it from the checkout used by the installed services; it refuses to install from a second clone. Voice, display, audio and gain-worker units must already be installed. Optional Spotify units and the age-check timer are selected only when enabled or active; absent or masked optional units are skipped. Existing systemd dependencies still apply. The age-check oneshot is not invoked directly, though its timer keeps its normal schedule.

Settings, credentials, model files, Bluetooth pairing and installed units/drop-ins are preserved. It does not install Python dependencies, convert GIFs, update Soloist, restart the backend/AI node or restart system PipeWire/Bluetooth. Restarting the renderer reloads the existing `.cubeanim` catalog and initializes display settings from the installed unit. Use `--restart-only` after changing animation files or Python client code.

Build or preflight failure leaves services untouched. A failed renderer installation, startup or socket check triggers an attempt to restore the previous binary and previously running services. A failure in another service is reported without restarting healthy services again. Errors return a nonzero exit status and show relevant `journalctl` commands. Check the final status; recovery is best-effort and does not restore previous source files or assets.

If preflight reports missing `CUBE_DISPLAY_USER`, set `Environment=CUBE_DISPLAY_USER=YOUR_PI_USER` in the installed display unit or a service drop-in, matching the voice unit's `User`. The script checks this direct setting and rejects display EnvironmentFile overrides or an UnsetEnvironment entry for it. If installed units changed, run `sudo systemctl daemon-reload` before retrying. The restart script does not replace or rewrite service configuration.

## Check the installation

Verify wake → capture → reply → idle, interrupt during processing and speech, display transitions, silent follow-up timeout, local Piper fallback, physical speaker routing and assistant/music volume separation. Test reboot/shutdown only as an explicit supervised hardware check. Confirm no stale canceled reply releases a power command. Keep private diagnostics out of Git. See [voice validation](../../docs/VOICE_BARGE_IN.md) and [testing limits](../../docs/PROJECT_STATUS.md).
