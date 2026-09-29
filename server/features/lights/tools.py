"""Strict light bindings over the existing controller."""
from typing import Literal

from pydantic import Field

from core.schemas import ToolDefinition, ToolResult
from core.response_options import approved_response_options
from core.tool_registry import ToolArguments
from .power import LightTarget, power_reply


class PowerArguments(ToolArguments):
    target: LightTarget
    state: Literal["on", "off"]


class BrightnessArguments(ToolArguments):
    percent: int = Field(ge=1, le=100)


class ColorArguments(ToolArguments):
    color: Literal["red", "green", "blue", "white", "yellow", "orange", "purple", "pink", "cyan"]


BINDINGS = [
    ("lights.set_power", "Turn lights on/off. 'The lights' or 'all lights' means all; Tuya/Smart Life lights means tuya; Govee lights means govee; mushroom lamp means mushroom. Lamp and mirror select only that light.", PowerArguments),
    ("lights.set_brightness", "Set all configured Govee lights to 1–100 percent brightness.", BrightnessArguments),
    ("lights.set_color", "Set all configured Govee lights to a supported color.", ColorArguments),
]


def tool_definitions():
    return [ToolDefinition(name=name, description=description, parameters=model.model_json_schema())
            for name, description, model in BINDINGS]


def register_tools(registry, *, control_lights, power_commands, continuation):
    def bind(name):
        def execute(arguments, context):
            if name == "lights.set_power":
                reply = power_reply(power_commands.set_power(arguments.target, arguments.state == "on"))
                alternatives = reply["response_options"][1:]
            elif name == "lights.set_brightness":
                control_lights("brightness", arguments.percent)
                reply = {"action": "lights_brightness", "value": arguments.percent,
                         "response": f"Brightness set to {arguments.percent} percent."}
                alternatives = [f"The lights are at {arguments.percent} percent brightness.",
                                f"I've set the brightness to {arguments.percent} percent."]
            else:
                control_lights("color", arguments.color)
                reply = {"action": "lights_color", "value": arguments.color,
                         "response": f"Lights set to {arguments.color}."}
                alternatives = [f"The lights are {arguments.color} now.",
                                f"I've changed the lights to {arguments.color}."]
            reply.update(continuation(context.client_id))
            reply["response_options"] = approved_response_options(reply["response"], alternatives)
            return ToolResult(tool=name, success=reply.get("success", True), data={"reply": reply},
                              error=reply.get("error"))
        return execute
    for definition, (_, _, model) in zip(tool_definitions(), BINDINGS):
        registry.register(definition, arguments_model=model, handler=bind(definition.name))
