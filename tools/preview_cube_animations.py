#!/usr/bin/env python3
"""Render contact sheet and GIF previews from native .cubeanim assets."""

import argparse
from pathlib import Path

from PIL import Image, ImageDraw

from convert_cube_animation import read_cubeanim


def preview(directory, output):
    directory, output = Path(directory), Path(output)
    assets = sorted(directory.glob("*.cubeanim"))
    if not assets:
        raise ValueError("no .cubeanim assets found")
    output.mkdir(parents=True, exist_ok=True)
    cell = 64 * 4
    sheet = Image.new("RGB", (cell * 4, (cell + 24) * len(assets)), "#171717")
    draw = ImageDraw.Draw(sheet)
    for row, path in enumerate(assets):
        frames = read_cubeanim(path)
        durations = [duration for duration, _ in frames]
        images = [image for _, image in frames]
        images[0].save(output / f"{path.stem}.gif", save_all=True,
                       append_images=images[1:], duration=durations, loop=0,
                       optimize=False)
        picks = [images[index * (len(images) - 1) // 3] for index in range(4)]
        for column, image in enumerate(picks):
            sheet.paste(image.resize((cell, cell), Image.Resampling.NEAREST),
                        (column * cell, row * (cell + 24)))
        draw.text((4, row * (cell + 24) + cell + 4), path.stem, fill="white")
    sheet.save(output / "contact-sheet.png")
    return len(assets)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    try:
        count = preview(args.directory, args.output)
    except ValueError as error:
        parser.error(str(error))
    print(f"Previewed {count} animations in {args.output}")


if __name__ == "__main__":
    main()
