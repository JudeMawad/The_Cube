"""Volume commands use mocked PipeWire sinks; no physical audio is changed."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from audio import speaker_control as speaker
from control.hardware import Hardware, ControlFailure
from control.worker import Controls, valid_command

LOCAL = "cube.assistant"
PHYSICAL = "alsa_output.usb-reSpeaker.analog-stereo"
BT = "bluez_output.02_00_00_00_00_03.1"


class VolumeTests(unittest.TestCase):
    def setUp(self):
        self.selected = PHYSICAL
        self.sinks = {
            LOCAL: {"front-left": 40, "front-right": 40},
            BT: {"mono": 30},
            PHYSICAL: {"front-left": 100, "front-right": 100},
            "cube.spotify": {"front-left": 100, "front-right": 100},
        }
        self.muted = {name: False for name in self.sinks}
        self.calls = []
        runner = patch.object(speaker, "_run", side_effect=self.run_pactl)
        runner.start()
        self.addCleanup(runner.stop)

    def run_pactl(self, args, **kwargs):
        self.calls.append(args)
        if args == ["pactl", "get-default-sink"]:
            return SimpleNamespace(stdout=self.selected)
        if args == ["pactl", "--format=json", "list", "sinks"]:
            return SimpleNamespace(stdout=json.dumps([
                {"name": name, "mute": self.muted[name], "volume": {
                    channel: {"value_percent": f"{level}%"}
                    for channel, level in levels.items()}}
                for name, levels in self.sinks.items()] ))
        if args[:2] == ["pactl", "set-sink-volume"]:
            sink = args[2]
            self.sinks[sink] = dict(zip(self.sinks[sink], [int(x[:-1]) for x in args[3:]]))
            return SimpleNamespace(stdout="")
        if args[:2] == ["pactl", "set-sink-mute"]:
            self.muted[args[2]] = args[3] == "1"
            return SimpleNamespace(stdout="")
        raise AssertionError(args)

    def test_internal_relative_exact_and_live_status(self):
        self.assertEqual(speaker.control_volume("adjust_volume", delta=10)["channels"],
                         {"front-left": 50, "front-right": 50})
        self.sinks[LOCAL]["front-left"] = 43
        self.sinks[LOCAL]["front-right"] = 47
        self.assertEqual(speaker.control_volume("get_volume")["channels"],
                         {"front-left": 43, "front-right": 47})
        self.assertEqual(speaker.control_volume("adjust_volume", delta=5)["channels"],
                         {"front-left": 48, "front-right": 52})
        self.assertEqual(speaker.control_volume("set_volume", percent=0)["channels"],
                         {"front-left": 0, "front-right": 0})
        self.assertEqual(speaker.control_volume("set_volume", percent=100)["channels"],
                         {"front-left": 100, "front-right": 100})
        speaker.control_volume("adjust_volume", delta=10)
        self.assertEqual(self.sinks[LOCAL], {"front-left": 100, "front-right": 100})
        self.assertFalse(any("set-default-sink" in command for command in self.calls))

    def test_bluetooth_selected_and_mute_restores_its_level(self):
        self.selected = BT
        self.sinks[LOCAL] = {"mono": 30}
        self.assertEqual(speaker.control_volume("adjust_volume", delta=-5)["channels"], {"mono": 25})
        self.assertEqual(self.sinks[BT], {"mono": 30})
        muted = speaker.control_volume("mute")
        self.assertTrue(muted["muted"])
        self.assertEqual(muted["channels"], {"mono": 25})
        self.assertEqual(speaker.control_volume("get_volume")["muted"], True)
        restored = speaker.control_volume("unmute")
        self.assertFalse(restored["muted"])
        self.assertEqual(restored["channels"], {"mono": 25})
        speaker.control_volume("mute")
        changed = speaker.control_volume("set_volume", percent=35)
        self.assertFalse(changed["muted"])
        self.assertEqual(changed["channels"], {"mono": 35})
        self.assertEqual(self.muted[BT], False)

    def test_positive_volume_recovers_sound_while_silent_changes_stay_muted(self):
        speaker.control_volume("mute")
        self.assertTrue(speaker.control_volume("set_volume", percent=0)["muted"])
        self.assertTrue(speaker.control_volume("adjust_volume", delta=-5)["muted"])
        raised = speaker.control_volume("adjust_volume", delta=5)
        self.assertFalse(raised["muted"])
        self.assertEqual(raised["channels"], {"front-left": 5, "front-right": 5})
        speaker.control_volume("mute")
        exact = speaker.control_volume("set_volume", percent=40)
        self.assertFalse(exact["muted"])
        self.assertEqual(exact["channels"], {"front-left": 40, "front-right": 40})

    def test_unavailable_output_and_invalid_values_fail_closed(self):
        self.sinks.pop(LOCAL)
        with self.assertRaises(speaker.SpeakerError):
            speaker.control_volume("get_volume")
        self.assertFalse(any(command[:2] == ["pactl", "set-sink-volume"] for command in self.calls))
        self.selected = LOCAL
        for value in (-1, 101, True, 50.5, "50"):
            with self.subTest(value=value), self.assertRaises(speaker.SpeakerError):
                speaker.control_volume("set_volume", percent=value)
        with self.assertRaises(ControlFailure) as error:
            Hardware().volume("get_volume")
        self.assertEqual(error.exception.code, "audio_unavailable")

    def test_changed_physical_output_does_not_redirect_assistant_volume(self):
        original = self.run_pactl
        reads = 0
        def changed(args, **kwargs):
            nonlocal reads
            if args == ["pactl", "get-default-sink"]:
                reads += 1
                if reads == 2:
                    self.selected = BT
            return original(args, **kwargs)
        with patch.object(speaker, "_run", side_effect=changed):
            speaker.control_volume("set_volume", percent=60)
            self.selected = BT
            speaker.control_volume("adjust_volume", delta=-10)
        self.assertEqual(self.sinks[LOCAL], {"front-left": 50, "front-right": 50})
        self.assertEqual(self.sinks[BT], {"mono": 30})
        self.assertEqual(self.sinks[PHYSICAL], {"front-left": 100, "front-right": 100})
        self.assertEqual(self.sinks["cube.spotify"], {"front-left": 100, "front-right": 100})
        self.assertFalse(any(c[:2] == ["pactl", "get-default-sink"] for c in self.calls))

    def test_above_normal_range_never_boosted(self):
        self.sinks[LOCAL] = {"front-left": 110, "front-right": 110}
        for delta in (10, -10):
            with self.assertRaises(speaker.SpeakerError):
                speaker.control_volume("adjust_volume", delta=delta)
        self.assertEqual(self.sinks[LOCAL], {"front-left": 110, "front-right": 110})
        speaker.control_volume("set_volume", percent=100)
        self.assertEqual(self.sinks[LOCAL], {"front-left": 100, "front-right": 100})

    def test_worker_validates_and_claims_one_volume_command(self):
        controls = Controls("http://invalid", "pi", hardware=Hardware())
        controls.session_id = "a" * 32
        receipt = controls.lifecycle.begin(controls.session_id)
        command = {"session_id": controls.session_id, "request_id": receipt.request_id,
                   "command_id": "b" * 32, "operation": "adjust_volume", "delta": 10}
        self.assertTrue(valid_command(command))
        self.assertEqual(controls.handle(command)["volume"]["channels"],
                         {"front-left": 50, "front-right": 50})
        self.assertEqual(controls.handle(command), {"success": False, "error": "command_expired"})
        self.assertEqual(self.sinks[LOCAL], {"front-left": 50, "front-right": 50})
        for bad in ({"delta": True}, {"delta": 0}, {"delta": 21}, {"delta": "10"},
                    {"operation": "mute; reboot", "delta": 10}):
            self.assertFalse(valid_command({**command, **bad}))
        controls.close()


class FakeTimer:
    def __init__(self, interval, callback):
        self.interval = interval
        self.callback = callback
        self.cancelled = False
        self.daemon = False
        self.started = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.callback()


class VolumeDisplayTests(unittest.TestCase):
    setUp = VolumeTests.setUp
    run_pactl = VolumeTests.run_pactl

    def make_hardware(self):
        timers = []
        def make_timer(interval, callback):
            timer = FakeTimer(interval, callback)
            timers.append(timer)
            return timer
        hardware = Hardware(timer_factory=make_timer)
        sender = Mock()
        hardware._content_command = sender
        return hardware, sender, timers

    def test_successful_volume_actions_show_actual_readback(self):
        hardware, sender, timers = self.make_hardware()
        self.assertEqual(hardware.volume("get_volume")["channels"],
                         {"front-left": 40, "front-right": 40})
        self.assertEqual(sender.call_args.args, ("control show_text 40%",))
        hardware.volume("adjust_volume", delta=10)
        self.assertEqual(sender.call_args.args, ("control show_text 50%",))
        hardware.volume("set_volume", percent=35)
        self.assertEqual(sender.call_args.args, ("control show_text 35%",))
        hardware.volume("mute")
        self.assertEqual(sender.call_args.args, ("control show_text MUTE",))
        hardware.volume("unmute")
        self.assertEqual(sender.call_args.args, ("control show_text 35%",))
        self.assertEqual(len(timers), 5)
        self.assertTrue(all(timer.started and timer.daemon and timer.interval == 2.0
                            for timer in timers))

    def test_new_readback_morphs_and_only_latest_timer_clears(self):
        hardware, sender, timers = self.make_hardware()
        hardware.volume("adjust_volume", delta=10)
        first = timers[-1]
        hardware.volume("adjust_volume", delta=10)
        second = timers[-1]
        self.assertTrue(first.cancelled)
        self.assertFalse(second.cancelled)
        self.assertEqual([call.args[0] for call in sender.call_args_list],
                         ["control show_text 50%", "control show_text 60%"])
        first.fire()
        self.assertEqual(sender.call_count, 2)
        second.fire()
        self.assertEqual(sender.call_args.args, ("control clear_content",))

    def test_muted_and_unequal_channels_never_show_inaccurate_percent(self):
        hardware, sender, _ = self.make_hardware()
        self.sinks[LOCAL] = {"front-left": 43, "front-right": 47}
        hardware.volume("get_volume")
        self.assertEqual(sender.call_args.args, ("control show_text VOL",))
        hardware.volume("mute")
        self.assertEqual(sender.call_args.args, ("control show_text MUTE",))

    def test_playback_refresh_retries_and_restarts_the_visible_window(self):
        hardware, sender, timers = self.make_hardware()
        sender.side_effect = [ControlFailure("display_unavailable"), None]
        hardware.volume("get_volume")
        self.assertEqual(timers, [])
        hardware.refresh_volume_display()
        self.assertEqual([call.args[0] for call in sender.call_args_list],
                         ["control show_text 40%", "control show_text 40%"])
        self.assertEqual(len(timers), 1)
        sender.side_effect = None
        timers[0].fire()
        hardware.refresh_volume_display()
        self.assertEqual(sender.call_args.args, ("control show_text 40%",))
        self.assertEqual(len(timers), 2)

    def test_renderer_failure_does_not_fail_audio_or_leave_timer(self):
        hardware, sender, timers = self.make_hardware()
        sender.side_effect = ControlFailure("display_unavailable")
        result = hardware.volume("adjust_volume", delta=10)
        self.assertEqual(result["channels"], {"front-left": 50, "front-right": 50})
        self.assertEqual(timers, [])
        self.assertEqual(self.sinks[LOCAL], {"front-left": 50, "front-right": 50})

    def test_timer_failure_clears_content_without_failing_audio(self):
        hardware = Hardware(timer_factory=Mock(side_effect=RuntimeError("no thread")))
        sender = Mock()
        hardware._content_command = sender
        result = hardware.volume("get_volume")
        self.assertEqual(result["channels"], {"front-left": 40, "front-right": 40})
        self.assertEqual([call.args[0] for call in sender.call_args_list],
                         ["control show_text 40%", "control clear_content"])

    def test_old_renderer_and_transport_failures_are_logged_without_failing_audio(self):
        hardware = Hardware()
        with patch.object(hardware, "_renderer_request", return_value={}):
            with self.assertLogs("control.hardware", level="WARNING") as logs:
                result = hardware.volume("adjust_volume", delta=10)
            self.assertEqual(result["channels"], {"front-left": 50, "front-right": 50})
            self.assertIn("control show_text 50%", logs.output[0])
            self.assertIn("acknowledgement {}", logs.output[0])
        with patch.object(hardware, "_renderer_request",
                          side_effect=ControlFailure("display_unavailable")):
            with self.assertLogs("control.hardware", level="WARNING") as logs:
                result = hardware.volume("get_volume")
            self.assertEqual(result["channels"], {"front-left": 50, "front-right": 50})
            self.assertIn("transport or JSON unavailable", logs.output[0])
        self.assertIsNone(hardware._volume_display._timer)

    def test_close_cancels_timer_and_clears_owned_content_once(self):
        hardware, sender, timers = self.make_hardware()
        hardware.volume("get_volume")
        hardware.close()
        hardware.close()
        self.assertTrue(timers[0].cancelled)
        self.assertEqual([call.args[0] for call in sender.call_args_list],
                         ["control show_text 40%", "control clear_content"])
        timers[0].fire()
        self.assertEqual(sender.call_count, 2)

    def test_content_protocol_requires_exact_acknowledgement(self):
        hardware = Hardware()
        with patch("display.transport.socket.socket") as factory:
            channel = factory.return_value.__enter__.return_value
            channel.recv.return_value = b'{"accepted":true}'
            hardware._content_command("control show_text 40%")
            channel.send.assert_called_once_with(b"control show_text 40%")
            channel.settimeout.assert_called_once_with(1)
            channel.connect.assert_called_once_with("\0cube-display")
            self.assertTrue(channel.bind.call_args.args[0].startswith("\0cube-control-"))
            for response in (b"{}", b'{"accepted":1}', b'{"accepted":true,"extra":1}', b"bad"):
                channel.recv.return_value = response
                with self.subTest(response=response), self.assertRaises(ControlFailure):
                    hardware._content_command("control clear_content")


if __name__ == "__main__":
    unittest.main()
