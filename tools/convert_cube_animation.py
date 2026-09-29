#!/usr/bin/env python3
"""Convert an animated GIF offline into a bounded 64x64 Cube animation."""

import argparse
from pathlib import Path
import struct

from PIL import GifImagePlugin, Image, UnidentifiedImageError


MAGIC = b"CUBEANIM"
VERSION = 1
SIZE = 64
MAX_FRAMES = 64
MIN_FRAME_MS = 20
MAX_FRAME_MS = 2000
MAX_TOTAL_MS = 60_000
HEADER = struct.Struct("<8sHHHH")
FRAME_BYTES = 2 + SIZE * SIZE * 2
MAX_BYTES = HEADER.size + MAX_FRAMES * FRAME_BYTES


def rgb565(rgb):
    red, green, blue = rgb
    return ((red >> 3) << 11) | ((green >> 2) << 5) | (blue >> 3)


def rgb888(value):
    red, green, blue = (value >> 11) & 31, (value >> 5) & 63, value & 31
    return ((red << 3) | (red >> 2), (green << 2) | (green >> 4),
            (blue << 3) | (blue >> 2))


def contain(frame):
    width, height = frame.size
    longest = max(width, height)
    scaled = (max(1, width * SIZE // longest), max(1, height * SIZE // longest))
    rgba = frame.convert("RGBA")
    image = rgba.resize(scaled, Image.Resampling.NEAREST)
    canvas = Image.new("RGB", (SIZE, SIZE), (0, 0, 0))
    canvas.paste(image, ((SIZE - scaled[0]) // 2, (SIZE - scaled[1]) // 2),
                 image.getchannel("A"))
    return canvas


def convert_gif(source, destination):
    source, destination = Path(source), Path(destination)
    # Pillow's sequential seek maintains the GIF composition and disposal
    # state. Keep transparent first frames in RGBA through later seeks.
    GifImagePlugin.LOADING_STRATEGY = GifImagePlugin.LoadingStrategy.RGB_ALWAYS
    try:
        with Image.open(source) as gif:
            if gif.format != "GIF":
                raise ValueError("input is not a GIF")
            if not 1 <= gif.n_frames <= MAX_FRAMES:
                raise ValueError("GIF frame count must be 1..64")
            output = bytearray(HEADER.pack(MAGIC, VERSION, SIZE, SIZE, gif.n_frames))
            total_ms = 0
            for index in range(gif.n_frames):
                gif.seek(index)
                duration = gif.info.get("duration", 100)
                if not isinstance(duration, (int, float)):
                    raise ValueError("invalid GIF duration")
                duration = max(MIN_FRAME_MS, min(MAX_FRAME_MS, int(duration)))
                total_ms += duration
                if total_ms > MAX_TOTAL_MS:
                    raise ValueError("GIF duration exceeds 60 seconds")
                frame = contain(gif.copy())
                output.extend(struct.pack("<H", duration))
                pixels = frame.tobytes()
                for offset in range(0, len(pixels), 3):
                    output.extend(struct.pack("<H", rgb565(pixels[offset:offset + 3])))
    except (UnidentifiedImageError, OSError) as error:
        raise ValueError(f"cannot decode GIF: {error}") from error
    assert len(output) <= MAX_BYTES
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(output)
    return len(output)


def read_cubeanim(source):
    """Development-only reader used by tests and preview generation."""
    data = Path(source).read_bytes()
    if not HEADER.size + FRAME_BYTES <= len(data) <= MAX_BYTES:
        raise ValueError("invalid animation size")
    magic, version, width, height, count = HEADER.unpack_from(data)
    if (magic, version, width, height) != (MAGIC, VERSION, SIZE, SIZE):
        raise ValueError("invalid animation header")
    if not 1 <= count <= MAX_FRAMES or len(data) != HEADER.size + count * FRAME_BYTES:
        raise ValueError("invalid animation frame count or payload size")
    frames = []
    total_ms = 0
    for index in range(count):
        start = HEADER.size + index * FRAME_BYTES
        duration = struct.unpack_from("<H", data, start)[0]
        if not MIN_FRAME_MS <= duration <= MAX_FRAME_MS:
            raise ValueError("invalid animation frame duration")
        total_ms += duration
        if total_ms > MAX_TOTAL_MS:
            raise ValueError("animation duration exceeds limit")
        pixels = struct.iter_unpack("<H", data[start + 2:start + FRAME_BYTES])
        frame = Image.new("RGB", (SIZE, SIZE))
        frame.putdata([rgb888(value) for (value,) in pixels])
        frames.append((duration, frame))
    return frames


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="source GIF")
    parser.add_argument("output", type=Path, help="destination .cubeanim")
    args = parser.parse_args(argv)
    try:
        size = convert_gif(args.input, args.output)
    except ValueError as error:
        parser.error(str(error))
    print(f"Wrote {args.output} ({size} bytes)")


if __name__ == "__main__":
    main()
