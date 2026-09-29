"""Deterministic lifecycle, recovery and DSP isolation tests; no real audio."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from audio.activity import ActivityLease, ActivityPublisher, atomic_json, read_json
from audio.ducking import Ducking, PipeWireGain
from interaction import Coordinator


class Clock:
    now = 10.0
    def __call__(self):
        return self.now


class Gain:
    def __init__(self):
        self.gain, self.serial = 1.0, "100"
        self.available = True
        self.writes = []
        self.play = Mock(side_effect=AssertionError("play forbidden"))
        self.pause = Mock(side_effect=AssertionError("pause forbidden"))
        self.resume = Mock(side_effect=AssertionError("resume forbidden"))
        self.transfer = Mock(side_effect=AssertionError("transfer forbidden"))
        self.set_volume = Mock(side_effect=AssertionError("Spotify volume forbidden"))
        self.phone_volume = 10
        self.paused = False

    def inspect(self):
        if not self.available:
            raise OSError("disconnected")
        return self.serial, self.gain

    def apply(self, gain, *, serial=None):
        if not self.available or (serial is not None and serial != self.serial):
            raise OSError("disconnected")
        self.gain = gain
        self.writes.append(gain)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.publisher = ActivityPublisher(clock=self.clock, boot="boot")
        self.lease = ActivityLease(boot="boot")
        self.gain = Gain()
        self.duck = Ducking(self.gain, clock=self.clock)
        self.coordinator = Coordinator(Mock(), Mock(), clock=self.clock,
                                       activity=self.publisher.publish)
        self.coordinator.set_state("idle")
        self.tick()

    def tick(self, seconds=0):
        self.clock.now += seconds
        self.lease.accept(self.publisher.latest, self.clock())
        self.duck.tick(self.lease.active(self.clock()))

    def tearDown(self):
        for operation in ("play", "pause", "resume", "transfer", "set_volume"):
            getattr(self.gain, operation).assert_not_called()

    def wake(self):
        owner = self.coordinator.begin(.004, wake=True)
        self.tick()
        return owner

    def test_wake_fades_and_all_active_states_stay_ducked(self):
        self.wake()
        self.tick(.075)
        self.assertAlmostEqual(self.gain.gain, .55)
        self.tick(.075)
        self.assertAlmostEqual(self.gain.gain, .10)
        for state in ("listening", "thinking", "speech 0.35", "followup"):
            self.coordinator.set_state(state)
            self.tick(.3)
            self.assertAlmostEqual(self.gain.gain, .10)
        self.coordinator.set_state("idle")
        self.tick()
        self.tick(.2)
        self.assertAlmostEqual(self.gain.gain, .55)
        self.tick(.2)
        self.assertAlmostEqual(self.gain.gain, 1)

    def test_followup_expiry_is_only_release(self):
        owner = self.wake()
        self.tick(.15)
        self.coordinator.complete(owner, 1, .004)
        self.tick()
        self.assertEqual(self.coordinator.state, "followup")
        self.assertAlmostEqual(self.gain.gain, .10)
        self.clock.now += 1.1
        self.coordinator.frame(b"", 0, False, .004)
        self.assertEqual(self.coordinator.state, "idle")
        self.tick()
        self.tick(.4)
        self.assertAlmostEqual(self.gain.gain, 1)

    def test_barge_in_rollover_and_stale_release_never_restore(self):
        old = self.wake()
        self.tick(.15)
        stale = dict(self.publisher.latest, active=False)
        self.coordinator.set_state("speech 0.35")
        new = self.wake()
        self.coordinator.complete(old, 0, .004)
        self.assertTrue(old.cancelled.is_set())
        self.assertIs(self.coordinator.current, new)
        self.assertFalse(self.lease.accept(stale, self.clock()))
        self.tick(.2)
        self.assertAlmostEqual(self.gain.gain, .10)
        # Even a higher sequence cannot release a newer generation.
        stale["sequence"] = 999
        self.assertFalse(self.lease.accept(stale, self.clock()))

    def test_new_wake_reverses_restore_from_applied_gain(self):
        owner = self.wake()
        self.tick(.15)
        self.coordinator.complete(owner, 0, .004)
        self.tick()
        self.tick(.2)
        before = self.gain.gain
        self.wake()
        self.assertEqual(self.gain.gain, before)
        self.tick(.075)
        self.assertAlmostEqual(self.gain.gain, (before + .10) / 2)
        self.tick(.075)
        self.assertAlmostEqual(self.gain.gain, .10)

    def test_voice_death_expires_lease_without_resuming_paused_music(self):
        self.wake()
        self.tick(.15)
        self.gain.paused = True
        self.gain.phone_volume = 67
        self.tick(2)
        self.tick(.4)
        self.assertAlmostEqual(self.gain.gain, 1)
        self.assertTrue(self.gain.paused)
        self.assertEqual(self.gain.phone_volume, 67)

    def test_already_paused_music_remains_paused(self):
        self.gain.paused = True
        self.test_wake_fades_and_all_active_states_stay_ducked()
        self.assertTrue(self.gain.paused)

    def test_disappearing_graph_returns_ducked_during_active_interaction(self):
        self.wake()
        self.tick(.15)
        self.gain.available = False
        self.tick(.3)
        self.gain.available, self.gain.serial, self.gain.gain = True, "200", .10
        self.tick(.3)
        self.assertAlmostEqual(self.gain.gain, .10)

    def test_control_restart_reads_retained_lease_before_neutral(self):
        self.wake()
        self.lease = ActivityLease(boot="boot")
        self.duck = Ducking(self.gain, clock=self.clock)
        self.gain.writes.clear()
        self.tick()
        self.assertEqual(self.gain.writes, [.10])

    def test_graph_replaced_between_periodic_inspections_is_not_raised_by_old_fade(self):
        self.wake()
        self.tick(.05)
        self.gain.serial, self.gain.gain = "new", .10
        self.tick(.02)
        self.assertEqual(self.gain.gain, .10)
        self.tick(.02)
        self.assertEqual(self.gain.gain, .10)

    def test_frame_refreshes_lease_but_cancellation_does_not_release(self):
        owner = self.wake()
        self.coordinator.recorder = None
        self.coordinator.set_state("thinking")
        for _ in range(20):
            self.clock.now += .25
            self.coordinator.frame(b"", 0, False, .004)
            self.tick()
            self.assertTrue(self.lease.active(self.clock()))
        owner.cancel()
        self.tick(.25)
        self.assertTrue(self.lease.active(self.clock()))


class LeaseTests(unittest.TestCase):
    def test_capture_publication_does_not_write_files(self):
        publisher = ActivityPublisher(clock=lambda: 10, boot="boot")
        with patch("audio.activity.atomic_json") as write:
            publisher.publish(1, True)
            publisher.publish(2, True)
            self.assertEqual(publisher.latest["sequence"], 2)
            self.assertEqual(publisher.latest["generation"], 2)
            write.assert_not_called()

    def test_invalid_and_previous_boot_or_process_packets(self):
        lease = ActivityLease(boot="boot")
        value = dict(boot="boot", instance="new", started=5, sent=10,
                     sequence=5, generation=3, active=True)
        self.assertTrue(lease.accept(value, 10))
        for changes in ({"boot": "old"}, {"sequence": 4}, {"generation": 2},
                        {"active": 1}, {"sent": float("nan")}, {"sent": 11},
                        {"started": 11}, {"instance": "old", "started": 4}):
            self.assertFalse(lease.accept(dict(value, **changes), 10))
        self.assertTrue(lease.accept(dict(value, instance="newer", started=9,
                                          sequence=1, generation=0, active=False), 10))
        self.assertFalse(lease.active(10))

    def test_snapshot_atomic_private_and_corruption_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "audio" / "interaction.json"
            atomic_json(path, {"active": True})
            self.assertEqual(read_json(path), {"active": True})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            path.write_text("broken")
            with self.assertRaises(ValueError):
                read_json(path)
            atomic_json(path, {"active": False})
            self.assertEqual(read_json(path), {"active": False})
            link = path.with_name("link")
            link.symlink_to(path)
            with self.assertRaises(OSError):
                read_json(link)


def graph_node(name="cube.spotify", bus="spotify", serial=42):
    return dict(id=11, type="PipeWire:Interface:Node", info={
        "props": {"node.name": name, "cube.audio.bus": bus,
                  "media.class": "Audio/Sink", "object.serial": serial},
        "params": {"Props": [{"volume": 1, "params": [
            "duck_left:Mult", .10, "duck_right:Mult", .10]}]}})


class AdapterTests(unittest.TestCase):
    def test_only_exact_owned_node_and_only_dsp_controls(self):
        run = Mock(return_value=SimpleNamespace(stdout=json.dumps([
            graph_node(), graph_node("cube.spotify.output"), graph_node("unrelated")]), stderr=""))
        adapter = PipeWireGain(run)
        self.assertEqual(adapter.inspect(), ("42", .10))
        adapter.apply(.4)
        args = run.call_args.args[0]
        self.assertEqual(args[:4], ("pw-cli", "set-param", "cube.spotify", "Props"))
        self.assertEqual(json.loads(args[4]), {"params": [
            "duck_left:Mult", .4, "duck_right:Mult", .4]})

    def test_missing_duplicate_wrong_identity_and_malformed_nodes(self):
        for nodes in ([], [graph_node(), graph_node()], [graph_node(bus="other")],
                      [graph_node("Spotify")], [{"type": "PipeWire:Interface:Node"}]):
            run = Mock(return_value=SimpleNamespace(stdout=json.dumps(nodes), stderr=""))
            with self.subTest(nodes=nodes), self.assertRaises(OSError):
                PipeWireGain(run).inspect()
        for value in (float("nan"), -1, 2):
            with self.assertRaises(ValueError):
                PipeWireGain(Mock()).apply(value)


class ServiceContractTests(unittest.TestCase):
    def test_worker_stop_cleanup_only_restores_music_dsp(self):
        from audio import duck_service
        adapter = Mock()
        with patch.object(duck_service, "PipeWireGain", return_value=adapter), \
             patch.object(duck_service.sys, "argv", ["duck_service", "neutral"]):
            duck_service.main()
        self.assertEqual([call[0] for call in adapter.mock_calls], ["inspect", "apply"])
        adapter.apply.assert_called_once_with(1.0)

    def test_services_are_independent_and_old_proof_is_retired(self):
        deploy = Path(__file__).resolve().parents[2] / "client/deploy"
        for name in ("cube-audio", "cube-audio-control", "cube-spotify-bridge", "cube-spotify"):
            unit = (deploy / f"{name}.service").read_text()
            self.assertNotIn("PartOf=", unit)
            self.assertNotIn("BindsTo=", unit)
            self.assertNotIn("Requires=cube-voice", unit)
        control = (deploy / "cube-audio-control.service").read_text()
        self.assertIn("ExecStopPost=", control)
        self.assertIn("audio.duck_service neutral", control)
        self.assertFalse((deploy / "cube-spotify-audio-proof.service").exists())
        self.assertFalse((deploy / "pipewire/cube-music-proof.conf").exists())
