"""Unified logical lighting power; vendor operations remain in their adapters."""

from typing import Literal, get_args

from core.response_options import approved_response_options
from integrations.govee import GoveeConfigError


LightTarget = Literal["all", "govee", "tuya", "lamp", "mirror", "mushroom"]
_PLUGS = ("lamp", "mirror", "mushroom")
_ERRORS = {
    "plug_not_configured": "unconfigured",
    "plug_configuration_error": "configuration_error",
    "plug_offline": "offline",
    "plug_unsupported": "unsupported",
    "plug_unavailable": "unavailable",
    "command_uncertain": "uncertain",
}


class LightsCommands:
    """Orchestrate independent operations using injected, shared controllers."""

    def __init__(self, *, control_govee, plugs):
        self._control_govee = control_govee
        self._plugs = plugs

    def _govee(self, action):
        try:
            self._control_govee(action)
        except (GoveeConfigError, SystemExit):
            # Preserve containment for injected/older adapters as well.
            return {"success": False, "accepted": False, "error": "configuration_error"}
        except Exception:
            # A transport error can follow an effect; do not infer rejection.
            return {"success": False, "accepted": None, "error": "unavailable"}
        return {"success": True, "accepted": True}

    def _plug(self, target, state):
        try:
            result = self._plugs.set_plug_power(target, state, confirm=False)
        except Exception:
            return {"success": False, "accepted": None, "error": "uncertain"}
        if not isinstance(result, dict):
            return {"success": False, "accepted": None, "error": "uncertain"}
        accepted = result.get("accepted")
        if accepted is not True and accepted is not False:
            accepted = None
        if result.get("success") is True and accepted is True:
            return {"success": True, "accepted": True}
        reason = result.get("error")
        reason = _ERRORS.get(reason, "unavailable") if isinstance(reason, str) else "unavailable"
        return {"success": False, "accepted": accepted, "error": reason}

    def set_power(self, target: LightTarget, state: bool) -> dict:
        if not isinstance(target, str) or target not in get_args(LightTarget):
            raise ValueError("Unknown lighting target.")
        if type(state) is not bool:
            raise ValueError("Lighting power must be True or False.")
        selected = (("govee", *_PLUGS) if target == "all" else
                    _PLUGS if target == "tuya" else (target,))
        action = "on" if state else "off"
        devices = {}
        for name in selected:
            devices[name] = self._govee(action) if name == "govee" else self._plug(name, state)
        succeeded = sum(item["success"] for item in devices.values())
        return {"target": target, "requested_state": action,
                "success": succeeded == len(devices), "partial": 0 < succeeded < len(devices),
                "devices": devices}


def power_reply(result):
    """Shared safe reply for tool execution and the existing generic fallback."""
    if result["success"]:
        response = "Done."
    else:
        failures = []
        for name, outcome in result["devices"].items():
            if outcome["success"]:
                continue
            label = "the Govee lights" if name == "govee" else f"the {name}"
            reason = outcome["error"]
            if reason == "offline" and result["partial"]:
                clause = f"the {name} light is offline"
            elif reason == "unconfigured":
                clause = f"{label} is not configured"
            elif reason == "configuration_error":
                clause = f"I couldn't access {label}; its configuration needs checking"
            elif reason == "unsupported":
                clause = f"I couldn't safely control {label}"
            elif reason == "uncertain":
                clause = f"I couldn't confirm the outcome for {label}"
            else:
                clause = f"I couldn't reach {label}"
            failures.append(clause)
        detail = "; ".join(failures)
        response = "Done, but " + detail + "." if result["partial"] else detail[:1].upper() + detail[1:] + "."
    return {**result, "action": "lights_" + result["requested_state"], "response": response,
            "response_options": approved_response_options(
                response, ("Okay, done.", "All set.") if result["success"] else ()),
            **({"error": "partial_failure" if result["partial"] else "lighting_failed"}
               if not result["success"] else {})}
