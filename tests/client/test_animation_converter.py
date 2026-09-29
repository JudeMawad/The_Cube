"""Hardware-free tests for offline GIF conversion and native asset bytes."""

from pathlib import Path
import tempfile
import unittest

try:
    from PIL import Image
    from tools.convert_cube_animation import (
        FRAME_BYTES, HEADER, MAX_BYTES, convert_gif, read_cubeanim,
    )
except ModuleNotFoundError as error:
    if error.name != "PIL":
        raise
    Image = None


@unittest.skipUnless(Image is not None, "Pillow is an offline development dependency")
class AnimationConverterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.gif"
        self.output = self.root / "output.cubeanim"

    def save(self, frames, durations, **options):
        frames[0].save(self.source, save_all=True, append_images=frames[1:],
                       duration=durations, loop=0, optimize=False, **options)

    def converted(self):
        convert_gif(self.source, self.output)
        return read_cubeanim(self.output)

    def test_single_frame_contain_letterbox_nearest_and_rgb565(self):
        image = Image.new("RGB", (100, 50), (0, 0, 0))
        for y in range(50):
            for x in range(100):
                image.putpixel((x, y), (250, 0, 0) if x < 50 else (0, 252, 0))
        self.save([image], [120])
        frames = self.converted()
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0][0], 120)
        frame = frames[0][1]
        self.assertEqual(frame.getpixel((0, 0)), (0, 0, 0))
        self.assertEqual(frame.getpixel((0, 15)), (0, 0, 0))
        self.assertEqual(frame.getpixel((0, 16)), (255, 0, 0))
        self.assertEqual(frame.getpixel((31, 31)), (255, 0, 0))
        self.assertEqual(frame.getpixel((32, 31)), (0, 255, 0))
        self.assertEqual(frame.getpixel((0, 48)), (0, 0, 0))
        self.assertEqual(len(self.output.read_bytes()), HEADER.size + FRAME_BYTES)

    def test_multiframe_durations_clamping_and_determinism(self):
        images = [Image.new("RGB", (3, 2), color) for color in
                  ((255, 0, 0), (0, 255, 0), (0, 0, 255))]
        self.save(images, [10, 110, 5000])
        first = self.converted()
        self.assertEqual([duration for duration, _ in first], [20, 110, 2000])
        self.assertEqual([image.getpixel((31, 31)) for _, image in first],
                         [(255, 0, 0), (0, 255, 0), (0, 0, 255)])
        initial = self.output.read_bytes()
        self.assertLessEqual(len(initial), MAX_BYTES)
        convert_gif(self.source, self.output)
        self.assertEqual(self.output.read_bytes(), initial)

    def test_zero_duration_cannot_busy_loop(self):
        self.save([Image.new("RGB", (2, 2), (255, 0, 0))], [10])
        data = bytearray(self.source.read_bytes())
        extension = data.find(b"\x21\xf9\x04")
        self.assertGreaterEqual(extension, 0)
        data[extension + 4:extension + 6] = b"\x00\x00"
        self.source.write_bytes(data)
        self.assertEqual(self.converted()[0][0], 20)

    def test_transparency_and_disposal(self):
        palette = [0, 0, 0, 255, 0, 0, 0, 255, 0, 0, 0, 255] + [0] * (768 - 12)
        frames = []
        for x, color in ((0, 1), (1, 2), (2, 3)):
            frame = Image.new("P", (4, 4), 0)
            frame.putpalette(palette)
            frame.putpixel((x, 1), color)
            frames.append(frame)
        self.save(frames, [100, 100, 100], transparency=0, disposal=[1, 2, 1])
        converted = self.converted()
        self.assertEqual(converted[0][1].getpixel((8, 16)), (255, 0, 0))
        self.assertEqual(converted[0][1].getpixel((48, 16)), (0, 0, 0))
        self.assertEqual(converted[1][1].getpixel((8, 16)), (255, 0, 0))
        self.assertEqual(converted[1][1].getpixel((24, 16)), (0, 255, 0))
        self.assertEqual(converted[2][1].getpixel((8, 16)), (0, 0, 0))
        self.assertEqual(converted[2][1].getpixel((24, 16)), (0, 0, 0))
        self.assertEqual(converted[2][1].getpixel((40, 16)), (0, 0, 255))

        self.save(frames, [100, 100, 100], transparency=0, disposal=[1, 3, 1])
        restored = self.converted()
        self.assertEqual(restored[2][1].getpixel((24, 16)), (0, 0, 0))
        self.assertEqual(restored[2][1].getpixel((8, 16)), (255, 0, 0))

    def test_invalid_gif_and_excessive_frames(self):
        self.source.write_bytes(b"not a GIF")
        with self.assertRaises(ValueError):
            self.converted()
        self.assertFalse(self.output.exists())
        frames = [Image.new("RGB", (1, 1), (255 if i % 2 else 0, 0, 0))
                  for i in range(65)]
        self.save(frames, [100] * len(frames))
        with self.assertRaisesRegex(ValueError, "frame count"):
            self.converted()

    def test_total_duration_limit(self):
        frames = [Image.new("RGB", (1, 1), (255 if i % 2 else 0, 0, 0))
                  for i in range(31)]
        self.save(frames, [2000] * len(frames))
        with self.assertRaisesRegex(ValueError, "duration"):
            self.converted()


if __name__ == "__main__":
    unittest.main()
