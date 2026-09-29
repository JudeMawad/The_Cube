# Pi assets

Voice operation requires a [user-provided wake model](models/README.md), openWakeWord supporting assets and a [Piper voice with matching configuration](voices/README.md). Model weights, training outputs and voice JSON are excluded from this public repository. See [setup](../../docs/SETUP.md) for configuration. Hardware-free tests need none of these assets.

`animations/source/` contains the cat and alien GIF previews from GIPHY; see [third-party notices](../../THIRD_PARTY_NOTICES.md) for their attribution and provenance status. These GIFs are not covered by Cube’s MIT license.

The original eyes, ghost, heart and rocket `.cubeanim` runtime files remain included under MIT, so the Pi does not need Pillow for playback. Their GIF inputs can be recreated with `tools/generate_cube_animation_samples.py`. PAC-MAN artwork remains excluded. See [display architecture](../../docs/DISPLAY_ARCHITECTURE.md) for conversion and runtime constraints.
