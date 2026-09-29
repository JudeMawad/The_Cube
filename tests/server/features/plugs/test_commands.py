from concurrent.futures import ThreadPoolExecutor
import importlib.util
from pathlib import Path
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from features.plugs import PlugArgumentError, PlugCommands
from integrations import tuya


PRIVATE = "PRIVATE_DEVICE_SECRET_TOKEN_SENTINEL"


class PlugCommandsTests(unittest.TestCase):
    def setUp(self):
        client_type = tuya.TuyaClient
        factory = patch("integrations.tuya.TuyaClient")
        self.factory = factory.start()
        self.addCleanup(factory.stop)
        self.client = Mock(spec=client_type)
        self.factory.return_value = self.client
        self.client.get_device_info.return_value = SimpleNamespace(
            id=PRIVATE, name=PRIVATE, category=PRIVATE, online=True)
        self.client.is_on.return_value = True
        self.client.set_power.return_value = None
        self.commands = PlugCommands()
        self.addCleanup(self.commands.close)
        guard = patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    def test_valid_aliases_query_and_reuse_client(self):
        for plug in ("lamp", "mirror", "mushroom"):
            result = self.commands.get_plug_state(plug)
            self.assertEqual(result, {"success": True, "plug": plug, "online": True, "state": True})
            self.client.get_device_info.assert_called_with(plug)
            self.client.is_on.assert_called_with(plug)
        self.factory.assert_called_once_with()
        self.client.set_power.assert_not_called()

    def test_invalid_alias_and_power_rejected_before_client_creation(self):
        for alias in ("Lamp", " lamp", "lamp ", "all", "unknown", PRIVATE, "", None, [], 1):
            for operation in (self.commands.get_plug_state,
                              lambda plug: self.commands.set_plug_power(plug, True)):
                with self.subTest(alias=alias), self.assertRaises(PlugArgumentError) as caught:
                    operation(alias)
                self.assertNotIn(PRIVATE, str(caught.exception))
        for state in ("on", "off", 0, 1, None, [], {}):
            with self.subTest(state=state), self.assertRaises(PlugArgumentError):
                self.commands.set_plug_power("lamp", state)
        self.factory.assert_not_called()

    def test_on_and_off_mapping_and_delegation(self):
        for state in (True, False):
            self.client.reset_mock()
            self.client.is_on.return_value = state
            result = self.commands.set_plug_power("lamp", state)
            self.assertEqual(result, {"success": True, "plug": "lamp", "online": True,
                                     "requested_state": state, "state": state,
                                     "accepted": True, "confirmed": True})
            self.assertEqual([call[0] for call in self.client.mock_calls],
                             ["get_device_info", "set_power", "is_on"])
            self.client.set_power.assert_called_once_with("lamp", state)
            self.client.is_on.assert_called_once_with("lamp")
            self.assertNotIn(PRIVATE, str(result))

    def test_state_query_false(self):
        self.client.is_on.return_value = False
        self.assertIs(self.commands.get_plug_state("lamp")["state"], False)

    def test_only_boolean_false_is_offline(self):
        self.client.get_device_info.return_value.online = False
        for operation in (lambda: self.commands.get_plug_state("lamp"),
                          lambda: self.commands.set_plug_power("lamp", True)):
            result = operation()
            self.assertFalse(result["success"])
            self.assertEqual(result["error"], "plug_offline")
            self.assertIs(result["online"], False)
            self.assertNotIn("state", result)
        self.client.is_on.assert_not_called()
        self.client.set_power.assert_not_called()

    def test_missing_or_malformed_online_is_unavailable(self):
        for info in (None, {}, SimpleNamespace(), *[SimpleNamespace(online=value)
                     for value in (None, 0, 1, "false", "true", "", [], {})]):
            self.client.get_device_info.return_value = info
            for operation in (lambda: self.commands.get_plug_state("lamp"),
                              lambda: self.commands.set_plug_power("lamp", True)):
                with self.subTest(info=info):
                    result = operation()
                    self.assertFalse(result["success"])
                    self.assertEqual(result["error"], "plug_unavailable")
                    self.assertNotIn("online", result)
        self.client.is_on.assert_not_called()
        self.client.set_power.assert_not_called()

    def test_unconfigured_alias_does_not_disable_other_plugs(self):
        online = self.client.get_device_info.return_value
        self.client.get_device_info.side_effect = lambda plug: (
            online if plug != "mirror" else self.missing())
        result = self.commands.get_plug_state("mirror")
        self.assertEqual(result["error"], "plug_not_configured")
        self.assertTrue(self.commands.get_plug_state("lamp")["success"])
        self.assertTrue(self.commands.get_plug_state("mushroom")["success"])
        self.assertNotIn(PRIVATE, str(result))

    @staticmethod
    def missing():
        raise tuya.TuyaDeviceNotConfigured(PRIVATE)

    def test_integration_errors_mapped_without_vendor_text(self):
        cases = [(tuya.TuyaDeviceNotConfigured(PRIVATE), "plug_not_configured"),
                 (tuya.TuyaConfigError(PRIVATE), "plug_configuration_error"),
                 (tuya.TuyaCapabilityError(PRIVATE), "plug_unsupported"),
                 (tuya.TuyaTransportError(), "plug_unavailable"),
                 (tuya.TuyaAuthError(1004), "plug_unavailable"),
                 (tuya.TuyaAPIError(1106), "plug_unavailable"),
                 (tuya.TuyaResponseError(PRIVATE), "plug_unavailable"),
                 (RuntimeError(PRIVATE), "plug_unavailable")]
        for error, reason in cases:
            self.client.get_device_info.side_effect = error
            for operation in (lambda: self.commands.get_plug_state("lamp"),
                              lambda: self.commands.set_plug_power("lamp", True)):
                with self.subTest(error=type(error)):
                    result = operation()
                    self.assertFalse(result["success"])
                    self.assertEqual(result["error"], reason)
                    self.assertNotIn(PRIVATE, str(result))
                    self.assertNotIn("1106", str(result))
        self.client.set_power.assert_not_called()

    def test_failed_state_read_and_malformed_power_are_unavailable(self):
        self.client.is_on.side_effect = tuya.TuyaResponseError(PRIVATE)
        self.assertEqual(self.commands.get_plug_state("lamp")["error"], "plug_unavailable")
        self.client.is_on.side_effect = None
        for state in (None, 0, 1, "on", "off", [], {}):
            self.client.is_on.return_value = state
            self.assertEqual(self.commands.get_plug_state("lamp")["error"], "plug_unavailable")

    def test_accepted_mismatch_is_unconfirmed_without_retry(self):
        for requested in (True, False):
            self.client.reset_mock()
            self.client.is_on.return_value = not requested
            result = self.commands.set_plug_power("lamp", requested)
            self.assertFalse(result["success"])
            self.assertIs(result["accepted"], True)
            self.assertIs(result["confirmed"], False)
            self.assertIs(result["state"], not requested)
            self.assertEqual(result["error"], "state_not_confirmed")
            self.client.set_power.assert_called_once_with("lamp", requested)
            self.client.is_on.assert_called_once_with("lamp")

    def test_accepted_failed_confirmation_preserves_acceptance(self):
        for error in (tuya.TuyaTransportError(), tuya.TuyaConfigError(PRIVATE), RuntimeError(PRIVATE)):
            self.client.reset_mock()
            self.client.is_on.side_effect = error
            result = self.commands.set_plug_power("lamp", True)
            self.assertFalse(result["success"])
            self.assertIs(result["accepted"], True)
            self.assertIs(result["confirmed"], False)
            self.assertIsNone(result["state"])
            self.assertEqual(result["error"], "state_not_confirmed")
            self.assertNotIn(PRIVATE, str(result))
            self.client.set_power.assert_called_once()
            self.client.is_on.assert_called_once()

    def test_accepted_malformed_confirmation_cannot_succeed(self):
        for state in (None, 0, 1, "true", "false", [], {}):
            self.client.is_on.return_value = state
            result = self.commands.set_plug_power("lamp", True)
            self.assertEqual((result["success"], result["accepted"], result["confirmed"]),
                             (False, True, False))
            self.assertIsNone(result["state"])

    def test_uncertain_write_has_no_retry_or_confirmation_read(self):
        for error in (tuya.TuyaCommandUncertain(PRIVATE), RuntimeError(PRIVATE)):
            self.client.reset_mock()
            self.client.set_power.side_effect = error
            result = self.commands.set_plug_power("lamp", True)
            self.assertEqual(result["error"], "command_uncertain")
            self.assertFalse(result["success"])
            self.assertIsNone(result["accepted"])
            self.assertIs(result["confirmed"], False)
            self.client.set_power.assert_called_once()
            self.client.is_on.assert_not_called()
            self.assertNotIn(PRIVATE, str(result))

    def test_rejected_write_has_no_confirmation_read(self):
        self.client.set_power.side_effect = tuya.TuyaAPIError(1106)
        result = self.commands.set_plug_power("lamp", False)
        self.assertFalse(result["success"])
        self.assertIs(result["accepted"], False)
        self.assertIs(result["confirmed"], False)
        self.client.set_power.assert_called_once()
        self.client.is_on.assert_not_called()

    def test_lazy_creation_and_context_cleanup(self):
        self.factory.assert_not_called()
        with self.commands:
            self.commands.get_plug_state("lamp")
            self.commands.get_plug_state("mirror")
        self.factory.assert_called_once()
        self.client.close.assert_called_once()
        self.commands.close()
        self.client.close.assert_called_once()

    def test_cleanup_after_failures(self):
        self.client.get_device_info.side_effect = RuntimeError(PRIVATE)
        with self.commands:
            self.assertFalse(self.commands.get_plug_state("lamp")["success"])
        self.client.close.assert_called_once()

    def test_command_and_confirmation_cannot_interleave(self):
        entered, release, second_started = Event(), Event(), Event()
        events = []
        def write(plug, state):
            events.append((plug, "write"))
            if plug == "lamp":
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test timeout")
        def read(plug):
            events.append((plug, "read"))
            return True
        def second():
            second_started.set()
            return self.commands.set_plug_power("mirror", True)
        self.client.set_power.side_effect = write
        self.client.is_on.side_effect = read
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.commands.set_plug_power, "lamp", True)
            try:
                self.assertTrue(entered.wait(5))
                other = pool.submit(second)
                self.assertTrue(second_started.wait(5))
            finally:
                release.set()
            self.assertTrue(first.result()["success"])
            self.assertTrue(other.result()["success"])
        self.assertEqual(events, [("lamp", "write"), ("lamp", "read"),
                                  ("mirror", "write"), ("mirror", "read")])

    def test_tuya_missing_mapping_subtype_retains_compatibility(self):
        # Exercise the real integration's resolver with synthetic settings only.
        from integrations.tuya import Settings
        real_type = self.client._spec_class
        with patch.object(Settings, "load", return_value=Settings("fake", "fake", "https://fake.invalid", {})):
            with real_type() as client:
                with self.assertRaises(tuya.TuyaDeviceNotConfigured) as caught:
                    client.resolve_device("lamp")
        self.assertIsInstance(caught.exception, tuya.TuyaConfigError)

    def test_module_import_and_construction_do_not_create_client(self):
        from features.plugs import commands
        spec = importlib.util.spec_from_file_location("plug_import_check", commands.__file__)
        module = importlib.util.module_from_spec(spec)
        with patch.object(Path, "read_text", side_effect=AssertionError("No config read")):
            spec.loader.exec_module(module)
            module.PlugCommands().close()
        self.factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
