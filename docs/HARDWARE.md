# Hardware overview

Cube combines a Raspberry Pi 5, 64×64 HUB75 RGB matrix, ReSpeaker microphone/audio hardware and speaker in a custom enclosure with an acoustic chamber. The project's author designed and built the physical device and software.

This repository provides software and hardware-facing configuration. It does not include enclosure CAD, dimensioned fabrication drawings, a complete bill of materials or a verified universal wiring guide. Device photography/video and mechanical design files can be added separately when available for publication.

## Ownership and interfaces

| Component | Software boundary | Commissioning requirement |
| --- | --- | --- |
| ReSpeaker microphone | One Pi-owned ALSA capture stream, 16 kHz mono PCM | Identify the actual card/channel and processed capture output; do not assume device index zero |
| Speaker | User PipeWire/WirePlumber session | Select the physical sink and verify ReSpeaker and Bluetooth routes independently |
| 64×64 matrix | Sole C++ renderer using the pinned RGB matrix dependency and RP1 PIO patch | Review panel wiring, geometry, rotation, power and brightness for the actual build |
| Enclosure/acoustic chamber | Physical design around microphone and speaker | Evaluate speaker leakage, user/speaker double-talk and wake performance in the assembled device |
| Pi power controls | Authenticated backend control session and Pi-local polkit rule | Supervise reboot/shutdown checks; canceled speech must not release a power action |

The repository templates retain the reference renderer geometry/rotation and audio defaults. They are starting points to review, not automatic hardware discovery or an assurance that another panel/microphone is wired identically. Use the matrix dependency's [wiring documentation](../client/third_party/rpi-rgb-led-matrix/wiring.md) after initializing the submodule, and the hardware vendor's documentation for your exact revisions.

## Physical verification

Run [hardware-free tests](../tests/README.md) first. Build only a renderer candidate; do not start a second renderer against an active panel. During deliberate commissioning, verify microphone ownership, selected speaker, assistant/music volume separation, duck/restore, display blanking/brightness and stale-state expiry.

Barge-in support does not by itself prove acoustic echo cancellation. Test speaker-only output and human/speaker double-talk at representative distances and volumes. An enabled DSP flag or Bluetooth connection is insufficient evidence of a working echo reference. [Voice validation](VOICE_BARGE_IN.md) includes ReSpeaker and upstream cancellation checks. No physical performance measurements are claimed by the CPU test results.
