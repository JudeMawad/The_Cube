"""Native content tests require only a C++17 compiler and Python's stdlib."""
import importlib.util
from pathlib import Path
import socket
import subprocess
import tempfile
from unittest import TestCase
from unittest.mock import patch

from tests.paths import ROOT

NATIVE = ROOT / "client/native/cube-display"
spec = importlib.util.spec_from_file_location("renderer_control_cli", NATIVE / "renderer-control.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class NativeContentTests(TestCase):
    def test_native_music(self):
        from display.music import metadata, artwork_packet
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "music-test"
            (root / "metadata").write_bytes(metadata(10, 1, 115000, 1, "a" * 64, "b" * 64, 100000, 25000))
            (root / "artwork").write_bytes(artwork_packet(10, 1, "a" * 64, "b" * 64, b"\xff\x00\x00" * 4096))
            # Match prepare_artwork's existing frame placement, with distinct
            # pixels to verify uniform scaling without cropping or a second offset.
            for name, width, height in (("square", 48, 48), ("landscape", 48, 24),
                                        ("portrait", 24, 48), ("odd", 48, 31),
                                        ("black", 48, 48)):
                pixels = bytearray(12288)
                left, top = (64 - width) // 2, (60 - height) // 2
                for y in range(height):
                    for x in range(width):
                        offset = ((top + y) * 64 + left + x) * 3
                        pixels[offset:offset + 3] = bytes((x + 1, y + 1, 200)) if name != "black" else bytes(3)
                (root / name).write_bytes(artwork_packet(10, 1, "a" * 64, "b" * 64, bytes(pixels)))
            result = subprocess.run(["g++", "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror",
                "-I", str(NATIVE), str(ROOT / "tests/client/native_music_test.cc"),
                str(NATIVE / "apps/music.cc"), "-o", str(binary)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([str(binary), directory], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_native_animation(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "animation-test"
            command = ["g++", "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror",
                       "-I", str(NATIVE), str(ROOT / "tests/client/native_animation_test.cc"),
                       str(NATIVE / "animation-asset.cc"), str(NATIVE / "apps/animation.cc"),
                       str(NATIVE / "content-mask.cc"), str(NATIVE / "content-renderer.cc"),
                       str(NATIVE / "dissolve-transition.cc"), "-o", str(binary)]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([str(binary), directory,
                                     str(ROOT / "client/assets/animations")],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Native animation assertions passed", result.stdout)

    def test_native_weather(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "weather-test"
            command = ["g++", "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror",
                       "-I", str(NATIVE), str(ROOT / "tests/client/native_weather_test.cc"),
                       str(NATIVE / "apps/weather.cc"), str(NATIVE / "apps/tetris-clock.cc"),
                       str(NATIVE / "content-mask.cc"), str(NATIVE / "content-renderer.cc"),
                       str(NATIVE / "dissolve-transition.cc"), "-o", str(binary)]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Native weather assertions passed", result.stdout)

    def test_native_tetris_clock(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "tetris-clock-test"
            result = subprocess.run([
                "g++", "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror",
                "-I", str(NATIVE), str(ROOT / "tests/client/native_tetris_clock_test.cc"),
                str(NATIVE / "apps/tetris-clock.cc"), str(NATIVE / "apps/weather.cc"),
                str(NATIVE / "content-mask.cc"),
                str(NATIVE / "content-renderer.cc"), str(NATIVE / "dissolve-transition.cc"),
                "-o", str(binary)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Native Tetris clock assertions passed", result.stdout)

    def test_clock_hardware_boundary(self):
        # Audit production native code only; matrix facade definitions belong to tests.
        owners = []
        for path in NATIVE.rglob("*"):
            if "build" in path.relative_to(NATIVE).parts or path.suffix not in {".h", ".cc"}:
                continue
            source = path.read_text()
            if "led-matrix.h" in source or "RGBMatrix" in source:
                owners.append(path.relative_to(NATIVE).as_posix())
        self.assertEqual(owners, ["cube-display.cc"])
        for path in (NATIVE / "apps").iterdir():
            source = path.read_text()
            for forbidden in ("RGBMatrix", "GPIO", "socket(", "http", "std::thread",
                              "sleep(", "brightness", "Coordinator"):
                self.assertNotIn(forbidden, source)

    def test_native_carousel(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "carousel-test"
            subprocess.run(["g++", "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror",
                            "-I", str(NATIVE), str(ROOT / "tests/client/native_carousel_test.cc"),
                            str(NATIVE / "content-mask.cc"), str(NATIVE / "content-renderer.cc"),
                            str(NATIVE / "dissolve-transition.cc"),
                            "-o", str(binary)], check=True, capture_output=True, text=True)
            result = subprocess.run([str(binary)], check=True, capture_output=True, text=True)
            self.assertIn("Native carousel assertions passed", result.stdout)

    def test_native_behavior(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "content-test"
            subprocess.run(["g++", "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror",
                            "-I", str(NATIVE), str(ROOT / "tests/client/native_content_test.cc"),
                            str(NATIVE / "content-mask.cc"), str(NATIVE / "content-renderer.cc"),
                            str(NATIVE / "dissolve-transition.cc"),
                            "-o", str(binary)], check=True, capture_output=True, text=True)
            result = subprocess.run([str(binary)], check=True, capture_output=True, text=True)
            self.assertIn("Native content assertions passed", result.stdout)


    def test_production_renderer_without_hardware(self):
        # Match the facade already used by test_controls.py. Production main
        # remains uncalled; recvmsg/sendto are injected by the C++ harness.
        header = r'''#pragma once
#include <cstdint>
namespace rgb_matrix {
class FrameCanvas {
public:
  int width() { return 64; }
  int height() { return 64; }
  void SetPixel(int, int, uint8_t, uint8_t, uint8_t) {}
  void Clear() {}
};
class RGBMatrix {
public:
  struct Options {
    const char* hardware_mapping;
    int rows, cols, chain_length, parallel, brightness;
  };
  static RGBMatrix* CreateFromFlags(int*, char***, Options*) { return nullptr; }
  FrameCanvas* CreateFrameCanvas() { return nullptr; }
  void SetBrightness(uint8_t) {}
  FrameCanvas* SwapOnVSync(FrameCanvas* frame) { return frame; }
  void Clear() {}
};
}
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "led-matrix.h").write_text(header)
            command = ["g++", "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror",
                       "-I", directory, "-I", str(NATIVE),
                       str(ROOT / "tests/client/native_renderer_test.cc"),
                       str(NATIVE / "content-mask.cc"), str(NATIVE / "content-renderer.cc"),
                       str(NATIVE / "dissolve-transition.cc"),
                       str(NATIVE / "apps/tetris-clock.cc"), str(NATIVE / "apps/weather.cc"),
                       str(NATIVE / "animation-asset.cc"), str(NATIVE / "apps/animation.cc"),
                       "-o", str(root / "renderer-test")]
            command.insert(-2, str(NATIVE / "apps/music.cc"))
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([str(root / "renderer-test")], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Production renderer regression assertions passed", result.stdout)


class ManualCliTests(TestCase):
    def test_uses_canonical_client_socket_address(self):
        from display.transport import ADDRESS
        self.assertEqual(cli.ADDRESS, ADDRESS)

    def test_demo(self):
        with patch.object(cli, "send_command") as send, patch.object(cli.time, "sleep") as sleep:
            self.assertEqual(cli.main(["demo", "dissolve"]), 0)
            self.assertEqual([call.args[0] for call in send.call_args_list], [
                ["control", "clear_content"], ["control", "show_text", "11:37"],
                ["control", "clear_content"]])
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 2, 2])

    def test_removed_styles_are_rejected(self):
        for style in ("pixels", "smooth", "dissolve"):
            with self.assertRaises(ValueError):
                cli.command_message(["control", "set_transition_style", style])
        for args in (["demo"], ["demo", "pixels"], ["demo", "smooth"], ["demo", "invalid"]):
            with patch.object(cli, "send_command") as send, patch.object(cli.sys, "stderr"):
                self.assertEqual(cli.main(args), 1)
                send.assert_not_called()

    def test_unicode_and_bound_reply(self):
        with patch.object(cli.socket, "socket") as factory:
            channel = factory.return_value.__enter__.return_value
            channel.recvmsg.return_value = (b'{"accepted":true}', [], 0, None)
            self.assertEqual(cli.send_command(["control", "show_text", "23°"]), {"accepted": True})
            channel.send.assert_called_once_with("control show_text 23°".encode("utf-8"))
            channel.settimeout.assert_called_once_with(1)
            channel.connect.assert_called_once_with("\0cube-display")
            self.assertTrue(channel.bind.call_args.args[0].startswith("\0cube-manual-"))

    def test_errors_and_no_retry(self):
        for payload, flags in [(b"{}", 0), (b"bad", 0), (b'{"accepted":1}', 0),
                               (b'{"accepted":true}', socket.MSG_TRUNC)]:
            with patch.object(cli.socket, "socket") as factory:
                channel = factory.return_value.__enter__.return_value
                channel.recvmsg.return_value = (payload, [], flags, None)
                with self.assertRaises(ValueError):
                    cli.send_command(["control", "clear_content"])
                channel.send.assert_called_once()
        with patch.object(cli.socket, "socket") as factory:
            channel = factory.return_value.__enter__.return_value
            channel.recvmsg.side_effect = TimeoutError()
            with self.assertRaises(TimeoutError):
                cli.send_command(["control", "get_status"])
            channel.send.assert_called_once()

    def test_assistant_messages_are_send_only(self):
        with patch.object(cli.socket, "socket") as factory:
            channel = factory.return_value.__enter__.return_value
            self.assertIsNone(cli.send_command(["speech", "0.35"]))
            channel.send.assert_called_once_with(b"speech 0.35")
            channel.recvmsg.assert_not_called()
        for tokens in [["speaking"], ["speech", "nan"], ["speech", "2"],
                       ["control", "reboot"], ["control", "set_brightness", "050"],
                       ["control", "set_display", "toggle"],
                       ["control", "show_text", "x" * 129]]:
            with self.assertRaises(ValueError):
                cli.command_message(tokens)

    def test_content_and_display_status_schemas(self):
        status = dict(display_enabled=True, master_brightness_percent=50)
        self.assertTrue(cli.valid_reply(status, "status"))
        self.assertFalse(cli.valid_reply({**status, "presentation": "full"}, "status"))
        content = dict(available=True, presentation="forming_content", target_pixels=400,
                       progress=0.5, pending=False)
        self.assertTrue(cli.valid_reply(content, "content_status"))
        self.assertTrue(cli.valid_reply({"available": False}, "content_status"))
        for replacement in ({"progress": True}, {"progress": float("nan")},
                            {"target_pixels": 4097}, {"presentation": []}):
            self.assertFalse(cli.valid_reply({**content, **replacement}, "content_status"))
