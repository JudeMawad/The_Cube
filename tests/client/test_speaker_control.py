"""Offline speaker tests: never touch real Bluetooth or audio output."""
import json
import os
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from audio import speaker_control as speaker


ADDRESS = "02:00:00:00:00:01"
SINK = "bluez_output.02_00_00_00_00_01.1"
INFO = (
    "Paired: yes\nConnected: no\nBlocked: no\n"
    "UUID: Audio Sink (" + speaker.AUDIO_SINK_UUID + ")\n"
)


class SpeakerTests(unittest.TestCase):
    def setUp(self):
        self.env_patch = patch.dict(os.environ, {"CUBE_SPEAKER_MAC": ""})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def test_exact_phrases_and_punctuation(self):
        with patch.object(speaker, "connect_speaker", return_value={}) as connect:
            for text in speaker.CONNECT_PHRASES | {"Connect to speaker!", "Please connect to speaker."}:
                self.assertTrue(speaker.handle_speaker_command(text)["success"])
            self.assertEqual(connect.call_count, len(speaker.CONNECT_PHRASES) + 2)

    def test_unrelated_and_negated_commands_do_nothing(self):
        with patch.object(speaker, "connect_speaker") as connect, patch.object(speaker, "disconnect_speaker") as disconnect:
            for text in ["don't disconnect speaker", "don't connect to speaker", "lights on",
                         "download Interstellar", "yes", "connect to speaker later"]:
                self.assertIsNone(speaker.handle_speaker_command(text))
            connect.assert_not_called()
            disconnect.assert_not_called()

    def test_errors_return_failure_without_crashing(self):
        for error in [speaker.SpeakerError("Speaker is off."), RuntimeError("private detail")]:
            with patch.object(speaker, "connect_speaker", side_effect=error):
                result = speaker.handle_speaker_command("connect to speaker")
            self.assertFalse(result["success"])
            self.assertEqual(result["response"], result["error"])
            self.assertNotIn("private detail", result["error"])

    def test_find_paired_logitech_audio_device(self):
        with patch.object(speaker, "_run", side_effect=[
            SimpleNamespace(stdout=f"Device {ADDRESS} Logitech BT Adapter\nDevice 02:00:00:00:00:02 Other\n"),
            SimpleNamespace(stdout=INFO),
        ]):
            self.assertEqual(speaker._speaker()[0], ADDRESS)

    def test_no_paired_speaker(self):
        with patch.object(speaker, "_run", return_value=SimpleNamespace(stdout="")):
            with self.assertRaisesRegex(speaker.SpeakerError, "paired Logitech"):
                speaker._speaker()

    def test_paired_keyboard_not_selected(self):
        with patch.object(speaker, "_run", side_effect=[
            SimpleNamespace(stdout=f"Device {ADDRESS} Logitech Keyboard"),
            SimpleNamespace(stdout="Paired: yes\n"),
        ]):
            with self.assertRaises(speaker.SpeakerError):
                speaker._speaker()

    def test_multiple_speakers_require_selection(self):
        with patch.object(speaker, "_run", side_effect=[
            SimpleNamespace(stdout=f"Device {ADDRESS} Logitech One\nDevice 02:00:00:00:00:02 Logitech Two"),
            SimpleNamespace(stdout=INFO), SimpleNamespace(stdout=INFO),
        ]):
            with self.assertRaisesRegex(speaker.SpeakerError, "More than one"):
                speaker._speaker()

    def test_address_override_must_be_valid_and_paired(self):
        with patch.dict(os.environ, {"CUBE_SPEAKER_MAC": "bad; touch /tmp/no"}):
            with patch.object(speaker, "_run") as run:
                with self.assertRaises(speaker.SpeakerError):
                    speaker._speaker()
                run.assert_not_called()
        with patch.dict(os.environ, {"CUBE_SPEAKER_MAC": ADDRESS}):
            with patch.object(speaker, "_run", return_value=SimpleNamespace(stdout=INFO)):
                self.assertEqual(speaker._speaker()[0], ADDRESS)

    def test_sink_matches_target_address_only(self):
        sinks = [{"name": "alsa_output.microphone"},
                 {"name": "bluez_output.02_00_00_00_00_02.1"},
                 {"name": SINK}]
        with patch.object(speaker, "_run", return_value=SimpleNamespace(stdout=json.dumps(sinks))):
            self.assertEqual(speaker._find_sink(ADDRESS), SINK)

    def test_malformed_sink_response(self):
        for text in ["bad JSON", "{}", '[{"name":null}]']:
            with patch.object(speaker, "_run", return_value=SimpleNamespace(stdout=text)):
                with self.assertRaises(speaker.SpeakerError):
                    speaker._find_sink(ADDRESS)

    def connection(self, info=INFO, sink=SINK, connect_ok=True):
        def run(args, **kwargs):
            text = ""
            code = 0
            if args[:2] == ["bluetoothctl", "show"]:
                text = "Powered: yes"
            elif args[:2] == ["bluetoothctl", "info"]:
                text = INFO.replace("Connected: no", "Connected: yes") if connect_ok else INFO
            elif args == ["pactl", "get-default-sink"]:
                text = SINK
            elif "connect" in args and not connect_ok:
                code = 1
            return SimpleNamespace(stdout=text, returncode=code)
        selection = patch.object(speaker, "_speaker", return_value=(ADDRESS, info))
        output = patch.object(speaker, "_find_sink", return_value=sink)
        runner = patch.object(speaker, "_run", side_effect=run)
        sleeper = patch.object(speaker.time, "sleep")
        for p in [selection, output, runner, sleeper]:
            self.addCleanup(p.stop)
        selection.start()
        output.start()
        sleeper.start()
        return runner.start()

    def test_connect_selects_output_without_touching_microphone_or_pairing(self):
        run = self.connection()
        self.assertEqual(speaker.connect_speaker()["speaker_sink"], SINK)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(["bluetoothctl", "--timeout", "15", "connect", ADDRESS, "a2dp-sink"], commands)
        self.assertIn(["pactl", "set-default-sink", SINK], commands)
        self.assertFalse(any("set-default-source" in cmd or "pair" in cmd or "remove" in cmd for cmd in commands))

    def test_already_connected_is_idempotent(self):
        run = self.connection(info=INFO.replace("Connected: no", "Connected: yes"))
        speaker.connect_speaker()
        self.assertFalse(any("connect" in c.args[0] for c in run.call_args_list))

    def test_unavailable_speaker_does_not_change_output(self):
        run = self.connection(connect_ok=False)
        with self.assertRaisesRegex(speaker.SpeakerError, "couldn't connect"):
            speaker.connect_speaker()
        self.assertFalse(any("set-default-sink" in c.args[0] for c in run.call_args_list))

    def test_missing_sink_does_not_change_output(self):
        run = self.connection(sink=None)
        with self.assertRaisesRegex(speaker.SpeakerError, "not ready"):
            speaker.connect_speaker()
        self.assertFalse(any("set-default-sink" in c.args[0] for c in run.call_args_list))

    def test_timeout_and_missing_tools_are_handled(self):
        for error in [FileNotFoundError(), subprocess.TimeoutExpired("bluetoothctl", 5), OSError()]:
            with patch.object(speaker.subprocess, "run", side_effect=error):
                with self.assertRaises(speaker.SpeakerError):
                    speaker._run(["bluetoothctl", "show"])

    def test_system_service_audio_environment(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(speaker.os, "getuid", return_value=1000):
            with patch.object(speaker.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
                speaker._run(["pactl", "get-default-sink"])
            env = run.call_args.kwargs["env"]
            self.assertEqual(env["XDG_RUNTIME_DIR"], "/run/user/1000")
            self.assertEqual(env["PULSE_SERVER"], "unix:/run/user/1000/pulse/native")
            self.assertEqual(env["LC_ALL"], "C")
            self.assertNotIn("shell", run.call_args.kwargs)

    def test_disconnect_phrases(self):
        with patch.object(speaker, "disconnect_speaker", return_value={}) as disconnect:
            with patch.object(speaker, "connect_speaker") as connect:
                for text in speaker.DISCONNECT_PHRASES | {"Disconnect from speaker!", "Please disconnect the speaker."}:
                    result = speaker.handle_speaker_command(text)
                    self.assertTrue(result["success"])
                    self.assertEqual(result["action"], "speaker_disconnect")
                self.assertEqual(disconnect.call_count, len(speaker.DISCONNECT_PHRASES) + 2)
                connect.assert_not_called()

    def test_disconnect_error_is_handled(self):
        with patch.object(speaker, "disconnect_speaker", side_effect=speaker.SpeakerError("Still connected.")):
            result = speaker.handle_speaker_command("disconnect from speaker")
        self.assertFalse(result["success"])
        self.assertEqual(result["action"], "speaker_disconnect")
        self.assertEqual(result["response"], "Still connected.")

    def disconnection(self, connected=True, stuck=False, audio_error=False):
        local = "alsa_output.usb-reSpeaker.analog-stereo"
        def run(args, **kwargs):
            if args == ["pactl", "get-default-sink"]:
                if audio_error:
                    raise speaker.SpeakerError("Audio unavailable.")
                return SimpleNamespace(stdout=SINK, returncode=0)
            if args[:2] == ["bluetoothctl", "info"]:
                return SimpleNamespace(
                    stdout=INFO.replace("Connected: no", "Connected: yes") if stuck else INFO,
                    returncode=0,
                )
            return SimpleNamespace(stdout="", returncode=0)
        patches = [
            patch.object(speaker, "_speaker", return_value=(
                ADDRESS, INFO.replace("Connected: no", "Connected: yes") if connected else INFO)),
            patch.object(speaker, "_run", side_effect=run),
            patch.object(speaker, "_restore_output", return_value=local),
            patch.object(speaker.time, "sleep"),
        ]
        mocks = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        return mocks[1], mocks[2]

    def test_disconnect_verifies_state_and_restores_output(self):
        run, restore = self.disconnection()
        result = speaker.disconnect_speaker()
        self.assertIn("playback_sink", result)
        commands = [c.args[0] for c in run.call_args_list]
        self.assertIn(["bluetoothctl", "--timeout", "10", "disconnect", ADDRESS], commands)
        restore.assert_called_once_with(ADDRESS, SINK)
        self.assertFalse(any("pair" in c or "remove" in c or "power" in c or "set-default-source" in c
                             for c in commands))

    def test_already_disconnected_is_idempotent(self):
        run, restore = self.disconnection(connected=False)
        speaker.disconnect_speaker()
        self.assertFalse(any("disconnect" in c.args[0] for c in run.call_args_list))
        restore.assert_called_once()

    def test_stuck_connection_does_not_claim_success_or_change_output(self):
        run, restore = self.disconnection(stuck=True)
        with self.assertRaisesRegex(speaker.SpeakerError, "couldn't disconnect"):
            speaker.disconnect_speaker()
        restore.assert_not_called()

    def test_audio_failure_does_not_block_bluetooth_disconnect(self):
        run, restore = self.disconnection(audio_error=True)
        speaker.disconnect_speaker()
        self.assertTrue(any("disconnect" in c.args[0] for c in run.call_args_list))
        restore.assert_called_once_with(ADDRESS, None)

    def test_fallback_failure_reports_partial_result_truthfully(self):
        run, restore = self.disconnection()
        restore.side_effect = speaker.SpeakerError("No local output.")
        with self.assertRaisesRegex(speaker.SpeakerError, "is disconnected, but playback"):
            speaker.disconnect_speaker()

    def test_restore_prefers_respeaker_and_never_changes_microphone(self):
        local = "alsa_output.usb-reSpeaker.analog-stereo"
        with patch.object(speaker, "_run", side_effect=[
            SimpleNamespace(stdout=json.dumps([{"name": "alsa_output.hdmi"}, {"name": local}, {"name": SINK}])),
            SimpleNamespace(stdout=""), SimpleNamespace(stdout=local),
        ]) as run:
            self.assertEqual(speaker._restore_output(ADDRESS, SINK), local)
        self.assertEqual(run.call_args_list[1].args[0], ["pactl", "set-default-sink", local])
        self.assertFalse(any("set-default-source" in c.args[0] for c in run.call_args_list))

    def test_restore_preserves_different_selected_output(self):
        with patch.object(speaker, "_run") as run:
            self.assertEqual(speaker._restore_output(ADDRESS, "alsa_output.custom"), "alsa_output.custom")
            run.assert_not_called()

    def test_no_local_output_is_handled(self):
        with patch.object(speaker, "_run", return_value=SimpleNamespace(stdout="[]")):
            with self.assertRaisesRegex(speaker.SpeakerError, "No local audio"):
                speaker._restore_output(ADDRESS, SINK)
