"""All hardware, speech subprocesses and HTTP calls are mocked."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import json
from pathlib import Path
import subprocess
import tempfile
from threading import Barrier
from unittest import TestCase
from unittest.mock import Mock, patch

from audio.playback import PlaybackLifecycle, PlaybackOutcome, PlaybackInterrupted
from tests.client.runtime_helpers import run_reply
from control.worker import Controls, valid_command
from control.hardware import Hardware, ControlFailure
from tests.paths import ROOT

SESSION = "a" * 32


class ControlTests(TestCase):
    def setUp(self):
        self.hardware = Mock(spec=Hardware)
        self.controls = Controls("http://cube.invalid", "pi", hardware=self.hardware)
        self.controls.session_id = SESSION
        self.receipt = self.controls.lifecycle.begin(SESSION)
        guard = patch("socket.socket.connect", side_effect=AssertionError("Real network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)
        self.power_guard = patch("control.hardware.subprocess.run", side_effect=AssertionError("Real OS calls forbidden"))
        self.power_guard.start()
        self.addCleanup(self.power_guard.stop)

    def command(self, operation, **arguments):
        return {"session_id": SESSION, "request_id": self.receipt.request_id,
                "command_id": "b" * 32, "operation": operation, **arguments}

    def acknowledge(self, *, played=True, operation="shutdown"):
        return run_reply({"_cube_playback": self.receipt, "success": True,
                                   "action": "cube_" + operation, "response": "Acknowledged."},
                                  lambda *args, **kwargs: played)

    def test_renderer_delegation(self):
        for operation, arguments in [("set_display", {"state": "off"}), ("set_display", {"state": "on"}),
                                     ("set_brightness", {"percent": 0}), ("set_brightness", {"percent": 100})]:
            with self.subTest(operation=operation, arguments=arguments):
                self.receipt = self.controls.lifecycle.begin(SESSION)
                self.assertEqual(self.controls.handle(self.command(operation, **arguments)), {"success": True})
                self.hardware.renderer.assert_called_with(operation, next(iter(arguments.values())))
                self.acknowledge()
                self.hardware.power.assert_not_called()

    def test_state_query(self):
        self.hardware.status.return_value = {"display_available": False, "uptime_seconds": 12.0}
        self.assertEqual(self.controls.handle(self.command("get_status")), {
            "success": True, "status": {"display_available": False, "uptime_seconds": 12.0}})

    def test_only_matching_success_releases_once(self):
        for operation in ("shutdown", "reboot"):
            with self.subTest(operation=operation):
                self.receipt = self.controls.lifecycle.begin(SESSION)
                self.hardware.reset_mock()
                command = self.command(operation)
                self.assertTrue(self.controls.handle(command)["success"])
                self.hardware.power.assert_not_called()
                self.acknowledge(operation=operation)
                self.hardware.power.assert_called_once_with(operation)
                self.acknowledge(operation=operation)
                self.assertFalse(self.controls.handle(command)["success"])
                self.hardware.power.assert_called_once()

    def test_failed_cancelled_interrupted_superseded_and_stale_never_release(self):
        for reason in ("failed", "cancelled", "interrupted", "superseded", "stale", "session_ended"):
            with self.subTest(reason=reason):
                self.receipt = self.controls.lifecycle.begin(SESSION)
                self.controls.handle(self.command("shutdown"))
                if reason == "failed":
                    self.acknowledge(played=False)
                elif reason == "superseded":
                    self.controls.lifecycle.begin(SESSION)
                elif reason == "stale":
                    self.receipt.deadline = 0
                elif reason == "session_ended":
                    self.controls._disconnect()
                else:
                    self.receipt.finish(PlaybackOutcome.CANCELLED, reason)
                self.acknowledge()
                self.hardware.power.assert_not_called()

    def test_unrelated_and_unsuccessful_reply_discard_power(self):
        for result in ({"success": True, "action": "lights_power", "response": "Done."},
                       {"success": False, "action": "cube_shutdown", "response": "Failed."},
                       {"success": True, "action": "cube_shutdown", "response": None}):
            self.receipt = self.controls.lifecycle.begin(SESSION)
            self.controls.handle(self.command("shutdown"))
            run_reply({"_cube_playback": self.receipt, **result}, lambda *a, **kw: True)
            self.acknowledge()
        self.hardware.power.assert_not_called()

    def test_interrupt_signal_and_terminal_race(self):
        self.controls.handle(self.command("shutdown"))
        self.assertFalse(run_reply({"_cube_playback": self.receipt, "success": True,
                               "action": "cube_shutdown", "response": "Shutting down."},
                              Mock(side_effect=PlaybackInterrupted())))
        self.assertIs(self.receipt.outcome, PlaybackOutcome.CANCELLED)
        self.acknowledge()
        self.hardware.power.assert_not_called()
        for _ in range(10):
            self.receipt = self.controls.lifecycle.begin(SESSION)
            self.controls.handle(self.command("shutdown"))
            self.receipt.authorize({"success": True, "action": "cube_shutdown"})
            self.hardware.reset_mock()
            barrier = Barrier(2)
            def finish(outcome):
                barrier.wait()
                self.receipt.finish(outcome)
            with ThreadPoolExecutor() as pool:
                a = pool.submit(finish, PlaybackOutcome.COMPLETED)
                b = pool.submit(finish, PlaybackOutcome.CANCELLED)
                a.result(); b.result()
            self.assertEqual(self.hardware.power.call_count,
                             int(self.receipt.outcome is PlaybackOutcome.COMPLETED))

    def test_strict_commands_cannot_be_shell_or_other_actions(self):
        for changes in ({"operation": "reboot; touch /tmp/private"}, {"operation": ["shutdown"]},
                        {"operation": "shutdown", "state": "off"}, {"command_id": "../shutdown"},
                        {"operation": "set_brightness", "percent": True},
                        {"operation": "set_brightness", "percent": 101},
                        {"operation": "set_display", "state": "shutdown"}):
            command = {**self.command("shutdown"), **changes}
            self.assertFalse(valid_command(command))
            self.assertFalse(self.controls.handle(command)["success"])
        self.hardware.power.assert_not_called()
        self.hardware.check_power.assert_not_called()
        self.hardware.renderer.assert_not_called()

    def test_duplicate_and_old_session_before_playback(self):
        command = self.command("shutdown")
        self.assertTrue(self.controls.handle(command)["success"])
        self.assertFalse(self.controls.handle(command)["success"])
        self.controls.close()
        self.acknowledge()
        self.assertFalse(self.controls.handle(command)["success"])
        self.hardware.power.assert_not_called()

    def test_safe_hardware_failure_and_no_retry(self):
        self.hardware.check_power.side_effect = ControlFailure("power_unavailable")
        self.assertEqual(self.controls.handle(self.command("shutdown")), {"success": False, "error": "power_unavailable"})
        self.acknowledge()
        self.hardware.power.assert_not_called()
        self.hardware.check_power.side_effect = None
        self.receipt = self.controls.lifecycle.begin(SESSION)
        self.hardware.power.side_effect = ControlFailure("power_unavailable")
        self.controls.handle(self.command("reboot"))
        self.acknowledge(operation="reboot")
        self.acknowledge(operation="reboot")
        self.hardware.power.assert_called_once()
        self.assertTrue(self.controls.take_power_failure())




    def test_overlapping_duplicate_playback_discards_pending_power(self):
        self.controls.handle(self.command("shutdown"))
        self.receipt.authorize({"success": True, "action": "cube_shutdown"})
        self.acknowledge()
        self.receipt.finish(PlaybackOutcome.COMPLETED)
        self.assertIs(self.receipt.outcome, PlaybackOutcome.CANCELLED)
        self.hardware.power.assert_not_called()

    def test_lost_result_disconnects_and_never_retries_power(self):
        import requests
        self.controls._disconnect()
        def post(url, **kwargs):
            if url.endswith("/session"):
                response = Mock(status_code=200)
                response.json.return_value = {"registered": True}
                return response
            self.controls.stopped.set()
            raise requests.ConnectionError("mock lost result")
        def get(*args, **kwargs):
            self.receipt = self.controls.lifecycle.begin(self.controls.session_id)
            response = Mock(status_code=200)
            response.json.return_value = {**self.command("shutdown"), "session_id": self.controls.session_id}
            return response
        with patch("control.worker.requests.Session") as factory, \
             patch.object(self.controls, "_headers", return_value={}):
            http = factory.return_value.__enter__.return_value
            http.post.side_effect = post
            http.get.side_effect = get
            self.controls._run()
            self.assertEqual(http.get.call_count, 1)
            self.assertEqual(http.post.call_count, 2)
        self.acknowledge()
        self.hardware.check_power.assert_called_once_with("shutdown")
        self.hardware.power.assert_not_called()
        self.assertIs(self.receipt.outcome, PlaybackOutcome.CANCELLED)


class HardwareTests(TestCase):
    def test_fixed_argv_only_and_denied_permission(self):
        hardware = Hardware()
        with patch("control.hardware.subprocess.run") as run:
            run.return_value.stdout = 's "yes"'
            for operation, command in (("shutdown", "poweroff"), ("reboot", "reboot")):
                hardware.check_power(operation)
                hardware.power(operation)
                self.assertEqual(run.call_args.args[0], ["/usr/bin/systemctl", "--no-ask-password", "--no-wall", "--check-inhibitors=yes", command])
                self.assertNotIn("shell", run.call_args.kwargs)
            run.reset_mock()
            for operation in ("poweroff", "shutdown now", "/usr/bin/reboot"):
                with self.assertRaises(ControlFailure):
                    hardware.power(operation)
            run.assert_not_called()
            run.return_value.stdout = 's "challenge"'
            with self.assertRaises(ControlFailure):
                hardware.check_power("shutdown")
            run.side_effect = subprocess.TimeoutExpired("PRIVATE", 1)
            with self.assertRaises(ControlFailure) as error:
                hardware.power("shutdown")
            self.assertNotIn("PRIVATE", str(error.exception))

    def test_renderer_protocol_and_unavailable_status(self):
        hardware = Hardware()
        with patch("display.transport.socket.socket") as factory:
            channel = factory.return_value.__enter__.return_value
            data = {"display_enabled": True, "master_brightness_percent": 30}
            channel.recv.return_value = json.dumps(data).encode()
            self.assertEqual(hardware.renderer("set_brightness", 30), data)
            channel.send.assert_called_once_with(b"control set_brightness 30")
            for malformed in (b"bad", b"{}", b'{"display_enabled":1,"master_brightness_percent":30}'):
                channel.recv.return_value = malformed
                with self.assertRaises(ControlFailure):
                    hardware.renderer("get_status")
            for field in ("master_brightness_percent",):
                for invalid in (True, -1, 101, "30", None):
                    channel.recv.return_value = json.dumps({**data, field: invalid}).encode()
                    with self.subTest(field=field, invalid=invalid), self.assertRaises(ControlFailure):
                        hardware.renderer("get_status")
        with patch.object(hardware, "renderer", side_effect=ControlFailure("display_unavailable")), \
             patch("pathlib.Path.read_text", side_effect=["100.5 20.0", "45000"]):
            self.assertEqual(hardware.status(), {"display_available": False, "uptime_seconds": 100.5, "cpu_temperature_c": 45.0})

    def test_content_commands_remain_outside_production_channel(self):
        for operation in ("show_text", "show_icon", "clear_content", "get_content_status"):
            with self.subTest(operation=operation), self.assertRaises(ControlFailure):
                Hardware().renderer(operation)
            self.assertFalse(valid_command({"session_id": SESSION, "request_id": "a" * 32,
                                            "command_id": "b" * 32, "operation": operation}))

    def test_native_display_state_without_hardware(self):
        source = r'''
#include "display-control.h"
#include <cassert>
int main() {
  DisplayControl d;
  assert(d.visible());
  assert(d.master_brightness_percent == 10);
  assert(!d.Apply("control set_animation off"));
  assert(d.Apply("control set_display off"));
  assert(!d.display_enabled && !d.visible());
  assert(d.Apply("control set_display on") && d.visible());
  assert(d.Apply("control set_brightness 0") && !d.visible());
  assert(d.display_enabled);
  assert(d.Apply("control set_brightness 30") && d.visible());
  assert(d.master_brightness_percent == 30);
  assert(!d.Apply("control set_brightness 101"));
  assert(!d.Apply("control set_brightness -1"));
  assert(!d.Apply("control set_brightness 30;reboot"));
  assert(!d.Apply("control set_brightness 030"));
  assert(!d.Apply("control shutdown"));
  assert(!d.Apply("control set_display false"));
  assert(d.Apply("control get_status"));
  assert(d.Status().find("\"master_brightness_percent\":30") != std::string::npos);
  for (int master : {0, 25, 50, 100}) {
    assert(d.Apply("control set_brightness " + std::to_string(master)));
    assert(d.master_brightness_percent == master);
    assert(d.visible() == (master > 0));
  }
  d.display_enabled = false;
  assert(!d.visible() && d.master_brightness_percent == 100);
  DisplayControl restarted;
  assert(restarted.master_brightness_percent == 10);
}
'''
        with tempfile.TemporaryDirectory() as directory:
            cpp = Path(directory) / "test.cc"
            binary = Path(directory) / "test"
            cpp.write_text(source)
            subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-I", str(ROOT / "client/native/cube-display"), str(cpp), "-o", str(binary)], check=True, capture_output=True)
            subprocess.run([str(binary)], check=True, capture_output=True)


class NativeRendererTests(TestCase):
    def test_real_renderer_compiles_and_mode_updates_preserve_controls(self):
        # Compile the production translation unit against an inert matrix facade.
        # The real renderer main/socket/hardware initialization is never executed.
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
        source = r'''
#define main UncalledRendererMain
#include "cube-display.cc"
#undef main
#include <cassert>
int main() {
  PresentationControl c;
  c.display.Apply("control set_display off");
  c.display.Apply("control set_brightness 30");
  for (const char* mode : {"wake", "listening", "thinking", "speech 0.35", "followup", "idle"}) {
    ApplyControlMessage(mode, &c);
    assert(!c.display.display_enabled);
    assert(c.display.master_brightness_percent == 30);
  }
}
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "led-matrix.h").write_text(header)
            (root / "test.cc").write_text(source)
            subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-I", directory,
                            "-I", str(ROOT / "client/native/cube-display"), str(root / "test.cc"),
                            str(ROOT / "client/native/cube-display/content-mask.cc"),
                            str(ROOT / "client/native/cube-display/content-renderer.cc"),
                            str(ROOT / "client/native/cube-display/dissolve-transition.cc"),
                            str(ROOT / "client/native/cube-display/apps/tetris-clock.cc"),
                            str(ROOT / "client/native/cube-display/apps/weather.cc"),
                            str(ROOT / "client/native/cube-display/animation-asset.cc"),
                            str(ROOT / "client/native/cube-display/apps/animation.cc"),
                            str(ROOT / "client/native/cube-display/apps/music.cc"),
                            "-o", str(root / "test")], check=True, capture_output=True)
            subprocess.run([str(root / "test")], check=True, capture_output=True)
