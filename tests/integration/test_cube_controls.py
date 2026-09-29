"""Serialized server-to-client controls through real registry, routes and worker.

Service-local imports remain independent; only this contract test imports both.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import importlib
import sys
import numpy as np  # Import native extensions before patch.dict restores modules.
from threading import Event
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from tests.paths import ROOT
sys.path.insert(0, str(ROOT / "client/app"))
from tests.client.runtime_helpers import run_reply, complete_reply
from audio.playback import PlaybackOutcome
from control.worker import Controls
from control.hardware import Hardware
from fastapi import FastAPI
from fastapi.testclient import TestClient
from core.schemas import ToolIntent
from core.tool_registry import ToolContext, ToolRegistry
from core.tool_execution import execute_tool
from features.cube import ControlChannel, CubeCommands, control_router
from features.cube.tools import register_tools
from features.cube.routes import authenticate


class CubeContractTests(TestCase):
    def setUp(self):
        self.channel = ControlChannel(timeout=2)
        self.addCleanup(self.channel.close)
        self.app = FastAPI()
        self.app.include_router(control_router(self.channel))
        self.app.dependency_overrides[authenticate] = lambda: None
        self.web = TestClient(self.app)
        self.addCleanup(self.web.close)
        self.registry = ToolRegistry()
        register_tools(self.registry, commands=CubeCommands(self.channel))
        self.hardware = Mock(spec=Hardware)
        self.client = Controls("http://testserver", "pi", hardware=self.hardware)
        self.addCleanup(self.client.close)
        self.headers = {"X-Cube-Client-ID": "pi"}
        self.session = "a" * 32
        self.client.session_id = self.session
        self.web.post("/cube/control/session", json={"session_id": self.session}, headers=self.headers)
        self.power_guard = patch("control.hardware.subprocess.run", side_effect=AssertionError("Real power forbidden"))
        self.power_guard.start()
        self.addCleanup(self.power_guard.stop)

    def round_trip(self, operation, **arguments):
        receipt = self.client.lifecycle.begin(self.session)
        tool = self.registry.validate_intent(ToolIntent(type="tool", tool="cube." + operation, arguments=arguments))
        context = ToolContext("pi", self.session, receipt.request_id)
        with ThreadPoolExecutor() as pool:
            future = pool.submit(asyncio.run, execute_tool(tool, context))
            delivered = self.web.get("/cube/control/next", params={"session_id": self.session}, headers=self.headers)
            self.assertEqual(delivered.status_code, 200)
            command = delivered.json()
            answer = self.client.handle(command)
            result = self.web.post("/cube/control/" + command["command_id"] + "/result",
                                   json={"session_id": self.session, **answer}, headers=self.headers)
            self.assertEqual(result.status_code, 200)
            outcome = future.result()
        return receipt, outcome

    def test_scoped_nonpower_round_trip(self):
        for operation, arguments in [("set_display", {"state": "off"}),
                                     ("set_brightness", {"percent": 30})]:
            with self.subTest(operation=operation):
                receipt, result = self.round_trip(operation, **arguments)
                self.assertTrue(result.success)
                self.hardware.renderer.assert_called_with(operation, next(iter(arguments.values())))
                run_reply({**result.data["reply"], "success": result.success, "_cube_playback": receipt},
                                   lambda *a, **kw: True)
        self.hardware.check_power.assert_not_called()
        self.hardware.power.assert_not_called()

    def test_master_and_effective_brightness_survive_status_round_trip(self):
        self.hardware.status.return_value = {
            "display_available": True, "display_enabled": True,
            "master_brightness_percent": 50,
        }
        _, result = self.round_trip("get_status")
        self.assertTrue(result.success)
        self.assertEqual(result.data["status"], self.hardware.status.return_value)
        self.assertIn("brightness is 50 percent", result.data["reply"]["response"])
        self.hardware.power.assert_not_called()

    def test_volume_round_trip_reads_selected_sink_and_no_duplicate(self):
        self.hardware.volume.return_value = {
            "sink": "bluez_output.02_00_00_00_00_03.1",
            "channels": {"mono": 55}, "muted": False,
        }
        receipt, outcome = self.round_trip("adjust_volume", delta=5)
        self.assertTrue(outcome.success)
        self.assertEqual(outcome.data["reply"]["response"], "Volume 55 percent.")
        self.assertEqual(outcome.data["reply"]["listen_for_seconds"], 10)
        self.hardware.volume.assert_called_once_with("adjust_volume", percent=None, delta=5)
        run_reply({**outcome.data["reply"], "success": True, "_cube_playback": receipt},
                           lambda *a, **kw: True)
        self.hardware.power.assert_not_called()

    def test_power_round_trip_waits_for_matching_audio_and_no_replay(self):
        receipt, outcome = self.round_trip("shutdown")
        self.hardware.power.assert_not_called()
        self.assertEqual(outcome.data["reply"]["response"], "Shutting down.")
        result = {**outcome.data["reply"], "success": True, "_cube_playback": receipt}
        run_reply(result, lambda *a, **kw: True)
        self.hardware.power.assert_called_once_with("shutdown")
        self.client.close()
        run_reply(result, lambda *a, **kw: True)
        self.hardware.power.assert_called_once()

    def test_current_audio_cleanup_precedes_power(self):
        order = []
        receipt, outcome = self.round_trip("reboot")
        self.hardware.power.side_effect = lambda action: order.append(action)
        def say(text):
            order.append("played_and_reaped")
            return True
        asyncio.run(complete_reply({**outcome.data["reply"], "success": True,
                                    "_cube_playback": receipt}, say, controls=self.client))
        self.assertEqual(order, ["played_and_reaped", "reboot"])

    def test_background_worker_consumes_structured_http_without_audio_thread(self):
        self.client._disconnect()
        registered = Event()
        delivery = Event()
        response = Mock(status_code=200)
        response.json.return_value = {"accepted": True}
        def post(url, **kwargs):
            result = self.web.post(url, **{key: value for key, value in kwargs.items()
                                          if key in {"headers", "json"}})
            if url.endswith("/session"):
                registered.set()
            else:
                self.client.stopped.set()
            return result
        def get(url, **kwargs):
            delivery.set()
            return self.web.get(url, params=kwargs["params"], headers=kwargs["headers"])
        with patch("control.worker.requests.Session") as factory, \
             patch.object(self.client, "_headers", return_value=self.headers):
            http = factory.return_value.__enter__.return_value
            http.post.side_effect = post
            http.get.side_effect = get
            self.client.start()
            self.assertTrue(registered.wait(2))
            self.assertTrue(delivery.wait(2))
            receipt, headers = self.client.begin_request()
            self.assertIsNotNone(receipt)
            session = headers["X-Cube-Control-Session"]
            result = self.channel.request("pi", session, receipt.request_id, "set_display", state="off")
            self.assertTrue(result.success)
            self.client.thread.join(2)
            self.assertFalse(self.client.thread.is_alive())
        self.hardware.renderer.assert_called_once_with("set_display", "off")
        self.hardware.power.assert_not_called()

    def test_voice_volume_fast_path_calls_pi_once_without_interpretation(self):
        from tests.server.core.test_voice_http import VoiceHTTPTests
        from features.cube.protocol import Result, VolumeStatus
        from core.ai_client import AINodeError
        from unittest.mock import AsyncMock
        fixture = VoiceHTTPTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        server = fixture.server
        server.ai_client = Mock()
        server.ai_client.config.url = "http://ai.invalid"
        server.ai_client.transcribe_remote = AsyncMock(side_effect=AINodeError("Mock offline"))
        server.ai_client.process_remote = AsyncMock(side_effect=AssertionError("No interpretation expected"))
        volume = VolumeStatus(sink="alsa_output.reSpeaker", channels={"mono": 50}, muted=False)
        with patch.object(server.control_channel, "request",
                          return_value=Result(session_id=self.session, success=True, volume=volume)) as dispatch, \
             patch.object(server, "authenticate_control"):
            response = fixture.voice("Volume up", {"X-Cube-Client-ID": "pi",
                "X-Cube-Control-Session": self.session, "X-Cube-Request-ID": "b" * 32})
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["processing_source"], "local_fast")
        self.assertEqual(result["response"], "Volume 50 percent.")
        dispatch.assert_called_once_with("pi", self.session, "b" * 32, "adjust_volume", delta=10)
        server.ai_client.process_remote.assert_not_called()

    def test_real_voice_composition_keeps_private_context_out_of_ai(self):
        from tests.server.core.test_voice_http import VoiceHTTPTests
        from core.ai_client import AINodeError
        from unittest.mock import AsyncMock
        fixture = VoiceHTTPTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        server = fixture.server
        self.assertIn("cube.shutdown", [tool.name for tool in server.tool_registry.list_executable_tools()])
        server.ai_client = Mock()
        server.ai_client.config.url = "http://ai.invalid"
        server.ai_client.transcribe_remote = AsyncMock(side_effect=AINodeError("Mock offline"))
        server.ai_client.process_remote = AsyncMock(return_value=ToolIntent(type="tool", tool="cube.set_display", arguments={"state": "off"}))
        from features.cube.protocol import Result
        with patch.object(server.control_channel, "request", return_value=Result(session_id=self.session, success=True)) as dispatch, \
             patch.object(server, "authenticate_control"):
            response = fixture.voice("Make the panel dark", {"X-Cube-Client-ID": "pi",
                "X-Cube-Control-Session": self.session, "X-Cube-Request-ID": "b" * 32})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["response"], "Done.")
        dispatch.assert_called_once_with("pi", self.session, "b" * 32, "set_display", state="off")
        request = server.ai_client.process_remote.call_args.args[0]
        self.assertNotIn(self.session, request.model_dump_json())
        self.assertNotIn("b" * 32, request.model_dump_json())
