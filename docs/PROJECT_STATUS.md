# Project status and validation

Cube includes voice interaction and barge-in, display apps, device controls, smart-home/media integrations and Spotify control and display. Running it requires your own hardware, accounts and configuration; follow [setup](SETUP.md).

The repository includes source, tests, configuration examples, deployment templates, original animation runtime files and a pinned matrix dependency. It does not include wake, speech or LLM weights, Soloist binaries, private configuration, recordings, logs, caches or runtime state. Animation sources and their attribution are described in [Pi assets](../client/assets/README.md) and [third-party notices](../THIRD_PARTY_NOTICES.md). The cat and alien previews have unresolved provenance noted there; their inclusion does not establish redistribution rights.

## Automated validation

The recorded test run used separate Python 3.13 CPU environments described in [tests](../tests/README.md). These are separate from production backend and AI environments. The counts below describe that run, not a fresh test of every checkout. Native unit tests compile an inert matrix facade and do not initialize HUB75 hardware.

| Suite | Tests run | Passed | Skipped | Failures / errors |
| --- | ---: | ---: | ---: | ---: |
| Client, including native tests | 209 | 207 | 2 | 0 / 0 |
| Backend | 577 | 577 | 0 | 0 / 0 |
| Mocked AI node | 61 | 61 | 0 | 0 / 0 |
| Integration | 25 | 25 | 0 | 0 / 0 |
| Total | 872 | 870 | 2 | 0 / 0 |

The two skipped client tests explicitly require an isolated PipeWire instance. They are opt-in, separate from normal unit tests. The recorded checks also covered the pinned native candidate build, generated voice catalog, local documentation targets, whitespace, privacy review and redacted credential scans. The native build reported warnings in upstream headers.

## What still needs hardware or live services

CPU tests do not certify Pi microphone/speaker behavior, ReSpeaker echo cancellation, Bluetooth, physical panel timing, GPU inference/cancellation, cloud devices or live Spotify/media accounts. Windows/WSL scripts need review/testing on their target host. Repository validation does not deploy or restart anything.

Before deploying, follow the checks in [hardware](HARDWARE.md), [voice](VOICE_BARGE_IN.md), [display](DISPLAY_ARCHITECTURE.md), and [Spotify](SPOTIFY.md), and review [network security](../SECURITY.md).

Adding model weights, other third-party assets or combined binaries requires a separate license and redistribution review. See the [third-party inventory](../THIRD_PARTY_NOTICES.md).
