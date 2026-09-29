# Backend systemd deployment

Complete [setup](../../docs/SETUP.md) first. Use a non-root service account with its own environment, model cache and private configuration. Run one backend worker.

From the checkout root, render using the backend account's real values:

```sh
python3 scripts/render_deployment.py server --user "$(id -un)" --uid "$(id -u)"   --home "$HOME" --repo "$PWD" --output /tmp/cube-server-units
systemd-analyze verify /tmp/cube-server-units/cube-server.service
```

Review the unit. It reads optional `~/.config/cube/server.env` and binds port 8765 on all interfaces; firewall it to trusted clients or change the bind interface. Review private integration files and model paths before installation. Verify paths exist and the service user can traverse/read them.

During an intentional deployment window:

```sh
sudo install -o root -g root -m 644 /tmp/cube-server-units/cube-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cube-server.service
```

For an existing service, `enable --now` does not reload changed process environment; perform a deliberate restart after reviewing the effect on in-flight requests. Preserve the previous unit/source/environment for rollback. Check `/health`, local STT/TTS, authenticated controls and configured integrations before declaring acceptance. Never publish raw journals or config files.
