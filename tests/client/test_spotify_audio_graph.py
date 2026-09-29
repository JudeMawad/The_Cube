"""Opt-in graph test on an isolated PipeWire server with no hardware modules.

Run with CUBE_TEST_ISOLATED_PIPEWIRE=1 where Unix sockets are permitted.
Never connects to the user's audio session or emits audio.
"""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import unittest
import wave

from audio import speech
from audio.ducking import PipeWireGain


@unittest.skipUnless(os.environ.get("CUBE_TEST_ISOLATED_PIPEWIRE") == "1",
                     "set CUBE_TEST_ISOLATED_PIPEWIRE=1 for the isolated graph test")
class AudioGraphTests(unittest.TestCase):
    def test_stereo_gain_changes_without_changing_node_volume(self):
        for tool in ("pipewire", "pw-dump", "pw-cli", "pw-play"):
            if not shutil.which(tool):
                self.skipTest(f"{tool} is not installed")
        with tempfile.TemporaryDirectory(prefix="cube-pw-proof-") as temporary:
            root = Path(temporary)
            server = root / "server.conf"
            server.write_text("""
context.properties = { core.daemon = true core.name = cube-proof-test }
context.spa-libs = {
    audio.convert.* = audioconvert/libspa-audioconvert
    support.* = support/libspa-support
}
context.modules = [
    { name = libpipewire-module-protocol-native }
    { name = libpipewire-module-client-node }
    { name = libpipewire-module-adapter }
    { name = libpipewire-module-access }
]
""")
            env = dict(os.environ, XDG_RUNTIME_DIR=temporary, PIPEWIRE_RUNTIME_DIR=temporary,
                       PIPEWIRE_REMOTE="cube-proof-test")
            processes = []
            with (root / "log").open("w+") as log:
                def start(config):
                    processes.append(subprocess.Popen(["pipewire", "-c", str(config)],
                                                       env=env, stdout=log, stderr=log))

                def command(*args):
                    return subprocess.run(args, env=env, capture_output=True, text=True,
                                          timeout=3, check=True).stdout

                try:
                    start(server)
                    deadline = time.monotonic() + 3
                    while not (root / "cube-proof-test").exists():
                        if processes[0].poll() is not None or time.monotonic() > deadline:
                            log.seek(0)
                            self.fail("Isolated PipeWire could not start: " + log.read())
                        time.sleep(.02)
                    start(Path(__file__).resolve().parents[2] /
                          "client/deploy/pipewire/cube-audio.conf")
                    deadline = time.monotonic() + 3
                    while True:
                        nodes = {obj["info"]["props"].get("node.name"): obj
                                 for obj in json.loads(command("pw-dump"))
                                 if obj["type"] == "PipeWire:Interface:Node"}
                        if all(name in nodes for name in ("cube.spotify", "cube.spotify.output",
                                                          "cube.assistant", "cube.assistant.output")):
                            break
                        if time.monotonic() > deadline:
                            log.seek(0)
                            self.fail("Proof nodes not created: " + log.read())
                        time.sleep(.02)
                    self.assertEqual(set(nodes), {"cube.spotify", "cube.spotify.output",
                                                  "cube.assistant", "cube.assistant.output"})
                    adapter = PipeWireGain(lambda args, **kwargs: subprocess.run(args, env=env, **kwargs))
                    self.assertEqual(adapter.inspect()[1], .10)
                    for name in nodes:
                        self.assertEqual(nodes[name]["info"]["props"]["node.link-group"], "cube.audio")
                    node_id = str(nodes["cube.spotify"]["id"])
                    adapter.apply(1.0, serial=adapter.inspect()[0])
                    before = command("pw-cli", "enum-params", node_id, "Props")
                    self.assertIn("duck_left:Mult", before)
                    self.assertIn("duck_right:Mult", before)
                    command("pw-cli", "set-param", node_id, "Props",
                            '{ params = [ "duck_left:Mult" 0.10 "duck_right:Mult" 0.10 ] }')
                    ducked = command("pw-cli", "enum-params", node_id, "Props")
                    multipliers = set(re.findall(
                        r'String "(duck_(?:left|right):Mult)"\s+Float ([0-9.]+)', ducked))
                    self.assertEqual(multipliers, {("duck_left:Mult", "0.100000"),
                                                  ("duck_right:Mult", "0.100000")})
                    command("pw-cli", "set-param", node_id, "Props",
                            '{ params = [ "duck_left:Mult" 1.0 "duck_right:Mult" 1.0 ] }')
                    restored = command("pw-cli", "enum-params", node_id, "Props")
                    # Initial subscription may repeat a Props object during graph setup.
                    self.assertEqual(set(before.split("  Object:")), set(restored.split("  Object:")))
                    # Node-level volumes remain identical; only DSP parameters changed.
                    self.assertEqual(before.split("Prop: key Spa:Pod:Object:Param:Props:params")[0],
                                     ducked.split("Prop: key Spa:Pod:Object:Param:Props:params")[0])
                    # Verify the installed pw-play parses our properties onto
                    # the real node. Target 0 prevents all links in this private
                    # graph; no live audio server or WirePlumber is involved.
                    wav = root / "cue.wav"
                    wav.write_bytes(speech.progress_wav())
                    args = speech.pw_play_args(wav)
                    args[args.index("--target") + 1] = "0"
                    processes.append(subprocess.Popen(args, env=env, stdout=log, stderr=log))
                    deadline = time.monotonic() + 3
                    while True:
                        streams = [obj["info"]["props"]
                                   for obj in json.loads(command("pw-dump"))
                                   if obj["type"] == "PipeWire:Interface:Node"
                                   and obj["info"]["props"].get("node.name") == "cube.speech"]
                        if streams:
                            break
                        if processes[-1].poll() is not None or time.monotonic() > deadline:
                            log.seek(0)
                            self.fail("Speech node not created: " + log.read())
                        time.sleep(.02)
                    self.assertEqual(len(streams), 1)
                    self.assertEqual(streams[0]["media.role"], "Communication")
                    self.assertEqual(streams[0]["application.name"], "Cube Speech")
                    self.assertEqual(streams[0]["application.id"], "cube.speech")
                    self.assertEqual(str(streams[0]["state.restore-props"]).lower(), "false")
                finally:
                    for process in reversed(processes):
                        if process.poll() is None:
                            process.terminate()
                            try:
                                process.wait(timeout=3)

                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.wait(timeout=3)

    def test_wireplumber_routes_both_buses_and_follows_output_changes(self):
        for tool in ("pipewire", "wireplumber", "pw-dump", "wpctl", "dbus-run-session"):
            if not shutil.which(tool):
                self.skipTest(f"{tool} is not installed")
        with tempfile.TemporaryDirectory(prefix="cube-routing-test-") as temporary:
            root = Path(temporary)
            server = root / "server.conf"
            server.write_text('''
context.properties = { core.daemon = true core.name = cube-routing-test }
context.spa-libs = {
    audio.convert.* = audioconvert/libspa-audioconvert
    support.* = support/libspa-support
}
context.modules = [
    { name = libpipewire-module-protocol-native }
    { name = libpipewire-module-client-node }
    { name = libpipewire-module-adapter }
    { name = libpipewire-module-access }
    { name = libpipewire-module-metadata }
    { name = libpipewire-module-link-factory }
    { name = libpipewire-module-spa-node-factory }
]
context.objects = [
    { factory = adapter args = {
        factory.name = support.null-audio-sink node.name = test.speaker.one
        media.class = Audio/Sink audio.position = [ FL FR ] priority.session = 1000
    } }
    { factory = adapter args = {
        factory.name = support.null-audio-sink node.name = test.speaker.two
        media.class = Audio/Sink audio.position = [ FL FR ] priority.session = 900
    } }
]
''')
            env = dict(os.environ, XDG_RUNTIME_DIR=temporary, PIPEWIRE_RUNTIME_DIR=temporary,
                       PIPEWIRE_REMOTE="cube-routing-test", XDG_STATE_HOME=str(root / "state"),
                       XDG_CONFIG_HOME=str(root / "config"), XDG_CACHE_HOME=str(root / "cache"),
                       WIREPLUMBER_CONFIG_DIR="/usr/share/wireplumber")
            processes = []
            with (root / "log").open("w+") as log:
                def start(*args):
                    processes.append(subprocess.Popen(args, env=env, stdout=log, stderr=log))
                def command(*args):
                    return subprocess.run(args, env=env, capture_output=True, text=True,
                                          check=True, timeout=3).stdout
                def graph():
                    return json.loads(command("pw-dump"))
                def wait_for(predicate):
                    deadline = time.monotonic() + 5
                    while not predicate():
                        if time.monotonic() > deadline:
                            log.seek(0)
                            self.fail("Private routing check timed out: " + log.read())
                        time.sleep(.02)
                def routed_to(name):
                    objects = graph()
                    nodes = {n["id"]: n["info"]["props"].get("node.name") for n in objects
                             if n["type"] == "PipeWire:Interface:Node"}
                    destinations = {(nodes.get(n["info"]["output-node-id"]),
                                     nodes.get(n["info"]["input-node-id"])) for n in objects
                                    if n["type"] == "PipeWire:Interface:Link"}
                    wanted = {("cube.spotify.output", name), ("cube.assistant.output", name)}
                    owned = {pair for pair in destinations if pair[0] in {
                        "cube.spotify.output", "cube.assistant.output"}}
                    return owned == wanted
                try:
                    start("pipewire", "-c", str(server))
                    wait_for(lambda: (root / "cube-routing-test").exists())
                    # Installed 'policy' profile has NO ALSA/Bluetooth/video
                    # monitors; this session can see only our null sinks.
                    start("dbus-run-session", "--", "wireplumber", "--profile", "policy")
                    start("pipewire", "-c", str(Path(__file__).resolve().parents[2] /
                                               "client/deploy/pipewire/cube-audio.conf"))
                    wait_for(lambda: routed_to("test.speaker.one"))
                    # Synthetic silence exercises real WirePlumber stream
                    # restoration on null sinks, never hardware audio.
                    wav = root / "silence.wav"
                    with wave.open(str(wav), "wb") as output:
                        output.setnchannels(1)
                        output.setsampwidth(2)
                        output.setframerate(16000)
                        output.writeframes(b"\0" * 16000 * 2 * 30)
                    start("pw-play", "--target", "cube.spotify", "--media-role", "Music",
                          "--properties", '{"node.name":"test.spotify"}', str(wav))
                    def get_node(name):
                        return next((n for n in graph() if n["type"] == "PipeWire:Interface:Node"
                                     and n["info"]["props"].get("node.name") == name), None)
                    wait_for(lambda: get_node("test.spotify") is not None)
                    command("pw-cli", "set-param", "test.spotify", "Props",
                            '{ channelVolumes = [ 0.00659 ] }')
                    start(*speech.pw_play_args(wav))
                    wait_for(lambda: get_node("cube.speech") is not None)
                    def channel_volumes(name):
                        node = get_node(name)
                        values = [p["channelVolumes"] for p in node["info"]["params"]["Props"]
                                  if "channelVolumes" in p]
                        return values[-1] if values else None
                    wait_for(lambda: channel_volumes("cube.speech") is not None)
                    self.assertEqual(channel_volumes("cube.speech"), [1.0])
                    self.assertAlmostEqual(channel_volumes("test.spotify")[0], .00659, places=5)
                    physical_before = channel_volumes("test.speaker.one")
                    command("pw-cli", "set-param", "cube.assistant", "Props",
                            '{ channelVolumes = [ 0.4 0.4 ] }')
                    command("pw-cli", "set-param", "cube.spotify", "Props",
                            '{ params = [ "duck_left:Mult" 1.0 "duck_right:Mult" 1.0 ] }')
                    self.assertAlmostEqual(channel_volumes("test.spotify")[0], .00659, places=5)
                    self.assertEqual(channel_volumes("test.speaker.one"), physical_before)
                    self.assertAlmostEqual(channel_volumes("cube.assistant")[0], .4, places=5)
                    nodes = {n["info"]["props"].get("node.name"): n["id"] for n in graph()
                             if n["type"] == "PipeWire:Interface:Node"}
                    command("wpctl", "set-default", str(nodes["test.speaker.two"]))
                    wait_for(lambda: routed_to("test.speaker.two"))
                    command("wpctl", "set-default", str(nodes["test.speaker.one"]))
                    wait_for(lambda: routed_to("test.speaker.one"))
                    command("pw-cli", "destroy", "test.speaker.one")
                    wait_for(lambda: routed_to("test.speaker.two"))
                    # A graph rebuild must restore only the assistant bus's
                    # unique state key, and start music attenuated again.
                    graph_process = processes[2]
                    graph_process.terminate()
                    graph_process.wait(timeout=3)
                    start("pipewire", "-c", str(Path(__file__).resolve().parents[2] /
                                               "client/deploy/pipewire/cube-audio.conf"))
                    wait_for(lambda: routed_to("test.speaker.two"))
                    start(*speech.pw_play_args(wav))
                    wait_for(lambda: abs(channel_volumes("cube.assistant")[0] - .4) < .00001)
                    adapter = PipeWireGain(lambda args, **kwargs: subprocess.run(args, env=env, **kwargs))
                    self.assertEqual(adapter.inspect()[1], .10)
                finally:
                    for process in reversed(processes):
                        if process.poll() is None:
                            process.terminate()
                            try:
                                process.wait(timeout=3)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.wait(timeout=3)
