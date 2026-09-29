# Pi assets

Voice operation requires a [user-provided wake model](models/README.md), openWakeWord supporting assets and a [Piper voice with matching configuration](voices/README.md). Model weights, training outputs and voice JSON are excluded from this public repository. See [setup](../../docs/SETUP.md) for configuration. Hardware-free tests need none of these assets.

`animations/source/` contains only `cat.gif` and `Alien.gif`, identified by the project author as GIPHY content. The renderer plays the matching `cat.cubeanim` and `alien.cubeanim` files in `animations/`; it does not read GIFs. Both runtime files are converted with `tools/convert_cube_animation.py`, so playback needs no Pillow installation. Restart the renderer after replacing animation files.

The GIFs and their converted runtime files are third-party artwork outside Cube’s MIT license. Exact source pages, creator credits and redistribution permission remain unverified; see [third-party notices](../../THIRD_PARTY_NOTICES.md). PAC-MAN artwork is not included. See [display architecture](../../docs/DISPLAY_ARCHITECTURE.md) for conversion and runtime limits.
