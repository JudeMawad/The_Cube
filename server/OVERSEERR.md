# Movie conversations through Cube

`~/.config/cube/overseerr.env` is the Overseerr configuration file.
Assignments are parsed as data, never sourced. Requests use
`/api/v1/request` POST with a TMDB ID. Confirmed cancellation also uses narrowly
scoped Radarr operations; Sonarr remains read-only.

## Asking for a movie

“Download Interstellar”, “Can you get Interstellar?”, and “Could you download
Interstellar, please?” request a unique exact match immediately after checking
existing status. Cube names the accepted movie and promises readiness updates
only when ownership tracking succeeds. **Do not use these as search smoke tests.**

“Download” asks “Which movie?” and accepts a bare title next. “I want to watch
Dune” checks availability and offers to request it if necessary. A status inquiry
never submits a request, including after clarification or a correction.

Close or approximate matches ask a short question. Up to three candidates are
spoken in selection order; answer with a title, year, “the first one”, “the newer
one”, or “the original”. Newer/original requires matching titles and known years.
For larger sets, supply the full title and year. “Yes please” confirms one
candidate, never an ambiguous list.

“No” rejects a suggestion and asks for the intended title. “No, I meant Alien”
corrects it. After a submitted request, corrections require confirmation before
adding another request. “Cancel” and “never mind” end the conversation; they do
not withdraw an accepted request. Light and speaker commands preserve pending
movie choices. Two misunderstood answers end automatic listening.

## Cancelling a request or download

Say **“Hey Cube, cancel the download”**, “stop downloading it”, “cancel Dune”,
or “cancel my last movie request”. Cube names the movie and year, warns that
partial files and the Radarr entry will be removed, and asks for confirmation.
Say “yes” to proceed or “no” to leave the request alone. Bare “cancel” and “never mind” still only
end the conversation. Confirmations expire after 120 seconds and never survive
restart; the remembered movie does survive restart.

Only confirmed requests owned by this Cube ID can be cancelled. New requests
save the returned Overseerr request ID. An older request can be matched only to
one standard-resolution request with the same TMDB ID and requesting API user,
created within 120 seconds of Cube's recorded submission. Shared requests,
unverified identities, and imported/available movies are refused.

Before confirmation and before each destructive step, Cube verifies ownership,
Overseerr request identity, the configured Radarr server, movie identity, and
queue entries. It unmonitors the movie, removes its confirmed queue entries and
partial download data without blocklisting or searching again. It then removes
the Overseerr request and deletes the empty Radarr movie entry. Radarr removal uses
`deleteFiles=false` and `addImportExclusion=false`: imported library files are
preserved, and the movie can be added again later. The entry must be verified
absent before cancellation is reported complete. These services do not offer a
shared transaction; if state changes or a response is lost, Cube reports an
incomplete or unverified cancellation.

“Any update?” and “Did you cancel it?” check cancellation progress without
repeating writes. “Try cancelling it again” checks the remaining work and asks
for fresh confirmation. Progress persists across restarts. Cancellation suppresses
pending and late notifications. Once completion is verified, “Get Dune” starts
a new tracked attempt that Overseerr can add to Radarr again, retaining the
previous history. Older cancellations that left an unmonitored entry can still
be requested again, or removed by asking to cancel the movie again and confirming.
Status checks never delete these older entries automatically.

## Listening versus remembering

| Behavior | Lifetime |
| --- | --- |
| Listen after a movie reply, without another wake word | 10 seconds after playback/cleanup |
| Unfinished title selection or confirmation | 120 seconds; cleared by backend restart |
| Selected movie for later status questions | Persistent until replaced or explicitly forgotten |

After ten minutes or several hours, say **“Hey Cube, has it started yet?”**,
“Hey Cube, is it ready?”, or “Hey Cube, any update?” Cube uses its remembered
movie and names it in the answer. Once the short listening window ends, the wake
word is required again. Speech that starts inside that window may finish normally.

**Documentation discrepancy:** this guide previously described playback as
uninterruptible, while the [voice guide](../docs/VOICE_BARGE_IN.md) describes
wake-word barge-in during playback. Movie and notification playback need a
separate behavior check to resolve that conflict.

The `client_movie_context` SQLite table stores a reference per Cube ID.
On upgrade, a missing reference falls back to that Cube's latest confirmed movie
request. “Forget that movie” writes an explicit cleared reference, preventing
that fallback. “What was the last movie I requested?” explicitly consults history.
Ambiguous searches do not overwrite the reference. Remembering a movie does not
reactivate an expired confirmation.

Status reads current Overseerr availability/request state and stored start/import
webhooks, even if their notifications have not been spoken. A start event proves
a download started, not that it is currently progressing. No percentages or ETAs
are invented. Upstream failures distinguish the last known event from current
status. Missed webhooks cannot be reconstructed from this history.

## Interfaces and operation

The Pi sends stable `X-Cube-Client-ID` headers (hostname by default; override with
`CUBE_CLIENT_ID`). Older callers fall back to direct IP. Memory belongs to the
Cube identity, not an identified speaker. Run one backend worker and one voice
process per ID. Existing voice/TTS endpoints remain trusted-network APIs.

`/voice` returns `listen_for_seconds`,
`context_expires_in` (temporary clarification only), and `timings` in seconds.
Legacy `follow_up` and `expires_in` remain on clarification responses. New Pi
clients use the explicit listening field; older-server fallback is capped at ten
seconds. `processing_time` remains transcription time. Timing fields include
transcription, command, voice total, and movie/upstream work when applicable.

Model execution is serialized separately for Whisper and Kokoro and runs outside
the async event loop. HTTP connections are reused. Kokoro caches up to 64 WAVs
by text and voice configuration. The Pi logs endpoint delay, voice round trip,
TTS readiness, and time from last detected speech to first reply playback start.
A slow turn can play one short local progress tone; all playback stays on the
main audio loop. Notifications wait until the turn and listening window finish.

Requests for one client are serialized separately from other clients. A per-movie
lock and a short accepted-submission guard prevent duplicates during upstream
status propagation. Uncertain POSTs are not automatically retried. An unresolved
attempt remains guarded for the process lifetime; check Overseerr before trying
again. Tracking and notification history retain their existing ownership rules.

## Validation and deployment

See [media events](MEDIA_EVENTS.md) for webhooks, ownership, and delivery limits.
Run the backend suite from the repository root; the [test guide](../tests/README.md)
also covers the client suite:

```sh
# From the repository root
PYTHONPATH=server PYTHONDONTWRITEBYTECODE=1 server/.venv/bin/python -m unittest discover -s tests/server -t . -v
```

The backend also provides `scripts/check_media.py`: it performs read-only service
calls, silent voice uploads, and TTS synthesis without playback or media requests.

Deploy the backend first, then the Pi client. Preserve the existing SQLite file;
the transactional migration adds cancellation state and request generations,
and rebuilds the notification uniqueness constraint while preserving IDs,
receipts, delivery state, and history.
Restart `cube-server.service`, run the read-only smoke check, update the Pi files,
and restart `cube-voice.service`. These updates do not require restarting the renderer.
Keep a SQLite backup and the previous source outside the repo. After a
cancellation, do not run an older backend against the migrated database: older
code cannot enforce cancellation or generation filters and could announce stale
events. Rollback needs coordinated code/database restoration and manual
reconciliation of any upstream changes; restoring SQLite cannot undo a download
removal. Live cancellation testing requires a deliberately disposable request.

After deployment, check real microphone pickup, short follow-ups, later wake-word
status questions, and notifications on the Pi. Compare identical recordings
before and after deployment and record median/p95 response delay; synthesized-input network
measurements do not replace physical microphone and playback checks.
