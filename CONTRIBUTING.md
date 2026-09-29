# Contributing

Start with [architecture](docs/ARCHITECTURE.md) and [tests](tests/README.md). Keep changes small and describe the concrete problem, resulting behavior, validation and hardware limits.

Keep service environments separate and preserve the HTTP contracts. The backend executes tools and stores state; the Pi controls audio and hardware; the Coordinator manages voice interactions. Use existing feature adapters rather than adding a second route to the same device. Do not combine a refactor with new functionality without explaining how the changes relate.

Run the affected component tests, generated-catalog check and cross-service tests when contracts change. Tests must not require private infrastructure or load/download models implicitly. Native builds create a candidate; they must not start another panel renderer or replace installed binaries.

Configuration examples use placeholders. Never submit credentials, tokens, recordings, transcripts, personal playlists, machine addresses or unreviewed third-party art. Check [security](SECURITY.md) and [third-party notices](THIRD_PARTY_NOTICES.md) before adding assets. Record the source, license and modifications for bundled third-party files.
