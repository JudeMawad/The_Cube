import unittest
from unittest.mock import Mock

from core.schemas import ToolIntent
from core.tool_registry import ToolContext, ToolRegistry, ToolValidationError
from features.lights import LightsCommands
from features.lights.tools import register_tools


class LightToolTests(unittest.TestCase):
    def setUp(self):
        self.control = Mock()
        self.continuation = Mock(return_value={"listen_for_seconds": 10, "context_expires_in": 40})
        self.registry = ToolRegistry()
        register_tools(self.registry, control_lights=self.control,
                       power_commands=LightsCommands(control_govee=self.control, plugs=Mock()),
                       continuation=self.continuation)

    def test_existing_controller_arguments_and_replies(self):
        for tool, arguments, call, response in [
            ("set_power", {"target": "govee", "state": "on"}, ("on",), "Done."),
            ("set_power", {"target": "govee", "state": "off"}, ("off",), "Done."),
            ("set_brightness", {"percent": 40}, ("brightness", 40), "Brightness set to 40 percent."),
            ("set_color", {"color": "blue"}, ("color", "blue"), "Lights set to blue."),
        ]:
            self.control.reset_mock()
            intent = self.registry.validate_intent(ToolIntent(type="tool", tool="lights." + tool, arguments=arguments))
            result = intent.handler(intent.arguments, ToolContext("pi"))
            self.control.assert_called_once_with(*call)
            self.assertTrue(result.success)
            self.assertEqual(result.data["reply"]["response"], response)
            options = result.data["reply"]["response_options"]
            self.assertEqual(options[0], response)
            self.assertEqual(len(options), 3)
            for text in options:
                self.assertTrue(text.strip())
                self.assertLessEqual(len(text), 500)
                if tool == "set_power":
                    self.assertIn(text, ["Done.", "Okay, done.", "All set."])
                else:
                    self.assertIn(str(call[1]), text)
                    if tool == "set_brightness":
                        self.assertIn("percent", text)
            self.assertEqual(result.data["reply"]["context_expires_in"], 40)
            self.continuation.assert_called_with("pi")

    def test_invalid_values_and_extra_action_fields_never_execute(self):
        cases = [("set_power", {"on": 1}), ("set_power", {"on": True, "target": "other"}),
                 ("set_color", {"color": "invisible"})]
        cases += [("set_brightness", {"percent": value}) for value in [0, 101, True, "50", 1.5]]
        for tool, arguments in cases:
            with self.subTest(arguments=arguments), self.assertRaises(ToolValidationError):
                self.registry.validate_intent(ToolIntent(type="tool", tool="lights." + tool, arguments=arguments))
        self.control.assert_not_called()

    def test_brightness_contract_uses_percent_and_rejects_old_value(self):
        schema = self.registry.get("lights.set_brightness").parameters
        self.assertEqual(schema["required"], ["percent"])
        self.assertNotIn("value", schema["properties"])
        for arguments in ({"value": 40}, {"percent": 40, "value": 40}):
            with self.assertRaises(ToolValidationError):
                self.registry.validate_intent(ToolIntent(type="tool", tool="lights.set_brightness", arguments=arguments))
        self.control.assert_not_called()
