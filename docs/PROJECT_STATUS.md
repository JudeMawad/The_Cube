# Project status and validation

This repository presents Cube's finished source architecture: voice interaction and barge-in, native display apps, deterministic controls, smart-home/media integrations and Spotify control/display. Hardware and accounts still require deliberate provisioning.

## Source release scope

The public repository has fresh Git history and includes source, tests, configuration examples, deployment templates, original generated animation samples and a pinned matrix dependency. It excludes custom wake-model output, speech/LLM weights, Soloist binaries, third-party artwork with unresolved rights, private configuration, recordings, logs, caches and runtime state.

A normal clone can run the hardware-free tests without private assets. A real voice deployment needs a compatible [user-provided wake model](../client/assets/models/README.md), [Piper voice](../client/assets/voices/README.md), backend speech assets and hardware configuration. The AI node is optional. Follow [setup](SETUP.md).

## Automated validation

Release validation uses separate Python 3.13 CPU environments described in [tests](../tests/README.md). Production backend/AI environments remain separate. Native unit tests compile an inert matrix facade and do not initialize HUB75 hardware.

| Suite | Tests run | Passed | Skipped | Failures / errors |
| --- | ---: | ---: | ---: | ---: |
| Client, including native tests | 209 | 207 | 2 | 0 / 0 |
| Backend | 577 | 577 | 0 | 0 / 0 |
| Mocked AI node | 61 | 61 | 0 | 0 / 0 |
| Integration | 25 | 25 | 0 | 0 / 0 |
| Total | 872 | 870 | 2 | 0 / 0 |

The two skipped client tests explicitly require an isolated PipeWire instance. They are opt-in, separate from normal unit tests. Validation also covers the pinned native candidate build, generated voice catalog, local documentation targets, whitespace, privacy review and redacted credential scans. Build warnings in upstream headers are retained rather than suppressed.

## Limits and manual review

CPU tests do not certify Pi microphone/speaker behavior, ReSpeaker echo cancellation, Bluetooth, physical panel timing, GPU inference/cancellation, cloud devices or live Spotify/media accounts. Windows/WSL scripts need review/testing on their target host. Repository validation does not deploy or restart anything.

Before deploying, follow the supervised checks in [hardware](HARDWARE.md), [voice](VOICE_BARGE_IN.md), [display](DISPLAY_ARCHITECTURE.md), and [Spotify](SPOTIFY.md). Use a trusted network; inference endpoints are not a general internet-facing authenticated API.

Before publishing the source, review README rendering, any added device photo/video, the [third-party inventory](../THIRD_PARTY_NOTICES.md), GitHub description/topics and private vulnerability reporting. Do not attach private configuration or runtime logs. Adding excluded assets or distributing combined binaries requires its own review; neither is part of this source release.
