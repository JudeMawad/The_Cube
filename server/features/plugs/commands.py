"""Logical Cube plug operations; all vendor access belongs to integrations.tuya."""

from threading import Lock
from typing import Literal, get_args

from integrations import tuya


PlugName = Literal["lamp", "mirror", "mushroom"]
PLUG_NAMES = get_args(PlugName)


class PlugArgumentError(ValueError):
    """Invalid feature arguments; messages never echo the supplied value."""


def _validate_plug(plug):
    if not isinstance(plug, str) or plug not in PLUG_NAMES:
        raise PlugArgumentError("Use the plug name lamp, mirror, or mushroom.")


def _failure_reason(error):
    if isinstance(error, tuya.TuyaDeviceNotConfigured):
        return "plug_not_configured"
    if isinstance(error, tuya.TuyaConfigError):
        return "plug_configuration_error"
    if isinstance(error, tuya.TuyaCapabilityError):
        return "plug_unsupported"
    return "plug_unavailable"


class PlugCommands:
    """Reuse one lazy client; serialize reads and complete write/read sequences.

    Results contain only Cube facts. A confirmed write means that one subsequent
    cloud state report matched the request, not independent physical verification.
    """

    def __init__(self):
        self._client = None
        self._lock = Lock()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        with self._lock:
            if self._client is not None:
                client, self._client = self._client, None
                try:
                    client.close()
                except Exception:
                    raise RuntimeError("Could not close the plug connection.") from None

    def _connection(self):
        if self._client is None:
            self._client = tuya.TuyaClient()
        return self._client

    def _check_online(self, plug, result):
        client = self._connection()
        info = client.get_device_info(plug)
        online = getattr(info, "online", None)
        # In particular, 0, None, missing fields, and strings are not offline.
        if online is False:
            result.update(online=False, error="plug_offline")
        elif online is not True:
            result["error"] = "plug_unavailable"
        else:
            result["online"] = True
        return client

    def get_plug_state(self, plug: PlugName) -> dict:
        """Read a configured online plug's latest reported Boolean power state."""
        _validate_plug(plug)
        result = {"success": False, "plug": plug}
        with self._lock:
            try:
                client = self._check_online(plug, result)
                if "error" in result:
                    return result
                state = client.is_on(plug)
                if type(state) is not bool:
                    result["error"] = "plug_unavailable"
                else:
                    result.update(success=True, state=state)
            except Exception as error:
                result["error"] = _failure_reason(error)
        return result

    def set_plug_power(self, plug: PlugName, state: bool, *, confirm: bool = True) -> dict:
        """Request power once; optionally confirm with one read after acceptance."""
        _validate_plug(plug)
        if type(state) is not bool:
            raise PlugArgumentError("Plug power must be True or False.")
        if type(confirm) is not bool:
            raise PlugArgumentError("Confirmation must be True or False.")
        result = {"success": False, "plug": plug, "requested_state": state,
                  "accepted": False, "confirmed": False}
        with self._lock:
            try:
                client = self._check_online(plug, result)
            except Exception as error:
                result["error"] = _failure_reason(error)
                return result
            if "error" in result:
                return result
            try:
                client.set_power(plug, state)
            except tuya.TuyaCommandUncertain:
                result.update(accepted=None, error="command_uncertain")
                return result
            except tuya.TuyaError as error:
                result["error"] = _failure_reason(error)
                return result
            except Exception:
                # An unexpected exception after starting a write can follow an
                # effect. Neither infer rejection nor repeat the operation.
                result.update(accepted=None, error="command_uncertain")
                return result
            result["accepted"] = True
            if not confirm:
                result["success"] = True
                return result
            try:
                observed = client.is_on(plug)
            except Exception:
                observed = None
            if type(observed) is not bool:
                result.update(state=None, error="state_not_confirmed")
            else:
                result["state"] = observed
                if observed is state:
                    result.update(success=True, confirmed=True)
                else:
                    result["error"] = "state_not_confirmed"
        return result
