# Spotify and audio

Cube uses Soloist as the Pi-local Spotify Connect receiver and the Spotify Web API for backend control. Spotify internet access, your own authorization and a reachable backend are required. The optional AI node is not required for music commands. No receiver binaries, keys, OAuth tokens or private playlists are bundled.

## Audio and control paths

```text
Phone / Spotify -> Soloist -> cube.spotify -> stereo DSP gain -> physical speaker
Speech / cues ------------> cube.assistant ----------------> physical speaker
Pi Coordinator -> expiring activity lease -> local gain worker
Pi receiver observer -> backend reporter -> MusicController
Voice -> backend deterministic parser -> validated music tool -> MusicController
```

The graph runs independently of voice. Assistant volume changes only `cube.assistant`; Spotify user volume and DSP duck gain are separate controls. Both outputs follow the selected physical speaker. The gain worker uses 0.10 duck gain, 150 ms down, 400 ms restore and 20 ms cadence, with lease/process liveness recovery. Receiver snapshots and interaction leases live in `$XDG_RUNTIME_DIR/cube-audio/`. The ducking lease expires if the voice process stops responding or crashes. ReSpeaker and Bluetooth need separate physical acceptance.

## Backend authorization

Create your own Spotify developer application and configure the exact loopback redirect `http://127.0.0.1:8768/callback`. The application Client ID is a public identifier; tokens and Soloist keys are secrets. From the repository root on the backend:

```sh
PYTHONPATH=server server/.venv/bin/python -m integrations.spotify.setup configure \
  --client-id YOUR_PUBLIC_CLIENT_ID --cube-id cube
PYTHONPATH=server server/.venv/bin/python -m integrations.spotify.setup authorize
```

Run the browser callback on the backend host, or intentionally tunnel its loopback port from your workstation. The helper binds only `127.0.0.1:8768`, validates PKCE/state and closes after completion or timeout. The production API does not expose an OAuth callback. Optional `authorize --playlists` requests private-playlist read access. Spotify account/app restrictions can change; review [current Web API documentation](https://developer.spotify.com/documentation/web-api) before setup.

The helper owns `~/.config/cube/spotify-web/` (0700): `config.json`, `tokens.json`, `tokens.lock` (0600). Files are bounded, owner-checked, no-symlink and atomically replaced. Refresh is locked across processes. Missing/invalid authorization is reported rather than guessed. `logout` removes local Web API tokens; revoke the grant separately in Spotify account settings if desired. Neither logs Soloist out.

The backend and Pi share `~/.config/cube/events.token`; `CUBE_CLIENT_ID` must match the configured Cube identity. Copy/edit `config/examples/spotify-web-bridge.env.example` to the Pi's `~/.config/cube/spotify-web-bridge.env` for the receiver status reporter. The backend `MusicController` manages Spotify state and commands. The Pi status reporter sends versioned receiver snapshots with an expiry time; the reporter does not control playback.

## Soloist installation

Read [Spotify's Soloist documentation](https://developer.spotify.com/documentation/soloist), obtain your own key, and download the ARM64 receiver directly from Spotify. Do not redistribute it. Builds expire; the helper records upstream build time rather than treating download time as a fresh build.

Prepare a candidate from an explicitly downloaded official archive:

```sh
python3 client/deploy/spotify/prepare-release.py \
  --archive /path/to/soloist.tar.gz --output /tmp/cube-soloist-candidate
python3 client/deploy/spotify/check-build-age.py --release /tmp/cube-soloist-candidate
```

Preparation validates archive/ELF structure, executes the trusted candidate's `--version`, retains notices and rejects already-old builds. Use a new destination. This does not install or authenticate the receiver.

To install, copy the reviewed candidate to root-owned `/opt/cube/soloist/releases/<build-id>/`, and create `current` as a relative symlink to that release. Preserve a still-unexpired accepted `previous` release for rollback. Install `client/deploy/spotify/*.py` and `soloist-launch` as root-owned 0644 files under `/opt/cube/soloist/tools/`. The unit invokes the launcher with Python. Install the current audio graph as described in [Pi deployment](../client/deploy/README.md).

Create owner-only directories `~/.config/cube/spotify`, `~/.local/share/cube/soloist`, and `~/.cache/cube/soloist`; store the key as `spotify/soloist.key` mode 0600 through a private editor or hidden prompt. The systemd unit uses `LoadCredential`; do not place the value in an environment variable or command line. The managed root-owned credential copy may be 0400 or 0440; the original remains 0600. Keep local session/cache/crash data private.

Render and review the Pi templates. Install the receiver, bridge, status, age-check service and timer only after their prerequisites exist. Start and enable them alongside the independent `cube-audio` graph and gain worker. Do not run a second receiver or the retired proof graph. Check build age before updating the `current` symlink; never edit the recorded timestamp to bypass expiry.

## Supported grammar and precedence

| Say after “Hey Cube” | Behavior |
| --- | --- |
| `play music`, `play some music`, `play Spotify` | Existing resume/transfer; no random selection |
| `resume music`, `resume` | Resume/transfer |
| `pause music`, `pause`, `stop music`, `stop the music` | Pause; preserve queue and receiver |
| `next`, `next song`, `next track`, `skip this song` | One next operation |
| `previous`, `previous song`, `previous track` | One previous operation |
| `music volume 40`, `Spotify volume 20`, `set the music volume to 40 percent` | Spotify user volume, integer 0–100 |
| `volume 40` | Assistant volume only |
| `play Blinding Lights by The Weeknd` | Require matching track title and artist |
| `play Don't Stop Me Now by Queen` | Positive command; negative title words preserved |
| `play track Blinding Lights` | Deliberately select the first eligible Spotify track search result |
| `play artist Pink Floyd` | Exact artist resolution; play artist context |
| `play playlist Evening Chill` | Exact playlist name, configured alias, or applicable personal playlist |
| `play my Discover Weekly` | Alias or accessible personal playlist; no public substitute |

Exact controls run before named playback. Explicit `artist`, `playlist`, and
`track` forms take precedence over the `by` grammar: everything after `play track`
is a title query. A qualified request with multiple `by` separators requests a
rewritten complete command rather than guessing the title/artist boundary.
Qualified tracks take precedence over implicit personal playlists, so `play My
Way by Frank Sinatra` searches for a track. Use `play playlist my Songs by Friends`
to explicitly request a personal playlist whose name contains `by`.
Capitalization, whitespace, terminal punctuation, and existing polite wrappers
are normalized. Fully quoted commands do not execute; quoted titles inside an
explicit positive command are allowed. No fuzzy command verbs or artist-name
corrections are added. Artist and qualified-track matching remain exact after
existing Unicode/case/punctuation normalization; imperfect names may not match.

All supported music requests use `processing_source=local_fast`; execution replies
use `processing_response_source=backend`. Invalid volume and recognized unsupported music
requests use `local_clarification`. Bare `stop` remains unsupported.

## AI independence and safety

Supported commands and recognized blocked/unsupported music requests never reach
AI `/process`. Music bindings remain in the local
registry for strict argument validation, but have `ai_visible=False`; the backend
also rejects any AI intent absent from the advertised tool set. Other Cube AI
features remain available. Shared STT/TTS may still attempt their configured
remote providers, but their existing backend/Pi fallbacks let music work with
the PC completely off. Spotify internet access and a reachable backend remain
required.

One shared decision classifies requests as executable, blocked, clarification, or
not music before any search, mutation, legacy routing, or AI interpretation.
Inspecting polite or conditional wording only identifies whether a request
concerns music; removing a negation or quote
can never create an executable command. Examples such as `don't play artist Queen`,
`play track Blinding Lights, not now`, and `play artist Queen if the lights are on`
receive a backend rejection, with no search or AI call. Blocked/clarification
replies have `success=false`, `processing_source=local_clarification`, and
`processing_response_source=backend`.

Leading negative title words remain valid: `Don't Stop Me Now`, `Never Gonna Give
You Up`, and `Imagine` work. Names resembling conditions, discussion, or extra
instructions conservatively require a new command or the Spotify app. Unquoted
commas, semicolons, colons and long dashes in names also require clarification;
ordinary quoted titles such as `play track "Bye, Bye, Bye"` remain literal names.
This is a bounded deterministic grammar, not general natural-language understanding.

Semantic/recommendation playback is unsupported. Requests such as “play something relaxing”, “play some music from the
80s”, and “put on some music” receive a backend clarification without search, AI interpretation,
or a playback claim. There is no pending music action for “yeah”
to confirm. Only actual backend music replies enter history. Ordinary nonmusic
conversation is unchanged, including `play chess only if you can`. Ambiguous
untyped names such as `play Queen` remain outside the music grammar and may reach
normal AI conversation; the music subsystem cannot execute those AI results.

## Resolution, aliases, and replies

The existing search adapter returns up to five eligible results. Only `play track`
selects the top result, without demanding an exact title or asking for artist
disambiguation. No results leave playback unchanged. Qualified tracks require
matching title and artist; artist and playlist requests retain exact-match and
ambiguity checks. Equivalent releases of the same title/performers remain
equivalent under the existing resolver. A wrong artist or ambiguous playlist
never silently selects unrelated content. Clarifications ask for a new complete
command; Cube does not keep a numbered selection list.

Successful music commands return `listen_for_seconds=0`: after the spoken
confirmation Cube returns to idle without opening a follow-up window. Say
"Hey Cube" again for another command. Clarification prompts retain their
existing follow-up window for a corrected command.

Configure playlist aliases in the backend user's private file:
`~/.config/cube/spotify/playlist-aliases.json` (0600 inside the existing 0700
directory), for example:

```json
{"YOUR_PLAYLIST_ALIAS": "spotify:playlist:YOUR_22_CHARACTER_PLAYLIST_ID"}
```

Use a real playlist ID. The file must fit the size limit, be owned by the service
user and not be a symlink.
Aliases are read on demand and apply only to playlist/auto resolution, never
explicit track/artist requests. An alias needs no additional playlist-read scope.
Personal library enumeration retains optional `playlist-read-private` access;
missing access returns the existing authorization instruction without replacing
tokens or falling back to public search. Discover Weekly/Release Radar retain
personal-playlist handling. Public duplicate playlists require a unique alias
or a more specific full command.

Success replies remain “Playing.”, “Paused.”, “Skipped.”, “Previous song.”,
and “Music volume set.” Only a validated successful controller command produces
a success reply. An accepted HTTP mutation acknowledgment does not prove the
final physical playback state. Empty, JSON, and non-JSON acknowledgment bodies
are ignored after accepted 200/202/204 statuses; reads retain strict JSON rules.
There are no post-mutation state reads or mutation retries.

History contains only the user's utterance and the actual concise backend reply;
no Spotify candidates, URIs, or raw responses enter AI context. Failed commands
record truthful failure replies; cancellation suppresses stale response/history
completion through the existing turn ownership mechanism.

## Authentication, duplicate commands and cancellation

Named playback uses the same authenticated `/voice` control session/request IDs
and configured Cube identity as exact controls. Request IDs are derived by the
existing hash. The controller caches up to 128 command receipts.
Its resolved-request cache also compares the selection policy.
Duplicate successful or failed mutations are not replayed. These caches are
process-local and bounded, not durable exactly-once delivery.

Cancellation cannot undo a command already sent to Spotify or replay it. The Pi
Coordinator manages voice interactions and ducking; see [audio and control paths](#audio-and-control-paths)
for gain settings and [voice lifecycle](VOICE_BARGE_IN.md) for cancellation behavior.

## Backend display API

`MusicController` manages cloud playback state and commands. Its
read cache accepts an optional freshness interval (display: 5 s, existing default:
15 s). The internal snapshot includes the album artwork URL; `/music/state` v1
does not expose that field.

Two GET endpoints use the existing music router's Cube token and trusted client
identity validation. Both return `Cache-Control: no-store`:

- `/music/display/state`: version 1 dictionary containing `available`, `playing`,
  `track_id`, `artwork_id`, `duration_ms`, `progress_ms`, `snapshot_age_ms`, and
  `valid_for_ms`. IDs are SHA-256 opaque identifiers. Missing IDs/positions are
  null. Validity is at most 15 s; no absolute backend clock crosses hosts.
- `/music/display/artwork/{artwork_id}`: exactly 64×64 RGB24 pixels (12,288 bytes),
  `application/octet-stream`, or 404 for unavailable artwork. Only IDs previously
  obtained from validated Spotify state can initiate a fetch. Callers cannot
  supply a URL. This route never controls playback.

`features/music/display.py` validates HTTPS artwork URLs against exact hosts
`i.scdn.co` and `mosaic.scdn.co`; custom ports, credentials, query strings,
fragments, redirects, and other origins are rejected. Artwork fetches use a
separate HTTP client without Spotify/Cube credentials, proxies, or OAuth.
JPEG/PNG only, ≤2 MiB encoded, ≤2048 pixels per dimension, ≤4,194,304 pixels.
Header dimensions are validated before decode; the existing PyAV dependency
also receives a pixel allocation limit. Prepared frames fit the image on black.

The in-memory image/URL cache holds eight entries, with one fetch/decode in flight
and a 30 s failed-image retry interval. Network reads/timeouts and total fetch
time are bounded; encoded HTTP compression is rejected. Artwork work occurs
outside the MusicController lock. Artwork is cached only in memory. Fetching it
needs no extra OAuth scope, playback command, search or AI call. State refresh
uses the controller's error/rate-limit backoff.

## Pi provider and local protocol

`display/music.py:MusicProvider` starts/stops with `cube.py`, following the weather
provider pattern. It uses `CUBE_SPOTIFY_BACKEND_URL` when set, otherwise the
existing voice backend base URL; it reuses `CUBE_CLIENT_ID` and
`~/.config/cube/events.token`. The provider runs within the voice process.
Voice and audio continue working if display configuration, authentication,
backend data or the renderer is unavailable.

Three background coroutines poll state (5 s active, 15 s idle), fetch changed or
missing art, and publish local receiver observations (1 s). Network waits never
block the microphone or local publication. An old image response cannot replace
a new track. Prepared artwork is resent every 5 s while available to recover
from renderer restarts or dropped datagrams. State errors cannot renew a lease;
the screen expires within 15 s of its last trusted snapshot. The local receiver
removes availability immediately on the next observation after disconnect or
inactivity. Cloud metadata/transfer detection can lag until the next poll.

The existing `@cube-display` credential-checked datagram socket carries:

```text
music v1 <epoch> <sequence> <expires_ms> <status> <track_hash> <art_hash> <duration_ms> <progress_ms>
music-art v1 <epoch> <sequence> <track_hash> <art_hash>\n<12288 binary RGB24 bytes>
```

- Metadata ≤256 bytes; artwork datagram ≤12,544 bytes with exact pixel length.
- Status: 0 unavailable, 1 playing, 2 paused, 3 buffering. Unavailable uses zero
  hashes and expiry 0; unknown position/duration uses -1.
- Epoch is provider-start `monotonic_ns`; sequence increases per publication.
  `expires_ms` is a **Pi-local** monotonic deadline, derived conservatively from
  the backend remaining lease and request start. The native steady clock shares
  this Pi clock. Expired packets or leases over 15 s are rejected.
- Metadata from older epochs or non-increasing sequences is rejected. Artwork
  must match the current epoch, sequence, track, and artwork ID. Track/provider
  changes clear old frames. Repeated identical paused state does not re-arm a notice.
- Native reception retains UID checks, truncation checks, 64 messages per frame,
  and the old 128-byte limit for all existing message types. At most one artwork
  is accepted per frame. Rendering performs no decoding, network I/O, file I/O,
  or per-frame image allocation.

The provider only supplies display data. Soloist plays audio on the Pi.


## Display behavior

The music app is available only with fresh backend state for playback on Cube and a connected, logged-in, active local receiver. Starting/resuming/track changes show it for five seconds; repeated updates do not extend the notice. A fresh playing-to-paused edge shows a five-second notice before the interrupted carousel entry resumes. Buffering freezes progress. Missing/stale/transferred state removes availability. See [display architecture](DISPLAY_ARCHITECTURE.md) for pixel geometry, composition and carousel ownership.

## Validation

Automated tests cover OAuth/storage, API boundaries, resolver/controller, deterministic grammar, receipts, Pi reporting, gain recovery and native display protocol. Use [component test commands](../tests/README.md); no live credentials are needed. The isolated PipeWire test is opt-in and is separate from ordinary unit tests.

On the Pi, verify: phone playback reaches the intended speaker; voice ducks/restores music without changing assistant volume; music controls work with AI off; successful commands return to idle; cancellation does not replay a mutation; transfer away removes the display; stale provider/image packets cannot restore an old track; pause/resume notices and progress are correct; expired receiver builds fail safely. Perform restart/recovery tests only in an intentional deployment window. Automated passes do not certify these hardware/account checks.
