# Pi deployment

Complete [setup](../../docs/SETUP.md), review wiring and select the physical audio device first. Templates target a non-root voice/audio account; only the panel renderer runs as root. Polkit permits that specific account's Cube power operations. Review the rule before installation.

## Render and review

From the checkout root as the intended Pi service user:

```sh
python3 scripts/render_deployment.py client --user "$(id -un)" --uid "$(id -u)"   --home "$HOME" --repo "$PWD" --output /tmp/cube-client-units
systemd-analyze verify /tmp/cube-client-units/*.service /tmp/cube-client-units/*.timer
```

Missing executable/config warnings indicate prerequisites still need provisioning. Rendering itself performs no installation. Verify `User`, `Group`, `HOME`, `WorkingDirectory`, `XDG_RUNTIME_DIR`, every `ExecStart`, matrix geometry/rotation/PIO flags, and the display account setting. The renderer requires `CUBE_DISPLAY_USER`; the template supplies the same user as voice. Local socket access remains restricted to that UID or root.

## Audio prerequisite

Voice targets `cube.assistant`; Soloist targets `cube.spotify`. Both buses follow the selected physical speaker. The standalone filter is a client of the user's existing PipeWire server, not a replacement server configuration.

Install the current graph into the path used by the audio unit:

```sh
sudo install -d -o root -g root -m 755 /opt/cube/soloist/config
sudo install -o root -g root -m 644 client/deploy/pipewire/cube-audio.conf /opt/cube/soloist/config/
```

The path retains the established installed layout even when Spotify is disabled. Provision a persistent user PipeWire/WirePlumber session; on a headless Pi, deliberately enable lingering for that user if required by your OS. `user@UID.service` alone does not prove the audio server is running. Verify `wpctl status`, the selected default sink and access to `/run/user/UID/pipewire-0`. Do not load the retired `cube-spotify-audio-proof` graph alongside the current buses.

## Install during a deliberate maintenance window

Build with `make -C client/native/cube-display -j2`. Preserve the previous installed binary/unit for rollback. Stop an existing renderer before copying the candidate; never run a second instance against HUB75. Install `build/cube-display` as `client/native/cube-display/cube-display` with root ownership and mode 0755. Ensure the service account cannot replace a root-executed binary without your intended deployment permissions.

For a first installation after prerequisites are complete:

```sh
sudo install -o root -g root -m 644 /tmp/cube-client-units/cube-display.service   /tmp/cube-client-units/cube-voice.service /tmp/cube-client-units/cube-audio.service   /tmp/cube-client-units/cube-audio-control.service /etc/systemd/system/
sudo install -o root -g root -m 644 /tmp/cube-client-units/60-cube-power.rules /etc/polkit-1/rules.d/
sudo systemctl daemon-reload
sudo systemctl enable --now cube-audio.service cube-audio-control.service cube-display.service cube-voice.service
```

Existing services need a deliberate restart to load new binaries/environment; do not treat `enable --now` as an update operation. No command here should be run automatically as part of repository cleanup.

`~/.config/cube/client.env` is the optional voice EnvironmentFile. Use the example, edit endpoints/weather/capture and protect it. The former `CUBE_HOME_WEATHER_*` drop-in names are not consumed; use `CUBE_WEATHER_LATITUDE`, `CUBE_WEATHER_LONGITUDE`, and `CUBE_WEATHER_TIMEZONE`.

## Optional Spotify services

Follow [Spotify](../../docs/SPOTIFY.md) to obtain a private receiver/key and configure backend authorization. Then review/install the rendered receiver, bridge, status, age-check service/timer files. Do not enable them before their required directories and `spotify-web-bridge.env` exist. They have independent lifetimes from voice/display.

## Acceptance

Verify wake → capture → reply → idle, interrupt during processing and speech, display transitions, silent follow-up timeout, local Piper fallback, physical speaker routing and assistant/music volume separation. Test reboot/shutdown only as an explicit supervised hardware check. Confirm no stale canceled reply releases a power command. Keep private diagnostics out of Git. See [voice validation](../../docs/VOICE_BARGE_IN.md) and [release checklist](../../docs/PROJECT_STATUS.md).
