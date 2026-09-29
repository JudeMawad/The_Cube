"""Private configuration and unchanged Govee dispatch, without live devices."""
import io
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest.mock import Mock, patch

from integrations import govee


SETTINGS = {"sku": "TEST", "devices": {"bulb1": "DEVICE1", "plant": "DEVICE2"},
            "all_spots_group": {"sku": "SameModeGroup", "device": "GROUP"}}


class GoveeTests(unittest.TestCase):
    def test_private_settings_and_invalid_or_missing_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "govee.json"
            with self.assertRaises(govee.GoveeConfigError):
                govee.load_settings(path)
            path.write_text(json.dumps(SETTINGS))
            self.assertEqual(govee.load_settings(path), SETTINGS)
            for value in ({}, [], {**SETTINGS, "devices": {}},
                          {**SETTINGS, "devices": {"all": "DEVICE"}},
                          {**SETTINGS, "all_spots_group": None}):
                path.write_text(json.dumps(value))
                with self.assertRaises(govee.GoveeConfigError):
                    govee.load_settings(path)

    def test_missing_key_raises_regular_configuration_error(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(Path, "home", return_value=Path(directory)):
            with self.assertRaises(govee.GoveeConfigError):
                govee.load_api_key()

    def test_all_color_and_individual_temperature_preserve_payloads(self):
        with patch.object(govee, "load_settings", return_value=SETTINGS), \
                patch.object(govee, "load_api_key", return_value="SYNTHETIC"), \
                patch.object(govee, "send_control") as send:
            govee.control("color", "blue")
            self.assertCountEqual([c.args for c in send.call_args_list], [
                ("SYNTHETIC", "TEST", device, "devices.capabilities.color_setting", "colorRgb", 255)
                for device in ("DEVICE1", "DEVICE2")])
            send.reset_mock()
            govee.control("temperature", 3000, "plant")
            send.assert_called_once_with("SYNTHETIC", "TEST", "DEVICE2",
                                         "devices.capabilities.color_setting", "colorTemperatureK", 3000)

    def test_invalid_actions_never_send(self):
        with patch.object(govee, "send_control") as send:
            for action, value in (("brightness", 0), ("brightness", 101),
                                  ("color", "unknown"), ("temperature", 100), ("unknown", None)):
                with self.assertRaises(ValueError):
                    govee.control(action, value)
            send.assert_not_called()

    def test_upstream_error_does_not_expose_body_or_key(self):
        failure = urllib.error.HTTPError("https://example.invalid", 403, "PRIVATE", {}, io.BytesIO(b"PRIVATE"))
        with patch.object(govee.urllib.request, "urlopen", side_effect=failure):
            with self.assertRaises(govee.GoveeError) as caught:
                govee.send_control("PRIVATE", "TEST", "DEVICE", "type", "instance", 1)
        self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertIn("403", str(caught.exception))

    def test_transport_body_is_preserved(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"code":200}'
        with patch.object(govee.urllib.request, "urlopen", return_value=response) as send:
            govee.send_control("SYNTHETIC", "TEST", "DEVICE", "type", "instance", 1)
        request = send.call_args.args[0]
        payload = json.loads(request.data)["payload"]
        self.assertEqual(payload, {"sku": "TEST", "device": "DEVICE",
                                  "capability": {"type": "type", "instance": "instance", "value": 1}})
