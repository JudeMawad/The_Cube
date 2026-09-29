"""Voice-facing Govee actions; transport and dispatch belong to the adapter."""
from integrations.govee import control


def control_lights(action: str, value=None, target: str = "all") -> None:
    if action.lower() not in {"on", "off", "brightness", "color"}:
        raise ValueError("Unknown action.")
    control(action, value, target)
