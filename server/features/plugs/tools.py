"""Strict assistant tools and safe wording for logical Smart Life plugs."""

from typing import Literal

from core.response_options import approved_response_options
from core.schemas import ToolDefinition, ToolResult
from core.tool_registry import ToolArguments
from .commands import PlugName


class StateArguments(ToolArguments):
    plug: PlugName


class PowerArguments(StateArguments):
    state: Literal["on", "off"]


BINDINGS = [
    ("plugs.set_power", "Set the named Smart Life plug on or off and check its reported state once: lamp, mirror, or mushroom (mushroom lamp).", PowerArguments),
    ("plugs.get_state", "Read whether the named Smart Life plug is on or off: lamp, mirror, or mushroom (mushroom lamp).", StateArguments),
]


def tool_definitions(*, include_power=True):
    return [ToolDefinition(name=name, description=description, parameters=model.model_json_schema())
            for name, description, model in BINDINGS
            if include_power or name != "plugs.set_power"]


def _response(result):
    plug = result["plug"]
    state = result.get("state")
    if result["success"]:
        return f"The {plug} plug reports that it's {state}."
    if result.get("accepted") is True:
        requested = result["requested_state"]
        if state in {"on", "off"}:
            return (f"The request to turn the {plug} plug {requested} was accepted, "
                    f"but the change is unconfirmed: it still reports {state}.")
        return (f"The request to turn the {plug} plug {requested} was accepted, "
                "but I couldn't confirm its state.")
    reason = result["error"]
    if reason == "command_uncertain":
        return (f"I couldn't confirm whether the {plug} plug accepted the request. "
                "Check its state before trying again.")
    if reason == "plug_offline":
        return f"The {plug} plug is offline."
    if reason == "plug_not_configured":
        return f"The {plug} plug is not configured."
    if reason == "plug_configuration_error":
        return f"I couldn't access the {plug} plug because its configuration needs checking."
    if reason == "plug_unsupported":
        return f"I couldn't safely identify power control for the {plug} plug."
    return f"The {plug} plug is unavailable right now."


def register_tools(registry, *, commands, continuation, include_power=True):
    def bind(name):
        def execute(arguments, context):
            if name == "plugs.set_power":
                result = commands.set_plug_power(arguments.plug, arguments.state == "on")
                action = "plug_power"
            else:
                result = commands.get_plug_state(arguments.plug)
                action = "plug_state"
            # Allowlist Cube facts rather than forwarding arbitrary adapter data.
            reply = {key: result[key] for key in (
                "plug", "state", "requested_state", "online", "accepted", "confirmed",
            ) if key in result}
            for key in ("state", "requested_state"):
                if type(reply.get(key)) is bool:
                    reply[key] = "on" if reply[key] else "off"
            reply["action"] = action
            reply["response"] = _response({**reply, "success": result["success"],
                                           "error": result.get("error")})
            reply.update(continuation(context.client_id))
            reply["response_options"] = approved_response_options(reply["response"])
            return ToolResult(tool=name, success=result["success"], data={"reply": reply},
                              error=result.get("error"))
        return execute

    bindings = [binding for binding in BINDINGS if include_power or binding[0] != "plugs.set_power"]
    for definition, (_, _, model) in zip(tool_definitions(include_power=include_power), bindings):
        registry.register(definition, arguments_model=model, handler=bind(definition.name))
