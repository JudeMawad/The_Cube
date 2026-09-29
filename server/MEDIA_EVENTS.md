# Media events

Use one backend worker and one voice process per Cube identity. See [backend setup](../docs/SETUP.md#backend) for runtime dependencies and
[deployment](deploy/README.md) for the service unit.

## Configure webhooks

Use your reachable backend base URL, for example `http://backend.example:8765/webhooks/radarr` and `/webhooks/sonarr`. Configure name resolution/routing from the Arr container network; container-local `localhost` is not a backend on another host. Keep these endpoints on a trusted network, with TLS or a private encrypted network across untrusted links.

The installed Webhook providers both support advanced custom **Headers**. Create
a separate Cube token; never reuse an Overseerr/Radarr/Sonarr API key. This
token also authenticates Cube controls and music routes; see [security](../SECURITY.md).
On the server, generate it once without printing it:

```sh
python3 - <<'PY'
from pathlib import Path
import os
import secrets
path = Path.home() / ".config/cube/events.token"
path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as f:
    f.write(secrets.token_urlsafe(32) + "\n")
PY
```

This refuses to overwrite an existing token. Privately transfer that file to
`~/.config/cube/events.token` for the Pi voice-service user, with mode `600`.
Privately enter its value in each Arr UI as the custom header `X-Cube-Token`.
Do not put it in a URL, command argument, log, repository or screenshot.
Token files are read on each call. Missing/invalid server configuration returns
503; a wrong supplied token returns 401. The other assistant functions still work.

In each service, open **Settings → Connect → + → Webhook**, enable advanced
settings, set Method to **POST**, enter its URL above, and add the header.

- Radarr: enable **On Grab** and **On Import** (`onDownload` in the API).
- Sonarr: enable **On Grab** and **On Import Complete** (`onImportComplete` in the API).
  Individual-file **On Import** (`onDownload`) callbacks are also accepted.
- Leave upgrade, rename, delete and health notifications disabled for Cube.
- Use **Test**, then **Save**. The Test event never produces speech.

Radarr and Sonarr API clients read their own `.env` files. Overseerr owns movie requests; Arr APIs supply verification, state and guarded cancellation.

## Verified event formats

Read-only installed status/schema checks found Radarr `6.3.0.10514` and Sonarr
`4.0.19.2979`, both using API v3. Their version-matched sources confirm:

- `Grab`: a release has been sent to a download client.
- `Download`: files have been imported, not merely finished in a download client.
  Radarr supplies `movie.tmdbId` and `movieFile.id` for readiness.
- Sonarr supplies `series.tvdbId` and episode season/number; its individual
  `episodeFile` and grouped `episodeFiles` import payloads both use `Download`.
- `Test`, rename, health, delete and other events never announce anything.
  An event named `Import` is not used by these versions.

Primary sources:

- [Radarr payload builders](https://github.com/Radarr/Radarr/blob/v6.3.0.10514/src/NzbDrone.Core/Notifications/Webhook/WebhookBase.cs)
- [Sonarr payload builders](https://github.com/Sonarr/Sonarr/blob/v4.0.19.2979/src/NzbDrone.Core/Notifications/Webhook/WebhookBase.cs)

## Stored requests and events

SQLite lives at `~/.local/state/cube/media-events.sqlite3` under the backend user,
with owner-only database permissions. Back it up with the service stopped; do
not delete it on upgrade or put it in Git. Stored fields include media type,
TMDB/TVDB identity, title/year, episode where applicable, UTC epoch request time,
client identity, confirmed state and start/ready announcement flags.

The server writes an intent before the Overseerr POST and activates it only
after verified success. An early webhook can be retained during that POST but
cannot be delivered before activation. Unverified attempts remain inactive;
a new attempt makes their stale events ineligible for delivery. No unrelated media is announced.
TV event tracking requires individual `SxxExx` requests; there is no TV
conversation or whole-series readiness claim in this implementation.

Each request generation gets at most one start and one ready notification,
enforced by a SQLite unique constraint inside a transaction. Download/file identity is also
retained. Upgrades/regrabs do not cause another announcement, and a Grab received
after ready is ignored. A completed Cube cancellation followed by a new request advances its generation,
retains history, and allows new announcements. Stored cancelled download IDs are
ignored. For these re-requests, readiness requires a fresh matching start/download
ID; an import without a matching Grab is suppressed. Status questions still read
live availability even when such a notification cannot be verified.

An uncertain Overseerr POST is never retried automatically. If a GET finds an
existing request, Cube reports that status without claiming ownership of a
possibly concurrent request made elsewhere. A crash between successful POST
and SQLite activation leaves an inactive intent for manual investigation.

## Delivery and audio coordination

The Pi uses `GET /events/next`, which waits up to 25 seconds and returns one
notification or HTTP 204. `POST /events/{id}/ack` accepts JSON containing its
`ack_token`. `POST /events/{id}/validate` uses the same body and returns
`{"valid": true}` only for an undelivered event belonging to the client's active,
confirmed generation. All three routes require `X-Cube-Token` and client identity.
GET and validation never mark anything delivered.

The Pi sends `X-Cube-Client-ID` on voice and event requests: hostname by default,
override with `CUBE_CLIENT_ID`. Keep it stable and unique. Older clients that
omit it are tracked by their direct IP. See [security](../SECURITY.md) for voice/TTS endpoint restrictions.

The network worker holds one notification while the main audio loop is busy;
it does not play speech or make further polls until that item is handled.
Listening, command processing, follow-up and speech finish before notification
playback. The main loop validates the event before synthesis and again immediately
before playback, dropping cancelled/obsolete events and deferring speech if
validation is unavailable. Idle playback uses existing `speech` animation messages, discards
microphone buffers twice, resets the wake model and returns to idle.

**Documentation discrepancy:** this buffer-discard and wake-reset description
conflicts with the continuous capture and notification barge-in described in
[voice lifecycle](../docs/VOICE_BARGE_IN.md). Confirm that behavior separately
before relying on these cleanup details.

Only successful Kokoro or Piper playback is acknowledged, after microphone
cleanup. An acknowledgement is idempotent: if its response is lost, the worker
retries ACK without replaying speech while the Pi process is running. A 404 ACK
for an obsolete event is dropped. Failed
playback retains the server event and retries after 30 seconds. Connection
failures back off at 5, 10, 20, 40, then 60 seconds without stopping voice service.

Movie references also persist per client in `client_movie_context`, including an
explicit cleared reference for “forget that movie”. Status questions use stored
event stages, not announcement flags, and never acknowledge notifications.
See [movie conversation](OVERSEERR.md) for the separate memory/listening lifetimes.

Pending events survive backend restarts and Pi disconnects. A Pi crash after
speech but before ACK can cause one replay; exactly-once audible delivery across
that crash window is not promised. Webhooks missed while the backend is offline
depend on Arr delivery/retry behavior; there is no history polling/backfill.

## Spoken wording

- Tracked request accepted: `Requested {title} from {year}. I'll let you know when it's ready.`
- Start less than 120 seconds after request: `{title} just started downloading.`
- Later start: `Hey, the movie you requested earlier, {title}, just started downloading.`
- Imported: `Hey, quick update — {title} is ready to watch.`

The notification recency threshold and wording live in `media_wording.py`. Request
acceptance itself never produces a download-start notification.

Cancellation operations persist their verified target, stage, and outcome in
`movie_cancellations`; no credentials are stored there. Requests carry a
`generation`, `overseerr_request_id`, and `cancellation_state`. Pending, partial,
uncertain, and cancelled generations cannot deliver or acknowledge events.
Reconciliation is read-only; unfinished writes require renewed confirmation.
See [cancellation behavior and rollback](OVERSEERR.md#cancelling-a-request-or-download).

## Deployment and validation

After reviewing/updating the server files and provisioning the token:

```sh
cd server
sudo systemctl restart cube-server.service
systemctl is-active cube-server.service
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/check_media.py
```

After updating `cube.py`, `audio/`, `notifications/`, `config/` and the token on the Pi:

```sh
sudo systemctl restart cube-voice.service
systemctl is-active cube-voice.service
```

Do not restart the renderer for these changes. Use Test in both Arr UIs after
backend restart. Verify wake-word, year clarification and actual playback on the
Pi after deployment. An explicit download command submits immediately;
do not use it as a read-only search test.

Offline tests require no downloads or hardware:

```sh
# From the repository root
PYTHONPATH=server server/.venv/bin/python -m unittest discover -s tests/server -t . -v
PYTHONPATH=client/app client/app/.venv/bin/python -m unittest discover -s tests/client -t . -v
```

`scripts/check_media.py` performs read-only service status/search/detail calls,
silent voice uploads and in-memory TTS synthesis. It does not submit a media
request or play audio. The live service requires a restart to load edited code;
a check in a separate process does not verify the running service.
