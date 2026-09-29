# Security and privacy

Cube is intended for a trusted network. Backend voice/transcription/TTS and AI-node inference endpoints are not general internet-facing authenticated APIs. Control, media-event and music routes use the shared Cube token and client identity checks; this does not secure every endpoint. Do not expose these services directly to the internet.

Store credentials under the service user's `~/.config/cube/`, with directory mode 0700 and file mode 0600. Spotify token storage additionally validates ownership, bounds and symlinks. Protect private backups, runtime databases and Soloist sessions as credentials/personal data. Client IDs are public identifiers; access/refresh tokens, API secrets and receiver keys are secrets. Device IDs, coordinates, hostnames and personal paths may reveal private infrastructure even when they are not credentials.

Normal local-command logging omits transcripts and raw vendor errors. Explicit `CUBE_LOG_TRANSCRIPTS=1` diagnostics can contain personal speech. Conversation history lives in backend memory and selected transcripts are sent to the configured AI service; media state persists locally. Review logs and diagnostic output before sharing, including upstream exceptions and paths. Never attach raw token/config files, recordings or database dumps to an issue.

## Reporting

Use GitHub private vulnerability reporting if enabled on this repository. Otherwise contact the maintainer privately before disclosing exploit details; do not open a public issue containing credentials or sensitive infrastructure. Enabling the private reporting channel is a pre-public release gate. No response-time guarantee is offered for this personal project.

If a credential has entered Git, revoke/rotate it first. Deleting the current file does not remove history, forks, caches or clones. Coordinate any history replacement with collaborators and review all published refs before changing visibility.
