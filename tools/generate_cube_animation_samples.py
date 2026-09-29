#!/usr/bin/env python3
"""Create four original pixel GIFs for the Cube animation catalog."""

import argparse
from pathlib import Path

from PIL import Image, ImageDraw

from convert_cube_animation import convert_gif


BLACK = (0, 0, 0)
WHITE = (245, 247, 255)


def blank():
    image = Image.new("RGB", (32, 32), BLACK)
    return image, ImageDraw.Draw(image)


def eyes(index):
    image, draw = blank()
    draw.rounded_rectangle((3, 8, 28, 25), radius=8, fill=(75, 36, 139))
    draw.rounded_rectangle((5, 10, 14, 21), radius=4, fill=WHITE)
    draw.rounded_rectangle((18, 10, 27, 21), radius=4, fill=WHITE)
    if index in (7, 8):
        draw.rectangle((5, 10, 14, 19), fill=(75, 36, 139))
        draw.rectangle((18, 10, 27, 19), fill=(75, 36, 139))
        draw.line((6, 20, 13, 20), fill=(190, 137, 240), width=2)
        draw.line((19, 20, 26, 20), fill=(190, 137, 240), width=2)
    else:
        shift = [-2, -2, -1, 0, 1, 2, 2, 0, 0, -1, -2, -2][index]
        draw.ellipse((8 + shift, 14, 11 + shift, 18), fill=(20, 20, 45))
        draw.ellipse((21 + shift, 14, 24 + shift, 18), fill=(20, 20, 45))
    draw.arc((13, 20, 19, 25), 0, 180, fill=(255, 166, 205), width=2)
    return image


def ghost(index):
    image, draw = blank()
    bob = [0, 0, -1, -2, -2, -1, 0, 1, 1, 0, -1, -1, 0, 1, 1, 0][index]
    left = 7 + (1 if index >= 8 else 0)
    draw.ellipse((left, 6 + bob, left + 18, 24 + bob), fill=(224, 241, 255))
    draw.rectangle((left, 15 + bob, left + 18, 24 + bob), fill=(224, 241, 255))
    for x in (left + 2, left + 8, left + 14):
        draw.ellipse((x, 22 + bob, x + 5, 27 + bob), fill=(224, 241, 255))
    draw.ellipse((left + 4, 15 + bob, left + 7, 19 + bob), fill=(36, 38, 87))
    draw.ellipse((left + 12, 15 + bob, left + 15, 19 + bob), fill=(36, 38, 87))
    draw.ellipse((left + 8, 20 + bob, left + 11, 23 + bob), fill=(110, 94, 158))
    draw.point((left + 2, 21 + bob), fill=(255, 128, 156))
    draw.point((left + 17, 21 + bob), fill=(255, 128, 156))
    draw.point((3 + index % 3, 10), fill=(109, 208, 255))
    return image


def heart(index):
    image, draw = blank()
    pulse = [0, 0, 1, 2, 2, 1, 0, 0, 1, 2, 2, 1][index]
    cx, cy = 16, 17
    red = (255, 65, 112)
    draw.ellipse((cx - 10 - pulse, cy - 9 - pulse, cx, cy + 1), fill=red)
    draw.ellipse((cx, cy - 9 - pulse, cx + 10 + pulse, cy + 1), fill=red)
    draw.polygon([(cx - 10 - pulse, cy - 2), (cx + 10 + pulse, cy - 2),
                  (cx, cy + 12 + pulse)], fill=red)
    draw.line((cx - 7, cy - 6, cx - 4, cy - 8), fill=(255, 177, 198), width=2)
    if pulse == 2:
        for x, y in ((2, 9), (29, 9), (5, 26), (27, 26)):
            draw.line((x - 1, y, x + 1, y), fill=(255, 224, 99))
            draw.line((x, y - 1, x, y + 1), fill=(255, 224, 99))
    return image


def rocket(index):
    image, draw = blank()
    lift = [0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 10, 8, 5, 2][index]
    x, y = 13, 12 - lift
    draw.point((4, 5), fill=(100, 159, 240))
    draw.point((27, 8), fill=(255, 226, 109))
    draw.point((7, 18), fill=(100, 159, 240))
    draw.polygon([(x + 3, y - 5), (x + 7, y), (x + 7, y + 10),
                  (x, y + 10), (x, y)], fill=(220, 236, 251))
    draw.rectangle((x - 2, y + 6, x + 1, y + 11), fill=(94, 176, 240))
    draw.rectangle((x + 6, y + 6, x + 9, y + 11), fill=(94, 176, 240))
    draw.rectangle((x + 2, y + 2, x + 5, y + 5), fill=(55, 122, 204))
    draw.rectangle((x + 2, y + 8, x + 5, y + 9), fill=(255, 89, 112))
    flame = 3 + index % 3
    draw.polygon([(x + 1, y + 11), (x + 6, y + 11), (x + 3, y + 11 + flame)],
                 fill=(255, 151, 49))
    draw.point((x + 3, y + 12), fill=(255, 238, 111))
    return image


def generate(source_directory, asset_directory):
    source_directory, asset_directory = Path(source_directory), Path(asset_directory)
    source_directory.mkdir(parents=True, exist_ok=True)
    asset_directory.mkdir(parents=True, exist_ok=True)
    for name, count, duration, draw in (
            ("eyes", 12, 120, eyes), ("ghost", 16, 110, ghost),
            ("heart", 12, 120, heart), ("rocket", 16, 100, rocket)):
        frames = [draw(index) for index in range(count)]
        gif_path = source_directory / f"{name}.gif"
        frames[0].save(gif_path, save_all=True, append_images=frames[1:],
                       duration=[duration] * count, loop=0, optimize=False)
        convert_gif(gif_path, asset_directory / f"{name}.cubeanim")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_directory", type=Path)
    parser.add_argument("asset_directory", type=Path)
    args = parser.parse_args(argv)
    generate(args.source_directory, args.asset_directory)


if __name__ == "__main__":
    main()
