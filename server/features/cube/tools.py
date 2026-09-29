"""Narrow assistant-facing Cube controls, separate from home lighting."""
from typing import Literal
from pydantic import Field, field_validator
from core.tool_registry import ToolArguments
from core.schemas import ToolDefinition, ToolResult
from core.response_options import approved_response_options


class EmptyArguments(ToolArguments):
    pass


class StateArguments(ToolArguments):
    state: Literal["on", "off"]


class BrightnessArguments(ToolArguments):
    percent: int = Field(ge=0, le=100)


class VolumeArguments(ToolArguments):
    percent: int = Field(ge=0, le=100)


class DeltaArguments(ToolArguments):
    delta: int = Field(ge=-20, le=20)

    @field_validator("delta")
    @classmethod
    def nonzero(cls, value):
        if value == 0:
            raise ValueError("A volume adjustment must be nonzero")
        return value


BINDINGS = [
    ("set_display", "Turn Cube's own display on or off. Cube keeps listening.", StateArguments),
    ("set_brightness", "Set Cube's display brightness directly from 0 to 100 percent.", BrightnessArguments),
    ("shutdown", "Shut down Cube itself when explicitly requested. This does not control lights or plugs.", EmptyArguments),
    ("reboot", "Restart or reboot Cube itself when explicitly requested.", EmptyArguments),
    ("get_status", "Read Cube's display, brightness, uptime and available temperature.", EmptyArguments),
    ("get_volume", "Read the actual volume and mute state of Cube's selected speaker output.", EmptyArguments),
    ("set_volume", "Set Cube's selected speaker output to a whole percentage from 0 to 100; a positive level also unmutes it.", VolumeArguments),
    ("adjust_volume", "Adjust Cube's selected speaker output by signed percentage points from -20 to 20; a positive adjustment also unmutes it.", DeltaArguments),
    ("mute", "Mute Cube's selected speaker output without changing its stored level.", EmptyArguments),
    ("unmute", "Unmute Cube's selected speaker output at its stored level.", EmptyArguments),
]


def response(operation, result):
    if not result["success"]:
        if result["error"] == "cube_unavailable":
            return "I couldn't reach the Cube."
        if result["error"] == "display_unavailable":
            return "I couldn't reach my display."
        if result["error"] == "power_unavailable":
            return "I can't perform that power action right now."
        if result["error"] == "audio_unavailable":
            return "I couldn't access the selected speaker output."
        return "I couldn't complete that Cube control request."
    if operation in {"get_volume", "set_volume", "adjust_volume", "mute", "unmute"}:
        volume = result["volume"]
        if operation == "mute":
            return "Muted."
        if volume["muted"]:
            return "Volume is muted." if operation == "get_volume" else "Volume changed, but still muted."
        levels = volume["channels"]
        if len(set(levels.values())) == 1:
            level = next(iter(levels.values()))
            prefix = "Unmuted. Volume" if operation == "unmute" else "Volume is" if operation == "get_volume" else "Volume"
            return f"{prefix} {level} percent."
        details = ", ".join(f"{name} {level} percent" for name, level in levels.items())
        return ("Unmuted. " if operation == "unmute" else "") + "Volume channels: " + details + "."
    if operation == "shutdown":
        return "Shutting down."
    if operation == "reboot":
        return "Restarting."
    if operation == "get_status":
        status = result["status"]
        if not status["display_available"]:
            return "I'm running, but my display is unavailable."
        display = "on" if status["display_enabled"] else "off"
        return (f"My display is {display}, and brightness is "
                f"{status['master_brightness_percent']} percent.")
    return "Done."


def tool_definitions():
    return [ToolDefinition(name="cube." + operation, description=description,
                           parameters=model.model_json_schema())
            for operation, description, model in BINDINGS]


def register_tools(registry, *, commands):
    def bind(operation):
        def execute(arguments, context):
            result = commands.execute(context, operation, **arguments.model_dump())
            text = response(operation, result)
            reply = {"response": text, "action": "cube_" + operation, "listen_for_seconds": 10 if operation == "adjust_volume" and result["success"] else 0,
                     "response_options": approved_response_options(text)}
            data = {"reply": reply}
            for key in ("status", "volume", "accepted"):
                if key in result:
                    data[key] = result[key]
            return ToolResult(tool="cube." + operation, success=result["success"],
                              error=result.get("error"), data=data)
        return execute

    for definition, (operation, _, model) in zip(tool_definitions(), BINDINGS):
        registry.register(definition, arguments_model=model, handler=bind(operation))
