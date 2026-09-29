import unittest
from unittest.mock import Mock

from features.lights import handle_command


class LightsTests(unittest.TestCase):
    def test_existing_commands_and_boundaries(self):
        for text, action, args in [
            ("Lights on!", "lights_on", ("on",)),
            ("switch the bulbs off", "lights_off", ("off",)),
            ("spots 1%", "lights_brightness", ("brightness", 1)),
            ("lights 100 percent", "lights_brightness", ("brightness", 100)),
            ("make the lights blue", "lights_color", ("color", "blue")),
        ]:
            with self.subTest(text=text):
                control = Mock()
                self.assertEqual(handle_command(text, control_lights=control)["action"], action)
                control.assert_called_once_with(*args)

    def test_unhandled_input_and_invalid_brightness_do_nothing(self):
        for text in ["hello", "blue", "lights 0%", "lights 101 percent", "volume 50 percent"]:
            control = Mock()
            self.assertIsNone(handle_command(text, control_lights=control))
            control.assert_not_called()

    def test_controller_failure_propagates_to_pipeline(self):
        with self.assertRaises(RuntimeError):
            handle_command("lights on", control_lights=Mock(side_effect=RuntimeError("offline")))
