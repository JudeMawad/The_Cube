# Cube Display — Raspberry Pi 5 renderer

This C++ process drives the 64×64 HUB75 panel. See
[display architecture](../../../docs/DISPLAY_ARCHITECTURE.md) for the frame,
apps, overlays, composition and transitions.

## Candidate build

From the repository root, with the pinned matrix submodule checked out:

```sh
make -C client/native/cube-display -j2
```

The result is the ignored `client/native/cube-display/build/cube-display`.
Building never replaces or runs the installed executable referenced by
`cube-display.service`. `prepare-matrix.py` applies the RP1 PIO shutdown
patch to a generated copy of the pinned dependency. Do not run a second
renderer against the panel.

## Local socket protocol

The renderer listens on the Linux abstract datagram socket
`@cube-display`. It accepts only the Pi user named by `CUBE_DISPLAY_USER` or root,
rejects truncated datagrams, reads at most 64 messages per frame, and replies
only to bound `control` senders. Existing messages retain their 128-byte limit.
Versioned music metadata allows 256 bytes; prepared music artwork allows a
bounded header plus exactly 12,288 RGB24 bytes, with at most one image accepted
per frame. See [music protocol](../../../docs/SPOTIFY.md).
Assistant
states `idle`, `wake`, `listening`, `thinking`, `followup`, and `speech <0..1>`
are send only. Wake maps to the listening presentation marker; the Pi Coordinator tracks interaction state
and rejects stale updates.
The overlay uses one gray bottom-row line: it is absent at idle, reveals
center-out for listening, has a traveling pure-white highlight for thinking,
responds to speech level, and remains steady during follow-up.

Acknowledged commands are `control set_display on|off`,
`control set_brightness <0..100>`, `control get_status`,
`control show_text <value>`, `control show_icon check|warning|pause`,
`control clear_content`, and `control get_content_status`. The old
`set_animation` command was removed with the ambient renderer.

Display status is exactly `display_enabled` and
`master_brightness_percent`. Master brightness is one direct physical setting;
the configured `--led-brightness` initializes it at startup (50% in the
repository unit template). Zero clears pixels explicitly. Display off blanks
output without mutating the master value. A normal black app frame still
traverses the rendering path.

Text uses 1–5 code points from `0–9`, `A–Z`, `:`, `%`, `!`, and `°`; invalid
input is rejected. Masks and icons are hardware independent. Explicit content
is solid white on black and owns the complete frame while active. Volume uses
the same two-second Pi-side text window and refresh behavior. The
900 ms dissolve forms, morphs, and releases content with spatial delays and
interruption continuity.

The standard-library manual helper can inspect the protocol; it imports only
the canonical socket address from the client display transport:

```sh
python3 client/native/cube-display/renderer-control.py control get_status
python3 client/native/cube-display/renderer-control.py control show_text '50%'
python3 client/native/cube-display/renderer-control.py control clear_content
```

## Verification

```sh
PYTHONPATH=client/app client/app/.venv/bin/python -m unittest \
  tests.client.test_native_content tests.client.test_controls tests.client.test_volume_control -v
make -C client/native/cube-display -j2
```

These tests use an inert matrix facade and never initialize HUB75 hardware.
See [project status](../../../docs/PROJECT_STATUS.md) for testing limits;
quantitative panel timing and RP1 behavior require physical checks. No deployment or service restart is
part of the candidate build.
