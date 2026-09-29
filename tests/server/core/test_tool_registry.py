import unittest
from unittest.mock import Mock

from pydantic import BaseModel, ConfigDict, Field

from core.tool_registry import ToolArguments, ToolRegistry, ToolValidationError
from core.schemas import ToolIntent
from features.media import tool_definitions as media_tools
from features.lights import tool_definitions as light_tools


class ToolRegistryTests(unittest.TestCase):
    def test_feature_metadata_aggregates_without_execution(self):
        registry = ToolRegistry()
        definitions = media_tools() + light_tools()
        for definition in definitions:
            registry.register(definition)
        self.assertEqual([tool.name for tool in registry.list_tools()], [tool.name for tool in definitions])
        self.assertIn("confirmation", registry.get("media.cancel_movie").description)
        with self.assertRaises(ValueError):
            registry.register(definitions[0])
        with self.assertRaises(KeyError):
            registry.get("unknown.tool")

    def test_callers_cannot_mutate_registered_metadata(self):
        registry = ToolRegistry()
        definition = light_tools()[0]
        registry.register(definition)
        definition.parameters.clear()
        registry.get(definition.name).parameters.clear()
        registry.list_tools()[0].parameters.clear()
        self.assertIn("properties", registry.get(definition.name).parameters)

    def binding(self):
        class Power(ToolArguments):
            on: bool
        handler = Mock()
        registry = ToolRegistry()
        definition = light_tools()[0]
        registry.register(definition, arguments_model=Power, handler=handler)
        return registry, Power, handler

    def test_executable_schema_comes_from_model_and_validation_does_not_execute(self):
        registry, model, handler = self.binding()
        definition = registry.list_executable_tools()[0]
        self.assertEqual(definition.parameters, model.model_json_schema())
        intent = ToolIntent(type="tool", tool="lights.set_power", arguments={"on": True})
        validated = registry.validate_intent(intent)
        self.assertIsInstance(validated.arguments, model)
        self.assertIs(validated.handler, handler)
        self.assertTrue(validated.arguments.on)
        self.assertEqual(validated.name, "lights.set_power")
        intent.arguments["on"] = False
        self.assertTrue(validated.arguments.on)
        handler.assert_not_called()

    def test_metadata_only_is_not_advertised_or_validated_as_executable(self):
        registry = ToolRegistry()
        registry.register(light_tools()[0])
        self.assertEqual(registry.list_executable_tools(), [])
        with self.assertRaises(ToolValidationError) as caught:
            registry.validate_intent(ToolIntent(type="tool", tool="lights.set_power", arguments={"on": True}))
        self.assertEqual(caught.exception.reason, "not_executable")
        with self.assertRaises(ToolValidationError) as caught:
            registry.validate_intent(ToolIntent(type="tool", tool="unknown", arguments={}))
        self.assertEqual(caught.exception.reason, "unknown_tool")

    def test_strict_missing_wrong_type_and_extra_arguments(self):
        registry, _, handler = self.binding()
        for arguments in [{}, {"on": "true"}, {"on": 1}, {"on": None},
                          {"on": True, "client_id": "SECRET"}, {"on": True, "extra": "SECRET"}]:
            with self.subTest(arguments=arguments), self.assertRaises(ToolValidationError) as caught:
                registry.validate_intent(ToolIntent(type="tool", tool="lights.set_power", arguments=arguments))
            self.assertEqual(caught.exception.reason, "invalid_arguments")
            self.assertNotIn("SECRET", str(caught.exception))
        handler.assert_not_called()

    def test_model_constraints_and_nested_arguments(self):
        class Level(ToolArguments):
            percent: int = Field(ge=1, le=100)
        class Arguments(ToolArguments):
            level: Level
        registry = ToolRegistry()
        handler = Mock()
        registry.register(light_tools()[0], arguments_model=Arguments, handler=handler)
        for level in [{"percent": 0}, {"percent": 101}, {"percent": True},
                      {"percent": "50"}, {"percent": 50, "extra": "SECRET"}]:
            with self.subTest(level=level), self.assertRaises(ToolValidationError):
                registry.validate_intent(ToolIntent(type="tool", tool="lights.set_power",
                                                   arguments={"level": level}))
        actual = registry.validate_intent(ToolIntent(type="tool", tool="lights.set_power",
                                                    arguments={"level": {"percent": 50}}))
        self.assertEqual(actual.arguments.level.percent, 50)
        handler.assert_not_called()

    def test_rejected_registration_is_atomic(self):
        class Power(ToolArguments):
            on: bool
        class Permissive(ToolArguments):
            model_config = ConfigDict(extra="allow")
        class Identity(ToolArguments):
            client_id: str
        async def async_handler(arguments, context):
            pass
        for kwargs in [
            {"arguments_model": Power}, {"handler": Mock()},
            {"arguments_model": BaseModel, "handler": Mock()},
            {"arguments_model": Permissive, "handler": Mock()},
            {"arguments_model": Identity, "handler": Mock()},
            {"arguments_model": Power, "handler": async_handler},
            {"arguments_model": Power, "handler": object()},
        ]:
            registry = ToolRegistry()
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                registry.register(light_tools()[0], **kwargs)
            self.assertEqual(registry.list_tools(), [])
            self.assertEqual(registry.list_executable_tools(), [])
            registry.register(light_tools()[0], arguments_model=Power, handler=Mock())

    def test_executable_metadata_copies_and_duplicates(self):
        registry, model, handler = self.binding()
        registry.list_executable_tools()[0].parameters.clear()
        registry.get("lights.set_power").parameters.clear()
        self.assertEqual(registry.list_executable_tools()[0].parameters, model.model_json_schema())
        replacement = Mock()
        with self.assertRaises(ValueError):
            registry.register(light_tools()[0], arguments_model=model, handler=replacement)
        validated = registry.validate_intent(ToolIntent(type="tool", tool="lights.set_power", arguments={"on": False}))
        self.assertIs(validated.handler, handler)
        replacement.assert_not_called()

    def test_local_only_tool_remains_validated_but_is_not_advertised(self):
        class Empty(ToolArguments):
            pass
        registry = ToolRegistry()
        definition = light_tools()[0]
        handler = Mock()
        registry.register(definition, arguments_model=Empty, handler=handler, ai_visible=False)
        self.assertEqual(registry.list_executable_tools(), [])
        self.assertEqual(len(registry.list_tools()), 1)
        tool = registry.validate_intent(ToolIntent(type="tool", tool=definition.name, arguments={}))
        self.assertIs(tool.handler, handler)
        handler.assert_not_called()
